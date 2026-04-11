"""SeekSeek — 메인 엔트리포인트

NTFS MFT 기반 파일/내용 검색 앱의 진입점.
관리자 권한이 없으면 UAC 프롬프트를 통해 자동으로 재실행한다.

■ 실행 흐름
  1. 관리자 권한 확인 (_is_admin)
  2. 권한 없음 → ShellExecuteW("runas") 로 UAC 재실행 후 종료
  3. 권한 있음 → 로깅 설정 → PyQt6 앱 초기화 → MainWindow 표시

■ 로그 설정
  위치: %LOCALAPPDATA%/SeekSeek/debug.log
  수준: DEBUG (전체 로그)
  형식: "YYYY-MM-DD HH:MM:SS [LEVEL] logger_name: message"
  파일: RotatingFileHandler (최대 1MB, 백업 2개)
  콘솔: StreamHandler (개발 중 실시간 확인용)

■ Qt 메시지 핸들러 (_qt_msg_handler)
  DirectWrite 관련 폰트 경고(Fixedsys 등 레거시 폰트 사용 시 발생)를 필터링하여
  debug.log에 노이즈가 쌓이지 않게 한다. 그 외 Warning/Critical은 로그에 기록.
"""
import sys
import os
import ctypes
import logging
from logging.handlers import RotatingFileHandler

# 프로젝트 루트를 sys.path 최우선으로 추가하여 core/gui 패키지를 임포트할 수 있게 함
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)  # 상대 경로(assets/icon.ico 등)의 기준 디렉터리를 루트로 고정

# ── 로깅 초기화 ────────────────────────────────────────────────────────────────
# 앱 시작 시 가장 먼저 설정해야 이후 임포트된 모듈의 로그도 정상 기록된다.
_APP_DIR  = os.path.join(os.environ.get("LOCALAPPDATA", PROJECT_ROOT), "SeekSeek")
os.makedirs(_APP_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_APP_DIR, "debug.log")

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# 파일 핸들러: 최대 1MB, 백업 2개 회전 (총 최대 ~3MB 보관)
_file_handler = RotatingFileHandler(
    _LOG_FILE, maxBytes=1 * 1024 * 1024, backupCount=2, encoding="utf-8"
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_fmt)

# 스트림 핸들러: 콘솔 실시간 출력 (개발/디버깅용)
_stream_handler = logging.StreamHandler()
_stream_handler.setLevel(logging.DEBUG)
_stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.DEBUG, handlers=[_stream_handler, _file_handler])
# core / gui 패키지 로거를 DEBUG 수준으로 활성화
for _mod in ("core", "gui", "__main__"):
    logging.getLogger(_mod).setLevel(logging.DEBUG)

_ICON_PATH = os.path.join(PROJECT_ROOT, "assets", "icon.ico")


# ── 관리자 권한 확인 / 재실행 ──────────────────────────────────────────────────

def _is_admin() -> bool:
    """현재 프로세스가 관리자 권한으로 실행 중인지 확인한다."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_as_admin():
    """UAC 프롬프트를 통해 관리자 권한으로 현재 스크립트를 재실행한다.

    ShellExecuteW의 "runas" 동사를 사용하면 UAC 다이얼로그가 표시된다.
    사용자가 승인하면 새 프로세스가 관리자로 시작되고, 현재 프로세스는 종료된다.
    사용자가 거부하면 ShellExecuteW가 실패하고 현재 프로세스도 sys.exit(0)으로 종료.
    """
    script = os.path.abspath(__file__)
    extra  = sys.argv[1:]
    # 인자에 공백이 포함될 수 있으므로 각 인자를 따옴표로 감쌈
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

    # PyQt6 및 앱 윈도우를 관리자 권한이 확인된 이후에 임포트
    # (일반 권한으로 임포트되면 MFT 접근 시 PermissionError 발생 가능)
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QFont, QIcon
        from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
        from gui.main_window import MainWindow
    except Exception:
        logging.getLogger("__main__").exception("임포트 실패")
        return

    def _qt_msg_handler(msg_type, _context, message):
        """Qt 내부 메시지를 Python 로거로 라우팅하는 핸들러.

        Fixedsys 등 레거시 폰트 사용 시 발생하는 DirectWrite 경고는 무시한다.
        (SetWindowsHookEx, GDI 폰트 렌더러와 DirectWrite 간의 호환 경고이며 기능상 무해)
        """
        if "DirectWrite: CreateFontFaceFromHDC" in message:
            return  # 레거시 폰트 경고 무시
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
    app.setStyle("Fusion")  # 플랫폼 무관하게 일관된 Fusion 스타일 사용

    if os.path.isfile(_ICON_PATH):
        app.setWindowIcon(QIcon(_ICON_PATH))

    app.setFont(QFont("맑은 고딕", 10))  # 앱 기본 폰트

    try:
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except Exception:
        logging.getLogger("__main__").exception("앱 실행 중 예외 발생")
        sys.exit(1)


if __name__ == "__main__":
    main()
