"""SQLite FTS5 기반 파일 인덱서

files 테이블(파일 메타데이터)과 file_contents 테이블(추출 텍스트)을 관리하며,
각각에 연결된 FTS5 가상 테이블(fts_files, fts_contents)을 트리거로 동기화한다.
■ FTS5(Full-Text Search 5) 역색인 개념
  역색인(inverted index)은 "단어 → 해당 단어를 포함하는 문서 ID 목록" 매핑이다.
  예: "python" → [doc1, doc3, doc7]  /  "search" → [doc2, doc3, doc5]
  이 구조로 MATCH 쿼리 시 O(N) 전체 스캔 대신 O(log N) 이하로 단어 위치를 찾는다.

  FTS5 내부 구조 (segment b-tree):
  ─ %_data 테이블: 항포스팅 리스트(doclist) + 위치 정보(poslist) 저장
  ─ %_idx  테이블: 항(term) → segment 내 위치 b-tree 인덱스
  ─ doclist: 각 term별로 [rowid1, rowid2, ...] 리스트 저장
  ─ poslist: doclist 내 각 rowid별 토큰 위치 [토큰오프셋1, 토큰오프셋2, ...]
  ─ merge: 새 데이터 INSERT 시 새 segment 생성 → automerge로 자동 병합

■ External Content 모드
  "content=files"로 선언하면 FTS5는 원본 데이터를 저장하지 않고
  files 테이블을 원본으로 참조한다. 단, 자동 동기화가 없으므로
  INSERT/DELETE/UPDATE 트리거로 직접 동기화해야 한다.
  → 저장 공간을 절반 이하로 줄이는 대신 DELETE 시 원본 텍스트를 알아야 함

■ 트리거 동기화 원리
  FTS5 external content 테이블의 DELETE는 특수 INSERT로 수행:
    INSERT INTO fts_table(fts_table, rowid, col1) VALUES('delete', old_id, old_text);
  이는 FTS5에게 "이 rowid의 역색인 엔트리를 제거하라"는 특수 명령이다.
  UPDATE는 기존 삭제 + 새로 삽입으로 두 단계로 처리.
DB 스키마 요약:
    files          — 경로·이름·확장자·크기·수정일·file_ref
    file_contents  — file_id 외래키 + 추출 텍스트 blob
    fts_files      — FTS5 가상 테이블 (files 기반, 트리거 동기화)
    fts_contents   — FTS5 가상 테이블 (file_contents 기반, 트리거 동기화)
    usn_state      — 드라이브별 USN Journal 증분 상태 (journal_id, next_usn)
"""
import os
import sqlite3
import logging
from contextlib import contextmanager

import config

logger = logging.getLogger(__name__)


@contextmanager
def get_connection():
    """SQLite 커넥션 컨텍스트 매니저.

    WAL 모드 + NORMAL 동기화로 읽기/쓰기 병행 성능을 높인다.
    busy_timeout 30초로 다중 스레드 충돌을 완화한다.
    """
    config.ensure_app_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """테이블, 인덱스, FTS5 가상 테이블, 트리거를 생성한다.

    이미 존재하는 오브젝트는 IF NOT EXISTS 로 건너뛰므로 여러 번 호출해도 안전하다.
    기존 DB에 file_ref 컬럼이 없으면 ALTER TABLE 로 마이그레이션한다.
    """
    config.ensure_app_dir()
    with get_connection() as conn:
        cur = conn.cursor()

        # ── 메인 파일 테이블 ────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                path        TEXT UNIQUE NOT NULL,  -- 절대 경로 (고유 키)
                name        TEXT NOT NULL,          -- 파일명 (basename)
                extension   TEXT,                   -- 소문자 확장자 (.py 등)
                size        INTEGER,                -- 바이트 크기
                modified    REAL,                   -- mtime (Unix timestamp)
                has_content INTEGER DEFAULT 0,      -- 내용 인덱싱 여부
                file_ref    INTEGER                 -- NTFS MFT 참조 번호
            )
        """)

        # file_ref 컬럼 마이그레이션 (구버전 DB 호환)
        cur.execute("PRAGMA table_info(files)")
        if "file_ref" not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE files ADD COLUMN file_ref INTEGER")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_file_ref ON files(file_ref)"
        )

        # ── USN Journal 증분 상태 테이블 ────────────────────────────────────
        # 드라이브별로 한 행씩 저장. 다음 증분 스캔의 시작점으로 사용.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usn_state (
                drive      TEXT PRIMARY KEY,
                journal_id INTEGER NOT NULL,
                next_usn   INTEGER NOT NULL
            )
        """)

        # ── 파일명 FTS5 가상 테이블 ─────────────────────────────────────────
        # content=files: external content 모드 — FTS5가 원본 데이터를 저장하지 않고
        #   files 테이블을 소스로 참조 (저장 공간 절반 이하로 감소)
        # content_rowid=id: files.id를 FTS5 테이블의 rowid로 매핑
        # name, path 컨럼은 FTS5 역색인에 등록되어 MATCH 쿼리 대상이 됨
        cur.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_files
            USING fts5(name, path, content=files, content_rowid=id)
        """)

        # ── 본문 저장 테이블 ─────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS file_contents (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                content TEXT  -- 추출된 전체 텍스트
            )
        """)

        # ── 본문 FTS5 가상 테이블 ───────────────────────────────────────────
        cur.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_contents
            USING fts5(content, content=file_contents, content_rowid=id)
        """)

        # ── 트리거: files ↔ fts_files 자동 동기화 ──────────────────────────
        # FTS5 external content 모드는 자동 동기화가 없으므로 트리거로 직접 처리.
        #
        # INSERT 트리거: 새 행을 FTS5 역색인에 등록
        # DELETE 트리거: FTS5 특수 DELETE 구문으로 역색인에서 제거
        #   → INSERT INTO fts_table(fts_table, rowid, col) VALUES('delete', old.id, old.text)
        #   첫 번째 인자 'delete'는 FTS5 특수 명령(컨텐츠 삭제)을 나타냄
        # UPDATE 트리거: 구 값 DELETE + 신 값 INSERT → 2단계 처리
        cur.executescript("""
            CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
                INSERT INTO fts_files(rowid, name, path)
                VALUES (new.id, new.name, new.path);
            END;

            CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
                INSERT INTO fts_files(fts_files, rowid, name, path)
                VALUES ('delete', old.id, old.name, old.path);
            END;

            CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
                INSERT INTO fts_files(fts_files, rowid, name, path)
                VALUES ('delete', old.id, old.name, old.path);
                INSERT INTO fts_files(rowid, name, path)
                VALUES (new.id, new.name, new.path);
            END;

            CREATE TRIGGER IF NOT EXISTS fc_ai AFTER INSERT ON file_contents BEGIN
                INSERT INTO fts_contents(rowid, content)
                VALUES (new.id, new.content); 
            END;

            CREATE TRIGGER IF NOT EXISTS fc_ad AFTER DELETE ON file_contents BEGIN
                INSERT INTO fts_contents(fts_contents, rowid, content)
                VALUES ('delete', old.id, old.content);
            END;
        """)
 
        # ── 색인 폴더 테이블 ─────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS indexed_folders (
                path       TEXT PRIMARY KEY,  -- 사용자가 등록한 색인 대상 폴더
                indexed_at REAL               -- 마지막 색인 완료 시각 (Unix timestamp), NULL이면 미색인
            )
        """)

        # 스키마 마이그레이션: indexed_at 칼럼이 없는 구버전 호환
        cur.execute("PRAGMA table_info(indexed_folders)")
        cols = {r[1] for r in cur.fetchall()}
        if "indexed_at" not in cols:
            cur.execute("ALTER TABLE indexed_folders ADD COLUMN indexed_at REAL")

        # ── MFT 캐시 테이블 (앱 재시작 시 빠른 복원용) ─────────────────────
        # 스키마 마이그레이션: 구버전(file_ref PK) → 신버전(drive+file_ref 복합 PK)
        cur.execute("SELECT sql FROM sqlite_master WHERE name='file_cache'")
        row = cur.fetchone()
        if row and 'drive' not in row[0]:
            cur.execute("DROP TABLE file_cache")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS file_cache (
                drive     TEXT NOT NULL,
                file_ref  INTEGER NOT NULL,
                path      TEXT NOT NULL,
                name      TEXT NOT NULL,
                extension TEXT,
                size      INTEGER DEFAULT 0,
                modified  REAL DEFAULT 0,
                is_dir    INTEGER DEFAULT 0,
                PRIMARY KEY (drive, file_ref)
            )
        """)

        # ── 캐시 저장 시점의 USN 상태 (앱 재시작 시 누락 구간 보충용) ────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS file_cache_usn (
                drive      TEXT PRIMARY KEY,
                journal_id INTEGER NOT NULL,
                next_usn   INTEGER NOT NULL
            )
        """)

        conn.commit()
    logger.info("DB 초기화 완료: %s", config.DB_PATH)


def upsert_file(conn: sqlite3.Connection, filepath: str,
                file_ref: int | None = None,
                size: int | None = None,
                modified: float | None = None) -> int | None:
    """파일 메타데이터를 files 테이블에 삽입하거나 갱신한다.

    size/modified 를 전달하면 os.stat() 를 호출하지 않는다 (이미 stat 한 경우).
    mtime 이 이전과 동일하면 DB를 변경하지 않고 기존 file_id를 반환한다.

    Returns:
        file_id (int) — 성공
        None          — os.stat() 실패 (파일 접근 불가)
    """
    if size is None or modified is None:
        try:
            stat = os.stat(filepath)
        except OSError:
            return None
        size     = stat.st_size
        modified = stat.st_mtime

    name = os.path.basename(filepath)
    ext  = os.path.splitext(name)[1].lower()

    cur = conn.cursor()
    cur.execute("SELECT id, modified FROM files WHERE path = ?", (filepath,))
    row = cur.fetchone()

    if row:
        file_id, old_mod = row
        if abs(old_mod - modified) < 0.001:
            # 수정일 변화 없음 — file_ref 만 업데이트하고 반환
            if file_ref is not None:
                cur.execute(
                    "UPDATE files SET file_ref=? WHERE id=?", (file_ref, file_id)
                )
            logger.debug("[upsert_file] SKIP (mtime 동일) id=%s %s", file_id, filepath)
            return file_id
        # 수정일 변경 — 메타데이터 전체 갱신
        cur.execute(
            "UPDATE files SET name=?, extension=?, size=?, modified=?, file_ref=? WHERE id=?",
            (name, ext, size, modified, file_ref, file_id),
        )
        logger.debug("[upsert_file] UPDATE id=%s %s", file_id, filepath)
    else:
        cur.execute(
            "INSERT INTO files (path, name, extension, size, modified, file_ref)"
            " VALUES (?,?,?,?,?,?)",
            (filepath, name, ext, size, modified, file_ref),
        )
        file_id = cur.lastrowid
        logger.debug("[upsert_file] INSERT id=%s %s", file_id, filepath)
    return file_id


def needs_content_update(conn: sqlite3.Connection, filepath: str) -> bool:
    """추출·색인이 필요한 파일인지 판단한다 (추출 전 사전 체크용).

    - DB에 없으면 True (새 파일)
    - has_content=0 이면 True (아직 미색인)
    - has_content=1 이고 mtime 변경 시 True (파일 수정됨)
    - has_content=1 이고 mtime 동일 시 False (스킵)
    """
    try:
        mtime = os.stat(filepath).st_mtime
    except OSError:
        return False
    row = conn.execute(
        "SELECT modified, has_content FROM files WHERE path = ?", (filepath,)
    ).fetchone()
    if row is None:
        return True
    old_mtime, has_content = row
    if not has_content:
        return True
    return abs(old_mtime - mtime) >= 0.001


def upsert_content(conn: sqlite3.Connection, file_id: int, text: str):
    """파일 추출 텍스트를 file_contents 테이블에 삽입하거나 교체한다.

    기존 행을 DELETE 후 INSERT 하는 방식으로 FTS5 트리거가 정상 동작하게 한다.
    """
    # 서로게이트 문자(잘못된 유니코드)가 포함되면 SQLite INSERT 시 UnicodeEncodeError 발생.
    # encode→decode로 안전하게 제거한다.
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    preview = text[:100].replace("\n", "↵")
    logger.debug("[upsert_content] file_id=%s text_len=%d preview=%r", file_id, len(text), preview)
    cur = conn.cursor()
    cur.execute("SELECT id FROM file_contents WHERE file_id = ?", (file_id,))
    row = cur.fetchone()
    if row:
        # DELETE 트리거로 기존 FTS 인덱스 항목 제거
        logger.debug("[upsert_content] DELETE 기존 id=%s", row[0])
        cur.execute("DELETE FROM file_contents WHERE id = ?", (row[0],))
    cur.execute(
        "INSERT INTO file_contents (file_id, content) VALUES (?, ?)",
        (file_id, text),
    )
    logger.debug("[upsert_content] INSERT 완료 file_id=%s lastrowid=%s", file_id, cur.lastrowid)
    conn.execute("UPDATE files SET has_content = 1 WHERE id = ?", (file_id,))


def bulk_upsert_files(conn: sqlite3.Connection,
                      paths: list[str]) -> dict[str, int]:
    """파일 목록을 일괄로 files 테이블에 삽입/갱신하고 path→id 맵을 반환한다.

    - 신규 파일: INSERT OR IGNORE
    - mtime 변경된 파일: UPDATE
    - mtime 동일한 파일: 스킵
    - os.stat() 실패한 파일: 스킵 (반환 맵에 포함 안 됨)
    """
    # 1. os.stat 수집 (접근 불가 파일 제외)
    stat_map: dict[str, tuple[str, str, int, float]] = {}  # path → (name, ext, size, mtime)
    for path in paths:
        try:
            st = os.stat(path)
            name = os.path.basename(path)
            ext  = os.path.splitext(name)[1].lower()
            stat_map[path] = (name, ext, st.st_size, st.st_mtime)
        except OSError:
            pass

    if not stat_map:
        return {}

    valid_paths = list(stat_map.keys())

    # 2. 현재 DB 상태 일괄 조회 (청크 999개 이하로 분할 — SQLite 변수 제한)
    existing: dict[str, tuple[int, float]] = {}  # path → (id, modified)
    chunk_size = 990
    for i in range(0, len(valid_paths), chunk_size):
        chunk = valid_paths[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, path, modified FROM files WHERE path IN ({placeholders})", chunk
        ).fetchall()
        for file_id, path, modified in rows:
            existing[path] = (file_id, modified)

    # 3. 신규 파일 일괄 INSERT
    new_rows = [
        (path, name, ext, size, mtime, None)
        for path, (name, ext, size, mtime) in stat_map.items()
        if path not in existing
    ]
    if new_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO files (path, name, extension, size, modified, file_ref)"
            " VALUES (?,?,?,?,?,?)",
            new_rows,
        )
        logger.debug("[bulk_upsert_files] INSERT %d개", len(new_rows))

    # 4. mtime 변경된 파일 일괄 UPDATE
    update_rows = [
        (name, ext, size, mtime, path)
        for path, (name, ext, size, mtime) in stat_map.items()
        if path in existing and abs(existing[path][1] - mtime) >= 0.001
    ]
    if update_rows:
        conn.executemany(
            "UPDATE files SET name=?, extension=?, size=?, modified=? WHERE path=?",
            update_rows,
        )
        logger.debug("[bulk_upsert_files] UPDATE %d개", len(update_rows))

    # 5. 삽입/수정된 파일의 id 회수
    path_to_id: dict[str, int] = {p: eid for p, (eid, _) in existing.items()}
    inserted_paths = [row[0] for row in new_rows]
    for i in range(0, len(inserted_paths), chunk_size):
        chunk = inserted_paths[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, path FROM files WHERE path IN ({placeholders})", chunk
        ).fetchall()
        for file_id, path in rows:
            path_to_id[path] = file_id

    return path_to_id


def bulk_upsert_contents(conn: sqlite3.Connection,
                         content_rows: list[tuple[int, str]]) -> int:
    """(file_id, text) 리스트를 file_contents에 일괄 삽입한다.

    기존 콘텐츠가 있으면 삭제 후 삽입 (FTS5 트리거 동기화 유지).
    반환값: 실제 삽입된 행 수
    """
    if not content_rows:
        return 0

    # 서로게이트 문자 정리
    cleaned = [
        (fid, text.encode("utf-8", errors="replace").decode("utf-8"))
        for fid, text in content_rows
    ]

    file_ids = [fid for fid, _ in cleaned]

    # 1. 기존 콘텐츠 일괄 삭제 (fc_ad 트리거 → FTS5 동기화)
    conn.executemany(
        "DELETE FROM file_contents WHERE file_id=?",
        [(fid,) for fid in file_ids],
    )

    # 2. 새 콘텐츠 일괄 INSERT (fc_ai 트리거 → FTS5 동기화)
    conn.executemany(
        "INSERT INTO file_contents (file_id, content) VALUES (?,?)",
        cleaned,
    )

    # 3. has_content 플래그 일괄 업데이트
    conn.executemany(
        "UPDATE files SET has_content=1 WHERE id=?",
        [(fid,) for fid in file_ids],
    )

    logger.debug("[bulk_upsert_contents] %d개 삽입 완료", len(cleaned))
    return len(cleaned)


def get_stats(conn: sqlite3.Connection) -> dict:
    """인덱스 통계(전체 파일 수, 내용 인덱싱 수)를 반환한다."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), (SELECT COUNT(*) FROM file_contents) FROM files")
    total, with_content = cur.fetchone()
    return {"total_files": total or 0, "indexed_contents": with_content or 0}


# ── USN Journal 증분 상태 관리 ────────────────────────────────────────────────

def save_usn_state(conn: sqlite3.Connection, drive: str,
                   journal_id: int, next_usn: int):
    """드라이브별 USN Journal 상태를 저장 (다음 증분 스캔 기준점)."""
    conn.execute(
        "INSERT OR REPLACE INTO usn_state (drive, journal_id, next_usn)"
        " VALUES (?, ?, ?)",
        (drive.upper(), journal_id, next_usn),
    )


def load_usn_state(conn: sqlite3.Connection,
                   drive: str) -> tuple[int, int] | None:
    """드라이브별 USN 상태를 로드한다. 저장된 상태가 없으면 None 반환."""
    row = conn.execute(
        "SELECT journal_id, next_usn FROM usn_state WHERE drive = ?",
        (drive.upper(),),
    ).fetchone()
    return (row[0], row[1]) if row else None


# ── 색인 폴더 관리 ────────────────────────────────────────────────────────────

def add_indexed_folder(conn: sqlite3.Connection, path: str):
    """색인 대상 폴더를 등록한다."""
    norm = os.path.normpath(path)
    conn.execute(
        "INSERT OR IGNORE INTO indexed_folders (path) VALUES (?)", (norm,)
    )


def remove_indexed_folder(conn: sqlite3.Connection, path: str):
    """색인 대상 폴더를 삭제하고 해당 폴더 하위 파일의 DB 데이터도 제거한다.

    file_contents 는 REFERENCES files(id) ON DELETE CASCADE 로 자동 삭제된다.
    """
    norm = os.path.normpath(path)
    # LIKE 특수문자 이스케이프 후 폴더 하위 경로 일괄 삭제
    escaped = (norm
               .replace("\\", "\\\\")
               .replace("%", "\\%")
               .replace("_", "\\_"))
    like_pattern = escaped + "\\\\" + "%"
    conn.execute(
        "DELETE FROM files WHERE path LIKE ? ESCAPE '\\'",
        (like_pattern,),
    )
    conn.execute("DELETE FROM indexed_folders WHERE path = ?", (norm,))


def get_indexed_folders(conn: sqlite3.Connection) -> list[str]:
    """등록된 색인 폴더 목록을 반환한다."""
    rows = conn.execute(
        "SELECT path FROM indexed_folders ORDER BY path"
    ).fetchall()
    return [r[0] for r in rows]


def get_indexed_folders_with_status(conn: sqlite3.Connection) -> list[tuple[str, float | None]]:
    """등록된 색인 폴더와 색인 완료 시각을 반환한다.

    Returns:
        [(path, indexed_at), ...] — indexed_at이 None이면 미색인
    """
    rows = conn.execute(
        "SELECT path, indexed_at FROM indexed_folders ORDER BY path"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def update_indexed_at(conn: sqlite3.Connection, path: str, timestamp: float):
    """폴더의 색인 완료 시각을 갱신한다."""
    norm = os.path.normpath(path)
    conn.execute(
        "UPDATE indexed_folders SET indexed_at = ? WHERE path = ?",
        (timestamp, norm),
    )


def get_file_content_by_path(conn: sqlite3.Connection, path: str) -> str | None:
    """파일 경로로 색인된 전체 텍스트를 반환한다. 없으면 None."""
    row = conn.execute(
        "SELECT fc.content FROM file_contents fc"
        " JOIN files f ON f.id = fc.file_id"
        " WHERE f.path = ?",
        (path,),
    ).fetchone()
    return row[0] if row else None


# ── MFT 캐시 영속화 ──────────────────────────────────────────────────────────

def save_file_cache(conn: sqlite3.Connection,
                    rows: list[tuple]) -> None:
    """인메모리 캐시를 file_cache 테이블에 일괄 저장한다.

    Args:
        rows: [(file_ref, path, name, extension, size, modified, is_dir), ...]
    """
    conn.execute("DELETE FROM file_cache")
    db_rows = []
    for fref, path, name, ext, size, modified, is_dir, *_ in rows:
        drive = path[0].upper() if len(path) >= 2 and path[1] == ':' else '?'
        db_rows.append((drive, fref, path, name, ext, size, modified, is_dir))
    conn.executemany(
        "INSERT INTO file_cache (drive, file_ref, path, name, extension, size, modified, is_dir)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        db_rows,
    )
    conn.commit()
    logger.info("file_cache 저장 완료: %d건", len(rows))


def load_file_cache(conn: sqlite3.Connection) -> list[tuple]:
    """file_cache 테이블에서 캐시 데이터를 로드한다.

    Returns:
        [(file_ref, path, name, extension, size, modified, is_dir), ...]
        테이블이 비어 있으면 빈 리스트.
    """
    rows = conn.execute(
        "SELECT file_ref, path, name, extension, size, modified, is_dir"
        " FROM file_cache"
    ).fetchall()
    return rows  # drive는 인메모리 캐시에 불필요 → SELECT에서 제외


def save_file_cache_usn(conn: sqlite3.Connection,
                        drive: str, journal_id: int, next_usn: int) -> None:
    """캐시 저장 시점의 USN 상태를 기록한다."""
    conn.execute(
        "INSERT OR REPLACE INTO file_cache_usn (drive, journal_id, next_usn)"
        " VALUES (?, ?, ?)",
        (drive.upper(), journal_id, next_usn),
    )


def load_file_cache_usn(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """캐시 저장 시점의 USN 상태를 로드한다.

    Returns:
        {drive: (journal_id, next_usn)} — 없으면 빈 딕셔너리.
    """
    rows = conn.execute(
        "SELECT drive, journal_id, next_usn FROM file_cache_usn"
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


