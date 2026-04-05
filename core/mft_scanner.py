"""NTFS MFT 기반 초고속 파일 열거 모듈

Windows FSCTL_ENUM_USN_DATA / MFT 직접 파싱 API를 사용하여 NTFS 볼륨의
전체 파일 목록을 수 초 내에 열거한다. 관리자 권한 필수.

■ MFT(Master File Table)란?
  NTFS의 핵심 메타데이터 구조로, 볼륨 내 모든 파일과 디렉터리에 대해
  하나 이상의 MFT 레코드(기본 1024바이트)를 할당한다.
  레코드 내부에는 $STANDARD_INFORMATION(0x10), $FILE_NAME(0x30),
  $DATA(0x80) 등의 속성(attribute)이 차례로 배치된다.
  → Microsoft 공식 문서: https://learn.microsoft.com/ko-kr/windows/win32/fileio/master-file-table

■ 스캔 전략 (2단계 폴백)
  1차: MFT 직접 파싱 (preferred)
    - FSCTL_GET_NTFS_VOLUME_DATA → MFT 시작 LCN, 레코드 크기, 클러스터 크기 획득
    - MFT 레코드 0($MFT 자체)의 $DATA 속성에서 data run list 파싱
    - data run을 따라 볼륨상의 실제 바이트 오프셋으로 MFT 레코드 순차 읽기
    - 각 레코드에서 fixup array 복원 → 속성 체인 순회 → MftFileEntry 생성
    - 장점: size, modified(mtime) 등 메타데이터를 MFT에서 직접 추출하므로
            os.stat() 호출 없이 완전한 파일 정보를 얻을 수 있음
  2차: FSCTL_ENUM_USN_DATA 폴백
    - MFT 직접 파싱이 실패할 경우(예: 비표준 MFT 구조)
    - USN_RECORD_V2 기반으로 파일명·부모 참조만 수집 (size 정보는 제한적)

■ USN Journal 증분 업데이트 흐름
  1. 저장된 (journal_id, start_usn) 로드
  2. FSCTL_READ_USN_JOURNAL — start_usn 이후 변경 레코드 열거
  3. 변경 원인(reason)으로 삭제/수정/이름 변경 분류
  → Microsoft 공식 문서: https://learn.microsoft.com/ko-kr/windows/win32/fileio/change-journals

■ 경로 재구성(_resolve_paths)
  MFT/USN 레코드는 파일명과 parent_ref(부모 디렉터리의 MFT 번호)만 가지므로,
  parent_ref 체인을 루트(자기 자신을 부모로 가리키는 레코드)까지 거슬러 올라가
  전체 경로를 조합한다. path_cache로 동일 디렉터리의 형제 파일 조회를 O(1)로 최적화.
"""
import ctypes
import ctypes.wintypes as wintypes
import os
import logging
import string
import struct
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Windows API 상수 ──────────────────────────────────────────────────────────
# CreateFileW() 의 dwDesiredAccess / dwShareMode / dwCreationDisposition 파라미터.
# 볼륨 핸들을 읽기 전용으로 열며, 다른 프로세스의 읽기/쓰기/삭제를 허용한다.
GENERIC_READ            = 0x80000000
FILE_SHARE_READ         = 0x00000001
FILE_SHARE_WRITE        = 0x00000002
FILE_SHARE_DELETE       = 0x00000004
OPEN_EXISTING           = 3
# FILE_FLAG_BACKUP_SEMANTICS: 백업 의미론으로 디렉터리 핸들을 열 수 있게 함 (관리자 필수)
FILE_FLAG_BACKUP_SEMANTICS  = 0x02000000
# FILE_FLAG_OPEN_BY_FILE_ID: 파일 경로 대신 MFT 참조 번호(File ID)로 파일을 열 때 사용
FILE_FLAG_OPEN_BY_FILE_ID   = 0x20000000
VOLUME_NAME_DOS         = 0x0

# DeviceIoControl 제어 코드 (FSCTL = File System Control)
# 제어 코드 구조: [DeviceType(16bit)] [Access(2bit)] [Function(12bit)] [Method(2bit)]
FSCTL_ENUM_USN_DATA    = 0x000900B3   # MFT 전체 열거 (USN_RECORD_V2 형태로 반환)
FSCTL_QUERY_USN_JOURNAL = 0x000900F4  # Journal 메타데이터 조회 (journal_id, next_usn 등)
FSCTL_READ_USN_JOURNAL  = 0x000900BB  # Journal 변경 레코드 읽기 (증분 업데이트용)

# USN_RECORD_V2 파일 특성 플래그 (FileAttributes 필드)
FILE_ATTRIBUTE_DIRECTORY = 0x10  # 비트4: 디렉터리 여부

# GetLastError 반환 코드 — FSCTL_READ_USN_JOURNAL 호출 후 확인
ERROR_HANDLE_EOF             = 38     # 38 = Journal 끝에 도달 (변경 없음, 정상 종료)
ERROR_JOURNAL_ENTRY_DELETED  = 0x570  # 1392 = Journal 항목 만료 → 전체 재스캔 필요
# Journal 항목 만료는 시스템이 MaximumSize를 초과하여 오래된 레코드를 삭제했을 때 발생.

# 드라이브 종류 (GetDriveTypeW 반환값)
DRIVE_REMOVABLE = 2
DRIVE_FIXED     = 3

# FSCTL_READ_USN_JOURNAL reason 필터 마스크
# USN reason 비트 플래그 — 파일 변경 유형을 나타내는 비트마스크.
# 하나의 USN 레코드에 여러 reason 비트가 동시에 설정될 수 있으며,
# "last-event-wins" 전략으로 같은 file_ref에 대해 마지막 reason만 채택한다.
USN_REASON_DATA_CHANGE = 0x00000007   # OVERWRITE(0x1) | EXTEND(0x2) | TRUNCATION(0x4) 조합
USN_REASON_FILE_CREATE = 0x00000100   # 새 파일 생성
USN_REASON_FILE_DELETE = 0x00000200   # 파일 완전 삭제
USN_REASON_RENAME_OLD  = 0x00001000   # 이름 변경 전 (구 경로) — 구 항목 삭제에 사용
USN_REASON_RENAME_NEW  = 0x00002000   # 이름 변경 후 (신 경로) — 신 항목 추가에 사용
_REASON_MASK = (
    USN_REASON_DATA_CHANGE | USN_REASON_FILE_CREATE |
    USN_REASON_FILE_DELETE | USN_REASON_RENAME_OLD | USN_REASON_RENAME_NEW
)

# I/O 버퍼 크기 (64 KB)
BUFFER_SIZE = 65536

# ── MFT 직접 파싱 상수 ─────────────────────────────────────────────────────────
# FSCTL_GET_NTFS_VOLUME_DATA: NTFS 볼륨의 물리 구조 정보를 반환
# → BytesPerFileRecordSegment(MFT 레코드 크기), MftStartLcn(MFT 시작 클러스터) 등
FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064

# MFT 레코드 속성(Attribute) 타입 코드
# MFT 레코드는 고정 헤더(첫 42바이트) 뒤에 가변 길이 속성이 연속 배치된다.
# 속성 체인 순회: attr_type → attr_length → 다음 속성 = offset + length
_ATTR_STANDARD_INFORMATION = 0x10   # 생성일, 수정일, 접근일, 파일 플래그
_ATTR_FILE_NAME            = 0x30   # 부모 디렉터리 참조, 파일명, 네임스페이스
_ATTR_DATA                 = 0x80   # 파일 본문 데이터 (resident 또는 non-resident)
_ATTR_END                  = 0xFFFFFFFF  # 속성 체인 종료 표시

# $FILE_NAME 네임스페이스 — 하나의 파일에 여러 $FILE_NAME 속성이 존재할 수 있다.
# Win32 이름(긴 이름)이 가장 유용하므로 우선순위를 두어 선택한다.
_NS_WIN32     = 1   # Win32 긴 이름 (최우선)
_NS_DOS       = 2   # 8.3 짧은 이름 (호환용, 가장 낮은 우선순위)
_NS_WIN32_DOS = 3   # Win32+DOS 통합 (긴/짧은 이름이 동일한 경우)

# 네임스페이스 우선순위 맵 (모듈 수준 상수 — 레코드당 dict 생성 방지)
# POSIX(0)는 Win32보다 낮지만 DOS보다 높게 설정
_NS_PRIORITY = {_NS_WIN32: 4, _NS_WIN32_DOS: 3, 0: 2, _NS_DOS: 1}

# FILETIME → Unix timestamp 변환 상수
# Windows FILETIME: 1601-01-01 UTC 기준 100나노초 단위
# Unix timestamp:   1970-01-01 UTC 기준 초 단위
# _FT_EPOCH_DIFF = (1970-01-01 - 1601-01-01) 의 100ns 틱 수
_FT_EPOCH_DIFF = 116444736000000000
_FT_TICKS_SEC  = 10_000_000

FILE_BEGIN = 0

# ── Win32 API 바인딩 ──────────────────────────────────────────────────────────
kernel32 = ctypes.windll.kernel32

CreateFileW = kernel32.CreateFileW
CreateFileW.restype  = wintypes.HANDLE
CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]

DeviceIoControl = kernel32.DeviceIoControl
DeviceIoControl.restype  = wintypes.BOOL
DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]

CloseHandle = kernel32.CloseHandle
CloseHandle.restype  = wintypes.BOOL
CloseHandle.argtypes = [wintypes.HANDLE]

# FILE_FLAG_OPEN_BY_FILE_ID 사용 시 lpFileName은 문자열 경로가 아니라
# 파일 참조 번호(File ID) 버퍼 포인터로 해석된다.
# 그래서 CreateFileW의 일반 LPCWSTR 시그니처와 별도로 c_void_p 바인딩을 둔다.
_CreateFileW_by_ref = kernel32.CreateFileW
_CreateFileW_by_ref.restype  = wintypes.HANDLE
_CreateFileW_by_ref.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]

_GetFinalPathNameByHandleW = kernel32.GetFinalPathNameByHandleW
_GetFinalPathNameByHandleW.restype  = wintypes.DWORD
_GetFinalPathNameByHandleW.argtypes = [
    wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
]

INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

SetFilePointerEx = kernel32.SetFilePointerEx
SetFilePointerEx.restype  = wintypes.BOOL
SetFilePointerEx.argtypes = [
    wintypes.HANDLE, ctypes.c_longlong,
    ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
]

ReadFile = kernel32.ReadFile
ReadFile.restype  = wintypes.BOOL
ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]


# ── OpenFileById 바인딩 ───────────────────────────────────────────────────────
# FILE_ID_DESCRIPTOR 구조체 (Type=0 -> FileIdType -> 64-bit LARGE_INTEGER)
# 실제 Win32 구조체는 union(LARGE_INTEGER/GUID/FILE_ID_128)을 포함하므로
# Python 측에서도 24바이트 정렬을 맞춰야 OpenFileById 호출이 안정적이다.
class FILE_ID_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),       # 4 bytes
        ("Type",   wintypes.DWORD),       # 4 bytes  (0 = FileIdType)
        ("FileId", ctypes.c_ulonglong),   # 8 bytes  (unsigned 64-bit)
        ("_pad",   ctypes.c_byte * 8),    # 8 bytes  (union 크기 맞춤 → 총 24바이트)
    ]

_OpenFileById = kernel32.OpenFileById
_OpenFileById.restype  = wintypes.HANDLE
_OpenFileById.argtypes = [
    wintypes.HANDLE,      # hVolumeHint
    ctypes.POINTER(FILE_ID_DESCRIPTOR),  # lpFileId
    wintypes.DWORD,       # dwDesiredAccess
    wintypes.DWORD,       # dwShareMode
    ctypes.c_void_p,      # lpSecurityAttributes
    wintypes.DWORD,       # dwFlagsAndAttributes
]


# ── 구조체 정의 ───────────────────────────────────────────────────────────────

class MFT_ENUM_DATA_V0(ctypes.Structure):
    """FSCTL_ENUM_USN_DATA 입력 구조체.

    StartFileReferenceNumber: 이 번호 이상의 MFT 엔트리부터 열거 시작 (첫 호출은 0)
    LowUsn / HighUsn: USN 범위 필터 (0 ~ journal.NextUsn 이면 전체)
    """
    _fields_ = [
        ("StartFileReferenceNumber", ctypes.c_ulonglong),
        ("LowUsn",                   ctypes.c_longlong),
        ("HighUsn",                  ctypes.c_longlong),
    ]


class USN_JOURNAL_DATA(ctypes.Structure):
    """FSCTL_QUERY_USN_JOURNAL 출력 구조체.

    UsnJournalID  : Journal 고유 식별자 (재포맷 시 변경됨)
    NextUsn       : 다음 레코드가 기록될 USN 위치
    LowestValidUsn: 유효한 가장 오래된 USN (이보다 오래된 start_usn은 만료)
    """
    _fields_ = [
        ("UsnJournalID",    ctypes.c_ulonglong),
        ("FirstUsn",        ctypes.c_longlong),
        ("NextUsn",         ctypes.c_longlong),
        ("LowestValidUsn",  ctypes.c_longlong),
        ("MaxUsn",          ctypes.c_longlong),
        ("MaximumSize",     ctypes.c_ulonglong),
        ("AllocationDelta", ctypes.c_ulonglong),
    ]


class _ReadUsnJournalData(ctypes.Structure):
    """FSCTL_READ_USN_JOURNAL 입력 구조체 (V0, 40 바이트).

    핵심 필드:
    - StartUsn: 이 값 이후의 변경 레코드부터 읽기 시작
    - ReasonMask: 관심 있는 USN_REASON 비트만 필터링
    - UsnJournalID: 저널 재생성 여부를 식별하는 안전장치
    """
    _fields_ = [
        ("StartUsn",          ctypes.c_longlong),
        ("ReasonMask",        wintypes.DWORD),
        ("ReturnOnlyOnClose", wintypes.DWORD),
        ("Timeout",           ctypes.c_ulonglong),
        ("BytesToWaitFor",    ctypes.c_ulonglong),
        ("UsnJournalID",      ctypes.c_ulonglong),
    ]


class NTFS_VOLUME_DATA_BUFFER(ctypes.Structure):
    """FSCTL_GET_NTFS_VOLUME_DATA 출력 구조체."""
    _fields_ = [
        ("VolumeSerialNumber",            ctypes.c_longlong),
        ("NumberSectors",                 ctypes.c_longlong),
        ("TotalClusters",                 ctypes.c_longlong),
        ("FreeClusters",                  ctypes.c_longlong),
        ("TotalReserved",                 ctypes.c_longlong),
        ("BytesPerSector",                wintypes.DWORD),
        ("BytesPerCluster",               wintypes.DWORD),
        ("BytesPerFileRecordSegment",     wintypes.DWORD),
        ("ClustersPerFileRecordSegment",  wintypes.DWORD),
        ("MftValidDataLength",            ctypes.c_longlong),
        ("MftStartLcn",                   ctypes.c_longlong),
        ("Mft2StartLcn",                  ctypes.c_longlong),
        ("MftZoneStart",                  ctypes.c_longlong),
        ("MftZoneEnd",                    ctypes.c_longlong),
    ]


# ── 데이터 클래스 ─────────────────────────────────────────────────────────────

@dataclass
class MftFileEntry:
    """MFT에서 열거된 파일/디렉터리 엔트리 하나.

    full_path는 _resolve_paths() 호출 후 채워진다.
    size/modified는 MFT 직접 파싱으로 채워진다.
    """
    file_ref:   int    # 이 파일의 MFT 참조 번호 (하위 6바이트)
    parent_ref: int    # 부모 디렉터리의 MFT 참조 번호
    name:       str    # 파일명 (경로 미포함, UTF-16LE 디코딩)
    is_dir:     bool   # True이면 디렉터리
    full_path:  str   = ""  # _resolve_paths() 가 채워주는 전체 경로
    size:       int   = 0   # 파일 크기 (바이트) — MFT $DATA 속성에서 직접 추출
    modified:   float = 0.0 # 수정 시각 (Unix timestamp) — MFT $STANDARD_INFORMATION


@dataclass
class MftScanResult:
    """enumerate_mft() 반환값."""
    files:         list[MftFileEntry] = field(default_factory=list)
    total_entries: int  = 0
    drive:         str  = ""
    success:       bool = False
    error:         str  = ""
    journal_id:    int  = 0   # 증분 업데이트 기준점 저장용
    next_usn:      int  = 0   # 증분 업데이트 기준점 저장용


@dataclass
class UsnChange:
    """read_usn_changes() 가 반환하는 변경 레코드 하나."""
    file_ref:     int   # 변경된 파일의 MFT 참조 번호 (하위 48비트)
    parent_ref:   int   # 부모 디렉터리 MFT 참조 번호
    name:         str   # 파일명
    reason:       int   # USN_REASON_* 플래그 조합
    # 시퀀스 번호를 포함한 원본 64비트 참조값.
    # 파일 재사용/경로 재해석 시 충돌을 줄이기 위해 유지한다.
    raw_file_ref: int = 0


# ── 헬퍼 함수 ─────────────────────────────────────────────────────────────────

def get_ntfs_drives() -> list[str]:
    """시스템에 마운트된 NTFS 드라이브 문자 목록을 반환한다.

    고정 디스크(DRIVE_FIXED)와 이동식 디스크(DRIVE_REMOVABLE) 중
    파일시스템이 NTFS인 것만 포함한다.
    """
    drives = []
    bitmask = kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not (bitmask & (1 << i)):
            continue
        drive_path = f"{letter}:\\"
        drive_type = kernel32.GetDriveTypeW(drive_path)
        if drive_type not in (DRIVE_REMOVABLE, DRIVE_FIXED):
            continue
        # GetVolumeInformationW 로 파일시스템 이름 확인
        fs_name = ctypes.create_unicode_buffer(32)
        ok = kernel32.GetVolumeInformationW(
            drive_path, None, 0, None, None, None, fs_name, 32
        )
        if ok and fs_name.value == "NTFS":
            drives.append(letter)
    return drives


def _open_volume(drive_letter: str):
    """볼륨 핸들을 열어 반환한다. 실패 시 None 반환."""
    volume_path = f"\\\\.\\{drive_letter}:"
    handle = CreateFileW(
        volume_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        err = kernel32.GetLastError()
        logger.error("볼륨 열기 실패 %s (오류 코드 %d)", volume_path, err)
        return None
    return handle


def _query_usn_journal(handle) -> USN_JOURNAL_DATA | None:
    """볼륨 핸들로 USN Journal 메타데이터를 조회한다."""
    journal_data  = USN_JOURNAL_DATA()
    bytes_returned = wintypes.DWORD(0)
    ok = DeviceIoControl(
        handle, FSCTL_QUERY_USN_JOURNAL,
        None, 0,
        ctypes.byref(journal_data), ctypes.sizeof(journal_data),
        ctypes.byref(bytes_returned), None,
    )
    if not ok:
        err = kernel32.GetLastError()
        logger.error("USN Journal 조회 실패 (오류 코드 %d)", err)
        return None
    return journal_data


def _parse_usn_record(rec: bytes) -> tuple[int, int, int, int, str, float] | None:
    """USN_RECORD_V2 바이트 시퀀스를 파싱한다.

    Returns:
        (file_ref, parent_ref, file_attrs, reason, name) 튜플,
        또는 파싱 불가 시 None.

    USN_RECORD_V2 필드 레이아웃:
        +0   4  RecordLength
        +4   2  MajorVersion
        +6   2  MinorVersion
        +8   8  FileReferenceNumber
        +16  8  ParentFileReferenceNumber
        +24  8  Usn
        +32  8  TimeStamp
        +40  4  Reason
        +44  4  SourceInfo
        +48  4  SecurityId
        +52  4  FileAttributes
        +56  2  FileNameLength
        +58  2  FileNameOffset  (보통 60)
        +60  ?  FileName        (UTF-16LE)
    """
    if len(rec) < 60:
        return None

    # 상위 2바이트는 시퀀스 번호이므로 하위 6바이트(48비트)만 사용
    file_ref   = int.from_bytes(rec[8:16],  "little") & 0x0000FFFFFFFFFFFF
    parent_ref = int.from_bytes(rec[16:24], "little") & 0x0000FFFFFFFFFFFF
    # TimeStamp: Windows FILETIME (100ns intervals since 1601-01-01) → Unix timestamp
    filetime   = int.from_bytes(rec[32:40], "little")
    modified   = (filetime - 116444736000000000) / 10_000_000 if filetime else 0.0
    reason     = int.from_bytes(rec[40:44], "little")
    file_attrs = int.from_bytes(rec[52:56], "little")
    fname_len  = int.from_bytes(rec[56:58], "little")
    fname_off  = int.from_bytes(rec[58:60], "little")

    fname_end = fname_off + fname_len
    if fname_end > len(rec):
        return None
    try:
        name = rec[fname_off:fname_end].decode("utf-16-le")
    except UnicodeDecodeError:
        return None

    if not name:
        return None
    return file_ref, parent_ref, file_attrs, reason, name, modified


# ── MFT 직접 파싱 함수 ───────────────────────────────────────────────────────

def _get_ntfs_volume_data(handle) -> NTFS_VOLUME_DATA_BUFFER | None:
    """FSCTL_GET_NTFS_VOLUME_DATA 로 NTFS 볼륨 메타데이터를 조회한다."""
    vol_data = NTFS_VOLUME_DATA_BUFFER()
    bytes_returned = wintypes.DWORD(0)
    ok = DeviceIoControl(
        handle, FSCTL_GET_NTFS_VOLUME_DATA,
        None, 0,
        ctypes.byref(vol_data), ctypes.sizeof(vol_data),
        ctypes.byref(bytes_returned), None,
    )
    if not ok:
        logger.error("NTFS 볼륨 데이터 조회 실패 (오류 코드 %d)", kernel32.GetLastError())
        return None
    return vol_data


def _apply_fixup(record: bytearray, bytes_per_sector: int) -> bool:
    """MFT 레코드의 fixup array 를 적용하여 원래 섹터 끝 바이트를 복원한다.

    ■ Fixup Array(복원 배열)이란?
      NTFS는 디스크 쎄터 경계(512바이트)를 걸치는 레코드의 무결성을 보장하기 위해
      fixup 메커니즘을 사용한다:
      1) 디스크에 쓰기 전, 각 섹터의 마지막 2바이트(sig)를 fixup 배열에 보존
      2) 해당 위치에 공통 시그니처(2바이트)를 덮어쓰기
      3) 읽을 때 시그니처 일치 여부로 손상 감지 후 원례 바이트 복원
      → 이 함수는 3번 단계를 수행하여 레코드를 원본 상태로 돌려놓는다.

    Returns:
        True  — fixup 성공 (시그니처 일치, 원본 복원 완료)
        False — 레코드 손상 감지 (시그니처 불일치)
    """
    if len(record) < 48:
        return False
    fixup_offset = struct.unpack_from('<H', record, 4)[0]
    fixup_count  = struct.unpack_from('<H', record, 6)[0]   # 1 + 섹터 수
    if fixup_count < 2 or fixup_offset + fixup_count * 2 > len(record):
        return False
    sig = record[fixup_offset:fixup_offset + 2]
    for i in range(1, fixup_count):
        pos = i * bytes_per_sector - 2
        if pos + 2 > len(record):
            break
        if record[pos:pos + 2] != sig:
            return False
        orig = fixup_offset + i * 2
        record[pos:pos + 2] = record[orig:orig + 2]
    return True


def _parse_data_runs(attr_bytes: bytes, runs_offset: int) -> list[tuple[int, int]]:
    """non-resident 속성의 data run list 를 파싱한다.

    ■ Data Run List 구조
      NTFS non-resident 속성의 실제 데이터는 디스크의 여러 구간(클러스터 연속 범위)에
      흐터져 저장된다. data run list는 이 구간들을 (offset, length) 쌍으로 나열한다.

      각 data run 항목:
        [header: 1바이트] [length: N바이트] [offset: M바이트]
        - header의 하위 4비트 = length 필드 크기(N 바이트)
        - header의 상위 4비트 = offset 필드 크기(M 바이트)
        - offset은 이전 run으로부터의 상대값(signed) → 누적 합계 = 절대 LCN
        - offset이 0바이트이면 sparse run (확보되었지만 ia4당되지 않은 영역)
        - header == 0x00 이면 리스트 종료

    Returns:
        [(절대_클러스터_오프셋, 클러스터_수), ...]
    """
    runs: list[tuple[int, int]] = []
    pos = runs_offset
    prev_lcn = 0
    while pos < len(attr_bytes):
        header = attr_bytes[pos]
        if header == 0:
            break
        pos += 1
        len_bytes = header & 0x0F
        off_bytes = (header >> 4) & 0x0F
        if len_bytes == 0 or pos + len_bytes + off_bytes > len(attr_bytes):
            break
        run_len = int.from_bytes(attr_bytes[pos:pos + len_bytes], 'little', signed=False)
        pos += len_bytes
        if off_bytes > 0:
            run_off = int.from_bytes(attr_bytes[pos:pos + off_bytes], 'little', signed=True)
            pos += off_bytes
            prev_lcn += run_off
            runs.append((prev_lcn, run_len))
        # off_bytes == 0 → sparse run, skip
    return runs


def _parse_mft_record(record: bytearray, record_number: int,
                      bytes_per_sector: int) -> MftFileEntry | None:
    """단일 MFT 레코드를 파싱하여 MftFileEntry 를 반환한다.

    ■ MFT 레코드 고정 헤더 레이아웃 (처음 ~42 바이트)
      +0   4  Signature ('FILE')
      +4   2  UpdateSequenceOffset (fixup 배열 시작 위치)
      +6   2  UpdateSequenceCount  (fixup 항목 수)
      +16  2  SequenceNumber       (레코드 재사용 횟수)
      +20  2  FirstAttributeOffset (첫 속성 시작 오프셋)
      +22  2  Flags (0x01=사용중, 0x02=디렉터리)
      +32  6  BaseFileRecordSegment (확장 레코드의 기본 레코드 번호)

    ■ 속성 체인 순회
      - $STANDARD_INFORMATION(0x10): +8 오프셋에 수정일(FILETIME) → Unix timestamp
      - $FILE_NAME(0x30): 부모 MFT 참조(4바이트), 네임스페이스, 파일명
      - $DATA(0x80): resident면 +16에 크기, non-resident면 +48에 실제 크기
    """
    if len(record) < 48 or record[:4] != b'FILE':
        return None
    if not _apply_fixup(record, bytes_per_sector):
        return None

    flags = struct.unpack_from('<H', record, 22)[0]
    if not (flags & 0x01):          # 삭제된 레코드
        return None
    is_dir = bool(flags & 0x02)

    # base record ≠ 0 이면 확장 레코드 → 건너뜀
    base_ref = int.from_bytes(record[32:38], 'little')
    if base_ref != 0:
        return None

    attr_off = struct.unpack_from('<H', record, 20)[0]
    modified    = 0.0
    best_name   = ""
    best_ns_pri = -1
    parent_ref  = 0
    file_size   = 0

    while attr_off + 16 <= len(record):
        atype = struct.unpack_from('<I', record, attr_off)[0]
        if atype == _ATTR_END or atype == 0:
            break
        alen = struct.unpack_from('<I', record, attr_off + 4)[0]
        if alen < 16 or attr_off + alen > len(record):
            break
        non_res  = record[attr_off + 8]
        name_len = record[attr_off + 9]   # 속성 이름 길이 (파일 이름 아님)

        if atype == _ATTR_STANDARD_INFORMATION and non_res == 0:
            coff = struct.unpack_from('<H', record, attr_off + 20)[0]
            abs_off = attr_off + coff
            if abs_off + 16 <= len(record):
                ft = struct.unpack_from('<Q', record, abs_off + 8)[0]
                if ft > _FT_EPOCH_DIFF:
                    modified = (ft - _FT_EPOCH_DIFF) / _FT_TICKS_SEC

        elif atype == _ATTR_FILE_NAME and non_res == 0:
            coff = struct.unpack_from('<H', record, attr_off + 20)[0]
            abs_off = attr_off + coff
            if abs_off + 66 <= len(record):
                # 하위 4바이트만 = 순수 레코드 번호 (상위 2바이트는 시퀀스 번호)
                pref   = int.from_bytes(record[abs_off:abs_off + 4], 'little')
                fn_len = record[abs_off + 64]       # 이름 글자 수
                ns     = record[abs_off + 65]        # 네임스페이스
                name_start = abs_off + 66
                name_end   = name_start + fn_len * 2
                if name_end <= len(record):
                    try:
                        name = record[name_start:name_end].decode('utf-16-le')
                    except UnicodeDecodeError:
                        name = ""
                    ns_pri = _NS_PRIORITY.get(ns, 0)
                    if ns_pri > best_ns_pri:
                        best_name   = name
                        best_ns_pri = ns_pri
                        parent_ref  = pref

        elif atype == _ATTR_DATA and name_len == 0:
            # unnamed $DATA = 주 데이터 스트림
            if non_res == 0:
                file_size = struct.unpack_from('<I', record, attr_off + 16)[0]
            else:
                if attr_off + 56 <= len(record):
                    file_size = struct.unpack_from('<Q', record, attr_off + 48)[0]

        attr_off += alen

    if not best_name:
        return None
    return MftFileEntry(
        file_ref=record_number, parent_ref=parent_ref,
        name=best_name, is_dir=is_dir,
        size=file_size, modified=modified,
    )


def _get_mft_data_runs(handle, vol_data: NTFS_VOLUME_DATA_BUFFER
                       ) -> list[tuple[int, int]] | None:
    """MFT 레코드 0 을 읽어 $MFT 의 데이터 런 목록을 반환한다."""
    rec_size = vol_data.BytesPerFileRecordSegment
    bps      = vol_data.BytesPerSector
    mft_byte = vol_data.MftStartLcn * vol_data.BytesPerCluster

    new_pos = ctypes.c_longlong(0)
    if not SetFilePointerEx(handle, mft_byte, ctypes.byref(new_pos), FILE_BEGIN):
        return None
    buf = ctypes.create_string_buffer(rec_size)
    br  = wintypes.DWORD(0)
    if not ReadFile(handle, buf, rec_size, ctypes.byref(br), None):
        return None

    rec = bytearray(buf.raw[:br.value])
    if rec[:4] != b'FILE':
        return None
    if not _apply_fixup(rec, bps):
        return None

    attr_off = struct.unpack_from('<H', rec, 20)[0]
    while attr_off + 16 <= len(rec):
        atype = struct.unpack_from('<I', rec, attr_off)[0]
        if atype == _ATTR_END or atype == 0:
            break
        alen = struct.unpack_from('<I', rec, attr_off + 4)[0]
        if alen < 16 or attr_off + alen > len(rec):
            break
        non_res  = rec[attr_off + 8]
        name_len = rec[attr_off + 9]
        if atype == _ATTR_DATA and name_len == 0 and non_res == 1:
            runs_off = struct.unpack_from('<H', rec, attr_off + 32)[0]
            return _parse_data_runs(rec[attr_off:attr_off + alen], runs_off)
        attr_off += alen
    return None


def _enumerate_mft_records(handle, vol_data: NTFS_VOLUME_DATA_BUFFER,
                           progress_callback) -> dict[int, MftFileEntry]:
    """$MFT 를 직접 읽어 모든 MFT 레코드를 파싱한다."""
    rec_size  = vol_data.BytesPerFileRecordSegment
    bpc       = vol_data.BytesPerCluster
    bps       = vol_data.BytesPerSector
    mft_valid = vol_data.MftValidDataLength

    data_runs = _get_mft_data_runs(handle, vol_data)
    if not data_runs:
        raise RuntimeError("$MFT 데이터 런 파싱 실패")

    # 읽기 청크: rec_size 의 배수로 정렬 (~4 MB)
    READ_CHUNK = (4 * 1024 * 1024 // rec_size) * rec_size

    entries: dict[int, MftFileEntry] = {}
    total_read = 0
    rec_num    = 0
    last_prog  = 0
    new_pos    = ctypes.c_longlong(0)
    br         = wintypes.DWORD(0)

    for cluster_lcn, cluster_cnt in data_runs:
        run_byte_off = cluster_lcn * bpc
        run_byte_len = cluster_cnt * bpc
        remain_valid = mft_valid - total_read
        if remain_valid <= 0:
            break
        run_byte_len = min(run_byte_len, remain_valid)

        if not SetFilePointerEx(handle, run_byte_off,
                                ctypes.byref(new_pos), FILE_BEGIN):
            total_read += run_byte_len
            rec_num    += run_byte_len // rec_size
            continue

        run_left = run_byte_len
        while run_left > 0:
            to_read = min(run_left, READ_CHUNK)
            chunk   = ctypes.create_string_buffer(to_read)
            if not ReadFile(handle, chunk, to_read, ctypes.byref(br), None):
                break
            actual = br.value
            if actual == 0:
                break
            n_recs = actual // rec_size
            raw    = chunk.raw
            rec_buf = bytearray(rec_size)
            for i in range(n_recs):
                off   = i * rec_size
                rec_buf[:] = raw[off:off + rec_size]
                entry = _parse_mft_record(rec_buf, rec_num, bps)
                if entry is not None:
                    entries[rec_num] = entry
                rec_num += 1
            total_read += actual
            run_left   -= actual
            if progress_callback and len(entries) - last_prog >= 50000:
                progress_callback(len(entries))
                last_prog = len(entries)

        if total_read >= mft_valid:
            break

    return entries


# ── MFT 전체 열거 (USN 폴백) ─────────────────────────────────────────────────

def _enumerate_mft_usn(handle, journal: USN_JOURNAL_DATA,
                       progress_callback) -> dict[int, MftFileEntry]:
    """FSCTL_ENUM_USN_DATA 를 이용한 기존 MFT 열거 (직접 파싱 실패 시 폴백)."""
    entries: dict[int, MftFileEntry] = {}
    enum_data = MFT_ENUM_DATA_V0()
    enum_data.StartFileReferenceNumber = 0
    enum_data.LowUsn  = 0
    enum_data.HighUsn = journal.NextUsn
    output_buffer  = ctypes.create_string_buffer(BUFFER_SIZE)
    bytes_returned = wintypes.DWORD(0)

    while True:
        ok = DeviceIoControl(
            handle, FSCTL_ENUM_USN_DATA,
            ctypes.byref(enum_data), ctypes.sizeof(enum_data),
            output_buffer, BUFFER_SIZE,
            ctypes.byref(bytes_returned), None,
        )
        if not ok:
            break
        returned = bytes_returned.value
        if returned <= 8:
            break
        next_ref = ctypes.c_ulonglong.from_buffer_copy(output_buffer, 0).value
        offset   = 8
        while offset < returned:
            if offset + 4 > returned:
                break
            record_length = int.from_bytes(
                output_buffer[offset:offset + 4], "little"
            )
            if record_length == 0 or offset + record_length > returned:
                break
            rec    = output_buffer[offset:offset + record_length]
            parsed = _parse_usn_record(rec)
            if parsed:
                file_ref, parent_ref, file_attrs, _, name, modified = parsed
                entries[file_ref] = MftFileEntry(
                    file_ref   = file_ref,
                    parent_ref = parent_ref,
                    name       = name,
                    is_dir     = bool(file_attrs & FILE_ATTRIBUTE_DIRECTORY),
                    modified   = modified,
                )
            offset += record_length
        enum_data.StartFileReferenceNumber = next_ref
        if progress_callback and len(entries) % 50000 == 0:
            progress_callback(len(entries))
    return entries


# ── MFT 열거 공개 API ─────────────────────────────────────────────────────────

def enumerate_mft(drive_letter: str, progress_callback=None,
                   exclude_fn=None) -> MftScanResult:
    """NTFS MFT 전체를 열거하여 파일 엔트리 목록을 반환한다.

    1차: MFT 직접 파싱 ($DATA/$STANDARD_INFORMATION 에서 size/modified 추출)
    2차: 실패 시 FSCTL_ENUM_USN_DATA 폴백

    Args:
        drive_letter:      드라이브 문자 (예: 'C')
        progress_callback: callable(entries_so_far: int) — 50,000개마다 호출
        exclude_fn:        callable(path: str) -> bool — 제외 경로 판별 콜백.

    Returns:
        MftScanResult (success=False면 error 필드에 사유 기술)
    """
    result = MftScanResult(drive=drive_letter)

    handle = _open_volume(drive_letter)
    if handle is None:
        result.error = f"{drive_letter}: 볼륨을 열 수 없습니다"
        return result

    try:
        journal = _query_usn_journal(handle)
        if journal is None:
            result.error = f"{drive_letter}: USN Journal 조회 실패"
            return result

        result.journal_id = journal.UsnJournalID
        result.next_usn   = journal.NextUsn

        # ── 1차: MFT 직접 파싱 (size/modified 포함) ───────────────────
        entries = None
        vol_data = _get_ntfs_volume_data(handle)
        if vol_data and vol_data.BytesPerFileRecordSegment > 0:
            try:
                entries = _enumerate_mft_records(handle, vol_data, progress_callback)
                logger.info("MFT 직접 파싱 성공: %s:\\ %d개 레코드",
                            drive_letter, len(entries))
            except Exception:
                logger.warning("MFT 직접 파싱 실패, USN 열거로 폴백",
                               exc_info=True)

        # ── 2차: 폴백 — FSCTL_ENUM_USN_DATA ──────────────────────────
        if entries is None:
            entries = _enumerate_mft_usn(handle, journal, progress_callback)
            logger.info("USN 열거 완료: %s:\\ %d개 엔트리",
                        drive_letter, len(entries))

        # 모든 엔트리의 전체 경로 재구성 (parent_ref 체인 추적)
        _resolve_paths(entries, root_prefix=f"{drive_letter}:\\",
                       exclude_fn=exclude_fn)

        result.files         = list(entries.values())
        result.total_entries = len(result.files)
        result.success       = True

    except Exception:
        logger.exception("MFT 열거 중 예외 발생 (%s:\\)", drive_letter)
        result.error = "MFT 열거 중 예외 발생"
    finally:
        CloseHandle(handle)

    return result


def _resolve_paths(entries: dict[int, MftFileEntry], root_prefix: str,
                   exclude_fn=None):
    """parent_ref 체인을 재귀적으로 따라 각 엔트리의 full_path를 채운다.

    path_cache 로 동일 폴더의 형제 파일들이 부모 경로를 공유하게 한다.
    depth > 256 이면 순환 참조로 판단하고 빈 경로를 반환한다.
    exclude_fn 이 주어지면, 제외 대상 경로를 조기에 차단하여
    하위 엔트리 전체를 건너뛴다.
    """
    path_cache: dict[int, str | None] = {}

    def _get_path(ref: int, depth: int = 0) -> str | None:
        if depth > 256:
            return ""  # 순환 참조 방지
        if ref in path_cache:
            return path_cache[ref]

        entry = entries.get(ref)
        if entry is None or entry.parent_ref == ref:
            # 엔트리 없음 또는 자기 자신을 부모로 가리키는 경우(루트 디렉터리) → 루트로 처리
            path_cache[ref] = root_prefix
            return root_prefix

        parent_path = _get_path(entry.parent_ref, depth + 1)
        if parent_path is None:
            # 부모가 제외됨 → 자식도 제외
            path_cache[ref] = None
            return None

        full = os.path.join(parent_path or root_prefix, entry.name)

        if exclude_fn and exclude_fn(full):
            path_cache[ref] = None
            return None

        path_cache[ref]  = full
        entry.full_path  = full
        return full

    for ref in list(entries.keys()):
        _get_path(ref)


# ── USN Journal 증분 변경 감지 ────────────────────────────────────────────────

def _resolve_one_path(root_handle, file_ref: int) -> str | None:
    """파일 참조 번호 → 전체 경로를 OpenFileById로 조회한다 (내부 전용).

    root_handle: 드라이브 루트 디렉토리 핸들 (예: C:\\ 를 CreateFileW로 연 것)
    file_ref:    시퀀스 포함 전체 64비트 MFT FileReferenceNumber
    """
    fid = FILE_ID_DESCRIPTOR()
    fid.dwSize = ctypes.sizeof(FILE_ID_DESCRIPTOR)
    fid.Type   = 0  # FileIdType
    fid.FileId = file_ref  # unsigned 64-bit MFT ref

    fh = _OpenFileById(
        root_handle,
        ctypes.byref(fid),
        0,  # dwDesiredAccess: 경로 조회만
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        FILE_FLAG_BACKUP_SEMANTICS,
    )
    if fh == INVALID_HANDLE_VALUE:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        ret = _GetFinalPathNameByHandleW(fh, buf, 32768, VOLUME_NAME_DOS)
        if ret > 0:
            path = buf.value
            # \\?\ 접두사 제거
            return path[4:] if path.startswith("\\\\?\\") else path
        return None
    finally:
        CloseHandle(fh)


def _open_root_dir(drive_letter: str):
    """드라이브 루트 디렉토리 핸들을 열어 반환한다. OpenFileById의 hVolumeHint에 사용."""
    root_path = f"{drive_letter}:\\"
    handle = CreateFileW(
        root_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        err = kernel32.GetLastError()
        logger.error("루트 디렉토리 열기 실패 %s (오류 코드 %d)", root_path, err)
        return None
    return handle


def resolve_paths_by_refs(drive_letter: str, file_refs: set[int],
                          raw_refs: dict[int, int] | None = None) -> dict[int, str]:
    """여러 파일 참조 번호를 한 번의 볼륨 핸들 오픈으로 일괄 경로 조회한다.

    Args:
        file_refs: 48비트 MFT 참조 번호 집합 (캐시/DB 키용)
        raw_refs:  {48bit_ref: 64bit_raw_ref} 매핑 (경로 해석용, 없으면 48bit 사용)

    Returns:
        {file_ref: full_path} — 조회 실패한 ref 는 포함되지 않음
    """
    result: dict[int, str] = {}
    if not file_refs:
        return result

    handle = _open_root_dir(drive_letter)
    if handle is None:
        return result
    try:
        for ref in file_refs:
            open_ref = raw_refs.get(ref, ref) if raw_refs else ref
            path = _resolve_one_path(handle, open_ref)
            if path:
                result[ref] = path
    finally:
        CloseHandle(handle)
    return result


def read_usn_changes(
    drive_letter: str,
    start_usn: int,
    journal_id: int,
    progress_callback=None,
) -> tuple[list[UsnChange], int] | tuple[None, None]:
    """USN Journal 에서 start_usn 이후 변경된 파일 목록을 읽는다.

    변경 레코드를 다 읽은 뒤 새 next_usn 도 반환한다.

    Returns:
        (changes, new_next_usn) — 정상 완료
        (None, None)            — Journal 재생성 또는 USN 만료 → 전체 재스캔 필요
    """
    handle = _open_volume(drive_letter)
    if handle is None:
        return None, None

    try:
        journal = _query_usn_journal(handle)
        if journal is None:
            return None, None

        # Journal ID 가 다르면 볼륨이 재포맷된 것 → 전체 재스캔
        if journal.UsnJournalID != journal_id:
            logger.warning("%s: USN Journal ID 변경됨 → 전체 재스캔 필요", drive_letter)
            return None, None

        # 저장된 USN 이 유효 범위 밖이면 만료된 것 → 전체 재스캔
        if start_usn < journal.LowestValidUsn:
            logger.warning(
                "%s: 저장된 USN(%d) 만료 (LowestValid=%d) → 전체 재스캔 필요",
                drive_letter, start_usn, journal.LowestValidUsn,
            )
            return None, None

        read_data = _ReadUsnJournalData()
        read_data.StartUsn          = start_usn
        read_data.ReasonMask        = _REASON_MASK
        read_data.ReturnOnlyOnClose = 0
        read_data.Timeout           = 0
        read_data.BytesToWaitFor    = 0
        read_data.UsnJournalID      = journal_id

        changes: list[UsnChange] = []
        output_buffer  = ctypes.create_string_buffer(BUFFER_SIZE)
        bytes_returned = wintypes.DWORD(0)

        while True:
            ok = DeviceIoControl(
                handle, FSCTL_READ_USN_JOURNAL,
                ctypes.byref(read_data), ctypes.sizeof(read_data),
                output_buffer, BUFFER_SIZE,
                ctypes.byref(bytes_returned), None,
            )
            if not ok:
                err = kernel32.GetLastError()
                if err == ERROR_HANDLE_EOF:
                    break  # 정상 종료
                if err == ERROR_JOURNAL_ENTRY_DELETED:
                    logger.warning("%s: USN 항목 만료 → 전체 재스캔 필요", drive_letter)
                    return None, None
                logger.error(
                    "%s: FSCTL_READ_USN_JOURNAL 실패 (오류 코드 %d)", drive_letter, err
                )
                break

            returned = bytes_returned.value
            if returned <= 8:
                break

            # 버퍼 앞 8바이트: 다음 StartUsn
            next_usn_val = ctypes.c_longlong.from_buffer_copy(output_buffer, 0).value
            read_data.StartUsn = next_usn_val

            offset = 8
            while offset < returned:
                if offset + 4 > returned:
                    break
                record_length = int.from_bytes(
                    output_buffer[offset:offset + 4], "little"
                )
                if record_length == 0 or offset + record_length > returned:
                    break

                rec    = output_buffer[offset:offset + record_length]
                parsed = _parse_usn_record(rec)
                if parsed:
                    file_ref, parent_ref, _, reason, name, _modified = parsed
                    # 경로 해석에는 시퀀스 포함 원본 64비트가 필요
                    raw_ref = int.from_bytes(rec[8:16], "little")
                    changes.append(UsnChange(
                        file_ref=file_ref, parent_ref=parent_ref,
                        name=name, reason=reason,
                        raw_file_ref=raw_ref,
                    ))

                offset += record_length

            if progress_callback:
                progress_callback(len(changes))

        return changes, journal.NextUsn

    except Exception:
        logger.exception("%s: USN 변경 읽기 중 예외", drive_letter)
        return None, None
    finally:
        CloseHandle(handle)
