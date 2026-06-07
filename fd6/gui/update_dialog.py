"""Centered 'Checking for updates' panel shown on every launch.

Lifecycle:
  1. Opens centered, modal, showing a "Checking for updates…" spinner.
  2. Runs UpdateChecker on a background thread (no UI freeze).
  3. Result:
       • up to date        → message + Close.
       • newer available   → "Update X available — install now?" with
                             Install / Later buttons.
       • check failed      → message + Close (and a Releases-page link).
  4. Install → downloads the new .exe with a progress bar, stages a swap+relaunch
     (see updater.download_and_apply), then asks the app to quit so the swap runs.

Everything is best-effort: any network/parse/download failure just tells the
user and lets them continue into the app.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QDialog, QLabel, QProgressBar, QPushButton, QHBoxLayout, QVBoxLayout
)

import fd6
from fd6.gui import updater


class _DownloadWorker(QThread):
    progress = Signal(int, int)   # read, total
    done = Signal()
    failed = Signal(str)

    def __init__(self, info, parent=None) -> None:
        super().__init__(parent)
        self._info = info

    def run(self) -> None:
        try:
            updater.download_and_apply(
                self._info,
                progress_cb=lambda r, t: self.progress.emit(r, t),
            )
            self.done.emit()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class UpdateDialog(QDialog):
    """Modal, centered update check / install panel."""

    quit_requested = Signal()  # emitted after a successful download+stage

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Forza Designer 6 — Updates")
        self.setModal(True)
        self.setMinimumWidth(440)
        flags = self.windowFlags() & ~Qt.WindowContextHelpButtonHint
        self.setWindowFlags(flags)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(12)

        self.heading = QLabel("Checking for updates…")
        hf = QFont(); hf.setBold(True); hf.setPointSize(13)
        self.heading.setFont(hf)
        self.heading.setAlignment(Qt.AlignCenter)
        root.addWidget(self.heading)

        self.body = QLabel(f"You're on v{fd6.__version__}. Contacting GitHub…")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignCenter)
        root.addWidget(self.body)

        # Indeterminate during the check; switches to a real % during download.
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)  # busy/indeterminate
        self.progress.setTextVisible(False)
        root.addWidget(self.progress)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.secondary_btn = QPushButton("Later")
        self.secondary_btn.clicked.connect(self.accept)
        self.secondary_btn.setVisible(False)
        self.primary_btn = QPushButton("Install update")
        self.primary_btn.setProperty("accent", True)
        self.primary_btn.setVisible(False)
        self.primary_btn.clicked.connect(self._on_install)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        self.close_btn.setVisible(False)
        btn_row.addWidget(self.secondary_btn)
        btn_row.addWidget(self.primary_btn)
        btn_row.addWidget(self.close_btn)
        root.addLayout(btn_row)

        self._info = None
        self._dl_worker: _DownloadWorker | None = None

        # Kick off the check on a background thread.
        self._chk_thread = QThread(self)
        self._checker = updater.UpdateChecker()
        self._checker.moveToThread(self._chk_thread)
        self._chk_thread.started.connect(self._checker.run)
        self._checker.update_available.connect(self._on_available)
        self._checker.no_update.connect(self._on_up_to_date)
        self._checker.failed.connect(self._on_failed)
        for sig in (self._checker.update_available, self._checker.no_update, self._checker.failed):
            sig.connect(self._stop_check_thread)
        self._chk_thread.start()

    # ── check results ────────────────────────────────────────────────────
    def _stop_check_thread(self, *_a) -> None:
        if self._chk_thread is not None:
            self._chk_thread.quit()
            self._chk_thread.wait(2000)
            self._chk_thread = None

    def _on_up_to_date(self) -> None:
        self.progress.setVisible(False)
        self.heading.setText("You're up to date")
        self.body.setText(f"Forza Designer 6 v{fd6.__version__} is the latest version.")
        self.close_btn.setVisible(True)
        self.close_btn.setDefault(True)

    def _on_failed(self, msg: str) -> None:
        self.progress.setVisible(False)
        self.heading.setText("Couldn't check for updates")
        self.body.setText(
            f"{msg}\n\nYou can keep using v{fd6.__version__}, or check the "
            "releases page in your browser."
        )
        self.secondary_btn.setText("Open releases page")
        self.secondary_btn.setVisible(True)
        self.secondary_btn.clicked.disconnect()
        self.secondary_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE)))
        self.close_btn.setVisible(True)
        self.close_btn.setDefault(True)

    def _on_available(self, info) -> None:
        self._info = info
        self.progress.setVisible(False)
        self.heading.setText(f"Update {info.version} available")
        notes = ""
        if info.notes:
            n = info.notes if len(info.notes) <= 400 else info.notes[:400] + "…"
            notes = f"\n\nWhat's new:\n{n}"
        self.body.setText(
            f"You have v{fd6.__version__}. Would you like to install "
            f"v{info.version} now?{notes}"
        )
        # Auto-install only makes sense for a frozen single-exe build with a
        # downloadable .exe asset. From source (or no asset) we can't safely
        # swap the running python — point the user at the releases page instead.
        if not info.asset_url or not updater.is_frozen():
            self.primary_btn.setText("Open releases page")
            try:
                self.primary_btn.clicked.disconnect()
            except Exception:
                pass
            self.primary_btn.clicked.connect(
                lambda: (QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE)), self.accept()))
        self.primary_btn.setVisible(True)
        self.primary_btn.setDefault(True)
        self.secondary_btn.setText("Later")
        self.secondary_btn.setVisible(True)

    # ── install ──────────────────────────────────────────────────────────
    def _on_install(self) -> None:
        if self._info is None:
            return
        self.primary_btn.setEnabled(False)
        self.secondary_btn.setEnabled(False)
        self.heading.setText(f"Downloading update {self._info.version}…")
        self.body.setText("Please wait — FD6 will relaunch on the new version when done.")
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        self.progress.setValue(0)

        self._dl_worker = _DownloadWorker(self._info, self)
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.done.connect(self._on_dl_done)
        self._dl_worker.failed.connect(self._on_dl_failed)
        self._dl_worker.start()

    def _on_dl_progress(self, read: int, total: int) -> None:
        if total:
            self.progress.setValue(int(100 * read / total))

    def _on_dl_done(self) -> None:
        self.heading.setText("Restarting to finish update")
        self.body.setText("Download complete. FD6 will now close and relaunch on the new version.")
        self.quit_requested.emit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Center over the parent window (or the screen if parentless).
        parent = self.parentWidget()
        if parent is not None:
            geo = parent.frameGeometry()
        else:
            from PySide6.QtWidgets import QApplication
            geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.center().x() - self.width() // 2,
                  geo.center().y() - self.height() // 2)

    def _on_dl_failed(self, msg: str) -> None:
        self.progress.setVisible(False)
        self.heading.setText("Update failed")
        self.body.setText(f"Couldn't download the update:\n{msg}")
        self.primary_btn.setVisible(False)
        self.secondary_btn.setText("Open releases page")
        self.secondary_btn.setEnabled(True)
        self.secondary_btn.setVisible(True)
        try:
            self.secondary_btn.clicked.disconnect()
        except Exception:
            pass
        self.secondary_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE)))
        self.close_btn.setVisible(True)
