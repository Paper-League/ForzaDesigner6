"""Regression tests for the generation queue.

The bug: status was tracked by file PATH, and adding the same image multiple
times made every duplicate resolve to the first matching row — later duplicates
never left "queued", so the auto-advance loop (_on_finished -> _start_next)
re-ran forever. Each entry now carries a unique id and runs exactly once.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from PySide6.QtWidgets import QApplication, QLabel

from fd6.gui.queue_panel import QueuePanel


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


def test_duplicate_paths_each_run_once(_app):
    q = QueuePanel()
    p = Path("same.png")
    uids = [q.add(p), q.add(p), q.add(p)]
    assert len(set(uids)) == 3, "each queued entry must get a unique id"

    ran = []
    guard = 0
    while True:
        guard += 1
        assert guard < 50, "queue never drains — infinite loop regression!"
        nxt = q.pop_next_queued()
        if nxt is None:
            break
        uid, _path = nxt
        q.set_status(uid, "running")
        q.set_status(uid, "done")
        ran.append(uid)

    assert ran == uids, "each duplicate path must generate exactly once"


def test_remove_by_uid_only_targets_that_entry(_app):
    q = QueuePanel()
    p = Path("dup.png")
    u1, u2 = q.add(p), q.add(p)
    assert q.remove(u1) is True
    # The second duplicate must survive and still be runnable.
    nxt = q.pop_next_queued()
    assert nxt is not None and nxt[0] == u2


def test_running_item_cannot_be_removed(_app):
    q = QueuePanel()
    uid = q.add(Path("busy.png"))
    q.set_status(uid, "running")
    assert q.remove(uid) is False


def test_title_marks_fix(_app):
    q = QueuePanel()
    labels = [w.text() for w in q.findChildren(QLabel)]
    assert any('fixed in 0.5.0' in t for t in labels), labels


def test_per_row_download_button_appears_on_done(_app):
    q = QueuePanel()
    got = []
    q.download_requested.connect(lambda uid: got.append(uid))
    u1 = q.add(Path("a.png"))
    u2 = q.add(Path("a.png"))  # duplicate path, separate entry

    row1 = q._row_widget_at(0)
    assert row1.dl_btn.isVisibleTo(row1) is False  # hidden while queued
    assert q.json_path_for(u1) is None

    q.set_json_path(u1, Path("out/a.json"))
    q.set_status(u1, "done")
    assert row1.dl_btn.isVisibleTo(row1) is True   # shown on done
    assert q.json_path_for(u1) == Path("out/a.json")
    # The other duplicate is still queued — its button stays hidden.
    row2 = q._row_widget_at(1)
    assert row2.dl_btn.isVisibleTo(row2) is False

    row1.dl_btn.click()
    assert got == [u1], "row download must emit that row's uid"
