"""The Python grep fallback reaches nothing outside the directory it searches.

ripgrep refuses to follow links that leave the tree; the Python worker
(utils.grep_worker) must too, or a link planted in a workspace leaks whatever
it points at into the search output.
"""

import pytest

from utils import confined_fs
from utils.grep_worker import search

linux_only = pytest.mark.skipif(not confined_fs.SUPPORTED, reason="needs dir_fd and O_NOFOLLOW (Linux)")


def _request(base, pattern="needle", **extra):
    request = {"pattern": pattern, "base": str(base)}
    request.update(extra)
    return request


def test_search_still_finds_matches_in_plain_files(tmp_path):
    (tmp_path / "a.txt").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("another needle\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("nothing\n", encoding="utf-8")

    answer = search(_request(tmp_path))

    assert answer == {"results": ["a.txt:1: needle here", "sub/b.txt:1: another needle"]}


@linux_only
def test_a_link_to_a_file_outside_is_never_read(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("REVIEW_PRIVATE_MARKER\n", encoding="utf-8")
    (workspace / "link.txt").symlink_to(secret)

    answer = search(_request(workspace, pattern="REVIEW_PRIVATE_MARKER"))

    assert answer == {"results": []}


@linux_only
def test_a_link_to_a_directory_outside_is_not_walked(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("REVIEW_PRIVATE_MARKER\n", encoding="utf-8")
    (workspace / "alias").symlink_to(outside, target_is_directory=True)

    answer = search(_request(workspace, pattern="REVIEW_PRIVATE_MARKER"))

    assert answer == {"results": []}
