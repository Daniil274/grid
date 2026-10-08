"""Restore brings a workspace back to before a turn, whatever changed its files."""

import subprocess

from core.checkpoints import Checkpoints


def workspace_with_repo(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "repo").mkdir(parents=True)
    (workspace / "notes.md").write_text("notes v1")
    (workspace / ".gitignore").write_text("build/\n")
    (workspace / "repo" / "app.py").write_text("print(1)\n")
    subprocess.run(["git", "init", "-q", str(workspace / "repo")], check=True)
    return workspace


def test_changes_by_any_means_are_listed_and_restored(tmp_path):
    workspace = workspace_with_repo(tmp_path)
    checkpoints = Checkpoints(tmp_path / "store")
    before = checkpoints.snapshot(workspace)

    # What a turn might do: edit, create, delete - in the workspace and in a nested repo.
    (workspace / "notes.md").write_text("notes v2")
    (workspace / "repo" / "app.py").unlink()
    (workspace / "repo" / "new" / "mod.py").parent.mkdir(parents=True)
    (workspace / "repo" / "new" / "mod.py").write_text("x = 1\n")
    (workspace / "build").mkdir()
    (workspace / "build" / "out.bin").write_text("ignored")
    after = checkpoints.snapshot(workspace)

    assert sorted((c["path"], c["status"]) for c in checkpoints.changes(before, after)) == [
        ("notes.md", "modified"), ("repo/app.py", "deleted"), ("repo/new/mod.py", "added"),
    ]

    restored = checkpoints.restore(workspace, before)

    assert sorted(r["path"] for r in restored) == ["notes.md", "repo/app.py", "repo/new/mod.py"]
    assert (workspace / "notes.md").read_text() == "notes v1"
    assert (workspace / "repo" / "app.py").read_text() == "print(1)\n"
    assert not (workspace / "repo" / "new").exists()
    assert (workspace / "build" / "out.bin").exists()  # ignored: never touched
    # The workspace's own repository was left alone.
    status = subprocess.run(["git", "-C", str(workspace / "repo"), "status", "--porcelain"], capture_output=True, text=True)
    assert "app.py" in status.stdout and not (workspace / ".git").exists()


def test_a_checkpoint_with_nothing_changed_restores_nothing(tmp_path):
    workspace = workspace_with_repo(tmp_path)
    checkpoints = Checkpoints(tmp_path / "store")
    before = checkpoints.snapshot(workspace)
    assert checkpoints.changes(before, checkpoints.snapshot(workspace)) == []
    assert checkpoints.restore(workspace, before) == []
