from __future__ import annotations

import hashlib
import logging
import os
import sys


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    from .background import BackgroundScheduler, install_autostart
    from .paths import data_dir
    from .store import Store
    from .ui.main_window import MainWindow
    from .worker import Worker

    fake = bool(os.environ.get("IGREADER_FAKE"))
    hidden = "--background" in sys.argv[1:]
    root = data_dir()
    app = QApplication([a for a in sys.argv if a != "--background"])
    app.setApplicationName("IGReader")
    app.setDesktopFileName("igreader")

    # Single instance per data dir: a second launch just brings the running window back.
    server_name = "igreader-" + hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:12]
    sock = QLocalSocket()
    sock.connectToServer(server_name)
    if sock.waitForConnected(500):
        if not hidden:
            sock.write(b"show\n")
            sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return 0

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(root / "igreader.log", encoding="utf-8"), logging.StreamHandler()],
    )
    logging.getLogger("igreader").info("starting, data dir %s%s%s", root, " (FAKE)" if fake else "",
                                       " (hidden)" if hidden else "")

    QLocalServer.removeServer(server_name)  # stale socket from a crashed instance
    server = QLocalServer()
    server.listen(server_name)

    app.setQuitOnLastWindowClosed(False)  # closing the window only hides it (see MainWindow)
    store = Store(root)
    worker = Worker(store, fake=fake)
    win = MainWindow(store, worker, fake)
    app.aboutToQuit.connect(worker.shutdown)

    def on_connection():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            conn.readyRead.connect(lambda c=conn: (c.readAll(), win.bring_to_front()))
            conn.disconnected.connect(conn.deleteLater)

    server.newConnection.connect(on_connection)

    scheduler = BackgroundScheduler(store, worker, started_hidden=hidden, parent=app)
    worker.start()
    scheduler.start()
    if not fake:
        from .updater import Updater
        install_autostart()

        def restart():
            """Replace this process with a fresh hidden instance (loads an updated instaloader)."""
            worker.shutdown()
            server.close()
            logging.shutdown()
            os.execv(sys.executable, [sys.executable, "-m", "igreader", "--background"])

        updater = Updater(store, worker, parent=app, restart=restart,
                          can_restart=lambda: not win.isVisible() and not worker.pending)
        updater.start()
    if hidden:
        win.hidden_since_start = True
    else:
        win.show()
        QTimer.singleShot(1500, win.startup_refresh)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
