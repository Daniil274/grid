"""File tools reach nothing outside a workspace that a container can change under them."""

import os
from types import SimpleNamespace

import pytest

from utils import confined_fs
from utils.path_utils import reset_current_factory, set_current_factory

linux_only = pytest.mark.skipif(not confined_fs.SUPPORTED, reason="needs dir_fd and O_NOFOLLOW (Linux)")


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("host secret", encoding="utf-8")
    return root


@pytest.fixture
def in_container(workspace):
    """A run with a container, working in *workspace*."""
    set_current_factory(SimpleNamespace(container_id="c0ffee", config=SimpleNamespace(get_working_directory=lambda: str(workspace))))
    yield workspace
    reset_current_factory()


@linux_only
def test_files_inside_are_read_written_and_removed(in_container):
    target = in_container / "src" / "notes.txt"

    confined_fs.make_dirs(target.parent)
    confined_fs.write_text(target, "hello")
    with confined_fs.open_file(target, "a", encoding="utf-8") as file:
        file.write(" world")

    assert confined_fs.read_text(target) == "hello world"
    confined_fs.unlink(target)
    assert not target.exists()


@linux_only
def test_a_directory_swapped_for_a_link_to_the_host_is_refused(in_container):
    """The race: the path was checked while x was a directory; now x points at /."""
    os.symlink(str(in_container.parent), in_container / "x")

    with pytest.raises(confined_fs.LinkRefused):
        confined_fs.read_text(in_container / "x" / "secret.txt")
    with pytest.raises(confined_fs.LinkRefused):
        confined_fs.write_text(in_container / "x" / "planted.txt", "x")
    with pytest.raises(confined_fs.LinkRefused):
        confined_fs.unlink(in_container / "x" / "secret.txt")
    assert (in_container.parent / "secret.txt").read_text() == "host secret"
    assert not (in_container.parent / "planted.txt").exists()


@linux_only
def test_a_file_swapped_for_a_link_is_refused(in_container):
    os.symlink(str(in_container.parent / "secret.txt"), in_container / "notes.txt")

    with pytest.raises(confined_fs.LinkRefused):
        confined_fs.read_text(in_container / "notes.txt")
    with pytest.raises(confined_fs.LinkRefused):
        confined_fs.write_text(in_container / "notes.txt", "overwritten")
    assert (in_container.parent / "secret.txt").read_text() == "host secret"
    # Removing the link removes the link, not the host file.
    confined_fs.unlink(in_container / "notes.txt")
    assert (in_container.parent / "secret.txt").exists()


def test_without_a_container_links_inside_the_workspace_keep_working(workspace):
    """One user, nobody else changing the workspace: plain operations."""
    real = workspace / "real"
    real.mkdir()
    (real / "a.txt").write_text("linked", encoding="utf-8")
    try:
        os.symlink(real, workspace / "alias", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this system does not let the tests create links")
    set_current_factory(SimpleNamespace(container_id=None, config=SimpleNamespace(get_working_directory=lambda: str(workspace))))
    try:
        assert confined_fs.read_text(workspace / "alias" / "a.txt") == "linked"
    finally:
        reset_current_factory()


@linux_only
def test_a_path_outside_the_workspace_is_refused(in_container):
    with pytest.raises(ValueError, match="escapes"):
        confined_fs.read_text(in_container.parent / "secret.txt")


@linux_only
@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="giving files away needs root")
def test_a_server_running_as_root_gives_what_it_makes_to_the_workspace_owner(in_container):
    # The container's unprivileged user owns the workspace; files the server
    # made as root it could not change.
    os.chown(in_container, 1000, 1000)
    target = in_container / "src" / "notes.txt"
    (in_container / "old.txt").write_text("root's", encoding="utf-8")

    confined_fs.make_dirs(target.parent)
    confined_fs.write_text(target, "hello")
    confined_fs.write_text(in_container / "old.txt", "rewritten")
    confined_fs.read_text(in_container / "old.txt")

    for path in (target.parent, target, in_container / "old.txt"):
        assert (path.stat().st_uid, path.stat().st_gid) == (1000, 1000), path


@linux_only
def test_reading_changes_no_owner(in_container, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    given = []
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: given.append(fd))
    (in_container / "a.txt").write_text("x", encoding="utf-8")

    confined_fs.read_text(in_container / "a.txt")

    assert given == []
