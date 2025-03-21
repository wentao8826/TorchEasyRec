# Copyright (c) 2024, Alibaba Group;
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#    http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy.typing as npt
import pyarrow as pa
import torch
from torchrec.sparse.jagged_tensor import JaggedTensor, KeyedJaggedTensor, KeyedTensor
from torchrec.streamable import Pipelineable

from tzrec.protos.data_pb2 import FieldType

BASE_DATA_GROUP = "__BASE__"
NEG_DATA_GROUP = "__NEG__"
CROSS_NEG_DATA_GROUP = "__CNEG__"

C_SAMPLE_MASK = "__SAMPLE_MASK__"
C_NEG_SAMPLE_MASK = "__NEG_SAMPLE_MASK__"

FIELD_TYPE_TO_PA = {
    FieldType.INT32: pa.int32(),
    FieldType.INT64: pa.int64(),
    FieldType.FLOAT: pa.float32(),
    FieldType.DOUBLE: pa.float64(),
    FieldType.STRING: pa.string(),
}


@dataclass
class ParsedData:
    """Internal parsed data structure."""

    name: str


@dataclass
class SparseData(ParsedData):
    """Internal data structure for sparse feature."""

    values: npt.NDArray
    lengths: npt.NDArray
    weights: Optional[npt.NDArray] = None


@dataclass
class DenseData(ParsedData):
    """Internal data structure for dense feature."""

    values: npt.NDArray


@dataclass
class SequenceSparseData(ParsedData):
    """Internal data structure for sequence sparse feature."""

    values: npt.NDArray
    key_lengths: npt.NDArray
    seq_lengths: npt.NDArray


@dataclass
class SequenceDenseData(ParsedData):
    """Internal data structure for sequence dense feature."""

    values: npt.NDArray
    seq_lengths: npt.NDArray


class RecordBatchTensor:
    """PyArrow RecordBatch use Tensor as buffer.

    For efficient transfer data between processes, e.g., mp.Queue.
    """

    def __init__(self, record_batch: Optional[pa.RecordBatch] = None) -> None:
        self._schema = None
        self._buff = None
        if record_batch:
            self._schema = record_batch.schema
            self._buff = torch.UntypedStorage.from_buffer(
                record_batch.serialize(), dtype=torch.uint8
            )

    def get(self) -> Optional[pa.RecordBatch]:
        """Get RecordBatch."""
        if self._buff is not None:
            # pyre-ignore[16]
            return pa.ipc.read_record_batch(
                pa.foreign_buffer(self._buff.data_ptr(), self._buff.size()),
                self._schema,
            )
        else:
            return None


@dataclass
class Batch(Pipelineable):
    """Input Batch."""

    # key of dense_features is data group name
    dense_features: Dict[str, KeyedTensor] = field(default_factory=dict)
    # key of sparse_features is data group name
    sparse_features: Dict[str, KeyedJaggedTensor] = field(default_factory=dict)
    # key of sequence_mulval_lengths is data group name
    #
    # for multi-value sequence, we flatten it, then store values & accumate lengths
    # into sparse_features, store key_lengths & seq_lengths into sequence_mulval_lengths
    #
    # e.g.
    # for the sequence `click_seq`: [[[3, 4], [5]], [6, [7, 8]]]
    # we can denote it in jagged formular with:
    #   values: [3, 4, 5, 6, 7, 8]
    #   key_lengths: [2, 1, 1, 2]
    #   seq_lengths: [2, 2]
    # then:
    #   sparse_features[dg]['click_seq'].values() = [3, 4, 5, 6, 7, 8]  # values
    #   sparse_features[dg]['click_seq'].lengths() = [3, 3]  # accumate lengths
    #   sequence_mulval_lengths[dg]['click_seq'].values() = [2, 1, 1, 2]  # key_lengths
    #   sequence_mulval_lengths[dg]['click_seq'].lengths() = [2, 2]  # seq_lengths
    sequence_mulval_lengths: Dict[str, KeyedJaggedTensor] = field(default_factory=dict)
    # key of sequence_dense_features is feature name
    sequence_dense_features: Dict[str, JaggedTensor] = field(default_factory=dict)
    # key of labels is label name
    labels: Dict[str, torch.Tensor] = field(default_factory=dict)
    # reserved inputs [for predict]
    reserves: RecordBatchTensor = field(default_factory=RecordBatchTensor)
    # size for user side input tile when do inference and INPUT_TILE=2 or 3
    tile_size: int = field(default=-1)
    # sample_weight
    sample_weights: Dict[str, torch.Tensor] = field(default_factory=dict)

    def to(self, device: torch.device, non_blocking: bool = False) -> "Batch":
        """Copy to specified device."""
        return Batch(
            dense_features={
                k: v.to(device=device, non_blocking=non_blocking)
                for k, v in self.dense_features.items()
            },
            sparse_features={
                k: v.to(device=device, non_blocking=non_blocking)
                for k, v in self.sparse_features.items()
            },
            sequence_mulval_lengths={
                k: v.to(device=device, non_blocking=non_blocking)
                for k, v in self.sequence_mulval_lengths.items()
            },
            sequence_dense_features={
                k: v.to(device=device, non_blocking=non_blocking)
                for k, v in self.sequence_dense_features.items()
            },
            labels={
                k: v.to(device=device, non_blocking=non_blocking)
                for k, v in self.labels.items()
            },
            reserves=self.reserves,
            tile_size=self.tile_size,
            sample_weights={
                k: v.to(device=device, non_blocking=non_blocking)
                for k, v in self.sample_weights.items()
            },
        )

    def record_stream(self, stream: torch.Stream) -> None:
        """Record which streams have used the tensor."""
        for v in self.dense_features.values():
            v.record_stream(stream)
        for v in self.sparse_features.values():
            v.record_stream(stream)
        for v in self.sequence_mulval_lengths.values():
            v.record_stream(stream)
        for v in self.sequence_dense_features.values():
            v.record_stream(stream)
        for v in self.labels.values():
            v.record_stream(stream)
        for v in self.sample_weights.values():
            v.record_stream(stream)

    def pin_memory(self) -> "Batch":
        """Copy to pinned memory."""
        # TODO(hongsheng.jhs): KeyedTensor do not have pin_memory()
        dense_features = {}
        for k, v in self.dense_features.items():
            dense_features[k] = KeyedTensor(
                keys=v.keys(),
                length_per_key=v.length_per_key(),
                values=v.values().pin_memory(),
                key_dim=v.key_dim(),
            )
        sequence_dense_features = {}
        for k, v in self.sequence_dense_features.items():
            weights = v._weights
            lengths = v._lengths
            offsets = v._offsets
            sequence_dense_features[k] = JaggedTensor(
                values=v.values().pin_memory(),
                weights=weights.pin_memory() if weights is not None else None,
                lengths=lengths.pin_memory() if lengths is not None else None,
                offsets=offsets.pin_memory() if offsets is not None else None,
            )
        return Batch(
            dense_features=dense_features,
            sparse_features={
                k: v.pin_memory() for k, v in self.sparse_features.items()
            },
            sequence_mulval_lengths={
                k: v.pin_memory() for k, v in self.sequence_mulval_lengths.items()
            },
            sequence_dense_features=sequence_dense_features,
            labels={k: v.pin_memory() for k, v in self.labels.items()},
            reserves=self.reserves,
            tile_size=self.tile_size,
            sample_weights={k: v.pin_memory() for k, v in self.sample_weights.items()},
        )

    def to_dict(
        self, sparse_dtype: Optional[torch.dtype] = None
    ) -> Dict[str, torch.Tensor]:
        """Convert to feature tensor dict."""
        tensor_dict = {}
        for x in self.dense_features.values():
            for k, v in x.to_dict().items():
                tensor_dict[f"{k}.values"] = v
        for x in self.sparse_features.values():
            if sparse_dtype:
                x = KeyedJaggedTensor(
                    keys=x.keys(),
                    values=x.values().to(sparse_dtype),
                    lengths=x.lengths().to(sparse_dtype),
                    weights=x.weights_or_none(),
                )
            for k, v in x.to_dict().items():
                tensor_dict[f"{k}.values"] = v.values()
                tensor_dict[f"{k}.lengths"] = v.lengths()
                if v.weights_or_none() is not None:
                    tensor_dict[f"{k}.weights"] = v.weights()
        for x in self.sequence_mulval_lengths.values():
            if sparse_dtype:
                x = KeyedJaggedTensor(
                    keys=x.keys(),
                    values=x.values().to(sparse_dtype),
                    lengths=x.lengths().to(sparse_dtype),
                )
            for k, v in x.to_dict().items():
                tensor_dict[f"{k}.key_lengths"] = v.values()
                tensor_dict[f"{k}.lengths"] = v.lengths()
        for k, v in self.sequence_dense_features.items():
            tensor_dict[f"{k}.values"] = v.values()
            tensor_dict[f"{k}.lengths"] = v.lengths()
        for k, v in self.labels.items():
            tensor_dict[f"{k}"] = v
        for k, v in self.sample_weights.items():
            tensor_dict[f"{k}"] = v
        if self.tile_size > 0:
            tensor_dict["batch_size"] = torch.tensor(self.tile_size, dtype=torch.int64)
        return tensor_dict


def calc_slice_position(
    row_count: int,
    slice_id: int,
    slice_count: int,
    batch_size: int,
    drop_redundant_bs_eq_one: bool,
    pre_total_remain: int = 0,
) -> Tuple[int, int, int]:
    """Calc table read position according to the slice information.

    Args:
        row_count (int): table total row count.
        slice_id (int): worker id.
        slice_count (int): total worker number.
        batch_size (int): batch_size.
        drop_redundant_bs_eq_one (bool): drop last redundant batch with batch_size
            equal one to prevent train_eval hung.
        pre_total_remain (int): remaining total count in pre-table is
            insufficient to meet the batch_size requirement for each worker.

    Return:
        start (int): start row position in table.
        end (int): start row position in table.
        total_remain (int): remaining total count in curr-table is
            insufficient to meet the batch_size requirement for each worker.
    """
    pre_remain_size = int(pre_total_remain / slice_count)
    pre_remain_split_point = pre_total_remain % slice_count

    size = int((row_count + pre_total_remain) / slice_count)
    split_point = (row_count + pre_total_remain) % slice_count
    if slice_id < split_point:
        start = slice_id * (size + 1)
        end = start + (size + 1)
    else:
        start = split_point * (size + 1) + (slice_id - split_point) * size
        end = start + size

    real_start = (
        start - pre_remain_size * slice_id - min(pre_remain_split_point, slice_id)
    )
    real_end = (
        end
        - pre_remain_size * (slice_id + 1)
        - min(pre_remain_split_point, slice_id + 1)
    )
    # when (end - start) % bz = 1 on some workers and
    # (end - start) % bz = 0 on other workers, train_eval will hang
    if (
        drop_redundant_bs_eq_one
        and split_point != 0
        and (end - start) % batch_size == 1
        and size % batch_size == 0
    ):
        real_end = real_end - 1
        split_point = 0
    return real_start, real_end, (size % batch_size) * slice_count + split_point


def remove_nullable(field_type: pa.DataType) -> pa.DataType:
    """Recursive removal of the null=False property from lists and nested lists."""
    if pa.types.is_list(field_type):
        # Get element fields
        value_field = field_type.value_field
        # Change the nullable to True
        normalized_value_field = value_field.with_nullable(True)
        # Recursive processing of element types
        normalized_value_type = remove_nullable(normalized_value_field.type)
        # Construct a new list type
        return pa.list_(normalized_value_type)

    else:
        return field_type
