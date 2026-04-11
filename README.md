# SeekSeek

> **Windows용 초고속 파일 검색·내용 검색 도구**
>
> NTFS MFT를 직접 파싱하여 수백만 파일을 순식간에 인덱싱하고,
> SQLite FTS5 역색인으로 문서 내용까지 검색합니다.

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| **MFT 직접 스캔** | NTFS 볼륨의 Master File Table을 직접 읽어 전체 파일 목록을 수초 만에 수집 |
| **USN Journal 증분 업데이트** | 변경 저널을 5초 주기로 폴링하여 파일 생성·삭제·수정을 실시간 반영 |
| **FTS5 전문 검색** | SQLite FTS5 역색인 기반 파일명 + 본문 동시 검색, BM25 랭킹 |
| **문서 내용 추출** | PDF, DOCX, XLSX, PPTX, HWP, HWPX, 텍스트 파일 등 40여 종 지원 |
| **메모리 안정화 색인** | 본문 색인 시 추출 결과를 소배치로 즉시 DB 반영하여 메모리 피크 완화 |
| **본문 미리보기** | 검색 키워드 하이라이팅, Ctrl+F 미리보기 내 재검색 |
| **폴더별 인덱싱** | 특정 폴더의 문서 내용을 선택적으로 인덱싱 |
| **제외 폴더 관리** | node_modules, $Recycle.Bin 등 고성능 필터링 |

### 색인 동작 메모 (v1.0.1+)

- 본문 색인 워커 수: CPU 기반 `2~4`개로 제한
- 추출 결과: 소배치 단위 즉시 DB flush (대용량 텍스트 누적 방지)
- 전체 색인 + 변경분 색인: 동시 실행 대신 직렬 실행
- `MAX_CONTENT_SIZE` 기본값: `200MB` 유지

---

## 스크린샷

<p align="center">
  <img src="docs/introduce.gif" alt="SeekSeek 데모" width="800">
</p>

---

## 설치 및 실행

### 요구 사항

- **Windows 10 / 11** (NTFS 파일시스템)
- **Python 3.10+** (소스 직접 실행 시)
- **관리자 권한** (MFT 접근에 필요)

### 소스에서 실행

```bash
# 1. 저장소 클론
git clone <repository-url>
cd everythingthing

# 2. 가상환경 생성 및 활성화
python -m venv .venv
.venv\Scripts\activate

# 3. 의존성 설치
pip install -r requirements.txt

# 4. 관리자 권한으로 실행 (자동 UAC 요청)
python main.py
```

### 설치 프로그램

[Releases](../../releases) 페이지에서 `SeekSeek_Setup.exe`를 다운로드하여 설치하세요.

---

## 기술 스택

```
┌─────────────────────────────────────────┐
│              GUI 계층 (PyQt6)           │
│  MainWindow / ResultTableModel / 다이얼로그 │
├─────────────────────────────────────────┤
│           검색 엔진 (searcher.py)        │
│   MFT 캐시 모드 ←→ FTS5 DB 모드         │
├─────────────────────────────────────────┤
│            인덱싱 계층                    │
│  indexer.py (SQLite FTS5) + extractor.py │
├─────────────────────────────────────────┤
│          MFT / USN Journal               │
│  mft_scanner.py (ctypes + kernel32)      │
├─────────────────────────────────────────┤
│         NTFS 볼륨 (Windows)              │
└─────────────────────────────────────────┘
```

---

## 프로젝트 구조

```
everythingthing/
├── main.py              # 진입점 (관리자 권한, QApplication)
├── config.py            # 전역 설정
├── build.py             # PyInstaller 빌드 스크립트
├── requirements.txt     # Python 의존성
│
├── core/                # 핵심 로직
│   ├── mft_scanner.py   # MFT 파싱, USN Journal
│   ├── mft_cache.py     # Everything식 메모리 캐시
│   ├── indexer.py        # SQLite FTS5 DB
│   ├── searcher.py       # 이중 모드 검색 엔진
│   ├── scanner.py        # QThread 워커
│   └── extractor.py      # 문서 텍스트 추출
│
├── gui/                 # GUI
│   ├── main_window.py   # 메인 윈도우
│   └── dialogs.py       # 다이얼로그
│
├── assets/              # 아이콘 등 리소스
├── docs/                # 문서
│   ├── introduce.gif    # 소개 GIF
│   ├── lectures/        # 기술 강의 (01~08)
│   └── tutorials/       # 실습 자료
│
├── seekseek.spec        # PyInstaller 빌드
└── installer.iss        # Inno Setup 설치
```

---

## 문서

- **[강의 자료](docs/lectures/)** — MFT 구조부터 빌드·배포까지 8개 강의
- **[실습 자료](docs/tutorials/)** — Jupyter 노트북 형식 튜토리얼

---

## 라이선스

이 프로젝트는 [LICENSE](LICENSE) 파일을 참조하세요.
