"""SeekSeek - 전역 설정

앱 전반에서 참조하는 경로, 상수, 사용자 설정 로드/저장 함수를 정의한다.
"""
import os
import json
import logging

logger = logging.getLogger(__name__)

# ── 앱 데이터 경로 ──────────────────────────────────────────────────────────
APP_DIR       = os.path.join(os.environ.get("LOCALAPPDATA", "."), "SeekSeek")
DB_PATH       = os.path.join(APP_DIR, "index.db")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

# ── 스캔 기본 설정 ──────────────────────────────────────────────────────────
MFT_SCAN_DRIVES: list[str] = []

# ── 본문 인덱싱 설정 ─────────────────────────────────────────────────────────
CONTENT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h",
    ".cs", ".html", ".css", ".xml", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".log", ".csv", ".bat", ".ps1", ".sh",
    ".sql", ".r", ".go", ".rs", ".kt", ".swift", ".rb", ".php",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".hwpx", ".hwp",
}

MAX_CONTENT_SIZE = 200 * 1024 * 1024
MAX_PREVIEW_SIZE = 100 * 1024

# ── 이름 기반 제외 폴더 설정 ─────────────────────────────────────────────────
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

EXCLUDED_DIRS: set[str] = {
    name for name, _, default in WELL_KNOWN_EXCLUDED_DIRS if default
}

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

MAX_SEARCH_RESULTS = None


# ── 앱 디렉터리 초기화 ───────────────────────────────────────────────────────

def ensure_app_dir():
    os.makedirs(APP_DIR, exist_ok=True)


# ── 설정 파일 저수준 I/O ──────────────────────────────────────────────────────

def _load_settings() -> dict:
    """settings.json 전체를 읽어 dict로 반환. 없거나 오류 시 빈 dict."""
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
    """settings.json의 지정 키를 갱신하고 나머지 키는 보존한다."""
    ensure_app_dir()
    data = _load_settings()
    data.update(kwargs)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 사용자 설정 저장/로드 ─────────────────────────────────────────────────────

def load_excluded_paths() -> list[str]:
    return _load_settings().get("excluded_paths", DEFAULT_EXCLUDED_PATHS[:])


def load_excluded_dirs() -> set[str]:
    defaults = {name for name, _, on in WELL_KNOWN_EXCLUDED_DIRS if on}
    saved = _load_settings().get("excluded_dirs")
    return set(saved) if saved is not None else defaults


def save_excluded_dirs(dirs: set[str]) -> None:
    _update_settings(excluded_dirs=sorted(dirs))


def save_excluded_paths(paths: list[str]) -> None:
    _update_settings(excluded_paths=paths)
