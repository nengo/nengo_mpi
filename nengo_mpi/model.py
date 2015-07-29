"""MPIModel"""

try:
    from mpi_sim import PythonMpiSimulator
    mpi_sim_available = True
except ImportError:
    print (
        "mpi_sim.so not available. Network files may be created, "
        "but simulations cannot be run.")
    mpi_sim_available = False

try:
    import h5py as h5
    h5py_available = True
except ImportError:
    print (
        "h5py not available. nengo_mpi cannot be used.")
    h5py_available = False


from nengo import builder
from nengo.builder import Builder as DefaultBuilder
from nengo.neurons import LIF, LIFRate, RectifiedLinear, Sigmoid
from nengo.neurons import AdaptiveLIF, AdaptiveLIFRate, Izhikevich
from nengo.synapses import LinearFilter, Lowpass, Alpha
from nengo.utils.filter_design import cont2discrete
from nengo.utils.graphs import toposort
from nengo.utils.builder import full_transform
from nengo.utils.simulator import operator_depencency_graph
from nengo.cache import NoDecoderCache
from nengo.network import Network
from nengo.connection import Connection
from nengo.ensemble import Ensemble
from nengo.node import Node
from nengo.probe import Probe

from spaun_mpi import SpaunStimulus, build_spaun_stimulus
from spaun_mpi import SpaunStimulusOperator

import numpy as np
from collections import defaultdict
import warnings
from itertools import chain
import re
import os
import tempfile

import logging
logger = logging.getLogger(__name__)

OP_DELIM = ";"
SIGNAL_DELIM = ":"
PROBE_DELIM = "|"


def make_builder(base):
    """
    Create a version of an existing builder function whose only difference
    is that it assumes the model is an instance of MpiModel, and uses that
    model to record which ops are built as part of building which high-level
    objects.

    Parameters
    ----------
    base: The existing builder function that we want to augment.

    """

    def build_object(model, obj):
        try:
            model.push_object(obj)
        except AttributeError:
            raise ValueError(
                "Must use an instance of MpiModel.")

        r = base(model, obj)
        model.pop_object()
        return r

    build_object.__doc__ = (
        "Builder function augmented to make use "
        "of MpiModels.\n\n" + str(base.__doc__))

    return build_object


class MpiBuilder(DefaultBuilder):
    builders = {}

MpiBuilder.builders.update(DefaultBuilder.builders)

with warnings.catch_warnings():

    # Ignore the warning generated by overwriting the builder functions.
    warnings.simplefilter('ignore')

    MpiBuilder.register(Ensemble)(
        make_builder(builder.build_ensemble))

    MpiBuilder.register(Node)(
        make_builder(builder.build_node))

    MpiBuilder.register(Connection)(
        make_builder(builder.build_connection))

    MpiBuilder.register(Probe)(
        make_builder(builder.build_probe))

    MpiBuilder.register(SpaunStimulus)(
        make_builder(build_spaun_stimulus))

    def mpi_build_network(model, network):
        """
        For each connection that emenates from a Node, has a non-None
        pre-slice, AND has no function attached to it, we replace it
        with a connection that is functionally equivalent, but has
        the slicing moved into the transform. This is done because
        in some such cases, the refimpl nengo builder will implement the
        slicing using a pyfunc, which we want to avoid in nengo_mpi.
        """

        remove_conns = []

        for conn in network.connections:
            replace_connection = (
                isinstance(conn.pre_obj, Node)
                and conn.pre_slice != slice(None)
                and conn.function is None)

            if replace_connection:
                transform = full_transform(conn)

                with network:
                    Connection(
                        conn.pre_obj, conn.post_obj,
                        synapse=conn.synapse,
                        transform=transform, solver=conn.solver,
                        learning_rule_type=conn.learning_rule_type,
                        eval_points=conn.eval_points,
                        scale_eval_points=conn.scale_eval_points,
                        seed=conn.seed)

                remove_conns.append(conn)

        if remove_conns:
            network.objects[Connection] = filter(
                lambda c: c not in remove_conns, network.connections)

            network.connections = network.objects[Connection]

        return builder.build_network(model, network)

    MpiBuilder.register(Network)(
        make_builder(mpi_build_network))


class DummyNdarray(object):
    """
    A dummy array intended to act as a place-holder for an
    ndarray. Preserves the type, shape, stride and size
    attributes of the original ndarray, but not its contents.
    """

    def __init__(self, value):
        self.dtype = value.dtype
        self.shape = value.shape
        self.size = value.size
        self.strides = value.strides


def no_den_synapse_args(input, output, b):
    return [
        "NoDenSynapse", signal_to_string(input),
        signal_to_string(output), b]


def simple_synapse_args(input, output, a, b):
    return [
        "SimpleSynapse", signal_to_string(input),
        signal_to_string(output), a, b]


def linear_filter_synapse_args(op, dt, method='zoh'):
    """
    A copy of some of the functionality that gets applied to
    linear filters in refimpl nengo.
    """
    num, den = op.synapse.num, op.synapse.den
    num, den, _ = cont2discrete(
        (num, den), dt, method=method)
    num = num.flatten()
    num = num[1:] if num[0] == 0 else num
    den = den[1:]  # drop first element (equal to 1)

    if len(num) == 1 and len(den) == 0:
        return no_den_synapse_args(op.input, op.output, b=num[0])
    elif len(num) == 1 and len(den) == 1:
        return simple_synapse_args(op.input, op.output, a=den[0], b=num[0])
    else:
        return [
            "Synapse", signal_to_string(op.input),
            signal_to_string(op.output), str(list(num)),
            str(list(den))]


def pyfunc_checks(val):
    """
    If the output can possibly be treated as a scalar, convert it
    to a python float. Otherwise, convert it to a numpy ndarray.
    """

    if isinstance(val, list):
        val = np.array(val, dtype=np.float64)

    elif isinstance(val, int):
        val = float(val)

    elif isinstance(val, float):
        if isinstance(val, np.float64):
            val = float(val)

    elif not isinstance(val, np.ndarray):
        raise ValueError(
            "python function returning unexpected value, %s" % str(val))

    if isinstance(val, np.ndarray):
        val = np.squeeze(val)

        if val.size == 1:
            val = float(val)
        elif getattr(val, 'dtype', None) != np.float64:
            val = np.asarray(val, dtype=np.float64)

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


class MpiSend(builder.operator.Operator):
    """
    MpiSend placeholder operator. Stores the signal that the operator will
    send and the component that it will be sent to. No makestep is defined,
    as it will never be called.
    """

    def __init__(self, dst, tag, signal):
        self.sets = []
        self.incs = []
        self.reads = []
        self.updates = []

        self.dst = dst
        self.tag = tag
        self.signal = signal


class MpiRecv(builder.operator.Operator):
    """
    MpiRecv placeholder operator. Stores the signal that the operator will
    receive and the component that it will be received from. No makestep is
    defined, as it will never be called.
    """

    def __init__(self, src, tag, signal):
        self.sets = []
        self.incs = []
        self.reads = []
        self.updates = []

        self.src = src
        self.tag = tag
        self.signal = signal


def split_connection(conn_ops, signal):
    """
    Split the operators belonging to a connection into a
    ``pre'' group and a ``post'' group. The connection is assumed
    to contain exactly 1 operation performing an update, which
    is assigned to the pre group. All ops that write to signals
    which are read by this updating op are assumed to belong to
    the pre group (as are all ops that write to signals which
    *those* ops read from, etc.). The remaining ops are assigned
    to the post group.

    Parameters
    ----------
    conn_ops: A list containing the operators implementing a nengo connection.

    signal: The signal where the connection will be split. Must be updated by
        one of the operators in ``conn_ops''.

    Returns
    -------
    pre_ops: A list of the ops that come before the updated signal.
    post_ops: A list of the ops that come after the updated signal.

    """

    pre_ops = []

    for op in conn_ops:
        if signal in op.updates:
            pre_ops.append(op)

    assert len(pre_ops) == 1

    reads = pre_ops[0].reads

    post_ops = filter(
        lambda op: op not in pre_ops, conn_ops)

    changed = True
    while changed:
        changed = []

        for op in post_ops:
            writes = set(op.incs) | set(op.sets)

            if writes & set(reads):
                pre_ops.append(op)
                reads.extend(op.reads)
                changed.append(op)

        post_ops = filter(
            lambda op: op not in changed, post_ops)

    return pre_ops, post_ops


def make_key(obj):
    """
    Create a key for an object. Must be unique, and reproducable (i.e. produce
    the same key if called with the same object multiple times).
    """
    if isinstance(obj, builder.signal.SignalView):
        return id(obj.base)
    else:
        return id(obj)


def signal_to_string(signal, delim=SIGNAL_DELIM):
    """
    Takes in a signal, and encodes the relevant information in a string.
    The format of the returned string:

        signal_key:shape:elemstrides:offset
    """
    shape = signal.shape if signal.shape else 1
    strides = signal.elemstrides if signal.elemstrides else 1

    signal_args = [
        make_key(signal), shape, strides, signal.offset]

    signal_string = delim.join(map(str, signal_args))
    signal_string = signal_string.replace(" ", "")
    signal_string = signal_string.replace("(", "")
    signal_string = signal_string.replace(")", "")

    return signal_string


def ndarray_to_mpi_string(a):
    if a.ndim == 0:
        s = "[1,1]%f" % a

    elif a.ndim == 1:
        s = "[%d,1]" % a.size
        s += ",".join([str(f) for f in a.flatten()])

    else:
        assert a.ndim == 2
        s = "[%d,%d]" % a.shape
        s += ",".join([str(f) for f in a.flatten()])

    return s


def store_string_list(
        h5_file, dset_name, strings, final_null=True, compression='gzip'):
    """
    Store a list of strings in a dataset in an hdf5 file or group. Strings
    are separated by null characters, with an additional null
    character optionally tacked on at the end.
    """
    big_string = '\0'.join(strings)

    if final_null:
        big_string += '\0'

    data = np.array(list(big_string))
    dset = h5_file.create_dataset(
        dset_name, data=data, dtype='S1', compression=compression)

    dset.attrs['n_strings'] = len(strings)


class MpiModel(builder.Model):
    """
    Output of the MpiBuilder, used by the Simulator.

    Differs from the Model in the reference implementation in that
    as the model is built, it keeps track of the object currently being
    built. This permits it to track which operators are added as part
    of which high-level objects, so that those operators can later be
    added to the correct MPI component (required since MPI components are
    specified in terms of the high-level nengo objects like nodes,
    networks and ensembles).
    """

    def __init__(
            self, n_components, assignments, dt=0.001, label=None,
            decoder_cache=NoDecoderCache(), save_file="", free_memory=True):

        if not h5py_available:
            raise Exception("h5py not available.")

        self.n_components = n_components
        self.assignments = assignments

        if not save_file and not mpi_sim_available:
            raise ValueError(
                "mpi_sim.so is unavailable, so nengo_mpi can only save "
                "network files (cannot run simulations). However, save_file "
                "argument was empty.")

        self.mpi_sim = (
            PythonMpiSimulator(n_components, dt) if not save_file else None)

        self.h5_compression = 'gzip'
        self.op_strings = defaultdict(list)
        self.probe_strings = defaultdict(list)
        self.all_probe_strings = []

        if not save_file:
            save_file = tempfile.mktemp()

        self.save_file_name = save_file

        # for each component, stores the keys of the signals that have
        # to be sent and received, respectively
        self.send_signals = defaultdict(list)
        self.recv_signals = defaultdict(list)

        # for each component, stores the signals that have
        # already been added to that component.
        self.signals = defaultdict(list)
        self.signal_key_set = defaultdict(set)
        self.total_signal_size = defaultdict(int)

        # operators for each component
        self.component_ops = defaultdict(list)

        # probe -> C++ key (int)
        # Used to query the C++ simulator for probe data
        self.probe_keys = {}

        self._object_context = [None]
        self.object_ops = defaultdict(list)

        self._mpi_tag = 0

        self.free_memory = free_memory

        self.pyfunc_args = []

        super(MpiModel, self).__init__(dt, label, decoder_cache)

    @property
    def runnable(self):
        return self.mpi_sim is not None

    def __str__(self):
        return "MpiModel: %s" % self.label

    def sanitize(self, s):
        s = re.sub('([0-9])L', lambda x: x.groups()[0], s)
        return s

    def build(self, *objs):
        return MpiBuilder.build(self, *objs)

    def get_new_mpi_tag(self):
        mpi_tag = self._mpi_tag
        self._mpi_tag += 1
        return mpi_tag

    def push_object(self, object):
        self._object_context.append(object)

    def pop_object(self):

        obj = self._object_context.pop()

        if not isinstance(obj, Connection):
            component = self.assignments[obj]

            self.add_ops(component, self.object_ops[obj])

        else:
            conn = obj
            pre_component = self.assignments[conn.pre_obj]
            post_component = self.assignments[conn.post_obj]

            if pre_component == post_component:
                self.add_ops(pre_component, self.object_ops[conn])

            else:
                if conn.learning_rule_type:
                    raise Exception(
                        "Connections crossing component boundaries "
                        "must not have learning rules.")

                if 'synapse_out' in self.sig[conn]:
                    signal = self.sig[conn]['synapse_out']
                else:
                    raise Exception(
                        "Connections crossing component boundaries "
                        "must be filtered so that there is an update.")

                tag = self.get_new_mpi_tag()

                self.send_signals[pre_component].append(
                    (signal, tag, post_component))
                self.recv_signals[post_component].append(
                    (signal, tag, pre_component))

                pre_ops, post_ops = split_connection(
                    self.object_ops[conn], signal)

                # Have to add the signal to both components, so can't delete it
                # the first time.
                self.add_signal(pre_component, signal, free_memory=False)
                self.add_signal(
                    post_component, signal, free_memory=self.free_memory)

                self.add_ops(pre_component, pre_ops)
                self.add_ops(post_component, post_ops)

    def add_ops(self, component, ops):
        for op in ops:
            for signal in op.all_signals:
                self.add_signal(
                    component, signal, free_memory=self.free_memory)

        self.component_ops[component].extend(ops)

    def add_signal(self, component, signal, free_memory=True):
        key = make_key(signal)

        if key not in self.signal_key_set[component]:
            logger.debug(
                "Component %d: Adding signal %s with key: %s",
                component, signal, make_key(signal))

            self.signal_key_set[component].add(key)
            self.signals[component].append((key, signal))
            self.total_signal_size[component] += signal.size

            # freeing memory doesn't make sense anymore.
            # if free_memory:
            #     # Replace the data stored in the signal by a dummy array,
            #     # which has no contents but has the same shape, size, etc
            #     # as the original. This should allow the memory to be
            #     # reclaimed.
            #     signal.base._value = DummyNdarray(signal.base._value)

    def add_op(self, op):
        """
        Records that the operator was added as part of building
        the object that is on the top of _object_context stack.
        """
        self.object_ops[self._object_context[-1]].append(op)

    def finalize_build(self):
        """
        Called once the MpiBuilder has finished running. Adds operators
        and probes to the mpi simulator. The signals should already have
        been added by this point; they are added to MPI as soon as they
        are built and then deleted from the python level, to save memory.
        """

        all_ops = list(chain(
            *[self.component_ops[component]
              for component in range(self.n_components)]))

        dg = operator_depencency_graph(all_ops)
        global_ordering = [
            op for op in toposort(dg) if hasattr(op, 'make_step')]
        self.global_ordering = {op: i for i, op in enumerate(global_ordering)}

        for component in range(self.n_components):
            self.add_ops_to_mpi(component)

        for probe in self.probes:
            self.add_probe(
                probe, self.sig[probe]['in'],
                sample_every=probe.sample_every)

        with h5.File(self.save_file_name, 'w') as save_file:
            save_file.attrs['dt'] = self.dt
            save_file.attrs['n_components'] = self.n_components

            for component in range(self.n_components):
                component_group = save_file.create_group(str(component))

                # signals
                signals = self.signals[component]
                signal_dset = component_group.create_dataset(
                    'signals', (self.total_signal_size[component],),
                    dtype='float64', compression=self.h5_compression)

                offset = 0
                for key, sig in signals:
                    A = sig.base._value

                    if A.ndim == 0:
                        A = np.reshape(A, (1, 1))

                    if A.dtype != np.float64:
                        A = A.astype(np.float64)

                    signal_dset[offset:offset+A.size] = A.flatten()
                    offset += A.size

                # signal keys
                component_group.create_dataset(
                    'signal_keys', data=[long(key) for key, sig in signals],
                    dtype='int64', compression=self.h5_compression)

                # signal shapes
                def pad(x):
                    return (
                        (1, 1) if len(x) == 0 else (
                            (x[0], 1) if len(x) == 1 else x))

                component_group.create_dataset(
                    'signal_shapes',
                    data=np.array([pad(sig.shape) for key, sig in signals]),
                    dtype='u2', compression=self.h5_compression)

                # signal_labels
                signal_labels = [str(p[1]) for p in signals]
                store_string_list(
                    component_group, 'signal_labels', signal_labels,
                    compression=self.h5_compression)

                # operators
                op_strings = self.op_strings[component]
                store_string_list(
                    component_group, 'operators', op_strings,
                    compression=self.h5_compression)

                # probes
                probe_strings = self.probe_strings[component]
                store_string_list(
                    component_group, 'probes', probe_strings,
                    compression=self.h5_compression)

            probe_strings = self.probe_strings[component]
            store_string_list(
                save_file, 'probe_info', self.all_probe_strings,
                compression=self.h5_compression)

        if self.mpi_sim is not None:
            self.mpi_sim.load_network(self.save_file_name)
            os.remove(self.save_file_name)

            for args in self.pyfunc_args:
                f = {
                    'N': self.mpi_sim.create_PyFunc,
                    'I': self.mpi_sim.create_PyFuncI,
                    'O': self.mpi_sim.create_PyFuncO,
                    'IO': self.mpi_sim.create_PyFuncIO}[args[0]]
                f(*args[1:])

            self.mpi_sim.finalize_build()

    def add_ops_to_mpi(self, component):
        """
        Adds to MPI all ops that are meant for the given component. Which ops
        are meant for which component is stored in self.component_ops.

        For all ops except PyFuncs, creates a string encoding all information
        about the op, and passes it into the C++ MPI simulator. For PyFuncs,
        we need to pass the python function to C++, so it is more involved.
        """

        send_signals = self.send_signals[component]
        recv_signals = self.recv_signals[component]
        component_ops = self.component_ops[component]

        for signal, tag, dst in send_signals:
            mpi_send = MpiSend(dst, tag, signal)

            update_indices = filter(
                lambda i: signal in component_ops[i].updates,
                range(len(component_ops)))

            assert len(update_indices) == 1

            self.global_ordering[mpi_send] = (
                self.global_ordering[component_ops[update_indices[0]]] + 0.5)

            # Put the send after the op that updates the signal.
            component_ops.insert(update_indices[0]+1, mpi_send)

        for signal, tag, src in recv_signals:
            mpi_recv = MpiRecv(src, tag, signal)

            read_indices = filter(
                lambda i: signal in component_ops[i].reads,
                range(len(component_ops)))

            self.global_ordering[mpi_recv] = (
                self.global_ordering[component_ops[read_indices[0]]] - 0.5)

            # Put the recv in front of the first op that reads the signal.
            component_ops.insert(read_indices[0], mpi_recv)

        op_order = sorted(component_ops, key=self.global_ordering.__getitem__)

        for op in op_order:
            op_type = type(op)

            if op_type == builder.node.SimPyFunc:
                if not self.runnable:
                    raise Exception(
                        "Cannot create SimPyFunc operator "
                        "when saving to file.")

                t_in = op.t_in
                fn = op.fn
                x = op.x

                if x is None:
                    if op.output is None:
                        pyfunc_args = ["N", fn, t_in]
                    else:
                        pyfunc_args = [
                            "O", make_checked_func(fn, t_in, False),
                            t_in, signal_to_string(op.output)]

                else:
                    if isinstance(x.value, DummyNdarray):
                        input_array = np.zeros(x.shape)
                    else:
                        input_array = x.value

                    if op.output is None:
                        pyfunc_args = [
                            "I", fn, t_in, signal_to_string(x), input_array]

                    else:
                        pyfunc_args = [
                            "IO", make_checked_func(fn, t_in, True), t_in,
                            signal_to_string(x), input_array,
                            signal_to_string(op.output)]

                self.pyfunc_args.append(
                    pyfunc_args + [self.global_ordering[op]])
            else:
                op_string = self.op_to_string(op)

                if op_string:
                    logger.debug(
                        "Component %d: Adding operator with string: %s",
                        component, op_string)

                    self.op_strings[component].append(op_string)

    def op_to_string(self, op):
        """
        Convert an operator into a string. The string will be passed into
        the C++ simulator, where it will be communicated using MPI to the
        correct MPI process. That process will then build an operator
        using the parameters specified in the string.
        """

        op_type = type(op)

        if op_type == builder.operator.Reset:
            op_args = ["Reset", signal_to_string(op.dst), op.value]

        elif op_type == builder.operator.Copy:
            op_args = [
                "Copy", signal_to_string(op.dst), signal_to_string(op.src)]

        elif op_type == builder.operator.DotInc:
            op_args = [
                "DotInc", signal_to_string(op.A), signal_to_string(op.X),
                signal_to_string(op.Y)]

        elif op_type == builder.operator.ElementwiseInc:
            op_args = [
                "ElementwiseInc", signal_to_string(op.A),
                signal_to_string(op.X), signal_to_string(op.Y)]

        elif op_type == builder.neurons.SimNeurons:
            n_neurons = op.J.size
            neuron_type = type(op.neurons)

            if neuron_type is LIF:
                tau_ref = op.neurons.tau_ref
                tau_rc = op.neurons.tau_rc
                min_voltage = op.neurons.min_voltage

                voltage_signal = signal_to_string(op.states[0])
                ref_time_signal = signal_to_string(op.states[1])

                op_args = [
                    "LIF", n_neurons, tau_rc, tau_ref, min_voltage, self.dt,
                    signal_to_string(op.J), signal_to_string(op.output),
                    voltage_signal, ref_time_signal]

            elif neuron_type is LIFRate:
                tau_ref = op.neurons.tau_ref
                tau_rc = op.neurons.tau_rc
                op_args = [
                    "LIFRate", n_neurons, tau_rc, tau_ref,
                    signal_to_string(op.J), signal_to_string(op.output)]

            elif neuron_type is AdaptiveLIF:
                tau_n = op.neurons.tau_n
                inc_n = op.neurons.inc_n

                tau_rc = op.neurons.tau_rc
                tau_ref = op.neurons.tau_ref

                min_voltage = op.neurons.min_voltage

                voltage_signal = signal_to_string(op.states[0])
                ref_time_signal = signal_to_string(op.states[1])
                adaptation = signal_to_string(op.states[2])

                op_args = [
                    "AdaptiveLIF", n_neurons, tau_n, inc_n, tau_rc, tau_ref,
                    min_voltage, self.dt, signal_to_string(op.J),
                    signal_to_string(op.output), voltage_signal,
                    ref_time_signal, adaptation]

            elif neuron_type is AdaptiveLIFRate:
                tau_n = op.neurons.tau_n
                inc_n = op.neurons.inc_n

                tau_rc = op.neurons.tau_rc
                tau_ref = op.neurons.tau_ref

                adaptation = signal_to_string(op.states[0])

                op_args = [
                    "AdaptiveLIFRate", n_neurons, tau_n, inc_n,
                    tau_rc, tau_ref, self.dt, signal_to_string(op.J),
                    signal_to_string(op.output), adaptation]

            elif neuron_type is RectifiedLinear:
                op_args = [
                    "RectifiedLinear", n_neurons, signal_to_string(op.J),
                    signal_to_string(op.output)]

            elif neuron_type is Sigmoid:
                op_args = [
                    "Sigmoid", n_neurons, op.neurons.tau_ref,
                    signal_to_string(op.J), signal_to_string(op.output)]

            elif neuron_type is Izhikevich:
                tau_recovery = op.neurons.tau_recovery
                coupling = op.neurons.coupling
                reset_voltage = op.neurons.reset_voltage
                reset_recovery = op.neurons.reset_recovery

                voltage = signal_to_string(op.states[0])
                recovery = signal_to_string(op.states[1])

                op_args = [
                    "Izhikevich", n_neurons, tau_recovery, coupling,
                    reset_voltage, reset_recovery, self.dt,
                    signal_to_string(op.J), signal_to_string(op.output),
                    voltage, recovery]

            else:
                raise NotImplementedError(
                    'nengo_mpi cannot handle neurons of type ' +
                    str(neuron_type))

        elif op_type == builder.synapses.SimSynapse:

            synapse = op.synapse

            if isinstance(synapse, Alpha) or isinstance(synapse, Lowpass):
                if synapse.tau <= .03 * self.dt:
                    op_args = no_den_synapse_args(op.input, op.output, b=1.0)
                else:
                    op_args = linear_filter_synapse_args(op, self.dt)

            elif isinstance(synapse, LinearFilter):
                op_args = linear_filter_synapse_args(op, self.dt)

            else:
                raise NotImplementedError(
                    'nengo_mpi cannot handle synapses of '
                    'type %s' % str(type(synapse)))

        elif op_type == builder.operator.PreserveValue:
            logger.debug(
                "Skipping PreserveValue, operator: %s, signal: %s",
                str(op.dst), signal_to_string(op.dst))

            op_args = []

        elif op_type == MpiSend:
            signal_key = make_key(op.signal)
            op_args = ["MpiSend", op.dst, op.tag, signal_key]

        elif op_type == MpiRecv:
            signal_key = make_key(op.signal)
            op_args = ["MpiRecv", op.src, op.tag, signal_key]

        elif op_type == SpaunStimulusOperator:
            output = signal_to_string(op.output)

            op_args = [
                "SpaunStimulus", output, op.stimulus_sequence,
                op.present_interval, op.present_blanks]

        else:
            raise NotImplementedError(
                "nengo_mpi cannot handle operator of "
                "type %s" % str(op_type))

        if op_args:
            op_args = [self.global_ordering[op]] + op_args

        op_string = OP_DELIM.join(map(str, op_args))
        op_string = op_string.replace(" ", "")
        op_string = op_string.replace("(", "")
        op_string = op_string.replace(")", "")

        return op_string

    def add_probe(self, probe, signal, sample_every=None):
        """Add a probe to the mpi simulator."""

        period = 1 if sample_every is None else sample_every / self.dt

        probe_key = make_key(probe)
        self.probe_keys[probe] = probe_key

        signal_string = signal_to_string(signal)

        component = self.assignments[probe]

        logger.debug(
            "Component: %d: Adding probe of signal %s.\n"
            "probe_key: %d, signal_string: %s, period: %d",
            component, str(signal), probe_key,
            signal_string, period)

        probe_string = PROBE_DELIM.join(
            str(i)
            for i
            in [component, probe_key, signal_string, period, str(probe)])

        self.probe_strings[component].append(probe_string)
        self.all_probe_strings.append(probe_string)
