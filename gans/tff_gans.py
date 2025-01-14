# Copyright 2019, Google LLC.
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
"""TFF orchestration logic for Federated GANs."""

import attr
import tensorflow as tf
import tensorflow_federated as tff
import tensorflow_privacy

from gans import gan_training_tf_fns


def tensor_spec_for_batch(dummy_batch):
  """Returns a TensorSpec for the given batch."""
  # TODO(b/131085687): Consider common util shared with model_utils.py.
  if hasattr(dummy_batch, '_asdict'):
    dummy_batch = dummy_batch._asdict()

  def _get_tensor_spec(tensor):
    # Convert input to tensors, possibly from nested lists that need to be
    # converted to a single top-level tensor.
    tensor = tf.convert_to_tensor(tensor)
    # Remove the batch dimension and leave it unspecified.
    spec = tf.TensorSpec(
        shape=[None] + tensor.shape.dims[1:], dtype=tensor.dtype)
    return spec

  return tf.nest.map_structure(_get_tensor_spec, dummy_batch)


# Set cmp=False to get a default hash function for tf.function.
@attr.s(eq=False, frozen=False)
class GanFnsAndTypes(object):
  """A container for functions and types need to build TFF GANs.

  This class holds the "context" needed in order to build a complete TFF
  Federated Computation for GAN training, including functions for building
  the models and optimizers, and the corresponding types.

  The members generally correspond to arguments of the same names
  passed to the functions of `gan_training_tf_fns.py`.

  """
  # Arguments to __init__
  # Functions that construct the generator and discriminator networks.
  generator_model_fn = attr.ib()
  discriminator_model_fn = attr.ib()
  # Dummy examples of input data formats of generator and discriminator.
  dummy_gen_input = attr.ib()
  dummy_real_data = attr.ib()

  # GAN training specifications: Functions that update generator or
  # discriminator during training (i.e., embody optimization of loss functions).
  # TODO(b/141112101): I suspect these are preventing this object from
  # being re-used, we probably need a builder that returns the
  # train_generator_fn so TFF can capture everything.
  train_generator_fn = attr.ib()
  train_discriminator_fn = attr.ib()

  # Additional optimizer used in federation: controls how global models are
  # updated after aggregating client computation model deltas.
  server_disc_update_optimizer_fn = attr.ib()
  # Instance of a class that implements the `DPQuery` interface
  # (https://github.com/tensorflow/privacy/blob/v.0.2.2/tensorflow_privacy/privacy/dp_query/dp_query.py#L54)
  # Defaults to `None`, meaning no differential privacy query is used, no
  # clipping or noising is performed, and standard stateless weighted
  # aggregation occurs. If specified, it MUST be an instance of a
  # `privacy.NormalizedQuery`.
  train_discriminator_dp_average_query = attr.ib(
      type=tensorflow_privacy.DPQuery, default=None)

  # TF Types for the above, all (structures of) `tf.TensorSpec`.
  gen_input_type = attr.ib(init=False)
  real_data_type = attr.ib(init=False)
  from_server_type = attr.ib(init=False)
  generator_weights_type = attr.ib(init=False)
  discriminator_weights_type = attr.ib(init=False)

  # Federated dataset types
  client_gen_input_type = attr.ib(init=False, type=tff.FederatedType)
  client_real_data_type = attr.ib(init=False, type=tff.FederatedType)
  server_gen_input_type = attr.ib(init=False, type=tff.FederatedType)

  # The aggregation process. If `train_discriminator_dp_average_query` is
  # specified, this will be used to perform the aggregation steps (clipping,
  # noising) necessary for differential privacy (DP). If
  # `train_discriminator_dp_average_query` (i.e., no DP), this will be a simple
  # stateless mean.
  aggregation_process = attr.ib(
      init=False, type=tff.templates.AggregationProcess, default=None)

  # Sample generator and discriminator.
  _generator = attr.ib(init=False)
  _discriminator = attr.ib(init=False)

  def __attrs_post_init__(self):
    self.gen_input_type = tensor_spec_for_batch(self.dummy_gen_input)
    self.real_data_type = tensor_spec_for_batch(self.dummy_real_data)

    # Model-weights based types
    self._generator = self.generator_model_fn()
    _ = self._generator(self.dummy_gen_input)
    if not isinstance(self._generator, tf.keras.models.Model):
      raise TypeError('Expected `tf.keras.models.Model`, found {}.'.format(
          type(self._generator)))
    self._discriminator = self.discriminator_model_fn()
    _ = self._discriminator(self.dummy_real_data)
    if not isinstance(self._discriminator, tf.keras.models.Model):
      raise TypeError('Expected `tf.keras.models.Model`, found {}.'.format(
          type(self._discriminator)))

    def vars_to_type(var_struct):
      # TODO(b/131681951): read_value() shouldn't be needed
      return tf.nest.map_structure(
          lambda v: tf.TensorSpec.from_tensor(v.read_value()), var_struct)

    self.discriminator_weights_type = vars_to_type(self._discriminator.weights)
    self.generator_weights_type = vars_to_type(self._generator.weights)

    self.from_server_type = gan_training_tf_fns.FromServer(
        generator_weights=self.generator_weights_type,
        discriminator_weights=self.discriminator_weights_type)

    self.client_gen_input_type = tff.type_at_clients(
        tff.SequenceType(self.gen_input_type))
    self.client_real_data_type = tff.type_at_clients(
        tff.SequenceType(self.real_data_type))
    self.server_gen_input_type = tff.type_at_server(
        tff.SequenceType(self.gen_input_type))

    if self.train_discriminator_dp_average_query is not None:
      self.aggregation_process = tff.aggregators.DifferentiallyPrivateFactory(
          query=self.train_discriminator_dp_average_query).create(
              value_type=tff.to_type(self.discriminator_weights_type))
    else:
      self.aggregation_process = tff.aggregators.MeanFactory().create(
          value_type=tff.to_type(self.discriminator_weights_type),
          weight_type=tff.to_type(tf.float32))


def build_server_initial_state_comp(gan: GanFnsAndTypes):
  """Returns a `tff.tf_computation` for the `server_initial_state`.

  This is a thin wrapper around `gan_training_tf_fns.server_initial_state`.

  Args:
     gan: A `GanFnsAndTypes` object.

  Returns:
    A `tff.tf_computation` that returns `ServerState@SERVER`.
  """

  @tff.tf_computation
  def server_initial_state():
    generator = gan.generator_model_fn()
    discriminator = gan.discriminator_model_fn()
    return gan_training_tf_fns.server_initial_state(generator, discriminator)

  return server_initial_state


def build_client_computation(gan: GanFnsAndTypes):
  """Returns a `tff.tf_computation` for the `client_computation`.

  This is a thin wrapper around `gan_training_tf_fns.client_computation`.

  Args:
    gan: A `GanFnsAndTypes` object.

  Returns:
    A `tff.tf_computation.`
  """

  @tff.tf_computation(
      tff.SequenceType(gan.gen_input_type),
      tff.SequenceType(gan.real_data_type), gan.from_server_type)
  def client_computation(gen_inputs, real_data, from_server):
    """Returns the client_output."""
    return gan_training_tf_fns.client_computation(
        gen_inputs_ds=gen_inputs,
        real_data_ds=real_data,
        from_server=from_server,
        generator=gan.generator_model_fn(),
        discriminator=gan.discriminator_model_fn(),
        train_discriminator_fn=gan.train_discriminator_fn)

  return client_computation


def build_server_computation(gan: GanFnsAndTypes, server_state_type: tff.Type,
                             client_output_type: tff.Type,
                             aggregation_state_type: tff.Type):
  """Returns a `tff.tf_computation` for the `server_computation`.

  This is a thin wrapper around `gan_training_tf_fns.server_computation`.

  Args:
    gan: A `GanFnsAndTypes` object.
    server_state_type: The `tff.Type` of the ServerState.
    client_output_type: The `tff.Type` of the ClientOutput.
    aggregation_state_type: The `tff.Type` of the state of the
      tff.templates.AggregationProcess.

  Returns:
    A `tff.tf_computation.`
  """

  @tff.tf_computation(server_state_type, tff.SequenceType(gan.gen_input_type),
                      client_output_type, aggregation_state_type)
  def server_computation(server_state, gen_inputs, client_output,
                         new_aggregation_state):
    """The wrapped server_computation."""
    return gan_training_tf_fns.server_computation(
        server_state=server_state,
        gen_inputs_ds=gen_inputs,
        client_output=client_output,
        generator=gan.generator_model_fn(),
        discriminator=gan.discriminator_model_fn(),
        server_disc_update_optimizer=gan.server_disc_update_optimizer_fn(),
        train_generator_fn=gan.train_generator_fn,
        new_aggregation_state=new_aggregation_state)

  return server_computation


def build_gan_training_process(gan: GanFnsAndTypes):
  """Constructs a `tff.Computation` for GAN training.

  Args:
    gan: A `GanFnsAndTypes` object.

  Returns:
    A `tff.templates.IterativeProcess` for GAN training.
  """

  # Generally, it is easiest to get the types correct by building
  # all of the needed tf_computations first, since this ensures we only
  # have non-federated types.
  client_computation = build_client_computation(gan)
  client_output_type = client_computation.type_signature.result

  @tff.federated_computation
  def fed_server_initial_state():
    state = tff.federated_eval(build_server_initial_state_comp(gan), tff.SERVER)
    server_initial_state = tff.federated_zip(
        gan_training_tf_fns.ServerState(
            state.generator_weights,
            state.discriminator_weights,
            state.counters,
            aggregation_state=gan.aggregation_process.initialize()))
    return server_initial_state

  @tff.federated_computation(fed_server_initial_state.type_signature.result,
                             gan.server_gen_input_type,
                             gan.client_gen_input_type,
                             gan.client_real_data_type)
  def run_one_round(server_state, server_gen_inputs, client_gen_inputs,
                    client_real_data):
    """The `tff.Computation` to be returned."""
    from_server = gan_training_tf_fns.FromServer(
        generator_weights=server_state.generator_weights,
        discriminator_weights=server_state.discriminator_weights)
    client_input = tff.federated_broadcast(from_server)
    client_outputs = tff.federated_map(
        client_computation, (client_gen_inputs, client_real_data, client_input))

    # Note that weight goes unused here if the aggregation is involving
    # Differential Privacy; the underlying AggregationProcess doesn't take the
    # parameter, as it just uniformly weights the clients.
    if gan.aggregation_process.is_weighted:
      aggregation_output = gan.aggregation_process.next(
          server_state.aggregation_state,
          client_outputs.discriminator_weights_delta,
          client_outputs.update_weight)
    else:
      aggregation_output = gan.aggregation_process.next(
          server_state.aggregation_state,
          client_outputs.discriminator_weights_delta)

    new_aggregation_state = aggregation_output.state
    averaged_discriminator_weights_delta = aggregation_output.result

    # TODO(b/131085687): Perhaps reconsider the choice to also use
    # ClientOutput to hold the aggregated client output.
    aggregated_client_output = gan_training_tf_fns.ClientOutput(
        discriminator_weights_delta=averaged_discriminator_weights_delta,
        # We don't actually need the aggregated update_weight, but
        # this keeps the types of the non-aggregated and aggregated
        # client_output the same, which is convenient. And I can
        # imagine wanting this.
        update_weight=tff.federated_sum(client_outputs.update_weight),
        counters=tff.federated_sum(client_outputs.counters))

    server_computation = build_server_computation(
        gan, server_state.type_signature.member, client_output_type,
        gan.aggregation_process.state_type.member)
    server_state = tff.federated_map(
        server_computation, (server_state, server_gen_inputs,
                             aggregated_client_output, new_aggregation_state))
    return server_state

  return tff.templates.IterativeProcess(fed_server_initial_state, run_one_round)
