from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

import pytest

from semantic_code_review import structural
from semantic_code_review.augment.schemas import AnnotatedDiff, PRInfo, lift_file
from semantic_code_review.format.parse import parse_raw_diff
from semantic_code_review.viewer.build_json import build_viewer_json


def context_file(tmp_path: Path, base: str, head: str, filename: str = "sample.py") -> dict[str, Any]:
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    for directory, source in [(base_dir, base), (head_dir, head)]:
        directory.mkdir(exist_ok=True)
        (directory / filename).write_text(source)
    raw = f"diff --git a/{filename} b/{filename}\n" + "".join(
        difflib.unified_diff(base.splitlines(True), head.splitlines(True), f"a/{filename}", f"b/{filename}", n=1)
    )
    parsed = parse_raw_diff(raw)
    diff = AnnotatedDiff(
        version=parsed.version,
        pr=PRInfo(pr_url="", base_sha="base", head_sha="head", model=""),
        files=[lift_file(file) for file in parsed.files],
    )
    return build_viewer_json(diff, {}, head_dir=head_dir, base_dir=base_dir)["files"][0]


def test_multiple_hunks_in_one_function_share_symbol_ranges(tmp_path: Path) -> None:
    base = "def process():\n" + "".join(f"    step_{number}()\n" for number in range(1, 25))
    head = base.replace("step_4()", "retry()").replace("step_20()", "record()")
    file = context_file(tmp_path, base, head)
    assert len(file["hunks"]) == 2
    expected = [{"start": 1, "end": 25, "name": "process"}]
    for hunk in file["hunks"]:
        assert file["symbol_context"][hunk["id"]] == {"old": expected, "new": expected}


def test_nested_function_expands_smallest_definition(tmp_path: Path) -> None:
    base = "class Container:\n    def first(self):\n        return 1\n\n    def second(self):\n        return 2\n"
    file = context_file(tmp_path, base, base.replace("return 1", "return 3"))
    expected = [{"start": 2, "end": 3, "name": "Container.first"}]
    assert file["symbol_context"]["H0_0"] == {"old": expected, "new": expected}


@pytest.mark.parametrize(
    ("filename", "base", "head", "start", "end", "name"),
    [
        (
            "sample.py",
            "@original\ndef process():\n    return 1\n",
            "@updated\ndef process():\n    return 1\n",
            1,
            3,
            "process",
        ),
        (
            "sample.rs",
            "/// Original\n#[inline]\nfn process() {\n    first();\n}\n",
            "/// Updated\n#[inline]\nfn process() {\n    first();\n}\n",
            1,
            5,
            "process",
        ),
    ],
)
def test_definition_attachments_are_required_context(
    tmp_path: Path,
    filename: str,
    base: str,
    head: str,
    start: int,
    end: int,
    name: str,
) -> None:
    file = context_file(tmp_path, base, head, filename)
    expected = [{"start": start, "end": end, "name": name}]
    assert file["symbol_context"]["H0_0"] == {"old": expected, "new": expected}


def test_deleted_lines_retain_both_symbol_images_with_shifted_lines(tmp_path: Path) -> None:
    base = "def process():\n    first()\n    removed()\n    last()\n\ndef next():\n    pass\n"
    file = context_file(tmp_path, base, base.replace("    removed()\n", ""))
    assert file["symbol_context"]["H0_0"] == {
        "old": [{"start": 1, "end": 4, "name": "process"}],
        "new": [{"start": 1, "end": 3, "name": "process"}],
    }


def test_duplicate_rust_method_names_keep_actual_changed_method(tmp_path: Path) -> None:
    base = (
        "impl First {\n    fn new() {\n        first();\n    }\n}\n"
        "impl Second {\n    fn new() {\n        second();\n    }\n}\n"
    )
    file = context_file(tmp_path, base, base.replace("first();", "changed();"), "sample.rs")
    expected = [{"start": 2, "end": 4, "name": "new"}]
    assert file["symbol_context"]["H0_0"] == {"old": expected, "new": expected}


def test_unsupported_language_has_no_symbol_context(tmp_path: Path) -> None:
    file = context_file(
        tmp_path, "fn process() {\n    first();\n}\n", "fn process() {\n    changed();\n}\n", "sample.cpp"
    )
    assert file["symbol_context"] == {}


def test_deleting_duplicate_rust_method_does_not_pull_in_surviving_method(tmp_path: Path) -> None:
    base = (
        "impl First {\n    fn new() {\n        first();\n    }\n}\n"
        "impl Second {\n    fn new() {\n        second();\n    }\n}\n"
    )
    head = base.replace("    fn new() {\n        second();\n    }\n", "")
    file = context_file(tmp_path, base, head, "sample.rs")
    assert file["symbol_context"]["H0_0"] == {
        "old": [{"start": 7, "end": 9, "name": "new"}],
        "new": [],
    }


@pytest.mark.parametrize(
    ("filename", "base", "head", "signature_line"),
    [
        ("sample.py", "@original\ndef process():\n    return 1\n", "@original\ndef process():\n    return 2\n", 2),
        (
            "sample.rs",
            "/// Documentation\n#[inline]\nfn process() {\n    first();\n}\n",
            "/// Documentation\n#[inline]\nfn process() {\n    changed();\n}\n",
            3,
        ),
    ],
)
def test_context_attachments_do_not_move_definition_fold_opener(
    tmp_path: Path,
    filename: str,
    base: str,
    head: str,
    signature_line: int,
) -> None:
    file = context_file(tmp_path, base, head, filename)
    region = next(region for region in file["fold_regions"] if region["qualified_name"] == "process")
    assert region["right_start"] == signature_line
    assert file["symbol_context"]["H0_0"]["new"][0]["start"] == 1


@pytest.mark.parametrize(
    "prefix", ["// Ordinary\n", "//// Ordinary\n", "/*** Ordinary */\n", "//! Inner\n", "/*! Inner */\n"]
)
def test_rust_unattached_comments_do_not_expand_context(tmp_path: Path, prefix: str) -> None:
    base = prefix + "fn process() {\n    first();\n}\n"
    file = context_file(tmp_path, base, base.replace("first();", "changed();"), "sample.rs")
    assert file["symbol_context"]["H0_0"]["new"][0]["start"] == prefix.count("\n") + 1


@pytest.mark.parametrize(
    "prefix", ["/// Documentation\n\n", "/** Documentation */\n\n", "/// First\n\n#[inline]\n/// Last\n"]
)
def test_rust_doc_comments_remain_attached_across_blank_lines(tmp_path: Path, prefix: str) -> None:
    base = prefix + "fn process() {\n    first();\n}\n"
    file = context_file(
        tmp_path, base, base.replace("Documentation", "Updated").replace("first();", "changed();"), "sample.rs"
    )
    assert file["symbol_context"]["H0_0"]["new"][0]["start"] == 1


@pytest.mark.parametrize(
    ("declaration", "wrapper", "name", "kind"),
    [
        ("fn process() {}", "", "process", "function"),
        ("fn process();", "trait Container", "Container.process", "function"),
        ("struct Process;", "", "Process", "struct"),
        ("enum Process { Ready }", "", "Process", "enum"),
        ("union Process { value: u32 }", "", "Process", "union"),
        ("trait Process {}", "", "Process", "trait"),
        ("type Process = u32;", "", "Process", "type"),
        ("type Process;", "trait Container", "Container.Process", "type"),
        ("const PROCESS: u32 = 1;", "", "PROCESS", "constant"),
        ("static PROCESS: u32 = 1;", "", "PROCESS", "static"),
        ("mod process {}", "", "process", "module"),
        ("macro_rules! process { () => {} }", "", "process", "macro"),
    ],
)
def test_query_context_covers_each_rust_definition_kind(
    declaration: str,
    wrapper: str,
    name: str,
    kind: str,
) -> None:
    source = "/// First\n\n/** Second */\n#[cfg(test)]\n" + declaration + "\n"
    if wrapper:
        source = wrapper + " {\n" + source + "}\n"
    symbols = structural.outline_symbols(source, "rust")
    definitions = structural.flatten(symbols)
    assert len(definitions) == (2 if wrapper else 1)
    symbol = definitions[name]
    assert symbol.kind == kind
    assert symbol.context_range is not None
    offset = 1 if wrapper else 0
    assert (symbol.context_range.start_line, symbol.range.start_line) == (1 + offset, 5 + offset)
    assert symbol.signature is not None
    assert "First" not in symbol.signature and "cfg" not in symbol.signature


@pytest.mark.parametrize("separator", ["// Ordinary\n", "fn earlier() {}\n"])
def test_rust_attachment_queries_stop_at_other_siblings(separator: str) -> None:
    source = "/// Earlier docs\n" + separator + "#[inline]\nfn process() {}\n"
    symbols = structural.flatten(structural.outline_symbols(source, "rust"))
    symbol = symbols["process"]
    assert symbol.context_range is not None
    assert (symbol.context_range.start_line, symbol.range.start_line) == (3, 4)


def test_decorated_python_definitions_merge_query_matches_and_keep_nested_names() -> None:
    source = "@registered\nclass Container:\n    @property\n    @cached\n    def value(self):\n        return 1\n"
    symbols = structural.outline_symbols(source, "python")
    assert len(symbols) == 1
    container = symbols[0]
    assert len(container.children) == 1
    method = container.children[0]
    assert [container.qualified_name, method.qualified_name] == ["Container", "Container.value"]
    for symbol, context_start, definition_start in [(container, 1, 2), (method, 3, 5)]:
        assert symbol.context_range is not None
        assert (symbol.context_range.start_line, symbol.range.start_line) == (context_start, definition_start)
        assert "@" not in symbol.signature


def test_blank_separators_of_inserted_method_do_not_expand_parent_class(tmp_path: Path) -> None:
    base = "class Container:\n    def first(self):\n        return 1\n\n    def last(self):\n        return 2\n"
    head = base.replace("    def last", "\n    def inserted(self):\n        return 3\n\n    def last")
    file = context_file(tmp_path, base, head)
    assert file["symbol_context"]["H0_0"] == {
        "old": [],
        "new": [{"start": 6, "end": 7, "name": "Container.inserted"}],
    }


def test_blank_only_edit_inside_function_keeps_complete_symbol_context(tmp_path: Path) -> None:
    base = "def process():\n    first()\n\n    last()\n"
    file = context_file(tmp_path, base, base.replace("\n\n", "\n"))
    assert file["symbol_context"]["H0_0"] == {
        "old": [{"start": 1, "end": 4, "name": "process"}],
        "new": [{"start": 1, "end": 3, "name": "process"}],
    }
