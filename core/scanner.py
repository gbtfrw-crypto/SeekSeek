"""파일 시스템 스캐너

QThread 기반 백그라운드 스캐너. 관리자 권한으로만 실행된다.

■ 스레드 아키텍처
  SeekSeek는 4가지 QThread 워커를 사용한다:

  1) ScannerThread — MFT 전체 스캔 / 캐시 초기화
     - cache_only=True : file_cache DB 로드 → MFT 캐시 채우기 (앵 시작 시)
     - cache_only=False: MFT 전체 열거 → 캐시 + DB 구축

  2) USNMonitorThread — 증분 변경 폴링
     - 5초 간격으로 FSCTL_READ_USN_JOURNAL 호출
     - 변경된 파일을 mft_cache에 동기화 (DB는 업데이트하지 않음)
     - 0.5초 단위 msleep로 중단 요청 응답성 확보

  3) ContentReindexThread — 변경 파일 재색인
     - ThreadPoolExecutor(max_workers=4~8)로 병렬 텍스트 추출
     - DB 쓰기는 단일 커넥션으로 순차 처리 (SQLite single writer)

  4) FolderIndexThread — 폴더 전체 색인
     - os.walk로 파일 수집 → ContentReindexThread 로직 재사용

■ 스레드 간 통신
  QThread → MainWindow: pyqtSignal (progress, finished_signal, error_signal)
  MainWindow → QThread: request_stop() 메서드 호출
  → pyqtSignal은 크로스 스레드 시그널을 자동으로 Qt 이벤트 큐를 통해 마샬링하므로
    GUI 스레드에서 안전하게 수신할 수 있다.

스캔 모드:
    MFT 전체 스캔  — NTFS MFT 직접 열거 (가장 빠름)
    USN 증분 스캔  — 마지막 스캔 이후 변경분만 업데이트
"""
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial

from PyQt6.QtCore import QThread, pyqtSignal
 
import config
from core.indexer import (get_connection, upsert_file, upsert_content,
                          needs_content_update,
                          save_usn_state, load_usn_state, load_file_cache_usn)
from core.extractor import extract_text
from core import mft_cache
from core.mft_scanner import (get_ntfs_drives, enumerate_mft,
                               read_usn_changes, resolve_paths_by_refs,
                               USN_REASON_FILE_DELETE)

logger = logging.getLogger(__name__)

# 배치 커밋 간격 (파일 N개마다 conn.commit())
_COMMIT_INTERVAL_MFT = 2000


def _load_excluded_paths_normalized() -> set[str]:
    """사용자 설정에서 제외 경로를 로드하고 소문자 정규화된 set 으로 반환한다."""
    return {os.path.normpath(p).lower() for p in config.load_excluded_paths()}


def _load_excluded_dirs() -> set[str]:
    """사용자 설정에서 이름 기반 제외 폴더 set을 로드한다."""
    return config.load_excluded_dirs()


def _should_exclude(filepath: str,
                    excluded_paths: set[str],
                    excluded_dirs: set[str]) -> bool:
    """파일/디렉터리가 제외 대상인지 확인한다.

    절대 경로 기반 제외(사용자 설정)와 이름 기반 제외를 모두 검사한다.
    숨김 폴더('.'으로 시작), $로 시작하는 폴더, ~$로 시작하는 파일(Office 임시)도 제외한다.
    """
    filename = os.path.basename(filepath)
    if filename.startswith("~$"):
        return True
    normed = os.path.normpath(filepath).lower()
    for exc in excluded_paths:
        if normed.startswith(exc + os.sep) or normed == exc:
            return True
    parts = filepath.replace("/", "\\").split("\\")
    for part in parts:
        if part in excluded_dirs or part.startswith(".") or part.startswith("$"):
            return True
    return False


class ScannerThread(QThread):
    """백그라운드 파일 스캔 스레드.

    Signals:
        progress(msg, count)       — 진행 상황 (UI 상태 표시용)
        finished_signal(total, content_count) — 완료 (전체 파일 수, 내용 색인 수)
        error_signal(msg)          — 치명적 오류 발생
        mode_signal(mode_name)     — 실제 사용된 스캔 모드 이름
    """

    progress        = pyqtSignal(str, int)   # (현재 경로/상태, 처리된 파일 수)
    finished_signal = pyqtSignal(int, int)   # (전체 파일 수, 내용 색인 수)
    error_signal    = pyqtSignal(str)        # 오류 메시지
    mode_signal     = pyqtSignal(str)        # 스캔 모드 이름

    def __init__(self, scan_paths: list[str] | None = None,
                 cache_only: bool = False,
                 parent=None):
        """
        Args:
            scan_paths: 드라이브 루트 목록 (예: ["C:\\", "D\\"]). None이면 자동 감지.
            cache_only: True이면 file_cache DB 로드(또는 MFT 열거)만 수행.
                        앱 시작 시 파일 목록 캐시를 빠르게 채우는 용도.
        """
        super().__init__(parent)
        self.scan_paths      = scan_paths
        self._cache_only     = cache_only
        self._stop_requested = False
        self._excluded_paths = _load_excluded_paths_normalized()
        self._excluded_dirs  = _load_excluded_dirs()

    # ── 외부 제어 API ──────────────────────────────────────────────────────────

    def request_stop(self):
        """스캔 중지를 요청한다."""
        self._stop_requested = True

    # ── 스레드 진입점 ──────────────────────────────────────────────────────────

    def run(self):
        try:
            if self._cache_only:
                self.mode_signal.emit("캐시 초기화(MFT)")
                self._run_cache_only_scan()
                return

            self.mode_signal.emit("MFT")
            self._run_mft_scan()

        except Exception as e:
            logger.exception("스캔 중 예외 발생")
            self.error_signal.emit(str(e))

    # ── 캐시 로드 후 누락 USN 구간 보충 ──────────────────────────────────────────

    def _catchup_usn(self, conn, drives: list[str]):
        """file_cache 저장 시점 ~ 현재 usn_state 사이의 변경분을 보충한다.

        캐시가 DB에 저장된 후 앱이 종료되기 전까지 USN 모니터가 처리했지만
        캐시에는 반영되지 않은 구간, 또는 버그로 누락된 구간을 복구한다.
        """
        cache_usn_map = load_file_cache_usn(conn)
        if not cache_usn_map:
            logger.debug("[catchup] file_cache_usn 없음 — 보충 건너뜀")
            return

        total_added = 0
        total_deleted = 0

        for drive in drives:
            cache_state = cache_usn_map.get(drive.upper())
            if cache_state is None:
                continue

            cache_journal_id, cache_next_usn = cache_state

            # 현재 USN 상태와 비교
            current_state = load_usn_state(conn, drive)
            if current_state is None:
                continue

            current_journal_id, current_next_usn = current_state

            # journal_id 가 다르면 볼륨이 바뀐 것 → 보충 불가
            if cache_journal_id != current_journal_id:
                logger.info("[catchup] %s: journal_id 변경 → 보충 건너뜀", drive)
                continue

            # 이미 최신이면 건너뜀
            if cache_next_usn >= current_next_usn:
                logger.debug("[catchup] %s: 캐시가 최신 (usn=%d)", drive, cache_next_usn)
                continue

            logger.info("[catchup] %s: USN 갭 보충 %d → %d",
                        drive, cache_next_usn, current_next_usn)

            changes, _ = read_usn_changes(drive, cache_next_usn, cache_journal_id)
            if changes is None:
                logger.warning("[catchup] %s: USN 만료 → 보충 불가", drive)
                continue
            if not changes:
                continue

            added, deleted, _ = _apply_usn_changes(
                changes, drive, self._should_exclude)
            total_added   += added
            total_deleted += deleted

        if total_added or total_deleted:
            logger.info("[catchup] 보충 완료: +%d / -%d (캐시=%d)",
                        total_added, total_deleted, mft_cache.count())
            self.progress.emit(
                f"누락 보충: +{total_added} / -{total_deleted}", mft_cache.count(),
            )

    # ── 캐시 전용 빠른 MFT 열거 (앱 시작용) ──────────────────────────────────────

    def _run_cache_only_scan(self):
        """MFT 열거만 수행하여 파일 목록 캐시를 채운다.

        DB에 file_cache가 있으면 즉시 로드하고 MFT 열거를 건너뛴다.
        없으면 MFT를 열거한 뒤 DB에 저장한다.
        """
        drives = config.MFT_SCAN_DRIVES or get_ntfs_drives()
        logger.info("[cache_only] 스캔 드라이브: %s", drives)
        if not drives:
            logger.warning("cache_only: NTFS 드라이브 없음")
            return

        with get_connection() as conn:
            # ── DB 캐시 우선 로드 시도 ─────────────────────────────
            if mft_cache.load_from_db(conn):
                removed = mft_cache.remove_excluded(self._should_exclude)
                total = mft_cache.count()
                logger.info("[cache_only] DB 캐시에서 즉시 로드: %d개 (제외 적용: -%d)", total, removed)
                self.progress.emit("파일 목록 캐시 로드 완료", total)
                # 캐시 저장 시점 이후 누락된 USN 변경분 보충
                self._catchup_usn(conn, drives)
                total = mft_cache.count()
                self.finished_signal.emit(total, 0)
                return

            # ── DB에 캐시 없음 → MFT 열거 ──────────────────────────
            all_entries = []
            for drive in drives:
                if self._stop_requested:
                    break
                self.progress.emit(f"파일 목록 로드: {drive}:\\", len(all_entries))
                result = enumerate_mft(drive, exclude_fn=self._should_exclude)
                if not result.success:
                    logger.warning("[cache_only] MFT 실패 %s: %s", drive, result.error)
                    continue
                logger.info("[cache_only] %s:\\ MFT 열거 완료: %d개 (제외 적용됨)",
                            drive, len(result.files))
                for entry in result.files:
                    if self._stop_requested:
                        break
                    if entry.full_path and entry.name:
                        all_entries.append(entry)
                # USN 상태 저장 — 폴링이 이 지점부터 변경을 감지할 수 있게 함
                if result.journal_id:
                    save_usn_state(conn, drive, result.journal_id, result.next_usn)
            conn.commit()

        if all_entries:
            mft_cache.populate(all_entries)
            # DB에 캐시 저장 (다음 시작 시 즉시 로드용)
            with get_connection() as conn:
                mft_cache.save_to_db(conn)
            logger.info("[cache_only] 캐시 초기화 완료: %d개", len(all_entries))
        else:
            logger.error("[cache_only] 캐시가 비어있음! MFT 실패 또는 모두 제외됨")

        total = len(all_entries)
        self.finished_signal.emit(total, 0)

    # ── MFT 전체 스캔 ─────────────────────────────────────────────────────────

    def _run_mft_scan(self):
        """NTFS MFT 전체 열거로 파일명 검색용 캐시(mft_cache + file_cache 테이블)를 구축한다.

        files/file_contents 테이블(내용 검색용)은 여기서 건드리지 않는다.
        내용 색인은 사용자가 "N개 변경" 버튼 혹은 "선택 폴더 색인" 버튼으로 별도 실행한다.
        """
        # scan_paths 가 드라이브 루트 목록이면 그 문자만 추출, 아니면 자동 감지
        if self.scan_paths:
            drives = [
                p[0].upper() for p in self.scan_paths
                if len(p) >= 2 and p[1] == ":"
            ] or (config.MFT_SCAN_DRIVES or get_ntfs_drives())
        else:
            drives = config.MFT_SCAN_DRIVES or get_ntfs_drives()
        if not drives:
            self.error_signal.emit("NTFS 드라이브를 찾을 수 없습니다")
            return

        all_entries = []  # 파일명 검색용 인메모리 캐시 구축에 사용

        with get_connection() as conn:
            for drive in drives:
                if self._stop_requested:
                    break

                self.progress.emit(f"{drive}:\\ 파일명 검색 스캔 중…", 0)
                result = enumerate_mft(
                    drive,
                    progress_callback=partial(self.progress.emit, f"{drive}:\\ 파일명 검색 스캔 중…"),
                    exclude_fn=self._should_exclude,
                )

                if not result.success:
                    logger.error("파일 목록읽기 실패 %s:\\ (%s)", drive, result.error)
                    self.progress.emit(f"{drive}:\\ MFT 실패: {result.error}", 0)
                    continue

                self.progress.emit(
                    f"{drive}:\\ 파일 목록읽기 완료 {result.total_entries:,}개",
                    result.total_entries,
                )

                for entry in result.files:
                    if self._stop_requested:
                        break
                    if entry.full_path and entry.name:
                        all_entries.append(entry)

                # USN 상태 저장 (USN 모니터 폴링 기준점)
                if result.success and result.journal_id:
                    save_usn_state(conn, drive, result.journal_id, result.next_usn)

            conn.commit()

        # mft_cache 갱신 + file_cache 테이블 저장 (파일명 검색용)
        if all_entries:
            mft_cache.populate(all_entries)
            with get_connection() as conn:
                mft_cache.save_to_db(conn)

        self.finished_signal.emit(len(all_entries), 0)

    # ── 제외 경로 판별 ────────────────────────────────────────────────────────

    def _should_exclude(self, filepath: str) -> bool:
        return _should_exclude(filepath, self._excluded_paths, self._excluded_dirs)


# ── 백그라운드 USN 모니터 ──────────────────────────────────────────────────────

class USNMonitorThread(QThread):
    """백그라운드에서 USN Journal을 주기적으로 폴링하여 파일 변경을 자동 반영한다.

    Signals:
        updated(added, deleted) — 변경 처리 완료 (추가/수정 건수, 삭제 건수)
        needs_full_scan         — Journal 만료 등으로 전체 재스캔 필요
    """

    updated         = pyqtSignal(int, int)  # (추가/수정 건수, 삭제 건수)
    paths_updated   = pyqtSignal(list)      # 추가/수정된 파일 경로 목록
    needs_full_scan = pyqtSignal()          # 전체 재스캔 필요

    # 폴링 간격 (초)
    POLL_INTERVAL = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_requested = False
        self._excluded_paths = _load_excluded_paths_normalized()
        self._excluded_dirs  = _load_excluded_dirs()
        self._drives = config.MFT_SCAN_DRIVES or get_ntfs_drives()

    def request_stop(self):
        self._stop_requested = True

    def run(self):
        logger.info("USN 모니터 시작 (폴링 간격: %ds)", self.POLL_INTERVAL)
        while not self._stop_requested:
            # POLL_INTERVAL 초 대기 (0.5초 단위로 끊어 stop 응답성 확보)
            for _ in range(self.POLL_INTERVAL * 2):
                if self._stop_requested:
                    return
                self.msleep(500)

            try:
                self._poll()
            except Exception:
                logger.exception("USN 모니터 폴링 중 예외")

        logger.info("USN 모니터 종료")

    def _poll(self):
        """USN Journal에서 변경분을 읽어 파일명 검색용 캐시(mft_cache)를 갱신한다.

        files/file_contents 테이블(내용 검색용)은 건드리지 않는다.
        """
        drives = self._drives
        total_updated = 0
        total_deleted = 0
        any_change    = False
        changed_paths: list[str] = []

        with get_connection() as conn:
            for drive in drives:
                if self._stop_requested:
                    break

                state = load_usn_state(conn, drive)
                if state is None:
                    continue  # 이 드라이브는 아직 전체 스캔 미완료

                journal_id, start_usn = state
                changes, new_next_usn = read_usn_changes(drive, start_usn, journal_id)

                if changes is None:
                    # Journal 만료 또는 재생성 → 전체 재스캔 필요
                    logger.warning("USN 모니터: %s Journal 만료 → 전체 재스캔 필요", drive)
                    self.needs_full_scan.emit()
                    return

                if not changes:
                    # 변경 없음 — USN 상태만 갱신
                    save_usn_state(conn, drive, journal_id, new_next_usn)
                    continue

                any_change = True
                # 파일명 검색용 캐시만 갱신 (내용 검색 DB는 "N개 변경" 버튼으로만 갱신)
                added, deleted, paths = _apply_usn_changes(
                    changes, drive, self._should_exclude,
                    stop_flag=lambda: self._stop_requested,
                )
                total_updated += added
                total_deleted += deleted
                changed_paths.extend(paths)

                save_usn_state(conn, drive, journal_id, new_next_usn)

            conn.commit()

        if any_change:
            self.updated.emit(total_updated, total_deleted)
            if changed_paths:
                self.paths_updated.emit(changed_paths)

    def _should_exclude(self, filepath: str) -> bool:
        return _should_exclude(filepath, self._excluded_paths, self._excluded_dirs)


# ── USN 변경 적용 (catchup/모니터 공용) ──────────────────────────────────────

def _apply_usn_changes(
    changes: list,
    drive: str,
    exclude_fn,
    stop_flag=None,
) -> tuple[int, int, list[str]]:
    """USN 변경 레코드를 mft_cache에 반영한다.

    ■ last-event-wins 전략
      하나의 파일에 대해 여러 변경 이벤트가 있을 수 있다 (e.g. 생성 → 수정 → 삭제).
      decisions dict에 file_ref 단위로 마지막 이벤트만 기록하여
      최종 상태(삭제/업데이트)만 반영한다.
    ■ 경로 해석
      to_update 파일들의 file_ref → 전체 경로 변환은
      resolve_paths_by_refs()가 OpenFileById + GetFinalPathNameByHandleW 로 수행.

    Args:
        changes:   UsnChange 리스트
        drive:     드라이브 문자 ('C' 등)
        exclude_fn: 경로 제외 판별 함수
        stop_flag: 중단 요청 관련 플래그. 참이면 동기화 갱신 루프 조기 종료.

    Returns:
        (added, deleted, changed_paths)
    """
    decisions: dict[int, str] = {}
    for ch in changes:
        if ch.reason & USN_REASON_FILE_DELETE:
            decisions[ch.file_ref] = "delete"
        else:
            decisions[ch.file_ref] = "update"

    raw_refs = {ch.file_ref: ch.raw_file_ref for ch in changes if ch.raw_file_ref}
    to_delete = {ref for ref, d in decisions.items() if d == "delete"}
    to_update = {ref for ref, d in decisions.items() if d == "update"}

    deleted = 0
    for ref in to_delete:
        mft_cache.remove_by_ref(ref)
        deleted += 1

    added = 0
    changed_paths: list[str] = []
    if to_update:
        ref_to_path = resolve_paths_by_refs(drive, to_update, raw_refs=raw_refs)
        for ref, path in ref_to_path.items():
            if stop_flag and stop_flag():
                break
            if exclude_fn(path):
                continue
            try:
                st = os.stat(path)
                is_dir = os.path.isdir(path)
                mft_cache.add_or_update(
                    ref, path, os.path.basename(path),
                    st.st_size, st.st_mtime, is_dir,
                )
            except OSError:
                mft_cache.add_or_update(ref, path, os.path.basename(path), 0, 0)
            added += 1
            changed_paths.append(path)

    return added, deleted, changed_paths


# ── 스레드 풀 워커 (모듈 수준) ────────────────────────────────────────────────

def _extract_for_path(path: str) -> str | None:
    """스레드 풀 워커: 파일 텍스트 추출 (크기·확장자 필터 포함)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in config.CONTENT_EXTENSIONS:
        return None
    try:
        fsize = os.path.getsize(path)
    except OSError:
        return None
    if not (0 < fsize <= config.MAX_CONTENT_SIZE):
        return None
    return extract_text(path)


# ── 변경 파일 재색인 스레드 ───────────────────────────────────────────────────

class ContentReindexThread(QThread):
    """변경된 파일 목록만 내용을 재색인한다.

    ThreadPoolExecutor 로 텍스트 추출을 병렬화하고,
    DB 쓰기는 단일 커넥션으로 순차 처리한다.

    Signals:
        progress(path, count) — 처리 중인 파일 경로와 누적 수
        finished_signal(count) — 완료 (색인된 파일 수)
    """

    progress        = pyqtSignal(str, int)
    finished_signal = pyqtSignal(int)
    total_count     = pyqtSignal(int)   # 처리 시작 전 전체 파일 수

    def __init__(self, file_paths: list[str], parent=None):
        super().__init__(parent)
        self._paths = file_paths

    def run(self):
        paths = self._paths
        self.total_count.emit(len(paths))
        indexed = 0
        max_workers = min(os.cpu_count() or 4, 8)

        # 1단계: DB 체크 — has_content + mtime 비교로 추출 필요한 파일만 추림
        paths_to_extract: list[str] = []
        try:
            with get_connection() as conn:
                for path in paths:
                    if needs_content_update(conn, path):
                        paths_to_extract.append(path)
        except Exception:
            logger.exception("ContentReindexThread 체크 단계 예외")
            paths_to_extract = list(paths)

        logger.info("[reindex] 색인 대상 %d / %d개", len(paths_to_extract), len(paths))
        for p in paths_to_extract:
            logger.debug("[reindex]  %s", p)

        # 2단계: 병렬 텍스트 추출 (필요한 파일만)
        results: list[tuple[str, str | None]] = []
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_map = {pool.submit(_extract_for_path, p): p
                              for p in paths_to_extract}
                for i, fut in enumerate(as_completed(future_map)):
                    path = future_map[fut]
                    self.progress.emit(path, i + 1)
                    try:
                        text = fut.result()
                    except Exception:
                        text = None
                    results.append((path, text))
        except Exception:
            logger.exception("ContentReindexThread 추출 단계 예외")

        # 3단계: DB 쓰기 (순차)
        try:
            with get_connection() as conn:
                for i, (path, text) in enumerate(results):
                    file_id = upsert_file(conn, path)
                    if file_id is not None and text:
                        upsert_content(conn, file_id, text)
                        indexed += 1
                    if (i + 1) % _COMMIT_INTERVAL_MFT == 0:
                        conn.commit()
                conn.commit()
        except Exception:
            logger.exception("ContentReindexThread DB 쓰기 예외")

        self.finished_signal.emit(indexed)


# ── 폴더 전체 색인 스레드 ─────────────────────────────────────────────────────

class FolderIndexThread(QThread):
    """체크된 폴더 하위 파일을 병렬로 내용 색인한다.

    os.walk 로 대상 파일을 수집한 뒤 ContentReindexThread 로직을 재사용한다.

    Signals:
        progress(path, count) — 처리 중인 파일 경로와 누적 수
        finished_signal(count) — 완료 (색인된 파일 수)
    """

    progress        = pyqtSignal(str, int)
    finished_signal = pyqtSignal(int)
    total_count     = pyqtSignal(int)   # 처리 시작 전 전체 파일 수

    def __init__(self, folder_paths: list[str], parent=None):
        super().__init__(parent)
        self._folders = folder_paths

    def run(self):
        excluded_paths = _load_excluded_paths_normalized()
        excluded_dirs  = _load_excluded_dirs()
        targets: list[str] = []

        for folder in self._folders:
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs
                           if d not in excluded_dirs and not d.startswith('.')]
                if any(os.path.normpath(root).lower().startswith(ep)
                       for ep in excluded_paths):
                    continue
                for name in files:
                    if name.startswith("~$"):
                        continue
                    if os.path.splitext(name)[1].lower() in config.CONTENT_EXTENSIONS:
                        targets.append(os.path.join(root, name))

        logger.info("FolderIndexThread: %d개 파일 대상", len(targets))

        # ContentReindexThread를 같은 스레드에서 직접 실행
        # (total_count/progress/finished 시그널을 자신의 시그널로 전달)
        reindex = ContentReindexThread(targets)
        reindex.total_count.connect(self.total_count)
        reindex.progress.connect(self.progress)
        reindex.finished_signal.connect(self.finished_signal)
        reindex.run()
