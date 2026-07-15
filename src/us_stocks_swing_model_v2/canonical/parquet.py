from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Iterable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..errors import ContractError, IntegrityError


def deterministic_table(table: pa.Table, schema: pa.Schema, sort_keys: Iterable[str]) -> pa.Table:
    if set(table.column_names) != set(schema.names):
        raise ContractError("canonical table columns differ from the frozen schema")
    table = table.select(schema.names).cast(schema, safe=True)
    keys = [(name, "ascending") for name in sort_keys]
    indices = pc.sort_indices(table, sort_keys=keys)
    return pc.take(table, indices)


def write_deterministic_parquet(
    table: pa.Table,
    path: Path,
    *,
    schema: pa.Schema,
    sort_keys: Iterable[str],
) -> Path:
    canonical = deterministic_table(table, schema, sort_keys)
    target = Path(path)
    if target.exists():
        raise IntegrityError(f"canonical Parquet never overwrites: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        pq.write_table(
            canonical,
            temporary,
            version="2.6",
            data_page_version="1.0",
            compression="zstd",
            compression_level=9,
            use_dictionary=False,
            write_statistics=True,
            row_group_size=65536,
            store_schema=True,
        )
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
