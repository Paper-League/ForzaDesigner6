"""Discord link + Rich Presence settings panel.

Shown:
  • Once on first launch (optional, clearly states it's not required).
  • Any time via Help -> Discord & auto-updates.

Lets the user:
  • Link / Unlink their Discord (PKCE, no secret).
  • Toggle Discord Rich Presence ("Using Forza Designer 6").

Explains plainly that auto-updates require linking AND being a member of the
FD6 server — and that not linking only costs them the auto-update panel.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QDialog, QLabel, QPushButton, QCheckBox, QVBoxLayout, QHBoxLayout, QFrame
)

from fd6.gui import discord_link


class DiscordSettingsDialog(QDialog):
    """Link/unlink Discord + Rich Presence toggle. `rich_presence_changed`
    fires with the new bool so MainWindow can start/stop presence live."""

    rich_presence_changed = Signal(bool)

    def __init__(self, parent=None, first_launch: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Discord & Auto-Updates")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(12)

        heading = QLabel("Link Discord for auto-updates" if first_launch
                         else "Discord & auto-updates")
        hf = QFont(); hf.setBold(True); hf.setPointSize(13)
        heading.setFont(hf)
        root.addWidget(heading)

        info = QLabel(
            "Linking your Discord is <b>optional</b> — Forza Designer 6 works "
            "fully without it.<br><br>"
            "If you link your Discord <b>and</b> you're a member of the FD6 "
            "Discord server, FD6 will automatically check for and offer updates "
            "on launch. <b>If you don't link (or aren't in the server), you "
            "won't get automatic update prompts</b> — you can still update "
            "manually any time via <i>Help → Check for updates</i>.<br><br>"
            "No bot, no password, and no client secret is used — linking opens "
            "Discord in your browser and only reads your username and which "
            "servers you're in."
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        root.addWidget(info)

        # Server invite line
        invite_row = QHBoxLayout()
        join_btn = QPushButton("Join the FD6 Discord server")
        join_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(discord_link.DISCORD_INVITE_URL)))
        invite_row.addWidget(join_btn)
        invite_row.addStretch()
        root.addLayout(invite_row)

        line = QFrame(); line.setFrameShape(QFrame.HLine); line.setStyleSheet("color:#333")
        root.addWidget(line)

        # Status + link/unlink
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        action_row = QHBoxLayout()
        self.link_btn = QPushButton("Link Discord")
        self.link_btn.setProperty("accent", True)
        self.link_btn.clicked.connect(self._on_link)
        self.unlink_btn = QPushButton("Unlink")
        self.unlink_btn.clicked.connect(self._on_unlink)
        action_row.addWidget(self.link_btn)
        action_row.addWidget(self.unlink_btn)
        action_row.addStretch()
        root.addLayout(action_row)

        # Rich Presence toggle
        self.rp_check = QCheckBox("Show \"Using Forza Designer 6\" on my Discord (Rich Presence)")
        self.rp_check.setChecked(discord_link.rich_presence_enabled())
        self.rp_check.toggled.connect(self._on_rp_toggled)
        root.addWidget(self.rp_check)

        # Close
        close_row = QHBoxLayout(); close_row.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        close_row.addWidget(self.close_btn)
        root.addLayout(close_row)

        self._link_thread: QThread | None = None
        self._link_worker = None
        self._refresh_status()

    def _refresh_status(self) -> None:
        if discord_link.is_linked():
            name = discord_link.linked_username() or "your account"
            self.status_label.setText(f"✅ Linked as <b>{name}</b>.")
            self.link_btn.setText("Re-link")
            self.unlink_btn.setEnabled(True)
        else:
            self.status_label.setText("Not linked. Auto-updates are off until you link + join the server.")
            self.link_btn.setText("Link Discord")
            self.unlink_btn.setEnabled(False)

    def _on_link(self) -> None:
        self.link_btn.setEnabled(False)
        self.link_btn.setText("Opening Discord in your browser…")
        self.status_label.setText("Waiting for you to authorize in the browser…")
        self._link_thread = QThread(self)
        self._link_worker = discord_link.LinkWorker()
        self._link_worker.moveToThread(self._link_thread)
        self._link_thread.started.connect(self._link_worker.run)
        self._link_worker.succeeded.connect(self._on_link_ok)
        self._link_worker.failed.connect(self._on_link_fail)
        self._link_worker.membership.connect(self._on_membership)
        for sig in (self._link_worker.succeeded, self._link_worker.failed):
            sig.connect(self._stop_link_thread)
        self._link_thread.start()

    def _stop_link_thread(self, *_a) -> None:
        if self._link_thread is not None:
            self._link_thread.quit()
            self._link_thread.wait(2000)
            self._link_thread = None
            self._link_worker = None

    def _on_link_ok(self, _link) -> None:
        self.link_btn.setEnabled(True)
        self._refresh_status()

    def _on_link_fail(self, msg: str) -> None:
        self.link_btn.setEnabled(True)
        self._refresh_status()
        self.status_label.setText(f"Link failed: {msg}")

    def _on_membership(self, is_member: bool) -> None:
        if is_member:
            self.status_label.setText(self.status_label.text() +
                                      "<br>🟢 You're in the FD6 server — auto-updates are on.")
        else:
            self.status_label.setText(self.status_label.text() +
                                      "<br>🟡 Linked, but you don't appear to be in the FD6 server yet — "
                                      "join it (button above) to receive auto-updates.")

    def _on_unlink(self) -> None:
        discord_link.clear_link()
        self._refresh_status()

    def _on_rp_toggled(self, on: bool) -> None:
        discord_link.set_rich_presence_enabled(on)
        self.rich_presence_changed.emit(on)
