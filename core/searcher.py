"""검색 엔진 모듈

■ 이중 검색 모드
  1) MFT 캐시 모드 (content_query 없음)
     - mft_cache 인메모리 인덱스에서 파일명 검색 (O(N) 순차 검색, ~1M 파일에 <50ms)
     - 와일드카드(*, ?) / 복수 토큰 AND 조건 지원

  2) DB FTS5 모드 (content_query 있음)
     - fts_files 가상 테이블: 파일명 FTS5 MATCH 검색
     - fts_contents 가상 테이블: 본문 FTS5 MATCH 검색
     - 두 결과를 path 기준으로 병합 → match_type "both"/"filename"/"content"

■ BM25 랭킹
  FTS5의 rank 컨럼은 bm25() 함수의 반환값(음수)이다.
  BM25(Best Matching 25) 공식:
    score(D, Q) = Σ IDF(qi) × (f(qi, D) × (k1 + 1)) / (f(qi, D) + k1 × (1 - b + b × |D|/avgdl))
  여기서:
    - IDF(qi) = log((N - n(qi) + 0.5) / (n(qi) + 0.5))  (역문서빈도)
    - f(qi, D) = 문서 D에서 토큰 qi의 출현 횟수
    - k1 = 1.2 (TF 포화 파라미터), b = 0.75 (문서 길이 정규화)
    - |D| = 문서 길이, avgdl = 평균 문서 길이
  → ORDER BY rank 로 관련성 높은 순서 정렬 (rank는 음수이므로 ASC 정렬)
"""
import os
import re
import sqlite3
import time as _time
import logging
from dataclasses import dataclass

import config
from core.indexer import get_connection
from core import mft_cache

logger = logging.getLogger(__name__)

# FTS5 네이티브 연산자 패턴.
# 사용자가 이미 FTS5 확장 문법을 입력한 경우(_build_fts_query에서 자동 변환하지 않고)
# 원문을 그대로 MATCH 우변으로 전달한다.
# 지원 문법: AND, OR, NOT, NEAR(토큰, 범위), "..."구문 검색, ^초기 토큰
_FTS5_NATIVE = re.compile(r'\b(AND|OR|NOT)\b|NEAR\s*\(|"|\^', re.IGNORECASE)


@dataclass
class SearchResult:
    """검색 결과 하나.

    match_type 유효값:
        "filename" — 파일명 매칭
        "content"  — 본문 FTS5 매칭
        "both"     — 파일명 + 본문 모두 매칭
    """
    file_id:    int
    path:       str
    name:       str
    extension:  str
    size:       int
    modified:   float
    match_type: str
    is_dir:     bool = False


# ── 공개 API ─────────────────────────────────────────────────────────────────

def search(filename_query: str = "", content_query: str = "",
           folder_paths: list[str] | None = None,
           max_results: int | None = None) -> list[SearchResult]:
    """파일명, 본문으로 검색 후 결과를 병합한다.

    content_query가 있으면 DB 검색 모드:
      - 파일명도 DB fts_files에서 검색
      - folder_paths로 검색 범위 제한
    content_query가 없으면 MFT 캐시 검색 모드:
      - 파일명을 인메모리 캐시에서 검색
    """
    _t = {}
    _t['start'] = _time.perf_counter()

    if not filename_query and not content_query:
        return []

    limit = max_results or config.MAX_SEARCH_RESULTS or 10_000_000

    results_map: dict[str, SearchResult] = {}

    if content_query:
        # ── DB 검색 모드 ──────────────────────────────────────────────────
        fts_ct = _build_fts_query(content_query)
        logger.debug("🔬 FTS query: %r", fts_ct)
        _t['fts_build'] = _time.perf_counter()
        with get_connection() as conn:
            if filename_query:
                fn_fts = _build_fts_query(filename_query)
                if fn_fts:
                    _search_db_filenames(conn, fn_fts, folder_paths, results_map, limit)
            _t['db_filename'] = _time.perf_counter()
            if fts_ct:
                _search_contents(conn, fts_ct, content_query,
                                 folder_paths, results_map, limit)
            _t['db_content'] = _time.perf_counter()

        # 파일명+본문 교집합: "both"만 남김
        if filename_query and content_query:
            results_map = {k: v for k, v in results_map.items()
                           if v.match_type == "both"}
        _t['filter'] = _time.perf_counter()

    else:
        # ── MFT 캐시 검색 모드 ───────────────────────────────────────────
        rows = mft_cache.search(name_query=filename_query, limit=limit)
        _t['cache_search'] = _time.perf_counter()
        for fref, path, name, ext, size, modified, is_dir, *_ in rows:
            results_map[path] = SearchResult(
                file_id=fref, path=path, name=name, extension=ext or "",
                size=size or 0, modified=modified or 0,
                match_type="filename", is_dir=is_dir,
            )
        _t['cache_build'] = _time.perf_counter()

    # 정렬: both > filename > content
    priority = {"both": 0, "filename": 1, "content": 2}
    _t['pre_sort'] = _time.perf_counter()
    results = sorted(
        results_map.values(),
        key=lambda r: (priority.get(r.match_type, 9), not r.is_dir, r.name.lower()),
    )
    _t['sorted'] = _time.perf_counter()
    final = results[:limit]
    _t['end'] = _time.perf_counter()

    s = _t['start']
    parts = []
    prev = s
    for label in ['fts_build', 'db_filename', 'db_content', 'filter',
                  'cache_search', 'cache_build', 'pre_sort', 'sorted', 'end']:
        if label in _t:
            parts.append(f"{label}={(_t[label]-prev)*1000:.1f}ms")
            prev = _t[label]
    logger.debug(" search() 내부 │ total=%.1fms │ results_map=%d │ final=%d │ %s",
                 (_t['end'] - s) * 1000, len(results_map), len(final), " → ".join(parts))
    return final


def match_label(match_type: str) -> str:
    """match_type 값을 UI 표시용 레이블 문자열로 변환한다."""
    return {
        "filename": "파일명",
        "content":  "본문",
        "both":     "파일명+본문",
    }.get(match_type, match_type)


# ── 내부 검색 함수 ────────────────────────────────────────────────────────────

def _build_fts_query(query: str) -> str:
    """사용자 입력을 FTS5 쿼리 문자열로 변환한다.

    ■ 변환 규칙
      1) 네이티브 FTS5 연산자(AND/OR/NOT/NEAR/""/^)가 있으면
         사용자가 의도한 FTS 문법으로 간주하고 그대로 반환
      2) 일반 텍스트는 공백 토큰 단위로 정리 후 "토큰"* 형태(prefix 검색)로 변환
         텍스트에서 영숫자·점(._-)·한글만 유지해 FTS5 특수문자 문제 방지

    ■ 예시
      "main.py"   → '"main.py"*'         (접두 일치)
      "my doc"    → '"my"* "doc"*'       (두 토큰 모두 prefix 일치)
      "A AND B"   → 'A AND B'            (네이티브 그대로 전달)
    """
    q = query.strip()
    if not q:
        return ""
    if _FTS5_NATIVE.search(q):
        return q
    parts = []
    for w in q.split():
        clean = "".join(c for c in w.rstrip('*') if c.isalnum() or c in "._-가-힣")
        if clean:
            parts.append(f'"{clean}"*')
    return " ".join(parts)


def _folder_clause(folder_paths: list[str] | None) -> tuple[str, list]:
    """폴더 필터 SQL 조건과 파라미터를 반환한다.

    ■ SQL Injection 방지
      f.path LIKE ? ESCAPE '\\' 패턴을 사용해 경로 접두 매칭을 수행.
      LIKE 메타문자(%, _, \)를 이스케이프하여 사용자 경로가
      SQL 패턴으로 해석되지 않게 한다.
      파라미터 바인딩(?) 사용으로 SQL Injection 원천 차단.
    """
    if not folder_paths:
        return "", []
    placeholders = " OR ".join("f.path LIKE ? ESCAPE '\\'" for _ in folder_paths)
    clause = f" AND ({placeholders})"
    params = []
    for p in folder_paths:
        norm = os.path.normpath(p)
        escaped = (norm
                   .replace("\\", "\\\\")
                   .replace("%", "\\%")
                   .replace("_", "\\_"))
        params.append(escaped + "\\\\" + "%")
    return clause, params


def _search_db_filenames(conn: sqlite3.Connection, fts_query: str,
                          folder_paths: list[str] | None,
                          results_map: dict, limit: int):
    """DB fts_files에서 파일명 검색. folder_paths로 범위 제한."""
    fold_clause, fold_params = _folder_clause(folder_paths)
    sql = f"""
        SELECT f.id, f.path, f.name, f.extension, f.size, f.modified
        FROM fts_files ft
        JOIN files f ON f.id = ft.rowid
        WHERE fts_files MATCH ?{fold_clause}
        ORDER BY rank
        LIMIT ?
    """
    try:
        for fid, path, name, ext, size, modified in conn.execute(
                sql, [fts_query] + fold_params + [limit]):
            results_map[path] = SearchResult(
                file_id=fid, path=path, name=name, extension=ext or "",
                size=size or 0, modified=modified or 0,
                match_type="filename",
            )
    except sqlite3.OperationalError as e:
        logger.debug("DB 파일명 검색 오류: %s", e)


def _search_contents(conn: sqlite3.Connection, fts_query: str,
                     raw_query: str,
                     folder_paths: list[str] | None,
                     results_map: dict, limit: int):
    """본문 FTS5 검색 (fts_contents 가상 테이블).

    이미 results_map 에 있는 파일이면 match_type 을 "both" 로 승격한다.
    """
    fold_clause, fold_params = _folder_clause(folder_paths)
    sql = f"""
        SELECT fc.file_id, f.path, f.name, f.extension, f.size, f.modified
        FROM fts_contents ft
        JOIN file_contents fc ON fc.id = ft.rowid
        JOIN files f ON f.id = fc.file_id
        WHERE fts_contents MATCH ?{fold_clause}
        ORDER BY rank
        LIMIT ?
    """
    # 결과 개수 상한(limit)은 DB 단계에서 1차 제한하고,
    # 최종 병합 후 search()에서 다시 한 번 슬라이스한다.
    params = [fts_query] + fold_params + [limit]
    logger.debug(" 본문검색 SQL │ MATCH=%r │ params=%s", fts_query, params)
    try:
        rows_count = 0
        for fid, path, name, ext, size, modified in conn.execute(sql, params):
            rows_count += 1
            if path in results_map:
                results_map[path].match_type = "both"
            else:
                results_map[path] = SearchResult(
                    file_id=fid, path=path, name=name, extension=ext or "",
                    size=size or 0, modified=modified or 0,
                    match_type="content",
                )
        logger.debug("본문검색 결과: %d건", rows_count)
    except sqlite3.OperationalError as e:
        logger.debug("본문 FTS 검색 오류: %s", e)
