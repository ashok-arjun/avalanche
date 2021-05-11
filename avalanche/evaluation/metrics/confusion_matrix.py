################################################################################
# Copyright (c) 2021 ContinualAI.                                              #
# Copyrights licensed under the MIT License.                                   #
# See the accompanying LICENSE file for terms.                                 #
#                                                                              #
# Date: 14-12-2020                                                             #
# Author(s): Lorenzo Pellegrini                                                #
# E-mail: contact@continualai.org                                              #
# Website: www.continualai.org                                                 #
################################################################################

from typing_extensions import Literal
from typing import Callable, Union, Optional, Mapping, TYPE_CHECKING

import numpy as np
import torch
from PIL.Image import Image
from torch import Tensor
from torch.nn.functional import pad

from avalanche.evaluation import PluginMetric, Metric
from avalanche.evaluation.metric_results import AlternativeValues, \
    MetricValue, MetricResult
from avalanche.evaluation.metric_utils import default_cm_image_creator, \
    phase_and_task, stream_type
if TYPE_CHECKING:
    from avalanche.training import BaseStrategy


class ConfusionMatrix(Metric[Tensor]):
    """
    The standalone confusion matrix metric.

    Instances of this metric keep track of the confusion matrix by receiving a
    pair of "ground truth" and "prediction" Tensors describing the labels of a
    minibatch. Those two tensors can both contain plain labels or
    one-hot/logit vectors.

    The result is the unnormalized running confusion matrix.

    Beware that by default the confusion matrix size will depend on the value of
    the maximum label as detected by looking at both the ground truth and
    predictions Tensors. When passing one-hot/logit vectors, this
    metric will try to infer the number of classes from the vector sizes.
    Otherwise, the maximum label value encountered in the truth/prediction
    Tensors will be used.

    If the user sets the `num_classes`, then the confusion matrix will always be
    of size `num_classes, num_classes`. Whenever a prediction or label tensor is
    provided as logits, only the first `num_classes` units will be considered in
    the confusion matrix computation. If they are provided as numerical labels,
    each of them has to be smaller than `num_classes`.

    The reset method will bring the metric to its initial state. By default
    this metric in its initial state will return an empty Tensor.
    """

    def __init__(self, num_classes: int = None,
                 normalize: Literal['true', 'pred', 'all'] = None):
        """
        Creates an instance of the standalone confusion matrix metric.

        By default this metric in its initial state will return an empty Tensor.
        The metric can be updated by using the `update` method while the running
        confusion matrix can be retrieved using the `result` method.

        :param num_classes: The number of classes. Defaults to None,
            which means that the number of classes will be inferred from
            ground truth and prediction Tensors (see class description for more
            details). If not None, the confusion matrix will always be of size
            `num_classes, num_classes` and only the first `num_classes` values
            of output logits or target logits will be considered in the update.
            If the output or targets are provided as numerical labels,
            there can be no label greater than `num_classes`.
        :param normalize: how to normalize confusion matrix.
            None to not normalize
        """
        self._cm_tensor: Optional[Tensor] = None
        """
        The Tensor where the running confusion matrix is stored.
        """
        self._num_classes: Optional[int] = num_classes

        self.normalize = normalize

    @torch.no_grad()
    def update(self, true_y: Tensor, predicted_y: Tensor) -> None:
        """
        Update the running confusion matrix given the true and predicted labels.

        :param true_y: The ground truth. Both labels and one-hot vectors
            are supported.
        :param predicted_y: The ground truth. Both labels and logit vectors
            are supported.
        :return: None.
        """
        if len(true_y) != len(predicted_y):
            raise ValueError('Size mismatch for true_y and predicted_y tensors')

        if len(true_y.shape) > 2:
            raise ValueError('Confusion matrix supports labels with at'
                             ' most 2 dimensions')
        if len(predicted_y.shape) > 2:
            raise ValueError('Confusion matrix supports predictions with at '
                             'most 2 dimensions')

        max_label = -1 if self._num_classes is None else self._num_classes - 1

        # SELECT VALID PORTION OF TARGET AND PREDICTIONS
        true_y = torch.as_tensor(true_y)
        if len(true_y.shape) == 2 and self._num_classes is not None:
            true_y = true_y[:, :max_label]
        predicted_y = torch.as_tensor(predicted_y)
        if len(predicted_y.shape) == 2 and self._num_classes is not None:
            predicted_y = predicted_y[:, :max_label]

        # COMPUTE MAX LABEL AND CONVERT TARGET AND PREDICTIONS IF NEEDED
        if len(predicted_y.shape) > 1:
            # Logits -> transform to labels
            if self._num_classes is None:
                max_label = max(max_label, predicted_y.shape[1]-1)
            predicted_y = torch.max(predicted_y, 1)[1]
        else:
            # Labels -> check non-negative
            min_label = torch.min(predicted_y).item()
            if min_label < 0:
                raise ValueError('Label values must be non-negative values')
            if self._num_classes is None:
                max_label = max(max_label, torch.max(predicted_y).item())
            elif torch.max(predicted_y).item() >= self._num_classes:
                raise ValueError("Encountered predicted label larger than"
                                 "num_classes")

        if len(true_y.shape) > 1:
            # Logits -> transform to labels
            if self._num_classes is None:
                max_label = max(max_label, true_y.shape[1]-1)
            true_y = torch.max(true_y, 1)[1]
        else:
            # Labels -> check non-negative
            min_label = torch.min(true_y).item()
            if min_label < 0:
                raise ValueError('Label values must be non-negative values')

            if self._num_classes is None:
                max_label = max(max_label, torch.max(true_y).item())
            elif torch.max(true_y).item() >= self._num_classes:
                raise ValueError("Encountered target label larger than"
                                 "num_classes")

        if max_label < 0:
            raise ValueError('The Confusion Matrix metric can only handle '
                             'positive label values')

        if self._cm_tensor is None:
            # Create the confusion matrix
            self._cm_tensor = torch.zeros((max_label+1, max_label+1),
                                          dtype=torch.long)
        elif max_label >= self._cm_tensor.shape[0]:
            # Enlarge the confusion matrix
            size_diff = 1 + max_label - self._cm_tensor.shape[0]
            self._cm_tensor = pad(self._cm_tensor,
                                  (0, size_diff, 0, size_diff))

        for pattern_idx in range(len(true_y)):
            self._cm_tensor[true_y[pattern_idx]][predicted_y[pattern_idx]] += 1

    def result(self) -> Tensor:
        """
        Retrieves the unnormalized confusion matrix.

        Calling this method will not change the internal state of the metric.

        :return: The running confusion matrix, as a Tensor.
        """
        if self._cm_tensor is None:
            matrix_shape = (0, 0)
            if self._num_classes is not None:
                matrix_shape = (self._num_classes, self._num_classes)
            return torch.zeros(matrix_shape, dtype=torch.long)
        if self.normalize is not None:
            return ConfusionMatrix._normalize_cm(self._cm_tensor,
                                                 self.normalize)
        return self._cm_tensor

    def reset(self) -> None:
        """
        Resets the metric.

        Calling this method will *not* reset the default number of classes
        optionally defined in the constructor optional parameter.

        :return: None.
        """
        self._cm_tensor = None

    @staticmethod
    def _normalize_cm(cm: Tensor,
                      normalization: Literal['true', 'pred', 'all']):
        if normalization not in ('true', 'pred', 'all'):
            raise ValueError('Invalid normalization parameter. Can be \'true\','
                             ' \'pred\' or \'all\'')

        if normalization == 'true':
            cm = cm / cm.sum(dim=1, keepdim=True, dtype=torch.float64)
        elif normalization == 'pred':
            cm = cm / cm.sum(dim=0, keepdim=True, dtype=torch.float64)
        elif normalization == 'all':
            cm = cm / cm.sum(dtype=torch.float64)
        cm = ConfusionMatrix.nan_to_num(cm)
        return cm

    @staticmethod
    def nan_to_num(matrix: Tensor) -> Tensor:
        # if version.parse(torch.__version__) >= version.parse("1.8.0"):
        #    # noinspection PyUnresolvedReferences
        #    return torch.nan_to_num(matrix)

        numpy_ndarray = matrix.numpy()
        numpy_ndarray = np.nan_to_num(numpy_ndarray)
        return torch.tensor(numpy_ndarray, dtype=matrix.dtype)


class StreamConfusionMatrix(PluginMetric[Tensor]):
    """
    The Stream Confusion Matrix metric.
    This plugin metric only works on the eval phase.

    Confusion Matrix computation can be slow if you compute it for a large
    number of classes. We recommend to set `save_image=False` if the runtime
    is too large.

    At the end of the eval phase, this metric logs the confusion matrix
    relative to all the patterns seen during eval.

    The metric can log either a Tensor or a PIL Image representing the
    confusion matrix.
    """

    def __init__(self,
                 num_classes: Union[int, Mapping[int, int]] = None,
                 normalize: Literal['true', 'pred', 'all'] = None,
                 save_image: bool = True,
                 image_creator: Callable[[Tensor], Image] =
                 default_cm_image_creator):
        """
        Creates an instance of the Stream Confusion Matrix metric.

        We recommend to set `save_image=False` if the runtime is too large.
        In fact, a large number of classes may increase the computation time
        of this metric.

        :param num_classes: The number of classes. Defaults to None,
            which means that the number of classes will be inferred from
            ground truth and prediction Tensors (see class description for more
            details). If not None, the confusion matrix will always be of size
            `num_classes, num_classes` and only the first `num_classes` values
            of output logits or target logits will be considered in the update.
            If the output or targets are provided as numerical labels,
            there can be no label greater than `num_classes`.
        :param normalize: Normalizes confusion matrix over the true (rows),
            predicted (columns) conditions or all the population. If None,
            confusion matrix will not be normalized. Valid values are: 'true',
            'pred' and 'all' or None.
        :param save_image: If True, a graphical representation of the confusion
            matrix will be logged, too. If False, only the Tensor representation
            will be logged. Defaults to True.
        :param image_creator: A callable that, given the tensor representation
            of the confusion matrix, returns a graphical representation of the
            matrix as a PIL Image. Defaults to `default_cm_image_creator`.
        """
        super().__init__()

        self._save_image: bool = save_image
        self.num_classes = num_classes
        self.normalize = normalize
        self._matrix: ConfusionMatrix = ConfusionMatrix(num_classes=num_classes,
                                                        normalize=normalize)

        if image_creator is None:
            image_creator = default_cm_image_creator
        self._image_creator: Callable[[Tensor], Image] = image_creator

    def reset(self) -> None:
        self._matrix = ConfusionMatrix(num_classes=self.num_classes,
                                       normalize=self.normalize)

    def result(self) -> Tensor:
        exp_cm = self._matrix.result()
        return exp_cm

    def update(self, true_y: Tensor, predicted_y: Tensor) -> None:
        self._matrix.update(true_y, predicted_y)

    def before_eval(self, strategy) -> None:
        self.reset()

    def after_eval_iteration(self, strategy: 'BaseStrategy') -> None:
        super().after_eval_iteration(strategy)
        self.update(strategy.mb_y,
                    strategy.mb_pred)

    def after_eval(self, strategy: 'BaseStrategy') -> MetricResult:
        return self._package_result(strategy)

    def _package_result(self, strategy: 'BaseStrategy') -> MetricResult:
        exp_cm = self.result()
        phase_name, _ = phase_and_task(strategy)
        stream = stream_type(strategy.experience)
        metric_name = '{}/{}_phase/{}_stream' \
            .format(str(self),
                    phase_name,
                    stream)
        plot_x_position = self.get_global_counter()

        if self._save_image:
            cm_image = self._image_creator(exp_cm)
            metric_representation = MetricValue(
                self, metric_name, AlternativeValues(cm_image, exp_cm),
                plot_x_position)
        else:
            metric_representation = MetricValue(
                self, metric_name, exp_cm, plot_x_position)

        return [metric_representation]

    def __str__(self):
        return "ConfusionMatrix_Stream"


__all__ = [
    'ConfusionMatrix',
    'StreamConfusionMatrix'
]
