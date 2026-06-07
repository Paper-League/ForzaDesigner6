import sys
from pathlib import Path
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from fd6.gui.main_window import MainWindow
from fd6.gui.splash import maybe_show_splash


def main() -> int:
    from fd6.gui.startup_log import log, reset
    reset(); log("main() start")
    app = QApplication(sys.argv)
    app.setApplicationName("Forza Designer 6")
    app.setOrganizationName("FD6")
    # Load bundled TTFs and apply the user's saved font choice (or the
    # "Default" pseudo-family = Segoe UI Variable → Segoe UI → sans fallback).
    from fd6.gui.fonts import load_bundled_fonts, apply_font, saved_font_name
    load_bundled_fonts()
    apply_font(app, saved_font_name())
    # Window icon matches the current theme's badge (Default → Pink)
    from fd6.gui.brand_banner import badge_path
    from fd6.gui.themes import badge_filename_for_theme, saved_theme_name
    bp = badge_path(badge_filename_for_theme(saved_theme_name()))
    if bp:
        app.setWindowIcon(QIcon(str(bp)))

    # Apply persisted theme before constructing MainWindow so styling applies cleanly
    from fd6.gui.themes import apply_theme, saved_theme_name
    apply_theme(app, saved_theme_name())

    log("constructing MainWindow")
    win = MainWindow()
    log("MainWindow constructed")

    def show_main():
        log("show_main()")
        win.show()
        win.raise_()
        win.activateWindow()
        log("window shown")
        # Defer music start by one event-loop tick so any splash teardown
        # finishes first (two simultaneous QMediaPlayer streams during splash
        # close was crashing the app).
        from PySide6.QtCore import QTimer
        QTimer.singleShot(150, win.start_music)
        # Deterministically run the post-splash startup flow (welcome panel /
        # update check). Triggered here rather than in showEvent because the
        # frozen --windowed build proved unreliable at firing it from showEvent.
        def _safe_startup():
            try:
                win.run_startup_flow()
            except Exception as exc:
                log(f"run_startup_flow EXCEPTION: {type(exc).__name__}: {exc}")
        QTimer.singleShot(350, _safe_startup)

    # Show splash if SplashScreen.mp4 is present, then open main window when video ends or user clicks/keypress.
    # If no splash file, show main window immediately.
    splash = maybe_show_splash(show_main)
    if splash is None:
        # Already shown by callback above
        pass

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
