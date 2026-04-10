"""문서 본문 추출 모듈

다양한 파일 형식에서 텍스트를 추출하여 문자열로 반환한다.
지원 형식: PDF, DOCX, XLSX, PPTX, HWPX, HWP, 일반 텍스트(40여 종)

■ 파일 형식별 파싱 전략
  PDF  : PyMuPDF(fitz) — 페이지 단위 텍스트 추출 (레이아웃 유지)
  DOCX : python-docx — 문단(paragraph) 단위 추출
  XLSX : openpyxl read_only 모드 — 셀 단위 텍스트 수집
  PPTX : python-pptx → 실패 시 ZIP 직접 파싱 (regex로 <a:t> 태그 추출)
  HWPX : ZIP+XML — Contents/*.xml 내 <t> 태그 추출 (HWP 다음 세대 포맷)
  HWP  : OLE2 바이너리 — olefile + 태그 레코드 파싱 (HWPTAG_PARA_TEXT) (HWP5 포맷)
  텍스트: UTF-8 → CP949 → EUC-KR → Latin-1 순으로 디코딩 시도

■ 선택적 의존성
  외부 라이브러리(fitz, docx, openpyxl, pptx, olefile)는 선택적 의존성으로,
  설치되지 않은 경우 해당 형식만 건너뛰고 None 을 반환한다.
"""
import os
import re
import zlib
import zipfile
import logging
import xml.etree.ElementTree as ET

import config

try:
    from pptx import Presentation
except ImportError:
    Presentation = None

logger = logging.getLogger(__name__)

# ── HWP 바이너리 파싱 상수 ────────────────────────────────────────────────────
#
# HWP5 문서 구조 — OLE2(Compound File Binary Format) 컨테이너
# ├─ FileHeader       : 문서 식별·버전·압축 플래그 등 메타데이터
# ├─ DocInfo          : 스타일·폰트·섹션 정의 등 문서 정보
# ├─ BodyText/Section0: 첫 번째 섹션의 본문 태그 레코드 스트림
# ├─ BodyText/Section1: 두 번째 섹션 ...
# └─ ...
#
# 태그 레코드 헤더 (4바이트):
#   [tag_id: 10비트] [level: 10비트] [size: 12비트]
#   - tag_id & 0x3FF = 태그 종류  (67 = HWPTAG_PARA_TEXT)
#   - size == 0xFFF → 확장 크기 (다음 4바이트에 실제 크기)
_HWP_TAG_MASK    = 0x3FF        # tag_id 추출용 마스크 (10비트)
_HWP_SIZE_MASK   = 0xFFF        # size 추출용 마스크 (12비트)
_HWP_SIZE_SHIFT  = 20           # size 필드 시작 비트 위치
_HWPTAG_PARA_TEXT = 67           # 문단 텍스트 태그 ID
_HWP_EXT_SIZE_SENTINEL = 0xFFF  # 이 값이면 다음 4바이트가 실제 크기

# HWPTAG_PARA_TEXT 내부 인라인 제어 코드
# 이 코드들은 조판 마1주(change tracking), 내장 그림 등 비텍스트 요소를 나타내며,
# 각 제어 코드 뒤에 12바이트(제어 문자 보조 데이터)가 추가로 따라온다.
_HWP_INLINE_CTRL_CODES = frozenset({1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23})
_HWP_INLINE_CTRL_EXTRA_BYTES = 12  # 제어 문자 보조 데이터 길이 (고정 12바이트)

# PPTX XML <a:t> 텍스트 추출용 정규식
_PPTX_T_RE = re.compile(r"<a:t[^>]*>(.*?)</a:t>", re.DOTALL)
# PPTX 슬라이드 파일명 패턴
_PPTX_SLIDE_RE = re.compile(r"ppt/slides/slide\d+\.xml$")


# ── 공개 API ─────────────────────────────────────────────────────────────────

def extract_text(filepath: str) -> str | None:
    """파일에서 텍스트를 추출한다.

    확장자에 따라 적합한 파서로 라우팅한다.
    파싱 오류나 라이브러리 미설치 시 None 을 반환하며, 예외를 외부로 전파하지 않는다.
    """
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".pdf":   return _extract_pdf(filepath)
        if ext == ".docx":  return _extract_docx(filepath)
        if ext == ".xlsx":  return _extract_xlsx(filepath)
        if ext == ".pptx":  return _extract_pptx(filepath)
        if ext == ".hwpx":  return _extract_hwpx(filepath)
        if ext == ".hwp":   return _extract_hwp(filepath)
        return _extract_plain(filepath)
    except Exception as e:
        logger.debug("내용 추출 실패 %s: %s", filepath, e)
        return None


# ── 형식별 파서 ───────────────────────────────────────────────────────────────

def _extract_plain(filepath: str) -> str | None:
    """파일을 바이트로 한 번 읽은 뒤 UTF-8 → CP949 → EUC-KR → Latin-1 순으로 디코딩."""
    try:
        with open(filepath, "rb") as f:
            raw = f.read(config.MAX_CONTENT_SIZE)
    except OSError:
        return None
    for enc in ("utf-8", "cp949", "euc-kr", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None


def _extract_pdf(filepath: str) -> str | None:
    try:
        import fitz
    except ImportError:
        logger.warning("PyMuPDF 미설치 — PDF 내용 인덱싱 건너뜀")
        return None
    doc = fitz.open(filepath)
    try:
        texts = [t for page in doc if (t := page.get_text())]
    finally:
        doc.close()
    return "\n".join(texts) if texts else None


def _extract_docx(filepath: str) -> str | None:
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx 미설치 — DOCX 내용 인덱싱 건너뜀")
        return None
    doc   = Document(filepath)
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(texts) if texts else None


def _extract_xlsx(filepath: str) -> str | None:
    try:
        import openpyxl
    except ImportError:
        logger.warning("openpyxl 미설치 — XLSX 내용 인덱싱 건너뜀")
        return None
    wb    = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    texts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            row_text = " ".join(str(c) for c in row if c is not None)
            if row_text.strip():
                texts.append(row_text)
    wb.close()
    return "\n".join(texts) if texts else None


def _extract_pptx(filepath: str) -> str | None:
    """python-pptx로 추출하고, 실패하면 ZIP 직접 파싱으로 폴백한다."""
    if Presentation is None:
        logger.warning("python-pptx 미설치 — PPTX 내용 인덱싱 건너뜀")
        return None
    try:
        prs   = Presentation(filepath)
        texts = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        if t := para.text.strip():
                            texts.append(t)
        return "\n".join(texts) if texts else None
    except Exception as e:
        logger.debug("python-pptx 파싱 실패, ZIP 폴백 시도 %s: %s", filepath, e)
        return _extract_pptx_fallback(filepath)


def _extract_pptx_fallback(filepath: str) -> str | None:
    """PPTX를 ZIP으로 열어 슬라이드 XML에서 <a:t> 텍스트를 직접 추출한다."""
    texts = []
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            for name in sorted(n for n in zf.namelist() if _PPTX_SLIDE_RE.match(n)):
                try:
                    xml_str = zf.read(name).decode("utf-8", errors="replace")
                    for m in _PPTX_T_RE.finditer(xml_str):
                        if t := m.group(1).strip():
                            texts.append(t)
                except Exception as e:
                    logger.debug("PPTX 슬라이드 XML 읽기 실패 %s/%s: %s", filepath, name, e)
    except Exception as e:
        logger.debug("PPTX ZIP 폴백 실패 %s: %s", filepath, e)
        return None
    return "\n".join(texts) if texts else None


def _extract_hwpx(filepath: str) -> str | None:
    """HWPX(ZIP+XML) 파일에서 <t> 태그 텍스트를 추출한다."""
    texts = []
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            for name in sorted(zf.namelist()):
                if not (name.startswith("Contents/") and name.endswith(".xml")):
                    continue
                try:
                    root = ET.fromstring(zf.read(name))
                    for elem in root.iter():
                        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                        if tag == "t" and elem.text and elem.text.strip():
                            texts.append(elem.text.strip())
                except (ET.ParseError, UnicodeDecodeError):
                    continue
    except zipfile.BadZipFile:
        return None
    return "\n".join(texts) if texts else None


def _hwp_decompress(data: bytes, filepath: str, stream_name: str) -> bytes | None:
    """HWP BodyText 스트림의 zlib 압축을 해제한다.

    ■ HWP5 압축 스펙
      FileHeader의 flags 필드(+36, 4바이트) 비트0이 1이면 BodyText 스트림이 압축됨.
      HWP5 표준은 raw deflate(wbits=-15)를 사용하지만, 일부 비표준 파일은
      zlib 헤더(0x78 9C / 0x78 DA 등)가 포함된 wrapped deflate로 저장된다.

    ■ 3단계 디코딩 전략
      1차: raw deflate (wbits=-15) — HWP5 표준, zlib 헤더 없음
      2차: wrapped deflate (wbits=15) — zlib 헤더 포함
           → "invalid distance too far back" 오류는 raw 파서가 zlib 헤더 2바이트를
             데이터로 잘못 해석해 distance 테이블이 틀어져 발생하는 전형적 증상
      3차: gzip/zlib 자동 감지 (wbits=47) — gzip 헤더 포함 파일 대응
    """
    # 1차 시도: HWP5 표준 — headerless raw deflate
    try:
        return zlib.decompress(data, -15)
    except zlib.error:
        pass

    # 2차 시도: zlib 헤더(0x78 9C / 0x78 DA 등) 포함된 wrapped deflate
    # "invalid distance too far back"은 raw 파서가 zlib 헤더 2바이트를
    # 데이터로 잘못 읽을 때 distance 테이블이 틀어져 발생하는 전형적 증상.
    try:
        return zlib.decompress(data, 15)
    except zlib.error:
        pass

    # 3차 시도: gzip/zlib 자동 감지(wbits=47)
    try:
        return zlib.decompress(data, 47)
    except zlib.error as e:
        logger.debug("HWP decompress 실패 (%s/%s): %s", filepath, stream_name, e)
        return None


def _extract_hwp(filepath: str) -> str | None:
    """레거시 HWP(OLE2 바이너리) 파일에서 텍스트를 추출한다. (olefile 직접 파싱)"""
    try:
        import olefile
    except ImportError:
        logger.warning("olefile 미설치 — HWP 내용 인덱싱 건너뜀")
        return None

    try:
        ole = olefile.OleFileIO(filepath)
    except Exception as e:
        logger.debug("OLE 파일 열기 실패 (%s): %s", filepath, e)
        return None

    compressed = False
    if ole.exists("FileHeader"):
        header_data = ole.openstream("FileHeader").read()
        if len(header_data) >= 40:
            flags = int.from_bytes(header_data[36:40], "little")
            compressed = bool(flags & 0x1)
    logger.debug("HWP 압축 여부 (%s): %s", filepath, compressed)

    texts = []
    try:
        for stream_name in ole.listdir():
            if not "/".join(stream_name).startswith("BodyText/Section"):
                continue
            data = ole.openstream(stream_name).read()
            if compressed:
                data = _hwp_decompress(data, filepath, "/".join(stream_name))
                if data is None:
                    continue
            if text := _extract_text_from_hwp_body(data):
                texts.append(text)
    finally:
        ole.close()
    return "\n".join(texts) if texts else None


# ── HWP 바이너리 파서 ─────────────────────────────────────────────────────────

def _extract_text_from_hwp_body(data: bytes) -> str:
    """
    HWP BodyText 스트림에서 HWPTAG_PARA_TEXT(태그 ID=67) 레코드를 추출한다.

    ■ 태그 레코드 순회 알고리즘
      1) 헤더 4바이트 읽기 → tag_id, size 추출
      2) size == 0xFFF → 확장 크기 (다음 4바이트가 실제 페이로드 크기)
      3) tag_id == 67(HWPTAG_PARA_TEXT) → _decode_hwp_para_text()로 디코딩
      4) 그 외 태그 → 페이로드 건너뛰기
      5) 스트림 끝까지 반복
    """
    texts = []
    i     = 0
    while i < len(data) - 4:
        header = int.from_bytes(data[i:i + 4], "little")
        tag_id = header & _HWP_TAG_MASK
        size   = (header >> _HWP_SIZE_SHIFT) & _HWP_SIZE_MASK

        if size == _HWP_EXT_SIZE_SENTINEL:
            if i + 8 > len(data):
                break
            size  = int.from_bytes(data[i + 4:i + 8], "little")
            i    += 8
        else:
            i += 4

        if i + size > len(data):
            break

        if tag_id == _HWPTAG_PARA_TEXT:
            if text := _decode_hwp_para_text(data[i:i + size]).strip():
                texts.append(text)
        i += size

    return "\n".join(texts)


def _decode_hwp_para_text(chunk: bytes) -> str:
    """HWP PARA_TEXT 레코드를 UTF-16LE 코드 단위로 디코딩한다."""
    chars = []
    j     = 0
    while j < len(chunk) - 1:
        code = int.from_bytes(chunk[j:j + 2], "little")
        j   += 2
        if code >= 0x0020:
            chars.append(chr(code))
        elif code in (0x0D, 0x0A):
            chars.append("\n")
        elif code == 0x09:
            chars.append(" ")
        elif code in _HWP_INLINE_CTRL_CODES:
            j += _HWP_INLINE_CTRL_EXTRA_BYTES
    return "".join(chars)
