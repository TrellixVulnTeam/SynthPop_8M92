#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Module with the model for DATGAN (Directed Acyclic Tabular GAN), based on TGAN.

This module contains two classes:

- :attr:`GraphBuilder`: That defines the graph and implements a Tensorpack compatible API.
- :attr:`TGANModel`: The public API for the model, that offers a simplified interface for the
  underlying operations with GraphBuilder and trainers in order to fit and sample data.
"""
import json
import os
import pickle
import tarfile

import numpy as np
import tensorflow as tf
from tensorpack import (
    BatchData, BatchNorm, Dropout, FullyConnected, InputDesc, ModelDescBase, ModelSaver,
    PredictConfig, QueueInput, SaverRestore, SimpleDatasetPredictor, logger)
from tensorpack.tfutils.scope_utils import auto_reuse_variable_scope
from tensorpack.tfutils.summary import add_moving_summary
from tensorpack.utils.argtools import memoized

from modules.datgan_noonehot.data import Preprocessor, RandomZData, TGANDataFlow
from modules.datgan_noonehot.trainer import GANTrainer

import networkx as nx

TUNABLE_VARIABLES = {
    'batch_size': [50, 100, 200],
    'z_dim': [50, 100, 200, 400],
    'num_gen_rnn': [100, 200, 300, 400, 500, 600],
    'num_gen_feature': [100, 200, 300, 400, 500, 600],
    'num_dis_layers': [1, 2, 3, 4, 5],
    'num_dis_hidden': [100, 200, 300, 400, 500],
    'learning_rate': [0.0002, 0.0005, 0.001],
    'noise': [0.05, 0.1, 0.2, 0.3]
}


class GraphBuilder(ModelDescBase):
    """Main model for DATGAN.

    Args:
        None

    Attributes:

    """

    def __init__(
        self,
        metadata,
        dag,
        batch_size=200,
        z_dim=200,
        noise=0.2,
        l2norm=0.00001,
        learning_rate=0.001,
        num_gen_rnn=100,
        num_gen_feature=100,
        num_dis_layers=1,
        num_dis_hidden=100,
        optimizer='AdamOptimizer',
        training=True
    ):
        """Initialize the object, set arguments as attributes."""
        self.metadata = metadata
        self.dag = dag
        self.batch_size = batch_size
        self.z_dim = z_dim
        self.noise = noise
        self.l2norm = l2norm
        self.learning_rate = learning_rate
        self.num_gen_rnn = num_gen_rnn
        self.num_gen_feature = num_gen_feature
        self.num_dis_layers = num_dis_layers
        self.num_dis_hidden = num_dis_hidden
        self.optimizer = optimizer
        self.training = training

    def collect_variables(self, g_scope='gen', d_scope='discrim'):
        """Assign generator and discriminator variables from their scopes.

        Args:
            g_scope(str): Scope for the generator.
            d_scope(str): Scope for the discriminator.

        Raises:
            ValueError: If any of the assignments fails or the collections are empty.

        """
        self.g_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, g_scope)
        self.d_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, d_scope)

        if not (self.g_vars or self.d_vars):
            raise ValueError('There are no variables defined in some of the given scopes')

    def build_losses(self, logits_real, logits_fake, extra_g=0, l2_norm=0.00001):
        r"""D and G play two-player minimax game with value function :math:`V(G,D)`.

        .. math::

            min_G max_D V(D, G) = IE_{x \sim p_{data}} [log D(x)] + IE_{z \sim p_{fake}}
                [log (1 - D(G(z)))]

        Args:
            logits_real (tensorflow.Tensor): discrim logits from real samples.
            logits_fake (tensorflow.Tensor): discrim logits from fake samples from generator.
            extra_g(float):
            l2_norm(float): scale to apply L2 regularization.

        Returns:
            None

        """
        with tf.name_scope("GAN_loss"):
            score_real = tf.sigmoid(logits_real)
            score_fake = tf.sigmoid(logits_fake)
            tf.summary.histogram('score-real', score_real)
            tf.summary.histogram('score-fake', score_fake)

            with tf.name_scope("discrim"):
                d_loss_pos = tf.reduce_mean(
                    tf.nn.sigmoid_cross_entropy_with_logits(
                        logits=logits_real,
                        labels=tf.ones_like(logits_real)) * 0.7 + tf.random_uniform(
                            tf.shape(logits_real),
                            maxval=0.3
                    ),
                    name='loss_real'
                )

                d_loss_neg = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=logits_fake, labels=tf.zeros_like(logits_fake)), name='loss_fake')

                d_pos_acc = tf.reduce_mean(
                    tf.cast(score_real > 0.5, tf.float32), name='accuracy_real')

                d_neg_acc = tf.reduce_mean(
                    tf.cast(score_fake < 0.5, tf.float32), name='accuracy_fake')

                d_loss = 0.5 * d_loss_pos + 0.5 * d_loss_neg + \
                    tf.contrib.layers.apply_regularization(
                        tf.contrib.layers.l2_regularizer(l2_norm),
                        tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "discrim"))

                self.d_loss = tf.identity(d_loss, name='loss')

            with tf.name_scope("gen"):
                g_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=logits_fake, labels=tf.ones_like(logits_fake))) + \
                    tf.contrib.layers.apply_regularization(
                        tf.contrib.layers.l2_regularizer(l2_norm),
                        tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'gen'))

                g_loss = tf.identity(g_loss, name='loss')
                extra_g = tf.identity(extra_g, name='klloss')
                self.g_loss = tf.identity(g_loss + extra_g, name='final-g-loss')

            add_moving_summary(
                g_loss, extra_g, self.g_loss, self.d_loss, d_pos_acc, d_neg_acc, decay=0.)

    @memoized
    def get_optimizer(self):
        """Return optimizer of base class."""
        return self._get_optimizer()

    def inputs(self):
        """Return metadata about entry data.

        Returns:
            list[tensorpack.InputDesc]

        Raises:
            ValueError: If any of the elements in self.metadata['details'] has an unsupported
                        value in the `type` key.

        """
        inputs = []

        for col in self.metadata['details'].keys():
            col_info = self.metadata['details'][col]
            if col_info['type'] == 'continuous':
                gaussian_components = col_info['n']
                inputs.append(
                    InputDesc(tf.float32,
                              (self.batch_size, 1),
                              'input_{}_value'.format(col))
                )

                inputs.append(
                    InputDesc(tf.float32,
                              (self.batch_size, gaussian_components),
                              'input_{}_cluster'.format(col)
                              )
                )

            elif col_info['type'] == 'category':
                inputs.append(InputDesc(tf.int32,
                                        (self.batch_size, 1),
                                        'input_{}'.format(col))
                              )

            else:
                raise ValueError(
                    "self.metadata['details'][{}]['type'] must be either `category` or "
                    "`continuous`. Instead it was {}.".format(col, col_info['type'])
                )

        return inputs

    def generator(self, z):
        r"""Build generator graph.

        We generate a numerical variable in 2 steps. We first generate the value scalar
        :math:`v_i`, then generate the cluster vector :math:`u_i`. We generate categorical
        feature in 1 step as a probability distribution over all possible labels.

        The output and hidden state size of LSTM is :math:`n_h`. The input to the LSTM in each
        step :math:`t` is the random variable :math:`z`, the previous hidden vector :math:`f_{t−1}`
        or an embedding vector :math:`f^{\prime}_{t−1}` depending on the type of previous output,
        and the weighted context vector :math:`a_{t−1}`. The random variable :math:`z` has
        :math:`n_z` dimensions.
        Each dimension is sampled from :math:`\mathcal{N}(0, 1)`. The attention-based context
        vector at is a weighted average over all the previous LSTM outputs :math:`h_{1:t}`.
        So :math:`a_t` is a :math:`n_h`-dimensional vector.
        We learn a attention weight vector :math:`α_t \in \mathbb{R}^t` and compute context as

        .. math::
            a_t = \sum_{k=1}^{t} \frac{\textrm{exp}  {\alpha}_{t, j}}
                {\sum_{j} \textrm{exp}  \alpha_{t,j}} h_k.

        We set :math: `a_0` = 0. The output of LSTM is :math:`h_t` and we project the output to
        a hidden vector :math:`f_t = \textrm{tanh}(W_h h_t)`, where :math:`W_h` is a learned
        parameter in the network. The size of :math:`f_t` is :math:`n_f` .
        We further convert the hidden vector to an output variable.

        * If the output is the value part of a continuous variable, we compute the output as
          :math:`v_i = \textrm{tanh}(W_t f_t)`. The hidden vector for :math:`t + 1` step is
          :math:`f_t`.

        * If the output is the cluster part of a continuous variable, we compute the output as
          :math:`u_i = \textrm{softmax}(W_t f_t)`. The feature vector for :math:`t + 1` step is
          :math:`f_t`.

        * If the output is a discrete variable, we compute the output as
          :math:`d_i = \textrm{softmax}(W_t f_t)`. The hidden vector for :math:`t + 1` step is
          :math:`f^{\prime}_{t} = E_i [arg_k \hspace{0.25em} \textrm{max} \hspace{0.25em} d_i ]`,
          where :math:`E \in R^{|D_i|×n_f}` is an embedding matrix for discrete variable
          :math:`D_i`.

        * :math:`f_0` is a special vector :math:`\texttt{<GO>}` and we learn it during the
          training.

        Args:
            z:

        Returns:
            list[tensorflow.Tensor]: Output

        Raises:
            ValueError: If any of the elements in self.metadata['details'] has an unsupported
                        value in the `type` key.

        """

        # Compute the in_edges of the dag
        in_edges = {}

        for n in self.dag.nodes:
            in_edges[n] = []
            for edge in self.dag.in_edges:
                if edge[1] == n:
                    in_edges[n].append(edge[0])

        # Create the NN structure
        with tf.variable_scope('LSTM'):
            # LSTM cell
            cell = tf.nn.rnn_cell.LSTMCell(self.num_gen_rnn)

            # initialize the variables
            zero_state = cell.zero_state(self.batch_size, dtype='float32')
            zero_attention = tf.zeros(
                shape=(self.batch_size, self.num_gen_rnn), dtype='float32')
            zero_input = tf.get_variable(name='go', shape=(1, self.num_gen_feature))  # <GO>
            zero_input = tf.tile(zero_input, [self.batch_size, 1])

            # Some variables
            ptr = 0
            outputs = []
            states = {}

            lstm_state = []
            inputs = []
            attentions = []
            name_to_id = {}

            input = None
            state = None
            attention = None

            # Go through all variables
            for col_id, col in enumerate(self.metadata['details'].keys()):
                print("\033[91m-> Creating cell for {} (in-edges: {})".format(col, len(in_edges[col])))

                # Get info
                col_info = self.metadata['details'][col]
                name_to_id[col] = col_id

                # Get the inputs, attention and state vector in function of the number of in edges
                if len(in_edges[col]) == 0:
                    input = zero_input
                    attention = zero_attention
                    # LSTM state
                    state = zero_state
                    # List of previous states for attention vector
                    states[col] = []
                elif len(in_edges[col]) == 1:
                    id_ = name_to_id[in_edges[col][0]]
                    input = inputs[id_]
                    attention = attentions[id_]
                    # LSTM state
                    state = lstm_state[id_]
                    # List of previous states for attention vector
                    states[col] = states[in_edges[col][0]]
                else:

                    id_ = name_to_id[in_edges[col][0]]

                    input = inputs[id_]
                    attention = attentions[id_]
                    # LSTM state
                    state = lstm_state[id_]
                    # List of previous states for attention vector
                    states[col] = states[in_edges[col][0]]

                    """ Do something about this...
                    print(inputs[0])
                    print(lstm_state[0])
                    # Deal with that later
                    asd
                    all_inputs = []
                    states[col] = []
                    for n in in_edges[col]:
                        all_inputs.append(inputs[name_to_id[n]])
                        inputs.append(tf.concat(all_inputs, axis=1))
                        for i in states[n]:
                            states[col].append(i)
                    """

                # Concat the input with the random variable z
                input = tf.concat([input, z], axis=1)

                # create the cell(s)
                if col_info['type'] == 'continuous':
                    output, state = cell(tf.concat([input, attention], axis=1), state)
                    states[col].append(state[1])

                    gaussian_components = col_info['n']
                    with tf.variable_scope("%02d" % ptr):
                        h = FullyConnected('FC', output, self.num_gen_feature, nl=tf.tanh)
                        outputs.append(FullyConnected('FC2', h, 1, nl=tf.tanh))
                        input = tf.concat([h, z], axis=1)
                        attw = tf.get_variable("attw", shape=(len(states[col]), 1, 1))
                        attw = tf.nn.softmax(attw, axis=0)
                        attention = tf.reduce_sum(tf.stack(states[col], axis=0) * attw, axis=0)

                    ptr += 1

                    output, state = cell(tf.concat([input, attention], axis=1), state)
                    states[col].append(state[1])
                    with tf.variable_scope("%02d" % ptr):
                        h = FullyConnected('FC', output, self.num_gen_feature, nl=tf.tanh)
                        w = FullyConnected('FC2', h, gaussian_components, nl=tf.nn.softmax)
                        outputs.append(w)
                        input = FullyConnected('FC3', w, self.num_gen_feature, nl=tf.identity)
                        attw = tf.get_variable("attw", shape=(len(states[col]), 1, 1))
                        attw = tf.nn.softmax(attw, axis=0)
                        attention = tf.reduce_sum(tf.stack(states[col], axis=0) * attw, axis=0)

                    ptr += 1

                elif col_info['type'] == 'category':
                    output, state = cell(tf.concat([input, attention], axis=1), state)
                    states[col].append(state[1])
                    with tf.variable_scope("%02d" % ptr):
                        h = FullyConnected('FC', output, self.num_gen_feature, nl=tf.tanh)
                        w = FullyConnected('FC2', h, col_info['n'], nl=tf.nn.softmax)
                        outputs.append(w)
                        one_hot = tf.one_hot(tf.argmax(w, axis=1), col_info['n'])
                        input = FullyConnected(
                            'FC3', one_hot, self.num_gen_feature, nl=tf.identity)
                        attw = tf.get_variable("attw", shape=(len(states[col]), 1, 1))
                        attw = tf.nn.softmax(attw, axis=0)
                        attention = tf.reduce_sum(tf.stack(states[col], axis=0) * attw, axis=0)

                    ptr += 1

                else:
                    raise ValueError(
                        "self.metadata['details'][{}]['type'] must be either `category` or "
                        "`continuous`. Instead it was {}.".format(col, col_info['type'])
                    )

                # Add the input to the list of inputs
                inputs.append(input)

                # Add the attention to the list of attentions
                attentions.append(attention)

                # Add the state to the list of states
                lstm_state.append(state)

        return outputs

    @staticmethod
    def batch_diversity(l, n_kernel=10, kernel_dim=10):
        r"""Return the minibatch discrimination vector.

        Let :math:`f(x_i) \in \mathbb{R}^A` denote a vector of features for input :math:`x_i`,
        produced by some intermediate layer in the discriminator. We then multiply the vector
        :math:`f(x_i)` by a tensor :math:`T \in \mathbb{R}^{A×B×C}`, which results in a matrix
        :math:`M_i \in \mathbb{R}^{B×C}`. We then compute the :math:`L_1`-distance between the
        rows of the resulting matrix :math:`M_i` across samples :math:`i \in {1, 2, ... , n}`
        and apply a negative exponential:

        .. math::

            cb(x_i, x_j) = exp(−||M_{i,b} − M_{j,b}||_{L_1} ) \in \mathbb{R}.

        The output :math:`o(x_i)` for this *minibatch layer* for a sample :math:`x_i` is then
        defined as the sum of the cb(xi, xj )’s to all other samples:

        .. math::
            :nowrap:

            \begin{aligned}

            &o(x_i)_b = \sum^{n}_{j=1} cb(x_i , x_j) \in \mathbb{R}\\
            &o(x_i) = \Big[ o(x_i)_1, o(x_i)_2, . . . , o(x_i)_B \Big] \in \mathbb{R}^B\\
            &o(X) ∈ R^{n×B}\\

            \end{aligned}

        Note:
            This is extracted from `Improved techniques for training GANs`_ (Section 3.2) by
            Tim Salimans, Ian Goodfellow, Wojciech Zaremba, Vicki Cheung, Alec Radford, and
            Xi Chen.

        .. _Improved techniques for training GANs: https://arxiv.org/pdf/1606.03498.pdf

        Args:
            l(tf.Tensor)
            n_kernel(int)
            kernel_dim(int)

        Returns:
            tensorflow.Tensor

        """
        M = FullyConnected('fc_diversity', l, n_kernel * kernel_dim, nl=tf.identity)
        M = tf.reshape(M, [-1, n_kernel, kernel_dim])
        M1 = tf.reshape(M, [-1, 1, n_kernel, kernel_dim])
        M2 = tf.reshape(M, [1, -1, n_kernel, kernel_dim])
        diff = tf.exp(-tf.reduce_sum(tf.abs(M1 - M2), axis=3))
        return tf.reduce_sum(diff, axis=0)

    @auto_reuse_variable_scope
    def discriminator(self, vecs):
        r"""Build discriminator.

        We use a :math:`l`-layer fully connected neural network as the discriminator.
        We concatenate :math:`v_{1:n_c}`, :math:`u_{1:n_c}` and :math:`d_{1:n_d}` together as the
        input. We compute the internal layers as

        .. math::
            \begin{aligned}

            f^{(D)}_{1} &= \textrm{LeakyReLU}(\textrm{BN}(W^{(D)}_{1}(v_{1:n_c} \oplus u_{1:n_c}
                \oplus d_{1:n_d})

            f^{(D)}_{1} &= \textrm{LeakyReLU}(\textrm{BN}(W^{(D)}_{i}(f^{(D)}_{i−1} \oplus
                \textrm{diversity}(f^{(D)}_{i−1})))), i = 2:l

            \end{aligned}

        where :math:`\oplus` is the concatenation operation. :math:`\textrm{diversity}(·)` is the
        mini-batch discrimination vector [42]. Each dimension of the diversity vector is the total
        distance between one sample and all other samples in the mini-batch using some learned
        distance metric. :math:`\textrm{BN}(·)` is batch normalization, and
        :math:`\textrm{LeakyReLU}(·)` is the leaky reflect linear activation function. We further
        compute the output of discriminator as :math:`W^{(D)}(f^{(D)}_{l} \oplus \textrm{diversity}
        (f^{(D)}_{l}))` which is a scalar.

        Args:
            vecs(list[tensorflow.Tensor]): List of tensors matching the spec of :meth:`inputs`

        Returns:
            tensorpack.FullyConected: a (b, 1) logits

        """
        logits = tf.concat(vecs, axis=1)
        for i in range(self.num_dis_layers):
            with tf.variable_scope('dis_fc{}'.format(i)):
                if i == 0:
                    logits = FullyConnected(
                        'fc', logits, self.num_dis_hidden, nl=tf.identity,
                        kernel_initializer=tf.truncated_normal_initializer(stddev=0.1)
                    )

                else:
                    logits = FullyConnected('fc', logits, self.num_dis_hidden, nl=tf.identity)

                logits = tf.concat([logits, self.batch_diversity(logits)], axis=1)
                logits = BatchNorm('bn', logits, center=True, scale=False)
                logits = Dropout(logits)
                logits = tf.nn.leaky_relu(logits)

        return FullyConnected('dis_fc_top', logits, 1, nl=tf.identity)

    @staticmethod
    def compute_kl(real, pred):
        r"""Compute the Kullback–Leibler divergence, :math:`D_{KL}(\textrm{pred} || \textrm{real})`.

        Args:
            real(tensorflow.Tensor): Real values.
            pred(tensorflow.Tensor): Predicted values.

        Returns:
            float: Computed divergence for the given values.

        """
        return tf.reduce_sum((tf.log(pred + 1e-4) - tf.log(real + 1e-4)) * pred)

    def build_graph(self, *inputs):
        """Build the whole graph.

        Args:
            inputs(list[tensorflow.Tensor]):

        Returns:
            None

        """
        z = tf.random_normal(
            [self.batch_size, self.z_dim], name='z_train')

        z = tf.placeholder_with_default(z, [None, self.z_dim], name='z')

        with tf.variable_scope('gen'):
            vecs_gen = self.generator(z)

            vecs_denorm = []
            ptr = 0
            # Go through all variables
            for col_id, col in enumerate(self.metadata['details'].keys()):
                # Get info
                col_info = self.metadata['details'][col]

                if col_info['type'] == 'category':
                    t = tf.argmax(vecs_gen[ptr], axis=1)
                    t = tf.cast(tf.reshape(t, [-1, 1]), 'float32')
                    vecs_denorm.append(t)
                    ptr += 1

                elif col_info['type'] == 'continuous':
                    vecs_denorm.append(vecs_gen[ptr])
                    ptr += 1
                    vecs_denorm.append(vecs_gen[ptr])
                    ptr += 1

                else:
                    raise ValueError(
                        "self.metadata['details'][{}]['type'] must be either `category` or "
                        "`continuous`. Instead it was {}.".format(col_id, col_info['type'])
                    )

            tf.identity(tf.concat(vecs_denorm, axis=1), name='gen')

        vecs_pos = []
        ptr = 0
        # Go through all variables
        for col_id, col in enumerate(self.metadata['details'].keys()):
            # Get info
            col_info = self.metadata['details'][col]

            if col_info['type'] == 'category':
                one_hot = tf.one_hot(tf.reshape(inputs[ptr], [-1]), col_info['n'])
                noise_input = one_hot

                if self.training:
                    noise = tf.random_uniform(tf.shape(one_hot), minval=0, maxval=self.noise)
                    noise_input = (one_hot + noise) / tf.reduce_sum(
                        one_hot + noise, keepdims=True, axis=1)

                vecs_pos.append(noise_input)
                ptr += 1

            elif col_info['type'] == 'continuous':
                vecs_pos.append(inputs[ptr])
                ptr += 1
                vecs_pos.append(inputs[ptr])
                ptr += 1

            else:
                raise ValueError(
                    "self.metadata['details'][{}]['type'] must be either `category` or "
                    "`continuous`. Instead it was {}.".format(col_id, col_info['type'])
                )

        KL = 0.
        ptr = 0
        if self.training:
            # Go through all variables
            for col_id, col in enumerate(self.metadata['details'].keys()):
                # Get info
                col_info = self.metadata['details'][col]

                if col_info['type'] == 'category':
                    dist = tf.reduce_sum(vecs_gen[ptr], axis=0)
                    dist = dist / tf.reduce_sum(dist)

                    real = tf.reduce_sum(vecs_pos[ptr], axis=0)
                    real = real / tf.reduce_sum(real)
                    KL += self.compute_kl(real, dist)
                    ptr += 1

                elif col_info['type'] == 'continuous':
                    ptr += 1
                    dist = tf.reduce_sum(vecs_gen[ptr], axis=0)
                    dist = dist / tf.reduce_sum(dist)
                    real = tf.reduce_sum(vecs_pos[ptr], axis=0)
                    real = real / tf.reduce_sum(real)
                    KL += self.compute_kl(real, dist)

                    ptr += 1

                else:
                    raise ValueError(
                        "self.metadata['details'][{}]['type'] must be either `category` or "
                        "`continuous`. Instead it was {}.".format(col_id, col_info['type'])
                    )

        with tf.variable_scope('discrim'):
            discrim_pos = self.discriminator(vecs_pos)
            discrim_neg = self.discriminator(vecs_gen)

        self.build_losses(discrim_pos, discrim_neg, extra_g=KL, l2_norm=self.l2norm)
        self.collect_variables()

    def _get_optimizer(self):
        if self.optimizer == 'AdamOptimizer':
            return tf.train.AdamOptimizer(self.learning_rate, 0.5)

        elif self.optimizer == 'AdadeltaOptimizer':
            return tf.train.AdadeltaOptimizer(self.learning_rate, 0.95)

        else:
            return tf.train.GradientDescentOptimizer(self.learning_rate)


class DATGAN:
    """Main model from TGAN.

    Args:
        continuous_columns (list[int]): 0-index list of column indices to be considered continuous.
        output (str, optional): Path to store the model and its artifacts. Defaults to
            :attr:`output`.
        gpu (list[str], optional):Comma separated list of GPU(s) to use. Defaults to :attr:`None`.
        max_epoch (int, optional): Number of epochs to use during training. Defaults to :attr:`5`.
        steps_per_epoch (int, optional): Number of steps to run on each epoch. Defaults to
            :attr:`10000`.
        save_checkpoints(bool, optional): Whether or not to store checkpoints of the model after
            each training epoch. Defaults to :attr:`True`
        restore_session(bool, optional): Whether or not continue training from the last checkpoint.
            Defaults to :attr:`True`.
        batch_size (int, optional): Size of the batch to feed the model at each step. Defaults to
            :attr:`200`.
        z_dim (int, optional): Number of dimensions in the noise input for the generator.
            Defaults to :attr:`100`.
        noise (float, optional): Upper bound to the gaussian noise added to categorical columns.
            Defaults to :attr:`0.2`.
        l2norm (float, optional):
            L2 reguralization coefficient when computing losses. Defaults to :attr:`0.00001`.
        learning_rate (float, optional): Learning rate for the optimizer. Defaults to
            :attr:`0.001`.
        num_gen_rnn (int, optional): Defaults to :attr:`400`.
        num_gen_feature (int, optional): Number of features of in the generator. Defaults to
            :attr:`100`
        num_dis_layers (int, optional): Defaults to :attr:`2`.
        num_dis_hidden (int, optional): Defaults to :attr:`200`.
        optimizer (str, optional): Name of the optimizer to use during `fit`,possible values are:
            [`GradientDescentOptimizer`, `AdamOptimizer`, `AdadeltaOptimizer`]. Defaults to
            :attr:`AdamOptimizer`.
    """

    def __init__(
        self, continuous_columns, output='output', gpu=None, max_epoch=5, steps_per_epoch=10000,
        save_checkpoints=True, restore_session=True, batch_size=200, z_dim=200, noise=0.2,
        l2norm=0.00001, learning_rate=0.001, num_gen_rnn=100, num_gen_feature=100,
        num_dis_layers=1, num_dis_hidden=100, optimizer='AdamOptimizer',
    ):
        """Initialize object."""
        # Output
        self.continuous_columns = continuous_columns
        self.log_dir = os.path.join(output, 'logs')
        self.model_dir = os.path.join(output, 'model')
        self.output = output

        # DAG
        self.dag = None
        self.var_order = None

        # Training params
        self.max_epoch = max_epoch
        self.steps_per_epoch = steps_per_epoch
        self.save_checkpoints = save_checkpoints
        self.restore_session = restore_session

        # Model params
        self.model = None
        self.batch_size = batch_size
        self.z_dim = z_dim
        self.noise = noise
        self.l2norm = l2norm
        self.learning_rate = learning_rate
        self.num_gen_rnn = num_gen_rnn
        self.num_gen_feature = num_gen_feature
        self.num_dis_layers = num_dis_layers
        self.num_dis_hidden = num_dis_hidden
        self.optimizer = optimizer

        if gpu:
            os.environ['CUDA_VISIBLE_DEVICES'] = gpu

        self.gpu = gpu

    def get_model(self, training=True):
        """Return a new instance of the model."""
        return GraphBuilder(
            metadata=self.metadata,
            dag=self.dag,
            batch_size=self.batch_size,
            z_dim=self.z_dim,
            noise=self.noise,
            l2norm=self.l2norm,
            learning_rate=self.learning_rate,
            num_gen_rnn=self.num_gen_rnn,
            num_gen_feature=self.num_gen_feature,
            num_dis_layers=self.num_dis_layers,
            num_dis_hidden=self.num_dis_hidden,
            optimizer=self.optimizer,
            training=training
        )

    def prepare_sampling(self):
        """Prepare model for generate samples."""
        if self.model is None:
            self.model = self.get_model(training=False)

        else:
            self.model.training = False

        predict_config = PredictConfig(
            session_init=SaverRestore(self.restore_path),
            model=self.model,
            input_names=['z'],
            output_names=['gen/gen', 'z'],
        )

        self.simple_dataset_predictor = SimpleDatasetPredictor(
            predict_config,
            RandomZData((self.batch_size, self.z_dim))
        )

    def fit(self, data, dag):
        """Fit the model to the given data.

        Args:
            data(pandas.DataFrame): dataset to fit the model.
            dag(networkx.classes.digraph.DiGraph): DAG for the relations between variables

        Returns:
            None

        """
        # Verify that the DAG has the same number of nodes as the number of variables in the data
        # and that it's indeed a DAG.
        self.dag = dag
        self.verify_dag(data)
        self.var_order = self.get_order_variables()

        self.preprocessor = Preprocessor(continuous_columns=self.continuous_columns, columns_order=self.var_order)
        data = self.preprocessor.fit_transform(data)
        self.metadata = self.preprocessor.metadata
        dataflow = TGANDataFlow(data, self.metadata)
        batch_data = BatchData(dataflow, self.batch_size)
        input_queue = QueueInput(batch_data)

        self.model = self.get_model(training=True)

        trainer = GANTrainer(
            model=self.model,
            input_queue=input_queue,
        )

        self.restore_path = os.path.join(self.model_dir, 'checkpoint')

        if os.path.isfile(self.restore_path) and self.restore_session:
            session_init = SaverRestore(self.restore_path)
            with open(os.path.join(self.log_dir, 'stats.json')) as f:
                starting_epoch = json.load(f)[-1]['epoch_num'] + 1

        else:
            session_init = None
            starting_epoch = 1

        action = 'k' if self.restore_session else None
        logger.set_logger_dir(self.log_dir, action=action)

        callbacks = []
        if self.save_checkpoints:
            callbacks.append(ModelSaver(checkpoint_dir=self.model_dir))

        trainer.train_with_defaults(
            callbacks=callbacks,
            steps_per_epoch=self.steps_per_epoch,
            max_epoch=self.max_epoch,
            session_init=session_init,
            starting_epoch=starting_epoch
        )

        self.prepare_sampling()

    def sample(self, num_samples):
        """Generate samples from model.

        Args:
            num_samples(int)

        Returns:
            None

        Raises:
            ValueError

        """
        max_iters = (num_samples // self.batch_size)

        results = []
        for idx, o in enumerate(self.simple_dataset_predictor.get_result()):
            results.append(o[0])
            if idx + 1 == max_iters:
                break

        results = np.concatenate(results, axis=0)

        ptr = 0
        features = {}
        # Go through all variables
        for col_id, col in enumerate(self.metadata['details'].keys()):
            # Get info
            col_info = self.metadata['details'][col]
            if col_info['type'] == 'category':
                features[col] = results[:, ptr:ptr + 1]
                ptr += 1

            elif col_info['type'] == 'continuous':
                gaussian_components = col_info['n']
                val = results[:, ptr:ptr + 1]
                ptr += 1
                pro = results[:, ptr:ptr + gaussian_components]
                ptr += gaussian_components
                features[col] = np.concatenate([val, pro], axis=1)

            else:
                raise ValueError(
                    "self.metadata['details'][{}]['type'] must be either `category` or "
                    "`continuous`. Instead it was {}.".format(col_id, col_info['type'])
                )

        return self.preprocessor.reverse_transform(features)[:num_samples].copy()

    def tar_folder(self, tar_name):
        """Generate a tar of :self.output:."""
        with tarfile.open(tar_name, 'w:gz') as tar_handle:
            for root, dirs, files in os.walk(self.output):
                for file_ in files:
                    tar_handle.add(os.path.join(root, file_))

            tar_handle.close()

    @classmethod
    def load(cls, path):
        """Load a pretrained model from a given path."""
        with tarfile.open(path, 'r:gz') as tar_handle:
            destination_dir = os.path.dirname(tar_handle.getmembers()[0].name)
            tar_handle.extractall()

        with open('{}/DATGANModel'.format(destination_dir), 'rb') as f:
            instance = pickle.load(f)

        instance.prepare_sampling()
        return instance

    def save(self, path, force=False):
        """Save the fitted model in the given path."""
        if os.path.exists(path) and not force:
            logger.info('The indicated path already exists. Use `force=True` to overwrite.')
            return

        base_path = os.path.dirname(path)
        if not os.path.exists(base_path):
            os.makedirs(base_path)

        model = self.model
        dataset_predictor = self.simple_dataset_predictor

        self.model = None
        self.simple_dataset_predictor = None

        with open('{}/DATGANModel'.format(self.output), 'wb') as f:
            pickle.dump(self, f)

        self.model = model
        self.simple_dataset_predictor = dataset_predictor

        self.tar_folder(path)

        logger.info('Model saved successfully.')

    def verify_dag(self, data):
        """
        Verify that the given graph is indeed a dag.

        :return:
        """

        # 1. Verify the type
        if type(self.dag) is not nx.classes.digraph.DiGraph:
            raise TypeError("Provided graph is not from the type \"networkx.classes.digraph."
                            "DiGraph\": {}".format(type(self.dag)))

        # 2. Verify that the graph is indeed a DAG
        if not nx.algorithms.dag.is_directed_acyclic_graph(self.dag):

            cycles = nx.algorithms.cycles.find_cycle(self.dag)

            if len(cycles) > 0:
                raise ValueError("Provided graph is not a DAG. Cycles found: {}".format(cycles))
            else:
                raise ValueError("Provided graph is not a DAG.")

        # 3. Verify that the dag has the correct number of nodes
        if len(self.dag.nodes) != len(data.columns):
            raise ValueError("DAG does not have the same number of nodes ({}) as the number of "
                             "variables in the data ({}).".format(len(self.dag.nodes), len(data.columns)))

    def get_in_edges(self):
        # Get the in_edges
        in_edges = {}

        for n in self.dag.nodes:
            in_edges[n] = []
            for edge in self.dag.in_edges:
                if edge[1] == n:
                    in_edges[n].append(edge[0])

        return in_edges

    def get_order_variables(self):
        """
        Get the order of all the variables in the graph

        :return: list of column names
        """
        # Get the in_edges
        in_edges = self.get_in_edges()

        untreated = set(self.dag.nodes)
        treated = []

        # Get all nodes with 0 in degree
        to_treat = [node for node, in_degree in self.dag.in_degree() if in_degree == 0]

        while len(untreated) > 0:
            # remove the treated nodes
            for n in to_treat:
                untreated.remove(n)
                treated.append(n)

            to_treat = []
            # Find the edges that are coming from the the treated nodes
            for edge in self.dag.in_edges:

                all_treated = True
                for l in in_edges[edge[1]]:
                    if l not in treated:
                        all_treated = False

                if edge[0] in treated and all_treated and edge[1] not in treated and edge[1] not in to_treat:
                    to_treat.append(edge[1])

        return treated

