# 7강: PyQt6 GUI 아키텍처

## 개요

SeekSeek의 GUI는 **PyQt6** (Qt 6의 Python 바인딩)으로 구현되어 있다.
이 강의에서는 Qt의 Model/View 아키텍처, 시그널/슬롯 패턴,
그리고 SeekSeek의 UI 구성 요소를 상세히 다룬다.

---

## 1. Qt Model/View 아키텍처

### MVC vs Model/View

Qt는 전통적인 MVC(Model-View-Controller)에서 Controller를 View에 통합한
**Model/View** 패턴을 사용한다:

```
전통 MVC:
  Model ←→ Controller ←→ View ←→ User

Qt Model/View:
  Model ←→ View(+Delegate) ←→ User

Model(데이터) ←─ 시그널/슬롯 ─→ View(표시+편집)
                                    │
                              Delegate(셀 렌더링 커스터마이징)
```

### 왜 Model/View인가?

| 장점 | 설명 |
|------|------|
| 데이터 분리 | Model 변경 없이 View 교체 가능 |
| 가상 렌더링 | 100만 행이어도 보이는 부분만 렌더링 |
| 메모리 효율 | QTableWidget처럼 QTableWidgetItem 객체를 행 수만큼 만들지 않음 |
| 정렬/필터 | QSortFilterProxyModel로 뷰 단에서 정렬·필터링 |

---

## 2. ResultTableModel (QAbstractTableModel)

SeekSeek의 검색 결과 테이블은 `QAbstractTableModel`을 상속한
커스텀 모델 `ResultTableModel`로 구현되어 있다.

### 열 구성

```python
_COL_LABELS = ["이름", "경로", "크기", "수정일", "확장자", "매칭"]
#               0       1       2       3        4        5

_COL_WIDTHS  = [200, 300, 60, 120, 55, 80]  # 픽셀 단위 열 너비
```

### 필수 오버라이드 메서드

```python
class ResultTableModel(QAbstractTableModel):
    """검색 결과를 테이블 형태로 제공하는 모델"""
    
    def __init__(self):
        super().__init__()
        self._results: list[SearchResult] = []
    
    def rowCount(self, parent=QModelIndex()) -> int:
        """테이블의 행 수 = 검색 결과 개수"""
        return len(self._results)
    
    def columnCount(self, parent=QModelIndex()) -> int:
        """테이블의 열 수 = 6"""
        return len(_COL_LABELS)
    
    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        """특정 셀의 데이터를 반환한다.
        
        role에 따라 다른 데이터를 제공:
          DisplayRole  → 화면에 표시할 문자열
          ToolTipRole  → 마우스 호버 시 전체 경로
          TextAlignmentRole → 숫자는 우측 정렬
          ForegroundRole → 디렉터리는 다른 색상
        """
        if not index.isValid():
            return None
        
        row = index.row()
        col = index.column()
        result = self._results[row]
        
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0: return result.name
            if col == 1: return result.path
            if col == 2: return _format_size(result.size)
            if col == 3: return _format_date(result.modified)
            if col == 4: return result.extension
            if col == 5: return result.match_type
    
    def headerData(self, section, orientation, role):
        """열 헤더 텍스트를 반환한다."""
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _COL_LABELS[section]
```

### 가상 렌더링의 원리

```
╔══════════════════════════════════════╗
║   QTableView (화면에 보이는 영역)    ║
║                                      ║
║   ┌──────┬──────┬──────┬──────┐     ║
║   │이름  │경로  │크기  │수정일│     ║ ← headerData() 호출
║   ├──────┼──────┼──────┼──────┤     ║
║   │row 0 │ ...  │ ...  │ ...  │     ║ ← data(row=0) 호출
║   │row 1 │ ...  │ ...  │ ...  │     ║ ← data(row=1) 호출
║   │row 2 │ ...  │ ...  │ ...  │     ║ ← data(row=2) 호출
║   │  :   │  :   │  :   │  :   │     ║
║   │row 19│ ...  │ ...  │ ...  │     ║ ← data(row=19) 호출
║   └──────┴──────┴──────┴──────┘     ║
║                                      ║
║   [스크롤바] ▓▓░░░░░░░░░░░░░░░     ║
╚══════════════════════════════════════╝

총 100,000개 결과 중 화면에 보이는 20행만 data() 호출됨!
→ QTableWidget이라면 100,000개 QTableWidgetItem 객체가 필요
→ QAbstractTableModel은 결과 리스트만 유지하면 됨
```

### 데이터 갱신

```python
def set_results(self, results: list[SearchResult]):
    """검색 결과를 교체한다."""
    self.beginResetModel()   # View에 "데이터가 완전히 바뀐다" 알림
    self._results = results
    self.endResetModel()     # View가 화면을 다시 그림
```

> **beginResetModel / endResetModel**: 이 쌍을 호출해야
> View가 이전 데이터에 대한 참조를 안전하게 해제하고 새로 그린다.
> 부분 변경이라면 `dataChanged` 시그널이 더 효율적이다.

---

## 3. MainWindow 구성

### UI 레이아웃

```
┌──────────────────────────────────────────────────────────┐
│  [검색 입력창]  [검색 버튼]  [콘텐츠 검색 입력창] [설정]  │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────────────────┬───────────────────────┐   │
│  │     검색 결과 테이블      │     미리보기 패널     │   │
│  │     (QTableView)         │     (QTextBrowser)    │   │
│  │                          │                       │   │
│  │  이름  | 경로  | 크기... │  파일 내용 미리보기   │   │
│  │  ──────────────────────  │  + 검색어 하이라이팅  │   │
│  │  file1.py | C:\...       │  + Ctrl+F 찾기        │   │
│  │  file2.doc | C:\...      │                       │   │
│  │  ...                     │                       │   │
│  └──────────────────────────┴───────────────────────┘   │
│                                                          │
├──────────────────────────────────────────────────────────┤
│  [상태 표시줄]  결과: 1,234개  |  인덱싱: 45%           │
│                                                          │
│  인덱싱 대상 폴더:                                       │
│  ┌──────────────────────────────────────────────┐       │
│  │ C:\Users\... [완료✓]  C:\Projects\... [대기] │       │
│  │ [+ 폴더 추가]  [본문 검색 색인]               │       │
│  └──────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────┘
```

### 폴더 배지 시스템

각 인덱싱 대상 폴더에는 상태를 나타내는 **배지(badge)**가 표시된다:

실제 배지 텍스트:

| 상태 키 | 표시 텍스트 | 의미 |
|---------|------------|------|
| `"pending"` | `"색인 대기"` | 아직 인덱싱 안 됨 |
| `"indexing"` | `"색인 중…"` | 현재 스캔/인덱싱 진행 중 |
| `"done"` | `"✓ 완료"` | 인덱싱 완료 |
| `"changed"` | `"{n}개 변경"` | USN으로 변경된 파일 n개 감지 |

---

## 4. 미리보기 패널

### 검색어 하이라이팅

```python
def _build_full_content_html(self, text: str, keywords: list[str]) -> str:
    """텍스트에서 검색 키워드를 HTML 마크업으로 하이라이팅한다.
    
    예시:
      text = "Python은 프로그래밍 언어입니다"
      keywords = ["python", "언어"]
      
      결과:
      "<span style='background:yellow'>Python</span>은 프로그래밍 
       <span style='background:yellow'>언어</span>입니다"
    """
```

### Ctrl+F 찾기 기능

미리보기 패널 내에서 **Ctrl+F**로 추가 텍스트 검색이 가능하다:

```python
def _find_in_preview(self, text: str, forward: bool = True):
    """QTextBrowser 내에서 텍스트를 찾아 스크롤한다."""
    flags = QTextDocument.FindFlag(0)
    if not forward:
        flags |= QTextDocument.FindFlag.FindBackward
    
    cursor = self._preview.document().find(text, self._preview.textCursor(), flags)
    if not cursor.isNull():
        self._preview.setTextCursor(cursor)
        self._preview.ensureCursorVisible()
```

---

## 5. 시그널/슬롯 연결 맵

```
MainWindow
│
├── _search_input.returnPressed ──→ _do_search()
├── _search_button.clicked ──→ _do_search()
│
├── _scanner.finished ──→ _on_scan_finished()
├── _scanner.progress ──→ _on_scan_progress()
├── _scanner.error ──→ _on_scan_error()
│
├── _usn_monitor.paths_updated ──→ _on_usn_paths_changed()
│
├── table.selectionModel().currentRowChanged ──→ _on_selection_changed()
│   └── 선택된 파일의 미리보기 표시
│
├── table.doubleClicked ──→ _on_double_click()
│   └── 파일 탐색기에서 열기
│
├── btn_add.clicked ──→ _add_indexed_folder()
└── btn_index.clicked ──→ _on_index_clicked()
```

---

## 6. 이벤트 루프와 반응성

### 메인 스레드에서 절대 하면 안 되는 것

```python
# ❌ 잘못된 예: 메인 스레드에서 무거운 작업
def _do_search(self):
    results = scan_entire_mft()  # 수 초 걸림 → GUI 멈춤!
    self._model.set_results(results)

# ✅ 올바른 예: 워커 스레드 사용
def _do_search(self):
    self._scanner = ScannerThread()
    self._scanner.finished.connect(self._on_results)
    self._scanner.start()  # 별도 스레드에서 실행

def _on_results(self, results):
    # 이 슬롯은 메인 스레드의 이벤트 루프에서 호출됨
    self._model.set_results(results)
```

> **규칙**: GUI 위젯 조작은 **반드시** 메인 스레드에서만 수행한다.
> 워커 스레드에서 직접 위젯을 수정하면 크래시가 발생한다.
> 워커 스레드는 시그널을 통해 메인 스레드에 데이터를 전달한다.

---

## 7. 다이얼로그

### ExcludedFoldersDialog

```
┌──────────────────────────────────────┐
│  제외 폴더 관리                       │
│                                      │
│  ┌──────────────────────────────┐   │
│  │ node_modules                 │   │
│  │ .git                        │   │
│  │ __pycache__                 │   │
│  │ .venv                       │   │
│  └──────────────────────────────┘   │
│                                      │
│  [추가]  [제거]  [확인]  [취소]      │
└──────────────────────────────────────┘
```

### SearchHelpDialog

FTS5 검색 문법을 사용자에게 안내하는 다이얼로그:

```
┌──────────────────────────────────────┐
│  검색 도움말                          │
│                                      │
│  기본 검색:    hello world           │
│  OR 검색:     hello OR world        │
│  NOT 검색:    hello NOT world       │
│  구문 검색:    "hello world"         │
│  접두사:       hel*                  │
│  확장자:       *.py                  │
│  경로 포함:    path:Documents        │
│                                      │
│  [확인]                              │
└──────────────────────────────────────┘
```

---

## 8. SeekSeek에서의 구현 위치

| 구현 요소 | 파일 | 클래스/함수 |
|-----------|------|-------------|
| 메인 윈도우 | `gui/main_window.py` | `MainWindow` |
| 결과 테이블 모델 | `gui/main_window.py` | `ResultTableModel` |
| 미리보기 패널 | `gui/main_window.py` | `MainWindow._build_full_content_html()` |
| 다이얼로그 | `gui/dialogs.py` | `ExcludedFoldersDialog`, `SearchHelpDialog`, `AboutDialog` |
| 앱 진입점 | `main.py` | `main()` — QApplication, 관리자 권한 확인 |

---

## 참고 자료

- [Qt 6 Model/View Programming](https://doc.qt.io/qt-6/model-view-programming.html)
- [PyQt6 공식 문서](https://www.riverbankcomputing.com/static/Docs/PyQt6/)
- [Qt Signals & Slots](https://doc.qt.io/qt-6/signalsandslots.html)
