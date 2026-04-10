# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SeekSeek — Windows NTFS 초고속 파일/내용 검색 도구. MFT 직접 파싱으로 수백만 파일을 수 초 내 인덱싱하고, SQLite FTS5로 문서 본문까지 검색한다. PyQt6 GUI 제공.

- **OS**: Windows 10/11 전용 (NTFS, 관리자 권한 필수)
- **Python**: 3.11
- **언어**: 한국어 (UI, 주석, 커밋 메시지)

## Development Environment

```bash
C:\Users\vaiv\anaconda3\envs\evt\Scripts\activate  # 가상환경 활성화
python main.py              # 앱 실행 (관리자 권한 필요)
```

## Build & Release

```bash
python build.py              # EXE + Portable ZIP + Installer 전체 빌드
python build.py --portable   # ZIP만
python build.py --installer  # Inno Setup 인스톨러만
```

CI/CD: `.github/workflows/build-release.yml` — `v*` 태그 푸시 시 자동 빌드+릴리즈

```bash
git tag v1.x.x
git push origin master --tags
```

## Architecture

### Dual-Index Strategy
- **In-Memory Cache** (`core/mft_cache.py`): 파일명 검색용, dict 기반 O(1) lookup, ~1M 파일 <50ms
- **SQLite FTS5 DB** (`core/indexer.py`): 파일명+내용 검색, BM25 랭킹, external content mode

### Two-Phase Scan
1. **Full Scan**: MFT 직접 파싱 (`core/mft_scanner.py`) → 전체 파일 열거
2. **Incremental**: USN Journal 5초 폴링 → 변경분만 반영

### Threading Model (`core/scanner.py`)
- `ScannerThread`: MFT 스캔 (full/cache-only 모드)
- `USNMonitorThread`: 5초 폴링, mft_cache에만 반영 (DB 아님)
- `ContentReindexThread`: ThreadPoolExecutor(4-8) 병렬 텍스트 추출, DB 순차 쓰기
- `FolderIndexThread`: 폴더 단위 인덱싱

PyQt6 signals/slots로 스레드 간 통신.

### Document Extraction (`core/extractor.py`)
PDF(fitz), DOCX(python-docx), XLSX(openpyxl), PPTX(python-pptx + ZIP fallback), HWP(olefile OLE2), HWPX(ZIP+XML), 텍스트(40종). 라이브러리 미설치 시 해당 포맷만 건너뜀.

## Key Paths

- 앱 데이터: `%LOCALAPPDATA%/SeekSeek/`
- DB: `%LOCALAPPDATA%/SeekSeek/index.db`
- 설정: `%LOCALAPPDATA%/SeekSeek/settings.json`
- 로그: `%LOCALAPPDATA%/SeekSeek/debug.log`

## Build Notes

- `seekseek.spec`: PyInstaller 스펙. `collect_submodules('pptx')` + `collect_data_files('pptx')` 사용
- `build.py`의 `strip_bloat()`: 빌드 후 불필요 DLL/폴더 제거 (Qt 플러그인, PIL, crypto 등)
- `build.py`의 `verify_bundled_packages()`: 주요 패키지 번들 여부 검증 (pptx, lxml, fitz 등)
- `excludes`에 lxml.html, PIL, tkinter, numpy 등 제외 — pptx가 lxml.etree에 의존하므로 lxml 자체는 유지

## Conventions

- 커밋 메시지: 한국어, `fix:` / `feat:` / `refactor:` 접두사
- 선택적 의존성: try/except ImportError로 graceful degradation
- SQLite: WAL 모드, NORMAL sync, 30s busy_timeout
