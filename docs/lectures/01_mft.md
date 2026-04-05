# 1강: NTFS Master File Table (MFT) 구조

## 개요

NTFS(New Technology File System)는 Windows NT 계열 운영 체제의 기본 파일 시스템이다.
NTFS의 핵심은 **MFT(Master File Table)**로, 볼륨에 존재하는 모든 파일과 디렉터리의 메타데이터를 저장하는 특수 파일이다.

SeekSeek 프로젝트는 이 MFT를 직접 파싱하여 **수 초 내에** 볼륨의 전체 파일 목록을 열거한다.
이는 `os.walk()`로 수분 이상 걸리는 작업을 획기적으로 단축하는 핵심 기술이다.

---

## 1. MFT란 무엇인가?

```
┌──────────────────────────────────────────────────────┐
│                   NTFS 볼륨                          │
│                                                      │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐     │
│  │ MFT 레코드 │  │ MFT 레코드 │  │ MFT 레코드 │ ... │
│  │  #0 ($MFT) │  │  #1        │  │  #2        │     │
│  └────────────┘  └────────────┘  └────────────┘     │
│                                                      │
│  ┌──────────────────────────────────────────┐        │
│  │         데이터 영역 (파일 내용)           │        │
│  └──────────────────────────────────────────┘        │
└──────────────────────────────────────────────────────┘
```

- **MFT**는 볼륨의 모든 파일·디렉터리에 대해 최소 하나의 레코드(entry)를 가진다
- MFT 자체도 파일이며, MFT 레코드 #0 (`$MFT`)이 바로 자신에 대한 레코드다
- 파일이 추가되면 MFT가 커지고, 삭제되면 해당 레코드는 "free"로 표시되어 재사용된다
- MFT는 한 번 커지면 **축소되지 않는다**

### MFT 시스템 파일 (처음 16개 레코드)

| 레코드 # | 이름 | 설명 |
|----------|------|------|
| 0 | `$MFT` | MFT 자체 |
| 1 | `$MFTMirr` | MFT 처음 4개 레코드의 백업 |
| 2 | `$LogFile` | 트랜잭션 로그 (저널링) |
| 3 | `$Volume` | 볼륨 이름, NTFS 버전 등 |
| 4 | `$AttrDef` | 속성 정의 테이블 |
| 5 | `.` | 루트 디렉터리 |
| 6 | `$Bitmap` | 클러스터 사용 현황 비트맵 |
| 7 | `$Boot` | 부트 섹터 정보 |
| 8 | `$BadClus` | 불량 클러스터 목록 |
| 9 | `$Secure` | ACL 보안 디스크립터 |
| 10 | `$UpCase` | 대문자 변환 테이블 |
| 11 | `$Extend` | 확장 메타데이터 디렉터리 |
| 12-15 | (예약) | 향후 사용을 위한 예약 영역 |

---

## 2. MFT 레코드 구조 (1024바이트)

각 MFT 레코드는 기본적으로 **1024바이트** 고정 크기를 가진다.
볼륨의 `bytes_per_mft_record` 값으로 확인 가능하다.

```
MFT 레코드 (1024 bytes)
┌───────────────────────────────────────────────────────┐
│  레코드 헤더 (42 bytes)                               │
│  ┌─────────────────────────────────────────────────┐  │
│  │  시그니처: "FILE" (4 bytes)                     │  │
│  │  fixup 배열 오프셋 (2 bytes)                    │  │
│  │  fixup 배열 엔트리 수 (2 bytes)                 │  │
│  │  $LogFile 시퀀스 번호 (8 bytes)                 │  │
│  │  시퀀스 번호 (2 bytes)                          │  │
│  │  하드링크 카운트 (2 bytes)                      │  │
│  │  첫 번째 속성 오프셋 (2 bytes)                  │  │
│  │  플래그: IN_USE(0x01), DIRECTORY(0x02) (2 bytes)│  │
│  │  실제 사용 크기 (4 bytes)                       │  │
│  │  할당된 크기 (4 bytes)                          │  │
│  │  base record 참조 (8 bytes)                     │  │
│  │  ...                                            │  │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  속성 1: $STANDARD_INFORMATION (0x10)                 │
│  속성 2: $FILE_NAME (0x30)                            │
│  속성 3: $DATA (0x80)                                 │
│  ...                                                  │
│  속성 종료 마커: 0xFFFFFFFF                           │
│  [남은 공간]                                           │
└───────────────────────────────────────────────────────┘
```

### Fixup 배열 (Multi-Sector Protection)

NTFS는 디스크 섹터(512 bytes) 경계에서 데이터 손상을 검출하기 위한 **fixup 배열**을 사용한다:

1. 각 섹터의 마지막 2바이트를 원래 값을 저장하는 배열로 백업
2. 각 섹터의 마지막 2바이트를 **fixup 시그니처**로 대체하여 기록
3. 읽을 때 시그니처를 확인하고, 원래 값을 복원

```python
# SeekSeek의 fixup 복원 로직 (core/mft_scanner.py)
def _apply_fixup(record: bytearray, fixup_offset: int, fixup_count: int):
    """fixup 배열을 적용하여 원본 레코드를 복원한다."""
    signature = struct.unpack_from('<H', record, fixup_offset)[0]
    for i in range(1, fixup_count):
        sector_end = i * 512 - 2
        stored = struct.unpack_from('<H', record, fixup_offset + i * 2)[0]
        # 섹터 끝의 시그니처가 일치하는지 확인
        actual = struct.unpack_from('<H', record, sector_end)[0]
        if actual != signature:
            raise ValueError("Fixup mismatch — 레코드 손상 가능")
        # 원래 값 복원
        struct.pack_into('<H', record, sector_end, stored)
```

> **왜 필요한가?**: 디스크 컨트롤러가 섹터를 쓰는 도중 전원이 끊기면, 불완전하게 기록된 섹터가 생긴다.
> fixup 배열은 이런 상황을 감지하여 데이터 무결성을 보장한다.

---

## 3. MFT 속성 (Attributes)

MFT 레코드는 여러 **속성(attribute)**으로 구성된다. 모든 파일 메타데이터는 속성 형태로 저장된다.

### 주요 속성 타입

| 타입 코드 | 이름 | 설명 |
|-----------|------|------|
| `0x10` | `$STANDARD_INFORMATION` | 생성·수정·접근 시간, 파일 특성 플래그 |
| `0x20` | `$ATTRIBUTE_LIST` | 여러 MFT 레코드에 걸쳐 속성 분산 시 사용 |
| `0x30` | `$FILE_NAME` | 파일명, 부모 디렉터리 참조, 네임스페이스 |
| `0x40` | `$OBJECT_ID` | 분산 링크 추적용 GUID |
| `0x50` | `$SECURITY_DESCRIPTOR` | ACL 보안 설정 |
| `0x60` | `$VOLUME_NAME` | 볼륨 이름 ($Volume 파일 전용) |
| `0x70` | `$VOLUME_INFORMATION` | NTFS 버전 등 ($Volume 파일 전용) |
| `0x80` | `$DATA` | **파일 실제 데이터** (또는 데이터 런 포인터) |
| `0x90` | `$INDEX_ROOT` | 디렉터리 인덱스 B-tree 루트 |
| `0xA0` | `$INDEX_ALLOCATION` | 디렉터리 인덱스 B-tree 노드 |
| `0xB0` | `$BITMAP` | 인덱스 할당 비트맵 |
| `0xC0` | `$REPARSE_POINT` | 심볼릭 링크, 마운트 포인트 등 |
| `0x100` | `$EA_INFORMATION` | 확장 속성 정보 |

### Resident vs Non-Resident 속성

```
Resident (레코드 내 직접 저장):
┌──────────────┬──────────────────────────┐
│  속성 헤더   │  데이터 (레코드 내 저장)  │
└──────────────┴──────────────────────────┘
   → 작은 파일(~700 bytes)은 MFT 레코드 안에 직접 저장

Non-Resident (데이터 런으로 외부 참조):
┌──────────────┬─────────────────────────────────┐
│  속성 헤더   │  Data Runs (클러스터 위치 목록)  │
└──────────────┴─────────────────────────────────┘
   → 큰 파일은 클러스터 위치를 Data Run으로 기록
```

---

## 4. SeekSeek의 MFT 파싱 전략

SeekSeek은 두 가지 전략으로 MFT를 열거한다:

### 전략 1: 직접 MFT 레코드 파싱 (Primary)

```
1. FSCTL_GET_NTFS_VOLUME_DATA    → 볼륨 메타데이터 (MFT 시작 LCN, 레코드 크기 등)
2. $MFT의 $DATA 속성에서 Data Run 목록 추출
3. Data Run을 따라 디스크에서 직접 MFT 레코드를 순차 읽기
4. 각 레코드를 파싱: fixup 복원 → 속성 순회 → 파일명/크기/시간 추출
5. parent_ref 체인으로 전체 경로 재구성
```

**장점**: DeviceIoControl API 우회, 더 빠르고 더 많은 메타데이터 접근 가능
**단점**: NTFS 내부 구조를 직접 다루므로 구현 복잡

### 전략 2: FSCTL_ENUM_USN_DATA (Fallback)

```
1. CreateFileW("\\.\C:")              → 볼륨 핸들 열기
2. FSCTL_QUERY_USN_JOURNAL            → journal_id, next_usn 확보
3. FSCTL_ENUM_USN_DATA (반복 호출)    → 64KB 버퍼로 USN_RECORD_V2 파싱
4. _resolve_paths()                   → parent_ref 체인으로 전체 경로 재구성
```

**장점**: 비교적 단순한 구현, Windows API가 레코드 파싱을 처리
**단점**: 직접 파싱보다 느림, 특정 메타데이터 접근 제한

### Data Run 인코딩

`$DATA` 속성이 non-resident인 경우, 데이터의 물리적 위치를 **Data Run**으로 인코딩한다:

```
Data Run 바이트 구조:
┌──────────────────────────────┐
│ 헤더 바이트:                 │
│   상위 니블 = offset 크기    │
│   하위 니블 = length 크기    │
│ length 필드 (1~4 bytes)      │
│ offset 필드 (1~4 bytes)      │
│   → signed, 이전 run 기준    │
│     상대 오프셋              │
└──────────────────────────────┘

예시: [0x31] [0x05] [0xA0, 0x00, 0x00]
       │        │     └─ offset = 0xA0 (3 bytes, signed) → LCN 160
       │        └─ length = 5 클러스터
       └─ 헤더: offset_size=3, length_size=1
```

```python
# SeekSeek의 Data Run 파싱 (core/mft_scanner.py)
def _parse_data_runs(data: bytes) -> list[tuple[int, int]]:
    """Non-resident $DATA 속성의 data run 목록을 파싱한다.
    
    Returns: [(offset_bytes, length_bytes), ...]
    """
    runs = []
    pos = 0
    prev_offset = 0
    while pos < len(data) and data[pos] != 0:
        header = data[pos]
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        pos += 1
        
        length = int.from_bytes(data[pos:pos+len_size], 'little', signed=False)
        pos += len_size
        
        offset = int.from_bytes(data[pos:pos+off_size], 'little', signed=True)
        pos += off_size
        
        prev_offset += offset  # 상대 오프셋 누적
        runs.append((prev_offset, length))
    return runs
```

---

## 5. 경로 재구성 (Path Resolution)

MFT 레코드의 `$FILE_NAME` 속성에는 부모 디렉터리의 **MFT 참조 번호**(parent_ref)가 포함된다.
전체 경로를 구성하려면 parent_ref 체인을 루트(`file_ref = 5`)까지 따라가야 한다:

```
파일: "readme.txt" (parent_ref = 100)
  ↓ parent_ref
디렉터리: "docs" (file_ref = 100, parent_ref = 50)
  ↓ parent_ref
디렉터리: "project" (file_ref = 50, parent_ref = 5)
  ↓ parent_ref
루트: "\" (file_ref = 5, parent_ref = 5)  ← 루트는 자기 참조

결과 경로: C:\project\docs\readme.txt
```

```python
# 경로 재구성 시 순환 참조 방지
MAX_DEPTH = 512  # 무한 루프 방지
```

### File Reference Number 구조

MFT 참조 번호는 8바이트(64비트)로 구성된다:

```
┌────────────────────┬──────────────────────┐
│ 시퀀스 번호 (16bit)│ MFT 엔트리 번호(48bit)│
└────────────────────┴──────────────────────┘
  상위 16비트          하위 48비트

file_ref & 0x0000FFFFFFFFFFFF = MFT 엔트리 인덱스
file_ref >> 48                = 시퀀스 번호 (재사용 감지)
```

시퀀스 번호는 MFT 엔트리가 삭제 후 재사용될 때마다 증가하여,
**stale reference**(이미 삭제된 파일을 가리키는 참조)를 감지할 수 있다.

---

## 6. FILETIME ↔ Unix Timestamp 변환

Windows FILETIME은 **1601년 1월 1일** 기준 100나노초 단위이고,
Unix timestamp는 **1970년 1월 1일** 기준 초 단위이다.

```python
# 변환 공식
_FT_EPOCH_DIFF = 116_444_736_000_000_000  # 1601~1970 차이 (100ns 단위)
_FT_TICKS_SEC  = 10_000_000               # 1초 = 10^7 × 100ns

def filetime_to_unix(ft: int) -> float:
    """Windows FILETIME (100ns ticks) → Unix timestamp (seconds)"""
    return (ft - _FT_EPOCH_DIFF) / _FT_TICKS_SEC
```

---

## 7. SeekSeek에서의 구현 위치

| 구현 요소 | 파일 | 함수/클래스 |
|-----------|------|------------|
| MFT 직접 파싱 | `core/mft_scanner.py` | `_parse_mft_record()`, `_enumerate_mft_records()` |
| Data Run 파싱 | `core/mft_scanner.py` | `_parse_data_runs()` |
| Fixup 배열 처리 | `core/mft_scanner.py` | `_apply_fixup()` (인라인) |
| USN 열거 (fallback) | `core/mft_scanner.py` | `enumerate_mft()` |
| 경로 재구성 | `core/mft_scanner.py` | `_resolve_paths()` |
| 결과 데이터 클래스 | `core/mft_scanner.py` | `MftFileEntry`, `MftScanResult` |
| 메모리 캐시 저장 | `core/mft_cache.py` | `MftCache.add()` |
| DB 영속 저장 | `core/indexer.py` | `upsert_file()` |

---

## 참고 자료

- [Microsoft Learn: Master File Table](https://learn.microsoft.com/en-us/windows/win32/fileio/master-file-table)
- [NTFS Documentation on Wikipedia](https://en.wikipedia.org/wiki/NTFS#Master_File_Table)
- [Flatcap NTFS Documentation](https://flatcap.github.io/linux-ntfs/ntfs/concepts/file_record.html)
