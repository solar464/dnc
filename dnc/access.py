# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""DNC access modules."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import sonnet as snt
import tensorflow as tf

from dnc import addressing, util

# For indexing directly into MemoryAccess state
MEMORY = 0
READ_WEIGHTS = 1
WRITE_WEIGHTS = 2
LINKAGE = 3
USAGE = 4


def _erase_and_write(memory, address, reset_weights, values):
    """Module to erase and write in the external memory.

    Erase operation:
      M_t'(i) = M_{t-1}(i) * (1 - w_t(i) * e_t)

    Add operation:
      M_t(i) = M_t'(i) + w_t(i) * a_t

    where e are the reset_weights, w the write weights and a the values.

    Args:
      memory: 3-D tensor of shape `[batch_size, memory_size, word_size]`.
      address: 3-D tensor `[batch_size, num_writes, memory_size]`.
      reset_weights: 3-D tensor `[batch_size, num_writes, word_size]`.
      values: 3-D tensor `[batch_size, num_writes, word_size]`.

    Returns:
      3-D tensor of shape `[batch_size, num_writes, word_size]`.
    """
    expand_address = tf.expand_dims(address, 3)
    reset_weights = tf.expand_dims(reset_weights, 2)
    weighted_resets = expand_address * reset_weights
    reset_gate = util.reduce_prod(1 - weighted_resets, 1)
    memory *= reset_gate

    add_matrix = tf.matmul(address, values, adjoint_a=True)
    memory += add_matrix

    return memory


class MemoryAccess(snt.RNNCore):
    """Access module of the Differentiable Neural Computer.

    This memory module supports multiple read and write heads. It makes use of:

    *   `addressing.TemporalLinkage` to track the temporal ordering of writes in
        memory for each write head.
    *   `addressing.Freeness` for keeping track of memory usage, where
        usage increase when a memory location is written to, and decreases when
        memory is read from that the controller says can be freed.

    Write-address selection is done by an interpolation between content-based
    lookup and using unused memory.

    Read-address selection is done by an interpolation of content-based lookup
    and following the link graph in the forward or backwards read direction.
    """

    def __init__(
        self,
        memory_size=128,
        word_size=20,
        num_reads=1,
        num_writes=1,
        name="memory_access",
        dtype=tf.float32,
    ):
        """Creates a MemoryAccess module.

        Args:
          memory_size: The number of memory slots (N in the DNC paper).
          word_size: The width of each memory slot (W in the DNC paper)
          num_reads: The number of read heads (R in the DNC paper).
          num_writes: The number of write heads (fixed at 1 in the paper).
          name: The name of the module.
        """
        super(MemoryAccess, self).__init__(name=name)
        self._memory_size = memory_size
        self._word_size = word_size
        self._num_reads = num_reads
        self._num_writes = num_writes

        self._dtype = dtype

        self._write_content_weights_mod = addressing.CosineWeights(
            num_writes, word_size, name="write_content_weights"
        )
        self._read_content_weights_mod = addressing.CosineWeights(
            num_reads, word_size, name="read_content_weights"
        )

        self._linkage = addressing.TemporalLinkage(memory_size, num_writes, dtype=dtype)
        self._freeness = addressing.Freeness(memory_size, dtype=dtype)

        self._linear_layers = {}

    # keras.layers.RNN abstract method
    def call(self, inputs, prev_state):
        return self.__call__(inputs, prev_state)

    # sonnet.RNNCore abstract method
    def __call__(self, inputs, prev_state):
        """Connects the MemoryAccess module into the graph.

        Args:
          inputs: tensor of shape `[batch_size, input_size]`. This is used to
              control this access module.
          prev_state: nested list of tensors containing the previous state.

        Returns:
          A tuple `(output, next_state)`, where `output` is a tensor of shape
          `[batch_size, num_reads, word_size]`, and `next_state` is the new
          nested list of tensors at the current time t.
        """
        (
            prev_memory,
            prev_read_weights,
            prev_write_weights,
            prev_linkage,
            prev_usage,
        ) = prev_state

        inputs = self._read_inputs(inputs)

        # Update usage using inputs['free_gate'] and previous read & write weights.
        usage = self._freeness(
            write_weights=prev_write_weights,
            free_gate=inputs["free_gate"],
            read_weights=prev_read_weights,
            prev_usage=prev_usage,
        )

        # Write to memory.
        write_weights = self._write_weights(inputs, prev_memory, usage)
        memory = _erase_and_write(
            prev_memory,
            address=write_weights,
            reset_weights=inputs["erase_vectors"],
            values=inputs["write_vectors"],
        )

        [link, precedence_weights] = self._linkage(write_weights, prev_linkage)

        # Read from memory.
        read_weights = self._read_weights(
            inputs, memory=memory, prev_read_weights=prev_read_weights, link=link
        )
        read_words = tf.matmul(read_weights, memory)

        return (
            read_words,
            [memory, read_weights, write_weights, [link, precedence_weights], usage],
        )

    def _read_inputs(self, inputs):
        """Applies transformations to `inputs` to get control for this module."""

        def _linear(dims, name, activation=None):
            """Returns a linear transformation of `inputs`, followed by a reshape."""
            linear = self._linear_layers.get(name)
            if not linear:
                linear = snt.Linear(np.prod(dims), name=name)
                self._linear_layers[name] = linear

            linear = linear(inputs)
            if activation is not None:
                linear = activation(linear, name=name + "_activation")
            return tf.reshape(linear, [-1, *dims])

        # v_t^i - The vectors to write to memory, for each write head `i`.
        write_vectors = _linear([self._num_writes, self._word_size], "write_vectors")

        # e_t^i - Amount to erase the memory by before writing, for each write head.
        erase_vectors = _linear(
            [self._num_writes, self._word_size], "erase_vectors", tf.sigmoid
        )

        # f_t^j - Amount that the memory at the locations read from at the previous
        # time step can be declared unused, for each read head `j`.
        free_gate = _linear([self._num_reads], "free_gate", tf.sigmoid)

        # g_t^{a, i} - Interpolation between writing to unallocated memory and
        # content-based lookup, for each write head `i`. Note: `a` is simply used to
        # identify this gate with allocation vs writing (as defined below).
        allocation_gate = _linear([self._num_writes], "allocation_gate", tf.sigmoid)

        # g_t^{w, i} - Overall gating of write amount for each write head.
        write_gate = _linear([self._num_writes], "write_gate", tf.sigmoid)

        # \pi_t^j - Mixing between "backwards" and "forwards" positions (for
        # each write head), and content-based lookup, for each read head.
        num_read_modes = 1 + 2 * self._num_writes
        read_mode = snt.BatchApply(tf.nn.softmax)(
            _linear([self._num_reads, num_read_modes], name="read_mode")
        )

        # Parameters for the (read / write) "weights by content matching" modules.
        write_keys = _linear([self._num_writes, self._word_size], "write_keys")
        write_strengths = _linear([self._num_writes], name="write_strengths")

        read_keys = _linear([self._num_reads, self._word_size], "read_keys")
        read_strengths = _linear([self._num_reads], name="read_strengths")

        result = {
            "read_content_keys": read_keys,
            "read_content_strengths": read_strengths,
            "write_content_keys": write_keys,
            "write_content_strengths": write_strengths,
            "write_vectors": write_vectors,
            "erase_vectors": erase_vectors,
            "free_gate": free_gate,
            "allocation_gate": allocation_gate,
            "write_gate": write_gate,
            "read_mode": read_mode,
        }
        return result

    def _write_weights(self, inputs, memory, usage):
        """Calculates the memory locations to write to.

        This uses a combination of content-based lookup and finding an unused
        location in memory, for each write head.

        Args:
          inputs: Collection of inputs to the access module, including controls for
              how to chose memory writing, such as the content to look-up and the
              weighting between content-based and allocation-based addressing.
          memory: A tensor of shape  `[batch_size, memory_size, word_size]`
              containing the current memory contents.
          usage: Current memory usage, which is a tensor of shape `[batch_size,
              memory_size]`, used for allocation-based addressing.

        Returns:
          tensor of shape `[batch_size, num_writes, memory_size]` indicating where
              to write to (if anywhere) for each write head.
        """
        # c_t^{w, i} - The content-based weights for each write head.
        write_content_weights = self._write_content_weights_mod(
            memory, inputs["write_content_keys"], inputs["write_content_strengths"]
        )

        # a_t^i - The allocation weights for each write head.
        write_allocation_weights = self._freeness.write_allocation_weights(
            usage=usage,
            write_gates=(inputs["allocation_gate"] * inputs["write_gate"]),
            num_writes=self._num_writes,
        )

        # Expands gates over memory locations.
        allocation_gate = tf.expand_dims(inputs["allocation_gate"], -1)
        write_gate = tf.expand_dims(inputs["write_gate"], -1)

        # w_t^{w, i} - The write weightings for each write head.
        return write_gate * (
            allocation_gate * write_allocation_weights
            + (1 - allocation_gate) * write_content_weights
        )

    def _read_weights(self, inputs, memory, prev_read_weights, link):
        """Calculates read weights for each read head.

        The read weights are a combination of following the link graphs in the
        forward or backward directions from the previous read position, and doing
        content-based lookup. The interpolation between these different modes is
        done by `inputs['read_mode']`.

        Args:
          inputs: Controls for this access module. This contains the content-based
              keys to lookup, and the weightings for the different read modes.
          memory: A tensor of shape `[batch_size, memory_size, word_size]`
              containing the current memory contents to do content-based lookup.
          prev_read_weights: A tensor of shape `[batch_size, num_reads,
              memory_size]` containing the previous read locations.
          link: A tensor of shape `[batch_size, num_writes, memory_size,
              memory_size]` containing the temporal write transition graphs.

        Returns:
          A tensor of shape `[batch_size, num_reads, memory_size]` containing the
          read weights for each read head.
        """
        # c_t^{r, i} - The content weightings for each read head.
        content_weights = self._read_content_weights_mod(
            memory, inputs["read_content_keys"], inputs["read_content_strengths"]
        )

        # Calculates f_t^i and b_t^i.
        forward_weights = self._linkage.directional_read_weights(
            link, prev_read_weights, forward=True
        )
        backward_weights = self._linkage.directional_read_weights(
            link, prev_read_weights, forward=False
        )

        backward_mode = inputs["read_mode"][:, :, : self._num_writes]
        forward_mode = inputs["read_mode"][
            :, :, self._num_writes : 2 * self._num_writes
        ]
        content_mode = inputs["read_mode"][:, :, 2 * self._num_writes]

        read_weights = (
            tf.expand_dims(content_mode, 2) * content_weights
            + tf.reduce_sum(
                input_tensor=tf.expand_dims(forward_mode, 3) * forward_weights, axis=2
            )
            + tf.reduce_sum(
                input_tensor=tf.expand_dims(backward_mode, 3) * backward_weights, axis=2
            )
        )

        return read_weights

    # keras uses get_initial_state
    def get_initial_state(self, batch_size=None, inputs=None, dtype=None):
        return util.initial_state_from_state_size(
            self.state_size, batch_size, self._dtype
        )

    # snt.RNNCore uses initial_state
    def initial_state(self, batch_size):
        return self.get_initial_state(batch_size=batch_size)

    @property
    def state_size(self):
        """Returns a list of the shape of the state tensors."""
        return [
            #  memory
            tf.TensorShape([self._memory_size, self._word_size]),
            #  read_weights
            tf.TensorShape([self._num_reads, self._memory_size]),
            #  write_weights
            tf.TensorShape([self._num_writes, self._memory_size]),
            #  linkage
            self._linkage.state_size,
            #  usage
            self._freeness.state_size,
        ]

    @property
    def output_size(self):
        """Returns the output shape."""
        return tf.TensorShape([self._num_reads, self._word_size])
