from __future__ import annotations

import difflib
import json
import subprocess

import pytest
import tree_sitter

from semantic_code_review import paths, structural
from semantic_code_review.augment import overview, tools
from semantic_code_review.structural import parse, references
from semantic_code_review.viewer import build_json, hunk_layout


@pytest.mark.parametrize("path", ["main.rs", "src/lib.rs", "crates/example/src/main.rs"])
def test_rust_language_detection(path):
    assert structural.language_for_path(path) == "rust"


def test_rust_query_compiles_and_parses():
    language, query = parse._load("rust")
    tree = tree_sitter.Parser(language).parse(b"fn main() {}")
    assert not tree.root_node.has_error
    captures = tree_sitter.QueryCursor(query).captures(tree.root_node)
    assert len(captures["definition.function"]) == 1


@pytest.mark.parametrize(
    ("source", "kind", "name", "signature"),
    [
        ("pub fn run() {}", "function", "run", "pub fn run()"),
        ("pub struct Named { pub value: u32 }", "struct", "Named", "pub struct Named"),
        ("pub struct Tuple<T>(pub T) where T: Copy;", "struct", "Tuple", "pub struct Tuple<T>(pub T) where T: Copy"),
        ("pub struct Unit;", "struct", "Unit", "pub struct Unit"),
        ("pub enum Choice { First, Second(u32) }", "enum", "Choice", "pub enum Choice"),
        ("pub union Bits { value: u32 }", "union", "Bits", "pub union Bits"),
        ("pub trait Read: Sized {}", "trait", "Read", "pub trait Read: Sized"),
        ("pub type Alias<T> = Option<T>;", "type", "Alias", "pub type Alias<T> = Option<T>"),
        ("pub const LIMIT: usize = 4;", "constant", "LIMIT", "pub const LIMIT: usize"),
        ("pub static mut COUNT: usize = 0;", "static", "COUNT", "pub static mut COUNT: usize"),
        ("pub mod nested {}", "module", "nested", "pub mod nested"),
        ("pub mod external;", "module", "external", "pub mod external"),
        ("macro_rules! make { () => { fn generated() {} } }", "macro", "make", "macro_rules! make"),
    ],
)
def test_rust_declarations(source, kind, name, signature):
    symbols = structural.outline_symbols(source, "rust")
    assert len(symbols) == 1
    symbol = symbols[0]
    assert (symbol.kind, symbol.name, symbol.signature) == (kind, name, signature)
    assert symbol.children == []


@pytest.mark.parametrize(
    ("source", "qualified_name", "signature"),
    [
        (
            "pub async unsafe fn borrow<'a, T>(\n value: &'a T,\n) -> &'a T\nwhere T: Copy { value }",
            "borrow",
            "pub async unsafe fn borrow<'a, T>( value: &'a T, ) -> &'a T where T: Copy",
        ),
        (
            'pub unsafe extern "C" fn export(value: u32) -> u32 { value }',
            "export",
            'pub unsafe extern "C" fn export(value: u32) -> u32',
        ),
        ('unsafe extern "C" { pub fn foreign(value: u32) -> u32; }', "foreign", "pub fn foreign(value: u32) -> u32"),
        ("trait Read { fn read(&self) -> u32; }", "Read.read", "fn read(&self) -> u32"),
        ("trait Read { fn ready(&self) -> bool { true } }", "Read.ready", "fn ready(&self) -> bool"),
        ("trait Read { type Item: Copy; }", "Read.Item", "type Item: Copy"),
        ("trait Read { const CAPACITY: usize; }", "Read.CAPACITY", "const CAPACITY: usize"),
        ("impl Read for Value { type Item = u32; }", "Item", "type Item = u32"),
        ("impl Value { pub fn get(&self) -> u32 { 1 } }", "get", "pub fn get(&self) -> u32"),
        ("impl Value { const CAPACITY: usize = 1; }", "CAPACITY", "const CAPACITY: usize"),
        ("mod nested { impl Value { fn get(&self) {} } }", "nested.get", "fn get(&self)"),
    ],
)
def test_rust_nested_signatures(source, qualified_name, signature):
    symbols = structural.flatten(structural.outline_symbols(source, "rust"))
    assert symbols[qualified_name].signature == signature


def test_rust_definitions_are_captured_once_and_nested_by_containment():
    source = """mod nested {
    trait Read {
        fn read(&self) -> u32;
        fn ready(&self) -> bool { true }
    }
    impl Value {
        fn get(&self) -> u32 { 1 }
    }
    fn free() {}
}
"""
    symbols = structural.outline_symbols(source, "rust")
    module = symbols[0]
    assert [symbol.name for symbol in module.children] == ["Read", "get", "free"]
    assert [symbol.qualified_name for symbol in module.children[0].children] == [
        "nested.Read.read",
        "nested.Read.ready",
    ]
    assert (module.range.start_line, module.range.end_line) == (1, 10)
    method = structural.enclosing_symbol(symbols, 7)
    assert method is not None
    assert method.qualified_name == "nested.get"
    assert (method.range.start_line, method.range.end_line) == (7, 7)


def test_rust_local_bindings_and_imports_are_not_symbols():
    source = "use std::io; fn run(value: u32) { let local = value; }"
    symbols = structural.outline_symbols(source, "rust")
    assert [symbol.name for symbol in symbols] == ["run"]
    assert symbols[0].children == []


@pytest.mark.parametrize("source", ["", "fn valid() {}\nfn broken("])
def test_rust_empty_source_and_error_recovery(source):
    assert [symbol.name for symbol in structural.outline_symbols(source, "rust")] == (["valid"] if source else [])


@pytest.mark.parametrize(
    ("base_source", "head_source", "bucket", "reason"),
    [
        ("", "fn work() {}", "added", None),
        ("fn work() {}", "", "removed", None),
        ("fn work() {}", "fn work(value: u32) {}", "modified", "signature"),
        ("fn work() { 1; }", "fn work() { 2; }", "modified", "body"),
        ("const LIMIT: u32 = 1;", "const LIMIT: u32 = 2;", "modified", "body"),
        ("static LIMIT: u32 = 1;", "static LIMIT: u32 = 2;", "modified", "body"),
        ("fn work() {}\n", "\nfn work() {}\n", "moved", None),
    ],
)
def test_rust_symbol_changes(base_source, head_source, bucket, reason):
    delta = structural.diff_file(
        "lib.rs",
        structural.outline_symbols(base_source, "rust"),
        structural.outline_symbols(head_source, "rust"),
        base_src=base_source,
        head_src=head_source,
    )
    for bucket_name in ("added", "removed", "modified", "moved"):
        changes = getattr(delta, bucket_name)
        assert len(changes) == (1 if bucket_name == bucket else 0)
    change = getattr(delta, bucket)[0]
    assert change.reason == reason


@pytest.mark.parametrize(
    ("expression", "name", "expected_count"),
    [
        ("helper();", "helper", 1),
        ("module::helper();", "helper", 1),
        ("value.helper();", "helper", 1),
        ("helper::<u32>();", "helper", 1),
        ("module::helper::<u32>();", "helper", 1),
        ("value.helper::<u32>();", "helper", 1),
        ("helper!();", "helper", 1),
        ("module::helper! {};", "helper", 1),
        ("other(helper);", "helper", 0),
        ("helper_two();", "helper", 0),
        ('let text = "helper()";', "helper", 0),
        ("// helper()\n", "helper", 0),
        ("other::<Helper>();", "Helper", 0),
        ("get(helper).other();", "helper", 0),
    ],
)
def test_rust_call_references(expression, name, expected_count):
    source = f"fn run() {{\n    {expression}\n}}"
    uses = references.references(source, "lib.rs", name)
    assert len(uses) == expected_count
    assert all(use.line == 2 and use.kind == "call" for use in uses)


@pytest.mark.parametrize("name", ["Read", "Value"])
def test_rust_implementation_header_references(name):
    source = "impl<T> module::Read<T> for Value<T> { fn run() {} }"
    uses = references.references(source, "lib.rs", name)
    assert len(uses) == 1
    assert uses[0].kind == "implementation"


def test_rust_definition_is_not_a_reference():
    assert references.references("fn helper() {}", "lib.rs", "helper") == []


@pytest.fixture
def rust_review(tmp_path):
    run_dir = paths.RunDir(tmp_path / "run").create()
    run_dir.base.mkdir()
    run_dir.head.mkdir()
    base_source = """mod operations {
    /// Compute a value.
    #[inline]
    pub fn compute(value: u32) -> u32 {
        value + 1
    }
}
"""
    head_source = base_source.replace("value + 1", "value + 2") + "fn added() { operations::compute(1); }\n"
    for directory in (run_dir.base, run_dir.head):
        (directory / "lib.rs").write_text(base_source, encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("add", "lib.rs"),
        ("-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-q", "-m", "Base"),
    ):
        subprocess.run(["git", *arguments], cwd=run_dir.head, check=True, capture_output=True)
    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=run_dir.head, text=True).strip()
    (run_dir.head / "lib.rs").write_text(head_source, encoding="utf-8")
    raw_diff = "diff --git a/lib.rs b/lib.rs\n" + "".join(
        difflib.unified_diff(
            base_source.splitlines(keepends=True), head_source.splitlines(keepends=True), "a/lib.rs", "b/lib.rs"
        )
    )
    run_dir.raw_diff.write_text(raw_diff, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps(
            {
                "title": "Change Rust",
                "url": "",
                "author": {"login": "test"},
                "baseRefOid": base_sha,
                "headRefOid": base_sha,
            }
        ),
        encoding="utf-8",
    )
    repo_tools = tools.RepoTools(
        head_worktree=run_dir.head, repo_git=run_dir.head, base_sha=base_sha, head_sha=base_sha
    )
    return run_dir, repo_tools


def test_rust_agent_tools_and_prompt_seed(rust_review):
    run_dir, repo_tools = rust_review
    outline = json.loads(tools.mcp_dispatch(repo_tools, "outline", {"path": "lib.rs"}))
    assert [symbol["name"] for symbol in outline] == ["operations", "added"]
    symbol = json.loads(tools.mcp_dispatch(repo_tools, "symbol_at", {"path": "lib.rs", "line": 5}))
    assert symbol["qualified_name"] == "operations.compute"
    assert "pub fn compute(value: u32) -> u32" in repo_tools.outline_seed("lib.rs")
    delta = repo_tools.compute_symbol_delta()
    assert [symbol.qualified_name for symbol in delta.added] == ["added"]
    assert {symbol.qualified_name for symbol in delta.modified} == {"operations", "operations.compute"}
    dispatched = json.loads(tools.mcp_dispatch(repo_tools, "changed_symbols", {"path": "lib.rs"}))
    assert dispatched["added"][0]["name"] == "added"
    seed = overview._format_symbol_seed(delta)
    assert "function added" in seed
    assert "function operations.compute" in seed
    uses = repo_tools.references("compute", path_glob="*.rs")
    assert uses.startswith("1 use site(s)")
    assert "lib.rs:8" in uses
    assert "fallback" not in uses


def test_rust_sidebar_and_base_head_folds(rust_review):
    run_dir, _ = rust_review
    data = build_json.build_pending_viewer_json(run_dir)
    assert data["files"][0]["language"] == "rust"
    assert [symbol["title"] for symbol in data["symbols"]] == ["operations", "added"]
    assert data["symbols"][0]["children"][0]["title"] == "compute"
    region = next(
        region for region in data["files"][0]["fold_regions"] if region["qualified_name"] == "operations.compute"
    )
    assert (region["left_start"], region["left_end"]) == (4, 6)
    assert (region["right_start"], region["right_end"]) == (4, 6)
    assert region["has_changes"]


def test_rust_doc_comments_and_attributes_attach_to_definition():
    source = "/// Documentation.\n#[inline]\npub fn work() {\n    1;\n}\n"
    symbols = structural.outline_symbols(source, "rust")
    spans = build_json._fold_spans(symbols)
    line_text = dict(enumerate(source.splitlines(), start=1))
    assert hunk_layout._definition_edges(line_text, spans) == {1}
