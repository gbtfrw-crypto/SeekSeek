"""SeekSeek — 전역 설정 모듈

앱 전반에서 참조하는 경로 상수, 동작 파라미터, 사용자 설정 로드/저장 함수를 정의한다.
이 모듈은 다른 어떤 내부 모듈도 임포트하지 않으며, 모든 내부 모듈이 임포트할 수 있다.

■ 사용자 설정 파일
  경로: %LOCALAPPDATA%/SeekSeek/settings.json
  형식: UTF-8 JSON, 키-값 맵
  저장 항목: excluded_paths (제외 경로 목록), excluded_dirs (제외 폴더명 집합)
"""
import os
import json
import logging

logger = logging.getLogger(__name__)

# ── 앱 데이터 경로 ──────────────────────────────────────────────────────────
# %LOCALAPPDATA%가 없는 환경(테스트 등)에서는 현재 디렉터리로 폴백
APP_DIR       = os.path.join(os.environ.get("LOCALAPPDATA", "."), "SeekSeek")
DB_PATH       = os.path.join(APP_DIR, "index.db")       # SQLite 데이터베이스
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")  # 사용자 설정

# ── 스캔 기본 설정 ──────────────────────────────────────────────────────────
# 빈 리스트이면 NTFS 드라이브를 자동 감지(get_ntfs_drives())
MFT_SCAN_DRIVES: list[str] = []

# ── 본문 인덱싱 대상 확장자 ───────────────────────────────────────────────────
# 이 집합에 포함된 확장자만 텍스트 추출 + FTS5 색인 대상이 된다.
# 바이너리/미디어 파일은 제외하여 불필요한 I/O와 DB 용량 낭비를 방지한다.
CONTENT_EXTENSIONS = {
    # 소스 코드
    ".txt", ".md", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h",
    ".cs", ".html", ".css", ".xml", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".log", ".csv", ".bat", ".ps1", ".sh",
    ".sql", ".r", ".go", ".rs", ".kt", ".swift", ".rb", ".php",
    # 오피스 문서
    ".pdf", ".docx", ".xlsx", ".pptx",
    # 한글 문서
    ".hwpx", ".hwp",
}

# ── 본문 처리 크기 제한 ───────────────────────────────────────────────────────
# MAX_CONTENT_SIZE: 이 크기를 초과하는 파일은 색인하지 않는다 (메모리·성능 보호)
MAX_CONTENT_SIZE = 200 * 1024 * 1024   # 200 MB
# MAX_PREVIEW_SIZE: 미리보기 렌더링 시 이 크기까지만 표시 (QTextEdit 부하 제한)
MAX_PREVIEW_SIZE = 100 * 1024           # 100 KB

# ── 잘 알려진 제외 폴더 목록 ──────────────────────────────────────────────────
# (폴더명, 설명, 기본 활성화 여부) 튜플 리스트.
# UI(제외 폴더 설정 다이얼로그)에서 이 목록을 체크박스로 보여주고,
# 기본값(True/False)으로 초기 상태를 결정한다.
WELL_KNOWN_EXCLUDED_DIRS: list[tuple[str, str, bool]] = [
    ("node_modules",              "Node.js 패키지 (파일 수 매우 많음)",     True),
    ("__pycache__",               "Python 바이트코드 캐시",                True),
    ("$Recycle.Bin",              "Windows 휴지통",                       True),
    ("System Volume Information", "Windows 시스템 볼륨 정보",              True),
    ("venv",                      "Python 가상환경 (venv)",                True),
    ("AppData",                   "Windows 앱 데이터",                    True),
    ("ProgramData",               "Windows 프로그램 데이터",               True),
    ("dist",                      "빌드 결과물 (dist)",                    False),
    ("build",                     "빌드 결과물 (build)",                   False),
    ("target",                    "Rust/Java 빌드 결과물 (target)",        False),
    ("vendor",                    "Go/PHP 외부 패키지 (vendor)",           False),
    ("tmp",                       "임시 파일 (tmp)",                       False),
    ("temp",                      "임시 파일 (temp)",                      False),
    ("__MACOSX",                  "macOS ZIP 아티팩트",                    False),
    ("coverage",                  "코드 커버리지 결과물",                   False),
]

# 기본 활성화(True)인 항목만 초기 제외 집합으로 추출
EXCLUDED_DIRS: set[str] = {
    name for name, _, default in WELL_KNOWN_EXCLUDED_DIRS if default
}

# ── 기본 제외 경로 ────────────────────────────────────────────────────────────
# 첫 실행 시 settings.json이 없으면 이 목록이 excluded_paths 기본값으로 사용된다.
# Windows 시스템 디렉터리 및 사용자 데이터 캐시 폴더를 기본적으로 제외한다.
DEFAULT_EXCLUDED_PATHS = [
    r"C:\Windows",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
    r"C:\ProgramData",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\Users\Default",
    os.path.join(os.environ.get("USERPROFILE", ""), "AppData"),
]

# 검색 결과 최대 건수. None이면 제한 없음 (mft_cache.search의 limit 기본값 적용).
MAX_SEARCH_RESULTS = None


# ── 앱 디렉터리 초기화 ───────────────────────────────────────────────────────

def ensure_app_dir():
    """APP_DIR(%LOCALAPPDATA%/SeekSeek)이 없으면 생성한다."""
    os.makedirs(APP_DIR, exist_ok=True)


# ── 설정 파일 저수준 I/O ──────────────────────────────────────────────────────

def _load_settings() -> dict:
    """settings.json 전체를 읽어 dict로 반환한다. 없거나 오류 시 빈 dict."""
    ensure_app_dir()
    if not os.path.isfile(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("설정 파일 읽기 실패, 기본값 사용: %s", SETTINGS_PATH)
        return {}


def _update_settings(**kwargs) -> None:
    """settings.json의 지정 키를 갱신하고 나머지 키는 보존한다.

    전체 파일을 읽은 뒤 kwargs로 업데이트하고 다시 씀으로써,
    한 함수에서 한 키만 저장해도 다른 키가 지워지지 않는다.
    """
    ensure_app_dir()
    data = _load_settings()
    data.update(kwargs)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 사용자 설정 저장/로드 ─────────────────────────────────────────────────────

def load_excluded_paths() -> list[str]:
    """제외할 절대 경로 목록을 반환한다. 저장된 값이 없으면 DEFAULT_EXCLUDED_PATHS 반환."""
    return _load_settings().get("excluded_paths", DEFAULT_EXCLUDED_PATHS[:])


def load_excluded_dirs() -> set[str]:
    """이름 기반 제외 폴더 집합을 반환한다.

    저장된 값이 없으면 WELL_KNOWN_EXCLUDED_DIRS 중 기본 활성화 항목의 집합을 반환.
    """
    defaults = {name for name, _, on in WELL_KNOWN_EXCLUDED_DIRS if on}
    saved = _load_settings().get("excluded_dirs")
    return set(saved) if saved is not None else defaults


def save_excluded_dirs(dirs: set[str]) -> None:
    """이름 기반 제외 폴더 집합을 settings.json에 저장한다."""
    _update_settings(excluded_dirs=sorted(dirs))


def save_excluded_paths(paths: list[str]) -> None:
    """제외할 절대 경로 목록을 settings.json에 저장한다."""
    _update_settings(excluded_paths=paths)
