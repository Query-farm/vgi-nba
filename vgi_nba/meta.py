"""Metadata helpers shared by the NBA catalog and its Arrow schemas."""

from __future__ import annotations

import json

import pyarrow as pa

_COMMENT_KEY = b"comment"


def field(name: str, kind: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field:
    """Build an Arrow field carrying its SQL column comment."""
    return pa.field(name, kind, nullable=nullable, metadata={_COMMENT_KEY: comment.encode()})


def comment_of(value: pa.Field) -> str:
    """Return the comment stored on an Arrow field."""
    if value.metadata and _COMMENT_KEY in value.metadata:
        return value.metadata[_COMMENT_KEY].decode()
    return ""


def column_comments(schema: pa.Schema) -> dict[str, str]:
    """Return the column-comment mapping expected by a catalog table."""
    return {value.name: comment_of(value) for value in schema if comment_of(value)}


def sql_type(kind: pa.DataType) -> str:
    """Render the DuckDB type name of a supported Arrow type."""
    if pa.types.is_decimal(kind):
        return f"DECIMAL({kind.precision},{kind.scale})"
    if pa.types.is_timestamp(kind):
        return "TIMESTAMP WITH TIME ZONE" if kind.tz else "TIMESTAMP"
    if pa.types.is_date(kind):
        return "DATE"
    if pa.types.is_boolean(kind):
        return "BOOLEAN"
    if pa.types.is_int32(kind):
        return "INTEGER"
    if pa.types.is_int64(kind):
        return "BIGINT"
    if pa.types.is_float64(kind):
        return "DOUBLE"
    if pa.types.is_string(kind):
        return "VARCHAR"
    raise ValueError(f"Unsupported NBA result type: {kind}")


def result_columns_schema(schema: pa.Schema) -> str:
    """Render a static Arrow result schema as VGI discovery metadata."""
    return json.dumps(
        [
            {
                "name": value.name,
                "type": sql_type(value.type),
                "description": comment_of(value).removesuffix("."),
            }
            for value in schema
        ],
        separators=(",", ":"),
    )
