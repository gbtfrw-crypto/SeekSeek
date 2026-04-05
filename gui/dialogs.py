"""설정 다이얼로그 모음

ExcludedFoldersDialog — 제외 폴더 목록 편집
SearchHelpDialog      — FTS5 검색 문법 도움말
AboutDialog           — 프로그램 정보
"""
import os
import ctypes
import string

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QFileDialog, QLineEdit,
    QGroupBox, QCheckBox, QMessageBox, QAbstractItemView, QTextEdit,
    QScrollArea, QWidget,
)

import config

# ── 공통 폰트 상수 ────────────────────────────────────────────────────────────
_FONT_SM  = QFont("맑은 고딕", 9)   # 버튼·레이블 등 보조 텍스트
_FONT_MD  = QFont("맑은 고딕", 10)  # 본문 입력창·목록


class ExcludedFoldersDialog(QDialog):
    """제외 폴더 설정 다이얼로그.

    사용자가 스캔에서 제외할 폴더를 추가·편집·삭제할 수 있다.
    확인 시 config.save_excluded_paths() 로 설정을 저장한다.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("제외 폴더 설정")
        self.setMinimumSize(520, 450)
        self.resize(560, 500)
        # 물음표(?) 버튼 숨기기
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        self._excluded_paths = config.load_excluded_paths()
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 설명 레이블 ──────────────────────────────────────────────────────
        desc = QLabel(
            "색인할 때 건너뛸 폴더를 설정합니다.\n"
            "아래 목록에 포함된 폴더와 그 하위 폴더는 스캔에서 제외됩니다."
        )
        desc.setFont(_FONT_SM)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # ── 이름 기반 자동 제외 폴더 (체크박스) ─────────────────────────────────
        dir_group = QGroupBox("이름 기반 자동 제외 폴더 (체크된 항목은 어느 위치든 건너뜀)")
        dir_group.setFont(_FONT_SM)
        dir_layout = QVBoxLayout(dir_group)
        dir_layout.setSpacing(2)
        dir_layout.setContentsMargins(8, 6, 8, 6)

        # 항목이 많으므로 스크롤 영역 안에 배치
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFixedHeight(160)
        scroll_area.setStyleSheet("QScrollArea { border: none; }")

        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(2)
        scroll_layout.setContentsMargins(0, 0, 0, 0)

        enabled_dirs = config.load_excluded_dirs()
        self._dir_checkboxes: list[tuple[str, QCheckBox]] = []

        for name, desc, _ in config.WELL_KNOWN_EXCLUDED_DIRS:
            cb = QCheckBox(f"{name}  —  {desc}")
            cb.setFont(_FONT_SM)
            cb.setChecked(name in enabled_dirs)
            scroll_layout.addWidget(cb)
            self._dir_checkboxes.append((name, cb))

        scroll_area.setWidget(scroll_widget)
        dir_layout.addWidget(scroll_area)

        # 자동 제외 항목 안내
        dot_note = QLabel(
            "⚙  '.'으로 시작하는 폴더(.git, .venv, .cache 등)는 위 목록과 무관하게 항상 자동 제외됩니다."
        )
        dot_note.setFont(_FONT_SM)
        dot_note.setWordWrap(True)
        dot_note.setStyleSheet("color: #777; padding: 2px 0;")
        dir_layout.addWidget(dot_note)

        layout.addWidget(dir_group)

        # ── 제외 폴더 목록 ───────────────────────────────────────────────────
        group = QGroupBox("다음 폴더 제외(O):")
        group.setFont(_FONT_SM)
        group_layout = QVBoxLayout(group)

        self.folder_list = QListWidget()
        self.folder_list.setFont(_FONT_MD)
        self.folder_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.folder_list.setAlternatingRowColors(True)
        for path in self._excluded_paths:
            self.folder_list.addItem(path)
        group_layout.addWidget(self.folder_list)

        # ── 폴더 목록 조작 버튼 ──────────────────────────────────────────────
        btn_row = QHBoxLayout()
        for label, slot in [
            ("폴더 추가(F)…", self._on_add_folder),
            ("직접 입력(L)…", self._on_add_manual),
            ("편집(D)…",      self._on_edit),
            ("제거(R)",        self._on_remove),
        ]:
            btn = QPushButton(label)
            btn.setFont(_FONT_SM)
            btn.clicked.connect(slot)
            btn_row.addWidget(btn)
        btn_row.insertStretch(0)  # 오른쪽 정렬
        group_layout.addLayout(btn_row)
        layout.addWidget(group)

        # ── 하단 버튼 (기본값 복원 / 확인 / 취소) ────────────────────────────
        bottom = QHBoxLayout()
        bottom.addStretch()

        btn_reset = QPushButton("기본값 복원")
        btn_reset.setFont(_FONT_SM)
        btn_reset.clicked.connect(self._on_reset)

        btn_ok = QPushButton("확인")
        btn_ok.setFont(_FONT_SM)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self._on_ok)

        btn_cancel = QPushButton("취소")
        btn_cancel.setFont(_FONT_SM)
        btn_cancel.clicked.connect(self.reject)

        bottom.addWidget(btn_reset)
        bottom.addWidget(btn_ok)
        bottom.addWidget(btn_cancel)
        layout.addLayout(bottom)

    # ── 버튼 슬롯 ────────────────────────────────────────────────────────────

    def _on_add_folder(self):
        """탐색기 대화상자로 폴더를 선택하여 추가한다."""
        d = QFileDialog.getExistingDirectory(self, "제외할 폴더 선택")
        if d:
            d = os.path.normpath(d)
            if not self._path_exists_in_list(d):
                self.folder_list.addItem(d)

    def _on_add_manual(self):
        """경로를 직접 입력하여 추가한다."""
        dlg = _InputDialog(self, "폴더 경로 직접 입력", "제외할 폴더 경로:")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            path = dlg.get_text().strip()
            if path and not self._path_exists_in_list(path):
                self.folder_list.addItem(os.path.normpath(path))

    def _on_edit(self):
        """선택된 항목의 경로를 편집한다."""
        items = self.folder_list.selectedItems()
        if not items:
            return
        item = items[0]
        dlg  = _InputDialog(self, "폴더 경로 편집", "제외 폴더 경로:", item.text())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_path = dlg.get_text().strip()
            if new_path:
                item.setText(os.path.normpath(new_path))

    def _on_remove(self):
        """선택된 항목을 목록에서 제거한다."""
        for item in self.folder_list.selectedItems():
            self.folder_list.takeItem(self.folder_list.row(item))

    def _on_reset(self):
        """목록을 DEFAULT_EXCLUDED_PATHS 로 초기화한다."""
        reply = QMessageBox.question(
            self, "기본값 복원",
            "제외 폴더 목록을 기본값으로 되돌리시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.folder_list.clear()
            for path in config.DEFAULT_EXCLUDED_PATHS:
                self.folder_list.addItem(path)

    def _on_ok(self):
        """현재 목록과 체크박스 상태를 저장하고 다이얼로그를 닫는다."""
        paths = [self.folder_list.item(i).text()
                 for i in range(self.folder_list.count())]
        config.save_excluded_paths(paths)

        checked_dirs = {name for name, cb in self._dir_checkboxes if cb.isChecked()}
        config.save_excluded_dirs(checked_dirs)

        self.accept()

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _path_exists_in_list(self, path: str) -> bool:
        """path 가 이미 목록에 있는지 대소문자 무관하게 확인한다."""
        normed = os.path.normpath(path).lower()
        return any(
            os.path.normpath(self.folder_list.item(i).text()).lower() == normed
            for i in range(self.folder_list.count())
        )


class _InputDialog(QDialog):
    """단순 텍스트 입력 다이얼로그 (내부 전용)."""

    def __init__(self, parent, title: str, label: str, default: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)

        lbl = QLabel(label)
        lbl.setFont(_FONT_SM)
        layout.addWidget(lbl)

        self._edit = QLineEdit(default)
        self._edit.setFont(_FONT_MD)
        layout.addWidget(self._edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_ok = QPushButton("확인")
        btn_ok.setFont(_FONT_SM)
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("취소")
        btn_cancel.setFont(_FONT_SM)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def get_text(self) -> str:
        return self._edit.text()


class SearchHelpDialog(QDialog):
    """FTS5 검색 문법 도움말 다이얼로그."""

    _CONTENT = """\
<style>
  body  { font-family: '맑은 고딕', sans-serif; font-size: 10pt; color: #222; }
  h3    { color: #1565c0; margin: 12px 0 4px; }
  table { border-collapse: collapse; width: 100%; }
  td    { padding: 3px 8px; vertical-align: top; }
  td:first-child { width: 44%; font-family: Consolas, monospace;
                   color: #1a237e; white-space: nowrap; }
  tr:nth-child(even) td { background: #f0f4ff; }
  .note { color: #888; font-size: 9pt; margin-top: 6px; }
</style>

<h3>기본 검색</h3>
<table>
  <tr><td>회의 보고서</td><td>회의 AND 보고서 — 두 단어가 문서 어딘가에 모두 포함 (순서·위치 무관)</td></tr>
  <tr><td>계약*</td><td>계약으로 시작하는 단어 → 계약서, 계약금, 계약조건 등</td></tr>
  <tr><td>보고서</td><td>보고서로 시작하는 단어 → 보고서, 보고서류 등 (자동 prefix 검색)</td></tr>
</table>

<h3>논리 연산자</h3>
<table>
  <tr><td>A AND B</td><td>A 와 B 둘 다 포함 (공백과 동일)</td></tr>
  <tr><td>A OR B</td><td>A 또는 B 중 하나 이상 포함</td></tr>
  <tr><td>A NOT B</td><td>A 는 포함, B 는 미포함</td></tr>
</table>

<h3>정확한 구문 검색</h3>
<table>
  <tr><td>"매출 현황 보고"</td><td>세 단어가 이 순서 그대로 붙어 있는 경우만 검색</td></tr>
  <tr><td>"프로젝트 일정"</td><td>정확히 이 구문이 들어있는 문서만 검색</td></tr>
</table>

<h3>근접 검색 (NEAR)</h3>
<table>
  <tr><td>NEAR(예산 결산, 10)</td><td>예산과 결산이 10단어 이내에 함께 등장</td></tr>
  <tr><td>NEAR(계획 실행 결과, 5)</td><td>세 단어가 5단어 이내에 모두 등장</td></tr>
</table>

<h3>우선순위 / 그룹화</h3>
<table>
  <tr><td>^중요한단어</td><td>해당 단어가 포함된 결과를 상위로 올림 (BM25 가중치 상승)</td></tr>
  <tr><td>(서울 OR 부산) AND 지점</td><td>괄호로 조건 묶기 — 서울이나 부산 중 하나 + 지점 포함</td></tr>
</table>

<h3>실전 예시</h3>
<table>
  <tr><td>매출 2025</td><td>매출과 2025가 둘 다 포함된 문서</td></tr>
  <tr><td>매출 NOT 손실</td><td>매출은 있고 손실은 없는 문서</td></tr>
  <tr><td>"프로젝트 일정표"</td><td>프로젝트 일정표 구문이 정확히 들어있는 문서</td></tr>
  <tr><td>NEAR(납품 검수, 5)</td><td>납품과 검수가 5단어 이내에 함께 있는 문서</td></tr>
  <tr><td>계약 NOT 해지</td><td>계약 관련 내용이 있되 해지는 언급 안 된 문서</td></tr>
  <tr><td>인사 OR 채용</td><td>인사 또는 채용 중 하나라도 포함된 문서</td></tr>
</table>

<p class="note">
※ AND / OR / NOT / NEAR 는 반드시 대문자로 입력하세요.<br>
※ 단어 뒤 * 는 직접 붙일 수 있고, 붙이지 않아도 자동으로 prefix 검색이 적용됩니다.<br>
※ 띄어쓰기로 구분한 여러 단어는 모두 포함(AND) 조건입니다. 순서는 무관합니다.<br>
※ 결과는 BM25 관련도 순으로 정렬됩니다.
</p>
"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("검색 문법 도움말")
        self.setMinimumSize(560, 540)
        self.resize(580, 560)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        view = QTextEdit()
        view.setReadOnly(True)
        view.setStyleSheet(
            "QTextEdit { background-color: #ffffff; color: #222222; border: 1px solid #ccc; }"
        )
        view.setHtml(self._CONTENT)
        view.setFont(_FONT_MD)
        layout.addWidget(view)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_ok = QPushButton("닫기")
        btn_ok.setFont(_FONT_MD)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(btn_ok)
        layout.addLayout(btn_row)


class AboutDialog(QDialog):
    """프로그램 정보 다이얼로그."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SeekSeek 정보")
        self.setFixedSize(480, 360)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        from PyQt6.QtGui import QPixmap, QIcon
        icon_pixmap = QIcon("assets/icon.ico").pixmap(64, 64)
        icon_label = QLabel()
        icon_label.setPixmap(icon_pixmap)
        text_label = QLabel("SeekSeek")
        text_label.setFont(QFont("맑은 고딕", 18, QFont.Weight.Bold))
        text_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        title_row = QHBoxLayout()
        title_row.addStretch()
        title_row.addWidget(icon_label)
        title_row.addSpacing(8)
        title_row.addWidget(text_label)
        title_row.addStretch()
        layout.addLayout(title_row)

        version = QLabel("v1.0.0")
        version.setFont(_FONT_MD)
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version.setStyleSheet("color: #666;")
        layout.addWidget(version)

        desc = QLabel(
            "SeekSeek은 Windows 파일 & 문서 본문 검색 프로그램입니다.\n\n<br>"
            "● NTFS MFT를 활용한 초고속 파일 스캔 (관리자 권한)\n\n<br>"
            "● SQLite FTS5 기반 검색 엔진 사용\n\n<br>"
            "● 파일명 / 문서 본문(텍스트) 독립 검색\n\n<br>"
            "● 본문 검색 지원 대상: pdf, doc, docx, xls, xlsx, ppt, pptx, hwp, hwpx, 플레인 텍스트\n<br>"
            "Unlicense — 자유롭게 사용, 수정, 배포할 수 있습니다.\n\n<br><br>"
            "버그, 개선사항, 궁금한 점은 \n"
            " gbtfrw@gmail.com 로 메일주세요."
        )
        desc.setFont(_FONT_MD)
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignLeft)
        desc.setTextFormat(Qt.TextFormat.RichText)
        desc.setOpenExternalLinks(True)
        layout.addWidget(desc)

        layout.addStretch()

        btn_ok = QPushButton("확인")
        btn_ok.setFont(_FONT_MD)
        btn_ok.setFixedWidth(100)
        btn_ok.clicked.connect(self.accept)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        btn_row.addStretch()
        layout.addLayout(btn_row)


def _get_all_drives() -> list[dict]:
    """시스템의 모든 드라이브 정보를 반환한다.

    Returns: [{"letter": "C", "label": "C: (NTFS, 로컬)", "ntfs": True}, ...]
    """
    kernel32 = ctypes.windll.kernel32
    DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_CDROM, DRIVE_REMOTE = 2, 3, 5, 4
    drives = []
    bitmask = kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not (bitmask & (1 << i)):
            continue
        drive_path = f"{letter}:\\"
        drive_type = kernel32.GetDriveTypeW(drive_path)
        if drive_type == 1:  # DRIVE_NO_ROOT_DIR
            continue
        fs_name = ctypes.create_unicode_buffer(32)
        vol_name = ctypes.create_unicode_buffer(256)
        kernel32.GetVolumeInformationW(
            drive_path, vol_name, 256, None, None, None, fs_name, 32
        )
        fs  = fs_name.value or "?"
        lbl = vol_name.value or ""
        type_str = {
            DRIVE_FIXED: "로컬", DRIVE_REMOVABLE: "이동식",
            DRIVE_CDROM: "CD/DVD", DRIVE_REMOTE: "네트워크",
        }.get(drive_type, "기타")
        display = f"{letter}:  [{fs}] {lbl}  ({type_str})"
        drives.append({"letter": letter, "label": display, "ntfs": fs == "NTFS"})
    return drives


class DriveSelectDialog(QDialog):
    """스캔할 드라이브를 선택하는 다이얼로그.

    selected_drives: 확인 후 선택된 드라이브 문자 리스트 (예: ["C", "D"])
    """

    def __init__(self, parent=None, preselect_ntfs: bool = True):
        super().__init__(parent)
        self.setWindowTitle("스캔 드라이브 선택")
        self.setFixedSize(400, 300)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.selected_drives: list[str] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel("스캔할 드라이브를 선택하세요:"))

        self._checks: list[tuple[QCheckBox, str]] = []
        drives = _get_all_drives()
        for d in drives:
            cb = QCheckBox(d["label"])
            cb.setFont(_FONT_SM)
            if preselect_ntfs and d["ntfs"]:
                cb.setChecked(True)
            layout.addWidget(cb)
            self._checks.append((cb, d["letter"]))

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_ok     = QPushButton("스캔 시작")
        btn_cancel = QPushButton("취소")
        btn_ok.setFont(_FONT_SM)
        btn_cancel.setFont(_FONT_SM)
        btn_ok.clicked.connect(self._accept)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def _accept(self):
        self.selected_drives = [letter for cb, letter in self._checks if cb.isChecked()]
        if not self.selected_drives:
            QMessageBox.warning(self, "선택 없음", "드라이브를 하나 이상 선택하세요.")
            return
        self.accept()
