"""First-run welcome panel.

Auto-opens the first time FD6 is launched (and re-openable from
Help -> Discord & auto-updates). Three clear choices:

    [ Link Discord ]   [ Check for updates ]   [ Skip ]

- Link Discord       — opens the Discord link/settings panel (optional; needed
                       for automatic update prompts + Rich Presence).
- Check for updates  — runs the GitHub update check right now.
- Skip               — close; the user can do all of this later from Help.

Plainly states linking is optional and that auto-updates need Discord link +
FD6-server membership.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout
)

import fd6


class WelcomeDialog(QDialog):
    """First-run welcome with Link Discord / Check for updates / Skip."""

    link_discord_requested = Signal()
    check_updates_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome to Forza Designer 6")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(14)

        heading = QLabel(f"Welcome to Forza Designer 6  ·  v{fd6.__version__}")
        hf = QFont(); hf.setBold(True); hf.setPointSize(14)
        heading.setFont(hf)
        heading.setAlignment(Qt.AlignCenter)
        root.addWidget(heading)

        body = QLabel(
            "Thanks for installing FD6!<br><br>"
            "<b>Link your Discord</b> (optional) to receive automatic update "
            "prompts and to show \"Using Forza Designer 6\" on your profile. "
            "Automatic updates require linking <b>and</b> being a member of the "
            "FD6 Discord server.<br><br>"
            "Not interested? Just <b>Skip</b> — everything still works, and you "
            "can always check for updates manually or link Discord later from "
            "<i>Help → Discord &amp; auto-updates</i>."
        )
        body.setTextFormat(Qt.RichText)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignCenter)
        root.addWidget(body)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.link_btn = QPushButton("Link Discord")
        self.link_btn.setProperty("accent", True)
        self.link_btn.setMinimumHeight(38)
        self.link_btn.clicked.connect(self._on_link)
        self.update_btn = QPushButton("Check for updates")
        self.update_btn.setMinimumHeight(38)
        self.update_btn.clicked.connect(self._on_check)
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setMinimumHeight(38)
        self.skip_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.link_btn)
        btn_row.addWidget(self.update_btn)
        btn_row.addWidget(self.skip_btn)
        root.addLayout(btn_row)

    def _on_link(self) -> None:
        # Hand off to MainWindow, then close the welcome panel.
        self.link_discord_requested.emit()
        self.accept()

    def _on_check(self) -> None:
        self.check_updates_requested.emit()
        self.accept()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is not None:
            geo = parent.frameGeometry()
        else:
            from PySide6.QtWidgets import QApplication
            geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.center().x() - self.width() // 2,
                  geo.center().y() - self.height() // 2)
