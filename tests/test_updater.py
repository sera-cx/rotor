"""Tests for Rotor's autonomous git updater."""

from __future__ import annotations

import logging
from pathlib import Path

import rotor.updater as updater
from rotor.updater import (
    DailyGitUpdater,
    GitCommandResult,
    GitUpdateResult,
    pull_latest,
)


def test_pull_latest_skips_non_git_worktree(tmp_path):
    calls: list[list[str]] = []

    def fake_git(_repo: Path, args: list[str], _timeout: float) -> GitCommandResult:
        calls.append(args)
        return GitCommandResult(returncode=1, stderr="not a git repository")

    result = pull_latest(repo_path=tmp_path, git_runner=fake_git)

    assert result.status == "skipped"
    assert "not a git worktree" in result.message
    assert calls == [["rev-parse", "--is-inside-work-tree"]]


def test_pull_latest_skips_dirty_worktree(tmp_path):
    calls: list[list[str]] = []
    responses = iter([
        GitCommandResult(returncode=0, stdout="true\n"),
        GitCommandResult(returncode=0, stdout=f"{tmp_path}\n"),
        GitCommandResult(returncode=0, stdout=" M rotor/mm/vl_runner.py\n"),
    ])

    def fake_git(_repo: Path, args: list[str], _timeout: float) -> GitCommandResult:
        calls.append(args)
        return next(responses)

    result = pull_latest(repo_path=tmp_path, git_runner=fake_git)

    assert result.status == "skipped"
    assert "local changes" in result.message
    assert ["pull", "--ff-only"] not in calls


def test_pull_latest_reports_fast_forward_update(tmp_path):
    before = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    after = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    calls: list[list[str]] = []
    responses = iter([
        GitCommandResult(returncode=0, stdout="true\n"),
        GitCommandResult(returncode=0, stdout=f"{tmp_path}\n"),
        GitCommandResult(returncode=0, stdout=""),
        GitCommandResult(returncode=0, stdout="main\n"),
        GitCommandResult(returncode=0, stdout=f"{before}\n"),
        GitCommandResult(returncode=0, stdout="Fast-forward\n"),
        GitCommandResult(returncode=0, stdout=f"{after}\n"),
    ])

    def fake_git(_repo: Path, args: list[str], _timeout: float) -> GitCommandResult:
        calls.append(args)
        return next(responses)

    result = pull_latest(
        repo_path=tmp_path,
        remote="origin",
        branch="main",
        git_runner=fake_git,
    )

    assert result.status == "updated"
    assert result.updated is True
    assert result.before == before
    assert result.after == after
    assert ["pull", "--ff-only", "origin", "main"] in calls


def test_pull_latest_reports_unchanged_when_head_does_not_move(tmp_path):
    head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    responses = iter([
        GitCommandResult(returncode=0, stdout="true\n"),
        GitCommandResult(returncode=0, stdout=f"{tmp_path}\n"),
        GitCommandResult(returncode=0, stdout=""),
        GitCommandResult(returncode=0, stdout="main\n"),
        GitCommandResult(returncode=0, stdout=f"{head}\n"),
        GitCommandResult(returncode=0, stdout="Already up to date.\n"),
        GitCommandResult(returncode=0, stdout=f"{head}\n"),
    ])

    def fake_git(_repo: Path, _args: list[str], _timeout: float) -> GitCommandResult:
        return next(responses)

    result = pull_latest(repo_path=tmp_path, git_runner=fake_git)

    assert result.status == "unchanged"
    assert result.updated is False
    assert result.before == head
    assert result.after == head


def test_daily_git_updater_runs_only_when_due(tmp_path):
    calls: list[dict] = []

    def fake_puller(**kwargs) -> GitUpdateResult:
        calls.append(kwargs)
        return GitUpdateResult(status="unchanged", message="already up to date")

    updater = DailyGitUpdater(
        repo_path=tmp_path,
        interval_seconds=10,
        remote="origin",
        branch="main",
        puller=fake_puller,
    )
    log = logging.getLogger("tests.updater")

    assert updater.run_if_due(log, now=100.0).status == "unchanged"
    assert updater.run_if_due(log, now=105.0) is None
    assert updater.run_if_due(log, now=111.0).status == "unchanged"
    assert len(calls) == 2
    assert calls[0]["repo_path"] == tmp_path
    assert calls[0]["remote"] == "origin"
    assert calls[0]["branch"] == "main"


def test_daily_git_updater_contains_puller_exceptions(tmp_path):
    def broken_puller(**_kwargs) -> GitUpdateResult:
        raise RuntimeError("network unavailable")

    updater = DailyGitUpdater(repo_path=tmp_path, puller=broken_puller)

    result = updater.run_if_due(logging.getLogger("tests.updater"), now=100.0)

    assert result is not None
    assert result.status == "failed"
    assert "network unavailable" in result.message


def test_restart_current_process_reexecs_same_command(monkeypatch):
    captured = {}

    def fake_execv(path, args):
        captured["path"] = path
        captured["args"] = args
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(updater.os, "execv", fake_execv)
    monkeypatch.setattr(updater.sys, "executable", "/venv/bin/python")
    monkeypatch.setattr(updater.sys, "argv", ["rotor", "mm", "vl", "--auto-update"])

    try:
        updater.restart_current_process()
    except RuntimeError as exc:
        assert str(exc) == "exec intercepted"

    assert captured == {
        "path": "/venv/bin/python",
        "args": ["/venv/bin/python", "rotor", "mm", "vl", "--auto-update"],
    }


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _deployment(tmp_path: Path) -> tuple[Path, Path]:
    """An upstream repo and a deployment cloned from it, as earn sets one up."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "README.md").write_text("v1\n")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "-q", "-m", "v1")
    deploy = tmp_path / "deploy"
    _git(tmp_path, "clone", "-q", str(upstream), str(deploy))
    # The operator's files live in the checkout, untracked.
    (deploy / "rotor").mkdir()
    (deploy / "rotor" / "vl.config.toml").write_text('fixed_bps = "50"\n')
    (deploy / ".rotor_state.testnet.sqlite").write_text("")
    (upstream / "README.md").write_text("v2\n")
    _git(upstream, "commit", "-q", "-am", "v2")
    return upstream, deploy


def test_untracked_operator_files_do_not_block_an_update(tmp_path):
    upstream, deploy = _deployment(tmp_path)

    result = pull_latest(repo_path=deploy)

    assert result.status == "updated"
    assert result.after == _git(upstream, "rev-parse", "HEAD")
    assert (deploy / "rotor" / "vl.config.toml").read_text() == 'fixed_bps = "50"\n'


def test_a_changed_tracked_file_still_blocks_an_update(tmp_path):
    _upstream, deploy = _deployment(tmp_path)
    (deploy / "README.md").write_text("edited by hand\n")

    result = pull_latest(repo_path=deploy)

    assert result.status == "skipped"
    assert "local changes" in result.message


def _pulled_to(head: str):
    def puller(**_kwargs) -> GitUpdateResult:
        return GitUpdateResult(status="unchanged", message="already up to date",
                               before=head, after=head)
    return puller


def test_new_code_must_pass_its_start_check_before_a_restart(tmp_path):
    checks: list[int] = []
    outcomes = iter([
        GitCommandResult(returncode=1, stderr="ModuleNotFoundError: No module named 'newdep'"),
        GitCommandResult(returncode=0, stdout="config ok"),
    ])

    def start_check() -> GitCommandResult:
        checks.append(1)
        return next(outcomes)

    log = logging.getLogger("tests.updater")
    updater = DailyGitUpdater(
        repo_path=tmp_path, interval_seconds=10, running_commit="a" * 40,
        puller=_pulled_to("b" * 40), start_check=start_check,
    )

    first = updater.run_if_due(log, now=100.0)
    # The new version cannot start: keep running the old one and say why.
    assert first.status == "failed"
    assert "keeps running aaaaaaaaaaaa" in first.message
    assert "poetry install" in first.message and "newdep" in first.message

    # The next check (no new pull) tries the same new version again.
    second = updater.run_if_due(log, now=111.0)
    assert second.updated is True
    assert (second.before, second.after) == ("a" * 40, "b" * 40)
    assert len(checks) == 2


def test_a_version_that_fails_its_check_is_not_left_checked_out(tmp_path):
    upstream, deploy = _deployment(tmp_path)
    running = _git(deploy, "rev-parse", "HEAD")
    outcomes = iter([GitCommandResult(returncode=1, stderr="ImportError: newdep"),
                     GitCommandResult(returncode=0)])
    updater = DailyGitUpdater(repo_path=deploy, interval_seconds=10, running_commit=running,
                              start_check=lambda: next(outcomes))
    log = logging.getLogger("tests.updater")

    first = updater.run_if_due(log, now=100.0)

    # A --once fire or restart now would load v1 again, not the failing v2.
    assert first.status == "failed"
    assert "the checkout is back on it" in first.message
    assert _git(deploy, "rev-parse", "HEAD") == running
    assert (deploy / "README.md").read_text() == "v1\n"
    # The operator's untracked files are untouched.
    assert (deploy / "rotor" / "vl.config.toml").read_text() == 'fixed_bps = "50"\n'

    # The next interval pulls v2 again, and this time it passes.
    second = updater.run_if_due(log, now=111.0)
    assert second.updated is True
    assert second.after == _git(upstream, "rev-parse", "HEAD")


def test_no_start_check_while_running_the_checked_out_commit(tmp_path):
    def start_check() -> GitCommandResult:
        raise AssertionError("not needed")

    updater = DailyGitUpdater(
        repo_path=tmp_path, running_commit="a" * 40,
        puller=_pulled_to("a" * 40), start_check=start_check,
    )

    assert updater.run_if_due(logging.getLogger("tests.updater"), now=1.0).status == "unchanged"


def test_a_skipped_update_is_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        updater.log_update_result(
            logging.getLogger("tests.updater"),
            GitUpdateResult(status="skipped", message="checkout is detached"),
        )

    assert "auto-update skipped: checkout is detached" in caplog.text


def test_a_failed_pull_says_why_without_credentials(tmp_path):
    def failed_pull(**_kwargs) -> GitUpdateResult:
        return GitUpdateResult(
            status="failed", message="git pull --ff-only failed",
            output="fatal: unable to access 'https://oauth2:glpat-SECRET@lab.example/rotor.git/'",
        )

    updater = DailyGitUpdater(repo_path=tmp_path, running_commit="a" * 40, puller=failed_pull)
    result = updater.run_if_due(logging.getLogger("tests.updater"), now=1.0)

    assert "unable to access 'https://<credentials>@lab.example/rotor.git/'" in result.message
    assert "glpat-SECRET" not in result.message
