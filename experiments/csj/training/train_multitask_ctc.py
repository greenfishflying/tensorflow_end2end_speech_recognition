#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Train the multi-task CTC model (CSJ corpus)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from os.path import join, isfile
import sys
import time
import tensorflow as tf
from setproctitle import setproctitle
import yaml
import shutil

sys.path.append('../../../')
from experiments.csj.data.load_dataset_multitask_ctc import Dataset
from experiments.csj.metrics.ctc import do_eval_cer
from experiments.utils.directory import mkdir, mkdir_join
from experiments.utils.parameter import count_total_parameters
from experiments.utils.csv import save_loss, save_ler
from experiments.utils.sparsetensor import list2sparsetensor
from models.ctc.load_model_multitask import load


def do_train(network, params):
    """Run training.
    Args:
        network: network to train
        params: A dictionary of parameters
    """
    # Load dataset
    train_data = Dataset(data_type='train',
                         label_type_main=params['label_type_main'],
                         label_type_sub=params['label_type_sub'],
                         train_data_size=params['train_data_size'],
                         batch_size=params['batch_size'],
                         num_stack=params['num_stack'],
                         num_skip=params['num_skip'],
                         sort_utt=True)
    dev_data_step = Dataset(data_type='dev',
                            label_type_main=params['label_type_main'],
                            label_type_sub=params['label_type_sub'],
                            train_data_size=params['train_data_size'],
                            batch_size=params['batch_size'],
                            num_stack=params['num_stack'],
                            num_skip=params['num_skip'],
                            sort_utt=False)
    dev_data_epoch = Dataset(data_type='dev',
                             label_type_main=params['label_type_main'],
                             label_type_sub=params['label_type_sub'],
                             train_data_size=params['train_data_size'],
                             batch_size=params['batch_size'],
                             num_stack=params['num_stack'],
                             num_skip=params['num_skip'],
                             sort_utt=False)

    # Tell TensorFlow that the model will be built into the default graph
    with tf.Graph().as_default():

        # Define placeholders
        network.inputs = tf.placeholder(
            tf.float32,
            shape=[None, None, network.input_size],
            name='input')
        indices_pl = tf.placeholder(tf.int64, name='indices')
        values_pl = tf.placeholder(tf.int32, name='values')
        shape_pl = tf.placeholder(tf.int64, name='shape')
        network.labels = tf.SparseTensor(indices_pl, values_pl, shape_pl)
        indices_sub_pl = tf.placeholder(tf.int64, name='indices_sub')
        values_sub_pl = tf.placeholder(tf.int32, name='values_sub')
        shape_sub_pl = tf.placeholder(tf.int64, name='shape_sub')
        network.labels_sub = tf.SparseTensor(indices_sub_pl,
                                             values_sub_pl,
                                             shape_sub_pl)
        network.inputs_seq_len = tf.placeholder(tf.int64,
                                                shape=[None],
                                                name='inputs_seq_len')
        network.keep_prob_input = tf.placeholder(tf.float32,
                                                 name='keep_prob_input')
        network.keep_prob_hidden = tf.placeholder(tf.float32,
                                                  name='keep_prob_hidden')

        # Add to the graph each operation
        loss_op, logits_main, logits_sub = network.compute_loss(
            network.inputs,
            network.labels,
            network.labels_sub,
            network.inputs_seq_len,
            network.keep_prob_input,
            network.keep_prob_hidden)
        train_op = network.train(
            loss_op,
            optimizer=params['optimizer'],
            learning_rate_init=float(params['learning_rate']),
            decay_steps=params['decay_steps'],
            decay_rate=params['decay_rate'])
        decode_op_main, decode_op_sub = network.decoder(
            logits_main,
            logits_sub,
            network.inputs_seq_len,
            decode_type='beam_search',
            beam_width=20)
        ler_op_main, ler_op_sub = network.compute_ler(
            decode_op_main, decode_op_sub,
            network.labels, network.labels_sub)

        # Build the summary tensor based on the TensorFlow collection of
        # summaries
        summary_train = tf.summary.merge(network.summaries_train)
        summary_dev = tf.summary.merge(network.summaries_dev)

        # Add the variable initializer operation
        init_op = tf.global_variables_initializer()

        # Create a saver for writing training checkpoints
        saver = tf.train.Saver(max_to_keep=None)

        # Count total parameters
        parameters_dict, total_parameters = count_total_parameters(
            tf.trainable_variables())
        for parameter_name in sorted(parameters_dict.keys()):
            print("%s %d" % (parameter_name, parameters_dict[parameter_name]))
        print("Total %d variables, %s M parameters" %
              (len(parameters_dict.keys()),
               "{:,}".format(total_parameters / 1000000)))

        csv_steps, csv_loss_train, csv_loss_dev = [], [], []
        csv_ler_main_train, csv_ler_main_dev = [], []
        csv_ler_sub_train, csv_ler_sub_dev = [], []
        # Create a session for running operation on the graph
        with tf.Session() as sess:

            # Instantiate a SummaryWriter to output summaries and the graph
            summary_writer = tf.summary.FileWriter(
                network.model_dir, sess.graph)

            # Initialize parameters
            sess.run(init_op)

            # Make mini-batch generator
            mini_batch_train = train_data.next_batch()
            mini_batch_dev = dev_data_step.next_batch()

            # Train model
            iter_per_epoch = int(train_data.data_num / params['batch_size'])
            train_step = train_data.data_num / params['batch_size']
            if (train_step) != int(train_step):
                iter_per_epoch += 1
            max_steps = iter_per_epoch * params['num_epoch']
            start_time_train = time.time()
            start_time_epoch = time.time()
            start_time_step = time.time()
            ler_main_dev_best = 1
            for step in range(max_steps):

                # Create feed dictionary for next mini batch (train)
                with tf.device('/cpu:0'):
                    inputs, labels_main, labels_sub, inputs_seq_len, _ = mini_batch_train.__next__()
                feed_dict_train = {
                    network.inputs: inputs,
                    network.labels: list2sparsetensor(labels_main,
                                                      padded_value=-1),
                    network.labels_sub: list2sparsetensor(labels_sub,
                                                          padded_value=-1),
                    network.inputs_seq_len: inputs_seq_len,
                    network.keep_prob_input: network.dropout_ratio_input,
                    network.keep_prob_hidden: network.dropout_ratio_hidden
                }

                # Update parameters
                sess.run(train_op, feed_dict=feed_dict_train)

                if (step + 1) % 200 == 0:

                    # Create feed dictionary for next mini batch (dev)
                    with tf.device('/cpu:0'):
                        inputs, labels_main, labels_sub, inputs_seq_len, _ = mini_batch_dev.__next__()
                    feed_dict_dev = {
                        network.inputs: inputs,
                        network.labels: list2sparsetensor(labels_main,
                                                          padded_value=-1),
                        network.labels_sub: list2sparsetensor(labels_sub,
                                                              padded_value=-1),
                        network.inputs_seq_len: inputs_seq_len,
                        network.keep_prob_input: network.dropout_ratio_input,
                        network.keep_prob_hidden: network.dropout_ratio_hidden
                    }

                    # Compute loss
                    loss_train = sess.run(loss_op, feed_dict=feed_dict_train)
                    loss_dev = sess.run(loss_op, feed_dict=feed_dict_dev)
                    csv_steps.append(step)
                    csv_loss_train.append(loss_train)
                    csv_loss_dev.append(loss_dev)

                    # Change to evaluation mode
                    feed_dict_train[network.keep_prob_input] = 1.0
                    feed_dict_train[network.keep_prob_hidden] = 1.0
                    feed_dict_dev[network.keep_prob_input] = 1.0
                    feed_dict_dev[network.keep_prob_hidden] = 1.0

                    # Compute accuracy & update event file
                    ler_main_train, ler_sub_train, summary_str_train = sess.run(
                        [ler_op_main, ler_op_sub, summary_train],
                        feed_dict=feed_dict_train)
                    ler_main_dev, ler_sub_dev, summary_str_dev = sess.run(
                        [ler_op_main, ler_op_sub,  summary_dev],
                        feed_dict=feed_dict_dev)
                    csv_ler_main_train.append(ler_main_train)
                    csv_ler_main_dev.append(ler_main_dev)
                    csv_ler_sub_train.append(ler_sub_train)
                    csv_ler_sub_dev.append(ler_sub_dev)
                    summary_writer.add_summary(summary_str_train, step + 1)
                    summary_writer.add_summary(summary_str_dev, step + 1)
                    summary_writer.flush()

                    duration_step = time.time() - start_time_step
                    print('Step %d: loss = %.3f (%.3f) / ler_main = %.4f (%.4f) / ler_sub = %.4f (%.4f) (%.3f min)' %
                          (step + 1, loss_train, loss_dev, ler_main_train, ler_main_dev,
                           ler_sub_train, ler_sub_dev, duration_step / 60))
                    sys.stdout.flush()
                    start_time_step = time.time()

                # Save checkpoint and evaluate model per epoch
                if (step + 1) % iter_per_epoch == 0 or (step + 1) == max_steps:
                    duration_epoch = time.time() - start_time_epoch
                    epoch = (step + 1) // iter_per_epoch
                    print('-----EPOCH:%d (%.3f min)-----' %
                          (epoch, duration_epoch / 60))

                    # Save model (check point)
                    checkpoint_file = join(network.model_dir, 'model.ckpt')
                    save_path = saver.save(
                        sess, checkpoint_file, global_step=epoch)
                    print("Model saved in file: %s" % save_path)

                    if epoch >= 5:
                        start_time_eval = time.time()
                        print('=== Dev Evaluation ===')
                        ler_main_dev_epoch = do_eval_cer(
                            session=sess,
                            decode_op=decode_op_main,
                            network=network,
                            dataset=dev_data_epoch,
                            label_type=params['label_type_main'],
                            eval_batch_size=params['batch_size'],
                            is_multitask=True,
                            is_main=True)
                        print('  CER (main): %f %%' %
                              (ler_main_dev_epoch * 100))

                        ler_sub_dev_epoch = do_eval_cer(
                            session=sess,
                            decode_op=decode_op_sub,
                            network=network,
                            dataset=dev_data_epoch,
                            label_type=params['label_type_sub'],
                            eval_batch_size=params['batch_size'],
                            is_multitask=True,
                            is_main=False)
                        print('  CER (sub): %f %%' %
                              (ler_sub_dev_epoch * 100))

                        if ler_main_dev_epoch < ler_main_dev_best:
                            ler_main_dev_best = ler_main_dev_epoch
                            print('■■■ ↑Best Score (CER main)↑ ■■■')

                        duration_eval = time.time() - start_time_eval
                        print('Evaluation time: %.3f min' %
                              (duration_eval / 60))

                    start_time_epoch = time.time()
                    start_time_step = time.time()

            duration_train = time.time() - start_time_train
            print('Total time: %.3f hour' % (duration_train / 3600))

            # Save train & dev loss, ler
            save_loss(csv_steps, csv_loss_train, csv_loss_dev,
                      save_path=network.model_dir)
            save_ler(csv_steps, csv_ler_main_train, csv_ler_sub_dev,
                     save_path=network.model_dir)
            save_ler(csv_steps, csv_ler_sub_train, csv_ler_sub_dev,
                     save_path=network.model_dir)

            # Training was finished correctly
            with open(join(network.model_dir, 'complete.txt'), 'w') as f:
                f.write('')


def main(config_path, model_save_path):

    # Read a config file (.yml)
    with open(config_path, "r") as f:
        config = yaml.load(f)
        params = config['param']

    # Except for a blank label
    if params['label_type_main'] == 'kanji':
        params['num_classes_main'] = 3386
    elif params['label_type_main'] == 'kana':
        params['num_classes_main'] = 147

    if params['label_type_sub'] == 'kana':
        params['num_classes_sub'] = 147
    elif params['label_type_sub'] == 'phone':
        params['num_classes_sub'] = 38

    # Model setting
    CTCModel = load(model_type=params['model'])
    network = CTCModel(batch_size=params['batch_size'],
                       input_size=params['input_size'] * params['num_stack'],
                       num_unit=params['num_unit'],
                       num_layer_main=params['num_layer_main'],
                       num_layer_sub=params['num_layer_sub'],
                       #    bottleneck_dim=params['bottleneck_dim'],
                       num_classes_main=params['num_classes_main'],
                       num_classes_sub=params['num_classes_sub'],
                       main_task_weight=params['main_task_weight'],
                       parameter_init=params['weight_init'],
                       clip_grad=params['clip_grad'],
                       clip_activation=params['clip_activation'],
                       dropout_ratio_input=params['dropout_input'],
                       dropout_ratio_hidden=params['dropout_hidden'],
                       num_proj=params['num_proj'],
                       weight_decay=params['weight_decay'])

    network.model_name = params['model']
    network.model_name += '_' + str(params['num_unit'])
    network.model_name += '_main' + str(params['num_layer_main'])
    network.model_name += '_sub' + str(params['num_layer_sub'])
    network.model_name += '_' + params['optimizer']
    network.model_name += '_lr' + str(params['learning_rate'])
    if params['bottleneck_dim'] != 0:
        network.model_name += '_bottoleneck' + str(params['bottleneck_dim'])
    if params['num_proj'] != 0:
        network.model_name += '_proj' + str(params['num_proj'])
    if params['num_stack'] != 1:
        network.model_name += '_stack' + str(params['num_stack'])
    network.model_name += '_taskweight' + str(params['main_task_weight'])
    if params['train_data_size'] == 'large':
        network.model_name += '_large'
    if params['decay_rate'] != 1:
        network.model_name += '_lrdecay' + \
            str(params['decay_steps'] + params['decay_rate'])

    # Set save path
    network.model_dir = mkdir('/n/sd8/inaguma/result/csj/')
    network.model_dir = mkdir_join(network.model_dir, 'ctc')
    network.model_dir = mkdir_join(
        network.model_dir,
        params['label_type_main'] + '_' + params['label_type_sub'])
    network.model_dir = mkdir_join(network.model_dir, network.model_name)

    # Reset model directory
    if not isfile(join(network.model_dir, 'complete.txt')):
        tf.gfile.DeleteRecursively(network.model_dir)
        tf.gfile.MakeDirs(network.model_dir)
    else:
        raise ValueError('File exists.')

    # Set process name
    setproctitle('csj_multictc_' + params['label_type_main'] + '_' +
                 params['label_type_sub'] + '_' + params['train_data_size'])

    # Save config file
    shutil.copyfile(config_path, join(network.model_dir, 'config.yml'))

    sys.stdout = open(join(network.model_dir, 'train.log'), 'w')
    print(network.model_name)
    do_train(network=network, params=params)


if __name__ == '__main__':

    args = sys.argv
    if len(args) != 3:
        raise ValueError
    main(config_path=args[1], model_save_path=args[2])
