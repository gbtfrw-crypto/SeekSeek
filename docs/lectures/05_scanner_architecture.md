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
│    progress(percent, message)  → 프로그레스 바 갱신          │
│    finished(result)            → 결과 테이블 갱신            │
│    error(message)              → 에러 표시                   │
│    usn_changes_applied(count)  → 캐시 갱신 후 재검색         │
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
| 취소 지원 | `requestInterruption()` | 커스텀 플래그 필요 |

### QThread 사용 패턴 (SeekSeek)

```python
class ScannerThread(QThread):
    """MFT 스캔을 수행하는 백그라운드 스레드"""
    
    # 시그널 정의 (클래스 레벨)
    progress = pyqtSignal(int, str)    # (percent, message)
    finished = pyqtSignal(object)       # (MftScanResult | MftCache)
    error    = pyqtSignal(str)          # (error_message)
    
    def __init__(self, cache_only=False):
        super().__init__()
        self._cache_only = cache_only
    
    def run(self):
        """스레드 진입점 — 별도 스레드에서 실행된다."""
        try:
            if self._cache_only:
                # DB에서 캐시 로드만
                cache = MftCache()
                cache.load_from_db()
                self.finished.emit(cache)
            else:
                # MFT 전체 스캔
                result = enumerate_mft(progress_callback=self._on_progress)
                self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))
    
    def _on_progress(self, percent, message):
        """콜백 → 시그널 변환"""
        if not self.isInterruptionRequested():
            self.progress.emit(percent, message)
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
    │                    _on_scan_progress(50, "...")
    │                    → self.progress_bar.setValue(50)
```

> **핵심**: Qt의 시그널/슬롯은 스레드 간 경계를 넘을 때 자동으로
> **QueuedConnection**이 사용된다. 발신 스레드에서 emit하면,
> 수신 스레드의 이벤트 큐에 넣어지고, 이벤트 루프가 안전하게 처리한다.

---

## 3. ScannerThread — MFT 전체 스캔

### 두 가지 모드

| 모드 | `cache_only` | 동작 |
|------|-------------|------|
| 전체 스캔 | `False` | MFT 열거 → MftScanResult 반환 |
| 캐시 로드 | `True` | DB → MftCache 로드만 |

### 전체 스캔 흐름

```
ScannerThread.run()
    │
    ├── cache_only=True?
    │   └── MftCache.load_from_db() → finished.emit(cache)
    │
    └── cache_only=False?
        ├── enumerate_mft() 호출
        │   ├── 전략 1: MFT 직접 파싱 시도
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
        └── finished.emit(MftScanResult)
```

### 앱 시작 시 스캔 순서

```python
# MainWindow._start_scan() 내부
def _start_scan(self):
    """앱 시작 시 호출. MFT 스캔 또는 캐시 로드를 수행한다."""
    if DB에_캐시가_존재:
        # 빠른 시작: DB에서 캐시만 로드
        self._scanner = ScannerThread(cache_only=True)
    else:
        # 최초 실행: 전체 MFT 스캔
        self._scanner = ScannerThread(cache_only=False)
    
    self._scanner.finished.connect(self._on_scan_finished)
    self._scanner.start()
```

---

## 4. USNMonitorThread — 증분 감시

### 핵심 로직

```python
class USNMonitorThread(QThread):
    """5초 간격으로 USN Journal을 폴링하여 변경을 감지한다."""

    paths_updated = pyqtSignal(list)  # 변경된 파일 경로 목록

    def run(self):
        while not self.isInterruptionRequested():
            self._poll()
            # 5초 대기 (인터럽트 가능하게 분할)
            for _ in range(50):  # 100ms × 50 = 5초
                if self.isInterruptionRequested():
                    return
                self.msleep(100)

    def _poll(self):
        """한 번의 폴링 사이클"""
        for drive in self._drives:
            journal_id, start_usn = load_usn_state(drive)
            changes, new_usn = read_usn_changes(drive, journal_id, start_usn)

            if changes:
                added, deleted, changed_paths = _apply_usn_changes(changes, self._cache)
                save_usn_state(drive, journal_id, new_usn)
                if changed_paths:
                    self.paths_updated.emit(changed_paths)
```

### 5초 대기의 인터럽트 처리

```python
# 왜 sleep(5) 대신 msleep(100) × 50 인가?
#
# QThread.sleep(5)는 5초 동안 블로킹되어 isInterruptionRequested()를
# 확인할 수 없다. 앱 종료 시 스레드가 5초간 멈춰있게 된다.
#
# 100ms 단위로 나누면 최대 100ms 내에 종료 요청을 감지할 수 있다.
```

---

## 5. ContentReindexThread — 콘텐츠 인덱싱

```
ContentReindexThread.run()
    │
    ├── DB에서 has_content=0 인 파일 목록 조회
    │
    ├── ThreadPoolExecutor (max_workers=4)
    │   ├── [Worker 1] extract_text(file_1.pdf)
    │   ├── [Worker 2] extract_text(file_2.docx)
    │   ├── [Worker 3] extract_text(file_3.hwp)
    │   └── [Worker 4] extract_text(file_4.xlsx)
    │
    ├── 추출 완료된 결과를 순차적으로 DB에 저장
    │   (SQLite single writer 제약)
    │
    └── finished.emit(indexed_count)
```

### 왜 병렬인가?

| 작업 유형 | 병목 | 병렬화 전략 |
|-----------|------|------------|
| 텍스트 추출 | **I/O 바운드** (디스크 읽기 + 라이브러리 파싱) | ThreadPoolExecutor |
| DB 쓰기 | **단일 Writer** (SQLite 제약) | 순차 실행 |

> Python의 GIL(Global Interpreter Lock)은 CPU 바운드 작업을 제한하지만,
> I/O 바운드 작업(파일 읽기, 라이브러리 C 확장 호출)에서는 GIL이 해제되므로
> 멀티스레딩이 효과적이다.

---

## 6. FolderIndexThread — 폴더 단위 인덱싱

```python
class FolderIndexThread(QThread):
    """특정 폴더의 파일을 os.walk으로 순회하며 인덱싱한다."""
    
    def run(self):
        for root, dirs, files in os.walk(self._folder):
            # 제외 폴더 필터링
            dirs[:] = [d for d in dirs if d not in WELL_KNOWN_EXCLUDED_DIRS]
            
            for f in files:
                path = os.path.join(root, f)
                # DB에 파일 메타데이터 upsert
                upsert_file(path, name, ext, size, mtime)
            
            self.progress.emit(percent, f"스캔 중: {root}")
        
        # 텍스트 추출은 ContentReindexThread에 위임
        self._reindex_thread = ContentReindexThread(...)
        self._reindex_thread.start()
        self._reindex_thread.wait()
```

---

## 7. _apply_usn_changes() — 변경 적용 로직

```python
def _apply_usn_changes(
    changes: list[UsnChange],
    cache: MftCache,
    indexer_conn: Connection
) -> int:
    """USN 변경 이벤트를 DB와 캐시에 일괄 적용한다.
    
    Last-Event-Wins 전략:
      같은 file_ref에 대해 여러 이벤트가 있으면 마지막 것만 처리
    
    처리 분류:
      FILE_DELETE  → DB에서 삭제, 캐시에서 제거
      FILE_CREATE  → DB에 upsert, 캐시에 추가 (경로 해석 필요)
      DATA_CHANGE  → has_content=0으로 리셋 (재인덱싱 대기)
      RENAME       → 구 경로 삭제 + 신 경로 추가
    """
```

### 이벤트 분류 플로우

```
UsnChange (reason 비트 플래그)
     │
     ├── reason & FILE_DELETE?
     │   └── DB + 캐시에서 해당 file_ref 제거
     │
     ├── reason & FILE_CREATE?
     │   ├── file_ref로 경로 해석 (OpenFileById)
     │   └── DB upsert + 캐시 추가
     │
     ├── reason & RENAME_NEW?
     │   ├── 구 경로 제거 (RENAME_OLD에서)
     │   ├── 신 경로 해석
     │   └── DB upsert + 캐시 갱신
     │
     └── reason & DATA_CHANGE?
         └── has_content = 0 (다음 인덱싱 사이클에서 재추출)
```

---

## 8. 스레드 생명주기 관리

### 시작

```python
# MainWindow에서 스캔 시작
self._scanner = ScannerThread()
self._scanner.finished.connect(self._on_scan_finished)
self._scanner.error.connect(self._on_scan_error)
self._scanner.start()  # QThread.start() → 내부적으로 run() 호출
```

### 종료

```python
# MainWindow.closeEvent()에서
def closeEvent(self, event):
    # 모든 워커 스레드에 인터럽트 요청
    if self._usn_monitor:
        self._usn_monitor.requestInterruption()
        self._usn_monitor.wait(3000)  # 최대 3초 대기
    
    if self._scanner and self._scanner.isRunning():
        self._scanner.requestInterruption()
        self._scanner.wait(5000)
    
    event.accept()
```

> **주의**: `QThread.terminate()`는 스레드를 강제 종료하며, 리소스 누수와
> 데이터 손상을 유발할 수 있으므로 사용하지 않는다.
> 항상 `requestInterruption()` + `wait()`로 안전하게 종료한다.

---

## 9. SeekSeek에서의 구현 위치

| 구현 요소 | 파일 | 클래스 |
|-----------|------|--------|
| MFT 스캔 워커 | `core/scanner.py` | `ScannerThread` |
| USN 모니터 | `core/scanner.py` | `USNMonitorThread` |
| 콘텐츠 인덱서 | `core/scanner.py` | `ContentReindexThread` |
| 폴더 인덱서 | `core/scanner.py` | `FolderIndexThread` |
| 변경 적용 | `core/scanner.py` | `_apply_usn_changes()` |
| 스레드 조율 | `gui/main_window.py` | `MainWindow._start_scan()` 등 |

---

## 참고 자료

- [Qt 6 Threading Basics](https://doc.qt.io/qt-6/thread-basics.html)
- [PyQt6 QThread 문서](https://www.riverbankcomputing.com/static/Docs/PyQt6/api/qtcore/qthread.html)
- [Python concurrent.futures](https://docs.python.org/3/library/concurrent.futures.html)
