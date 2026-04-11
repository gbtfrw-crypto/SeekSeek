"""파일 시스템 스캐너

QThread 기반 백그라운드 스캐너. 관리자 권한으로만 실행된다.

■ 스레드 아키텍처
  SeekSeek는 4가지 QThread 워커를 사용한다:

  1) ScannerThread — MFT 전체 스캔 / 캐시 초기화
     - cache_only=False: MFT 전체 열거 → mft_cache + file_cache DB 구축
     - cache_only=True : file_cache DB 로드 → mft_cache 채우기 (앱 시작 시 빠른 경로)

  2) USNMonitorThread — 증분 변경 폴링
     - 5초 간격으로 FSCTL_READ_USN_JOURNAL 호출
     - 변경된 파일을 mft_cache에만 동기화 (DB files/file_contents 는 건드리지 않음)
     - 0.5초 단위 msleep으로 stop 요청 응답성 확보

  3) ContentReindexThread — 변경 파일 재색인
     - ThreadPoolExecutor(4~12 workers)로 텍스트 추출을 병렬화
     - DB 쓰기는 단일 커넥션으로 순차 처리 (SQLite single-writer 제약)
     - 1000개 청크 단위로 체크→추출→벌크쓰기 반복

  4) FolderIndexThread — 폴더 전체 색인
     - os.walk로 대상 파일 수집
     - ContentReindexThread.run()을 직접 호출하여 로직 재사용

■ 스레드 간 통신
  QThread → MainWindow: pyqtSignal (progress, finished_signal, error_signal)
  MainWindow → QThread: request_stop() 메서드 호출
  → pyqtSignal은 크로스-스레드 시그널을 Qt 이벤트 큐를 통해 자동 마샬링하므로
    GUI 스레드에서 별도 락 없이 안전하게 수신·업데이트 가능

■ 제외 경로 아키텍처
  _ExclusionMixin: 제외 경로 로딩 + 판별 로직을 ScannerThread / USNMonitorThread가
  공유할 수 있도록 믹스인으로 추출. 각 워커 __init__에서 _load_exclusions() 1회 호출.
  FolderIndexThread: _ExclusionMixin 없이 run() 내에서 모듈 레벨 _should_exclude()를
  직접 호출 (짧게 실행되는 워커이므로 인스턴스 상태 불필요).
"""
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial

from PyQt6.QtCore import QThread, pyqtSignal

import config
from core.indexer import (get_connection, bulk_upsert_files, bulk_upsert_contents,
                          save_usn_state, load_usn_state, load_file_cache_usn)
from core.extractor import extract_text
from core import mft_cache
from core.mft_scanner import (get_ntfs_drives, enumerate_mft,
                               read_usn_changes, resolve_paths_by_refs,
                               USN_REASON_FILE_DELETE)

logger = logging.getLogger(__name__)

# 배치 처리 크기. SQLite 변수 바인딩 제한(999) 이하로 유지.
_BATCH_SIZE = 999
# 추출 결과를 소량 단위로 DB에 즉시 반영하여 텍스트 누적 메모리를 제한.
_RESULT_FLUSH_SIZE = 32


# ── 제외 경로 로딩 헬퍼 ───────────────────────────────────────────────────────

def _load_excluded_paths_normalized() -> set[str]:
    """사용자 설정에서 제외 경로를 로드하고 소문자·정규화된 set으로 반환한다.

    os.path.normpath: 슬래시 방향 통일, 중복 슬래시 제거
    .lower(): 대소문자 무관 비교를 위한 정규화
    """
    return {os.path.normpath(p).lower() for p in config.load_excluded_paths()}


def _load_excluded_dirs() -> set[str]:
    """사용자 설정에서 이름 기반 제외 폴더 set을 로드한다.

    절대 경로가 아닌 폴더명 단위 제외 (예: 'node_modules', '__pycache__').
    경로의 어느 깊이에 있어도 일치하면 제외된다.
    """
    return config.load_excluded_dirs()


def _should_exclude(filepath: str,
                    excluded_paths: set[str],
                    excluded_dirs: set[str]) -> bool:
    """파일/디렉터리가 제외 대상인지 확인한다.

    ■ 제외 조건 (OR, 하나라도 해당하면 True)
      1. 파일명이 '~$'로 시작 — Office 임시 잠금 파일
      2. 절대 경로가 excluded_paths 중 하나의 하위 경로 또는 동일 경로
      3. 경로 컴포넌트 중 하나가 excluded_dirs에 포함
      4. 경로 컴포넌트 중 하나가 '.'으로 시작 — 숨김 폴더
      5. 경로 컴포넌트 중 하나가 '$'으로 시작 — Windows 시스템 메타데이터 폴더

    Args:
        filepath:       검사할 파일 또는 디렉터리의 절대 경로.
        excluded_paths: 제외할 절대 경로 집합 (소문자 정규화됨).
        excluded_dirs:  제외할 폴더명 집합 (대소문자 구분).

    Returns:
        True이면 제외 대상.
    """
    filename = os.path.basename(filepath)
    # 조건 1: Office 임시 파일 (~$로 시작)
    if filename.startswith("~$"):
        return True
    # 조건 2: 절대 경로 기반 제외
    normed = os.path.normpath(filepath).lower()
    for exc in excluded_paths:
        if normed.startswith(exc + os.sep) or normed == exc:
            return True
    # 조건 3~5: 경로 컴포넌트 이름 기반 제외
    parts = filepath.replace("/", "\\").split("\\")
    for part in parts:
        if part in excluded_dirs or part.startswith(".") or part.startswith("$"):
            return True
    return False


# ── 제외 경로 공유 믹스인 ──────────────────────────────────────────────────────

class _ExclusionMixin:
    """ScannerThread / USNMonitorThread 의 공통 제외 경로 로딩·판별 믹스인.

    두 스레드 모두 __init__에서 동일하게 excluded_paths + excluded_dirs를 로드하고,
    _should_exclude(filepath)를 단일 인자로 호출하는 패턴을 공유한다.
    이 믹스인으로 중복 코드를 제거하고, 향후 제외 로직 변경을 한 곳에서 관리한다.
    """

    def _load_exclusions(self) -> None:
        """제외 경로·폴더를 설정에서 로드하여 인스턴스 변수에 저장한다."""
        self._excluded_paths: set[str] = _load_excluded_paths_normalized()
        self._excluded_dirs:  set[str] = _load_excluded_dirs()

    def _should_exclude(self, filepath: str) -> bool:
        """filepath가 현재 인스턴스의 제외 규칙에 해당하는지 반환한다."""
        return _should_exclude(filepath, self._excluded_paths, self._excluded_dirs)


# ── MFT 스캔 스레드 ───────────────────────────────────────────────────────────

class ScannerThread(_ExclusionMixin, QThread):
    """백그라운드 파일 스캔 스레드.

    두 가지 모드로 동작한다:
      cache_only=True  — 앱 시작 시 빠른 캐시 초기화. file_cache DB 로드 → MFT 열거 폴백
      cache_only=False — 전체 MFT 재스캔. mft_cache + file_cache DB 전체 갱신

    Signals:
        progress(msg, count)              — 진행 상황 (UI 상태 표시용)
        finished_signal(total, content_count) — 완료 (전체 파일 수, 내용 색인 수)
        error_signal(msg)                 — 치명적 오류 발생
        mode_signal(mode_name)            — 실제 사용된 스캔 모드 이름
    """

    progress        = pyqtSignal(str, int)   # (현재 경로/상태 메시지, 처리된 파일 수)
    finished_signal = pyqtSignal(int, int)   # (전체 파일 수, 내용 색인 수)
    error_signal    = pyqtSignal(str)        # 오류 메시지
    mode_signal     = pyqtSignal(str)        # 스캔 모드 이름

    def __init__(self, scan_paths: list[str] | None = None,
                 cache_only: bool = False,
                 parent=None):
        """
        Args:
            scan_paths: 드라이브 루트 목록 (예: ["C:\\", "D:\\"]). None이면 자동 감지.
            cache_only: True이면 file_cache DB 로드(또는 MFT 열거)만 수행.
                        앱 시작 시 파일 목록 캐시를 빠르게 채우는 용도.
        """
        super().__init__(parent)
        self.scan_paths      = scan_paths
        self._cache_only     = cache_only
        self._stop_requested = False
        self._load_exclusions()  # _ExclusionMixin: 제외 경로 로딩

    # ── 외부 제어 API ──────────────────────────────────────────────────────────

    def request_stop(self):
        """스캔 중지를 요청한다. 플래그만 설정하며 즉시 중단되지는 않는다."""
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
        캐시에는 반영되지 않은 구간, 또는 비정상 종료로 누락된 구간을 복구한다.

        ■ 보충 불가 케이스
          - file_cache_usn 레코드가 없음 (이전 앱 버전에서 저장된 캐시)
          - journal_id가 바뀜 (볼륨 재포맷 또는 저널 재생성)
          - USN 레코드가 만료됨 (변경량이 많아 저널 순환)
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

            # 현재 usn_state와 비교하여 보충 구간(cache_next_usn ~ current_next_usn) 계산
            current_state = load_usn_state(conn, drive)
            if current_state is None:
                continue

            current_journal_id, current_next_usn = current_state

            # journal_id가 다르면 볼륨이 재포맷됨 → 보충 불가
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
                # USN 만료 — 전체 재스캔 없이 보충 불가
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
        """mft_cache를 빠르게 채운다. 우선 file_cache DB를 확인하고, 없으면 MFT 열거.

        ■ 동작 흐름
          1. file_cache DB 로드 시도
             → 성공: 즉시 캐시 채우기 + _catchup_usn으로 누락 구간 보충
             → 실패(비어 있음): MFT 전체 열거 → 결과를 캐시 + DB에 저장
          2. 로드 후 _catchup_usn으로 캐시 저장 시점 이후 USN 변경분 보충
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
                # USN 상태 저장 — 이 시점 이후 변경분을 USN 폴링이 감지할 수 있게 함
                if result.journal_id:
                    save_usn_state(conn, drive, result.journal_id, result.next_usn)
            conn.commit()

        if all_entries:
            mft_cache.populate(all_entries)
            # DB에 캐시 저장 (다음 앱 시작 시 즉시 로드 가능)
            with get_connection() as conn:
                mft_cache.save_to_db(conn)
            logger.info("[cache_only] 캐시 초기화 완료: %d개", len(all_entries))
        else:
            logger.error("[cache_only] 캐시가 비어있음! MFT 실패 또는 모두 제외됨")

        total = len(all_entries)
        self.finished_signal.emit(total, 0)

    # ── MFT 전체 스캔 ─────────────────────────────────────────────────────────

    def _run_mft_scan(self):
        """NTFS MFT 전체 열거로 파일명 검색용 캐시와 file_cache DB를 구축한다.

        ■ 이 메서드가 건드리는 것
          - mft_cache (인메모리 파일명 인덱스)
          - file_cache 테이블 (앱 재시작용 영속 캐시)
          - usn_state 테이블 (USN 모니터 폴링 기준점)

        ■ 이 메서드가 건드리지 않는 것
          - files / file_contents 테이블 (본문 검색용 DB)
          → 본문 색인은 사용자가 "본문 검색 색인" 버튼으로 별도 실행
        """
        # scan_paths가 드라이브 루트 목록이면 드라이브 문자만 추출
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

        # mft_cache 전체 갱신 + file_cache 테이블 저장
        if all_entries:
            mft_cache.populate(all_entries)
            with get_connection() as conn:
                mft_cache.save_to_db(conn)

        self.finished_signal.emit(len(all_entries), 0)


# ── 백그라운드 USN 모니터 ──────────────────────────────────────────────────────

class USNMonitorThread(_ExclusionMixin, QThread):
    """백그라운드에서 USN Journal을 주기적으로 폴링하여 파일 변경을 자동 반영한다.

    ■ 업데이트 범위
      - mft_cache (파일명 검색용 인메모리 인덱스) 만 갱신
      - files / file_contents DB(본문 검색용)는 건드리지 않음
      → DB 갱신은 사용자가 "N개 변경" 버튼을 눌러 ContentReindexThread로 별도 실행

    ■ 폴링 설계
      POLL_INTERVAL 초를 0.5초 단위로 쪼개어 슬립. stop 요청 시 최대 0.5초 이내에 응답.

    Signals:
        updated(added, deleted)  — 변경 처리 완료 (추가/수정 건수, 삭제 건수)
        paths_updated(paths)     — 추가/수정된 파일 경로 목록
        needs_full_scan          — Journal 만료 등으로 전체 재스캔 필요
    """

    updated         = pyqtSignal(int, int)  # (추가/수정 건수, 삭제 건수)
    paths_updated   = pyqtSignal(list)      # 추가/수정된 파일 경로 목록
    needs_full_scan = pyqtSignal()          # 전체 재스캔 필요

    POLL_INTERVAL = 5  # 폴링 간격 (초)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_requested = False
        self._load_exclusions()  # _ExclusionMixin: 제외 경로 로딩
        self._drives = config.MFT_SCAN_DRIVES or get_ntfs_drives()

    def request_stop(self):
        """USN 모니터 중지를 요청한다."""
        self._stop_requested = True

    def run(self):
        logger.info("USN 모니터 시작 (폴링 간격: %ds)", self.POLL_INTERVAL)
        while not self._stop_requested:
            # POLL_INTERVAL 초 대기 — 0.5초 단위로 쪼개어 stop 응답성 확보
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

        ■ 처리 흐름
          1. 각 드라이브의 usn_state(journal_id, start_usn)를 DB에서 로드
          2. FSCTL_READ_USN_JOURNAL로 start_usn 이후 변경 레코드 읽기
          3. 변경 레코드를 _apply_usn_changes()로 mft_cache에 반영
          4. 새 next_usn을 DB의 usn_state에 저장 (다음 폴링 기준점 갱신)

        ■ Journal 만료 처리
          FSCTL_READ_USN_JOURNAL가 None을 반환하면 Journal이 만료(오래된 레코드 덮어쓰임)된 것.
          이 경우 증분 업데이트가 불가능하므로 needs_full_scan 시그널을 보내 전체 재스캔 요청.
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
                    # Journal 만료 또는 재생성 → 증분 업데이트 불가, 전체 재스캔 필요
                    logger.warning("USN 모니터: %s Journal 만료 → 전체 재스캔 필요", drive)
                    self.needs_full_scan.emit()
                    return

                if not changes:
                    # 변경 없음 — next_usn만 갱신하여 다음 폴링 기준점 전진
                    save_usn_state(conn, drive, journal_id, new_next_usn)
                    continue

                any_change = True
                # mft_cache만 갱신 (DB는 건드리지 않음)
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


# ── USN 변경 적용 (catchup / USN 모니터 공용) ────────────────────────────────

def _apply_usn_changes(
    changes: list,
    drive: str,
    exclude_fn,
    stop_flag=None,
) -> tuple[int, int, list[str]]:
    """USN 변경 레코드를 mft_cache에 반영한다.

    ■ last-event-wins 전략
      하나의 파일에 대해 여러 변경 이벤트가 발생할 수 있다 (생성 → 수정 → 삭제 등).
      decisions dict에 file_ref 단위로 마지막 이벤트만 기록하여
      최종 상태(삭제 or 업데이트)만 반영한다.

    ■ 경로 해석
      to_update 파일들의 file_ref → 전체 경로 변환은
      resolve_paths_by_refs()가 OpenFileById + GetFinalPathNameByHandleW로 수행.
      파일이 이미 삭제된 경우 경로를 얻지 못하므로 해당 항목은 건너뜀.

    Args:
        changes:   UsnChange 리스트 (mft_scanner.read_usn_changes 반환값).
        drive:     드라이브 문자 ('C' 등).
        exclude_fn: 경로 제외 판별 함수 (filepath: str) -> bool.
        stop_flag:  중단 요청 람다. 참이면 업데이트 루프 조기 종료.

    Returns:
        (added, deleted, changed_paths)
        - added:         mft_cache에 추가/수정된 항목 수
        - deleted:       mft_cache에서 삭제된 항목 수
        - changed_paths: 추가/수정된 파일 경로 목록
    """
    # 같은 파일에 대한 여러 이벤트 중 마지막 이벤트만 채택 (last-event-wins)
    decisions: dict[int, str] = {}
    for ch in changes:
        if ch.reason & USN_REASON_FILE_DELETE:
            decisions[ch.file_ref] = "delete"
        else:
            decisions[ch.file_ref] = "update"

    raw_refs = {ch.file_ref: ch.raw_file_ref for ch in changes if ch.raw_file_ref}
    to_delete = {ref for ref, d in decisions.items() if d == "delete"}
    to_update = {ref for ref, d in decisions.items() if d == "update"}

    # 삭제된 파일 먼저 캐시에서 제거
    deleted = 0
    for ref in to_delete:
        mft_cache.remove_by_ref(ref)
        deleted += 1

    # 추가/수정된 파일: 경로를 OpenFileById로 해석한 뒤 캐시에 반영
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
                # 경로는 얻었지만 stat 실패 (삭제 경합 등) — 크기 0으로 등록
                mft_cache.add_or_update(ref, path, os.path.basename(path), 0, 0)
            added += 1
            changed_paths.append(path)

    return added, deleted, changed_paths


# ── 스레드 풀 워커 (모듈 수준) ────────────────────────────────────────────────

def _extract_for_path(path: str) -> str | None:
    """스레드 풀 워커: 파일 텍스트 추출 (크기·확장자 필터 포함).

    ThreadPoolExecutor에서 병렬로 실행된다.
    CONTENT_EXTENSIONS에 없는 파일, 비어있거나 너무 큰 파일은 None을 반환한다.
    """
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

    ■ 처리 흐름 (1000개 청크 단위 반복)
      1. 체크 단계: DB에서 현재 파일의 mtime + has_content 일괄 조회
         → mtime이 동일하고 has_content=1인 파일은 스킵
      2. 추출 단계: ThreadPoolExecutor로 텍스트 추출 병렬화 (4~12 workers)
      3. 쓰기 단계: bulk_upsert_files + bulk_upsert_contents로 DB 순차 일괄 쓰기

    ■ SQLite 단일 쓰기 제약
      SQLite는 동시 쓰기를 지원하지 않으므로, 추출은 병렬이지만 쓰기는 단일 커넥션.
      WAL 모드로 읽기와 쓰기는 동시 가능.

    Signals:
        progress(path, count)  — 처리 중인 파일 경로와 누적 수
        finished_signal(count) — 완료 (색인된 파일 수)
        total_count(n)         — 처리 시작 전 전체 파일 수
    """

    progress        = pyqtSignal(str, int)
    finished_signal = pyqtSignal(int)
    total_count     = pyqtSignal(int)

    def __init__(self, file_paths: list[str], parent=None):
        super().__init__(parent)
        self._paths = file_paths

    def run(self):
        paths = self._paths
        self.total_count.emit(len(paths))
        indexed   = 0
        processed = 0
        cpu = os.cpu_count() or 4
        max_workers = max(2, min(cpu, 4))

        logger.info("[reindex] 시작: 전체 %d개, workers=%d", len(paths), max_workers)

        def _flush_results(conn, rows: list[tuple[str, str | None]]) -> int:
            if not rows:
                return 0
            path_to_id = bulk_upsert_files(conn, [p for p, _ in rows])
            content_rows = [
                (path_to_id[p], t)
                for p, t in rows
                if t and p in path_to_id
            ]
            cnt = bulk_upsert_contents(conn, content_rows)
            rows.clear()
            return cnt

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for chunk_start in range(0, len(paths), _BATCH_SIZE):
                chunk = paths[chunk_start: chunk_start + _BATCH_SIZE]

                # ── 1단계: 배치 DB 체크 (IN절 1번 쿼리로 mtime+has_content 일괄 조회) ──
                try:
                    with get_connection() as conn:
                        placeholders = ",".join("?" * len(chunk))
                        rows = conn.execute(
                            f"SELECT path, modified, has_content FROM files"
                            f" WHERE path IN ({placeholders})", chunk
                        ).fetchall()
                    existing = {r[0]: (r[1], r[2]) for r in rows}
                except Exception:
                    logger.exception("[reindex] 체크 단계 예외 (chunk %d)", chunk_start)
                    existing = {}

                # DB에 없거나 mtime 변경된 파일만 추출 대상으로 선별
                needs_extract: list[str] = []
                for path in chunk:
                    try:
                        mtime = os.stat(path).st_mtime
                    except OSError:
                        continue
                    if path not in existing:
                        needs_extract.append(path)
                    else:
                        old_mtime, has_content = existing[path]
                        if not has_content or abs(old_mtime - mtime) >= 0.001:
                            needs_extract.append(path)

                logger.info("[reindex] 청크 %d~%d: 추출 대상 %d / %d개",
                            chunk_start, chunk_start + len(chunk),
                            len(needs_extract), len(chunk))

                if not needs_extract:
                    processed += len(chunk)
                    continue

                # ── 2단계+3단계: 병렬 텍스트 추출 + 소배치 즉시 DB 반영 ─────────
                try:
                    with get_connection() as conn:
                        results_buffer: list[tuple[str, str | None]] = []
                        future_map = {pool.submit(_extract_for_path, p): p
                                      for p in needs_extract}
                        for i, fut in enumerate(as_completed(future_map)):
                            path = future_map[fut]
                            self.progress.emit(path, processed + i + 1)
                            try:
                                text = fut.result()
                            except Exception:
                                text = None
                            results_buffer.append((path, text))

                            if len(results_buffer) >= _RESULT_FLUSH_SIZE:
                                cnt = _flush_results(conn, results_buffer)
                                indexed += cnt
                                conn.commit()

                        cnt = _flush_results(conn, results_buffer)
                        indexed += cnt
                        conn.commit()
                        logger.info("[reindex] 청크 commit: 누적 %d개", indexed)
                except Exception:
                    logger.exception("[reindex] DB 쓰기 예외 (chunk %d)", chunk_start)

                processed += len(chunk)

        logger.info("[reindex] 완료: indexed=%d / total=%d", indexed, len(paths))
        self.finished_signal.emit(indexed)


# ── 폴더 전체 색인 스레드 ─────────────────────────────────────────────────────

class FolderIndexThread(QThread):
    """체크된 폴더 하위 파일을 병렬로 내용 색인한다.

    ■ 처리 흐름
      1. os.walk로 폴더 하위 파일을 수집 (제외 규칙 적용)
      2. ContentReindexThread.run()을 직접 호출하여 색인 로직 재사용

    ■ 제외 적용 방식
      _should_exclude()를 호출하여 폴더/파일 단위 제외를 처리한다.
      - 루트 디렉터리가 제외 대상이면 dirnames[:] = []로 하위 탐색 중단
      - 서브디렉터리는 _should_exclude()로 필터링 후 dirs[:] 재할당
      - 개별 파일도 _should_exclude()로 확인

    Signals:
        progress(path, count)  — 처리 중인 파일 경로와 누적 수
        finished_signal(count) — 완료 (색인된 파일 수)
        total_count(n)         — 처리 시작 전 전체 파일 수
    """

    progress        = pyqtSignal(str, int)
    finished_signal = pyqtSignal(int)
    total_count     = pyqtSignal(int)

    def __init__(self, folder_paths: list[str], parent=None):
        super().__init__(parent)
        self._folders = folder_paths

    def run(self):
        # 매 실행마다 최신 설정 로드 (앱 실행 중 설정 변경 반영)
        excluded_paths = _load_excluded_paths_normalized()
        excluded_dirs  = _load_excluded_dirs()
        targets: list[str] = []

        for folder in self._folders:
            for root, dirs, files in os.walk(folder):
                # 현재 디렉터리 자체가 제외 대상이면 하위 탐색 전체 중단
                if _should_exclude(root, excluded_paths, excluded_dirs):
                    dirs[:] = []
                    continue
                # 서브디렉터리 목록에서 제외 대상 제거 (os.walk가 해당 디렉터리로 진입하지 않음)
                dirs[:] = [d for d in dirs
                           if not _should_exclude(os.path.join(root, d),
                                                   excluded_paths, excluded_dirs)]
                # 파일 필터: 제외 대상 제거 + 지원 확장자만 선택
                for name in files:
                    fpath = os.path.join(root, name)
                    if _should_exclude(fpath, excluded_paths, excluded_dirs):
                        continue
                    if os.path.splitext(name)[1].lower() in config.CONTENT_EXTENSIONS:
                        targets.append(fpath)

        logger.info("FolderIndexThread: %d개 파일 대상", len(targets))

        # ContentReindexThread의 run()을 직접 호출하여 청크 처리 로직 재사용
        # (.start()가 아닌 .run() 직접 호출 — 새 QThread를 생성하지 않고 현재 스레드에서 실행)
        reindex = ContentReindexThread(targets)
        reindex.total_count.connect(self.total_count)
        reindex.progress.connect(self.progress)
        reindex.finished_signal.connect(self.finished_signal)
        reindex.run()
