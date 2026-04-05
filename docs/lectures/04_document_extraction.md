# 4강: 문서 텍스트 추출 (Document Extraction)

## 개요

SeekSeek은 파일명 검색뿐만 아니라 **문서 내부의 텍스트 내용**까지 검색한다.
이를 위해 다양한 문서 포맷에서 텍스트를 추출하는 **document extraction** 파이프라인을 갖추고 있다.

지원 포맷: PDF, DOCX, XLSX, PPTX, HWPX, HWP, TXT 및 기타 텍스트 기반 파일

---

## 1. 추출 아키텍처

```
파일 경로 입력
     │
     ▼
extract_text(path)
     │
     ├── 확장자 판별
     │
     ├── .pdf  → _extract_pdf()    [PyMuPDF/fitz]
     ├── .docx → _extract_docx()   [python-docx]
     ├── .xlsx → _extract_xlsx()   [openpyxl]
     ├── .pptx → _extract_pptx()   [python-pptx → ZIP fallback]
     ├── .hwpx → _extract_hwpx()   [ZIP + XML 파싱]
     ├── .hwp  → _extract_hwp()    [OLE2 + 바이너리 태그 파싱]
     └── 기타  → _extract_text()   [텍스트 파일 직접 읽기]
     │
     ▼
추출된 텍스트 문자열 (str | None)
```

### 크기 제한

```python
MAX_CONTENT_SIZE = 200 * 1024 * 1024  # 200 MB — 이보다 큰 파일은 건너뜀
MAX_PREVIEW_SIZE = 100 * 1024         # 100 KB — 미리보기용 텍스트 길이 제한
```

---

## 2. PDF 추출 (PyMuPDF/fitz)

**PyMuPDF**(패키지명: `fitz`)는 PDF 렌더링 라이브러리 MuPDF의 Python 바인딩이다.

```python
import fitz  # PyMuPDF

def _extract_pdf(path: str) -> str | None:
    """PDF 파일에서 모든 페이지의 텍스트를 추출한다."""
    doc = fitz.open(path)
    texts = []
    for page in doc:
        texts.append(page.get_text())  # 페이지별 텍스트 반환
    doc.close()
    return "\n".join(texts) or None
```

**동작 원리**:
- PDF의 텍스트 객체(text object)를 파싱하여 글리프(glyph) 순서대로 텍스트 추출
- 이미지 내 텍스트(OCR 필요)는 추출되지 않음
- 암호화된 PDF는 비밀번호 없이 열 수 없을 수 있음

---

## 3. DOCX 추출 (python-docx)

DOCX는 실제로는 **ZIP 아카이브** 안에 XML 파일들이 들어있는 구조다.

```
sample.docx (ZIP)
├── [Content_Types].xml
├── _rels/.rels
├── word/
│   ├── document.xml    ← 본문 텍스트
│   ├── styles.xml
│   ├── fontTable.xml
│   └── ...
└── docProps/
    ├── app.xml
    └── core.xml
```

```python
from docx import Document

def _extract_docx(path: str) -> str | None:
    """DOCX 파일에서 모든 문단의 텍스트를 추출한다."""
    doc = Document(path)
    texts = [p.text for p in doc.paragraphs]
    return "\n".join(texts) or None
```

---

## 4. XLSX 추출 (openpyxl)

```python
from openpyxl import load_workbook

def _extract_xlsx(path: str) -> str | None:
    """XLSX 파일에서 모든 시트의 셀 값을 추출한다."""
    wb = load_workbook(path, read_only=True, data_only=True)
    texts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for cell in row:
                if cell is not None:
                    texts.append(str(cell))
    wb.close()
    return " ".join(texts) or None
```

- `read_only=True`: 메모리 효율을 위해 읽기 전용 모드
- `data_only=True`: 수식 대신 계산된 값을 반환

---

## 5. PPTX 추출 (이중 전략)

### 전략 1: python-pptx 라이브러리

```python
from pptx import Presentation

def _extract_pptx_primary(path: str) -> str | None:
    """python-pptx로 슬라이드 텍스트를 추출한다."""
    prs = Presentation(path)
    texts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    texts.append(para.text)
    return "\n".join(texts) or None
```

### 전략 2: ZIP + XML 파싱 (Fallback)

python-pptx가 실패하면, ZIP 내부의 XML을 직접 파싱한다:

```python
import zipfile
import xml.etree.ElementTree as ET

def _extract_pptx_fallback(path: str) -> str | None:
    """ZIP 구조에서 직접 XML을 파싱하여 텍스트를 추출한다."""
    with zipfile.ZipFile(path) as z:
        texts = []
        for name in sorted(z.namelist()):
            if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                xml_data = z.read(name)
                root = ET.fromstring(xml_data)
                # 모든 텍스트 노드를 추출
                for elem in root.iter():
                    if elem.text:
                        texts.append(elem.text.strip())
    return "\n".join(texts) or None
```

---

## 6. HWPX 추출 (한컴 오피스 신규 포맷)

HWPX는 한컴 오피스의 **OOXML 기반** 문서 포맷으로, ZIP 아카이브 내부에 XML이 있다.

```
sample.hwpx (ZIP)
├── META-INF/
├── Contents/
│   ├── content.hpf     ← 본문 XML 목록
│   ├── section0.xml    ← 본문 텍스트 (섹션별)
│   ├── section1.xml
│   └── ...
└── settings.xml
```

```python
import zipfile
import xml.etree.ElementTree as ET

def _extract_hwpx(path: str) -> str | None:
    """HWPX 파일에서 섹션별 XML의 텍스트를 추출한다."""
    with zipfile.ZipFile(path) as z:
        texts = []
        for name in sorted(z.namelist()):
            if "section" in name.lower() and name.endswith(".xml"):
                xml_data = z.read(name)
                root = ET.fromstring(xml_data)
                for elem in root.iter():
                    if elem.text and elem.text.strip():
                        texts.append(elem.text.strip())
    return "\n".join(texts) or None
```

---

## 7. HWP 추출 (레거시 바이너리 포맷)

HWP(아래아 한글 문서)는 **OLE2(Compound File)** 기반의 바이너리 포맷이다.
이것이 가장 복잡한 추출 대상이다.

### OLE2 구조

```
sample.hwp (OLE2 Compound File)
├── FileHeader           ← 파일 버전, 압축 플래그
├── DocInfo              ← 문서 정보
├── BodyText/
│   ├── Section0         ← 본문 섹션 (zlib 압축)
│   ├── Section1
│   └── ...
├── BinData/              ← 이미지 등 바이너리 데이터
└── Scripts/              ← 매크로 스크립트
```

### 태그 레코드 구조

HWP 본문은 **태그 레코드(tag record)** 의 연속으로 이루어진다:

```
태그 레코드 헤더 (4 bytes):
┌────────────────────────────────────────┐
│  비트 31~20: 태그 ID (12 bits)         │
│  비트 19~10: 레벨 (10 bits)            │
│  비트  9~ 0: 크기 (10 bits)            │
│  → 크기가 0x3FF이면 다음 4바이트가     │
│    실제 크기 (확장 크기)               │
└────────────────────────────────────────┘
```

### HWPTAG_PARA_TEXT (태그 ID = 67)

문단 텍스트는 태그 ID 67번에 UTF-16LE로 저장된다:

```python
HWPTAG_PARA_TEXT = 67  # 문단 텍스트 태그

def _extract_text_from_hwp_body(data: bytes) -> str:
    """HWP 태그 레코드에서 HWPTAG_PARA_TEXT를 찾아 텍스트를 추출한다."""
    texts = []
    pos = 0
    while pos + 4 <= len(data):
        header = struct.unpack_from('<I', data, pos)[0]
        tag_id = header & 0x3FF          # 하위 10비트... 가 아니라
        # 실제로는:
        tag_id = (header >> 20) & 0xFFF  # 상위 12비트
        size = header & 0x3FF            # 하위 10비트
        pos += 4
        
        if size == 0x3FF:  # 확장 크기
            size = struct.unpack_from('<I', data, pos)[0]
            pos += 4
        
        if tag_id == HWPTAG_PARA_TEXT:
            # UTF-16LE 텍스트에서 제어 문자 필터링
            raw = data[pos:pos+size]
            text = _filter_hwp_control_chars(raw)
            if text:
                texts.append(text)
        
        pos += size
    
    return "\n".join(texts)
```

### HWP 제어 문자 필터링

HWP 텍스트에는 특수 제어 문자(인라인 이미지, 표 등)가 16비트 코드로 삽입된다:

```
제어 문자 범위:
  0x0000~0x001F: 일반 제어 문자 (대부분 무시)
  0x0001: 예약
  0x0002: 섹션/열 정의
  0x0003: 필드 시작
  ...
  0x000D: 줄 바꿈 (\r) → "\n"으로 변환
  0x000A: 줄 바꿈 (\n) → 유지

필터링 전략: 0x0020 미만의 글리프는 제외 (줄바꿈만 유지)
```

### zlib 압축 해제

HWP 본문 섹션은 일반적으로 **zlib 압축**되어 있다:

```python
import zlib

def _hwp_decompress(data: bytes) -> bytes:
    """HWP 섹션 데이터의 zlib 압축을 해제한다.
    
    3가지 방식을 순차 시도:
      1. Raw deflate (wbits=-15)     — 헤더 없는 deflate
      2. Wrapped deflate (wbits=15)  — zlib 헤더 포함
      3. gzip auto-detect (wbits=47) — gzip 헤더 자동 감지
    """
    for wbits in (-15, 15, 47):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            continue
    raise ValueError("모든 zlib 해제 방식 실패")
```

> **왜 3가지 방식?**: HWP 버전과 생성 도구에 따라 압축 헤더 형식이 다를 수 있다.
> SeekSeek은 가장 넓은 호환성을 위해 3가지 방식을 순차적으로 시도한다.

---

## 8. 텍스트 파일 추출

확장자가 CONTENT_EXTENSIONS에 포함된 텍스트 기반 파일은 직접 읽는다:

```python
def _extract_text(path: str) -> str | None:
    """일반 텍스트 파일을 읽어 내용을 반환한다."""
    encodings = ['utf-8', 'cp949', 'euc-kr', 'latin-1']
    for enc in encodings:
        try:
            with open(path, 'r', encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None
```

한국어 환경에서는 `cp949`(EUC-KR 확장)가 자주 사용되므로 다중 인코딩 시도가 중요하다.

---

## 9. 병렬 추출 파이프라인

대량의 파일을 처리할 때, SeekSeek은 **ThreadPoolExecutor**로 병렬 추출한다:

```python
from concurrent.futures import ThreadPoolExecutor

class ContentReindexThread(QThread):
    """문서 텍스트 추출 + DB 인덱싱을 수행하는 백그라운드 스레드"""
    
    def run(self):
        # 병렬 추출 (I/O 바운드 작업에 적합)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(extract_text, path): file_id
                for file_id, path in pending_files
            }
            # 결과를 순차적으로 DB에 저장 (SQLite는 단일 writer)
            for future in as_completed(futures):
                file_id = futures[future]
                text = future.result()
                if text:
                    upsert_content(file_id, text)
```

> **핵심**: 텍스트 추출은 I/O 바운드이므로 멀티스레드가 효과적이다.
> 하지만 SQLite 쓰기는 단일 스레드로 직렬화한다 (SQLite의 단일 writer 제약).

---

## 10. SeekSeek에서의 구현 위치

| 구현 요소 | 파일 | 함수 |
|-----------|------|------|
| 추출 라우터 | `core/extractor.py` | `extract_text()` |
| PDF 추출 | `core/extractor.py` | `_extract_pdf()` |
| DOCX 추출 | `core/extractor.py` | `_extract_docx()` |
| XLSX 추출 | `core/extractor.py` | `_extract_xlsx()` |
| PPTX 추출 | `core/extractor.py` | `_extract_pptx()` |
| HWPX 추출 | `core/extractor.py` | `_extract_hwpx()` |
| HWP 추출 | `core/extractor.py` | `_extract_hwp()` |
| zlib 해제 | `core/extractor.py` | `_hwp_decompress()` |
| 병렬 실행 | `core/scanner.py` | `ContentReindexThread.run()` |
| 지원 확장자 | `config.py` | `CONTENT_EXTENSIONS` |
| 크기 제한 | `config.py` | `MAX_CONTENT_SIZE` |

---

## 참고 자료

- [PyMuPDF 공식 문서](https://pymupdf.readthedocs.io/)
- [python-docx 공식 문서](https://python-docx.readthedocs.io/)
- [한컴 HWP 바이너리 포맷 명세](https://www.hancom.com/etc/hwpDownload.do)
- [OLE2 Compound File 구조](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-cfb/)
