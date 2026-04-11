# 5강: 스캐너 아키텍처 (Scanner Architecture)

## 개요

SeekSeek의 백그라운드 작업(MFT 스캔, USN 모니터링, 콘텐츠 인덱싱 등)은
모두 **QThread 기반 워커 스레드**로 구현되어 있다.

이 강의에서는 스캐너의 스레드 아키텍처, 시그널/슬롯 통신,
그리고 각 워커 스레드의 역할과 상호작용을 다룬다.

---

## 1. 스레드 아키텍처 전체 구조

```
┌─────────────────────────────────────────────────────────────┐
│                    GUI 메인 스레드                            │
│            (MainWindow, 이벤트 루프)                          │
│                                                             │
│  시그널 수신:                                                │
│    progress(msg, count)          → 프로그레스 바 갱신        │
│    finished_signal(total, n)     → 결과 테이블 갱신          │
│    error_signal(msg)             → 에러 표시                 │
│    updated(added, deleted)       → 캐시 갱신 후 상태 표시    │
│    paths_updated(paths)          → 변경 경로 목록 → 재검색   │
└──────────┬──────────────────────────────────────────────────┘
           │ 시그널/슬롯 연결
           │
     ┌─────┼──────────────┬──────────────────┐
     │     │              │                  │
     ▼     ▼              ▼                  ▼
┌─────────┐ ┌──────────┐ ┌────────────────┐ ┌──────────────┐
│Scanner  │ │USNMonitor│ │ContentReindex │ │FolderIndex  │
│Thread   │ │Thread    │ │Thread         │ │Thread        │
│         │ │          │ │               │ │              │
│MFT 전체 │ │5초 폴링  │ │텍스트 추출 +  │ │os.walk +     │
│스캔     │ │USN 변경  │ │DB 인덱싱      │ │ContentReindex│
│+ 캐시   │ │감지·적용 │ │(병렬)         │ │위임          │
└─────────┘ └──────────┘ └────────────────┘ └──────────────┘
```

---

## 2. QThread 기본 개념

### QThread vs Python threading.Thread

| 특성 | QThread | threading.Thread |
|------|---------|-----------------|
| 이벤트 루프 | 지원 (exec_) | 미지원 |
| 시그널/슬롯 | ✅ 크로스스레드 안전 | ❌ |
| GUI 업데이트 | 시그널로 안전하게 | 직접 호출 시 크래시 위험 |
| 취소 지원 | `request_stop()` (커스텀 플래그) | 커스텀 플래그 필요 |

> **왜 `requestInterruption()` 대신 `request_stop()`인가?**
> Qt의 `requestInterruption()` / `isInterruptionRequested()`는 표준 API이지만,
> SeekSeek는 자체 `_stop_requested` 불리언 플래그를 사용한다.
> 이유: `_stop_requested`는 run() 외부 루프(ThreadPoolExecutor 콜백 등)에서도
> 동일한 방식으로 확인할 수 있어 코드가 일관된다.

### QThread 사용 패턴 (SeekSeek)

```python
class ScannerThread(_ExclusionMixin, QThread):
    """MFT 스캔을 수행하는 백그라운드 스레드"""

    # 시그널 정의 (클래스 레벨)
    progress        = pyqtSignal(str, int)   # (상태 메시지, 처리된 파일 수)
    finished_signal = pyqtSignal(int, int)   # (전체 파일 수, 내용 색인 수)
    error_signal    = pyqtSignal(str)        # 오류 메시지

    def __init__(self, scan_paths=None, cache_only=False, parent=None):
        super().__init__(parent)
        self._cache_only     = cache_only
        self._stop_requested = False
        self._load_exclusions()   # _ExclusionMixin: 제외 경로를 인스턴스에 저장

    def request_stop(self):
        """스캔 중지를 요청한다. 플래그만 설정하며 즉시 중단되지는 않는다."""
        self._stop_requested = True

    def run(self):
        """스레드 진입점 — 별도 스레드에서 실행된다."""
        try:
            if self._cache_only:
                self._run_cache_only_scan()
            else:
                self._run_mft_scan()
        except Exception as e:
            self.error_signal.emit(str(e))
```

### 크로스스레드 시그널의 동작

```
워커 스레드                          메인 스레드
    │                                   │
    │   self.progress.emit(50, "...")    │
    │ ─────────────────────────────────→ │
    │        Qt 이벤트 큐에 인큐         │
    │                                   │
    │                    이벤트 루프가 디큐하여
    │                    슬롯 함수 호출 (안전)
    │                                   │
    │                    _on_scan_progress("...", 50)
    │                    → self.progress_bar.setValue(50)
```

> **핵심**: Qt의 시그널/슬롯은 스레드 간 경계를 넘을 때 자동으로
> **QueuedConnection**이 사용된다. 발신 스레드에서 emit하면,
> 수신 스레드의 이벤트 큐에 넣어지고, 이벤트 루프가 안전하게 처리한다.

---

## 3. _ExclusionMixin — 제외 경로 공유 믹스인

`ScannerThread`와 `USNMonitorThread` 모두 제외 경로(절대 경로 목록)와
제외 폴더(이름 기반 목록)를 로드하고 판별하는 동일한 로직이 필요하다.
이 중복을 제거하기 위해 `_ExclusionMixin`을 도입했다.

```python
class _ExclusionMixin:
    """ScannerThread / USNMonitorThread 의 공통 제외 경로 로딩·판별 믹스인."""

    def _load_exclusions(self) -> None:
        """제외 경로·폴더를 설정에서 로드하여 인스턴스 변수에 저장한다."""
        self._excluded_paths: set[str] = _load_excluded_paths_normalized()
        self._excluded_dirs:  set[str] = _load_excluded_dirs()

    def _should_exclude(self, filepath: str) -> bool:
        """filepath가 현재 인스턴스의 제외 규칙에 해당하는지 반환한다."""
        return _should_exclude(filepath, self._excluded_paths, self._excluded_dirs)
```

### 다중 상속과 MRO

```python
class ScannerThread(_ExclusionMixin, QThread):
    # MRO: ScannerThread → _ExclusionMixin → QThread → QObject → ...
    def __init__(self, ...):
        super().__init__(...)    # _ExclusionMixin.__init__ → QThread.__init__ 체인
        self._load_exclusions()  # 믹스인 메서드 호출
```

`FolderIndexThread`는 짧게 실행되고 run() 내에서만 제외 판별이 필요하므로
믹스인 없이 모듈 레벨 `_should_exclude()` 함수를 직접 호출한다.

---

## 4. ScannerThread — MFT 전체 스캔

### 두 가지 모드

| 모드 | `cache_only` | 동작 |
|------|-------------|------|
| 전체 스캔 | `False` | MFT 열거 → mft_cache + file_cache DB 구축 |
| 캐시 로드 | `True` | file_cache DB 로드 → mft_cache 채우기 (빠른 시작) |

### 전체 스캔 흐름

```
ScannerThread.run()
    │
    ├── cache_only=True?
    │   └── _run_cache_only_scan()
    │       ├── file_cache DB 로드 → mft_cache 채우기
    │       ├── _catchup_usn()  ← DB 저장 후 ~ 현재 사이 누락 변경분 보충
    │       └── finished_signal.emit(total, content_count)
    │
    └── cache_only=False?
        └── _run_mft_scan()
            ├── enumerate_mft() 호출
            │   ├── 전략 1: MFT 직접 파싱
            │   │   ├── FSCTL_GET_NTFS_VOLUME_DATA → 볼륨 정보
            │   │   ├── $MFT $DATA data runs 파싱
            │   │   └── 각 레코드 파싱 → MftFileEntry 생성
            │   │
            │   └── 전략 2: FSCTL_ENUM_USN_DATA (fallback)
            │       ├── 볼륨 핸들 열기
            │       ├── USN_JOURNAL 조회
            │       └── 64KB 버퍼로 반복 열거
            │
            ├── _resolve_paths() → 전체 경로 구성
            ├── mft_cache 갱신 + file_cache DB 저장
            └── finished_signal.emit(total, content_count)
```

### 앱 시작 시 스캔 순서

```python
# MainWindow._start_scan() 내부
def _start_scan(self, scan_paths=None):
    self._scanner_thread = ScannerThread(scan_paths=scan_paths)
    self._scanner_thread.progress.connect(self._on_scan_progress)
    self._scanner_thread.finished_signal.connect(self._on_scan_finished)
    self._scanner_thread.error_signal.connect(self._on_scan_error)
    self._scanner_thread.mode_signal.connect(self._on_scan_mode)
    self._scanner_thread.start()
```

---

## 5. USNMonitorThread — 증분 감시

### 핵심 로직

```python
class USNMonitorThread(_ExclusionMixin, QThread):
    """5초 간격으로 USN Journal을 폴링하여 변경을 감지한다."""

    POLL_INTERVAL = 5  # 폴링 간격 (초)

    updated         = pyqtSignal(int, int)  # (추가/수정 건수, 삭제 건수)
    paths_updated   = pyqtSignal(list)      # 추가/수정된 파일 경로 목록
    needs_full_scan = pyqtSignal()          # Journal 만료 → 전체 재스캔 필요

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_requested = False
        self._load_exclusions()  # _ExclusionMixin
        self._drives = config.MFT_SCAN_DRIVES or get_ntfs_drives()

    def request_stop(self):
        self._stop_requested = True

    def run(self):
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

    def _poll(self):
        """한 번의 폴링 사이클"""
        for drive in self._drives:
            state = load_usn_state(conn, drive)
            journal_id, start_usn = state
            changes, new_next_usn = read_usn_changes(drive, start_usn, journal_id)

            if changes is None:
                # Journal 만료 → 증분 불가, 전체 재스캔 요청
                self.needs_full_scan.emit()
                return

            if changes:
                added, deleted, paths = _apply_usn_changes(
                    changes, drive, self._should_exclude,
                    stop_flag=lambda: self._stop_requested,
                )
                save_usn_state(conn, drive, journal_id, new_next_usn)
                self.updated.emit(added, deleted)
                if paths:
                    self.paths_updated.emit(paths)
```

### 0.5초 대기의 인터럽트 처리

```python
# 왜 msleep(5000) 대신 msleep(500) × 10 인가?
#
# msleep(5000)은 5초 동안 블로킹되어 _stop_requested를
# 확인할 수 없다. 앱 종료 시 closeEvent가 스레드를 기다리는 동안
# 최대 5초가 낭비된다.
#
# 500ms 단위로 나누면 최대 500ms 내에 종료 요청을 감지할 수 있다.
```

---

## 6. ContentReindexThread — 콘텐츠 인덱싱

```
ContentReindexThread.run()
    │
    ├── DB에서 has_content=0 인 파일 목록 조회 (최대 1000개 청크)
    │
    ├── ThreadPoolExecutor (max_workers=2~4, CPU 코어 수 기반)
    │   ├── [Worker 1] extract_text(file_1.pdf)
    │   ├── [Worker 2] extract_text(file_2.docx)
    │   ├── [Worker 3] extract_text(file_3.hwp)
    │   └── [Worker 4] extract_text(file_4.xlsx)
    │
    ├── 추출 완료된 결과를 소배치로 즉시 DB에 저장
    │   (SQLite single writer 제약)
    │
    └── finished_signal.emit(indexed_count)
```

### 왜 병렬인가?

| 작업 유형 | 병목 | 병렬화 전략 |
|-----------|------|------------|
| 텍스트 추출 | **I/O 바운드** (디스크 읽기 + 라이브러리 파싱) | ThreadPoolExecutor |
| DB 쓰기 | **단일 Writer** (SQLite 제약) | 순차 실행 |

> Python의 GIL(Global Interpreter Lock)은 CPU 바운드 작업을 제한하지만,
> I/O 바운드 작업(파일 읽기, 라이브러리 C 확장 호출)에서는 GIL이 해제되므로
> 멀티스레딩이 효과적이다.

### 메모리 피크 완화 포인트 (v1.0.1+)

- 워커 수를 `2~4`로 제한해 대용량 파일 동시 파싱 개수를 제어한다.
- 추출 결과를 청크 전체에 모으지 않고 소배치로 즉시 flush한다.
- `MAX_CONTENT_SIZE`는 유지하되, 동시 추출량과 누적 버퍼를 줄여 응답없음 리스크를 낮춘다.

### 전체 색인/변경분 색인 실행 순서

이전에는 전체 색인과 변경분 색인이 동시에 실행될 수 있었지만,
현재는 메모리 피크 완화를 위해 **전체 색인 완료 후 변경분 색인을 직렬로 실행**한다.

---

## 7. FolderIndexThread — 폴더 단위 인덱싱

```python
class FolderIndexThread(QThread):
    """특정 폴더의 파일을 os.walk으로 순회하며 인덱싱한다."""

    def run(self):
        # 매 실행마다 최신 설정 로드
        excluded_paths = _load_excluded_paths_normalized()
        excluded_dirs  = _load_excluded_dirs()
        targets: list[str] = []

        for folder in self._folders:
            for root, dirs, files in os.walk(folder):
                # 현재 디렉터리 자체가 제외 대상이면 하위 탐색 전체 중단
                if _should_exclude(root, excluded_paths, excluded_dirs):
                    dirs[:] = []
                    continue

                # 서브디렉터리에서 제외 대상 제거
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

        # ContentReindexThread.run()을 직접 호출하여 청크 처리 로직 재사용
        # (.start()가 아닌 .run() 직접 호출 — 현재 스레드에서 실행)
        reindex = ContentReindexThread(targets)
        reindex.total_count.connect(self.total_count)
        reindex.progress.connect(self.progress)
        reindex.finished_signal.connect(self.finished_signal)
        reindex.run()
```

> **`_should_exclude()` 3단계 적용**
> 1. `root` 자체 제외 → `dirs[:] = []`로 하위 탐색 전체 차단
> 2. `dirs[:]` 재할당 → os.walk가 제외 폴더로 진입하지 않음
> 3. 개별 파일 확인 → 숨김 파일, 임시 파일 등 누락 없이 필터링

---

## 8. _apply_usn_changes() — 변경 적용 로직

```python
def _apply_usn_changes(
    changes: list,
    drive: str,
    exclude_fn,          # _should_exclude 메서드 또는 동등한 callable
    stop_flag=None,
) -> tuple[int, int, list[str]]:
    """USN 변경 레코드를 mft_cache에 반영한다.

    Last-Event-Wins 전략:
      같은 file_ref에 대해 여러 이벤트가 있으면 마지막 것만 처리

    처리 분류:
      FILE_DELETE  → 캐시에서 제거
      FILE_CREATE  → 경로 해석 후 캐시에 추가
      DATA_CHANGE  → 캐시 갱신 (경로 변경 없음)
      RENAME       → 구 경로 제거 + 신 경로 추가
    """
```

### 이벤트 분류 플로우

```
UsnChange (reason 비트 플래그)
     │
     ├── reason & FILE_DELETE?
     │   └── mft_cache에서 해당 file_ref 제거
     │
     ├── reason & FILE_CREATE?
     │   ├── file_ref로 경로 해석 (OpenFileById)
     │   └── 제외 대상 아니면 mft_cache에 추가
     │
     ├── reason & RENAME_NEW?
     │   ├── 구 경로 제거 (RENAME_OLD에서)
     │   ├── 신 경로 해석
     │   └── mft_cache 갱신
     │
     └── reason & DATA_CHANGE?
         └── 경로 재해석 후 mft_cache 항목 갱신
```

---

## 9. 스레드 생명주기 관리

### 시작

```python
# MainWindow에서 USN 모니터 시작
def _start_usn_monitor(self):
    if self._usn_monitor is not None:
        self._usn_monitor.request_stop()
        self._usn_monitor.wait()
    self._usn_monitor = USNMonitorThread()
    self._usn_monitor.paths_updated.connect(self._on_usn_paths_changed)
    self._usn_monitor.needs_full_scan.connect(self._on_usn_needs_full_scan)
    self._usn_monitor.start()
```

### 종료

```python
# MainWindow.closeEvent()에서
def closeEvent(self, event):
    # 스캔 스레드: stop 요청 후 최대 10초 대기
    for attr in ('_cache_init_thread', '_scanner_thread'):
        t = getattr(self, attr, None)
        if t is not None and t.isRunning():
            t.request_stop()
            t.wait(10000)

    # 색인 스레드: 진행 중이면 완료까지 최대 10초 대기 (강제 중단 없음)
    for attr in ('_reindex_thread', '_folder_index_thread'):
        t = getattr(self, attr, None)
        if t is not None and t.isRunning():
            t.wait(10000)

    # USN 모니터: stop 요청 후 최대 5초 대기
    if self._usn_monitor is not None:
        self._usn_monitor.request_stop()
        self._usn_monitor.wait(5000)

    # mft_cache → DB 저장 (다음 시작 시 빠른 캐시 로드용)
    if mft_cache.count() > 0:
        with get_connection() as conn:
            mft_cache.save_to_db(conn)

    super().closeEvent(event)
```

> **주의**: `QThread.terminate()`는 스레드를 강제 종료하며, 리소스 누수와
> 데이터 손상을 유발할 수 있으므로 사용하지 않는다.
> 항상 `request_stop()` + `wait()`로 안전하게 종료한다.

---

## 10. SeekSeek에서의 구현 위치

| 구현 요소 | 파일 | 클래스 / 함수 |
|-----------|------|--------------|
| 제외 경로 공유 믹스인 | `core/scanner.py` | `_ExclusionMixin` |
| MFT 스캔 워커 | `core/scanner.py` | `ScannerThread` |
| USN 모니터 | `core/scanner.py` | `USNMonitorThread` |
| 콘텐츠 인덱서 | `core/scanner.py` | `ContentReindexThread` |
| 폴더 인덱서 | `core/scanner.py` | `FolderIndexThread` |
| USN 변경 적용 | `core/scanner.py` | `_apply_usn_changes()` |
| 스레드 조율 | `gui/main_window.py` | `MainWindow._start_scan()` 등 |

---

## 참고 자료

- [Qt 6 Threading Basics](https://doc.qt.io/qt-6/thread-basics.html)
- [PyQt6 QThread 문서](https://www.riverbankcomputing.com/static/Docs/PyQt6/api/qtcore/qthread.html)
- [Python concurrent.futures](https://docs.python.org/3/library/concurrent.futures.html)
