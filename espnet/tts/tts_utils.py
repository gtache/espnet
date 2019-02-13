#!/usr/bin/env python

# Copyright 2018 Nagoya University (Tomoki Hayashi)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)
import json
import logging
import random

import numpy as np

from chainer.training import extensions

from espnet.utils.training.iterators import ShufflingEnabler
from espnet.utils.training.train_utils import prepare_asr_tts_trainer
from espnet.utils.training.train_utils import REPORT_INTERVAL


def make_args_batchset(data, args):
    return make_batchset(data, args.batch_size, args.maxlen_in, args.maxlen_out, args.minibatches, args.batch_sort_key,
                         min_batch_size=args.ngpu if args.ngpu > 1 else 1, shortest_first=not args.sortagrad == 0)


def make_batchset(data, batch_size, max_length_in, max_length_out,
                  num_batches=0, batch_sort_key='shuffle', min_batch_size=1, shortest_first=False):
    """Make batch set from json dictionary

    :param dict data: dictionary loaded from data.json
    :param int batch_size: batch size
    :param int max_length_in: maximum length of input to decide adaptive batch size
    :param int max_length_out: maximum length of output to decide adaptive batch size
    :param int num_batches: # number of batches to use (for debug)
    :param str batch_sort_key: 'shuffle' or 'input' or 'output'
    :param int min_batch_size: minimum batch size (for multi-gpu)
    :return: list of batches
    """
    # sort data with batch_sort_key
    if batch_sort_key == 'shuffle':
        logging.info('use shuffled batch.')
        sorted_data = random.sample(data.items(), len(data.items()))
    elif batch_sort_key == 'input':
        logging.info('use batch sorted by input length and adaptive batch size.')
        # sort it by input lengths (long to short)
        # NOTE: input and output are reversed due to the use of same json as asr
        sorted_data = sorted(data.items(), key=lambda data: int(
            data[1]['output'][0]['shape'][0]), reverse=not shortest_first)
    elif batch_sort_key == 'output':
        logging.info('use batch sorted by output length and adaptive batch size.')
        # sort it by output lengths (long to short)
        # NOTE: input and output are reversed due to the use of same json as asr
        sorted_data = sorted(data.items(), key=lambda data: int(
            data[1]['input'][0]['shape'][0]), reverse=not shortest_first)
    else:
        raise ValueError('batch_sort_key should be selected from None, input, and output.')
    logging.info('# utts: ' + str(len(sorted_data)))

    # check #utts is more than min_batch_size
    if len(sorted_data) < min_batch_size:
        raise ValueError("#utts is less than min_batch_size.")

    # make list of minibatches
    minibatches = []
    start = 0
    while True:
        if batch_sort_key == 'shuffle':
            end = min(len(sorted_data), start + batch_size)
        else:
            # NOTE: input and output are reversed due to the use of same json as asr
            ilen = int(sorted_data[start][1]['output'][0]['shape'][0])
            olen = int(sorted_data[start][1]['input'][0]['shape'][0])
            factor = max(int(ilen / max_length_in), int(olen / max_length_out))
            # change batchsize depending on the input and output length
            # if ilen = 1000 and max_length_in = 800
            # then b = batchsize / 2
            # and max(1, .) avoids batchsize = 0
            bs = max(1, int(batch_size / (1 + factor)))
            end = min(len(sorted_data), start + bs)

        # check each batch is more than minimum batchsize
        minibatch = sorted_data[start:end]
        if shortest_first:
            minibatch.reverse()
        if len(minibatch) < min_batch_size:
            mod = min_batch_size - len(minibatch) % min_batch_size
            additional_minibatch = [sorted_data[i] for i in np.random.randint(0, start, mod)]
            if shortest_first:
                additional_minibatch.reverse()
            minibatch.extend(additional_minibatch)
        minibatches.append(minibatch)

        if end == len(sorted_data):
            break
        start = end

    # for debugging
    if num_batches > 0:
        minibatches = minibatches[:num_batches]
    logging.info('# minibatches: ' + str(len(minibatches)))

    return minibatches


def get_dimensions(args):
    # get input and output dimension info
    with open(args.valid_json, 'rb') as f:
        valid_json = json.load(f)['utts']
    utts = list(valid_json.keys())

    # reverse input and output dimension
    idim = int(valid_json[utts[0]]['output'][0]['shape'][1])
    odim = int(valid_json[utts[0]]['input'][0]['shape'][1])
    if args.use_cbhg:
        args.spc_dim = int(valid_json[utts[0]]['input'][1]['shape'][1])
    if args.use_speaker_embedding:
        args.spk_embed_dim = int(valid_json[utts[0]]['input'][1]['shape'][0])
    else:
        args.spk_embed_dim = None
    logging.info('#input dims : ' + str(idim))
    logging.info('#output dims: ' + str(odim))
    return idim, odim


def get_plot_report_keys(use_cbhg):
    to_report = [("l1_loss", ['main/l1_loss', 'validation/main/l1_loss']),
                 ("mse_loss", ['main/mse_loss', 'validation/main/mse_loss']),
                 ("bce_loss", ['main/bce_loss', 'validation/main/bce_loss'])]
    # Make a plot for training and validation values
    plot_keys = ['main/loss', 'validation/main/loss',
                 'main/l1_loss', 'validation/main/l1_loss',
                 'main/mse_loss', 'validation/main/mse_loss',
                 'main/bce_loss', 'validation/main/bce_loss']
    if use_cbhg:
        plot_keys += ['main/cbhg_l1_loss', 'validation/main/cbhg_l1_loss',
                      'main/cbhg_mse_loss', 'validation/main/cbhg_mse_loss']
        to_report.append(("cbhg_l1_loss", ['main/cbhg_l1_loss', 'validation/main/cbhg_l1_loss']))
        to_report.append(("cbhg_mse_loss", ['main/cbhg_mse_loss', 'validation/main/cbhg_mse_loss']))
    to_report.append(("loss", plot_keys))
    return plot_keys


def add_progress_report(trainer, keys):
    trainer.extend(extensions.LogReport(trigger=(REPORT_INTERVAL, 'iteration')))
    keys[0:0] = ['epoch', 'iteration', 'elapsed_time']
    trainer.extend(extensions.PrintReport(keys), trigger=(REPORT_INTERVAL, 'iteration'))
    trainer.extend(extensions.ProgressBar(update_interval=REPORT_INTERVAL))


def prepare_trainer(updater, evaluator, converter, model, train_iters, valid_json, args, device):
    """Prepares a tts trainer with common extensions

    :param updater: The training updater
    :param evaluator: The training evaluator
    :param converter: The batch converter
    :param model: The model
    :param train_iters: The training iterator(s)
    :param valid_json: The validation json
    :param args: The program arguments
    :param device: The device to use
    :return: The trainer
    """
    plot_keys = get_plot_report_keys(args.use_cbhg)
    trainer = prepare_asr_tts_trainer(updater, evaluator, converter, model, train_iters, valid_json, args, device,
                                      plot_keys,
                                      reverse_par=True)
    add_progress_report(trainer, plot_keys[:])
    return trainer
