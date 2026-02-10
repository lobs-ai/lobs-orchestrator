import types

import pytest

from orchestrator.core.worker import WorkerManager


class _CP:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _mk_manager():
    # Avoid WorkerManager.__init__ (it provisions agents / reads settings).
    # These tests only exercise git helper methods.
    return WorkerManager.__new__(WorkerManager)


def test_git_pull_rebase_with_autostash_clean_repo(monkeypatch, tmp_path):
    mgr = _mk_manager()

    calls = []

    def fake_run(cmd, cwd=None, **kwargs):
        calls.append((tuple(cmd), str(cwd) if cwd else None))
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return _CP(stdout=b"")
        if cmd[:3] == ["git", "pull", "--rebase"]:
            return _CP(stdout=b"ok")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("orchestrator.core.worker.subprocess.run", fake_run)

    repo = tmp_path / "repo"
    repo.mkdir()

    mgr._git_pull_rebase_with_autostash(repo, task_id="abc12345")

    assert [c[0] for c in calls] == [
        ("git", "status", "--porcelain"),
        ("git", "pull", "--rebase"),
    ]


def test_git_pull_rebase_with_autostash_dirty_repo(monkeypatch, tmp_path):
    mgr = _mk_manager()

    calls = []

    def fake_run(cmd, cwd=None, **kwargs):
        calls.append((tuple(cmd), str(cwd) if cwd else None))
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return _CP(stdout=b" M foo.py\n")
        if cmd[:3] == ["git", "stash", "push"]:
            return _CP(stdout=b"saved")
        if cmd[:3] == ["git", "pull", "--rebase"]:
            return _CP(stdout=b"ok")
        if cmd[:3] == ["git", "stash", "pop"]:
            # In this codepath we run with text=True so stdout is a str.
            return types.SimpleNamespace(stdout="applied", stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("orchestrator.core.worker.subprocess.run", fake_run)

    repo = tmp_path / "repo"
    repo.mkdir()

    mgr._git_pull_rebase_with_autostash(repo, task_id="abc12345")

    assert [c[0][:3] for c in calls] == [
        ("git", "status", "--porcelain"),
        ("git", "stash", "push"),
        ("git", "pull", "--rebase"),
        ("git", "stash", "pop"),
    ]
