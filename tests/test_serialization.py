"""Moneta fork: every tool reply drops fields that have no value and keeps every one that has."""

from __future__ import annotations

import json

from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent

from plane_mcp.moneta.serialization import compacted_result, drop_nulls


def test_nulls_are_dropped_at_every_depth():
    record = {"id": "1", "parent": None, "links": [{"url": "u", "title": None}], "stats": {"a": None, "b": 2}}
    assert drop_nulls(record) == {"id": "1", "links": [{"url": "u"}], "stats": {"b": 2}}


def test_values_that_are_falsy_but_not_null_are_kept():
    record = {"count": 0, "done": False, "note": "", "labels": [], "extra": {}, "none": None}
    assert drop_nulls(record) == {"count": 0, "done": False, "note": "", "labels": [], "extra": {}}


def test_null_list_elements_are_kept():
    assert drop_nulls({"ids": ["a", None, "b"]}) == {"ids": ["a", None, "b"]}


def test_the_input_is_not_mutated():
    record = {"a": None, "b": {"c": None}}
    drop_nulls(record)
    assert record == {"a": None, "b": {"c": None}}


def test_an_envelope_keeps_its_paging_keys_when_null():
    page = {"results": [{"id": "1", "x": None}], "next_cursor": None, "prev_cursor": None, "grouped_by": None}
    assert drop_nulls(page) == {"results": [{"id": "1"}], "next_cursor": None, "prev_cursor": None}


def test_a_record_with_a_results_key_but_no_paging_is_not_an_envelope():
    assert drop_nulls({"results": [1], "next_cursor": None}) == {"results": [1], "next_cursor": None}
    assert drop_nulls({"results": "x", "other": None}) == {"results": "x"}


def _result(value) -> ToolResult:
    return ToolResult(content=json.dumps(value), structured_content=value)


def test_both_the_text_and_the_structured_content_are_compacted():
    out = compacted_result(_result({"id": "1", "parent": None}))
    assert json.loads(out.content[0].text) == {"id": "1"}
    assert out.structured_content == {"id": "1"}


def test_text_is_re_encoded_without_padding():
    out = compacted_result(_result({"id": "1", "name": "é", "gone": None}))
    assert out.content[0].text == '{"id":"1","name":"é"}'


def test_a_wrapped_list_result_keeps_its_wrapper_key():
    wrapped = ToolResult(content='[{"id":"1","a":null}]', structured_content={"result": [{"id": "1", "a": None}]})
    out = compacted_result(wrapped)
    assert out.structured_content == {"result": [{"id": "1"}]}
    assert json.loads(out.content[0].text) == [{"id": "1"}]


def test_a_null_result_survives():
    out = compacted_result(ToolResult(content="null", structured_content={"result": None}))
    assert out.structured_content == {"result": None}
    assert out.content[0].text == "null"


def test_plain_text_and_non_text_blocks_are_untouched():
    image = ImageContent(type="image", data="AAAA", mimeType="image/png")
    result = ToolResult(
        content=[TextContent(type="text", text="Error: nope"), TextContent(type="text", text="12"), image]
    )
    assert compacted_result(result).content == result.content


def test_an_error_result_is_returned_as_is():
    result = ToolResult(content='{"a":null}', is_error=True)
    assert compacted_result(result) is result
