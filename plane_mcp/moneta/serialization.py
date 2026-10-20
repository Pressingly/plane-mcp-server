"""One serialiser for every tool reply: no null fields (Moneta fork).

Plane's models carry every column of a record, and an unset one arrives as ``null``.
A reply is often mostly those, so each tool answer passes through :func:`compacted_result`
on its way out. It is the ``exclude_none`` of ``model_dump(exclude_none=True)`` applied
at the one place every tool shares, which keeps ``plane_mcp/tools`` byte-for-byte upstream.

Only a ``null`` that is a dict value is dropped. A list element is kept, and so are the
paging keys of an envelope: a missing ``next_cursor`` would read as "unknown" where
``null`` reads as "last page".
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.tools.tool import ToolResult
from mcp.types import ContentBlock, TextContent

from plane_mcp.toolkit.paging import ENVELOPE_FIELDS

WRAPPER_KEY = "result"


def _is_envelope(value: dict[str, Any]) -> bool:
    return isinstance(value.get("results"), list) and any(name in value for name in ENVELOPE_FIELDS)


def drop_nulls(value: Any) -> Any:
    """A copy of ``value`` without the dict entries whose value is ``None``."""
    if isinstance(value, list):
        return [drop_nulls(item) for item in value]
    if not isinstance(value, dict):
        return value
    kept = ENVELOPE_FIELDS if _is_envelope(value) else ()
    return {key: drop_nulls(item) for key, item in value.items() if item is not None or key in kept}


def _compacted_structured(structured: dict[str, Any] | None) -> dict[str, Any] | None:
    if structured is None:
        return None
    if set(structured) == {WRAPPER_KEY}:
        return {WRAPPER_KEY: drop_nulls(structured[WRAPPER_KEY])}
    return drop_nulls(structured)


def _decoded_container(text: str) -> dict[str, Any] | list[Any] | None:
    try:
        decoded = json.loads(text)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict | list) else None


def _compacted_block(block: ContentBlock) -> ContentBlock:
    if not isinstance(block, TextContent):
        return block
    decoded = _decoded_container(block.text)
    if decoded is None:
        return block
    text = json.dumps(drop_nulls(decoded), ensure_ascii=False, separators=(",", ":"))
    return block.model_copy(update={"text": text})


def compacted_result(result: ToolResult) -> ToolResult:
    """The result with ``null`` dropped from its text blocks and its structured content."""
    if result.is_error:
        return result
    return result.model_copy(
        update={
            "content": [_compacted_block(block) for block in result.content],
            "structured_content": _compacted_structured(result.structured_content),
        }
    )
