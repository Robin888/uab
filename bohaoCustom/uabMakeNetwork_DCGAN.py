"""
This architecture uses the following reference:
1. http://bamos.github.io/2016/08/09/deep-completion/
2. https://github.com/bamos/dcgan-completion.tensorflow
3. https://github.com/carpedm20/DCGAN-tensorflow
"""
import os
import time
import math
import numpy as np
import tensorflow as tf
from bohaoCustom import uabMakeNetwork as network
from bohaoCustom import uabMakeNetwork_DeepLabV2


class batch_norm(object):
  def __init__(self, epsilon=1e-5, momentum = 0.9, name="batch_norm"):
    with tf.variable_scope(name):
      self.epsilon  = epsilon
      self.momentum = momentum
      self.name = name

  def __call__(self, x, train=True):
    return tf.contrib.layers.batch_norm(x,
                      decay=self.momentum,
                      updates_collections=None,
                      epsilon=self.epsilon,
                      scale=True,
                      is_training=train,
                      scope=self.name)


def conv_out_size_same(size, stride):
  return int(math.ceil(float(size) / float(stride)))


def image_summary(prediction):
    return (255 * (prediction / 2 + 0.5)).astype(np.uint8)


def lrelu(x, leak=0.2, name="lrelu"):
    with tf.variable_scope(name):
        f1 = 0.5 * (1 + leak)
        f2 = 0.5 * (1 - leak)
        return f1 * x + f2 * abs(x)


def conv2d(input_, output_dim,
           k_h=5, k_w=5, d_h=2, d_w=2, stddev=0.02,
           name="conv2d"):
    with tf.variable_scope(name):
        w = tf.get_variable('w', [k_h, k_w, input_.get_shape()[-1], output_dim],
                            initializer=tf.truncated_normal_initializer(stddev=stddev))
        conv = tf.nn.conv2d(input_, w, strides=[1, d_h, d_w, 1], padding='SAME')

        biases = tf.get_variable('biases', [output_dim], initializer=tf.constant_initializer(0.0))
        conv = tf.nn.bias_add(conv, biases)

        return conv


def deconv2d(input_, output_shape,
             k_h=5, k_w=5, d_h=2, d_w=2, stddev=0.02,
             name="deconv2d", with_w=False):
    with tf.variable_scope(name):
        # filter : [height, width, output_channels, in_channels]
        w = tf.get_variable('w', [k_h, k_w, output_shape[-1], input_.get_shape()[-1]],
                            initializer=tf.random_normal_initializer(stddev=stddev))

        deconv = tf.nn.conv2d_transpose(input_, w, output_shape=output_shape,
                                        strides=[1, d_h, d_w, 1])

        biases = tf.get_variable('biases', [output_shape[-1]], initializer=tf.constant_initializer(0.0))
        deconv = tf.reshape(tf.nn.bias_add(deconv, biases), deconv.get_shape())

        if with_w:
            return deconv, w, biases
        else:
            return deconv


def linear(input_, output_size, scope=None, stddev=0.02, bias_start=0.0, with_w=False):
    shape = input_.get_shape().as_list()
    with tf.variable_scope(scope or "Linear"):
        matrix = tf.get_variable("Matrix", [shape[1], output_size], tf.float32,
                                 tf.random_normal_initializer(stddev=stddev))
        bias = tf.get_variable("bias", [output_size],
            initializer=tf.constant_initializer(bias_start))
        if with_w:
            return tf.matmul(input_, matrix) + bias, matrix, bias
        else:
            return tf.matmul(input_, matrix) + bias


def conv2d_trans_layer(input_, n_filter, name, training, k_size=(5, 5), stride=(2, 2), padding='same', bias=True,
                       bn=True, activation=True):
    with tf.variable_scope('trans_layer_{}'.format(name)):
        deconv = tf.layers.conv2d_transpose(input_, n_filter, k_size, stride, padding=padding,
                                              name='trans_{}'.format(name))
        '''if bias:
            biases = tf.get_variable('biases', [n_filter], initializer=tf.constant_initializer(0.0))
            deconv = tf.nn.bias_add(deconv, biases)'''
        if bn:
            deconv = tf.layers.batch_normalization(deconv, epsilon=1e-5, momentum=0.9,
                                                   training=training, name='bn_{}'.format(name))
        if not activation:
            return tf.nn.relu(deconv)
        else:
            return deconv


def conv2d_layer(input_, n_filter, name, training, k_size=(5, 5), stride=(2, 2), padding='same', bias=True, bn=True):
    with tf.variable_scope('conv_layer_{}'.format(name)):
        conv = tf.layers.conv2d(input_, n_filter, k_size, stride, padding, name='conv_{}'.format(name))
        '''if bias:
            biases = tf.get_variable('biases', [n_filter], initializer=tf.constant_initializer(0.0))
            conv = tf.nn.bias_add(conv, biases)'''
        if bn:
            conv = tf.layers.batch_normalization(conv, epsilon=1e-5, momentum=0.9,
                                                 training=training, name='bn_{}'.format(name))
        return conv


class DCGAN(uabMakeNetwork_DeepLabV2.DeeplabV3):
    def __init__(self, inputs, trainable, input_size, model_name='', dropout_rate=None,
                 learn_rate=1e-4, decay_step=60, decay_rate=0.1, epochs=100,
                 batch_size=5, start_filter_num=32, z_dim=1000):
        network.Network.__init__(self, inputs, trainable, dropout_rate,
                                 learn_rate, decay_step, decay_rate, epochs, batch_size)
        self.name = 'DCGAN'
        self.model_name = self.get_unique_name(model_name)
        self.sfn = start_filter_num
        self.learning_rate = None
        self.valid_d_summary = tf.placeholder(tf.float32, [])
        self.valid_g_summary = tf.placeholder(tf.float32, [])
        self.valid_iou = tf.placeholder(tf.float32, [])
        self.valid_images = tf.placeholder(tf.uint8, shape=[None, input_size[0],
                                                            input_size[1], 3], name='validation_images')
        self.channel_axis = 3
        self.update_ops = None
        self.z_dim = z_dim
        # this architecture only supports square images
        assert input_size[0] == input_size[1]
        self.output_height, self.output_width = input_size[0], input_size[1]
        self.depth = int(np.log2(input_size[0] / 4))

        self.d_bn1 = batch_norm(name='d_bn1')
        self.d_bn2 = batch_norm(name='d_bn2')
        self.d_bn3 = batch_norm(name='d_bn3')
        self.d_bn4 = batch_norm(name='d_bn4')

        self.g_bn0 = batch_norm(name='g_bn0')
        self.g_bn1 = batch_norm(name='g_bn1')
        self.g_bn2 = batch_norm(name='g_bn2')
        self.g_bn3 = batch_norm(name='g_bn3')
        self.g_bn4 = batch_norm(name='g_bn4')

    def generator(self, z, class_num):
        '''with tf.variable_scope('generator'):
            z_linear = linear(z, self.sfn*(2 ** (self.depth - 1))*4*4, 'z_linear')
            deconv = tf.reshape(z_linear, [self.bs, 4, 4, self.sfn*(2 ** (self.depth - 1))])
            for i in range(self.depth-1, 0, -1):
                deconv = conv2d_trans_layer(deconv, self.sfn*(2 ** (i-1)), '{}'.format(i), self.trainable)
            deconv_final = conv2d_trans_layer(deconv, class_num, 'final', self.trainable, activation=False, bn=False)
        return tf.nn.tanh(deconv_final)'''
        with tf.variable_scope("generator") as scope:
            s_h, s_w = self.output_height, self.output_width
            s_h2, s_w2 = conv_out_size_same(s_h, 2), conv_out_size_same(s_w, 2)
            s_h4, s_w4 = conv_out_size_same(s_h2, 2), conv_out_size_same(s_w2, 2)
            s_h8, s_w8 = conv_out_size_same(s_h4, 2), conv_out_size_same(s_w4, 2)
            s_h16, s_w16 = conv_out_size_same(s_h8, 2), conv_out_size_same(s_w8, 2)
            s_h32, s_w32 = conv_out_size_same(s_h16, 2), conv_out_size_same(s_w16, 2)

            # project `z` and reshape
            self.z_, self.h0_w, self.h0_b = linear(
                z, self.sfn * 16 * s_h32 * s_w32, 'g_h0_lin', with_w=True)

            self.h0 = tf.reshape(
                self.z_, [-1, s_h32, s_w32, self.sfn * 16])
            h0 = tf.nn.relu(self.g_bn0(self.h0))

            self.h1, self.h1_w, self.h1_b = deconv2d(
                h0, [self.bs, s_h16, s_w16, self.sfn * 8], name='g_h1', with_w=True)
            h1 = tf.nn.relu(self.g_bn1(self.h1))

            h2, self.h2_w, self.h2_b = deconv2d(
                h1, [self.bs, s_h8, s_w8, self.sfn * 4], name='g_h2', with_w=True)
            h2 = tf.nn.relu(self.g_bn2(h2))

            h3, self.h3_w, self.h3_b = deconv2d(
                h2, [self.bs, s_h4, s_w4, self.sfn * 2], name='g_h3', with_w=True)
            h3 = tf.nn.relu(self.g_bn3(h3))

            h4, self.h4_w, self.h4_b = deconv2d(
                h3, [self.bs, s_h2, s_w2, self.sfn * 1], name='g_h4', with_w=True)
            h4 = tf.nn.relu(self.g_bn4(h4))

            h5, self.h5_w, self.h5_b = deconv2d(
                h4, [self.bs, s_h, s_w, self.class_num], name='g_h5', with_w=True)

            return tf.nn.tanh(h5)

    def discriminator(self, input_, reuse=False, reduce_dim=True):
        '''with tf.variable_scope('discriminator'):
            if reuse:
                tf.get_variable_scope().reuse_variables()
            conv = lrelu(conv2d_layer(input_, self.sfn, '0', self.trainable, bn=False))
            for i in range(self.depth-1):
                power = i + 1
                conv = lrelu(conv2d_layer(conv, self.sfn*(2 ** power), '{}'.format(power), self.trainable))
            if reduce_dim:
                conv_final = linear(tf.reshape(conv, [self.bs, 4*4*self.sfn*(2 ** (self.depth - 1))]), self.z_dim, 'reshape_first')
                conv_final = linear(conv_final, 1, 'reshape_final')
            else:
                conv_final = linear(tf.reshape(conv, [self.bs, 4*4*self.sfn*(2 ** (self.depth - 1))]), 1, 'reshape_final')
            return tf.nn.sigmoid(conv_final), conv_final'''
        with tf.variable_scope("discriminator") as scope:
            if reuse:
                scope.reuse_variables()
            h0 = lrelu(conv2d(input_, self.sfn, name='d_h0_conv'))
            h1 = lrelu(self.d_bn1(conv2d(h0, self.sfn * 2, name='d_h1_conv')))
            h2 = lrelu(self.d_bn2(conv2d(h1, self.sfn * 4, name='d_h2_conv')))
            h3 = lrelu(self.d_bn3(conv2d(h2, self.sfn * 8, name='d_h3_conv')))
            h4 = lrelu(self.d_bn4(conv2d(h3, self.sfn * 16, name='d_h4_conv')))
            h5 = linear(tf.reshape(h4, [self.bs, 4*4*512]), 1, 'd_h5_lin')

            return tf.nn.sigmoid(h5), h5

    def create_graph(self, x_name, class_num, start_filter_num=32, reduce_dim=True):
        self.class_num = class_num
        '''self.gener = self.generator(tf.reshape(self.inputs['Z'], [self.bs, self.z_dim]), class_num)
        self.discr_r, self.discr_r_logits = self.discriminator(self.inputs[x_name], reduce_dim=reduce_dim)
        self.discr_f, self.discr_f_logits = self.discriminator(self.gener, reduce_dim=reduce_dim, reuse=True)'''
        self.G = self.generator(tf.reshape(self.inputs['Z'], [self.bs, self.z_dim]), class_num)
        self.D, self.D_logits = self.discriminator(self.inputs[x_name], reuse=False)
        self.D_, self.D_logits_ = self.discriminator(self.G, reuse=True)

    def make_loss(self, z_name, loss_type='xent', **kwargs):
        with tf.variable_scope('d_loss'):
            '''d_loss_r = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=self.discr_r_logits,
                                                                              labels=tf.ones([self.bs, 1])))
                                                                              #labels=tf.ones_like(self.discr_r)))
            d_loss_f = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=self.discr_f_logits,
                                                                              labels=tf.zeros([self.bs, 1])))
                                                                              #labels=tf.zeros_like(self.discr_f)))
            self.d_loss = 0.5 * d_loss_r + 0.5 * d_loss_f'''
            self.d_loss_real = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(logits=self.D_logits, labels=tf.ones_like(self.D)))
            self.d_loss_fake = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(logits=self.D_logits_, labels=tf.zeros_like(self.D_)))
            self.d_loss = 0.5 * self.d_loss_real + 0.5 * self.d_loss_fake
        with tf.variable_scope('g_loss'):
            '''self.g_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=self.discr_f_logits,
                                                                                 labels=tf.ones([self.bs, 1])))'''
            self.g_loss = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(logits=self.D_logits_, labels=tf.ones_like(self.D_)))

    def make_optimizer(self, train_var_filter):
        t_vars = tf.trainable_variables()
        d_vars = [var for var in t_vars if 'd_' in var.name]
        g_vars = [var for var in t_vars if 'g_' in var.name]
        optm_d = tf.train.AdamOptimizer(self.learning_rate, beta1=0.5).minimize(self.d_loss,
                                                                                var_list=d_vars,
                                                                                global_step=self.global_step)
        optm_g = tf.train.AdamOptimizer(self.learning_rate, beta1=0.5).minimize(self.g_loss,
                                                                                var_list=g_vars,
                                                                                global_step=self.global_step)
        self.optimizer = {'d': optm_d, 'g': optm_g}

    def make_update_ops(self, x_name, z_name):
        tf.add_to_collection('inputs', self.inputs[x_name])
        tf.add_to_collection('inputs', self.inputs[z_name])
        self.update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

    def make_summary(self, hist=False):
        tf.summary.scalar('d loss', self.d_loss)
        tf.summary.scalar('g loss', self.g_loss)
        tf.summary.scalar('learning rate', self.learning_rate)
        self.summary = tf.summary.merge_all()

    def train_config(self, x_name, z_name, n_train, n_valid, patch_size, ckdir, loss_type='xent', train_var_filter=None,
                     hist=False, **kwargs):
        self.make_loss(z_name, loss_type, **kwargs)
        self.make_learning_rate(n_train)
        self.make_update_ops(x_name, z_name)
        self.make_optimizer(train_var_filter)
        self.make_ckdir(ckdir, patch_size)
        self.make_summary()
        self.config = tf.ConfigProto()
        self.n_train = n_train
        self.n_valid = n_valid

    def train(self, x_name, z_name, n_train, sess, summary_writer, n_valid=1000,
              train_reader=None, valid_reader=None,
              image_summary=None, verb_step=100, save_epoch=5,
              img_mean=np.array((0, 0, 0), dtype=np.float32),
              continue_dir=None, valid_iou=False):
        # define summary operations
        valid_g_summary_op = tf.summary.scalar('g_loss_validation', self.valid_g_summary)
        valid_d_summary_op = tf.summary.scalar('d_loss_validation', self.valid_d_summary)
        valid_image_summary_op = tf.summary.image('Validation_images_summary', self.valid_images,
                                                  max_outputs=10)

        if continue_dir is not None:
            self.load(continue_dir, sess)
            gs = sess.run(self.global_step)
            start_epoch = int(np.ceil(gs/n_train*self.bs))
            start_step = gs - int(start_epoch*n_train/self.bs)
        else:
            start_epoch = 0
            start_step = 0

        loss_valid_min = np.inf
        for epoch in range(start_epoch, self.epochs):
            start_time = time.time()
            for step in range(start_step, n_train, self.bs):
                X_batch, _ = train_reader.readerAction(sess)
                Z_batch = np.random.uniform(self.bs, 1, [self.bs, self.z_dim]).astype(np.float32)
                X_batch = 2 * (X_batch / 255 - 0.5)
                _, _, self.global_step_value = sess.run([self.optimizer['d'], self.optimizer['g'], self.global_step],
                                                     feed_dict={self.inputs[x_name]:X_batch,
                                                                self.inputs[z_name]:Z_batch,
                                                                self.trainable: True})
                Z_batch = np.random.uniform(self.bs, 1, [self.bs, self.z_dim]).astype(np.float32)
                _, self.global_step_value = sess.run([self.optimizer['g'], self.global_step],
                                                     feed_dict={self.inputs[z_name]: Z_batch,
                                                                self.trainable: True})

                if self.global_step_value % verb_step == 0:
                    d_loss, g_loss, step_summary = sess.run([self.d_loss, self.g_loss, self.summary],
                                                    feed_dict={self.inputs[x_name]: X_batch,
                                                               self.inputs[z_name]: Z_batch,
                                                               self.trainable: False})
                    summary_writer.add_summary(step_summary, self.global_step_value)
                    print('Epoch {:d} step {:d}\td_loss = {:.3f}, g_loss = {:.3f}'.
                          format(epoch, self.global_step_value, d_loss, g_loss))
            # validation
            loss_valid_mean = []
            g_loss_val_mean = []
            d_loss_val_mean = []
            for step in range(0, n_valid, self.bs):
                X_batch_val, _ = valid_reader.readerAction(sess)
                Z_batch_val = np.random.uniform(self.bs, 1, [self.bs, self.z_dim]).astype(np.float32)
                X_batch_val = 2 * (X_batch_val / 255 - 0.5)
                d_loss_val, g_loss_val = sess.run([self.d_loss, self.g_loss],
                                                  feed_dict={self.inputs[x_name]: X_batch_val,
                                                             self.inputs[z_name]: Z_batch_val,
                                                             self.trainable: False})
                loss_valid_mean.append(d_loss_val+g_loss_val)
                g_loss_val_mean.append(g_loss_val)
                d_loss_val_mean.append(d_loss_val)
            loss_valid_mean = np.mean(loss_valid_mean)
            duration = time.time() - start_time
            print('Validation loss: {:.3f}, duration: {:.3f}'.format(loss_valid_mean, duration))
            valid_g_summary = sess.run(valid_g_summary_op,
                                       feed_dict={self.valid_g_summary: np.mean(g_loss_val_mean)})
            valid_d_summary = sess.run(valid_d_summary_op,
                                       feed_dict={self.valid_d_summary: np.mean(d_loss_val_mean)})
            summary_writer.add_summary(valid_g_summary, self.global_step_value)
            summary_writer.add_summary(valid_d_summary, self.global_step_value)
            if loss_valid_mean < loss_valid_min:
                loss_valid_min = loss_valid_mean
                saver = tf.train.Saver(var_list=tf.global_variables(), max_to_keep=1)
                saver.save(sess, '{}/best_model.ckpt'.format(self.ckdir))

            valid_img_gen = sess.run(self.G, feed_dict={self.inputs[z_name]: Z_batch_val})
            if image_summary is not None:
                valid_image_summary = sess.run(valid_image_summary_op,
                                               feed_dict={self.valid_images:
                                                              image_summary(valid_img_gen)})
                summary_writer.add_summary(valid_image_summary, self.global_step_value)

            if epoch % save_epoch == 0:
                saver = tf.train.Saver(var_list=tf.global_variables(), max_to_keep=1)
                saver.save(sess, '{}/model_{}.ckpt'.format(self.ckdir, epoch), global_step=self.global_step)

    def run(self, train_reader=None, valid_reader=None, test_reader=None, pretrained_model_dir=None, layers2load=None,
            isTrain=False, img_mean=np.array((0, 0, 0), dtype=np.float32), verb_step=100, save_epoch=5, gpu=None,
            tile_size=(5000, 5000), patch_size=(572, 572), truth_val=1, continue_dir=None, load_epoch_num=None,
            fineTune=False, valid_iou=False, best_model=True):
        if gpu is not None:
            os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
            os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)
        if isTrain:
            coord = tf.train.Coordinator()
            with tf.Session(config=self.config) as sess:
                # init model
                init = [tf.global_variables_initializer(), tf.local_variables_initializer()]
                sess.run(init)
                saver = tf.train.Saver(var_list=tf.global_variables(), max_to_keep=1)
                # load model
                if pretrained_model_dir is not None:
                    if layers2load is not None:
                        self.load_weights(pretrained_model_dir, layers2load)
                    else:
                        self.load(pretrained_model_dir, sess, saver, epoch=load_epoch_num)
                threads = tf.train.start_queue_runners(coord=coord, sess=sess)
                try:
                    train_summary_writer = tf.summary.FileWriter(self.ckdir, sess.graph)
                    self.train('X', 'Z', self.n_train, sess, train_summary_writer,
                               n_valid=self.n_valid, train_reader=train_reader, valid_reader=valid_reader,
                               image_summary=image_summary, img_mean=img_mean,
                               verb_step=verb_step, save_epoch=save_epoch, continue_dir=continue_dir,
                               valid_iou=valid_iou)
                finally:
                    coord.request_stop()
                    coord.join(threads)
                    saver.save(sess, '{}/model.ckpt'.format(self.ckdir), global_step=self.global_step)
