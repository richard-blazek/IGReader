from __future__ import annotations

import logging
import os
import sys


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .paths import data_dir
    from .store import Store
    from .ui.main_window import MainWindow
    from .worker import Worker

    fake = bool(os.environ.get("IGREADER_FAKE"))
    root = data_dir()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(root / "igreader.log", encoding="utf-8"), logging.StreamHandler()],
    )
    logging.getLogger("igreader").info("starting, data dir %s%s", root, " (FAKE)" if fake else "")

    app = QApplication(sys.argv)
    app.setApplicationName("IGReader")
    app.setDesktopFileName("igreader")
    store = Store(root)
    worker = Worker(store, fake=fake)
    win = MainWindow(store, worker, fake)
    worker.start()
    win.show()
    QTimer.singleShot(1500, win.startup_refresh)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
