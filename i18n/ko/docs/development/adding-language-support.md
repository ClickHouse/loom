<!-- i18n: source=docs/development/adding-language-support.md blob=ee2dee0c86ab7bfb46f22017d1d71ab8005a2276 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md), which is authoritative.

<a id="add-language-support-to-the-code-index"></a>

# 코드 인덱스에 언어 지원 추가하기

코드 인덱스는 저장소를 두 개의 티어로 읽습니다.

- **빠른 티어**(tree-sitter 문법과 직접 작성한 스캐너)는 모든 커밋에서 모든 파일에 대해 실행됩니다. 청크, 심볼, 호출 엣지를 생성합니다.
- **컴파일러 티어**(변경된 파일에 대한 SCIP 인덱서)는 인덱서가 있는 경우 컴파일러 수준으로 정확한 심볼과 엣지를 추가합니다.

빠른 티어에 해당 언어를 처리하는 레인이 있으면 그 언어를 지원하는 것입니다. [코드 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)에서 티어에 대한 설명과 각 언어가 제공받는 기능 목록을 확인할 수 있습니다.

<a id="the-one-rule-stay-outside-the-extractor-stamp"></a>

## 단 하나의 규칙: extractor stamp를 건드리지 않는다

빠른 티어 행에는 해당 행을 만든 코드의 버전 스탬프가 들어 있습니다 (`server/code/domain/extract/extractor_stamp.py`):

- 스탬프는 `STAGE_MODULES`에 속한 모든 모듈의 **코드 토큰**(`logic_tokens`: 주석, 빈 줄, docstring 제외. import는 패키지 경로가 아니라 바인딩되는 이름으로 계산)과 `STAMPED_GRAMMARS`에 있는 grammar(`tree-sitter`, `tree-sitter-cpp`, `tree-sitter-python`, `tree-sitter-bash`)의 설치된 버전에 대한 sha256입니다.
- 스탬프가 바뀌면 ingest 워커는 영향을 받는 언어 계열에 속한, 인덱싱된 모든 네임스페이스의 모든 파일에 대해 변경된 단계(`chunk`, `symbol`, `edge`, `guard`)를 속도를 조절한 배치로 다시 도출합니다 (`server/code/app/index/ingest_pass.py`의 `_maybe_rechunk`).
- `MODULE_LANGUAGES`에서 한 계열에만 매핑된 모듈(`lang_python.py`, `ts_*` 모듈)은 해당 계열의 파일만 다시 처리하게 합니다. 공유 모듈(`treesitter.py`, `chunker.py`)이나 핵심 `tree-sitter` grammar가 바뀌면 모든 파일이 다시 처리됩니다.
- 대규모 배포에서는 ingest에 몇 시간이 걸립니다.

스탬프가 바뀌지 않는 위치에 새 언어를 추가합니다:

| 파일                                                           | 스탬프에 포함? | 들어갈 내용                                                                                                 |
| -------------------------------------------------------------- | -------------- | ----------------------------------------------------------------------------------------------------------- |
| `server/code/domain/extract/lang_<lang>.py`                    | 아니요         | 레인: 심볼, include, 호출 지점을 담은 `parse_<lang>(path, source) -> ParsedFile`.                           |
| `server/code/domain/extract/lang_detect.py`                    | 아니요         | `LANG_BY_EXTENSION`(및 셸 규칙): 어떤 파일이 이 언어에 해당하는지 정합니다.                                 |
| `server/code/app/index/ingest_parse.py`                        | 아니요         | `_PARSERS`의 `_parse_<lang>` 래퍼. 레인에 import가 있으면 `ingest_resolve.py`에 include 해석을 추가합니다. |
| `server/code/domain/extract/lane_units.py`                     | 아니요         | grammar가 **없는** 레인을 위한 공유 헬퍼: `span_chunks`와 `bind_sites` 참조 바인더.                         |
| `server/code/domain/extract/treesitter.py`                     | **예**         | 스탬프된 핵심 언어 전용 `lang_for_path`. 여기에 확장자를 추가하면 모든 네임스페이스가 다시 청킹됩니다.      |
| `chunker.py`, `edges_treesitter.py`, 그 외 `STAGE_MODULES`     | **예**         | 공유 청킹 및 edge 추출. 모두에게 적용되는 추출 방식 자체가 바뀔 때만 변경합니다.                            |

`lang_for_path`에 이름이 없는 언어는 `lang_detect.py`에서 감지하므로, 언어를 추가해도 스탬프는 바뀌지 않습니다. 스탬프된 모듈의 주석과 docstring은 자유롭게 수정해도 됩니다. 이 모듈들은 포매터 대상에서 제외되어 있는데, 포매터가 코드 토큰인 문자열 따옴표를 다시 쓸 수 있기 때문입니다.

`server/code/domain/extract/` 아래의 모든 모듈은 `STAGE_MODULES`의 단계에 속하거나, `tests/unit/test_code_extractor_stamp.py`의 `NON_EXTRACTOR_MODULES`에 이유와 함께 명시되어 있어야 합니다. 새 레인 모듈은 `NON_EXTRACTOR_MODULES`에 넣어야 하며, 그렇지 않으면 해당 테스트가 실패합니다.

<a id="1-choose-the-lanes-shape"></a>

## 1. 레인의 형태 선택

| 형태              | 예시                                                                                                       | 제공해야 할 것                                                                                                                                                              |
| ----------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 문법 레인         | `lang_typescript.py`, `lang_rust.py`, `lang_go.py`, `lang_java.py`                                         | `pyproject.toml`의 tree-sitter 문법 의존성. 심볼과 호출 지점은 트리에서 추출하며, 청크와 엣지는 공유 청커와 엣지 워커가 만듭니다.                                           |
| 트리 없는 레인    | `lang_proto.py`, `lang_gotemplate.py`, `lang_hcl.py`, `lang_k8s.py`, `lang_jsonnet.py` (`LANE_UNIT_LANGS`) | 직접 작성한 스캐너. `ParsedFile.tree`는 `None`으로 유지되며, 청크는 `lane_units.span_chunks`에서, 엣지는 `reference_sites` + `SITE_RULES`를 `lane_units.bind_sites`로 바인딩해 만듭니다. |
| include 전용 레인 | `tla`, `flatbuffers`, `cmake`, `doc` (`_LANE_INCLUDE_LANGS`)                                               | 단순 레인 리졸버가 해석하는 심볼과 include.                                                                                                                                 |
| 임베디드 언어     | Go와 Java 안의 SQL (`go_sql_units`, `java_sql_units`), Dockerfile과 Makefile 안의 셸 영역                  | 호스트 파일의 파싱에 합쳐지는 유닛(`_join_island_symbols`). 보통 킬 스위치 설정이 함께 제공됩니다.                                                                          |
| 청크 전용         | `.css`, 텍스트 파일 (`lang_text.py`)                                                                       | 감지 외에는 필요 없음. 파일은 청크로 나뉘어 검색할 수 있지만 심볼은 없습니다.                                                                                               |

문법 레인의 문법은 `STAMPED_GRAMMARS`에 **포함되지 않으므로**, 문법을 업그레이드해도 스탬프가 바뀌지 않습니다. 레인이 자체 행을 다시 도출할 책임을 집니다.

<a id="2-write-the-parser"></a>

## 2. 파서 작성

`parse_<lang>(path: str, source: bytes) -> ParsedFile`은 언어 태그, 심볼(`treesitter.Symbol`: 한정 이름(qualified name), 종류, 범위, 시그니처), include, 그리고 엣지 단계에 필요한 정보를 반환합니다. 가장 가까운 기존 레인을 따르세요.

- **한정 이름(qualified name)**은 언어의 모듈 경로를 따릅니다(Rust: 파일 경로에서 얻은 crate 경로이며, `mod.rs` / `lib.rs`는 해당 디렉터리의 이름이 됩니다). 심볼 id는 한정 이름과 정규화된 시그니처를 해시한 값이므로, 이름이 불안정하면 재인덱싱할 때마다 행이 바뀝니다.
- **Include**는 네임스페이스가 가진 경로를 기준으로 해석합니다(`wanted_paths`, 이어서 레인의 `resolve_imports` / `resolve_uses`를 사용하며 `ingest_resolve.py`에서 호출합니다). 외부 패키지는 추측하지 않고 아무것도 가리키지 않도록 해석합니다.
- **호출 엣지**는 같은 파일에서 유일한 이름을 먼저 바인딩하고, 그다음 저장소 전체에서 유일한 이름을 더 낮은 신뢰도로 바인딩하며, 이름이 모호하면 바인딩하지 않습니다(트리가 없는 레인에서는 `lane_units.bind_sites`가 이를 처리합니다).
- **컨테이너 종류**(클래스, impl 블록)는 레인에 속합니다. 청커의 언어별 테이블에 등록하면 `chunker.py`가 바뀌고 스탬프가 달라집니다.

레인을 등록합니다.

```python
# server/code/app/index/ingest_parse.py
def _parse_rust(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_rust import parse_rust

    return parse_rust(path, source)


_PARSERS = {
    ...
    "rust": _parse_rust,
    ...
}
```

그리고 `LANG_BY_EXTENSION`에 확장자(`".rs": "rust"`)를 추가합니다. 래퍼는 레인을 지연 import하므로 선택적 grammar는 계속 선택적으로 남습니다. 새 tree-sitter grammar는 모든 파싱이 가장 먼저 거치는 `bounded_parse._LOADERS`에도 추가해야 합니다.

<a id="3-test-the-lane"></a>

## 3. 레인 테스트

- **레인 테스트**, `tests/unit/test_code_lang_<lang>.py`는 작은 인라인 소스를 파싱하고 이름, 종류, 범위, include, 엣지를 검증합니다. 문법(grammar)이 없으면 건너뜁니다: `pytest.importorskip("tree_sitter_rust")`. `test_code_lang_rust.py`와 `test_code_lang_typescript.py`가 모범 사례입니다.
- **레인 코퍼스.** `tests/fixtures/code_lanes/<lang>/` 아래에 픽스처를 추가합니다. 직접 만든 합성 코드이거나, 고유한 라이선스를 가진 오픈 소스 발췌본이며, 후자는 `tests/fixtures/code_lanes/NOTICE.md`에 나열합니다. `tests/unit/test_code_lanes_corpus.py`는 모든 픽스처에서 언어, 심볼, 패싯, include, 엣지를 도출하여 `tests/fixtures/code_lanes/expected.json`과 비교합니다. 다시 생성한 뒤 diff를 검토합니다:

  ```bash
  python -m tests.code_lanes.corpus --write
  git diff tests/fixtures/code_lanes/expected.json
  ```

  여기서 값이 바뀌면 모든 네임스페이스에 저장되는 내용이 달라집니다. 반드시 검토하세요.

- **비용 가드.** `ingest_parse._PARSERS`에 추가한 파서는 `tests/unit/lane_walks.py`에 `SAMPLES` 항목이 필요합니다. 그러면 `tests/unit/test_code_regex_cost.py`가 생성된 여러 형태의 입력으로 레인의 실행 시간을 측정하고, 선형을 넘는 탐색이 있으면 실패합니다.
- **스탬프 테스트.** `tests/unit/test_code_extractor_stamp.py`에서 새 모듈을
  `NON_EXTRACTOR_MODULES`에 추가합니다.
- **검색.** `code.search`나 `code.symbol`에 좋은 답을 내야 하는 레인이라면, 변경 전과 후에 코드 인텔리전스 게이트를 실행합니다. `make bench-code-intel-dry`는 질문 세트를 오프라인으로 검사하고, `make bench-code-intel`은 실제 임베더가 있는 서버를 대상으로 실행합니다. 가짜 임베더는 절대 사용하지 마세요 ([벤치마크 실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="4-bring-existing-namespaces-up-to-date"></a>

## 4. 기존 네임스페이스 최신 상태로 갱신하기

스탬프 밖의 레인은 이미 인덱싱된 것을 바꾸지 않습니다. 새 커밋은 해당 레인을 사용하지만, 기존 파일은 이전 행을 그대로 유지합니다(대개 chunk만 있는 상태). 대상을 지정한 backfill로 다시 도출하세요.

```bash
git -C <checkout of the repository> ls-files '*.rs' > rust-paths.txt
loom code backfill --org <org> --namespace <code-namespace> --paths-file rust-paths.txt
```

`--paths-file`은 브랜치 팁에서 해당 저장소 상대 경로만 다시 수집하며 워터마크는 건드리지 않습니다. 서버의 `.env`와 저장소 미러가 있는 곳, 예를 들어 workers 컨테이너에서 실행하세요([코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)).

> **경고:** 변경이 스탬프된 모듈(`chunker.py` 수정, `treesitter.py` 확장)을 건드려야 한다면 PR에 그 사실을 명시하세요. 해당 변경을 반영하는 모든 배포는 모든 네임스페이스에서 영향받는 계열의 모든 파일에 대해 이동된 단계를 다시 도출합니다. `chunker.py` 변경은 `chunk`만 이동시키고, `edges_treesitter.py` 변경은 `edge`, `chunk`, `guard`를 이동시키며, 파서 변경은 모든 단계를 이동시킵니다.

<a id="5-add-a-compiler-tier-optional"></a>

## 5. 컴파일러 티어 추가하기 (선택 사항)

빠른 티어의 엣지가 모호하고(오버로드, 동적 디스패치, re-export) SCIP 인덱서가 존재한다면 컴파일러 티어가 도움이 됩니다. `server/code/domain/index/compiler_tiers.py`의 `TIERS`에 티어 목록이 있습니다. 각 `TierSpec`은 인덱서(`scip-clang`, `scip-typescript`, `scip-go`, `scip-python`), 해당 인덱서의 `code_index_state` 워터마크 키(`scip_commit`, `scip_commit_ts`, `scip_commit_go`, `scip_commit_py`), 그리고 이를 진행시키는 워커를 지정합니다. 새 티어에는 다음이 필요합니다.

1. 커밋 범위에서 변경된 파일에 대해 인덱서를 실행하고 그 출력을 Loom의 JSON 델타로 변환하는 하니스: `scripts/scip_harness/` (`run_delta.sh`, `run_typescript_delta.sh`, `run_python_delta.sh`, `run_go_monorepo_delta.sh`, `scip_to_delta.py`).
2. 델타 워커(`server/code/workers/scip_ts_delta.py`, `scip_py_delta.py`, `scip_go_delta.py`가 모범 사례입니다): 워커의 `run_pass`는 자신의 `_run_repo`를 `server/code/app/index/compiler_tier.py`의 `run_tier`에 넘기며, 이 함수가 엔트리 루프, 실패 격리, 전체 실행 초기화를 담당합니다. 워커에는 `LOOM_CODE_SCIP_<LANG>_DELTA_ENABLED` / `_INTERVAL_SECONDS` / `_TIMEOUT_SECONDS` 설정이 함께 제공됩니다([워커 추가하기](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-worker.md)). 외부 생산자는 대신 `code.scip_ingest`로 업로드하며, `code_scip_apply` 워커가 완결된 배치를 적용합니다.
3. `code_scip_apply`의 `watermark_key_for`에 추가하는 워터마크 키와 `TierSpec` 행. 이를 통해 `code.index_status`와 대시보드가 해당 티어를 보고합니다.

컴파일러 티어 행에는 `tier='scip'`이 지정됩니다. 읽기 시에는 이 행을 빠른 티어와 한정된 심볼 이름(qualified name, 모듈 또는 컨테이너로 한정된 심볼 이름) 기준으로 병합하며, 컴파일러 티어를 우선합니다. 인덱서가 건너뛴 파일과 델타가 반영되기 전의 커밋은 계속 빠른 티어에서 읽습니다.

<a id="related"></a>

## 관련 문서

- [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- [코드 인덱스 아키텍처](../architecture/code-index-architecture.md)
- [코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)
- [Loom 확장하기](https://github.com/ClickHouse/loom/blob/main/docs/development/extending-loom.md)
