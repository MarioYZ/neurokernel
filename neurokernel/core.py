#!/usr/bin/env python

"""
Core Neurokernel classes.

Notes
-----
All major object instances are assigned UIDs using
Python's builtin id() function. Since these object instance IDs are
only unique for instantiated objects, generated unique identifiers
should eventually used if the dynamic creation and destruction of
modules must eventually be supported.

"""

from contextlib import contextmanager
import copy
import cPickle as pickle
import multiprocessing as mp
import os
import signal
import sys
import threading
import time

import bidict
import numpy as np
import twiggy
import zmq
from zmq.eventloop.ioloop import IOLoop
from zmq.eventloop.zmqstream import ZMQStream

from ctrl_proc import ControlledProcess, LINGER_TIME
from ctx_managers import IgnoreKeyboardInterrupt, OnKeyboardInterrupt, \
     ExceptionOnSignal, TryExceptionOnSignal
from neurokernel.tools.comm_utils import is_poll_in
from routing_table import RoutingTable
from uid import uid

PORT_DATA = 5000
PORT_CTRL = 5001

class BaseModule(ControlledProcess):
    """
    Processing module.

    This class repeatedly executes a work method until it receives a
    quit message via its control port.

    Parameters
    ----------
    net: str
        Network connectivity. May be `unconnected` for no connection,
        `ctrl` for incoming control data only,
        `in` for incoming data, `out` for outgoing data, or
        `full` for both incoming and outgoing data.
    port_data : int
        Port to use when communicating with broker.
    port_ctrl : int
        Port used by broker to control module.

    Attributes
    ----------
    in_data : list of tuples
       In-bound data received from other modules; each tuple contains
       the ID of the destination module and another data structure
       containing the actual data.
    in_ids : list of int
       List of source module IDs.
    out_data : list of tuples
       Out-bound data generated by the module instance; each tuple
       contains the ID of the destination module and another data structure
       containing the actual data.
    out_ids : list of int
       List of destination module IDs.

    Methods
    -------
    run()
        Body of process.
    run_step(data)
        Processes the specified data and returns a result for
        transmission to other modules.

    Notes
    -----
    If the ports specified upon instantiation are None, the module
    instance ignores the network entirely.

    Children of the BaseModule class should also contain attributes containing
    the connectivity objects.

    """

    # Define properties to perform validation when connectivity status
    # is set:
    _net = 'unconnected'
    @property
    def net(self):
        """
        Network connectivity.
        """
        return self._net
    @net.setter
    def net(self, value):
        if value not in ['unconnected', 'ctrl', 'in', 'out', 'full']:
            raise ValueError('invalid network connectivity value')
        self.logger.info('net status changed: %s -> %s' % (self._net, value))
        self._net = value

    def __init__(self, net='unconnected',
                 port_data=PORT_DATA, port_ctrl=PORT_CTRL):
        super(BaseModule, self).__init__(port_ctrl, signal.SIGUSR1)

        # Logging:
        self.logger = twiggy.log.name('module %s' % self.id)

        # Network connection type:
        self.net = net

        # Data port:
        if port_data == port_ctrl:
            raise ValueError('data and control ports must differ')
        self.port_data = port_data

        # Flag indicating when the module instance is running:
        self.running = False

        # Lists used for storing incoming and outgoing data; each
        # entry is a tuple whose first entry is the source or destination
        # module ID and whose second entry is the data:
        self.in_data = []
        self.out_data = []

        # Lists of incoming and outgoing module IDs; these should be populated
        # when a module instance is connected to another instance:
        self.in_ids = []
        self.out_ids = []

        # Children of the BaseModule class should also contain a dictionary (or
        # multiple dictionaries) of connectivity objects describing incoming
        # connections to the module instance.
        
    def _ctrl_handler(self, msg):
        """
        Control port handler.
        """

        self.logger.info('recv: %s' % str(msg))
        if msg[0] == 'quit':
            try:
                self.stream_ctrl.flush()
                self.stream_ctrl.stop_on_recv()
                self.ioloop_ctrl.stop()
            except IOError:
                self.logger.info('streams already closed')
            except:
                self.logger.info('other error occurred')
            self.logger.info('issuing signal %s' % self.quit_sig)
            self.sock_ctrl.send('ack')
            self.logger.info('sent to manager: ack')
            os.kill(os.getpid(), self.quit_sig)
        # One can define additional messages to be recognized by the control handler:
        # elif msg[0] == 'conn':
        #     self.logger.info('conn payload: '+str(pickle.loads(msg[1])))
        #     self.sock_ctrl.send('ack')
        #     self.logger.info('sent ack') 
        else:
            self.sock_ctrl.send('ack')
            self.logger.info('sent ack')            
            
    def _init_net(self):
        """
        Initialize network connection.
        """

        if self.net == 'unconnected':
            self.logger.info('not initializing network connection')
        else:

            # Don't allow interrupts to prevent the handler from
            # completely executing each time it is called:
            with IgnoreKeyboardInterrupt():
                self.logger.info('initializing network connection')

                # Initialize control port handler:
                super(BaseModule, self)._init_net()

                # Use a nonblocking port for the data interface; set
                # the linger period to prevent hanging on unsent
                # messages when shutting down:
                self.sock_data = self.zmq_ctx.socket(zmq.DEALER)
                self.sock_data.setsockopt(zmq.IDENTITY, self.id)
                self.sock_data.setsockopt(zmq.LINGER, LINGER_TIME)
                self.sock_data.connect("tcp://localhost:%i" % self.port_data)
                self.logger.info('network connection initialized')
                
    def _sync(self):
        """
        Send output data and receive input data.
            
        Notes
        -----
        Assumes that the attributes used for input and output already
        exist.

        Each message is a tuple containing a module ID and data; for
        outbound messages, the ID is that of the destination module.
        for inbound messages, the ID is that of the source module.
        Data is serialized before being sent and unserialized when
        received.

        """

        if self.net in ['unconnected', 'ctrl']:
            self.logger.info('not synchronizing with network')
            if self.net == 'ctrl' and not self.running:
                return
        else:
            self.logger.info('synchronizing with network')

            # Send all outbound data:
            if self.net in ['out', 'full']:
                ## should check to make sure that out_data contains
                ## entries for all IDs in self.out_ids
                for out_id, data in self.out_data:
                    self.sock_data.send(pickle.dumps((out_id, data)))
                    self.logger.info('sent to   %s: %s' % (out_id, str(data)))
                self.logger.info('sent data to all output IDs')

            # Wait until inbound data is received from all source modules:
            if self.net in ['in', 'full']:
                recv_ids = copy.copy(self.in_ids)
                self.in_data = []
                while recv_ids:
                    in_id, data = pickle.loads(self.sock_data.recv())
                    self.logger.info('recv from %s: %s ' % (in_id, str(data)))
                    recv_ids.remove(in_id)
                    self.in_data.append((in_id, data))
                self.logger.info('recv data from all input IDs')

    def run_step(self, *args, **kwargs):
        """
        Perform a single step of computation.
        
        This method should be implemented to do something interesting with its
        arguments. It should not interact with any other class attributes.

        """

        self.logger.info('running execution step')

    def run(self):
        """
        Body of process.
        """

        with TryExceptionOnSignal(self.quit_sig, Exception, self.id):

            # Don't allow keyboard interruption of process:
            self.logger.info('starting')
            with IgnoreKeyboardInterrupt():

                self._init_net()
                self.running = True

                np.random.seed()
                while True:

                    # Run the processing step:
                    self.run_step()

                    # Move data created by run_step to self.out_data:
                    # (this example populates the latter with random data):
                    if self.net in ['out', 'full']:
                        self.out_data = []
                        for i in self.out_ids:
                            self.out_data.append((i, str(np.random.rand())))

                    # Synchronize:
                    self._sync()

            self.logger.info('exiting')

class Broker(ControlledProcess):
    """
    Broker for communicating between modules.

    Parameters
    ----------
    port_data : int
        Port to use for communication with modules.
    port_ctrl : int
        Port used to control modules.

    Methods
    -------
    run()
        Body of process.
    sync()
        Synchronize with network.

    """

    def __init__(self, port_data=PORT_DATA, port_ctrl=PORT_CTRL,
                 routing_table=None):
        super(Broker, self).__init__(port_ctrl, signal.SIGUSR1)

        # Logging:
        self.logger = twiggy.log.name('broker %s' % self.id)

        # Data port:
        if port_data == port_ctrl:
            raise ValueError('data and control ports must differ')
        self.port_data = port_data

        # Routing table:
        self.routing_table = routing_table

        # Buffers used to accumulate data to route:
        self.data_to_route = []
        self.recv_coords_list = routing_table.coords

    def _ctrl_handler(self, msg):
        """
        Control port handler.
        """

        self.logger.info('recv: '+str(msg))
        if msg[0] == 'quit':
            try:
                self.stream_ctrl.flush()
                self.stream_data.flush()
                self.stream_ctrl.stop_on_recv()
                self.stream_data.stop_on_recv()
                self.ioloop.stop()
            except IOError:
                self.logger.info('streams already closed')
            except Exception as e:
                self.logger.info('other error occurred: '+e.message)
            self.sock_ctrl.send('ack')
            self.logger.info('sent to  broker: ack')
            # For some reason, the following lines cause problems:
            # self.logger.info('issuing signal %s' % self.quit_sig)
            # os.kill(os.getpid(), self.quit_sig)
            
    def _data_handler(self, msg):
        """
        Data port handler.

        Notes
        -----
        Assumes that each message contains a source module ID
        (provided by zmq) and a pickled tuple; the tuple contains
        the destination module ID and the data to be transmitted.

        """

        if len(msg) != 2:
            self.logger.info('skipping malformed message: %s' % str(msg))
        else:

            # When a message arrives, remove its source ID from the
            # list of source modules from which data is expected:
            in_id = msg[0]
            out_id, data = pickle.loads(msg[1])
            self.logger.info('recv from %s: %s' % (in_id, data))
            self.logger.info('recv coords list len: '+ str(len(self.recv_coords_list)))
            if (in_id, out_id) in self.recv_coords_list:
                self.data_to_route.append((in_id, out_id, data))
                self.recv_coords_list.remove((in_id, out_id))

            # When data with source/destination IDs corresponding to
            # every entry in the routing table has been received,
            # deliver the data:
            if not self.recv_coords_list:
                self.logger.info('recv from all modules')
                for in_id, out_id, data in self.data_to_route:
                    self.logger.info('sent to   %s: %s' % (out_id, data))

                    # Route to the destination ID and send the source ID
                    # along with the data:
                    self.sock_data.send_multipart([out_id,
                                                   pickle.dumps((in_id, data))])

                # Reset the incoming data buffer and list of connection
                # coordinates:
                self.data_to_route = []
                self.recv_coords_list = self.routing_table.coords
                self.logger.info('----------------------')

    def _init_ctrl_handler(self):
        """
        Initialize control port handler.
        """

        # Set the linger period to prevent hanging on unsent messages
        # when shutting down:
        self.logger.info('initializing ctrl handler')
        self.sock_ctrl = self.zmq_ctx.socket(zmq.DEALER)
        self.sock_ctrl.setsockopt(zmq.IDENTITY, self.id)
        self.sock_ctrl.setsockopt(zmq.LINGER, LINGER_TIME)
        self.sock_ctrl.connect('tcp://localhost:%i' % self.port_ctrl)

        self.stream_ctrl = ZMQStream(self.sock_ctrl, self.ioloop)
        self.stream_ctrl.on_recv(self._ctrl_handler)

    def _init_data_handler(self):
        """
        Initialize data port handler.
        """

        # Set the linger period to prevent hanging on unsent
        # messages when shutting down:
        self.logger.info('initializing data handler')
        self.sock_data = self.zmq_ctx.socket(zmq.ROUTER)
        self.sock_data.setsockopt(zmq.LINGER, LINGER_TIME)
        self.sock_data.bind("tcp://*:%i" % self.port_data)

        self.stream_data = ZMQStream(self.sock_data, self.ioloop)
        self.stream_data.on_recv(self._data_handler)

    def _init_net(self):
        """
        Initialize the network connection.
        """

        # Since the broker must behave like a reactor, the event loop
        # is started in the main thread:
        self.zmq_ctx = zmq.Context()
        self.ioloop = IOLoop.instance()
        self._init_ctrl_handler()
        self._init_data_handler()
        self.ioloop.start()

    def run(self):
        """
        Body of process.
        """

        with TryExceptionOnSignal(self.quit_sig, Exception, self.id):
            self.recv_coords_list = self.routing_table.coords
            self._init_net()
        self.logger.info('exiting')

class BaseConnectivity(object):
    """
    Intermodule connectivity class.

    """

    def __init__(self):

        # Unique object ID:
        self.id = uid()

class BaseManager(object):
    """
    Module manager.

    Parameters
    ----------
    port_data : int
        Port to use for communication with modules.
    port_ctrl : int
        Port used to control modules.

    """

    def __init__(self, port_data=PORT_DATA, port_ctrl=PORT_CTRL):

        # Unique object ID:
        self.id = uid()

        self.logger = twiggy.log.name('manage %s' % self.id)
        self.port_data = port_data
        self.port_ctrl = port_ctrl

        # Set up a router socket to communicate with other topology
        # components; linger period is set to 0 to prevent hanging on
        # unsent messages when shutting down:
        self.zmq_ctx = zmq.Context()
        self.sock_ctrl = self.zmq_ctx.socket(zmq.ROUTER)
        self.sock_ctrl.setsockopt(zmq.LINGER, LINGER_TIME)
        self.sock_ctrl.bind("tcp://*:%i" % self.port_ctrl)

        # Data structures for storing broker, module, and connectivity instances:
        self.brok_dict = bidict.bidict()
        self.mod_dict = bidict.bidict()
        self.conn_dict = bidict.bidict()

        # Set up a dynamic table to contain the routing table:
        self.routing_table = RoutingTable()

    def connect(self, m_src, m_dest, conn):
        """
        Connect two module instances with a connectivity object instance.

        Parameters
        ----------
        m_src : BaseModule
           Source module instance.
        m_dest : BaseModule
           Destination module instance.
        conn : BaseConnectivity
           Connectivity object instance.

        """

        if not isinstance(m_src, BaseModule) or not isinstance(m_dest, BaseModule) or \
            not isinstance(conn, BaseConnectivity):
            raise ValueError('invalid types')

        # Add the module and connection instances to the internal
        # dictionaries of the manager instance if they are not already there:
        if m_src.id not in self.mod_dict:
            self.add_mod(m_src)
        if m_dest.id not in self.mod_dict:
            self.add_mod(m_dest)
        if conn.id not in self.conn_dict:
            self.add_conn(conn)

        # Add the connection to the routing table:
        self.routing_table[m_src.id, m_dest.id] = 1

        # Update the network connectivity of the source and
        # destination module instances if necessary:
        if m_src.net == 'unconnected':
            m_src.net = 'out'
        if m_src.net == 'in':
            m_src.net = 'full'
        if m_dest.net == 'unconnected':
            m_dest.net = 'in'
        if m_dest.net == 'out':
            m_dest.net = 'full'

        # Update each module's lists of incoming and outgoing modules:
        m_src.in_ids = self.routing_table.row_ids(m_src.id)
        m_src.out_ids = self.routing_table.col_ids(m_src.id)
        m_dest.in_ids = self.routing_table.row_ids(m_dest.id)
        m_dest.out_ids = self.routing_table.col_ids(m_dest.id)

    @property
    def N_brok(self):
        """
        Number of brokers.
        """
        return len(self.brok_dict)

    @property
    def N_mod(self):
        """
        Number of modules.
        """
        return len(self.mod_dict)

    @property
    def N_conn(self):
        """
        Number of connectivity objects.
        """

        return len(self.conn_dict)

    def add_brok(self, b=None):
        """
        Add or create a broker instance to the emulation.
        """

        # TEMPORARY: only allow one broker:
        if self.N_brok == 1:
            raise RuntimeError('only one broker allowed')

        if not isinstance(b, Broker):
            b = Broker(port_data=self.port_data,
                       port_ctrl=self.port_ctrl, routing_table=self.routing_table)
        self.brok_dict[b.id] = b
        self.logger.info('added broker %s' % b.id)
        return b

    def add_mod(self, m=None):
        """
        Add or create a module instance to the emulation.
        """

        if not isinstance(m, BaseModule):
            m = BaseModule(port_data=self.port_data, port_ctrl=self.port_ctrl)
        self.mod_dict[m.id] = m
        self.logger.info('added module %s' % m.id)
        return m

    def add_conn(self, c=None):
        """
        Add or create a connectivity instance to the emulation.
        """

        if not isinstance(c, BaseConnectivity):
            c = BaseConnectivity()
        self.conn_dict[c.id] = c
        self.logger.info('added connectivity %s' % c.id)
        return c

    def start(self):
        """
        Start execution of all processes.
        """

        with IgnoreKeyboardInterrupt():
            for b in self.brok_dict.values():
                b.start()
            for m in self.mod_dict.values():
                m.start();

    def send_ctrl_msg(self, i, *msg):
        """
        Send control message(s) to a module.
        """

        self.sock_ctrl.send_multipart([i]+msg)
        self.logger.info('sent to   %s: %s' % (i, msg))
        poller = zmq.Poller()
        poller.register(self.sock_ctrl, zmq.POLLIN)
        while True:
            if is_poll_in(self.sock_ctrl, poller):
                j, data = self.sock_ctrl.recv_multipart()
                self.logger.info('recv from %s: ack' % j)
                break
                
    def stop(self):
        """
        Stop execution of all processes.
        """

        self.logger.info('stopping all processes')
        poller = zmq.Poller()
        poller.register(self.sock_ctrl, zmq.POLLIN)
        recv_ids = self.mod_dict.keys()
        while recv_ids:

            # Send quit messages and wait for acknowledgments:
            i = recv_ids[0]
            self.logger.info('sent to   %s: quit' % i)
            self.sock_ctrl.send_multipart([i, 'quit'])
            if is_poll_in(self.sock_ctrl, poller):
                 j, data = self.sock_ctrl.recv_multipart()
                 self.logger.info('recv from %s: ack' % j)
                 if j in recv_ids:
                     recv_ids.remove(j)
                     self.mod_dict[j].join(1)
        self.logger.info('all modules stopped')

        # After all modules have been stopped, shut down the broker:
        for i in self.brok_dict.keys():
            self.logger.info('sent to   %s: quit' % i)
            self.sock_ctrl.send_multipart([i, 'quit'])
            self.brok_dict[i].join(1)
        self.logger.info('all brokers stopped')

        
def setup_logger(file_name='neurokernel.log', screen=True, port=None):
    """
    Convenience function for setting up logging with twiggy.

    Parameters
    ----------
    file_name : str
        Log file.
    screen : bool
        If true, write logging output to stdout.
    port : int
        If set to a ZeroMQ port number, publish 
        logging output to that port.
        
    Returns
    -------
    logger : twiggy.logger.Logger
        Logger object.

    Bug
    ---
    To use the ZeroMQ output class, it must be added as an emitter within each
    process.
    
    """

    if file_name:
        file_output = \
          twiggy.outputs.FileOutput(file_name, twiggy.formats.line_format, 'w')
        twiggy.addEmitters(('file', twiggy.levels.DEBUG, None, file_output))

    if screen:
        screen_output = \
          twiggy.outputs.StreamOutput(twiggy.formats.line_format,
                                      stream=sys.stdout)
        twiggy.addEmitters(('screen', twiggy.levels.DEBUG, None, screen_output))

    if port:
        port_output = ZMQOutput('tcp://*:%i' % port,
                               twiggy.formats.line_format)
        twiggy.addEmitters(('port', twiggy.levels.DEBUG, None, port_output))
        
    return twiggy.log.name(('{name:%s}' % 12).format(name='main'))
    
if __name__ == '__main__':

    # Set up logging:
    logger = setup_logger()
    
    # Set up and start emulation:
    man = BaseManager()
    man.add_brok()
    #m1 = man.add_mod(BaseModule(net='ctrl'))
    #m2 = man.add_mod(BaseModule(net='ctrl'))
    #m3 = man.add_mod(BaseModule(net='ctrl'))
    #m4 = man.add_mod(BaseModule(net='ctrl'))
    conn = man.add_conn()
    # m_list = [man.add_mod() for i in xrange(10)]
    # for m1, m2 in zip(m_list, [m_list[-1]]+m_list[:-1]):
    #     man.connect(m1, m2, conn)

    m1 = man.add_mod()
    m2 = man.add_mod()
    m3 = man.add_mod()
    m4 = man.add_mod()

    man.connect(m1, m2, conn)
    man.connect(m2, m1, conn)
    man.connect(m2, m3, conn)
    man.connect(m3, m2, conn)
    man.connect(m3, m4, conn)
    man.connect(m4, m3, conn)
    man.connect(m4, m1, conn)
    man.connect(m1, m4, conn)
    man.connect(m2, m4, conn)
    man.connect(m4, m2, conn)

    man.start()
    time.sleep(1)
    man.stop()
    logger.info('all done')
