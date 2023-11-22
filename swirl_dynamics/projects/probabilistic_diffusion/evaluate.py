# Copyright 2023 The swirl_dynamics Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Modules for evaluating conditional samples."""

from collections.abc import Mapping, Sequence
import dataclasses
from typing import Any

from clu import metrics as clu_metrics
import flax
import jax
import jax.numpy as jnp
from swirl_dynamics.lib.metrics import probabilistic_forecast as prob_metrics
from swirl_dynamics.projects.probabilistic_diffusion import inference
from swirl_dynamics.templates import evaluate


Array = jax.Array
PyTree = Any


@dataclasses.dataclass(frozen=True)
class CondSamplingBenchmark(evaluate.Benchmark):
  """Draws conditional samples and evaluates probabilistic scores.

  Required `batch` schema::

    batch["cond"]: dict[str, jax.Array] | None  # a-priori condition
    batch["guidance_inputs"]: dict[str, jax.Array] | None  # guidance inputs
    batch["obs"]: jax.Array  # observation wrt which the samples are evaluated

  NOTE: Batch size *should always be 1*.

  Attributes:
    num_samples_per_cond: The number of conditional samples to generate per
      condition. The samples are generated in batches.
    sample_batch_size: The batch size to generate conditional samples at.
    brier_score_thresholds: The threshold values to evaluate for the Brier
      scores.
  """

  num_samples_per_cond: int
  sample_batch_size: int
  brier_score_thresholds: Sequence[int | float]

  def __post_init__(self):
    if self.num_samples_per_cond % self.sample_batch_size != 0:
      raise ValueError(
          f"`sample_batch_size` ({self.sample_batch_size}) must be divisible by"
          f" `num_samples_per_cond` ({self.num_samples_per_cond})."
      )

  def run_batch_inference(
      self,
      inference_fn: inference.CondSampler,
      batch: Mapping[str, Any],
      rng: Array,
  ) -> Array:
    """Runs batch inference on a conditional sampler."""
    num_batches = self.num_samples_per_cond // self.sample_batch_size
    rngs = jax.random.split(rng, num=num_batches)

    squeeze_fn = lambda x: jnp.squeeze(x, axis=0)
    cond = jax.tree_map(squeeze_fn, batch["cond"])
    guidance_inputs = (
        batch["guidance_inputs"] if "guidance_inputs" in batch else None
    )

    def _batch_inference_fn(rng: jax.Array) -> jax.Array:
      return inference_fn(
          num_samples=self.sample_batch_size,
          rng=rng,
          cond=cond,
          guidance_inputs=guidance_inputs,
      )

    # using `jax.lax.map` instead of `jax.vmap` because the former is less
    # memory intensive and batch inference is expected to be very demanding
    samples = jax.lax.map(
        _batch_inference_fn, rngs
    )  # ~ (num_batches, batch_size, *spatial_dims, channels)
    samples = jnp.reshape(samples, (1, -1) + samples.shape[2:])
    return samples

  def compute_batch_metrics(
      self, pred: jax.Array, batch: Mapping[str, Any]
  ) -> tuple[dict[str, jax.Array], dict[str, jax.Array]]:
    """Computes metrics on the batch.

    Results consist of collected types and aggregated types (see
    `swirl_dynamics.templates.Benchmark` protocol for their definitions and
    distinctions).

    The collected metrics consist of:
      * The observation
      * A subset of conditional examples generated
      * Channel-wise CRPS
      * Threshold Brier scores
      * Conditional standard deviations

    The aggregated metrics consist of:
      * Global mean CRPS (scalar)
      * Local mean CRPS (averaged for each location)
      * Global mean threshold Brier scores
      * Local mean threshold Brier score (averaged for each location)

    Args:
      pred: The conditional samples generated by a benchmarked model.
      batch: The evaluation batch data containing a reference observation.

    Returns:
      Metrics to be collected and aggregated respectively.
    """
    # pred ~ (1, num_samples, *spatial, channels)
    obs = batch["obs"]  # ~ (1, *spatial, channels)
    cond_stddev = jnp.std(pred, axis=1)  # ~ (1, *spatial, channels)
    crps = prob_metrics.crps(
        pred, obs, direct_broadcast=False
    )  # ~ (1, *spatial, channels)
    thres_brier_scores = prob_metrics.threshold_brier_score(
        pred, obs, jnp.asarray(self.brier_score_thresholds)
    )  # ~ (1, *spatial, channels, n_thresholds);
    batch_collect = dict(
        observation=obs,
        example1=pred[:, 0],
        cond_stddev=cond_stddev,
        crps=crps,
        thres_brier_scores=thres_brier_scores,
    )
    batch_result = dict(
        crps=crps,
        thres_brier_scores=thres_brier_scores,
    )
    return batch_collect, batch_result


class CondSamplingEvaluator(evaluate.Evaluator):
  """Evaluator for the conditional sampling benchmark."""

  @flax.struct.dataclass
  class AggregatingMetrics(clu_metrics.Collection):
    global_mean_crps: evaluate.TensorAverage(axis=None).from_output("crps")
    local_mean_crps: evaluate.TensorAverage(axis=0).from_output("crps")
    global_mean_threshold_brier_score: evaluate.TensorAverage(
        axis=(0, 1, 2, 3)  # NOTE: specific to 2d case
    ).from_output("thres_brier_scores")
    local_mean_threshold_brier_score: evaluate.TensorAverage(
        axis=0
    ).from_output("thres_brier_scores")

  @property
  def scalar_metrics_to_log(self) -> dict[str, Array]:
    """Logs global crps and threshold brier scores."""
    scalar_metrics = {}
    agg_metrics = self.state.compute_aggregated_metrics()
    for model_key, metric_dict in agg_metrics.items():
      scalar_metrics[f"{model_key}/global_mean_crps"] = metric_dict[
          "global_mean_crps"
      ]
      for i, sc in enumerate(metric_dict["global_mean_threshold_brier_score"]):
        scalar_metrics[f"{model_key}/global_mean_thres_brier_score_{i}"] = sc
    return scalar_metrics
