"""파일 목록 인메모리 인덱스 (Everything 방식)

MFT 열거 결과를 메모리에 상주시키고 파일명 검색을 순수 메모리에서 수행한다.
앱 재시작 시 DB(file_cache 테이블)에서 빠르게 복원한다.

갱신 경로:
  populate(entries)       — MFT 전체 스캔 완료 후 전체 교체
  add_or_update(...)      — USN 모니터가 개별 파일 추가/수정 시
  remove_by_ref(file_ref) — USN 모니터가 파일 삭제 시

내부 구조:
  _by_ref  : dict[file_ref → row_tuple]  — O(1) 조회/갱신/삭제
  _by_path : dict[path.lower() → file_ref] — 경로 역방향 인덱스 (중복 경로 제거용)

캐시 항목 튜플: (file_ref, path, name, extension, size, modified, is_dir, name_lower)

검색 문법 (name_query):
  - 일반 문자열  : 대소문자 무관 파일명 부분 일치
  - *            : 0개 이상의 임의 문자 (글로브 와일드카드)
  - ?            : 정확히 1개의 임의 문자
  - 여러 단어(공백): 모두 포함 (AND)
  예) *.mp3       → 이름이 .mp3로 끝나는 파일
      report??.docx → report + 임의 2글자 + .docx
      my doc       → "my" 와 "doc" 모두 포함
"""
import os
import re
import sqlite3
import threading
import logging

from core.indexer import (save_file_cache, load_file_cache,
                          save_file_cache_usn)

logger = logging.getLogger(__name__)

# 기본 저장소: file_ref → (file_ref, path, name, extension, size, modified, is_dir, name_lower)
_by_ref:  dict[int, tuple] = {}
# 역방향 인덱스: path.lower() → file_ref  (경로 중복 제거 + O(1) 경로 조회)
_by_path: dict[str, int]   = {}
_lock = threading.RLock()


def populate(entries) -> None:
    """MFT 전체 스캔 결과(MftFileEntry 리스트)로 인덱스를 전체 교체한다."""
    global _by_ref, _by_path
    by_ref:  dict[int, tuple] = {}
    by_path: dict[str, int]   = {}
    for e in entries:
        if not e.full_path or not e.name:
            continue
        if e.name.startswith('$'):
            continue
        ext = os.path.splitext(e.name)[1].lower()
        row = (e.file_ref, e.full_path, e.name, ext,
               e.size, e.modified, getattr(e, 'is_dir', False), e.name.lower())
        by_ref[e.file_ref]           = row
        by_path[e.full_path.lower()] = e.file_ref
    with _lock:
        _by_ref  = by_ref
        _by_path = by_path
    logger.info("파일 인덱스 갱신: %d개 (파일+폴더)", len(by_ref))


def add_or_update(file_ref: int, path: str, name: str,
                  size: int, modified: float, is_dir: bool = False) -> None:
    """USN 변경으로 추가/수정된 파일을 인덱스에 반영한다. O(1)."""
    if name.startswith('$'):
        return
    ext = os.path.splitext(name)[1].lower()
    row = (file_ref, path, name, ext, size, modified, is_dir, name.lower())
    path_lower = path.lower()
    with _lock:
        # 같은 file_ref 의 기존 경로가 바뀐 경우 → 구 경로 역인덱스 제거
        old_row = _by_ref.get(file_ref)
        if old_row and old_row[1].lower() != path_lower:
            _by_path.pop(old_row[1].lower(), None)
        # 같은 경로를 가진 다른 file_ref 가 있으면 → 구 항목 제거
        old_ref = _by_path.get(path_lower)
        if old_ref is not None and old_ref != file_ref:
            _by_ref.pop(old_ref, None)
        _by_ref[file_ref]    = row
        _by_path[path_lower] = file_ref


def remove_by_ref(file_ref: int) -> None:
    """USN 삭제 이벤트로 파일을 인덱스에서 제거한다. O(1)."""
    with _lock:
        old_row = _by_ref.pop(file_ref, None)
        if old_row:
            _by_path.pop(old_row[1].lower(), None)


def count() -> int:
    with _lock:
        return len(_by_ref)


def remove_excluded(exclude_fn) -> int:
    """로드된 캐시에서 exclude_fn(path)==True 인 항목을 제거한다.

    file_cache DB 로드 후 제외 경로 설정을 소급 적용할 때 사용한다.
    Returns: 제거된 항목 수
    """
    with _lock:
        to_remove = [ref for ref, row in _by_ref.items() if exclude_fn(row[1])]
        for ref in to_remove:
            _by_path.pop(_by_ref.pop(ref)[1].lower(), None)
    return len(to_remove)


def _compile_pattern(query: str) -> re.Pattern | None:
    """와일드카드(* ?)가 있으면 글로브 패턴으로 컴파일. 없으면 None 반환."""
    if "*" not in query and "?" not in query:
        return None
    pattern = "^" + re.escape(query).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.compile(pattern, re.IGNORECASE)


def search(name_query: str = "", limit: int = 10_000_000) -> list[tuple]:
    """이름으로 인덱스를 검색한다.

    name_query: 검색어 (와일드카드 * ? 지원, 공백은 AND 조건)

    Returns: list of (file_ref, path, name, extension, size, modified, is_dir)
    """
    if not name_query:
        with _lock:
            return list(_by_ref.values())[:limit]

    tokens = name_query.split()
    matchers: list[re.Pattern | str] = [
        (_compile_pattern(t) or t.lower()) for t in tokens
    ]

    results = []
    for row in _by_ref.values():
        name_lower = row[7]
        if all(
            m.fullmatch(name_lower) if isinstance(m, re.Pattern) else m in name_lower
            for m in matchers
        ):
            results.append(row)
            if len(results) >= limit:
                break
    return results


# ── DB 영속화 브릿지 ──────────────────────────────────────────────────────────

def save_to_db(conn: sqlite3.Connection) -> None:
    """현재 인메모리 캐시를 file_cache 테이블에 저장한다.

    동시에 각 드라이브의 현재 USN 상태를 file_cache_usn에 기록하여
    다음 앱 시작 시 누락 변경분을 자동 보충할 수 있게 한다.
    """
    with _lock:
        snapshot = list(_by_ref.values())
    save_file_cache(conn, snapshot)
    _save_cache_usn_snapshot(conn)


def _save_cache_usn_snapshot(conn: sqlite3.Connection) -> None:
    """현재 usn_state의 각 드라이브 상태를 file_cache_usn에 복사한다."""
    rows = conn.execute("SELECT drive, journal_id, next_usn FROM usn_state").fetchall()
    for drive, journal_id, next_usn in rows:
        save_file_cache_usn(conn, drive, journal_id, next_usn)
    if rows:
        logger.info("file_cache_usn 저장: %s", {r[0]: r[2] for r in rows})


def load_from_db(conn: sqlite3.Connection) -> bool:
    """file_cache 테이블에서 캐시를 로드하여 인메모리 인덱스를 채운다.

    Returns:
        True  — 데이터가 있어서 캐시가 채워졌음
        False — 테이블이 비어 있음 (MFT 열거 필요)
    """
    global _by_ref, _by_path
    rows = load_file_cache(conn)
    if not rows:
        return False
    by_ref:  dict[int, tuple] = {}
    by_path: dict[str, int]   = {}
    for fref, path, name, ext, size, modified, is_dir in rows:
        row = (fref, path, name, ext, size, modified, is_dir, name.lower())
        by_ref[fref]          = row
        by_path[path.lower()] = fref
    with _lock:
        _by_ref  = by_ref
        _by_path = by_path
    logger.info("file_cache에서 로드 완료: %d개", len(by_ref))
    return True
