# 6강: 검색 엔진 (Search Engine)

## 개요

SeekSeek의 검색 엔진은 **이중 모드(dual-mode)** 설계로,
사용자의 검색 의도에 따라 최적의 경로를 자동 선택한다:

- **파일명 검색**: 메모리 내 MFT 캐시에서 O(N) 글롭 패턴 매칭
- **내용 검색**: SQLite FTS5 역색인을 통한 전문 검색 + BM25 랭킹

---

## 1. 검색 모드 자동 선택

```
사용자 입력
     │
     ├── content_query 있음? (내용 검색 텍스트 입력)
     │   │
     │   ├── Yes → DB FTS5 모드
     │   │         (파일명 + 내용 동시 검색)
     │   │
     │   └── No  → MFT 캐시 모드
     │             (파일명만 검색, 초고속)
     │
     ▼
  SearchResult 리스트 반환
```

### SearchResult 데이터 구조

```python
@dataclass
class SearchResult:
    file_id:    int          # DB의 files.id (캐시 모드에서는 0)
    path:       str          # 파일 절대 경로
    name:       str          # 파일명 (basename)
    extension:  str          # 확장자 (.py, .docx 등)
    size:       int          # 바이트 크기
    modified:   float        # Unix timestamp
    match_type: str          # "filename" | "content" | "both"
    is_dir:     bool         # 디렉터리 여부
```

---

## 2. MFT 캐시 모드 (파일명 검색)

### MftCache 내부 구조

```
MftCache
├── _by_ref: dict[int, tuple]    # file_ref → (file_ref, path, name, ext, size, mtime, is_dir, name_lower)
├── _by_path: dict[str, int]     # path_lower → file_ref
└── _lock: threading.RLock       # 스레드 안전성 보장

메모리 사용: ~100바이트/파일 × 100만 파일 ≈ 100 MB
```

### 글롭 패턴 매칭

```python
def search(self, query: str, folder: str = None) -> list[tuple]:
    """캐시에서 파일명으로 검색한다.
    
    지원 패턴:
      - "*.py"          → 확장자가 .py인 파일
      - "test*"         → test로 시작하는 파일
      - "hello world"   → "hello"와 "world" 모두 포함 (AND)
      - "*.py *.js"     → .py이면서 이름에 .js도 포함? (비직관적)
    
    실제 동작:
      1. 쿼리를 공백으로 분할 → 토큰 리스트
      2. 각 토큰에 '*'가 없으면 양쪽에 '*' 추가  → "*토큰*"
      3. 모든 _by_ref 항목을 순회하며 fnmatch 체크
      4. 모든 토큰이 매칭되는 항목만 반환 (AND 조건)
    """
```

```
검색어: "main py"

토큰 분할:     ["main", "py"]
글롭 변환:     ["*main*", "*py*"]
매칭 대상:     name_lower 필드

"main.py"        → "*main*" ✓, "*py*" ✓ → 매칭!
"main_test.cpp"  → "*main*" ✓, "*py*" ✗ → 불매칭
"deploy.py"      → "*main*" ✗             → 불매칭
```

### O(N) 선형 탐색의 타당성

MFT 캐시 검색은 `O(N)` 선형 탐색이다. 왜 이것이 충분한가?

- N = 100만 파일 기준, `fnmatch` 비교는 C 확장으로 구현되어 빠름
- dict 값 순회는 메모리 연속 접근으로 캐시 친화적
- 실측: 100만 파일에서 약 **50~200ms** (Everything 앱과 유사한 수준)

> 이보다 빠르려면 접미사 배열(suffix array)이나 트라이(trie) 구조가 필요하지만,
> 현재 성능으로 충분하다.

---

## 3. DB FTS5 모드 (내용 검색)

### 검색 흐름

```
사용자 입력: name_query="report", content_query="매출"
     │
     ├── 1단계: fts_contents에서 "매출*" 검색
     │   → content_hits = {file_id: rank_score}
     │
     ├── 2단계: fts_files에서 "report*" 검색
     │   → filename_hits = {file_id: rank_score}
     │
     ├── 3단계: 두 결과 통합
     │   ├── 양쪽 모두 매칭 → match_type = "both", priority = 0
     │   ├── 파일명만 매칭  → match_type = "filename", priority = 1
     │   └── 내용만 매칭    → match_type = "content", priority = 2
     │
     ├── 4단계: folder 필터 적용
     │   └── path LIKE 'C:\Users\%' 등
     │
     └── 5단계: 정렬 (priority ASC, rank ASC)
```

### FTS5 쿼리 빌더

```python
def _build_fts_query(raw: str) -> str:
    """사용자 입력을 FTS5 쿼리 문자열로 변환한다.
    
    변환 규칙:
      1. 입력을 토큰으로 분할
      2. 특수문자 제거/이스케이프
      3. 각 토큰에 '*' 접미사 추가 (접두사 매칭)
      4. 토큰을 공백으로 결합 (암시적 AND)
    
    예시:
      "hello world"  → "hello* world*"
      "main.py"      → "main* py*"     (점이 토큰 구분자)
      "2024-report"  → "2024* report*"
    """
```

> **접두사 매칭의 장점**: 사용자가 "repo"만 입력해도 "report", "repository" 등을 모두 찾는다.
> FTS5는 내부적으로 B-tree 범위 스캔(`term >= "repo" AND term < "repp"`)으로 처리한다.

### 폴더 필터

```python
def _folder_clause(folder: str) -> tuple[str, list]:
    """폴더 경로를 SQL LIKE 절로 변환한다.
    
    특수문자 이스케이프:
      '%' → '\%',  '_' → '\_'
    
    결과:
      folder="C:\\Users\\test"
      → ("path LIKE ? ESCAPE '\\'", ["C:\\Users\\test\\%"])
    """
```

---

## 4. 결과 정렬 전략

### 매칭 우선순위

```python
PRIORITY = {
    "both":     0,   # 파일명 + 내용 모두 매칭 → 최상위
    "filename": 1,   # 파일명만 매칭
    "content":  2,   # 내용만 매칭 → 최하위
}
```

### GUI에서의 정렬

```python
def _sort_results(self, results: list[SearchResult], column: int, order: Qt.SortOrder):
    """결과 테이블 정렬
    
    폴더 우선 정렬:
      1. is_dir DESC (디렉터리가 항상 위)
      2. 사용자 선택 열로 정렬
    """
```

---

## 5. 검색 파이프라인 최적화

| 기법 | 설명 |
|------|------|
| 접두사 자동 변환 | 사용자 편의를 위해 모든 토큰에 `*` 추가 |
| 이중 FTS 테이블 | 파일명과 내용을 분리하여 독립적 인덱스 유지 |
| External Content | 데이터 중복 방지로 디스크 절약 |
| 결과 통합 | 두 FTS 결과를 dict 기반 O(1) 머지 |
| 폴더 필터 | SQL LIKE로 서브셋 필터링 (인덱스 스캔 불필요) |
| WAL 모드 | 쓰기 중에도 검색 가능 (읽기/쓰기 병행) |

---

## 6. SeekSeek에서의 구현 위치

| 구현 요소 | 파일 | 함수/클래스 |
|-----------|------|------------|
| 검색 진입점 | `core/searcher.py` | `search()` |
| FTS 쿼리 빌더 | `core/searcher.py` | `_build_fts_query()` |
| 폴더 필터 | `core/searcher.py` | `_folder_clause()` |
| 결과 데이터 | `core/searcher.py` | `SearchResult` |
| MFT 캐시 검색 | `core/mft_cache.py` | `MftCache.search()` |
| 검색 실행 (GUI) | `gui/main_window.py` | `MainWindow._do_search()` |
| 결과 테이블 모델 | `gui/main_window.py` | `ResultTableModel` |

---

## 참고 자료

- [SQLite FTS5 Query Syntax](https://www.sqlite.org/fts5.html#full_text_query_syntax)
- [Python fnmatch 모듈](https://docs.python.org/3/library/fnmatch.html)
- [BM25 알고리즘 (Wikipedia)](https://en.wikipedia.org/wiki/Okapi_BM25)
