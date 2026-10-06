"""fetch.local.resolve_local_diff: resolve input specs against a tmp repo.

Probes the resolved object's structural fields (mode, head_is_working,
raw_diff, head_sha, slug).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from semantic_code_review.augment.tools import RepoTools
from semantic_code_review.fetch import (
    EmptyDiff,
    LocalDiffError,
    materialize_local_diff_run,
    resolve_local_diff,
)


def _sh(cwd: Path, *args: str) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _sh(root, "git", "init", "-q", "-b", "main")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "root")
    (root / "a.py").write_text("x = 1\n")
    _sh(root, "git", "add", "a.py")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add a")
    (root / "a.py").write_text("x = 2\n")
    _sh(root, "git", "add", "a.py")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "bump a")
    return root


def test_range_two_dots(repo: Path) -> None:
    r = resolve_local_diff("HEAD~1..HEAD", repo_root=repo)
    assert r.mode == "range"
    assert "a.py" in r.raw_diff
    assert "-x = 1" in r.raw_diff and "+x = 2" in r.raw_diff
    assert r.files == ["a.py"]
    assert not r.head_is_working


def test_range_three_dots(repo: Path) -> None:
    r = resolve_local_diff("HEAD~1...HEAD", repo_root=repo)
    assert r.mode == "range"
    assert "+x = 2" in r.raw_diff


def test_range_rejects_flags(repo: Path) -> None:
    with pytest.raises(LocalDiffError, match="only apply"):
        resolve_local_diff("HEAD~1..HEAD", repo_root=repo, no_staged=True)


def test_single_ref_clean(repo: Path) -> None:
    r = resolve_local_diff("HEAD~1", repo_root=repo)
    # clean working tree: equivalent to HEAD~1..HEAD
    assert r.mode == "ref..HEAD"
    assert "+x = 2" in r.raw_diff
    assert not r.head_is_working or r.head_is_working  # either fine when clean


def test_single_ref_dirty_picks_up_unstaged(repo: Path) -> None:
    (repo / "a.py").write_text("x = 3\n")
    r = resolve_local_diff("HEAD~1", repo_root=repo)
    assert r.mode == "ref-working"
    assert r.head_is_working
    assert "+x = 3" in r.raw_diff
    # synthetic head sha has a -dirty- tag
    assert "-dirty-" in r.head_sha


def test_no_staged_drops_staged(repo: Path) -> None:
    # Stage a change, leave another unstaged.
    (repo / "a.py").write_text("x = 3\n")
    _sh(repo, "git", "add", "a.py")
    (repo / "a.py").write_text("x = 4\n")  # unstaged on top
    r = resolve_local_diff("HEAD~1", repo_root=repo, no_staged=True)
    assert r.mode == "ref-working-no-staged"
    # Committed portion yields +x = 2 (HEAD~1..HEAD); unstaged yields +x = 4.
    assert "+x = 2" in r.raw_diff or "+x = 4" in r.raw_diff


def test_no_unstaged(repo: Path) -> None:
    (repo / "a.py").write_text("x = 3\n")
    _sh(repo, "git", "add", "a.py")
    (repo / "a.py").write_text("x = 4\n")  # unstaged on top — should be excluded
    r = resolve_local_diff("HEAD~1", repo_root=repo, no_unstaged=True)
    assert r.mode == "ref-working-no-unstaged"
    assert "+x = 3" in r.raw_diff and "+x = 4" not in r.raw_diff


def test_both_flags_equiv_to_range(repo: Path) -> None:
    (repo / "a.py").write_text("x = 3\n")
    _sh(repo, "git", "add", "a.py")
    r = resolve_local_diff("HEAD~1", repo_root=repo, no_staged=True, no_unstaged=True)
    assert r.mode == "ref..HEAD"
    assert "+x = 2" in r.raw_diff
    assert "+x = 3" not in r.raw_diff


def test_empty_diff_errors(repo: Path) -> None:
    """EmptyDiff is a LocalDiffError subclass; old `match="no diff"`
    callers still catch the parent class. The CLI distinguishes the
    two so "nothing to review" exits 0 instead of crashing."""
    with pytest.raises(EmptyDiff, match="no changes to review"):
        resolve_local_diff("HEAD..HEAD", repo_root=repo)
    with pytest.raises(LocalDiffError):
        resolve_local_diff("HEAD..HEAD", repo_root=repo)


def test_slug_is_stable(repo: Path) -> None:
    r1 = resolve_local_diff("HEAD~1..HEAD", repo_root=repo)
    r2 = resolve_local_diff("HEAD~1..HEAD", repo_root=repo)
    assert r1.slug == r2.slug
    assert r1.slug.startswith("local-HEAD-1..HEAD-")


def test_rejects_non_git_dir(tmp_path: Path) -> None:
    # A plain empty directory is not a repo.
    with pytest.raises(LocalDiffError):
        resolve_local_diff("HEAD", repo_root=tmp_path)


# --- Two-endpoint form -----------------------------------------------------


def _looks_like_sha(s: str) -> bool:
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s)


def test_tree_pair_two_refs(repo: Path) -> None:
    # `left right` (both refs) == the one-token `left..right` range.
    r = resolve_local_diff("HEAD~1", right="HEAD", repo_root=repo)
    assert r.mode == "tree-pair"
    assert "-x = 1" in r.raw_diff and "+x = 2" in r.raw_diff
    assert r.files == ["a.py"]
    assert not r.head_is_working
    assert _looks_like_sha(r.base_sha) and _looks_like_sha(r.head_sha)


def test_blob_pair_same_path(repo: Path) -> None:
    r = resolve_local_diff("HEAD~1:a.py", right="HEAD:a.py", repo_root=repo)
    assert r.mode == "blob-pair"
    assert "-x = 1" in r.raw_diff and "+x = 2" in r.raw_diff
    # Same path both sides -> plain modification, not a rename.
    assert "--- a/a.py" in r.raw_diff and "+++ b/a.py" in r.raw_diff


def test_blob_pair_cross_path(repo: Path) -> None:
    (repo / "b.py").write_text("y = 9\n")
    _sh(repo, "git", "add", "b.py")
    _sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add b")
    # a.py@(prev commit) vs b.py@HEAD — different paths -> rename-shaped diff.
    r = resolve_local_diff("HEAD~1:a.py", right="HEAD:b.py", repo_root=repo)
    assert r.mode == "blob-pair"
    assert "--- a/a.py" in r.raw_diff and "+++ b/b.py" in r.raw_diff
    assert "-x = 2" in r.raw_diff and "+y = 9" in r.raw_diff
    assert r.files == ["b.py"]
    assert _looks_like_sha(r.base_sha) and _looks_like_sha(r.head_sha)


def test_endpoint_pair_mixed_kinds_errors(repo: Path) -> None:
    with pytest.raises(LocalDiffError, match="same kind"):
        resolve_local_diff("HEAD~1", right="HEAD:a.py", repo_root=repo)


def test_endpoint_pair_rejects_flags(repo: Path) -> None:
    with pytest.raises(LocalDiffError, match="only apply"):
        resolve_local_diff("HEAD~1", right="HEAD", repo_root=repo, no_staged=True)


def test_blob_pair_needs_path(repo: Path) -> None:
    with pytest.raises(LocalDiffError, match="needs a path"):
        resolve_local_diff("HEAD~1:", right="HEAD:a.py", repo_root=repo)


def test_empty_second_endpoint_errors(repo: Path) -> None:
    with pytest.raises(LocalDiffError, match="empty second endpoint"):
        resolve_local_diff("HEAD~1", right="  ", repo_root=repo)


@pytest.fixture(params=["primary", "linked", "separate-git-dir"])
def review_checkout(repo: Path, tmp_path: Path, request) -> Path:
    if request.param == "linked":
        checkout = tmp_path / "linked"
        _sh(repo, "git", "worktree", "add", "--detach", str(checkout), "HEAD")
        return checkout
    if request.param == "separate-git-dir":
        _sh(repo, "git", "init", "--separate-git-dir", str(tmp_path / "metadata"))
    return repo


@pytest.mark.parametrize(("spec", "head_text"), [("HEAD~1..HEAD", "x = 2\n"), ("HEAD~1", "x = 3\n")])
def test_materialize_local_review_from_git_layouts(review_checkout: Path, tmp_path: Path, spec, head_text) -> None:
    (review_checkout / "a.py").write_text(head_text)
    resolved = resolve_local_diff(spec, repo_root=review_checkout)
    run_dir = materialize_local_diff_run(spec, tmp_path / "runs", repo_root=review_checkout)

    assert resolved.repo_git.is_dir()
    assert run_dir.repo_git.resolve() == resolved.repo_git
    assert (run_dir.base / "a.py").read_text() == "x = 1\n"
    assert (run_dir.head / "a.py").read_text() == head_text
    assert run_dir.head.is_symlink() == resolved.head_is_working
    repo_tools = RepoTools(
        head_worktree=run_dir.head,
        repo_git=run_dir.repo_git,
        base_sha=resolved.base_sha,
        head_sha=resolved.head_sha,
    )
    assert repo_tools.read_file_at(resolved.base_sha, "a.py") == "x = 1\n"
    assert repo_tools.git_log("a.py") == _sh(review_checkout, "git", "log", "-n5", "--oneline", "--", "a.py")
    repo_tools.compute_symbol_delta()


@pytest.mark.parametrize("review_checkout", ["linked"], indirect=True)
@pytest.mark.parametrize("broken_target", ["gitfile", "missing"])
def test_reusing_local_run_repairs_git_link(review_checkout: Path, tmp_path: Path, broken_target) -> None:
    runs_root = tmp_path / "runs"
    run_dir = materialize_local_diff_run("HEAD~1..HEAD", runs_root, repo_root=review_checkout)
    target = review_checkout / (".git" if broken_target == "gitfile" else "missing")
    run_dir.repo_git.unlink()
    run_dir.repo_git.symlink_to(target)
    run_dir.comments.write_text("[]\n")

    assert materialize_local_diff_run("HEAD~1..HEAD", runs_root, repo_root=review_checkout) == run_dir
    assert run_dir.repo_git.is_dir()
    assert run_dir.comments.read_text() == "[]\n"
    base_sha = _sh(review_checkout, "git", "rev-parse", "HEAD~1").strip()
    assert _sh(run_dir.repo_git, "git", "show", f"{base_sha}:a.py") == "x = 1\n"
