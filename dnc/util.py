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
"""DNC util ops and modules."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow as tf


def batch_invert_permutation(permutations):
  """Returns batched `tf.invert_permutation` for every row in `permutations`."""
  with tf.compat.v1.name_scope('batch_invert_permutation', values=[permutations]):
    perm = tf.cast(permutations, tf.float32)
    dim = int(perm.get_shape()[-1])
    size = tf.cast(tf.shape(input=perm)[0], tf.float32)
    delta = tf.cast(tf.shape(input=perm)[-1], tf.float32)
    rg = tf.range(0, size * delta, delta, dtype=tf.float32)
    rg = tf.expand_dims(rg, 1)
    rg = tf.tile(rg, [1, dim])
    perm = tf.add(perm, rg)
    flat = tf.reshape(perm, [-1])
    perm = tf.math.invert_permutation(tf.cast(flat, tf.int32))
    perm = tf.reshape(perm, [-1, dim])
    return tf.subtract(perm, tf.cast(rg, tf.int32))


def batch_gather(values, indices):
  """Returns batched `tf.gather` for every row in the input."""
  with tf.compat.v1.name_scope('batch_gather', values=[values, indices]):
    idx = tf.expand_dims(indices, -1)
    size = tf.shape(input=indices)[0]
    rg = tf.range(tf.cast(size, tf.int64), dtype=tf.int64)
    rg = tf.expand_dims(rg, -1)
    rg = tf.tile(rg, [1, int(indices.get_shape()[-1])])
    rg = tf.expand_dims(rg, -1)
    gidx = tf.concat([rg, idx], -1)
    return tf.gather_nd(values, gidx)


def one_hot(length, index):
  """Return an nd array of given `length` filled with 0s and a 1 at `index`."""
  result = np.zeros(length)
  result[index] = 1
  return result

def reduce_prod(x, axis, name=None):
  """Efficient reduce product over axis.

  Uses tf.cumprod and tf.gather_nd as a workaround to the poor performance of calculating tf.reduce_prod's gradient on CPU.
  """
  """with tf.compat.v1.name_scope(name, 'util_reduce_prod', values=[x]):
    cp = tf.math.cumprod(x, axis, reverse=True)
    size = tf.shape(input=cp)[0]
    idx1 = tf.range(tf.cast(size, tf.float32), dtype=tf.float32)
    idx2 = tf.zeros([size], tf.float32)
    indices = tf.stack([idx1, idx2], 1)
    return tf.gather_nd(cp, tf.cast(indices, tf.int32))"""
  return tf.math.reduce_prod(x, axis=axis, name=name)

# tf2 and sonnet2 compatibility
def state_size_from_initial_state(initial_state):
    if isinstance(initial_state, tf.Tensor):
        return initial_state.shape[1:] # remove batch size

    state_size_dict = {}
    #import ipdb; ipdb.set_trace()
    for field, value in initial_state._asdict().items():
        state_size_dict[field] = state_size_from_initial_state(value)
    return type(initial_state)(**state_size_dict)

def initial_state_from_state_size(state_size, batch_size, dtype):
    if isinstance(state_size, tf.TensorShape):
        return tf.zeros(batch_size + state_size, dtype=dtype)

    initial_state_dict = {}
    for field, value in state_size._asdict().items():
        initial_state_dict[field] = initial_state_from_state_size(value, batch_size, dtype)
    return type(state_size)(**initial_state_dict)
