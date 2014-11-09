"""Build an MpiSimulatorChunk that can be manipulated from Python"""

from nengo import builder
from nengo.neurons import LIF, LIFRate
from nengo.utils.graphs import toposort
from nengo.utils.simulator import operator_depencency_graph

import numpy as np
import mpi_sim

import logging
logger = logging.getLogger(__name__)


def pyfunc_checks(val):
    """
    If the output can possibly be treated as a scalar, convert it
    to a python float. Otherwise, convert it to a numpy ndarray.
    """

    if isinstance(val, list):
        val = np.array(val, dtype=np.float64)

    elif isinstance(val, np.ndarray):

        if getattr(val, 'shape', None) == ():
            val = float(val)

        elif getattr(val, 'dtype', None) != np.float64:
            val = np.asarray(val, dtype=np.float64)

    elif isinstance(val, int):
        val = float(val)

    elif isinstance(val, float):
        if isinstance(val, np.float64):
            val = float(val)

    else:
        raise ValueError(
            "python function returning unexpected value, %s" % str(val))

    return val


def make_checked_func(func, t_in, takes_input):
    def f():
        return pyfunc_checks(func())

    def ft(t):
        return pyfunc_checks(func(t))

    def fit(t, i):
        return pyfunc_checks(func(t, i))

    if t_in and takes_input:
        return fit
    elif t_in or takes_input:
        return ft
    else:
        return f


def make_key(obj):
    """
    Create a key for an object. Must be unique, and reproducable (i.e. produce
    the same key if called with the same object multiple times).
    """
    if isinstance(obj, builder.signal.SignalView):
        return id(obj.base)
    else:
        return id(obj)


class MpiSend(builder.operator.Operator):
    """
    MpiSend placeholder operator. Stores the signal that the operator will
    send and the partition that it will be sent to. No makestep is defined,
    as it will never be called.
    """
    def __init__(self, dst, signal):
        self.sets = []
        self.incs = []
        self.reads = []
        self.updates = []
        self.dst = dst
        self.signal = signal


class MpiRecv(builder.operator.Operator):
    """
    MpiRecv placeholder operator. Stores the signal that the operator will
    receive and the partition that it will be received from. No makestep is
    defined, as it will never be called.
    """
    def __init__(self, src, signal):
        self.sets = []
        self.incs = []
        self.reads = []
        self.updates = []
        self.src = src
        self.signal = signal


class MpiWait(builder.operator.Operator):
    def __init__(self, signal):
        """Sets the signal so that the signal has a set. Otherwise an assertion
        is violated in the order finding algorithm. Also puts the MpiWait in
        the right place, i.e. before any operators that read from the signal.
        """

        self.sets = [signal]
        self.incs = []
        self.reads = []
        self.updates = []
        self.signal = signal

    def make_step():
        """Dummy function, so this op gets included in the ordering"""
        pass


class SimulatorChunk(object):

    def __init__(self, mpi_chunk, model=None, dt=0.001):

        self.model = model

        # C++ key (int) -> ndarray
        self.sig_dict = {}

        # Interface to the C++ MpiSimulatorChunk
        self.mpi_chunk = mpi_chunk

        self.dt = dt

        self.signals = builder.signal.SignalDict(
            __time__=np.asarray(0.0, dtype=np.float64))

        if model is not None:
            print "MODEL", model
            print "SEND SIGNALS", model.send_signals
            print "RECV SIGNALS", model.recv_signals

            for op in model.operators:
                op.init_signals(self.signals)

            for signal, dst in self.model.send_signals:
                mpi_wait = MpiWait(signal)
                self.model.operators.append(mpi_wait)

            for signal, src in self.model.recv_signals:
                mpi_wait = MpiWait(signal)
                self.model.operators.append(mpi_wait)

            self.dg = operator_depencency_graph(self.model.operators)
            self._step_order = [node for node in toposort(self.dg)
                                if hasattr(node, 'make_step')]

            for signal, dst in model.send_signals:
                # find the op that updates the signal
                updates_signal = map(
                    lambda x: signal in x.updates, self._step_order)

                update_index = updates_signal.index(True)

                mpi_send = MpiSend(dst, signal)

                self._step_order.insert(update_index+1, mpi_send)

            for signal, src in model.recv_signals:
                # find the first op that reads from the signal
                reads = map(
                    lambda x: signal in x.reads, self._step_order)

                read_index_last = len(reads) - reads[::-1].index(True) - 1

                mpi_recv = MpiRecv(src, signal)

                # Put the recv after the last read,
                # and the wait before the first read
                self._step_order.insert(read_index_last+1, mpi_recv)

            for sig, numpy_array in self.signals.items():
                self.add_signal(make_key(sig), numpy_array, str(sig))

        print "ALL SIGNALS ADDED"
        print self._step_order

        for op in self._step_order:
            op_type = type(op)

            if op_type == builder.operator.Reset:
                logger.debug(
                    "Creating Reset, dst:%d, Val:%f",
                    make_key(op.dst), op.value)

                self.mpi_chunk.create_Reset(make_key(op.dst), op.value)

            elif op_type == builder.operator.Copy:
                logger.debug(
                    "Creating Copy, dst:%d, src:%d",
                    make_key(op.dst), make_key(op.src))

                self.mpi_chunk.create_Copy(make_key(op.dst), make_key(op.src))

            elif op_type == builder.operator.DotInc:

                logger.debug(
                    "Creating DotInc, A:%d, X:%d, Y:%d",
                    make_key(op.A), make_key(op.X), make_key(op.Y))

                self.mpi_chunk.create_DotInc(
                    make_key(op.A), make_key(op.X), make_key(op.Y))

                # self.add_dot_inc(
                #     make_key(op.A), make_key(op.X), make_key(op.Y))

            elif op_type == builder.operator.ElementwiseInc:
                logger.debug(
                    "Creating ElementwiseInc, A: %d, X: %d, Y:%d",
                    make_key(op.A), make_key(op.X), make_key(op.Y))

                self.mpi_chunk.create_ElementwiseInc(
                    make_key(op.A), make_key(op.X), make_key(op.Y))

            elif op_type == builder.synapses.SimFilterSynapse:
                logger.debug(
                    "Creating Filter, input:%d, output:%d, numer:%s, denom:%s",
                    make_key(op.input), make_key(op.output), str(op.num),
                    str(op.den))

                self.mpi_chunk.create_Filter(
                    make_key(op.input), make_key(op.output), op.num, op.den)

            elif op_type == builder.neurons.SimNeurons:
                n_neurons = op.J.size

                if type(op.neurons) is LIF:
                    tau_ref = op.neurons.tau_ref
                    tau_rc = op.neurons.tau_rc

                    logger.debug(
                        "Creating LIF, N: %d, J:%d, output:%d",
                        n_neurons, make_key(op.J), make_key(op.output))

                    self.mpi_chunk.create_SimLIF(
                        n_neurons, tau_rc, tau_ref, self.dt,
                        make_key(op.J), make_key(op.output))

                elif type(op.neurons) is LIFRate:
                    tau_ref = op.neurons.tau_ref
                    tau_rc = op.neurons.tau_rc

                    logger.debug(
                        "Creating LIFRate, N: %d, J:%d, output:%d",
                        n_neurons, make_key(op.J), make_key(op.output))

                    self.mpi_chunk.create_SimLIFRate(
                        n_neurons, tau_rc, tau_ref, self.dt,
                        make_key(op.J), make_key(op.output))
                else:
                    raise NotImplementedError(
                        'nengo_mpi cannot handle neurons of type ' +
                        str(type(op.neurons)))

            elif op_type == builder.node.SimPyFunc:
                t_in = op.t_in
                fn = op.fn
                x = op.x

                output_id = (make_key(op.output)
                             if op.output is not None
                             else -1)

                if x is None:
                    logger.debug(
                        "Creating PyFunc, output:%d", make_key(op.output))

                    if op.output is None:
                        self.mpi_chunk.create_PyFunc(fn, t_in)
                    else:
                        self.mpi_chunk.create_PyFuncO(
                            output_id, make_checked_func(fn, t_in, False),
                            t_in)

                else:
                    logger.debug(
                        "Creating PyFuncWithInput, output:%d",
                        make_key(op.output))

                    if op.output is None:

                        self.mpi_chunk.create_PyFuncI(
                            fn, t_in, make_key(x), x.value)

                    else:
                        self.mpi_chunk.create_PyFuncIO(
                            output_id, make_checked_func(fn, t_in, True),
                            t_in, make_key(x), x.value)

            elif op_type == MpiSend:
                signal_key = make_key(op.signal)
                logger.debug(
                    "Creating MpiSend, dst: %d, signal: %s, signal_key: %d",
                    op.dst, str(op.signal), signal_key)

                self.mpi_chunk.create_MPISend(op.dst, signal_key, signal_key)

            elif op_type == MpiRecv:
                signal_key = make_key(op.signal)
                logger.debug(
                    "Creating MpiRecv, src: %d, signal: %s, signal_key: %d",
                    op.src, str(op.signal), signal_key)

                self.mpi_chunk.create_MPIRecv(op.src, signal_key, signal_key)

            elif op_type == MpiWait:
                signal_key = make_key(op.signal)
                logger.debug(
                    "Creating MpiWait, signal: %s, signal_key: %d",
                    str(op.signal), signal_key)

                self.mpi_chunk.create_MPIWait(signal_key)

            else:
                raise NotImplementedError(
                    'nengo_mpi cannot handle operator of type ' + str(op_type))

            if hasattr(op, 'tag'):
                logger.debug("op.tag: %s", op.tag)

        self._probe_outputs = self.model.params

        for probe in self.model.probes:
            self.add_probe(
                probe, make_key(self.model.sig[probe]['in']),
                sample_every=probe.sample_every)

    def add_dot_inc(self, A_key, X_key, Y_key):

        A = self.sig_dict[A_key]
        X = self.sig_dict[X_key]

        A_shape = A.shape
        X_shape = X.shape

        if A.ndim > 1 and A_shape[0] > 1 and A_shape[1] > 1:
            # check whether A has to be treated as a matrix
            self.mpi_chunk.create_DotIncMV(A_key, X_key, Y_key)
            logger.debug(
                "Creating DotIncMV, A:%d, X:%d, Y:%d", A_key, X_key, Y_key)
        else:
            # if it doesn't, treat it as a vector
            A_scalar = A_shape == () or A_shape == (1,)
            X_scalar = X_shape == () or X_shape == (1,)

            # if one of them is a scalar and the other isn't, make A the scalar
            if X_scalar and not A_scalar:
                self.mpi_chunk.create_DotIncVV(X_key, A_key, Y_key)
                logger.debug(
                    "Creating DotIncVV(inv), A:%d, X:%d, Y:%d",
                    A_key, X_key, Y_key)
            else:
                logger.debug(
                    "Creating DotIncVV, A:%d, X:%d, Y:%d", A_key, X_key, Y_key)
                self.mpi_chunk.create_DotIncVV(A_key, X_key, Y_key)

    def add_signal(self, key, A, label=''):
        if A.ndim == 0:
            A = np.reshape(A, (1, 1))

        self.mpi_chunk.add_signal(key, A, label)

        self.sig_dict[key] = A

    def add_probe(self, probe, signal_key, probe_key=None,
                  sample_every=None, period=1):

        if sample_every is not None:
            period = 1 if sample_every is None else int(sample_every / self.dt)

        self._probe_outputs[probe] = []
        self.probe_keys[probe] = (make_key(probe)
                                  if probe_key is None
                                  else probe_key)

        self.mpi_chunk.create_Probe(self.probe_keys[probe], signal_key, period)
