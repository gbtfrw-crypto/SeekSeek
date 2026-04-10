"""PyQt6 메인 윈도우

검색 UI, 스캔 제어, 결과 테이블, 내용 미리보기, 색인 폴더 패널을 포함한다.

■ Qt Model/View 아키텍처
  ResultTableModel(QAbstractTableModel) → QTableView
  - data() 는 화면에 보이는 행만 호출되는 가상 렌더링 방식
  - 100만 건 결과도 냥비 없이 표시 가능 (O(1) 랜덤 액세스)
  - set_results() 시 beginResetModel/endResetModel 로
    QTableView에 데이터 변경을 알림

■ 시그널/슬롯 구조 (QThread ↔ GUI 통신)
  ScannerThread.progress       → MainWindow._on_scan_progress
  ScannerThread.finished_signal → MainWindow._on_scan_finished
  USNMonitorThread.updated      → MainWindow._on_usn_updated
  ContentReindexThread.progress → MainWindow._on_reindex_progress
  → Qt가 시그널 전달 시 자동으로 스레드 경계를 마샬링하므로
    별도 락 없이 GUI 업데이트 안전
"""
import os
import html
import re
import subprocess
import logging
import time
from datetime import datetime

from PyQt6.QtCore import Qt, QSize, QFileInfo, QAbstractTableModel, QModelIndex, QEvent
from PyQt6.QtGui import QFont, QColor, QAction
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QTableView,
    QHeaderView, QLabel, QFileDialog,
    QMessageBox, QProgressBar, QMenu, QAbstractItemView,
    QTextEdit, QSplitter, QApplication, QListWidget, QListWidgetItem,
    QFileIconProvider,
)

import config
from core.scanner import ScannerThread, USNMonitorThread, ContentReindexThread, FolderIndexThread
from core.searcher import search, SearchResult, match_label
from core.indexer import (init_db, get_connection, get_stats,
                           add_indexed_folder, remove_indexed_folder,
                           get_indexed_folders, get_indexed_folders_with_status,
                           update_indexed_at, get_file_content_by_path)
from core.extractor import extract_text
from core import mft_cache
from gui.dialogs import ExcludedFoldersDialog, AboutDialog, SearchHelpDialog

logger = logging.getLogger(__name__)

# ── 배지 스타일 상수 ──────────────────────────────────────────────────────────
_BADGE = {
    "pending":  "QLabel { background:#555;    color:#ccc;    border:none; padding:0 4px; border-radius:3px; }",
    "done":     "QLabel { background:#2e7d32; color:#c8e6c9; border:none; padding:0 4px; border-radius:3px; }",
    "changed":  "QLabel { background:#7b3f00; color:#ffcc44; border:none; padding:0 4px; border-radius:3px; }",
    "indexing": "QLabel { background:#1565c0; color:#bbdefb; border:none; padding:0 4px; border-radius:3px; }",
}


def _set_badge(badge: QLabel, state: str, text: str) -> None:
    badge.setText(text)
    badge.setStyleSheet(_BADGE[state])
    badge.setVisible(True)


# ── 결과 테이블 모델 ──────────────────────────────────────────────────────────

class ResultTableModel(QAbstractTableModel):
    """가상 렌더링 모델 — 화면에 보이는 행만 data()가 호출된다."""

    _COL_LABELS = ["이름", "경로", "크기", "수정일", "확장자", "매칭"]
    _COLOR_MAP  = {
        "both":    QColor("#2e7d32"),
        "content": QColor("#1565c0"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: list[SearchResult] = []
        self._sort_col: int = -1
        self._sort_asc: bool = True
        self._icon_provider = QFileIconProvider()
        self._folder_icon   = self._icon_provider.icon(QFileIconProvider.IconType.Folder)
        self._icon_cache: dict[str, object] = {}

    # ── 필수 override ─────────────────────────────────────────────────────────

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._results)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 6

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r   = self._results[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0: return r.name
            if col == 1: return os.path.dirname(r.path)
            if col == 2: return _format_size(r.size)
            if col == 3:
                return datetime.fromtimestamp(r.modified).strftime("%Y-%m-%d %H:%M") \
                    if r.modified else ""
            if col == 4: return r.extension
            if col == 5: return match_label(r.match_type)

        elif role == Qt.ItemDataRole.UserRole:
            return r.path

        elif role == Qt.ItemDataRole.DecorationRole:
            if col == 0:
                if r.is_dir:
                    return self._folder_icon
                ext = r.extension.lower()
                if ext not in self._icon_cache:
                    self._icon_cache[ext] = self._icon_provider.icon(QFileInfo(r.path))
                return self._icon_cache[ext]

        elif role == Qt.ItemDataRole.ForegroundRole:
            if col == 4:
                return QColor("#6a1b9a")
            if col == 5:
                return self._COLOR_MAP.get(r.match_type)

        return None

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            label = self._COL_LABELS[section]
            if section == self._sort_col:
                label += " ▲" if self._sort_asc else " ▼"
            return label
        return None

    # ── 공개 API ──────────────────────────────────────────────────────────────

    def set_results(self, results: list[SearchResult]) -> None:
        self.beginResetModel()
        self._results = results
        self.endResetModel()

    def result_at(self, row: int) -> SearchResult | None:
        if 0 <= row < len(self._results):
            return self._results[row]
        return None

    def set_sort_indicator(self, col: int, asc: bool) -> None:
        self._sort_col = col
        self._sort_asc = asc
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, 5)


# ── 메인 윈도우 ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    """SeekSeek 메인 창."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SeekSeek - 파일 & 내용 검색  [관리자]")
        self.setMinimumSize(900, 600)
        self.resize(1180, 700)

        self._scanner_thread: ScannerThread | None = None
        self._cache_init_thread: ScannerThread | None = None
        self._usn_monitor: USNMonitorThread | None = None
        self._reindex_thread: ContentReindexThread | None = None
        self._folder_index_thread: FolderIndexThread | None = None
        self._results: list[SearchResult] = []
        self._pending_reindex_by_folder: dict[str, set[str]] = {}
        self._folder_badges: dict[str, QLabel] = {}
        self._folder_indexed_at: dict[str, float | None] = {}
        self._indexing_folders: list[str] = []
        self._index_total: int = 0
        self._sort_col: int = -1
        self._sort_asc: bool = True
        self._preview_cache: dict[str, str] = {}
        # 동시 색인(전체+변경분) 시 완료 처리를 1회로 합치기 위한 카운터.
        # 각 스레드 finished_signal에서 _on_index_thread_done이 감소시킨다.
        self._running_index_count: int = 0
        self._child_windows: list[QMainWindow] = []

        self._initializing = False

        init_db()
        self._build_ui()
        self._update_status_stats()
        self._load_indexed_folders()

        try:
            with get_connection() as conn:
                cache_cnt = conn.execute('SELECT COUNT(*) FROM file_cache').fetchone()[0]
            if cache_cnt < 1000:
                self._initializing = True
                for w in (self.input_filename, self.input_content):
                    w.setEnabled(False)
        except Exception:
            pass

        logger.info("SeekSeek 시작됨 [관리자]")

        if mft_cache.count() > 0:
            self.lbl_scan_status.setText(" 준비됨")
            for w in (self.input_filename, self.input_content):
                w.setEnabled(True)
        elif self._initializing:
            try:
                with get_connection() as conn:
                    if mft_cache.load_from_db(conn):
                        logger.info("이전 스캔 미완 — file_cache 선로드: %d개", mft_cache.count())
            except Exception:
                pass
            self.lbl_scan_status.setText("🔄 초기화 중 — 파일 스캔 후 검색 가능")
            self._start_scan()
        else:
            self._start_cache_init()

    # ── UI 구성 ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_menu_bar()

        central     = QWidget()
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(central)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setHandleWidth(4)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, True)
        splitter.widget(0).setMinimumWidth(100)
        splitter.widget(1).setMinimumWidth(80)
        splitter.widget(1).setMaximumWidth(900)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([750, 300])
        splitter.setStyleSheet(
            "QSplitter::handle:horizontal {"
            "  background:#39ff1466; min-width:4px; max-width:4px; }"
        )
        root_layout.addWidget(splitter)
        self.statusBar().setFont(QFont("맑은 고딕", 9))

    def _build_left_panel(self) -> QWidget:
        panel  = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(6)

        font_search = QFont("맑은 고딕", 9)
        lbl_style   = "font-weight:bold; color:#333; min-width:60px;"

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)

        for icon, attr, placeholder, tooltip in [
            ("파일명 검색:",   "input_filename", "파일명으로 검색…", ""),
            (" 본문 검색:", "input_content",  "본문 내용으로 검색…",
             "FTS5 검색 문법 (도움말 메뉴에서 자세히 보기)\n\n"
             "단순 단어     단어1 단어2          → 두 단어 모두 포함 (prefix 일치)\n"
             "AND / OR     A AND B / A OR B    → 논리 연산\n"
             "NOT          A NOT B             → A 포함, B 미포함\n"
             '구문 검색     "import pandas"     → 정확한 구문\n'
             "와일드카드    func*               → func으로 시작하는 단어\n"
             "근접 검색     NEAR(def return, 10) → 10단어 이내 근접"),
        ]:
            lbl  = QLabel(icon)
            lbl.setFont(font_search)
            lbl.setStyleSheet(lbl_style)
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            edit.setFont(font_search)
            edit.setClearButtonEnabled(True)
            if tooltip:
                edit.setToolTip(tooltip)
            edit.returnPressed.connect(self._do_search)
            form.addRow(lbl, edit)
            setattr(self, attr, edit)

        layout.addLayout(form)

        self.lbl_result_count = QLabel("")
        self.lbl_result_count.setFont(QFont("맑은 고딕", 9))
        self.lbl_result_count.setStyleSheet("color:#666; padding:0 2px;")
        layout.addWidget(self.lbl_result_count)

        # 결과 테이블 (가상 렌더링: 보이는 행만 data() 호출)
        self._model = ResultTableModel()
        self.table = QTableView()
        self.table.setModel(self._model)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(False)
        for col, w in enumerate([200, 300, 60, 120, 55, 80]):
            self.table.setColumnWidth(col, w)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setFont(QFont("맑은 고딕", 8))
        self.table.verticalHeader().setDefaultSectionSize(18)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.doubleClicked.connect(self._on_row_double_click)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.selectionModel().currentRowChanged.connect(self._on_selection_changed)
        hdr.sectionClicked.connect(self._on_header_clicked)
        hdr.setSortIndicatorShown(True)
        layout.addWidget(self.table, stretch=1)

        layout.addLayout(self._build_scan_bar())
        return panel

    def _build_scan_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setMaximumHeight(18)
        self.progress.setMaximumWidth(100)
        self.lbl_scan_status = QLabel("")
        self.lbl_scan_status.setFont(QFont("맑은 고딕", 9))
        bar.addWidget(self.progress)
        bar.addWidget(self.lbl_scan_status, stretch=1)
        return bar

    def _build_right_panel(self) -> QWidget:
        # 미리보기 파일명 레이블
        self.lbl_preview_name = QLabel("")
        self.lbl_preview_name.setFont(QFont("맑은 고딕", 8))
        self.lbl_preview_name.setStyleSheet(
            "QLabel { color:#555; background:#e8e8e8; padding:2px 6px;"
            "  border-bottom:1px solid #ccc; }"
        )
        self.lbl_preview_name.setFixedHeight(20)

        self.snippet_view = QTextEdit()
        self.snippet_view.setReadOnly(True)
        self.snippet_view.setFont(QFont("맑은 고딕", 9))
        self.snippet_view.setStyleSheet(
            "QTextEdit { color:#333; background:#f5f5f5; border:none; padding:6px; }"
            "QScrollBar:vertical { background:#eee; width:8px; border:none; }"
            "QScrollBar::handle:vertical { background:#aaa; border-radius:4px; min-height:20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )

        # 찾기 바 (Ctrl+F)
        find_bar = QWidget()
        find_bar.setFixedHeight(28)
        find_bar.setStyleSheet("background:#f0f0f0; border-top:1px solid #ccc;")
        find_layout = QHBoxLayout(find_bar)
        find_layout.setContentsMargins(4, 2, 4, 2)
        find_layout.setSpacing(4)

        self._find_input = QLineEdit()
        self._find_input.setPlaceholderText("찾기…")
        self._find_input.setFont(QFont("맑은 고딕", 9))
        self._find_input.setFixedHeight(22)
        self._find_input.setStyleSheet("QLineEdit { background:#ffffff; }")
        self._find_input.returnPressed.connect(self._find_next)
        self._find_input.installEventFilter(self)
        self._find_input.textChanged.connect(self._find_reset)

        btn_prev = QPushButton("▲")
        btn_next = QPushButton("▼")
        btn_close = QPushButton("✕")
        for b in (btn_prev, btn_next, btn_close):
            b.setFixedSize(22, 22)
            b.setFont(QFont("맑은 고딕", 8))
        btn_prev.setToolTip("이전 (Shift+Enter)")
        btn_next.setToolTip("다음 (Enter)")
        btn_close.setToolTip("닫기 (Esc)")
        btn_prev.clicked.connect(self._find_prev)
        btn_next.clicked.connect(self._find_next)
        btn_close.clicked.connect(self._find_bar_hide)

        self._find_count_lbl = QLabel("")
        self._find_count_lbl.setFont(QFont("맑은 고딕", 8))
        self._find_count_lbl.setStyleSheet("color:#666;")

        find_layout.addWidget(self._find_input, stretch=1)
        find_layout.addWidget(btn_prev)
        find_layout.addWidget(btn_next)
        find_layout.addWidget(self._find_count_lbl)
        find_layout.addWidget(btn_close)
        find_bar.setVisible(False)
        self._find_bar = find_bar

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)
        self.snippet_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.snippet_view.customContextMenuRequested.connect(self._show_preview_context_menu)

        preview_layout.addWidget(self.lbl_preview_name)
        preview_layout.addWidget(self.snippet_view)
        preview_layout.addWidget(find_bar)

        v_splitter = QSplitter(Qt.Orientation.Vertical)
        v_splitter.addWidget(preview_panel)
        v_splitter.addWidget(self._build_folders_panel())
        v_splitter.setStretchFactor(0, 3)
        v_splitter.setStretchFactor(1, 1)
        v_splitter.setHandleWidth(4)
        v_splitter.setStyleSheet(
            "QSplitter::handle:vertical {"
            "  background:#39ff1466; min-height:4px; max-height:4px; }"
        )
        return v_splitter

    def _build_folders_panel(self) -> QWidget:
        panel  = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header_widget = QWidget()
        header_widget.setFixedHeight(28)
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(8, 0, 4, 0)
        header_layout.setSpacing(4)
        header_lbl = QLabel(" 본문 검색 대상 폴더")
        header_lbl.setFont(QFont("맑은 고딕", 9, QFont.Weight.Bold))
        btn_add = QPushButton("+")
        btn_add.setFixedSize(22, 22)
        btn_add.setFont(QFont("맑은 고딕", 10, QFont.Weight.Bold))
        btn_add.setToolTip("본문 검색 대상 폴더 추가")
        btn_add.clicked.connect(self._add_indexed_folder)
        header_layout.addWidget(header_lbl)
        header_layout.addStretch()
        header_layout.addWidget(btn_add)
        layout.addWidget(header_widget)

        self.folders_list = QListWidget()
        self.folders_list.setFont(QFont("맑은 고딕", 8))
        self.folders_list.setAlternatingRowColors(True)
        self.folders_list.setStyleSheet(
            "QListWidget { border:1px solid #ddd; padding:0; }"
            "QListWidget::item { padding:0; border-bottom:1px solid #e8e8e8; }"
            "QListWidget::item:selected { background:#cce5ff; }"
            "QListWidget::item:alternate { background:#f7f7f7; }"
        )
        self.folders_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folders_list.customContextMenuRequested.connect(self._show_folder_context_menu)
        layout.addWidget(self.folders_list, stretch=1)

        self.btn_index = QPushButton(" 본문 검색 색인")
        self.btn_index.setFixedHeight(28)
        self.btn_index.setFont(QFont("맑은 고딕", 9, QFont.Weight.Bold))
        self.btn_index.setStyleSheet(
            "QPushButton { font-weight:bold; padding:4px 8px; }"
        )
        self.btn_index.clicked.connect(self._on_index_clicked)
        layout.addWidget(self.btn_index)
        return panel

    # ── 메뉴바 ────────────────────────────────────────────────────────────────

    def _build_menu_bar(self):
        mb = self.menuBar()
        mb.setFont(QFont("맑은 고딕", 9))

        file_menu = mb.addMenu("파일(&F)")
        act_new = QAction("새 창 띄우기(&N)", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self._on_new_window)
        file_menu.addAction(act_new)
        file_menu.addSeparator()
        act_db = QAction("DB 파일 위치 폴더 열기", self)
        act_db.triggered.connect(self._on_open_db_folder)
        file_menu.addAction(act_db)
        file_menu.addSeparator()
        act_rescan = QAction("파일명 검색 재스캔(&R)", self)
        act_rescan.setShortcut("F5")
        act_rescan.triggered.connect(self._on_full_rescan)
        file_menu.addAction(act_rescan)
        file_menu.addSeparator()
        act_exit = QAction("종료(&X)", self)
        act_exit.setShortcut("Alt+F4")
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        tool_menu = mb.addMenu("도구(&T)")
        act_excl = QAction("제외 폴더 설정…", self)
        act_excl.triggered.connect(self._on_excluded_folders)
        tool_menu.addAction(act_excl)

        help_menu = mb.addMenu("도움말(&H)")
        act_help = QAction("본문 검색 도움말(&S)…", self)
        act_help.setShortcut("F1")
        act_help.triggered.connect(self._on_search_help)
        help_menu.addAction(act_help)
        help_menu.addSeparator()
        act_about = QAction("이 프로그램에 대해…", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

    # ── 메뉴 슬롯 ────────────────────────────────────────────────────────────

    def _on_new_window(self):
        win = MainWindow()
        win.show()
        win.destroyed.connect(lambda _=None, w=win: self._forget_child_window(w))
        self._child_windows.append(win)

    def _forget_child_window(self, win: QMainWindow) -> None:
        try:
            self._child_windows.remove(win)
        except ValueError:
            pass

    def _on_full_rescan(self):
        """MFT 전체 재스캔 후 file_cache DB에 저장한다."""
        if self._scanner_thread is not None and self._scanner_thread.isRunning():
            QMessageBox.information(self, "재스캔", "이미 스캔이 진행 중입니다.")
            return
        reply = QMessageBox.question(
            self, "전체 재스캔",
            "MFT 전체를 다시 스캔합니다.\n파일 수가 많으면 수십 초 걸릴 수 있습니다.\n\n계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._start_scan()

    def _on_open_db_folder(self):
        if os.path.isdir(config.APP_DIR):
            os.startfile(config.APP_DIR)
        else:
            QMessageBox.information(self, "DB 폴더", f"DB 폴더가 아직 없습니다:\n{config.APP_DIR}")

    def _on_excluded_folders(self):
        if ExcludedFoldersDialog(self).exec():
            logger.info("제외 폴더 설정 저장됨")

    def _on_search_help(self):
        SearchHelpDialog(self).exec()

    def _on_about(self):
        AboutDialog(self).exec()

    # ── 테이블 정렬 ──────────────────────────────────────────────────────────

    def _on_header_clicked(self, col: int):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        order = Qt.SortOrder.AscendingOrder if self._sort_asc else Qt.SortOrder.DescendingOrder
        self.table.horizontalHeader().setSortIndicator(col, order)
        self._model.set_sort_indicator(col, self._sort_asc)
        self._populate_table(self._sort_results(self._results))

    def _sort_results(self, results: list) -> list:
        """폴더를 항상 맨 위에 두고 지정된 컬럼으로 정렬한다."""
        key_fns = {
            0: lambda r: (not r.is_dir, r.name.lower()),
            1: lambda r: (not r.is_dir, os.path.dirname(r.path).lower()),
            2: lambda r: (not r.is_dir, r.size),
            3: lambda r: (not r.is_dir, r.modified),
            4: lambda r: (not r.is_dir, r.extension.lower()),
            5: lambda r: (not r.is_dir, r.match_type),
        }
        key_fn = key_fns.get(self._sort_col, lambda r: (not r.is_dir, r.name.lower()))
        return sorted(results, key=key_fn, reverse=not self._sort_asc)

    # ── 검색 ─────────────────────────────────────────────────────────────────

    def _do_search(self):
        """현재 입력창 상태로 즉시 검색을 실행한다.

        설계 포인트:
        - 본문 검색어가 있을 때만 폴더 범위(folder_paths)를 적용한다.
        - 파일명 단독 검색은 인메모리 MFT 캐시 경로가 가장 빠르므로 전체 범위를 허용한다.
        """
        if self._initializing:
            return
        fn_query = self.input_filename.text().strip()
        ct_query = self.input_content.text().strip()

        if not fn_query and not ct_query:
            self._model.set_results([])
            self._results = []
            self.lbl_result_count.setText("")
            self.statusBar().showMessage("검색어를 입력하세요")
            self.snippet_view.setHtml("")
            return

        folder_paths = self._get_registered_folders() if ct_query else None
        logger.info(" 검색 시작 │ 파일명=[%s] 내용=[%s] │ 캐시=%d개 │ 폴더=%s",
                    fn_query, ct_query, mft_cache.count(),
                    len(folder_paths) if folder_paths else "전체")
        _t0 = time.perf_counter()
        new_results = search(
            filename_query=fn_query,
            content_query=ct_query,
            folder_paths=folder_paths,
        )
        _t1 = time.perf_counter()

        self._preview_cache.clear()
        self._results = new_results
        self._populate_table(self._sort_results(self._results))
        _t2 = time.perf_counter()
        logger.info(" 결과=%d건 │ search()=%.3fs  populate()=%.3fs  total=%.3fs",
                    len(self._results), _t1 - _t0, _t2 - _t1, _t2 - _t0)

        n = len(self._results)
        self.lbl_result_count.setText(f"{n:,}건")
        self.lbl_result_count.setStyleSheet("color:#666; padding:0 2px;")

        parts = []
        if fn_query: parts.append(f"파일명='{fn_query}'")
        if ct_query: parts.append(f"내용='{ct_query}'")
        info = " | ".join(parts) or "검색"
        self.statusBar().showMessage(f"{info} → {n:,}건")
        logger.info("SEARCH ▶ %s | 결과 %d건", info, n)

    def _populate_table(self, results: list[SearchResult]):
        _pt0 = time.perf_counter()
        self._model.set_results(results)
        self.table.clearSelection()
        self.snippet_view.setHtml("")
        self.lbl_preview_name.setText("")
        logger.debug(" populate(%d행)=%.3fs", len(results), time.perf_counter() - _pt0)

    # ── 미리보기 ──────────────────────────────────────────────────────────────

    def _on_selection_changed(self, current: QModelIndex, _previous: QModelIndex):
        """행 선택 변경 시 내용 미리보기를 갱신한다."""
        row = current.row()
        r = self._model.result_at(row)
        if r is None:
            self.snippet_view.setHtml("")
            self.lbl_preview_name.setText("")
            return
        self.lbl_preview_name.setText(r.name)
        keyword = self.input_content.text().strip()

        # 우선순위 1) DB에 이미 색인된 본문 (일관된 스니펫/하이라이트 제공)
        try:
            with get_connection() as conn:
                content = get_file_content_by_path(conn, r.path)
        except Exception:
            content = None

        if content:
            self._show_preview(content, keyword)
            return

        # 우선순위 2) 미색인 파일은 추출기 지원 확장자일 때만 파일 직접 읽기
        if os.path.splitext(r.path)[1].lower() not in config.CONTENT_EXTENSIONS:
            self.snippet_view.setHtml(
                '<p style="color:#999;font-size:9pt;">미리보기를 지원하지 않는 파일 형식입니다.</p>'
            )
            return

        # 인메모리 캐시 확인
        if (cached := self._preview_cache.get(r.path)) is not None:
            self._show_preview(cached)
            return

        # 파일 크기 확인
        try:
            fsize = os.path.getsize(r.path)
        except OSError:
            self.snippet_view.setHtml('<p style="color:#999;font-size:9pt;">파일에 접근할 수 없습니다.</p>')
            return
        if fsize > config.MAX_CONTENT_SIZE:
            self.snippet_view.setHtml('<p style="color:#999;font-size:9pt;">파일이 너무 큽니다.</p>')
            return

        self.snippet_view.setHtml('<p style="color:#999;font-size:9pt;">미리보기 로드 중…</p>')
        extracted = extract_text(r.path)
        if extracted:
            self._preview_cache[r.path] = extracted
            if self.table.currentIndex().row() == row:
                self._show_preview(extracted, keyword="")
        else:
            self.snippet_view.setHtml('<p style="color:#999;font-size:9pt;">내용을 추출할 수 없습니다.</p>')

    def _show_preview(self, content: str, keyword: str = "") -> None:
        """텍스트를 미리보기에 표시한다. keyword가 있으면 하이라이트 후 첫 매치로 스크롤."""
        truncated = len(content) > config.MAX_PREVIEW_SIZE
        if truncated:
            content = content[:config.MAX_PREVIEW_SIZE]
        html_text = _build_full_content_html(content, keyword)
        if truncated:
            html_text += (
                '<p style="color:#999;font-size:8pt;margin-top:8px;">'
                f'⋯ 내용이 길어 {config.MAX_PREVIEW_SIZE // 1024}KB까지만 표시합니다.</p>'
            )
        self.snippet_view.setHtml(html_text)
        if keyword:
            self.snippet_view.scrollToAnchor("first_match")

    # ── 미리보기 찾기 (Ctrl+F) ────────────────────────────────────────────────

    def _show_preview_context_menu(self, pos):
        menu = QMenu(self)
        act_find = QAction(" 찾기 (Ctrl+F)", self)
        act_find.triggered.connect(self._find_bar_show)
        menu.addAction(act_find)
        menu.addSeparator()
        act_clear = QAction("🗑 미리보기 지우기", self)
        act_clear.triggered.connect(self._clear_preview)
        menu.addAction(act_clear)
        menu.exec(self.snippet_view.viewport().mapToGlobal(pos))

    def _clear_preview(self):
        self.snippet_view.setHtml("")
        self.lbl_preview_name.setText("")
        self._find_bar_hide()

    def _find_bar_show(self):
        self._find_bar.setVisible(True)
        self._find_input.setFocus()
        self._find_input.selectAll()

    def _find_bar_hide(self):
        self._find_bar.setVisible(False)
        self.snippet_view.setExtraSelections([])
        self._find_count_lbl.setText("")
        self.snippet_view.setFocus()

    def _find_reset(self):
        """검색어 변경 시 문서 처음부터 다시 찾기."""
        cur = self.snippet_view.textCursor()
        cur.movePosition(cur.MoveOperation.Start)
        self.snippet_view.setTextCursor(cur)
        self._find_count_lbl.setText("")
        self._find_next()

    def _find_next(self):
        self._find_in_preview(backward=False)

    def _find_prev(self):
        self._find_in_preview(backward=True)

    def _find_in_preview(self, backward: bool = False):
        from PyQt6.QtGui import QTextDocument, QTextCharFormat, QTextCursor
        term = self._find_input.text()
        if not term:
            self._find_count_lbl.setText("")
            self.snippet_view.setExtraSelections([])
            return
        flags = QTextDocument.FindFlag(0)
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        found = self.snippet_view.find(term, flags)
        if not found:
            cur = self.snippet_view.textCursor()
            cur.movePosition(
                QTextCursor.MoveOperation.End if backward
                else QTextCursor.MoveOperation.Start
            )
            self.snippet_view.setTextCursor(cur)
            found = self.snippet_view.find(term, flags)

        if found:
            total = self.snippet_view.document().toPlainText().lower().count(term.lower())
            self._find_count_lbl.setText(f"{total}개")
            self._find_input.setStyleSheet("QLineEdit { background:#ffffff; }")
            # 현재 매치를 ExtraSelection으로 노란색 강조 (포커스 없이도 보임)
            fmt = QTextCharFormat()
            fmt.setBackground(QColor("#ffe082"))
            fmt.setForeground(QColor("#000000"))
            sel = QTextEdit.ExtraSelection()
            sel.cursor = self.snippet_view.textCursor()
            sel.format  = fmt
            self.snippet_view.setExtraSelections([sel])
            # 커서 selection 제거 (파란 드래그 효과 제거)
            cur = self.snippet_view.textCursor()
            cur.clearSelection()
            self.snippet_view.setTextCursor(cur)
            self.snippet_view.ensureCursorVisible()
            self._find_input.setFocus()
        else:
            self._find_count_lbl.setText("없음")
            self._find_input.setStyleSheet("QLineEdit { background:#ffd0d0; }")
            self.snippet_view.setExtraSelections([])

    def eventFilter(self, watched, event):
        if watched is self._find_input and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._find_bar_hide()
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_F
                and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
            self._find_bar_show()
            return
        super().keyPressEvent(event)

    # ── 파일 열기 / 컨텍스트 메뉴 ────────────────────────────────────────────

    def _row_path(self, row: int) -> str | None:
        r = self._model.result_at(row)
        return r.path if r else None

    def _on_row_double_click(self, index):
        path = self._row_path(index.row())
        if path and os.path.exists(path):
            os.startfile(path)

    def _show_context_menu(self, pos):
        row  = self.table.indexAt(pos).row()
        path = self._row_path(row)
        if not path:
            return
        menu = QMenu(self)
        act_open = QAction("파일 열기", self)
        act_open.triggered.connect(lambda: os.startfile(path) if os.path.exists(path) else None)
        menu.addAction(act_open)
        act_folder = QAction("폴더 열기", self)
        act_folder.triggered.connect(lambda: subprocess.Popen(["explorer", "/select,", path]))
        menu.addAction(act_folder)
        act_copy = QAction("경로 복사", self)
        act_copy.triggered.connect(lambda: QApplication.clipboard().setText(path))
        menu.addAction(act_copy)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ── 스캔 제어 ─────────────────────────────────────────────────────────────

    def _start_cache_init(self):
        t = ScannerThread(cache_only=True)
        t.progress.connect(lambda path, _n: self.lbl_scan_status.setText(path))
        t.finished_signal.connect(self._on_cache_init_done)
        t.start()
        self._cache_init_thread = t

    def _on_cache_init_done(self, total: int, _content: int):
        logger.info("📂 파일 목록 로드 완료: %s개", f"{total:,}")
        self.lbl_scan_status.setText("준비")
        self._start_usn_monitor()
        self._do_search()

    def _start_scan(self, scan_paths: list[str] | None = None):
        self._scanner_thread = ScannerThread(scan_paths=scan_paths)
        self._scanner_thread.progress.connect(self._on_scan_progress)
        self._scanner_thread.finished_signal.connect(self._on_scan_finished)
        self._scanner_thread.error_signal.connect(self._on_scan_error)
        self._scanner_thread.mode_signal.connect(self._on_scan_mode)
        self._scanner_thread.start()
        self.progress.setVisible(True)
        self.lbl_scan_status.setText("스캔 준비 중…")

    def _on_scan_mode(self, mode: str):
        self.lbl_scan_status.setText(f"모드: {mode}")
        logger.info("SCAN MODE → %s", mode)

    def _on_scan_progress(self, current_path: str, count: int):
        self.lbl_scan_status.setText(f"{count:,}개 파일 스캔 | {current_path}")

    def _on_scan_finished(self, total: int, content_count: int):
        self.progress.setVisible(False)
        self.lbl_scan_status.setText(f"파일명 검색 {total:,}개 파일 스캔 완료")
        logger.info(" SCAN COMPLETE │ files=%s │ contents=%s", f"{total:,}", f"{content_count:,}")
        if self._initializing:
            self._initializing = False
            for w in (self.input_filename, self.input_content):
                w.setEnabled(True)
            logger.info(" 초기화 완료 — 검색 가능")
        self._update_status_stats()
        self._load_indexed_folders()
        self._start_usn_monitor()
        self._do_search()

    def _on_scan_error(self, msg: str):
        self.progress.setVisible(False)
        logger.error("SCAN ERROR │ %s", msg)
        QMessageBox.warning(self, "스캔 오류", msg)

    def _start_usn_monitor(self):
        """USN 모니터 스레드를 재시작한다.

        스캔/재스캔 이후에는 기준 USN이 바뀔 수 있으므로 기존 모니터를 종료하고
        새 스레드를 띄워 기준점 불일치를 방지한다.
        """
        if self._usn_monitor is not None:
            self._usn_monitor.request_stop()
            self._usn_monitor.wait()
        self._usn_monitor = USNMonitorThread()
        self._usn_monitor.paths_updated.connect(self._on_usn_paths_changed)
        self._usn_monitor.needs_full_scan.connect(self._on_usn_needs_full_scan)
        self._usn_monitor.start()
        logger.info(" USN 모니터 시작 (폴링 간격 %ds)", USNMonitorThread.POLL_INTERVAL)

    def _on_usn_paths_changed(self, changed_paths: list):
        folders = list(self._folder_badges.keys())
        if not folders:
            return
        for path in changed_paths:
            norm_path = os.path.normpath(path).lower()
            for folder in folders:
                norm_folder = os.path.normpath(folder).lower()
                if norm_path.startswith(norm_folder + os.sep) or norm_path == norm_folder:
                    self._pending_reindex_by_folder.setdefault(folder, set()).add(path)
        for folder in folders:
            badge   = self._folder_badges.get(folder)
            pending = self._pending_reindex_by_folder.get(folder, set())
            if badge and pending:
                _set_badge(badge, "changed", f"{len(pending)}개 변경")

    def _on_usn_needs_full_scan(self):
        logger.warning("⚠️ USN Journal 만료 — 전체 MFT 재스캔 시작")
        self._start_scan()

    # ── 색인 폴더 관리 ────────────────────────────────────────────────────────

    def _load_indexed_folders(self):
        try:
            with get_connection() as conn:
                folders_with_status = get_indexed_folders_with_status(conn)
        except Exception:
            return

        self._folder_badges.clear()
        self._folder_indexed_at.clear()
        self.folders_list.clear()

        for folder, indexed_at in folders_with_status:
            self._folder_indexed_at[folder] = indexed_at

            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 22))
            row_w = QWidget()
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(4, 0, 4, 0)
            row_l.setSpacing(4)

            lbl = QLabel(folder)
            lbl.setFont(QFont("맑은 고딕", 8))

            badge = QLabel("")
            badge.setFixedHeight(18)
            badge.setFont(QFont("맑은 고딕", 7))
            if indexed_at is None:
                _set_badge(badge, "pending", "색인 대기")
            else:
                _set_badge(badge, "done", "✓ 완료")

            row_l.addWidget(lbl, stretch=1)
            row_l.addWidget(badge)
            self._folder_badges[folder] = badge
            item.setData(Qt.ItemDataRole.UserRole, folder)
            self.folders_list.addItem(item)
            self.folders_list.setItemWidget(item, row_w)

    def _add_indexed_folder(self):
        path = QFileDialog.getExistingDirectory(self, "본문 검색 대상 폴더 선택")
        if not path:
            return
        path = os.path.normpath(path)

        with get_connection() as conn:
            existing = get_indexed_folders(conn)

        def _is_subpath(child: str, parent: str) -> bool:
            return child.lower().startswith(parent.lower() + os.sep)

        for ex in existing:
            if _is_subpath(path, ex):
                QMessageBox.warning(
                    self, "추가 불가",
                    f"상위 폴더가 이미 등록되어 있습니다:\n{ex}\n\n"
                    f"'{path}'은(는) 이미 본문 검색 범위에 포함됩니다."
                )
                return

        children = [ex for ex in existing if _is_subpath(ex, path)]
        if children:
            names = "\n".join(f"  • {c}" for c in children)
            reply = QMessageBox.question(
                self, "하위 폴더 교체",
                f"다음 {len(children)}개 폴더가 '{path}'에 포함됩니다:\n{names}\n\n"
                "기존 항목을 제거하고 상위 폴더로 교체할까요?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        with get_connection() as conn:
            for c in children:
                remove_indexed_folder(conn, c)
            add_indexed_folder(conn, path)
            conn.commit()

        self._load_indexed_folders()
        logger.info("색인 폴더 추가: %s (하위 %d개 제거)", path, len(children))

    def _show_folder_context_menu(self, pos):
        item = self.folders_list.itemAt(pos)
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        act_open = QAction("폴더 열기", self)
        act_open.triggered.connect(lambda: os.startfile(path) if os.path.isdir(path) else None)
        menu.addAction(act_open)
        menu.addSeparator()
        act_del = QAction("🗑 본문 검색 대상에서 제거", self)
        act_del.triggered.connect(lambda: self._remove_indexed_folder(path))
        menu.addAction(act_del)
        menu.exec(self.folders_list.viewport().mapToGlobal(pos))

    def _remove_indexed_folder(self, path: str):
        reply = QMessageBox.question(
            self, "본문 검색 대상 폴더 제거",
            f"아래 폴더를 본문 검색 대상에서 제거할까요?\n\n{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        with get_connection() as conn:
            remove_indexed_folder(conn, path)
            conn.commit()
        self._folder_badges.pop(path, None)
        self._pending_reindex_by_folder.pop(path, None)
        self._load_indexed_folders()
        logger.info("색인 폴더 삭제: %s", path)

    def _get_registered_folders(self) -> list[str] | None:
        folders = list(self._folder_indexed_at.keys())
        return folders if folders else None

    # ── 색인 실행 ─────────────────────────────────────────────────────────────

    def _start_index_thread(self, attr: str, thread) -> None:
        """색인 스레드를 attr에 저장하고 공통 시그널을 연결한 뒤 시작한다.

        두 종류의 색인 스레드(FolderIndexThread, ContentReindexThread)가
        동일한 진행/완료 슬롯을 재사용하도록 연결을 통일한다.
        """
        setattr(self, attr, thread)
        thread.total_count.connect(self._on_index_total_known)
        thread.progress.connect(self._on_index_progress)
        thread.finished_signal.connect(self._on_reindex_finished)
        thread.finished_signal.connect(self._on_index_thread_done)
        thread.start()

    def _on_index_clicked(self):
        folders = list(self._folder_indexed_at.keys())
        if not folders:
            QMessageBox.information(self, "본문 검색 색인", "등록된 폴더가 없습니다.")
            return

        full_index_folders: list[str] = []
        pending_paths: list[str] = []
        pending_folders: list[str] = []

        for f in folders:
            if self._folder_indexed_at.get(f) is None:
                full_index_folders.append(f)
            else:
                pending = self._pending_reindex_by_folder.pop(f, set())
                if pending:
                    pending_paths.extend(pending)
                    pending_folders.append(f)

        if not full_index_folders and not pending_paths:
            QMessageBox.information(self, "본문 검색 색인", "모든 폴더가 이미 준비 완료 상태입니다.")
            return

        # 실행 대상 폴더 배지 선반영: 사용자가 즉시 상태 변화를 인지할 수 있게 한다.
        active_folders = full_index_folders + pending_folders
        for f in active_folders:
            if badge := self._folder_badges.get(f):
                _set_badge(badge, "indexing", "색인 중…")

        self._indexing_folders = active_folders
        self.btn_index.setEnabled(False)

        if full_index_folders and pending_paths:
            self._running_index_count = 2
            self._start_index_thread(
                '_folder_index_thread', FolderIndexThread(full_index_folders))
            self._start_index_thread(
                '_reindex_thread', ContentReindexThread(pending_paths))
            self.lbl_scan_status.setText("파일 수집 중…")
            logger.info(" 전체 색인: %s / 변경분: %d개", full_index_folders, len(pending_paths))
        elif full_index_folders:
            self._running_index_count = 1
            self._start_index_thread(
                '_folder_index_thread', FolderIndexThread(full_index_folders))
            self.lbl_scan_status.setText("파일 수집 중…")
            logger.info(" 전체 색인 시작: %s", full_index_folders)
        else:
            self._running_index_count = 1
            self._start_index_thread(
                '_reindex_thread', ContentReindexThread(pending_paths))
            self.lbl_scan_status.setText("변경분 색인 중…")
            logger.info(" 변경분 색인 시작: %d개 파일", len(pending_paths))

    def _on_index_thread_done(self, _count: int):
        # 카운터는 어떤 순서로 스레드가 끝나도 안전하도록 하한을 0으로 고정.
        self._running_index_count = max(self._running_index_count - 1, 0)
        if self._running_index_count <= 0:
            self.btn_index.setEnabled(True)

    def _on_index_total_known(self, total: int):
        self._index_total = total
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.lbl_scan_status.setText(f"본문 검색 색인 대상: {total:,}개 파일")

    def _on_index_progress(self, path: str, n: int):
        self.progress.setValue(n)
        total = self._index_total
        if total > 0:
            self.lbl_scan_status.setText(
                f"색인 {n:,}/{total:,} ({n * 100 // total}%): {os.path.basename(path)}"
            )
        else:
            self.lbl_scan_status.setText(f"색인 {n:,}: {os.path.basename(path)}")

    def _on_reindex_finished(self, count: int):
        # 동시 실행(전체+변경분)에서는 마지막 스레드에서만 완료 후처리를 수행한다.
        # (상태바/배지/indexed_at 갱신을 2회 실행하면 깜빡임과 잘못된 상태가 생길 수 있음)
        if self._running_index_count > 1:
            logger.info(" 색인 스레드 완료 대기 중… (남은 스레드: %d)", self._running_index_count - 1)
            return

        self.lbl_scan_status.setText(f"본문 검색 색인 완료: {count:,}개 파일")
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self._index_total = 0
        self._update_status_stats()
        logger.info(" 재색인 완료: %d개 파일 색인됨", count)

        now = time.time()
        try:
            with get_connection() as conn:
                for folder in getattr(self, '_indexing_folders', []):
                    update_indexed_at(conn, folder, now)
                conn.commit()
        except Exception:
            logger.exception("색인 완료 시각 저장 실패")
        for folder in getattr(self, '_indexing_folders', []):
            self._folder_indexed_at[folder] = now
            if badge := self._folder_badges.get(folder):
                pending = self._pending_reindex_by_folder.get(folder, set())
                _set_badge(badge, "changed" if pending else "done",
                           f"{len(pending)}개 변경" if pending else "✓ 완료")
        self._indexing_folders = []

    # ── 종료 처리 ─────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        for attr in ('_cache_init_thread', '_scanner_thread'):
            t = getattr(self, attr, None)
            if t is not None and t.isRunning():
                t.request_stop()
                t.wait(10000)
        for attr in ('_reindex_thread', '_folder_index_thread'):
            t = getattr(self, attr, None)
            if t is not None and t.isRunning():
                t.wait(10000)
        if self._usn_monitor is not None:
            self._usn_monitor.request_stop()
            self._usn_monitor.wait(5000)
        try:
            if mft_cache.count() > 0:
                with get_connection() as conn:
                    mft_cache.save_to_db(conn)
        except Exception:
            logger.exception("closeEvent: 캐시 저장 실패")
        super().closeEvent(event)

    # ── 상태 표시 ─────────────────────────────────────────────────────────────

    def _update_status_stats(self):
        try:
            with get_connection() as conn:
                stats = get_stats(conn)
            self.statusBar().showMessage(
                f"인덱스: 파일 {stats['total_files']:,}개 | "
                f"내용 인덱싱 {stats['indexed_contents']:,}개"
            )
        except Exception:
            self.statusBar().showMessage("DB 없음 — 본문 검색 색인이 필요합니다")


# ── 모듈 수준 유틸리티 ────────────────────────────────────────────────────────

def _format_size(size: int) -> str:
    if size < 1024:        return f"{size} B"
    if size < 1024 ** 2:   return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:   return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def _extract_highlight_terms(query: str) -> list[str]:
    """FTS5 쿼리에서 하이라이트용 순수 단어/구문을 추출한다."""
    terms: list[str] = []
    q = query.strip()

    for phrase in re.findall(r'"([^"]+)"', q):
        if phrase.strip():
            terms.append(phrase.strip())
    q = re.sub(r'"[^"]*"', ' ', q)

    for near_body in re.findall(r'NEAR\s*\(([^)]+)\)', q, flags=re.IGNORECASE):
        for w in re.split(r'[\s,]+', near_body):
            w = w.strip().rstrip('*')
            if w and not w.isdigit():
                terms.append(w)
    q = re.sub(r'NEAR\s*\([^)]*\)', ' ', q, flags=re.IGNORECASE)

    q = re.sub(r'\b(AND|OR|NOT)\b', ' ', q, flags=re.IGNORECASE)
    q = re.sub(r'[\^()]+', ' ', q)
    for w in q.split():
        if w := w.strip().rstrip('*'):
            terms.append(w)

    return terms


def _build_full_content_html(content: str, keyword: str) -> str:
    """전체 파일 내용을 HTML로 렌더링한다. keyword가 있으면 하이라이트."""
    text = html.escape(content)

    if keyword:
        anchor_placed = [False]
        count         = [0]

        def replacer(m: re.Match) -> str:
            if count[0] >= 500:
                return m.group()
            count[0] += 1
            prefix = ""
            if not anchor_placed[0]:
                anchor_placed[0] = True
                prefix = '<a name="first_match"></a>'
            return f'{prefix}<b style="color:#1565c0;background:#e3f2fd;">{m.group()}</b>'

        for term in _extract_highlight_terms(keyword):
            escaped = html.escape(term)
            if escaped:
                text = re.compile(re.escape(escaped), re.IGNORECASE).sub(replacer, text)

    text = text.replace("\n", "<br>")
    return (
        '<p style="font-family:\'맑은 고딕\',sans-serif;font-size:9pt;'
        f'color:#333;margin:0;white-space:pre-wrap;">{text}</p>'
    )
