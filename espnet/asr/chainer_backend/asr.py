#!/usr/bin/env python

# Copyright 2017 Johns Hopkins University (Shinji Watanabe)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

from __future__ import division

import collections
import logging
import math
import six

# chainer related
import chainer

from chainer import cuda
from chainer import training
from chainer import Variable

from chainer.datasets import TransformDataset

from chainer.training import extensions
from chainer.training.updaters.multiprocess_parallel_updater import gather_grads
from chainer.training.updaters.multiprocess_parallel_updater import gather_params
from chainer.training.updaters.multiprocess_parallel_updater import scatter_grads

# espnet related
from espnet.asr.asr_utils import get_dimensions

from espnet.asr.asr_utils import make_batchset
from espnet.asr.asr_utils import prepare_trainer
from espnet.asr.asr_utils import single_beam_search
from espnet.asr.asr_utils import write_results

from espnet.nets.chainer_backend.e2e_asr import E2E
from espnet.utils.io_utils import LoadInputsAndTargets

from espnet.utils.training.iterators import ShufflingEnabler
from espnet.utils.training.iterators import ToggleableShufflingMultiprocessIterator
from espnet.utils.training.iterators import ToggleableShufflingSerialIterator

from espnet.utils.deterministic_utils import set_deterministic_chainer
from espnet.utils.training.train_utils import check_early_stop
from espnet.utils.training.train_utils import get_model_conf
from espnet.utils.training.train_utils import load_json
from espnet.utils.training.train_utils import load_jsons
from espnet.utils.training.train_utils import write_conf

from espnet.utils.chainer_utils import chainer_load
from espnet.utils.chainer_utils import warn_if_no_cuda

# rnnlm
import espnet.lm.chainer_backend.extlm as extlm_chainer
import espnet.lm.chainer_backend.lm as lm_chainer

# numpy related
import numpy as np


# copied from https://github.com/chainer/chainer/blob/master/chainer/optimizer.py
def sum_sqnorm(arr):
    sq_sum = collections.defaultdict(float)
    for x in arr:
        with cuda.get_device_from_array(x) as dev:
            if x is not None:
                x = x.ravel()
                s = x.dot(x)
                sq_sum[int(dev)] += s
    return sum([float(i) for i in six.itervalues(sq_sum)])


class CustomUpdater(training.StandardUpdater):
    """Custom updater for chainer"""

    def __init__(self, train_iter, optimizer, converter, device):
        super(CustomUpdater, self).__init__(
            train_iter, optimizer, converter=converter, device=device)

    # The core part of the update routine can be customized by overriding.
    def update_core(self):
        # When we pass one iterator and optimizer to StandardUpdater.__init__,
        # they are automatically named 'main'.
        train_iter = self.get_iterator('main')
        optimizer = self.get_optimizer('main')

        # Get batch and convert into variables
        batch = train_iter.next()
        x = self.converter(batch, self.device)

        # Compute the loss at this time step and accumulate it
        loss = optimizer.target(*x)
        optimizer.target.cleargrads()  # Clear the parameter gradients
        loss.backward()  # Backprop
        loss.unchain_backward()  # Truncate the graph
        # compute the gradient norm to check if it is normal or not
        grad_norm = np.sqrt(sum_sqnorm(
            [p.grad for p in optimizer.target.params(False)]))
        logging.info('grad norm={}'.format(grad_norm))
        if math.isnan(grad_norm):
            logging.warning('grad norm is nan. Do not update model.')
        else:
            optimizer.update()


class CustomParallelUpdater(training.updaters.MultiprocessParallelUpdater):
    """Custom parallel updater for chainer"""

    def __init__(self, train_iters, optimizer, converter, devices):
        super(CustomParallelUpdater, self).__init__(
            train_iters, optimizer, converter=converter, devices=devices)

    # The core part of the update routine can be customized by overriding.
    def update_core(self):
        self.setup_workers()

        self._send_message(('update', None))
        with cuda.Device(self._devices[0]):
            from cupy.cuda import nccl
            # For reducing memory
            self._master.cleargrads()

            optimizer = self.get_optimizer('main')
            batch = self.get_iterator('main').next()
            x = self.converter(batch, self._devices[0])

            loss = self._master(*x)

            self._master.cleargrads()
            loss.backward()
            loss.unchain_backward()

            # NCCL: reduce grads
            null_stream = cuda.Stream.null
            if self.comm is not None:
                gg = gather_grads(self._master)
                self.comm.reduce(gg.data.ptr, gg.data.ptr, gg.size,
                                 nccl.NCCL_FLOAT,
                                 nccl.NCCL_SUM,
                                 0, null_stream.ptr)
                scatter_grads(self._master, gg)
                del gg

            # check gradient value
            grad_norm = np.sqrt(sum_sqnorm(
                [p.grad for p in optimizer.target.params(False)]))
            logging.info('grad norm={}'.format(grad_norm))

            # update
            if math.isnan(grad_norm):
                logging.warning('grad norm is nan. Do not update model.')
            else:
                optimizer.update()

            if self.comm is not None:
                gp = gather_params(self._master)
                self.comm.bcast(gp.data.ptr, gp.size, nccl.NCCL_FLOAT,
                                0, null_stream.ptr)


class CustomConverter(object):
    """Custom Converter

    :param int subsampling_factor : The subsampling factor
    """

    def __init__(self, subsampling_factor=1, preprocess_conf=None):
        self.subsampling_factor = subsampling_factor
        self.load_inputs_and_targets = LoadInputsAndTargets(
            mode='asr', load_output=True, preprocess_conf=preprocess_conf)

    def transform(self, item):
        return self.load_inputs_and_targets(item)

    def __call__(self, batch, device):
        # set device
        xp = cuda.cupy if device != -1 else np

        # batch should be located in list
        assert len(batch) == 1
        xs, ys = batch[0]

        # perform subsampling
        if self.subsampling_factor > 1:
            xs = [x[::self.subsampling_factor, :] for x in xs]

        # get batch of lengths of input sequences
        ilens = [x.shape[0] for x in xs]

        # convert to Variable
        xs = [Variable(xp.array(x, dtype=xp.float32)) for x in xs]
        ilens = xp.array(ilens, dtype=xp.int32)
        ys = [Variable(xp.array(y, dtype=xp.int32)) for y in ys]

        return xs, ilens, ys


def train(args):
    """Train with the given args

    :param Namespace args: The program arguments
    """
    # display chainer version
    logging.info('chainer version = ' + chainer.__version__)

    set_deterministic_chainer(args)

    warn_if_no_cuda()

    # get input and output dimension info
    idim, odim = get_dimensions(args.valid_json)

    # check attention type
    if args.atype not in ['noatt', 'dot', 'location']:
        raise NotImplementedError('chainer supports only noatt, dot, and location attention.')

    # specify model architecture
    model = E2E(idim, odim, args, flag_return=False)

    # write model config
    write_conf(args, idim, odim)

    # Set gpu
    ngpu = args.ngpu
    if ngpu == 1:
        gpu_id = 0
        # Make a specified GPU current
        chainer.cuda.get_device_from_id(gpu_id).use()
        model.to_gpu()  # Copy the model to the GPU
        logging.info('single gpu calculation.')
    elif ngpu > 1:
        gpu_id = 0
        devices = {'main': gpu_id}
        for gid in six.moves.xrange(1, ngpu):
            devices['sub_%d' % gid] = gid
        logging.info('multi gpu calculation (#gpus = %d).' % ngpu)
        logging.info('batch size is automatically increased (%d -> %d)' % (
            args.batch_size, args.batch_size * args.ngpu))
    else:
        gpu_id = -1
        logging.info('cpu calculation')

    # Setup an optimizer
    if args.opt == 'adadelta':
        optimizer = chainer.optimizers.AdaDelta(eps=args.eps)
    elif args.opt == 'adam':
        optimizer = chainer.optimizers.Adam()
    optimizer.setup(model)
    optimizer.add_hook(chainer.optimizer.GradientClipping(args.grad_clip))

    train_json, valid_json = load_jsons(args)

    # set up training iterator and updater
    converter = CustomConverter(subsampling_factor=model.subsample[0],
                                preprocess_conf=args.preprocess_conf)
    use_sortagrad = args.sortagrad == -1 or args.sortagrad > 0
    if ngpu <= 1:
        # make minibatch list (variable length)
        train = make_batchset(train_json, args.batch_size,
                              args.maxlen_in, args.maxlen_out, args.minibatches, shortest_first=use_sortagrad)
        # hack to make batchsize argument as 1
        # actual batchsize is included in a list
        if args.n_iter_processes > 0:
            train_iters = list(ToggleableShufflingMultiprocessIterator(
                TransformDataset(train, converter.transform),
                batch_size=1, n_processes=args.n_iter_processes, n_prefetch=8, maxtasksperchild=20,
                shuffle=not use_sortagrad))
        else:
            train_iters = list(ToggleableShufflingSerialIterator(
                TransformDataset(train, converter.transform),
                batch_size=1, shuffle=not use_sortagrad))

        # set up updater
        updater = CustomUpdater(
            train_iters[0], optimizer, converter=converter, device=gpu_id)
    else:
        # set up minibatches
        train_subsets = []
        for gid in six.moves.xrange(ngpu):
            # make subset
            train_json_subset = {k: v for i, (k, v) in enumerate(train_json.items())
                                 if i % ngpu == gid}
            # make minibatch list (variable length)
            train_subsets += [make_batchset(train_json_subset, args.batch_size,
                                            args.maxlen_in, args.maxlen_out, args.minibatches)]

        # each subset must have same length for MultiprocessParallelUpdater
        maxlen = max([len(train_subset) for train_subset in train_subsets])
        for train_subset in train_subsets:
            if maxlen != len(train_subset):
                for i in six.moves.xrange(maxlen - len(train_subset)):
                    train_subset += [train_subset[i]]

        # hack to make batchsize argument as 1
        # actual batchsize is included in a list
        if args.n_iter_processes > 0:
            train_iters = [ToggleableShufflingMultiprocessIterator(
                TransformDataset(train_subsets[gid], converter.transform),
                batch_size=1, n_processes=args.n_iter_processes, n_prefetch=8, maxtasksperchild=20,
                shuffle=not use_sortagrad)
                for gid in six.moves.xrange(ngpu)]
        else:
            train_iters = [ToggleableShufflingSerialIterator(
                TransformDataset(train_subsets[gid], converter.transform),
                batch_size=1, shuffle=not use_sortagrad)
                for gid in six.moves.xrange(ngpu)]

        # set up updater
        updater = CustomParallelUpdater(
            train_iters, optimizer, converter=converter, devices=devices)
    valid = make_batchset(valid_json, args.batch_size,
                          args.maxlen_in, args.maxlen_out, args.minibatches)
    if args.n_iter_processes > 0:
        valid_iter = chainer.iterators.MultiprocessIterator(
            TransformDataset(valid, converter.transform),
            batch_size=1, repeat=False, shuffle=False,
            n_processes=args.n_iter_processes, n_prefetch=8, maxtasksperchild=20)
    else:
        valid_iter = chainer.iterators.SerialIterator(
            TransformDataset(valid, converter.transform),
            batch_size=1, repeat=False, shuffle=False)
    evaluator = extensions.Evaluator(valid_iter, model, converter=converter, device=gpu_id)

    trainer = prepare_trainer(updater, evaluator, converter, model, train_iters, valid_json, args, gpu_id)

    # Run the training
    trainer.run()
    check_early_stop(trainer, args.epochs)


def recog(args):
    """Decode with the given args

    :param Namespace args: The program arguments
    """
    # display chainer version
    logging.info('chainer version = ' + chainer.__version__)

    set_deterministic_chainer(args)

    # read training config
    idim, odim, train_args = get_model_conf(args.model, args.model_conf)

    for key in sorted(vars(args).keys()):
        logging.info('ARGS: ' + key + ': ' + str(vars(args)[key]))

    # specify model architecture
    logging.info('reading model parameters from ' + args.model)
    model = E2E(idim, odim, train_args)
    chainer_load(args.model, model)

    # read rnnlm
    if args.rnnlm:
        rnnlm_args = get_model_conf(args.rnnlm, args.rnnlm_conf)
        rnnlm = lm_chainer.ClassifierWithState(lm_chainer.RNNLM(
            len(train_args.char_list), rnnlm_args.layer, rnnlm_args.unit))
        chainer_load(args.rnnlm, rnnlm)
    else:
        rnnlm = None

    if args.word_rnnlm:
        rnnlm_args = get_model_conf(args.word_rnnlm, args.word_rnnlm_conf)
        word_dict = rnnlm_args.char_list_dict
        char_dict = {x: i for i, x in enumerate(train_args.char_list)}
        word_rnnlm = lm_chainer.ClassifierWithState(lm_chainer.RNNLM(
            len(word_dict), rnnlm_args.layer, rnnlm_args.unit))
        chainer_load(args.word_rnnlm, word_rnnlm)

        if rnnlm is not None:
            rnnlm = lm_chainer.ClassifierWithState(
                extlm_chainer.MultiLevelLM(word_rnnlm.predictor,
                                           rnnlm.predictor, word_dict, char_dict))
        else:
            rnnlm = lm_chainer.ClassifierWithState(
                extlm_chainer.LookAheadWordLM(word_rnnlm.predictor,
                                              word_dict, char_dict))

    js = load_json(args.recog_json)

    load_inputs_and_targets = LoadInputsAndTargets(
        mode='asr', load_output=False, sort_in_input_length=False,
        preprocess_conf=train_args.preprocess_conf
        if args.preprocess_conf is None else args.preprocess_conf)

    # decode each utterance
    new_js = single_beam_search(model, js, args, train_args, rnnlm, load_inputs_and_targets)
    write_results(new_js, args.result_label)
