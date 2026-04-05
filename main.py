"""SeekSeek — 메인 엔트리포인트

관리자 권한 필수. 일반 권한으로 실행 시 UAC를 통해 재실행한다.
"""
import sys
import os
import ctypes
import logging
from logging.handlers import RotatingFileHandler

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

_APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", PROJECT_ROOT), "SeekSeek")
os.makedirs(_APP_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_APP_DIR, "debug.log")

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_file_handler = RotatingFileHandler(
    _LOG_FILE, maxBytes=1 * 1024 * 1024, backupCount=2, encoding="utf-8"
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_fmt)

_stream_handler = logging.StreamHandler()
_stream_handler.setLevel(logging.DEBUG)
_stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.DEBUG, handlers=[_stream_handler, _file_handler])
for _mod in ("core", "gui", "__main__"):
    logging.getLogger(_mod).setLevel(logging.DEBUG)

_ICON_PATH = os.path.join(PROJECT_ROOT, "assets", "icon.ico")


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_as_admin():
    """UAC 프롬프트를 통해 관리자 권한으로 재실행한다."""
    script = os.path.abspath(__file__)
    extra  = sys.argv[1:]
    params = " ".join(f'"{a}"' for a in [script] + extra)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )
    sys.exit(0)


def main():
    admin = _is_admin()
    logging.getLogger("__main__").info("관리자 여부: %s", admin)
    if not admin:
        logging.getLogger("__main__").warning("관리자 아님 → UAC 재실행 시도")
        _relaunch_as_admin()
        return

    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QFont, QIcon
        from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
        from gui.main_window import MainWindow
    except Exception:
        logging.getLogger("__main__").exception("임포트 실패")
        return

    def _qt_msg_handler(msg_type, context, message):
        if "DirectWrite: CreateFontFaceFromHDC" in message:
            return
        if msg_type == QtMsgType.QtWarningMsg:
            logging.getLogger("qt").warning(message)
        elif msg_type == QtMsgType.QtCriticalMsg:
            logging.getLogger("qt").error(message)

    qInstallMessageHandler(_qt_msg_handler)

    try:
        app = QApplication(sys.argv)
    except Exception:
        logging.getLogger("__main__").exception("QApplication 생성 실패")
        return

    app.setApplicationName("SeekSeek")
    app.setStyle("Fusion")

    if os.path.isfile(_ICON_PATH):
        app.setWindowIcon(QIcon(_ICON_PATH))

    app.setFont(QFont("맑은 고딕", 10))

    try:
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except Exception:
        logging.getLogger("__main__").exception("앱 실행 중 예외 발생")
        sys.exit(1)


if __name__ == "__main__":
    main()
