# Copyright 2021 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Builders for video datasets."""

import abc
import collections
import copy
import enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import tensorflow as tf


# Common types.
FeaturesDict = Dict[str, tf.Tensor]
Parser = Callable[[tf.Tensor], FeaturesDict]
Processor = Callable[[FeaturesDict], FeaturesDict]
FeatureProcessor = Callable[[tf.Tensor], tf.Tensor]
ProcessorState = Dict[str, Any]
StatefulProcessor = Callable[[FeaturesDict, ProcessorState], FeaturesDict]
StatefulFeatureProcessor = Callable[[tf.Tensor, ProcessorState], tf.Tensor]
FilterFn = Callable[[FeaturesDict], tf.Tensor]  # Boolean tensor of shape ().

# Default name of features for each modality in `FeaturesDict`.
# User should reference those via variable and not string directly.
AUDIO_FEATURE_NAME = 'audio'
FLOW_FEATURE_NAME = 'flow'
IMAGE_FEATURE_NAME = 'image'
KEY_FEATURE_NAME = 'key'
LABEL_INDEX_FEATURE_NAME = 'label'
LABEL_NAME_FEATURE_NAME = 'label_name'
TEXT_INDICES_FEATURE_NAME = 'text_indices'
TEXT_FEATURE_NAME = 'text'


class Phase(enum.Enum):
  """Phases of the data processing graph."""
  READ = enum.auto()
  PARSE = enum.auto()
  SAMPLE = enum.auto()
  DECODE = enum.auto()
  PREPROCESS = enum.auto()
  POSTPROCESS = enum.auto()


class RawFormat(enum.Enum):
  """Supported formats of raw data."""
  TF_EXAMPLE = enum.auto()
  TF_SEQUENCE_EXAMPLE = enum.auto()


class BaseParserBuilder(abc.ABC):
  """Builder for the parser function.

  The parse function is supposed to process a `tf.Tensor` with the bytes of a
  raw data representation into a features dictionary. The dictionary should keep
  features in their rawest format, as the decode function will be responsible
  for parsing those to usable formats, hence avoiding to decode more than
  necessary.

  Usage:

  ```python
  parser_builder = ChildClassParserBuilder()
  parse_fn = (parser_builder
              .parse_feature('image/encoded',
                             tf.io.FixedLenSequenceFeature((), dtype=tf.string),
                             IMAGE_FEATURE_NAME)
              .parse_feature('WAVEFORM/feature/floats',
                             tf.io.VarLenFeature(dtype=tf.float32),
                             AUDIO_FEATURE_NAME)
              .parse_feature('my_text_feature',
                             tf.io.VarLenFeature(dtype=tf.string),
                             TEXT_FEATURE_NAME,
                             child_class_arg=42)  # Argument for child class.
              .parse_feature('my_own_modality_feature',
                             tf.io.FixedLenFeature(dtype=tf.int32),
                             'my_chosen_name')
              .build())

  raw_data = tf.Tensor(raw_data_bytes, dtype=tf.string)
  features_dict = parse_fn(raw_data)

  # features_dict: {
  #     'image': tf.Tensor(bytes_representation_of_image),
  #     'audio': tf.SparseTensor(audio_floats),
  #     'text': tf.SparseTensor(text_as_string),
  #     'my_chosen_name': tf.Tensor(int_value)
  # }
  ```

  Names in the generated features dictionary (`output_name`) should be the same
  for a given modality (even if input names are different), as long as they have
  the same meaning, so following processing code can be reused easily by using
  the same `feature_name` in processors (e.g. `builders.IMAGE` can be used as
  `output_name` for frames features independently of how they are stored in the
  input serialized example).
  """

  @abc.abstractmethod
  def parse_feature(self,
                    feature_name: str,
                    feature_type: Union[tf.io.VarLenFeature,
                                        tf.io.FixedLenFeature,
                                        tf.io.FixedLenSequenceFeature],
                    output_name: Optional[str] = None,
                    **kwargs) -> 'BaseParserBuilder':
    """Parses the given feature when parsing the raw data.

    Args:
      feature_name: Name of the feature to be parsed (in the raw data).
      feature_type: Type of `tf.Tensor` to be generated from parsing the given
        feature. The type will depend on the structure of the raw data. E.g.
        a sequence of frames (list with one JPEG as bytes string) should be
        `tf.io.FixedLenSequenceFeature`, while a single feature of variable
        length (audio) should be `tf.io.VarLenFeature`.
      output_name: Name of the feature in the resulting dictionary. This should
        be a meaningful name and preferably the same across different datasets,
        so later processing functions can be reused easily. Name should be
        unique over all features. If no `output_name` is provided,
        `feature_name` is used.
      **kwargs: Named arguments extended by the child class.

    Returns:
      This instance of the `BaseParserBuilder`.
    """

  @abc.abstractmethod
  def _parse_fn(self, raw_data: tf.Tensor) -> FeaturesDict:
    """Converts bytes of raw data to a features dictionary.

    Args:
      raw_data: `tf.Tensor` of bytes (string).

    Returns:
      The features dictionary obtained from parsing the raw data.
    """

  def build(self) -> Parser:
    """Builds parse function."""
    return self._parse_fn


class SequenceExampleParserBuilder(BaseParserBuilder):
  """Builder for the parser function from raw `tf.train.SequenceExample`."""

  def __init__(self):
    super().__init__()
    self._features: Dict[Tuple[str, bool],
                         Union[tf.io.VarLenFeature, tf.io.FixedLenFeature,
                               tf.io.FixedLenSequenceFeature]] = {}
    self._name_dict: Dict[Tuple[str, bool], List[str]] = {}

  def parse_feature(self,
                    feature_name: str,
                    feature_type: Union[tf.io.VarLenFeature,
                                        tf.io.FixedLenFeature,
                                        tf.io.FixedLenSequenceFeature],
                    output_name: Optional[str] = None,
                    is_context: bool = False) -> 'SequenceExampleParserBuilder':
    """Parses the given feature when parsing the raw `tf.train.SequenceExample`.

    The same input feature can be added more than once with different
    `output_name` but always with the same `feature_type`. This is useful when
    multiple views (with different processings down the line) of the same data
    is needed.

    Args:
      feature_name: See base class.
      feature_type: See base class.
      output_name: See base class.
      is_context: True if feature is in the `context` of the
        `tf.train.SequenceExample` and false if it is in the `feature_lists`.
        Note that it depends on the structure of the parsed
        `tf.train.SequenceExample`.

    Returns:
      This instance of `SequenceExampleParserBuilder`.

    Raises:
      ValueError: `output_name` is not unique.
      ValueError: Different `feature_type` for the same input feature.
    """

    # Validate name.
    output_name = output_name or feature_name
    for name_list in self._name_dict.values():
      if output_name in name_list:
        raise ValueError(f'Given `output_name` {output_name} is not unique.')

    feature_key = (feature_name, is_context)
    if feature_key not in self._features:
      self._features[feature_key] = feature_type
    elif self._features[feature_key] != feature_type:
      raise ValueError('Different `feature_type` given for the same feature '
                       f'{feature_name} with `is_context` {is_context}.')

    if (feature_name, is_context) not in self._name_dict:
      self._name_dict[(feature_name, is_context)] = []
    self._name_dict[(feature_name, is_context)].append(output_name)

    return self

  def _parse_fn(self, raw_data: tf.Tensor) -> FeaturesDict:
    """Converts bytes of `tf.train.SequenceExample` to a features dictionary."""
    context_features = {n: t for (n, c), t in self._features.items() if c}
    sequence_features = {n: t for (n, c), t in self._features.items() if not c}

    parsed_context, parsed_sequence = tf.io.parse_single_sequence_example(
        raw_data, context_features, sequence_features)

    # Rename features dict.
    output = {}
    for context, parsed in [(True, parsed_context), (False, parsed_sequence)]:
      for k, f in parsed.items():
        output_names = self._name_dict[(k, context)]
        for output_name in output_names:
          output[output_name]: tf.Tensor = tf.identity(f)

    return output


class ExampleParserBuilder(BaseParserBuilder):
  """Builder for the parser function from raw `tf.train.Example`."""

  def __init__(self):
    super().__init__()
    self._features = {}
    self._name_dict: Dict[str, List[str]] = {}

  def parse_feature(
      self,
      feature_name: str,
      feature_type: Union[tf.io.VarLenFeature, tf.io.FixedLenFeature],
      output_name: Optional[str] = None) -> 'ExampleParserBuilder':
    """Parses the given feature when parsing the raw `tf.train.Example`.

    The same input feature can be added more than once with different
    `output_name` but always with the same `feature_type`. This is useful when
    multiple views (with different processings down the line) of the same data
    is needed.

    Args:
      feature_name: See base class.
      feature_type: See base class.
      output_name: See base class.

    Returns:
      This instance of `ExampleParserBuilder`.

    Raises:
      ValueError: `output_name` is not unique.
      ValueError: Different `feature_type` for the same input feature.
    """

    # Validate name.
    output_name = output_name or feature_name
    for name_list in self._name_dict.values():
      if output_name in name_list:
        raise ValueError(f'Given output_name {output_name} is not unique.')

    if feature_name not in self._features:
      self._features[feature_name] = feature_type
    elif self._features[feature_name] != feature_type:
      raise ValueError('Different `feature_type` given for the same feature '
                       f'{feature_name}.')

    if feature_name not in self._name_dict:
      self._name_dict[feature_name] = []
    self._name_dict[feature_name].append(output_name)

    return self

  def _parse_fn(self, raw_data: tf.Tensor) -> FeaturesDict:
    """Converts bytes of raw Example to a features dictionary."""
    parsed = tf.io.parse_single_example(
        serialized=raw_data, features=self._features)

    # Rename features dict.
    output = {}
    for k, f in parsed.items():
      output_names = self._name_dict[k]
      for output_name in output_names:
        output[output_name]: tf.Tensor = tf.identity(f)

    return output


RAW_FORMAT_TO_PARSER = {
    RawFormat.TF_EXAMPLE: ExampleParserBuilder,
    RawFormat.TF_SEQUENCE_EXAMPLE: SequenceExampleParserBuilder,
}


_FunctionDescription = collections.namedtuple(
    '_FunctionDescription', ('fn_name', 'fn', 'feature_name', 'stateful'))


class _Builder(abc.ABC):
  """Base class for processor builders.

  This builder can be used to build a process function that takes as input a
  features dictionary and outputs another features dictionary. Each function
  added to the builder can transform either a single feature (`tf.Tensor`) when
  a `feature_name` is provided, outputting its transformed version, or transform
  the entire `FeaturesDict` when no `feature_name` is provided (this can be used
  when the function needs access to more than one feature). The generated
  processor is a function which executes each one of the added functions in
  order.

  Basic usage:

  ```python
  def crop_image(image: tf.Tensor) -> tf.Tensor:
    ...
    return cropped_image

  def text_to_indices(features_dict: FeaturesDict) -> FeaturesDict:
    text = features_dict[TEXT_FEATURE_NAME]
    indices = tokenize_text(text)
    del features_dict[TEXT_FEATURE_NAME]
    features_dict[TEXT_INDICES_FEATURE_NAME] = indices
    return features_dict

  builder = _Builder()
  process_fn = (builder
                .add_fn(crop_image, feature_name=IMAGE_FEATURE_NAME)
                .add_fn(text_to_indices)
                .build())

  # input_features_dict = {
  #     'image': tf.Tensor(rgb_representation),
  #     'text': tf.Tensor(sentences)
  # }
  output_features_dict = process_fn(input_features_dict)

  # output_features_dict: {
  #     'image': tf.Tensor(cropped_rgb_representation)
  #     'text_indices': tf.Tensor(indices)
  # }
  ```

  This builder also supports more flexible control by allowing deleting and
  replacing added functions and inserting new ones. This allows more granular
  operations and better control over the data processing graph.

  Usage:

  ```python
  def standard_crop_image(image: tf.Tensor) -> tf.Tensor:
    ...
    return cropped_image

  def special_crop_image(image: tf.Tensor) -> tf.Tensor:
    ...
    return specially_cropped_image

  builder = _Builder().add_fn(standard_crop_image, IMAGE_FEATURE_NAME, 'crop')
  # Add other things to builder.

  builder.replace_fn('crop', special_crop_image)
  ```

  In order to easily add different modalities, this builder allows a shared
  state among all added functions. The state is a mutable dictionary passed to
  the stateful functions and might be modified in order to keep metadata. A
  basic use case is sampling video and audio consistently.

  Usage:

  ```python
  def sample_image(frames: tf.Tensor, state: Dict[str, Any]) -> tf.Tensor:
    ...
    state['start_sample_time'] = start_time
    state['end_sample_time'] = end_time
    return sampled_frames

  def sample_audio(audio: tf.Tensor, state: Dict[str, Any]) -> tf.Tensor:
    start_time = state['start_sample_time']
    end_time = state['end_sample_time']
    ...
    return sampled_audio_according_to_start_and_end

  builder = _Builder().add_fn(sample_image, IMAGE_FEATURE_NAME, stateful=True)
                      .add_fn(sample_audio, AUDIO_FEATURE_NAME, stateful=True)
  ```
  """

  def __init__(self):
    self._fns_list = []
    self._fn_idx = 0

  def add_fn(self,
             fn: Union[Processor, FeatureProcessor, StatefulProcessor,
                       StatefulFeatureProcessor],
             feature_name: Optional[str] = None,
             fn_name: Optional[str] = None,
             stateful: bool = False,
             add_before_fn_name: Optional[str] = None) -> '_Builder':
    """Adds the given function to the processor.

    Args:
      fn: Function to be added to the processor.
      feature_name: Name of the feature input and output of the function. If no
        name is provided, the entire features dictionary will be given as input
        to the function.
      fn_name: Name for the function being added. This allows users to replace
        specific functions if needed instead of rebuilding the entire processor
        graph. If no name is given a unique identifier will be used.
      stateful: Whether the function has access to the state of the builder. If
        `True`, the function should receive the state as second parameter.
      add_before_fn_name: Name of the function before which the given function
        should be added. If None, given function will be appended to the list.

    Returns:
      This instance of the builder.

    Raises:
      ValueError: `fn_name` is not unique.
      ValueError: Value of `add_before_fn_name` does not exist.
    """
    if fn_name is None:
      fn_name = f'fn_{self._fn_idx}'
      self._fn_idx += 1

    if fn_name in [fd.fn_name for fd in self._fns_list]:
      raise ValueError(f'Given `fn_name` {fn_name} is not unique.')

    new_fd = _FunctionDescription(fn_name, fn, feature_name, stateful)

    if add_before_fn_name:
      add_before_idx = [
          i for i, fd in enumerate(self._fns_list)
          if fd.fn_name == add_before_fn_name
      ]
      if not add_before_idx:
        raise ValueError(
            f'Given `add_before_idx` {add_before_idx} does not exist.')

      add_before_idx = add_before_idx[0]
      self._fns_list.insert(add_before_idx, new_fd)
    else:
      self._fns_list.append(new_fd)

    return self

  def reset(self) -> '_Builder':
    """Resets the list of functions in the builder."""
    self._fns_list = []
    return self

  def remove_fn(self, fn_name: str) -> '_Builder':
    """Removes the given function from the builder.

    Args:
      fn_name: Name of the function to be deleted.

    Returns:
      This instance of the builder.
    """
    self._fns_list = [fd for fd in self._fns_list if fd.fn_name != fn_name]
    return self

  def replace_fn(
      self, fn_name: str, fn: Union[Processor, FeatureProcessor,
                                    StatefulProcessor, StatefulFeatureProcessor]
  ) -> '_Builder':
    """Replaces the function with the given name by the given function.

    Args:
      fn_name: Name of the function to be replaced.
      fn: Function to be used as replacement.

    Returns:
      This instance of the builder.

    Raises:
      ValueError: `fn_name` name does not exist.
    """
    idx = [i for i, fd in enumerate(self._fns_list) if fd.fn_name == fn_name]
    if not idx:
      raise ValueError(f'Given `fn_name` {fn_name} does not exist.')

    idx = idx[0]
    fd = self._fns_list[idx]
    new_fd = _FunctionDescription(fd.fn_name, fn, fd.feature_name, fd.stateful)
    self._fns_list[idx] = new_fd
    return self

  def get_summary(self):
    """Returns a summary of the current functions in the builder."""
    return copy.copy(self._fns_list)

  def build(self) -> Processor:
    """Builds process function."""
    fns_list = tuple(self._fns_list)

    def process_fn(features_dict: FeaturesDict) -> FeaturesDict:
      """Adds function one at a time."""
      output = copy.copy(features_dict)
      state: Dict[str, Any] = {}
      for fd in fns_list:
        if fd.feature_name:
          if fd.stateful:
            output[fd.feature_name] = fd.fn(output[fd.feature_name], state)
          else:
            output[fd.feature_name] = fd.fn(output[fd.feature_name])
        else:
          if fd.stateful:
            output = fd.fn(output, state)
          else:
            output = fd.fn(output)

      return output

    return process_fn


class SamplerBuilder(_Builder):
  """Builder for the sample function.

  The sample function is supposed to sample only the useful bits of the given
  features dictionary in order to avoid later useless decoding. E.g. sample only
  the necessary frames from the video. Function is run on unbatched examples.

  For usage see parent class docstring.
  """


class DecoderBuilder(_Builder):
  """Builder for the decode function.

  The decode function is supposed to transform raw features into usable formats.
  E.g. decode JPEG string tensors to rgb. This function should not implement
  operations as crop, resize, etc. and instead should do more basic operations
  (that are common over independent datasets or usages of the same dataset).
  Function is run on unbatched examples.

  For usage see parent class docstring.
  """


class PreprocessorBuilder(_Builder):
  """Builder for the preprocess function.

  The preprocess function is supposed to transform features in order to put them
  in the desired format. E.g. crop, pad, resize, etc. Function is run on
  unbatched examples.

  For usage see parent class docstring.
  """


class PostprocessorBuilder(_Builder):
  """Builder for postprocess function.

  Same as `PreprocessorBuilder` but runs on batched examples. E.g. transpose.

  For usage see parent class docstring.
  """


class FilterBuilder:
  """Agglomerator of filter functions for each data process phase.

  Usage:

  ```python
  def filter_on_key(features_dict: FeaturesDict) -> tf.Tensor:
    return tf.not_equal(
        tf.strings.substr(features_dict[KEY_FEATURE_NAME], 0, 7), 'invalid')

  def filter_on_channels(features_dict: FeaturesDict) -> tf.Tensor:
    return tf.equal(tf.shape(features_dict[IMAGE_FEATURE_NAME])[3], 3)

  filter_builder = (FilterBuilder()
                    .add_filter_fn(filter_on_key, Phase.READ)
                    .add_filter_fn(filter_on_channels, Phase.DECODE))

  filter_fn_post_read = filter_builder.build(Phase.PARSE)
  filter_fn_post_decode = filter_builder.build(Phase.DECODE)

  # input_ds = [{
  #     'image': tf.Tensor(rgb_representation_with_channel_3),
  #     'key': tf.Tensor('invalid_key_0')
  # },
  # {
  #     'image': tf.Tensor(rgb_representation_with_channel_3),
  #     'key': tf.Tensor('valid_key_1')
  # },
  # {
  #     'image': tf.Tensor(rgb_representation_with_channel_1),
  #     'key': tf.Tensor('valid_key_2')
  # }]

  # Read.
  ds = input_ds.filter(filter_fn_post_parse)
  # Decode.
  ds = ds.filter(filter_fn_post_decode)

  # ds: [{
  #     'image': tf.Tensor(rgb_representation_with_channel_3),
  #     'key': tf.Tensor('valid_key_1')
  # }]
  ```
  """

  def __init__(self):
    self._filter_fns: Dict[Phase, List[FilterFn]] = {}
    for phase in Phase:
      self._filter_fns[phase] = []

  def add_filter_fn(self, filter_fn: FilterFn,
                    after_phase: Phase) -> 'FilterBuilder':
    """Adds the given function to the filter.

    Args:
      filter_fn: Function to be added to the filter. It must receive as
        parameter a features dictionary and output a boolean `tf.Tensor` of
        shape () indicating if the example should be kept.
      after_phase: Phase after which the filter should be applied. In order to
        avoid useless processing, the earliest possible phase should be used.

    Returns:
      This instance of the `FilterBuilder`.
    """
    self._filter_fns[after_phase].append(filter_fn)
    return self

  def build(self, after_phase: Phase) -> FilterFn:
    """Builds the filter function for the given phase."""
    filter_fns = copy.copy(self._filter_fns[after_phase])

    def filter_fn(features_dict: FeaturesDict) -> tf.Tensor:
      keep = tf.constant(True)
      for fn in filter_fns:
        keep = tf.logical_and(keep, fn(features_dict))

      return keep

    return filter_fn
