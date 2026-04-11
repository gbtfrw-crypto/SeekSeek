# 8강: 빌드·배포와 전체 흐름도

## 개요

이 강의에서는 SeekSeek의 빌드·배포 파이프라인을 설명하고,
마지막으로 각 핵심 기능의 **흐름도(flowchart)**를 Mermaid 다이어그램으로 정리한다.

---

## 1. 빌드 파이프라인

### PyInstaller (seekseek.spec)

SeekSeek은 **PyInstaller**를 사용하여 단일 실행 파일(또는 폴더)로 패키징한다.

```
소스 코드 (.py)
     │
     ▼
PyInstaller (seekseek.spec)
     │
     ├── Python 인터프리터 내장
     ├── 의존 패키지 수집 (PyQt6, fitz, docx, openpyxl 등)
     ├── assets 폴더 복사
     └── 실행 파일 생성
     │
     ▼
dist/seekseek/ (또는 dist/seekseek.exe)
```

### Inno Setup (installer.iss)

Windows 설치 프로그램은 **Inno Setup**으로 생성한다:

```
PyInstaller 출력 (dist/seekseek/)
     │
     ▼
Inno Setup (installer.iss)
     │
     ├── 설치 경로 설정
     ├── 시작 메뉴 바로가기
     ├── 언인스톨러 포함
     └── 인스톨러 .exe 생성
     │
     ▼
SeekSeek_Setup.exe
```

---

## 2. 의존성 관리 (requirements.txt)

```
PyQt6>=6.5          # GUI 프레임워크
PyMuPDF>=1.23       # PDF 텍스트 추출
python-docx>=0.8    # DOCX 텍스트 추출
openpyxl>=3.1       # XLSX 텍스트 추출
python-pptx>=0.6    # PPTX 텍스트 추출
olefile>=0.46       # HWP(OLE2) 파일 파싱
```

---

## 3. 관리자 권한 처리

MFT 직접 파싱과 볼륨 핸들 접근에는 **관리자 권한**이 필수다:

```python
# main.py
def _is_admin() -> bool:
    """현재 프로세스가 관리자 권한으로 실행 중인지 확인"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def _relaunch_as_admin():
    """UAC 대화상자를 표시하여 관리자 권한으로 재실행"""
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas",
        sys.executable,       # python.exe 경로
        " ".join(sys.argv),   # 인자
        None, 1               # SW_SHOWNORMAL
    )
    sys.exit()
```

---

## 4. 전체 흐름도 (Mermaid Diagrams)

### 4.1. 앱 시작 흐름

```mermaid
flowchart TD
    A[앱 실행] --> B{관리자 권한?}
    B -- No --> C[UAC 재실행 요청]
    C --> D[프로세스 종료]
    B -- Yes --> E[QApplication 초기화]
    E --> F[DB 초기화 init_db]
    F --> G[MainWindow 생성]
    G --> H{DB에 캐시 존재?}
    H -- Yes --> I[ScannerThread\ncache_only=True]
    H -- No --> J[ScannerThread\ncache_only=False]
    I --> K[캐시 로드 완료]
    J --> L[MFT 전체 스캔 완료]
    K --> M[USNMonitorThread 시작]
    L --> N[캐시 구축 + DB 저장]
    N --> M
    M --> O[메인 이벤트 루프]
```

### 4.2. MFT 스캔 흐름

```mermaid
flowchart TD
    A[enumerate_mft 호출] --> B[NTFS 드라이브 목록 수집]
    B --> C{각 드라이브}
    C --> D[볼륨 핸들 열기\nCreateFileW]
    D --> E{직접 파싱 시도}
    E -- 성공 --> F[FSCTL_GET_NTFS_VOLUME_DATA]
    F --> G[MFT $DATA data runs 파싱]
    G --> H[MFT 레코드 순차 읽기]
    H --> I[fixup 배열 복원]
    I --> J[속성 파싱\n$FILE_NAME\n$STANDARD_INFORMATION\n$DATA]
    J --> K[MftFileEntry 생성]
    E -- 실패 --> L[FSCTL_QUERY_USN_JOURNAL]
    L --> M[FSCTL_ENUM_USN_DATA\n반복 호출]
    M --> N[USN_RECORD_V2 파싱]
    N --> K
    K --> O[_resolve_paths\nparent_ref 체인으로\n전체 경로 구성]
    O --> P[MftScanResult 반환]
```

### 4.3. USN 증분 업데이트 흐름

```mermaid
flowchart TD
    A[USNMonitorThread._poll] --> B[DB에서 usn_state 로드\njournal_id, start_usn]
    B --> C[read_usn_changes 호출]
    C --> D[FSCTL_READ_USN_JOURNAL]
    D --> E{결과?}
    E -- "ERROR_HANDLE_EOF" --> F[변경 없음\n다음 폴링으로]
    E -- "ERROR_JOURNAL_ENTRY_DELETED" --> G[전체 재스캔\n트리거]
    E -- "성공" --> H[USN_RECORD_V2 파싱]
    H --> I[UsnChange 리스트 생성]
    I --> J[_apply_usn_changes]
    J --> K{reason 분석}
    K -- FILE_DELETE --> L[mft_cache에서 삭제]
    K -- FILE_CREATE --> M[경로 해석 → mft_cache 추가]
    K -- DATA_CHANGE --> N[mft_cache 항목 갱신]
    K -- RENAME --> O[구 경로 제거\n신 경로 추가]
    L --> P[new_usn 저장]
    M --> P
    N --> P
    O --> P
    P --> Q[updated + paths_updated\n시그널 발신]
    Q --> R[5초 대기\n→ _poll 재호출]
```

### 4.4. 검색 흐름

```mermaid
flowchart TD
    A[사용자 검색 입력] --> B{content_query\n있음?}
    B -- No --> C[MFT 캐시 모드]
    C --> D[MftCache.search\n글롭 패턴 매칭]
    D --> E[SearchResult 리스트]
    B -- Yes --> F[DB FTS5 모드]
    F --> G[_build_fts_query\n토큰 → 접두사* 변환]
    G --> H[fts_files MATCH\n파일명 검색]
    G --> I[fts_contents MATCH\n내용 검색]
    H --> J[결과 통합]
    I --> J
    J --> K{folder 필터?}
    K -- Yes --> L[path LIKE 필터 적용]
    K -- No --> M[전체 결과]
    L --> M
    M --> N[우선순위 정렬\nboth > filename > content]
    N --> E
    E --> O[ResultTableModel.set_results]
    O --> P[QTableView 갱신]
```

### 4.5. 콘텐츠 인덱싱 흐름

```mermaid
flowchart TD
    A[인덱싱 시작\nFolderIndexThread] --> B[os.walk로\n파일 목록 수집]
    B --> C[제외 폴더 필터링]
    C --> D[파일 메타데이터\nDB upsert]
    D --> E[ContentReindexThread\n시작]
    E --> F[has_content=0인\n파일 목록 조회]
    F --> G[ThreadPoolExecutor\nmax_workers=4]
    G --> H1[Worker 1\nextract_text PDF]
    G --> H2[Worker 2\nextract_text DOCX]
    G --> H3[Worker 3\nextract_text HWP]
    G --> H4[Worker 4\nextract_text XLSX]
    H1 --> I[추출 결과 수집]
    H2 --> I
    H3 --> I
    H4 --> I
    I --> J[순차 DB 저장\nupsert_content\nSQLite single writer]
    J --> K{다음 파일?}
    K -- Yes --> G
    K -- No --> L[인덱싱 완료\nfinished 시그널]
```

### 4.6. HWP 텍스트 추출 흐름

```mermaid
flowchart TD
    A[_extract_hwp 호출] --> B[OLE2 파일 열기\nolefile.OleFileIO]
    B --> C{FileHeader\n압축 플래그?}
    C --> D[BodyText/Section0\n스트림 읽기]
    D --> E{압축됨?}
    E -- Yes --> F[zlib 해제 시도\n3가지 wbits]
    F --> F1[raw deflate\nwbits=-15]
    F1 -- 실패 --> F2[wrapped deflate\nwbits=15]
    F2 -- 실패 --> F3[gzip auto\nwbits=47]
    F3 --> G[태그 레코드 파싱]
    F1 -- 성공 --> G
    F2 -- 성공 --> G
    E -- No --> G
    G --> H{태그 ID =\nHWPTAG_PARA_TEXT?}
    H -- Yes --> I[UTF-16LE 디코딩\n제어 문자 필터링]
    H -- No --> J[다음 태그로]
    I --> K[텍스트 수집]
    J --> G
    K --> L[섹션 텍스트 반환]
```

---

## 5. 프로젝트 디렉터리 구조

```
everythingthing/
├── main.py              # 진입점 (관리자 권한 확인, QApplication)
├── config.py            # 전역 설정 (경로, 상수, 제외 목록)
├── requirements.txt     # Python 의존성
├── seekseek.spec        # PyInstaller 빌드 설정
├── installer.iss        # Inno Setup 설치 프로그램 설정
│
├── core/                # 핵심 로직
│   ├── __init__.py
│   ├── mft_scanner.py   # MFT 직접 파싱, USN Journal
│   ├── mft_cache.py     # Everything식 메모리 캐시
│   ├── indexer.py        # SQLite FTS5 DB 관리
│   ├── searcher.py       # 이중 모드 검색 엔진
│   ├── scanner.py        # QThread 워커 스레드
│   └── extractor.py      # 문서 텍스트 추출
│
├── gui/                 # GUI 계층
│   ├── __init__.py
│   ├── main_window.py   # 메인 윈도우, ResultTableModel
│   └── dialogs.py       # 설정/도움말/정보 다이얼로그
│
├── assets/              # 아이콘 등 리소스
│
└── docs/                # 문서
    ├── introduce.gif    # 소개 GIF
    ├── lectures/        # 강의 자료 (이 파일들!)
    └── tutorials/       # 실습 자료
```

---

## 참고 자료

- [PyInstaller 공식 문서](https://pyinstaller.org/)
- [Inno Setup 공식 문서](https://jrsoftware.org/isinfo.php)
- [Mermaid 다이어그램 문법](https://mermaid.js.org/)
