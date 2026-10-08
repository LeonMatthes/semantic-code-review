"""`scr review --wait <run_id>`: Claude's end of the stream (ADR 0009).

Against a live `ReviewServer` on a tmp run dir: a Send lands as a batch;
nothing sent is `nothing-yet` after the timeout; a server that has gone —
no `server.json`, a stale one, or one that reports the session ended —
is `ended` with the remaining drafts, which are then marked delivered by
the CLI writing the store directly. An unknown run id exits 2.
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semantic_code_review import paths
from semantic_code_review.cli import app
from semantic_code_review.review import stream
from semantic_code_review.review.comments import CommentStore
from semantic_code_review.review.server import ReviewServer


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def run_dir(runs_root: Path) -> paths.RunDir:
    rd = paths.RunDir(runs_root / "local-main-abc12345").create()
    rd.meta.write_text(json.dumps({"title": "t", "baseRefOid": "a", "headRefOid": "b"}), encoding="utf-8")
    rd.head.mkdir()
    (rd.head / "a.py").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    return rd


@pytest.fixture
def server(run_dir: paths.RunDir, tmp_path: Path):
    """A server holding the run, recorded in `server.json` as
    `serve_review` records it."""
    srv = ReviewServer(
        run_dir=run_dir,
        viewer_json={"version": "1", "files": [{"path": "a.py"}]},
        counterpart="claude",
        argv=(),
        prefs_path=tmp_path / "prefs.json",
    )
    srv.start()
    run_dir.server_json.write_text(json.dumps(srv.info.to_json()), encoding="utf-8")
    yield srv
    srv.stop()
    run_dir.server_json.unlink(missing_ok=True)


def _post(url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def _wait(run_dir: paths.RunDir, *, timeout: float = 5) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = stream.run_wait(run_dir, timeout=timeout, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _wait_in_background(server: ReviewServer, run_dir: paths.RunDir) -> tuple[threading.Thread, dict]:
    """Start a `--wait` on a thread and return once it is attached."""
    result: dict = {}

    def run() -> None:
        code, out, err = _wait(run_dir, timeout=10)
        result.update({"code": code, "out": out, "err": err})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(200):
        if server.session.listening:
            break
        time.sleep(0.01)
    assert server.session.listening
    return t, result


@pytest.fixture
def record_codex_queue(monkeypatch):
    def record(exit_code=0):
        commands = []

        def queue_command(arguments, *, check):
            commands.append(arguments)
            return subprocess.CompletedProcess(arguments, exit_code)

        monkeypatch.setattr(stream.subprocess, "run", queue_command)
        return commands

    return record


def test_a_send_lands_as_a_batch(server, run_dir: paths.RunDir) -> None:
    _post(server.url() + "/comments", {"id": "c1", "file": "a.py", "side": "new", "line": 3, "body": "why three?"})
    _post(server.url() + "/comments/c1/send")

    code, out, err = _wait(run_dir)

    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0] == "status: batch"
    assert lines[1] == f"# Batch 1 for {run_dir.slug}"
    assert "## c1 — new — a.py:3 (new)" in out
    assert "> why three?" in out
    assert "> 3 | three" in out
    assert {c.id: c.delivery for c in CommentStore(run_dir.comments).all()} == {"c1": "delivered"}


@pytest.mark.parametrize("queue_exit_code", [0, 7])
def test_codex_wait_queues_a_batch_and_returns_the_command_exit_code(
    server, run_dir, record_codex_queue, queue_exit_code
):
    body = 'Why "three"? Keep $(this) and `that` literal.\nA second line.'
    _post(server.url() + "/comments", {"id": "c1", "file": "a.py", "side": "new", "line": 3, "body": body})
    _post(server.url() + "/comments/c1/send")
    commands = record_codex_queue(queue_exit_code)
    result = CliRunner().invoke(
        app,
        [
            "review",
            "--wait",
            run_dir.slug,
            "--runs-root",
            str(run_dir.path.parent),
            "--codex-thread",
            "thread-123",
            "--codex-command",
            "/app with spaces/codex",
        ],
    )

    assert result.exit_code == queue_exit_code
    assert result.stdout == ""
    assert len(commands) == 1
    assert commands[0][:-1] == ["/app with spaces/codex", "queue", "--thread", "thread-123", "--message"]
    message = commands[0][-1]
    assert message.startswith(f"status: batch\n# Batch 1 for {run_dir.slug}\n")
    assert '> Why "three"? Keep $(this) and `that` literal.' in message
    assert "> A second line." in message
    assert "> 3 | three" in message
    assert CommentStore(run_dir.comments).all()[0].delivery == "delivered"


def test_codex_wait_renews_empty_polls_before_queueing(server, run_dir, monkeypatch, record_codex_queue, capsys):
    responses = iter(
        [
            {"status": "nothing-yet"},
            {"status": "nothing-yet"},
            {"status": "batch", "batch_no": 1, "entries": []},
        ]
    )

    def poll_wait(info, *, timeout):
        return next(responses)

    monkeypatch.setattr(stream, "_poll_wait", poll_wait)
    commands = record_codex_queue()

    assert stream.run_codex_wait(run_dir, timeout=5, codex_thread="thread-123", codex_command="codex") == 0
    assert len(commands) == 1
    assert commands[0][-1].startswith("status: batch\n")
    assert "nothing-yet" not in commands[0][-1]
    assert capsys.readouterr().out == ""


def test_codex_wait_queues_remaining_comments_when_the_review_ends(run_dir, record_codex_queue):
    store = CommentStore(run_dir.comments)
    store.upsert({"id": "draft", "file": "a.py", "side": "new", "line": 1, "body": "remaining feedback"})
    commands = record_codex_queue()

    assert stream.run_codex_wait(run_dir, timeout=5, codex_thread="thread-123", codex_command="codex") == 0
    assert len(commands) == 1
    assert commands[0][-1].startswith("status: ended\n")
    assert "> remaining feedback" in commands[0][-1]
    assert CommentStore(run_dir.comments).all()[0].delivery == "delivered"


def test_codex_wait_reports_a_missing_executable_without_changing_delivery(server, run_dir, tmp_path, capsys):
    _post(server.url() + "/comments", {"id": "c1", "file": "a.py", "side": "new", "line": 1, "body": "now"})
    _post(server.url() + "/comments/c1/send")

    assert (
        stream.run_codex_wait(
            run_dir, timeout=5, codex_thread="thread-123", codex_command=str(tmp_path / "missing-codex")
        )
        == 2
    )
    assert "could not execute" in capsys.readouterr().err
    assert CommentStore(run_dir.comments).all()[0].delivery == "delivered"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--wait", "unknown", "--codex-thread", "thread-123"],
        ["--wait", "unknown", "--codex-command", "codex"],
        ["HEAD", "--codex-thread", "thread-123", "--codex-command", "codex"],
    ],
)
def test_codex_options_require_each_other_and_wait(arguments):
    result = CliRunner().invoke(app, ["review", *arguments])
    assert result.exit_code == 2
    assert "must be used together with --wait" in result.stderr


def test_a_send_all_is_one_batch_and_a_later_send_the_next(server, run_dir: paths.RunDir) -> None:
    for i, line in enumerate((1, 2), start=1):
        _post(server.url() + "/comments", {"id": f"c{i}", "file": "a.py", "side": "new", "line": line, "body": "n"})
    _post(server.url() + "/comments/send-all")
    _post(server.url() + "/comments", {"id": "c3", "file": "a.py", "side": "new", "line": 4, "body": "later"})
    _post(server.url() + "/comments/c3/send")

    _, first, _ = _wait(run_dir)
    _, second, _ = _wait(run_dir)

    assert "# Batch 1 for" in first and "## c1 — new" in first and "## c2 — new" in first and "c3" not in first
    assert "# Batch 2 for" in second and "## c3 — new" in second and "_1 comment in this batch._" in second


def test_nothing_sent_is_nothing_yet(server, run_dir: paths.RunDir) -> None:
    code, out, err = _wait(run_dir, timeout=0.3)
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "status: nothing-yet"
    assert len(out.splitlines()) == 2


def test_a_wait_is_woken_by_a_send(server, run_dir: paths.RunDir) -> None:
    _post(server.url() + "/comments", {"id": "c1", "file": "a.py", "side": "new", "line": 1, "body": "now"})
    t, result = _wait_in_background(server, run_dir)

    _post(server.url() + "/comments/c1/send")
    t.join(timeout=5)

    assert not t.is_alive()
    assert result["out"].startswith("status: batch\n")


def test_no_server_json_is_ended_with_the_remaining_drafts(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert({"id": "d1", "file": "a.py", "side": "new", "line": 1, "body": "a draft"})
    store.upsert({"id": "s1", "file": "a.py", "side": "new", "line": 2, "body": "sent, never delivered"})
    store.send("s1")
    store.upsert({"id": "old", "file": "a.py", "side": "new", "line": 3, "body": "already delivered"})
    store.send("old")
    store.deliver(2)

    code, out, err = _wait(run_dir)

    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0] == "status: ended"
    assert lines[1] == f"# Review ended — remaining comments for {run_dir.slug}"
    assert "> a draft" in out and "> sent, never delivered" in out and "already delivered" not in out
    assert "_2 comments total._" in out
    assert all(c.delivery == "delivered" for c in CommentStore(run_dir.comments).all())
    # A second wait finds nothing left.
    _, again, _ = _wait(run_dir)
    assert "No comments left" in again


def test_a_stale_server_json_is_ended_and_removed(run_dir: paths.RunDir) -> None:
    run_dir.server_json.write_text(
        json.dumps({"port": 1, "pid": 1, "started_at": 0.0, "url": "http://127.0.0.1:1"}), encoding="utf-8"
    )
    code, out, _ = _wait(run_dir)
    assert code == 0 and out.startswith("status: ended\n")
    assert not run_dir.server_json.exists()


def test_a_server_that_ends_mid_wait_is_ended(server, run_dir: paths.RunDir, monkeypatch) -> None:
    monkeypatch.setattr(stream, "_ENDED_GRACE", 0.5)
    _post(server.url() + "/comments", {"id": "d1", "file": "a.py", "side": "new", "line": 1, "body": "left over"})
    t, result = _wait_in_background(server, run_dir)

    server.stop()
    run_dir.server_json.unlink()
    t.join(timeout=10)

    assert not t.is_alive()
    assert result["code"] == 0
    assert result["out"].startswith("status: ended\n")
    assert "> left over" in result["out"]


def test_an_unknown_run_id_exits_2(runs_root: Path) -> None:
    code, out, err = _wait(paths.RunDir(runs_root / "no-such-run"))
    assert code == 2
    assert out == ""
    assert "unknown run id 'no-such-run'" in err


def test_the_cli_surface(server, run_dir: paths.RunDir, runs_root: Path) -> None:
    """`scr review --wait <run_id> --runs-root … --wait-timeout N`."""
    _post(server.url() + "/comments", {"id": "c1", "file": "a.py", "side": "new", "line": 5, "body": "five"})
    _post(server.url() + "/comments/c1/send")

    result = CliRunner().invoke(
        app, ["review", "--wait", run_dir.slug, "--runs-root", str(runs_root), "--wait-timeout", "5"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("status: batch\n")
    assert "## c1 — new — a.py:5 (new)" in result.stdout

    missing = CliRunner().invoke(app, ["review", "--wait", "nope", "--runs-root", str(runs_root)])
    assert missing.exit_code == 2


# --- the reply channel: scr comment ------------------------------------------


def _comment(*args: str, runs_root: Path, stdin: str | None = None):
    return CliRunner().invoke(app, ["comment", *args, "--runs-root", str(runs_root)], input=stdin)


def test_reply_adds_a_claude_entry_to_the_thread(server, run_dir: paths.RunDir, runs_root: Path) -> None:
    _post(server.url() + "/comments", {"id": "c1", "file": "a.py", "side": "new", "line": 2, "body": "why?"})

    result = _comment("reply", run_dir.slug, "c1", "Because two.", runs_root=runs_root)

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("replied as claude-")
    by_id = {c.id: c for c in CommentStore(run_dir.comments).all()}
    [reply] = [c for c in by_id.values() if c.source == "claude"]
    assert reply.author == "claude" and reply.in_reply_to_id == "c1" and reply.body == "Because two."
    assert (reply.file, reply.side, reply.line) == ("a.py", "new", 2)
    # Fanned out live, so the open tab shows it without a reload.
    with server.ctx.state_lock:
        frames = [(ev.event_type, ev.payload) for ev in server.ctx.buffer]
    assert [p["id"] for t, p in frames if t == "comment" and p["source"] == "claude"] == [reply.id]


def test_reply_body_comes_from_stdin_when_omitted(server, run_dir: paths.RunDir, runs_root: Path) -> None:
    _post(server.url() + "/comments", {"id": "c1", "file": "a.py", "side": "new", "line": 2, "body": "why?"})
    result = _comment("reply", run_dir.slug, "c1", runs_root=runs_root, stdin="from stdin\nsecond line\n")
    assert result.exit_code == 0, result.output
    [reply] = [c for c in CommentStore(run_dir.comments).all() if c.source == "claude"]
    assert reply.body == "from stdin\nsecond line\n"

    empty = _comment("reply", run_dir.slug, "c1", runs_root=runs_root, stdin="  \n")
    assert empty.exit_code == 2 and "empty" in empty.output


def test_reply_to_an_unknown_comment_is_refused(server, run_dir: paths.RunDir, runs_root: Path) -> None:
    result = _comment("reply", run_dir.slug, "ghost", "hello", runs_root=runs_root)
    assert result.exit_code == 2
    assert "404" in result.output and "ghost" in result.output


def test_resolve_and_unresolve_flip_the_thread(server, run_dir: paths.RunDir, runs_root: Path) -> None:
    _post(server.url() + "/comments", {"id": "c1", "file": "a.py", "side": "new", "line": 2, "body": "why?"})
    _comment("reply", run_dir.slug, "c1", "done", runs_root=runs_root)

    resolved = _comment("resolve", run_dir.slug, "c1", runs_root=runs_root)

    assert resolved.exit_code == 0, resolved.output
    assert "resolved the thread of c1 (2 comment(s) changed)" in resolved.stdout
    assert all(c.thread_resolved for c in CommentStore(run_dir.comments).all())

    reopened = _comment("unresolve", run_dir.slug, "c1", runs_root=runs_root)

    assert reopened.exit_code == 0, reopened.output
    assert not any(c.thread_resolved for c in CommentStore(run_dir.comments).all())


def test_comment_commands_exit_2_without_a_live_server(run_dir: paths.RunDir, runs_root: Path) -> None:
    CommentStore(run_dir.comments).upsert({"id": "c1", "file": "a.py", "side": "new", "line": 2, "body": "why?"})

    result = _comment("reply", run_dir.slug, "c1", "too late", runs_root=runs_root)

    assert result.exit_code == 2
    assert "the review has ended" in result.output
    assert [c.id for c in CommentStore(run_dir.comments).all()] == ["c1"]  # nothing written
    unknown = _comment("resolve", "no-such-run", "c1", runs_root=runs_root)
    assert unknown.exit_code == 2 and "unknown run id" in unknown.output
