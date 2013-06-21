#!/usr/bin/env python

"""
Base Neurokernel classes.
"""

from contextlib import contextmanager
import copy
import multiprocessing as mp
import os
import signal
import string
import sys
import threading
import time

import bidict
import numpy as np
import scipy.sparse
import scipy as sp
import twiggy
import zmq
from zmq.eventloop.ioloop import IOLoop
from zmq.eventloop.zmqstream import ZMQStream
import msgpack_numpy as msgpack

from ctrl_proc import ControlledProcess, LINGER_TIME
from ctx_managers import IgnoreKeyboardInterrupt, OnKeyboardInterrupt, \
     ExceptionOnSignal, TryExceptionOnSignal
from tools.comm import is_poll_in
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
        Network connectivity. May be `none` for no connection,
        `ctrl` for incoming control data only,
        `in` for incoming data, `out` for outgoing data, or
        `full` for both incoming and outgoing data.
    port_data : int
        Port to use when communicating with broker.
    port_ctrl : int
        Port used by broker to control module.

    Attributes
    ----------
    conn_dict dict of BaseConnectivity
       Connectivity objects connecting the module instance with
       other module instances.
    in_ids : list of int
       List of source module IDs.
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
    _net = 'none'
    @property
    def net(self):
        """
        Network connectivity.
        """
        return self._net
    @net.setter
    def net(self, value):
        if value not in ['none', 'ctrl', 'in', 'out', 'full']:
            raise ValueError('invalid network connectivity value')
        self.logger.info('net status changed: %s -> %s' % (self._net, value))
        self._net = value

    def __init__(self, net='none',
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

        # Lists used for storing incoming and outgoing data; each
        # entry is a tuple whose first entry is the source or destination
        # module ID and whose second entry is the data:
        self._in_data = []
        self._out_data = []

        # Objects describing connectivity between this module and other modules
        # keyed by source object ID:
        self.conn_dict = {}
        self.conn_dict['in'] = {}
        self.conn_dict['out'] = {}
        
    @property
    def in_ids(self):
        """
        IDs of source modules.
        """

        return self.conn_dict['in'].keys()

    @property
    def out_ids(self):
        """
        IDs of destination modules.
        """

        return self.conn_dict['out'].keys()

    def add_conn(self, conn, conn_type, id):
        """
        Add the specified connectivity object.

        Parameters
        ----------
        conn : BaseConnectivity
            Connectivity object.
        conn_type : {'in', 'out'}
            Connectivity type. 
        id :
            ID of module instance that is being connected via the specified
            object.
        
        """

        if not isinstance(conn, BaseConnectivity):
            raise ValueError('invalid connectivity object')
        self.conn_dict[conn_type][id] = conn
        
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
        # One can define additional messages to be recognized by the control
        # handler:        
        # elif msg[0] == 'conn':
        #     self.logger.info('conn payload: '+str(msgpack.unpackb(msg[1])))
        #     self.sock_ctrl.send('ack')
        #     self.logger.info('sent ack') 
        else:
            self.sock_ctrl.send('ack')
            self.logger.info('sent ack')

    def _init_net(self):
        """
        Initialize network connection.
        """

        if self.net == 'none':
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

    def _get_in_data(self):
        """
        Get input data from incoming transmission buffer.

        Notes
        -----
        This method should retrieve input data transmitted to the module from
        the input buffer to a data structure which can be used for processing.
        """

        self.logger.info('retrieving input')

    def _put_out_data(self):
        """
        Put output data in outgoing transmission buffer.

        Notes
        -----
        This method should put generated data that must be transmitted to other
        modules into the output buffer.

        """

        self.logger.info('populating output buffer')        
                
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

        if self.net in ['none', 'ctrl']:
            self.logger.info('not synchronizing with network')
        else:
            self.logger.info('synchronizing with network')

            # Send outbound data:
            if self.net in ['out', 'full']:

                # Send all data in outbound buffer:
                send_ids = self.out_ids
                for out_id, data in self._out_data:
                    self.sock_data.send(msgpack.packb((out_id, data)))
                    send_ids.remove(out_id)
                    self.logger.info('sent to   %s: %s' % (out_id, str(data)))
                
                # Send data tuples containing None to those modules for which no
                # actual data was generated to satisfy the barrier condition:
                for out_id in send_ids:
                    self.sock_data.send(msgpack.packb((out_id, None)))
                    self.logger.info('sent to   %s: %s' % (out_id, None))

                # All output IDs should be sent data by this point:
                self.logger.info('sent data to all output IDs')

            # Receive inbound data:
            if self.net in ['in', 'full']:

                # Wait until inbound data is received from all source modules:  
                recv_ids = self.in_ids
                self._in_data = []
                while recv_ids:
                    in_id, data = msgpack.unpackb(self.sock_data.recv())
                    self.logger.info('recv from %s: %s ' % (in_id, str(data)))
                    recv_ids.remove(in_id)

                    # Ignore incoming data containing None:
                    if data is not None:
                        self._in_data.append((in_id, data))
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
                while True:

                    # Get input data:
                    self._get_in_data()

                    # Run the processing step:
                    self.run_step()

                    # Prepare the generated data for output:
                    self._put_out_data()

                    # Synchronize:
                    self._sync()

            self.logger.info('exiting')

class Broker(ControlledProcess):
    """
    Broker for communicating between modules.

    Waits to receive data from all input modules before transmitting the
    collected data to destination modules.
   
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
        (provided by zmq) and a serialized tuple; the tuple contains
        the destination module ID and the data to be transmitted.

        """

        if len(msg) != 2:
            self.logger.info('skipping malformed message: %s' % str(msg))
        else:

            # When a message arrives, remove its source ID from the
            # list of source modules from which data is expected:
            in_id = msg[0]
            out_id, data = msgpack.unpackb(msg[1])
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
                                                   msgpack.packb((in_id, data))])

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
    Intermodule connectivity.

    Stores the connectivity between two LPUs as a series of sparse matrices.
    Every entry in an instance of the class has the following indices:

    - source port ID
    - destination port ID
    - synapse number (when two neurons are connected by more than one neuron)
    - direction ('+' for source to destination, '-' for destination to source)
    - parameter name (the default is 'conn' for simple connectivity)
 
    Each connection may therefore have several parameters; parameters associated
    with nonexistent connections (i.e., those whose 'conn' parameter is set to
    0) should be ignored.
    
    Parameters
    ----------
    N_src : int
        Number of source neurons.
    N_dest: int
        Number of destination neurons.
    N_mult: int
        Maximum supported number of synapses between any two neurons.

    Methods
    -------
    transpose()
        Returns a BaseConnectivity instance with the source and destination
        flipped.
    
    Examples
    --------
    The first connection between port 0 in one LPU with port 3 in some other LPU can
    be accessed as c[0,3,0,'+']. The 'weight' parameter associated with this
    connection can be accessed as c[0,3,0,'+','weight']
    
    Notes
    -----
    Since connections between LPUs should necessarily not contain any recurrent
    connections, it is more efficient to store the inter-LPU connections in two
    separate matrices that respectively map to and from the ports in each LPU
    rather than a large matrix whose dimensions comprise the total number of
    ports in both LPUs.
    
    """

    def __init__(self, N_src, N_dest, N_mult=1):

        # Unique object ID:
        self.id = uid()

        # The number of ports in both of the LPUs must be nonzero:
        assert N_src != 0
        assert N_dest != 0

        # The maximum number of synapses between any two neurons must be
        # nonzero:
        assert N_mult != 0

        self.N_src = N_src
        self.N_dest = N_dest
        self.N_mult = N_mult
        
        # All matrices are stored in this dict:
        self._data = {}

        # Keys corresponding to each connectivity direction are stored in the
        # following lists:
        self._keys_by_dir = {'+': [],
                             '-': []}

        # Create connectivity matrices for both directions:
        key = self._make_key(0, '+', 'conn')
        self._data[key] = self._make_matrix(self.shape, int)
        self._keys_by_dir['+'].append(key)        
        key = self._make_key(0, '-', 'conn')
        self._data[key] = self._make_matrix(self.shape, int)
        self._keys_by_dir['-'].append(key)

    @property
    def shape(self):
        return self.N_src, self.N_dest
            
    @property
    def src_mask(self):
        """
        Mask of source neurons with connections to destination neurons.
        """

        # XXX Performing a sum over the results of this list comprehension
        # might not be necessary if multapses are assumed to always have an
        # entry in the first connectivity matrix:
        m_list = [self._data[k] for k in self._keys_by_dir['+']]
        return np.any(np.sum(m_list).toarray(), axis=1)
                      
    @property
    def src_idx(self):
        """
        Indices of source neurons with connections to destination neurons.
        """
        
        return np.arange(self.shape[1])[self.src_mask]
    
    @property
    def nbytes(self):
        """
        Approximate number of bytes required by the class instance.

        Notes
        -----
        Only accounts for nonzero values in sparse matrices.
        """

        count = 0
        for key in self._data.keys():
            count += self._data[key].dtype.itemsize*self._data[key].nnz
        return count
    
    def _format_bin_array(self, a, indent=0):
        """
        Format a binary array for printing.
        
        Notes
        -----
        Assumes a 2D array containing binary values.
        """
        
        sp0 = ' '*indent
        sp1 = sp0+' '
        a_list = a.toarray().tolist()
        if a.shape[0] == 1:
            return sp0+str(a_list)
        else:
            return sp0+'['+str(a_list[0])+'\n'+''.join(map(lambda s: sp1+str(s)+'\n', a_list[1:-1]))+sp1+str(a_list[-1])+']'
        
    def __repr__(self):
        result = 'src -> dest\n'
        result += '-----------\n'
        for key in self._keys_by_dir['+']:
            result += key + '\n'
            result += self._format_bin_array(self._data[key]) + '\n'
        result += '\ndest -> src\n'
        result += '-----------\n'
        for key in self._keys_by_dir['-']:
            result += key + '\n'
            result += self._format_bin_array(self._data[key]) + '\n'
        return result
        
    def _make_key(self, syn, dir, param):
        """
        Create a unique key for a matrix of synapse properties.
        """
        
        return string.join(map(str, [syn, dir, param]), '/')

    def _make_matrix(self, shape, dtype=np.double):
        """
        Create a sparse matrix of the specified shape.
        """
        
        return sp.sparse.lil_matrix(shape, dtype=dtype)
            
    def get(self, source, dest, syn=0, dir='+', param='conn'):
        """
        Retrieve a value in the connectivity class instance.
        """

        assert type(syn) == int
        assert dir in ['-', '+']
        
        result = self._data[self._make_key(syn, dir, param)][source, dest]
        if not np.isscalar(result):
            return result.toarray()
        else:
            return result

    def set(self, source, dest, syn=0, dir='+', param='conn', val=1):
        """
        Set a value in the connectivity class instance.

        Notes
        -----
        Creates a new storage matrix when the one specified doesn't exist.        
        """

        assert type(syn) == int
        assert dir in ['-', '+']
        
        key = self._make_key(syn, dir, param)
        if not self._data.has_key(key):

            # XX should ensure that inserting a new matrix for an existing param
            # uses the same type as the existing matrices for that param XX
            self._data[key] = self._make_matrix(self.shape, type(val))
            self._keys_by_dir[dir].append(key)

            # Increment the maximum number of synapses between two neurons as needed:
            if syn+1 > self.N_mult:
                self.N_mult += 1
                
        self._data[key][source, dest] = val

    def transpose(self):
        """
        Returns an object instance with the source and destination LPUs flipped.
        """

        c = BaseConnectivity(self.N_dest, self.N_dest)
        c._keys_by_dir['+'] = []
        c._keys_by_dir['-'] = []
        for old_key in self._data.keys():

            # Reverse the direction in the key:
            key_split = old_key.split('/')
            old_dir = key_split[1]
            if old_dir == '+':
                new_dir = '-'
            elif old_dir == '-':
                new_dir = '+'
            else:
                raise ValueError('invalid direction in key')    
            key_split[1] = new_dir
            new_key = '/'.join(key_split)
            c._data[new_key] = self._data[old_key].T           
            c._keys_by_dir[new_dir].append(new_key)
        return c

    @property
    def T(self):
        return self.transpose()
    
    def __getitem__(self, s):        
        return self.get(*s)

    def __setitem__(self, s, val):
        self.set(*s, val=val)
        
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

    def connect(self, m_src, m_dest, conn, dir='='):
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
        dir : {'+','-','='}
           Connectivity direction; '+' denotes a connection from `m_src` to
           `m_dest`; '-' denotes a connection from `m_dest` to `m_src`; '='
           denotes connections in both directions.
        
        Notes
        -----
        A module's connectivity can only be increased; if it already is either
        'in' or 'out', attempting to create a connection that would cause its
        connectivity to become 'out' or 'in' if unconnected will cause it to be
        set to 'full'. 
        
        """

        if not isinstance(m_src, BaseModule) or \
            not isinstance(m_dest, BaseModule) or \
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

        # Update the routing table and the network connectivity of the source
        # and destination module instances:
        if dir == '+':
            self.routing_table[m_src.id, m_dest.id] = 1
            
            if m_src.net == 'none':
                m_src.net = 'out'
            elif m_src.net == 'in':
                m_src.net = 'full'
                
            if m_dest.net == 'none':
                m_dest.net = 'in'
            elif m_dest.net == 'out':
                m_dest.net = 'full'

            m_src.add_conn(conn, 'out', m_dest.id)
            m_dest.add_conn(conn.T, 'in', m_src.id)            
        elif dir == '-':
            self.routing_table[m_dest.id, m_src.id] = 1
            
            if m_src.net == 'none':                
                m_src.net = 'in'
            elif m_src.net == 'out':
                m_src.net = 'full'
                
            if m_dest.net == 'none':
                m_dest.net = 'out'
            elif m_dest.net == 'in':
                m_dest.net = 'full'

            m_src.add_conn(conn, 'in', m_dest.id)
            m_dest.add_conn(conn.T, 'out', m_src.id)            
        elif dir == '=':
            self.routing_table[m_src.id, m_dest.id] = 1
            self.routing_table[m_dest.id, m_src.id] = 1
            m_src.net = 'full'
            m_dest.net = 'full'

            m_src.add_conn(conn, 'out', m_dest.id)
            m_dest.add_conn(conn.T, 'in', m_src.id)            
            m_src.add_conn(conn, 'in', m_dest.id)
            m_dest.add_conn(conn.T, 'out', m_src.id)                        
        else:
            raise ValueError('unrecognized connectivity direction')

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

    def add_conn(self, c):
        """
        Add or create a connectivity instance to the emulation.
        """

        if not isinstance(c, BaseConnectivity):
            raise ValueError('invalid connectivity object')
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

    class MyModule(BaseModule):
        """
        Example of derived module class.
        """

        def _put_out_data(self):
            super(MyModule, self)._put_out_data()

            if self.net in ['out', 'full']:
                self._out_data = []
                for i in self.out_ids:
                    self._out_data.append((i, str(np.random.rand()))) 
   
    # Set up logging:
    logger = setup_logger()

    np.random.seed(0)

    # Set up and start emulation:
    man = BaseManager()
    man.add_brok()

    m1 = man.add_mod(MyModule(net='out'))
    m2 = man.add_mod(MyModule(net='in'))
    # m3 = man.add_mod(MyModule(net='full'))
    # m4 = man.add_mod(MyModule(net='full'))

    conn = BaseConnectivity(3, 3)
    man.add_conn(conn)
    man.connect(m1, m2, conn)
    # man.connect(m2, m1, conn)
    # man.connect(m4, m3, conn)
    # man.connect(m3, m4, conn)
    # man.connect(m4, m1, conn)
    # man.connect(m1, m4, conn)
    # man.connect(m2, m4, conn)
    # man.connect(m4, m2, conn)

    man.start()
    time.sleep(1)
    man.stop()
    logger.info('all done')
