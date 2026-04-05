# 3강: SQLite FTS5와 역색인(Inverted Index)

## 개요

**FTS5(Full-Text Search 5)**는 SQLite의 **전문 검색(full-text search)** 가상 테이블 모듈이다.
SeekSeek은 FTS5를 사용하여 수만~수십만 개 파일의 이름과 문서 내용을 **밀리초 단위**로 검색한다.

이 강의에서는 FTS5의 핵심 원리인 **역색인(Inverted Index)** 구조부터,
SeekSeek이 사용하는 External Content 모드와 트리거 동기화까지 다룬다.

---

## 1. 역색인(Inverted Index)이란?

### 순방향 인덱스 vs 역색인

```
순방향 인덱스 (Forward Index):
  문서1 → ["hello", "world", "foo"]
  문서2 → ["hello", "bar", "baz"]
  문서3 → ["world", "baz", "qux"]

  → "hello"가 포함된 문서를 찾으려면 모든 문서를 순차 탐색
  → O(N) 시간 복잡도 (N = 문서 수)

역색인 (Inverted Index):
  "hello" → [문서1, 문서2]
  "world" → [문서1, 문서3]
  "foo"   → [문서1]
  "bar"   → [문서2]
  "baz"   → [문서2, 문서3]
  "qux"   → [문서3]

  → "hello"가 포함된 문서를 즉시 조회
  → O(1) ~ O(log N) 시간 복잡도
```

역색인은 **토큰(token) → 문서 목록(posting list)** 의 매핑이다.
이것이 Google, Elasticsearch, Lucene 등 모든 전문 검색 엔진의 핵심 자료 구조다.

### FTS5의 역색인 구조 (상세)

FTS5는 각 토큰에 대해 **doclist(문서 목록)**를 저장한다:

```
토큰 "python":
  ┌─────────────────────────────────────────────┐
  │  doclist:                                   │
  │  rowid=1  col=0  offset=3                   │
  │  rowid=1  col=1  offset=0                   │
  │  rowid=5  col=0  offset=7                   │
  │  rowid=12 col=1  offset=2, offset=15        │
  └─────────────────────────────────────────────┘

각 항목에는:
  - rowid: 문서(행)의 고유 ID
  - col: 토큰이 나타난 열 번호
  - offset: 열 값 내에서 토큰의 위치 (몇 번째 토큰인지)
```

이 정보 덕분에:
- **단순 검색**: "python" 포함 문서 → doclist 조회만으로 즉시
- **구문 검색**: "python tutorial" → 두 토큰이 연속 위치에 있는 문서
- **근접 검색**: NEAR(python, tutorial, 5) → 5 토큰 이내에 함께 나타나는 문서
- **BM25 랭킹**: 각 문서별 히트 수를 이용한 관련도 계산

---

## 2. FTS5 가상 테이블 생성

### 기본 생성

```sql
-- 기본 FTS5 테이블 (모든 데이터를 내부에 저장)
CREATE VIRTUAL TABLE docs USING fts5(title, body);

-- 데이터 삽입
INSERT INTO docs VALUES('Python Guide', 'Python is a programming language');
INSERT INTO docs VALUES('SQL Tutorial', 'SQL is used for databases');

-- 전문 검색
SELECT * FROM docs WHERE docs MATCH 'python';
SELECT * FROM docs WHERE docs MATCH 'python AND programming';
```

### External Content 모드 (SeekSeek 사용 방식)

SeekSeek은 **External Content** 모드를 사용한다. 이 모드에서 FTS5 테이블은
인덱스만 저장하고, 실제 데이터는 별도 테이블(content table)에서 조회한다.

```sql
-- SeekSeek의 FTS5 설정 (core/indexer.py에서 발췌)

-- 원본 데이터 테이블
CREATE TABLE files (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    extension TEXT,
    size      INTEGER,
    modified  REAL,
    has_content INTEGER DEFAULT 0,
    file_ref  INTEGER
);

-- FTS5 External Content 가상 테이블
CREATE VIRTUAL TABLE fts_files USING fts5(
    name,                     -- 검색할 열: 파일명
    path,                     -- 검색할 열: 경로
    content='files',          -- ← External Content: files 테이블 참조
    content_rowid='id'        -- ← 원본 테이블의 PK 열 이름
);
```

**External Content의 장점**:
- 데이터 중복 저장 방지 → 디스크 공간 절약
- 원본 테이블의 다른 열(size, modified 등)에 자유롭게 접근 가능
- FTS5 인덱스는 토큰·위치 정보만 저장

**External Content의 주의점**:
- 원본 테이블의 변경을 FTS5에 수동으로 반영해야 함
- → SeekSeek은 **트리거(trigger)**로 자동 동기화

---

## 3. 트리거 기반 FTS5 동기화

External Content FTS5 테이블은 원본 테이블 변경을 자동으로 감지하지 못한다.
SeekSeek은 INSERT/DELETE/UPDATE 트리거로 동기화한다:

```sql
-- INSERT 트리거: 새 파일이 추가되면 FTS 인덱스에도 추가
CREATE TRIGGER IF NOT EXISTS trg_files_ai AFTER INSERT ON files BEGIN
    INSERT INTO fts_files(rowid, name, path)
    VALUES (new.id, new.name, new.path);
END;

-- DELETE 트리거: 파일이 삭제되면 FTS 인덱스에서도 제거
-- 'delete' 명령어로 기존 인덱스 항목을 제거한다
CREATE TRIGGER IF NOT EXISTS trg_files_ad AFTER DELETE ON files BEGIN
    INSERT INTO fts_files(fts_files, rowid, name, path)
    VALUES ('delete', old.id, old.name, old.path);
END;

-- UPDATE 트리거: 파일 정보가 변경되면 기존 항목 제거 후 새 항목 추가
CREATE TRIGGER IF NOT EXISTS trg_files_au AFTER UPDATE ON files BEGIN
    INSERT INTO fts_files(fts_files, rowid, name, path)
    VALUES ('delete', old.id, old.name, old.path);
    INSERT INTO fts_files(rowid, name, path)
    VALUES (new.id, new.name, new.path);
END;
```

> **핵심**: FTS5의 DELETE는 특별한 구문을 사용한다.
> `INSERT INTO fts_table(fts_table, rowid, ...)  VALUES('delete', old_rowid, ...)`
> 이 "delete 명령"은 역색인에서 해당 토큰-문서 매핑을 제거한다.

---

## 4. FTS5 쿼리 문법

### 기본 쿼리

```sql
-- 단일 토큰 검색
SELECT * FROM fts_files WHERE fts_files MATCH 'python';

-- AND (암시적: 공백으로 구분)
SELECT * FROM fts_files WHERE fts_files MATCH 'python tutorial';
-- = SELECT * FROM fts_files WHERE fts_files MATCH 'python AND tutorial';

-- OR
SELECT * FROM fts_files WHERE fts_files MATCH 'python OR java';

-- NOT
SELECT * FROM fts_files WHERE fts_files MATCH 'python NOT test';

-- 구문(phrase) 검색
SELECT * FROM fts_files WHERE fts_files MATCH '"hello world"';
```

### 접두사(prefix) 검색

```sql
-- "py"로 시작하는 모든 토큰 매칭
SELECT * FROM fts_files WHERE fts_files MATCH 'py*';
```

> **SeekSeek의 자동 접두사 변환**: 사용자가 "report"를 입력하면,
> `_build_fts_query()`가 자동으로 `report*`로 변환하여 보다 넓은 범위를 검색한다.

### 열 필터

```sql
-- name 열에서만 검색
SELECT * FROM fts_files WHERE fts_files MATCH 'name : python';

-- path 열에서만 검색
SELECT * FROM fts_files WHERE fts_files MATCH 'path : /home/user';
```

---

## 5. BM25 랭킹 알고리즘

FTS5는 **BM25(Best Matching 25)** 알고리즘으로 검색 결과의 관련도를 계산한다.

### BM25 공식

$$
\text{score}(D, Q) = -1 \times \sum_{i=1}^{n} \text{IDF}(q_i) \cdot \frac{f(q_i, D) \cdot (k_1 + 1)}{f(q_i, D) + k_1 \cdot \left(1 - b + b \cdot \frac{|D|}{\text{avgdl}}\right)}
$$

여기서:
- $D$ = 문서, $Q$ = 검색 쿼리
- $q_i$ = 쿼리의 i번째 구문(phrase)
- $f(q_i, D)$ = 문서 D에서 구문 $q_i$의 출현 빈도
- $|D|$ = 문서 D의 토큰 수
- $\text{avgdl}$ = 전체 문서의 평균 토큰 수
- $k_1 = 1.2$, $b = 0.75$ (하드코딩된 상수)

### IDF (Inverse Document Frequency)

$$
\text{IDF}(q_i) = \ln\left(\frac{N - n(q_i) + 0.5}{n(q_i) + 0.5}\right)
$$

- $N$ = 전체 문서 수
- $n(q_i)$ = 구문 $q_i$를 포함하는 문서 수
- 희귀한 토큰일수록(= 적은 문서에 나타남) IDF 값이 높아져 더 높은 가중치

### FTS5의 BM25 특이점

FTS5는 BM25 결과에 **-1을 곱한다**. 따라서:
- **더 좋은 매칭일수록 더 작은 (음의) 값**
- `ORDER BY rank` (기본 ASC)로 정렬하면 자연스럽게 관련도 높은 순서

```sql
-- 관련도 높은 순서로 정렬
SELECT *, rank FROM fts_files WHERE fts_files MATCH 'python' ORDER BY rank;

-- 열별 가중치 부여 (name 열에 10배 가중치)
SELECT *, bm25(fts_files, 10.0, 1.0) as score
FROM fts_files WHERE fts_files MATCH 'python'
ORDER BY score;
```

---

## 6. FTS5 내부 저장 구조

### Segment B-Tree

FTS5는 역색인을 **segment b-tree** 형태로 저장한다:

```
트랜잭션 커밋 → 새 segment b-tree 생성 (Level 0)
                    │
         ┌──────────┴──────────┐
         │  automerge 작동     │
         ▼                     │
남은 Level 0 b-tree들을 병합 → Level 1 b-tree 생성
                    │
         ┌──────────┴──────────┐
         │  다시 automerge      │
         ▼                     │
Level 1 b-tree들을 병합    → Level 2 b-tree 생성
         ...
```

- 검색 시 모든 segment b-tree를 조회하고 결과를 병합한다
- segment가 너무 많아지면 검색이 느려진다
- `automerge` 옵션으로 자동 병합 임계값 조절 (기본: 4)
- `INSERT INTO ft(ft) VALUES('optimize')` 로 모든 세그먼트를 하나로 병합

### 물리 테이블 (Shadow Tables)

FTS5 테이블 `ft`를 생성하면, SQLite는 다음 shadow table을 자동 생성한다:

| Shadow Table | 용도 |
|-------------|------|
| `ft_data` | 역색인 데이터 (segment b-tree 블롭) |
| `ft_idx` | segment b-tree 인덱스 (term → 페이지 맵핑) |
| `ft_config` | FTS5 설정 값 (pgsz, automerge 등) |
| `ft_docsize` | 각 문서의 열별 토큰 수 (BM25 계산용) |
| `ft_content` | 문서 원본 데이터 (External Content 모드에서는 미사용) |

---

## 7. SeekSeek의 FTS5 활용 패턴

### 이중 FTS5 테이블 구조

```
┌──────────────┐     ┌──────────────────┐
│   files      │ ←── │   fts_files      │  파일명/경로 검색
│   (원본)     │     │   (FTS5 인덱스)   │
└──────────────┘     └──────────────────┘

┌──────────────┐     ┌──────────────────┐
│ file_contents│ ←── │   fts_contents   │  문서 내용 검색
│   (원본)     │     │   (FTS5 인덱스)   │
└──────────────┘     └──────────────────┘
```

- `fts_files`: 파일 이름과 경로에 대한 전문 검색
- `fts_contents`: 추출된 문서 텍스트에 대한 전문 검색
- 검색 시 두 결과를 통합하여 파일명 매칭과 내용 매칭을 모두 제공

### 쿼리 빌더 (_build_fts_query)

```python
def _build_fts_query(user_input: str) -> str:
    """사용자 입력을 FTS5 쿼리로 변환한다.
    
    변환 규칙:
      - 각 토큰에 자동으로 '*' 접미사 추가 (접두사 매칭)
      - 특수 문자 이스케이프
      - 빈 토큰 무시
    
    예시:
      "hello world" → "hello* world*"
      "main.py"     → "main* py*"
    """
```

---

## 8. FTS5 성능 최적화 팁

| 기법 | 설명 | SeekSeek 적용 |
|------|------|--------------|
| Prefix Index | `prefix='2 3'`으로 접두사 인덱스 생성 | 미적용 (기본값 사용) |
| External Content | 데이터 중복 방지 | ✅ 적용 |
| WAL 모드 | 읽기/쓰기 병행 | ✅ `PRAGMA journal_mode=WAL` |
| 일괄 INSERT | 트랜잭션 내 대량 삽입 | ✅ 적용 |
| `rank` vs `bm25()` | 내장 rank 열이 더 빠름 | ✅ `ORDER BY rank` 사용 |

---

## 참고 자료

- [SQLite FTS5 공식 문서](https://www.sqlite.org/fts5.html)
- [Wikipedia: Inverted Index](https://en.wikipedia.org/wiki/Inverted_index)
- [Wikipedia: Okapi BM25](https://en.wikipedia.org/wiki/Okapi_BM25)
- [SQLite FTS5 Data Structures](https://www.sqlite.org/fts5.html#fts5_data_structures)
