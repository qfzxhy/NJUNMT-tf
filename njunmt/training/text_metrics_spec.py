# Copyright 2017 Natural Language Processing Group, Nanjing University, zhaocq.nlp@gmail.com.
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
""" Define evaluation metric hooks and build_eval_metrics function. """
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import os
import time
from abc import ABCMeta, abstractmethod

import numpy
import six

import tensorflow as tf
from tensorflow import gfile
from tensorflow.python.training import saver as saver_lib

from njunmt.data.text_inputter import ParallelTextInputter
from njunmt.data.text_inputter import TextLineInputter
from njunmt.inference.decode import evaluate
from njunmt.inference.decode import infer
from njunmt.utils.model_builder import model_fn
from njunmt.utils.configurable import update_infer_params
from njunmt.utils.global_names import ModeKeys
from njunmt.utils.global_names import GlobalNames
from njunmt.utils.metrics import multi_bleu_score
from njunmt.utils.expert_utils import StepTimer
from njunmt.utils.misc import get_dict_from_collection
from njunmt.utils.summary_writer import SummaryWriter


def build_eval_metrics(model_configs, dataset, is_cheif=True, model_name=None):
    """ Builds training hooks to evaluate model performance.

    Args:
        model_configs: A dictionary of all configurations.
        dataset: A `Dataset` object.
        is_chief: Whether this is the chief process. Only chief process saves
          summaries.
        model_name: A string, the top scope name of all variables.

    Returns: A list of `tf.train.SessionRunHook` objects.
    """
    metrics = []
    if "metrics" in model_configs and isinstance(model_configs["metrics"], list):
        for metric in model_configs["metrics"]:
            metrics.append(eval(metric["class"])(
                model_configs, dataset,
                do_summary=is_cheif,
                model_name=model_name,
                **metric["params"]))
    return metrics


@six.add_metaclass(ABCMeta)
class TextMetricSpec(tf.train.SessionRunHook):
    """ Define base class for metric hook.  """

    def __init__(self,
                 model_configs,
                 dataset,
                 start_at=0,
                 eval_steps=100,
                 do_summary=True,
                 model_name=None):
        """ Initializes base metric hook.

        Args:
            model_configs: A dictionary of all configurations.
            dataset: A `Dataset` object.
            start_at: A python integer, start to evaluate model at this step.
            eval_steps: A python integer, evaluate model every N steps.
            do_summary: Whether to save summaries.
            model_name: A string, the top scope name of all variables.
        """
        self._model_configs = copy.deepcopy(model_configs)
        self._dataset = dataset
        self._checkpoint_dir = model_configs["model_dir"]
        self._start_at = start_at
        self._eval_steps = eval_steps
        self._do_summary = do_summary
        self._global_step = tf.train.get_global_step()
        self._summary_writer = None
        self._timer = None
        self._model_name = model_name

    def begin(self):
        """ Creates StepTimer and SummaryWriter.

        Calls `_prepare()` method implemented by derived classes.
        """
        self._prepare()
        self._timer = StepTimer(every_steps=self._eval_steps,
                                start_at=self._start_at)
        if self._do_summary:
            self._summary_writer = SummaryWriter(self._checkpoint_dir)

    def before_run(self, run_context):
        """  Called before each call to run().

        Args:
            run_context: A `SessionRunContext` object.

        Returns: A `SessionRunArgs` object containing global_step.
        """
        self._timer.register_before_run()
        return tf.train.SessionRunArgs(self._global_step)

    def after_run(self, run_context, run_values):
        """ Checks running steps and do evaluation.

        Calls `_do_evaluation()` method implemented by derived classes.
        Args:
            run_context: A `SessionRunContext` object.
            run_values: A SessionRunValues object.
        """
        global_step = run_values.results
        if self._timer.should_trigger_for_step(global_step):
            self._do_evaluation(run_context, global_step)
            self._timer.update_last_triggered_step(global_step)

    @abstractmethod
    def _prepare(self):
        """ Prepares for evaluation, e.g. building the model (reusing variables)
        """
        raise NotImplementedError

    @abstractmethod
    def _do_evaluation(self, run_context, global_step):
        """ Evaluates the model.

        Args:
            run_context: A `SessionRunContext` object.
            global_step: A python integer, the current training step.
        """
        raise NotImplementedError


class LossMetricSpec(TextMetricSpec):
    """ Define a hook that evaluates the model by loss function. """

    def __init__(self,
                 model_configs,
                 dataset,
                 start_at=0,
                 eval_steps=100,
                 batch_size=128,
                 do_summary=True,
                 model_name=None):
        """ Initializes the metric hook.

        Args:
            model_configs: A dictionary of all configurations.
            dataset: A `Dataset` object.
            start_at: A python integer, start to evaluate model at this step.
            eval_steps: A python integer, evaluate model every N steps.
            batch_size: A python integer, the batch size for each running step.
            do_summary: Whether to save summaries.
            model_name: A string, the top scope name of all variables.
        """
        super(LossMetricSpec, self).__init__(model_configs, dataset,
                                             start_at, eval_steps,
                                             do_summary, model_name)
        tf.logging.info("Create LossMetric")
        self._batch_size = batch_size

    def _prepare(self):
        """ Prepares for evaluation.

        Builds the model with reuse=True, mode=EVAL and preprocesses
        data file(s).

        Furthermore, if the decay_type of optimizer is "loss_decay", creates
        the controller variables/operations.
        """
        text_inputter = ParallelTextInputter(
            dataset=self._dataset,
            features_field_name="eval_features_file",
            labels_field_name="eval_labels_file",
            batch_size=self._batch_size,
            batch_tokens_size=None,
            maximum_features_length=None,
            maximum_labels_length=None,
            shuffle_every_epoch=None,
            bucketing=True)
        self._eval_feeding_data = text_inputter.make_feeding_data()
        estimator_spec = model_fn(model_configs=self._model_configs, mode=ModeKeys.EVAL, dataset=self._dataset,
                                  name=self._model_name, reuse=True, verbose=False)
        self._loss_op = estimator_spec.loss
        # for learning decay decay
        self._half_lr = False
        if self._model_configs["optimizer_params"]["optimizer.lr_decay"]["decay_type"] == "loss_decay":
            self._half_lr = True
            lr_tensor_dict = get_dict_from_collection(GlobalNames.LEARNING_RATE_VAR_NAME)
            self._learning_rate = lr_tensor_dict[GlobalNames.LEARNING_RATE_VAR_NAME]
            self._max_patience = self._model_configs["optimizer_params"]["optimizer.lr_decay"]["patience"]
            div_factor = lr_tensor_dict[GlobalNames.LR_ANNEAL_DIV_FACTOR_NAME]
            self._half_lr_op = div_factor.assign(div_factor * 2.)
            self._patience = 0
            self._min_loss = 10000.

    def _do_evaluation(self, run_context, global_step):
        """ Evaluates the model by loss function.

        Furthermore, if the decay_type of optimizer is "loss_decay", anneal
        the learning rate at the right time.

        Args:
            run_context: A `SessionRunContext` object.
            global_step: A python integer, the current training step.
        """
        loss = evaluate(
            sess=run_context.session,
            eval_op=self._loss_op,
            feeding_data=self._eval_feeding_data)
        tf.logging.info("Evaluating DEVSET: DevLoss=%f  GlobalStep=%d" % (loss, global_step))
        if self._summary_writer is not None:
            self._summary_writer.add_summary("Metrics/DevLoss", loss, global_step)
        if self._half_lr:
            if loss <= self._min_loss:
                self._min_loss = loss
                self._patience = 0
            else:
                self._patience += 1
                if self._patience >= self._max_patience:
                    self._patience = 0
                    run_context.session.run(self._half_lr_op)
                    now_lr = run_context.session.run(self._learning_rate)
                    tf.logging.info("Hit maximum patience=%d. HALF THE LEARNING RATE TO %f at %d"
                                    % (self._max_patience, now_lr, global_step))


class BleuMetricSpec(TextMetricSpec):
    """ Define a hook that evaluates the model via BLEU score. """

    def __init__(self,
                 model_configs,
                 dataset,
                 start_at=0,
                 eval_steps=1000,
                 batch_size=None,
                 beam_size=None,
                 maximum_labels_length=None,
                 length_penalty=None,
                 delimiter=" ",
                 maximum_keep_models=5,
                 multibleu_script="./njunmt/tools/multi-bleu.perl",
                 tokenize_script="./njunmt/tools/tokenizeChinese.py",
                 char_level=False,
                 early_stop=True,
                 estop_patience=30,
                 do_summary=True,
                 model_name=None):
        """ Initializes the metric hook.

        Args:
            model_configs: A dictionary of all configurations.
            dataset: A `Dataset` object.
            start_at: A python integer, start to evaluate model at this step.
            eval_steps: A python integer, evaluate model every N steps.
            batch_size: A python integer, the batch size for each inference step.
            beam_size: A python integer, the beam width of inference.
            maximum_labels_length: A python integer, the maximum lengths that model
              generates.
            length_penalty: A python float, length penalty rate. If not provided
              or < 0, simply average each beam by length of predicted
              sequence.
            delimiter: The delimiter of output token sequence.
            maximum_keep_models: The maximum number of models that will have a
              backup according to the BLEU score.
            multibleu_script: The multi-bleu script path.
            tokenize_script: The tokenizeChinese.py script path.
            char_level: Whether to split words into characters (only for Chinese).
            early_stop: Whether to early stop the program when the model does not
              improve BLEU anymore.
            estop_patience: A python integer, the maximum patience for early stop.
            do_summary: Whether to save summaries.
            model_name: A string, the top scope name of all variables.
        """
        super(BleuMetricSpec, self).__init__(model_configs, dataset,
                                             start_at, eval_steps,
                                             do_summary, model_name)
        tf.logging.info("Create BleuMetric.")
        self._batch_size = batch_size
        self._beam_size = beam_size
        self._maximum_labels_length = maximum_labels_length
        self._length_penalty = length_penalty
        self._delimiter = delimiter
        self._char_level = char_level
        self._tokenize_script = tokenize_script
        self._multibleu_script = multibleu_script
        self._early_stop = early_stop
        self._estop_patience_max = estop_patience
        self._maximum_keep_models = maximum_keep_models

        self._best_checkpoint_bleus = list()
        self._best_checkpoint_names = list()

    def _read_ckpt_bleulog(self):
        """Read the best BLEU scores and the name of corresponding
        checkpoint archives from log file."""
        if gfile.Exists(GlobalNames.TOP_BLEU_CKPTLOG_FILENAME):
            with gfile.GFile(GlobalNames.TOP_BLEU_CKPTLOG_FILENAME, "r") as fp:
                self._best_checkpoint_bleus = [float(x) for x in fp.readline().strip().split(",")]
                self._best_checkpoint_names = [x for x in fp.readline().strip().split(",")]

    def _write_ckpt_bleulog(self):
        """Write the best BLEU scores and the name of corresponding
        checkpoint archives to log file."""
        with gfile.GFile(GlobalNames.TOP_BLEU_CKPTLOG_FILENAME, "w") as fw:
            fw.write(','.join([str(x) for x in self._best_checkpoint_bleus]) + "\n")
            fw.write(','.join([x for x in self._best_checkpoint_names]) + "\n")

    def _check_bleu_script(self):
        """ Checks the correctness of the multi-bleu script.

        Returns: True/False

        Raises:
            OSError: if multi-bleu script not exists, or if
              evaluation labels file not exits, or if BLEU score
              is not correct.
        """
        if not gfile.Exists(self._multibleu_script):
            raise OSError("File not found. Fail to open multi-bleu scrip: {}"
                          .format(self._multibleu_script))
        if gfile.Exists(self._eval_labels_file):
            pseudo_predictions = self._eval_labels_file
        else:
            pseudo_predictions = self._eval_labels_file + "0"
            if not gfile.Exists(pseudo_predictions):
                raise OSError("File not found. Fail to open eval_labels_file: {} or {}"
                              .format(self._eval_labels_file, pseudo_predictions))
        score = multi_bleu_score(self._multibleu_script, self._eval_labels_file, pseudo_predictions)
        if int(score) < 100:
            raise OSError("Fail to run multi-bleu scrip: {}. "
                          "The evaluation output is {} which should be 100"
                          .format(self._multibleu_script, score))

    def _prepare(self):
        """ Prepares for evaluation.

        Builds the model with reuse=True, mode=EVAL and preprocesses
        data file(s).
        """
        text_inputter = TextLineInputter(
            dataset=self._dataset,
            data_field_name="eval_features_file",
            batch_size=self._batch_size,
            maximum_line_length=None)
        self._eval_feeding_data = text_inputter.make_feeding_data()
        self._model_configs = update_infer_params(  # update inference parameters
            self._model_configs,
            beam_size=self._beam_size,
            maximum_labels_length=self._maximum_labels_length,
            length_penalty=self._length_penalty)
        estimator_spec = model_fn(model_configs=self._model_configs, mode=ModeKeys.INFER, dataset=self._dataset,
                                  name=self._model_name, reuse=True, verbose=False)
        self._predict_ops = estimator_spec.predictions
        tmp_trans_dir = os.path.join(self._model_configs["model_dir"], GlobalNames.TMP_TRANS_DIRNAME)
        if not gfile.Exists(tmp_trans_dir):
            gfile.MakeDirs(tmp_trans_dir)
        self._tmp_trans_file_prefix = os.path.join(tmp_trans_dir, GlobalNames.TMP_TRANS_FILENAME_PREFIX)
        self._read_ckpt_bleulog()
        self._eval_labels_file = self._dataset.eval_labels_file
        self._check_bleu_script()
        self._estop_patience = 0
        self._best_bleu_score = 0.

    def _do_evaluation(self, run_context, global_step):
        """ Infers the evaluation data and computes the BLEU score.

        Args:
            run_context: A `SessionRunContext` object.
            global_step: A python integer, the current training step.
        """
        start_time = time.time()
        output_prediction_file = self._tmp_trans_file_prefix + str(global_step)
        samples_src, samples_trg = infer(
            sess=run_context.session,
            prediction_op=self._predict_ops,
            feeding_data=self._eval_feeding_data,
            output=output_prediction_file,
            vocab_target=self._dataset.vocab_target,
            alpha=self._model_configs["model_params"]["inference.length_penalty"],
            delimiter=self._delimiter,
            output_attention=False,
            tokenize_output=self._char_level,
            tokenize_script=self._tokenize_script,
            verbose=False)
        # print translation samples
        for idx, (s, p) in enumerate(zip(samples_src, samples_trg)):
            tf.logging.info("Sample%d Source: %s" % (idx, s))
            tf.logging.info("Sample%d Prediction: %s\n" % (idx, p))
        # evaluate with BLEU
        bleu = multi_bleu_score(self._multibleu_script, self._eval_labels_file, output_prediction_file)
        if self._summary_writer is not None:
            self._summary_writer.add_summary("Metrics/BLEU", bleu, global_step)
        _, elapsed_time_all = self._timer.update_last_triggered_step(global_step)
        tf.logging.info("Evaluating DEVSET: BLEU=%.2f (Best %.2f)  GlobalStep=%d    UD %.2f   UDfromStart %.2f"
                        % (bleu, self._best_bleu_score, global_step,
                           time.time() - start_time, elapsed_time_all))
        self._update_bleu_ckpt(run_context, bleu, global_step)

    def _update_bleu_ckpt(self, run_context, bleu, global_step):
        """ Updates the best checkpoints according to BLEU score and
        removes the worst model if the number of checkpoint archives
        exceeds maximum_keep_models.

        If the model does not improves BLEU score anymore (hits the
        maximum patience), request stop session.

        Args:
            run_context: A `SessionRunContext` object.
            bleu: A python float, the BLEU score derived by the model
              at this step.
            global_step: A python integer, the current training step.
        """
        if bleu >= self._best_bleu_score:
            self._best_bleu_score = bleu
            self._estop_patience = 0
        else:
            self._estop_patience += 1
        if self._estop_patience >= self._estop_patience_max and self._early_stop:
            tf.logging.info("early stop.")
            run_context.request_stop()
        # saving checkpoints if eval_steps and save_checkpoint_steps mismatch
        if not gfile.Exists("{}-{}.meta".format(
                os.path.join(self._checkpoint_dir, GlobalNames.MODEL_CKPT_FILENAME), global_step)):
            saver = saver_lib._get_saver_or_default()
            saver.save(run_context.session,
                       os.path.join(self._checkpoint_dir, GlobalNames.MODEL_CKPT_FILENAME),
                       global_step=global_step)
        if len(self._best_checkpoint_names) == 0 or bleu > self._best_checkpoint_bleus[0]:
            tarname = "{}{}.tar.gz".format(GlobalNames.CKPT_TGZ_FILENAME_PREFIX, global_step)
            os.system("tar -zcvf {tarname} {checkpoint} {model_config} {model_analysis} {ckptdir}/*{global_step}*"
                .format(
                tarname=tarname,
                checkpoint=os.path.join(self._checkpoint_dir, "checkpoint"),
                model_config=os.path.join(self._checkpoint_dir, GlobalNames.MODEL_CONFIG_YAML_FILENAME),
                model_analysis=os.path.join(self._checkpoint_dir, GlobalNames.MODEL_ANALYSIS_FILENAME),
                ckptdir=self._checkpoint_dir,
                global_step=global_step))
            self._best_checkpoint_bleus.append(bleu)
            self._best_checkpoint_names.append(tarname)
            if len(self._best_checkpoint_bleus) > self._maximum_keep_models:
                tidx = numpy.argsort(self._best_checkpoint_bleus)
                _bleus = [self._best_checkpoint_bleus[i] for i in tidx]
                _names = [self._best_checkpoint_names[i] for i in tidx]
                self._best_checkpoint_bleus = _bleus[1:]
                self._best_checkpoint_names = _names[1:]
                os.system("rm {}".format(_names[0]))
            self._write_ckpt_bleulog()
