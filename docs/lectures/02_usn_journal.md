# 2강: USN Journal (변경 저널)

## 개요

**USN(Update Sequence Number) Journal**은 NTFS 볼륨에서 파일과 디렉터리의
변경 사항을 시간순으로 기록하는 **변경 저널(Change Journal)**이다.

SeekSeek은 USN Journal을 사용하여 **전체 MFT 재스캔 없이** 증분(incremental)
업데이트를 수행한다. 5초 간격으로 폴링하여 변경된 파일만 감지·반영한다.

---

## 1. USN Journal이란?

### 전통적 방법의 한계

| 방법 | 장점 | 단점 |
|------|------|------|
| `os.walk()` 전체 스캔 | 간단 | 대용량 볼륨에서 수 분 소요 |
| `FindFirstChangeNotification` | 실시간 | 앱이 항상 실행 중이어야 함, 메모리 소비 |
| `ReadDirectoryChangesW` | 상세 정보 | 모니터링 중 놓친 변경 복구 불가 |
| **USN Journal** | 빠름, 놓친 변경 복구 가능 | 관리자 권한 필요, NTFS 전용 |

### USN Journal의 동작 원리

```
파일 생성/수정/삭제 발생
         │
         ▼
┌────────────────────────────┐
│  NTFS 파일 시스템 드라이버  │
│  USN Journal에 레코드 추가  │
│  (USN 번호 자동 증가)      │
└────────────────────────────┘
         │
         ▼
┌────────────────────────────┐
│  $Extend\$UsnJrnl 파일     │
│  $J 데이터 스트림에 저장    │
└────────────────────────────┘
```

- 각 변경 이벤트에 고유한 **USN(Update Sequence Number)**이 할당된다
- USN은 단조 증가하며, 절대 감소하지 않는다
- Journal은 오래된 레코드를 자동으로 삭제하여 디스크 공간을 관리한다

---

## 2. USN_RECORD_V2 구조

각 변경 이벤트는 `USN_RECORD_V2` 구조체로 기록된다:

```c
typedef struct {
    DWORD         RecordLength;       // 레코드 전체 바이트 크기
    WORD          MajorVersion;       // 버전 (보통 2)
    WORD          MinorVersion;
    DWORDLONG     FileReferenceNumber;  // 변경된 파일/디렉터리의 MFT 참조
    DWORDLONG     ParentFileReferenceNumber;  // 부모 디렉터리 MFT 참조
    USN           Usn;                // 이 레코드의 USN
    LARGE_INTEGER TimeStamp;          // 변경 발생 시간 (FILETIME)
    DWORD         Reason;             // 변경 원인 비트 플래그
    DWORD         SourceInfo;
    DWORD         SecurityId;
    DWORD         FileAttributes;     // 파일 속성 (DIRECTORY 등)
    WORD          FileNameLength;     // 파일명 바이트 크기
    WORD          FileNameOffset;     // 파일명 시작 오프셋
    WCHAR         FileName[1];        // 가변 길이 파일명 (UTF-16LE)
} USN_RECORD_V2;
```

### Reason 비트 플래그 (변경 원인)

| 상수 | 값 | 설명 |
|------|-----|------|
| `USN_REASON_DATA_OVERWRITE` | `0x00000001` | 파일 데이터 덮어쓰기 |
| `USN_REASON_DATA_EXTEND` | `0x00000002` | 파일 크기 증가 |
| `USN_REASON_DATA_TRUNCATION` | `0x00000004` | 파일 크기 축소 |
| `USN_REASON_FILE_CREATE` | `0x00000100` | 파일/디렉터리 생성 |
| `USN_REASON_FILE_DELETE` | `0x00000200` | 파일/디렉터리 삭제 |
| `USN_REASON_RENAME_OLD_NAME` | `0x00001000` | 이름 변경 전 (이전 경로) |
| `USN_REASON_RENAME_NEW_NAME` | `0x00002000` | 이름 변경 후 (새 경로) |
| `USN_REASON_CLOSE` | `0x80000000` | 파일 핸들 닫기 |

> **SeekSeek의 필터 마스크**: `_REASON_MASK = 0x00003307`
> DATA 변경(0x07) + CREATE(0x100) + DELETE(0x200) + RENAME(0x3000) 만 감시

---

## 3. USN Journal 관련 제어 코드 (FSCTL)

### FSCTL_QUERY_USN_JOURNAL (0x000900F4)

현재 Journal의 메타데이터를 조회한다:

```python
# SeekSeek 코드 (core/mft_scanner.py)
class USN_JOURNAL_DATA(ctypes.Structure):
    _fields_ = [
        ("UsnJournalID",    ctypes.c_uint64),  # Journal 고유 ID
        ("FirstUsn",        ctypes.c_int64),    # 현재 보관 중인 최소 USN
        ("NextUsn",         ctypes.c_int64),    # 다음에 기록될 USN
        ("LowestValidUsn",  ctypes.c_int64),    # 유효한 최소 USN
        ("MaxUsn",          ctypes.c_int64),    # Journal 최대 크기
        ("MaximumSize",     ctypes.c_uint64),   # 디스크 할당 최대 크기
        ("AllocationDelta", ctypes.c_uint64),   # 할당 단위
    ]
```

### FSCTL_ENUM_USN_DATA (0x000900B3)

MFT의 모든 USN 레코드를 열거한다 (전체 스캔용):

```
입력:  MFT_ENUM_DATA_V0 { StartFileReferenceNumber, LowUsn, HighUsn }
출력:  [NextUSN (8 bytes)] + [USN_RECORD_V2, USN_RECORD_V2, ...]

반복 호출하며 StartFileReferenceNumber를 갱신하여 전체 MFT 순회
```

### FSCTL_READ_USN_JOURNAL (0x000900BB)

특정 USN 이후의 변경 레코드만 읽는다 (증분 업데이트용):

```
입력:  READ_USN_JOURNAL_DATA_V0 {
         StartUsn,        // 이 USN 이후의 변경만
         ReasonMask,      // 관심 있는 변경 원인 필터
         ReturnOnlyOnClose, // CLOSE 이벤트만 반환할지
         Timeout,
         BytesToWaitFor,
         UsnJournalID     // Journal ID (무효화 감지)
       }
출력:  [NextUSN (8 bytes)] + [USN_RECORD_V2, USN_RECORD_V2, ...]
```

---

## 4. SeekSeek의 증분 업데이트 흐름

```
┌─────────────────────────────────────────────────────────┐
│                USNMonitorThread (5초 폴링)               │
│                                                         │
│  1. DB에서 저장된 (journal_id, next_usn) 로드            │
│     │                                                   │
│  2. read_usn_changes(drive, journal_id, start_usn) 호출  │
│     │                                                   │
│     ├─ FSCTL_READ_USN_JOURNAL로 변경 레코드 열거         │
│     ├─ 각 레코드를 UsnChange 객체로 변환                 │
│     └─ 새로운 next_usn 반환                              │
│     │                                                   │
│  3. _apply_usn_changes()로 변경 적용                     │
│     │                                                   │
│     ├─ reason에 FILE_DELETE 포함?                        │
│     │   → DB에서 삭제, 캐시에서 제거                      │
│     ├─ reason에 FILE_CREATE 포함?                        │
│     │   → DB에 upsert, 캐시에 추가                       │
│     └─ reason에 DATA_CHANGE 포함?                        │
│         → DB의 has_content=0으로 리셋 (재인덱싱 대기)     │
│     │                                                   │
│  4. (journal_id, new_next_usn) DB에 저장                 │
│     │                                                   │
│  5. 5초 후 1번으로 돌아감                                │
└─────────────────────────────────────────────────────────┘
```

### Last-Event-Wins 전략

같은 file_ref에 대해 여러 변경 이벤트가 있으면, **마지막 이벤트를 기준**으로 처리한다:

```python
# 이벤트 우선순위 결정 로직
by_ref: dict[int, UsnChange] = {}
for change in changes:
    by_ref[change.file_ref] = change  # 나중 이벤트가 이전 이벤트를 덮어씀

# 예: rename_old → rename_new → data_change
#     마지막인 data_change만 처리
```

### Journal 무효화 처리

```python
# ERROR_JOURNAL_ENTRY_DELETED (0x570) 발생 시
# → 저장된 start_usn이 이미 Journal에서 만료됨
# → 전체 재스캔(full MFT scan) 트리거
```

---

## 5. UsnChange 데이터 구조

```python
@dataclass
class UsnChange:
    """USN Journal 변경 이벤트 하나를 나타내는 데이터 클래스"""
    file_ref:   int     # 변경된 파일의 MFT 참조 번호
    parent_ref: int     # 부모 디렉터리의 MFT 참조 번호
    name:       str     # 파일/디렉터리 이름
    is_dir:     bool    # 디렉터리 여부
    reason:     int     # 변경 원인 비트 플래그
    usn:        int     # USN (증분 스캔의 다음 시작점)
```

---

## 6. 에러 처리 시나리오

| 에러 | 원인 | 대응 |
|------|------|------|
| `ERROR_HANDLE_EOF` (38) | Journal 끝에 도달 | 정상 종료 (모든 변경 읽기 완료) |
| `ERROR_JOURNAL_ENTRY_DELETED` (0x570) | `start_usn`이 만료됨 | 전체 MFT 재스캔 트리거 |
| `Journal ID 불일치` | Journal이 재생성됨 | `start_usn = 0`으로 초기화 후 전체 읽기 |
| 권한 부족 | 관리자가 아님 | 스캔 스킵, 사용자에게 알림 |

---

## 7. SeekSeek에서의 구현 위치

| 구현 요소 | 파일 | 함수/클래스 |
|-----------|------|------------|
| USN Journal 조회 | `core/mft_scanner.py` | `_query_usn_journal()` |
| USN 레코드 열거 | `core/mft_scanner.py` | `enumerate_mft()` (fallback) |
| USN 변경 읽기 | `core/mft_scanner.py` | `read_usn_changes()` |
| 증분 업데이트 적용 | `core/scanner.py` | `_apply_usn_changes()` |
| 5초 폴링 모니터 | `core/scanner.py` | `USNMonitorThread._poll()` |
| USN 상태 저장/로드 | `core/indexer.py` | `save_usn_state()`, `load_usn_state()` |

---

## 참고 자료

- [Microsoft Learn: Change Journals](https://learn.microsoft.com/en-us/windows/win32/fileio/change-journals)
- [FSCTL_READ_USN_JOURNAL](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_read_usn_journal)
- [USN_RECORD_V2 structure](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-usn_record_v2)
