<!-- i18n: source=docs/features/integrations/grep-shim.md blob=1ce5e2990b99fc9c2e3b50a80f6b06f33fa415f0 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md), which is authoritative.

<a id="the-grep-shim"></a>

# grep shim

grep shim은 `grep`/`rg`를 감싸는 래퍼로, 코드 인덱스에서 찾은 각 결과의 상위 함수를 일치하는 줄 끝에 덧붙입니다. Node 18 이상, 뷰어 토큰, 그리고 [코드 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)에 등록된 리포지토리가 필요합니다. 셸 grep만 사용하는 에이전트나 훅이 동작하지 않는 환경(`claude --bare`는 [Claude Code 플러그인의](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) 코드 shunt를 건너뜁니다)에서 쓰도록 만들어졌습니다. 에이전트, 프롬프트, 하네스는 그대로 유지됩니다.

```text
$ grep -rn "getSettingsRef().max_threads" src/Processors
src/Processors/QueryPlan/ReadFromMergeTree.cpp:1466:    ... max_threads ...   ⟵ DB::ReadFromMergeTree::selectRangesToRead
src/Processors/Executors/PipelineExecutor.cpp:298:      ... max_threads ...   ⟵ DB::PipelineExecutor::initializeExecution
loom: 2/2 hits scoped to their enclosing function via code.enclosing, index at c7a9fe7c9f, 180 ms
```

<a id="install"></a>

## 설치

```bash
export PATH=/path/to/loom/integrations/grep-shim/bin:$PATH
export LOOM_BASE_URL=https://loom.example.com   # or ~/.loom/config.json {url, token}
export LOOM_API_KEY=<viewer token>              # or ~/.loom/token
export LOOM_CODE_NAMESPACE=code-myrepo
export LOOM_AGENT=pr-analyst                    # optional: names the caller in the ledger
```

`which grep`이 shim을 가리켜야 합니다. 에이전트는 네임스페이스가 인덱싱한 체크아웃 안에서 실행하세요. 경로는 `git rev-parse --show-toplevel` 기준 상대 경로로 변환됩니다. 브랜치가 일치하지 않으면 잘못된 결과가 아니라 해결되지 않은 결과로 나타납니다.

shim은 `PATH`에서 자기 자신이 아닌 첫 번째 `grep` 또는 `rg`를 실행합니다(`LOOM_GREP_REAL`, `LOOM_RG_REAL`로 경로를 강제할 수 있습니다).

<a id="guarantees"></a>

## 보장 사항

- **실제 도구가 출력하는 모든 바이트가 순서대로 그대로 출력됩니다.** 달라지는 점은 인덱스가 상위 함수를 아는 일치 줄에 선택적 접미사 `   ⟵ <qualified name>`이 붙는 것과 stderr에 한 줄 요약이 출력되는 것뿐입니다.
- **종료 코드가 유지됩니다** (grep: 0은 결과 있음, 1은 결과 없음, 2는 오류).
- **두 가지 형태에 주석이 붙습니다:** `path:line:text`(여러 피연산자, 디렉터리에 `-r`, `-H`)와 정확히 하나의 파일 피연산자에서 나오는 `line:text`(`grep -n pattern file.cpp`, 에이전트가 파일을 알게 된 뒤 자리 잡는 형태이며, 경로가 피연산자이므로 `-h`를 써도 됩니다). 그 외에는 모두 그대로 통과합니다: `-n` 없음, `-l`, `-L`, `-c`, `-o`, `-z`, `-Z`, `-b`, `-q`, `--color=always`(rg: `-N`, `--json`, `--files`, `-0`), stdin, `-h`와 함께 쓴 여러 파일.
- **엄격한 지연 시간 한도.** `LOOM_GREP_SHIM_TIMEOUT_MS`(기본값 2500). 오류, 타임아웃, 토큰 누락, 또는 8 MB를 넘는 출력(`LOOM_GREP_SHIM_MAX_BYTES`)이 발생하면 원본 출력을 그대로 출력합니다.
- **서버 부하 제한.** distinct 결과 200개당 `code.enclosing` 호출은 한 번, 병렬로는 최대 5개입니다. 1,000개 결과에 주석이 붙고 나머지는 주석 없이 출력됩니다.

<a id="the-dependency-graph-block"></a>

## 의존성 그래프 블록

`LOOM_GREP_SHIM_GRAPH=on`(기본값 off)이면 실제 출력 뒤에 `[Related from loom dependency graph]` 블록이 붙습니다. 이어서 상위 함수마다 한 줄씩, 모듈과 최대 `LOOM_GREP_SHIM_GRAPH_K`(5)개의 호출자, 피호출자, 테스트가 나오며, 그래프가 불완전한 곳에는 `(lower bound: …)` 메모가 붙습니다. 헤더 줄부터 잘라내면 실제 바이트를 그대로 되찾을 수 있습니다. 이 블록은 같은 `code.enclosing` 호출, 타임아웃, fail-open 규칙을 사용하며, 함수가 `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS`(20)개를 넘거나 출력이 스트리밍된 경우(8 MB 초과) 생략됩니다.

<a id="switches"></a>

## 스위치

| 변수                                    | 기본값                                 | 효과                                                                         |
| --------------------------------------- | -------------------------------------- | ---------------------------------------------------------------------------- |
| `LOOM_GREP_SHIM`                        | on                                     | `off`이면 shim이 단순 통과 래퍼가 됩니다                                      |
| `LOOM_GREP_SHIM_QUIET`                  | unset                                  | 설정하면 stderr 요약 줄을 출력하지 않습니다                                   |
| `LOOM_GREP_SHIM_TIMEOUT_MS`             | 2500                                   | 지연 시간 한도                                                               |
| `LOOM_GREP_SHIM_MAX_BYTES`              | 8 MB                                   | 이를 넘는 출력은 원본 그대로 통과합니다                                       |
| `LOOM_GREP_SHIM_GRAPH`                  | off                                    | `on`이면 의존성 그래프 블록을 덧붙입니다                                      |
| `LOOM_GREP_SHIM_GRAPH_K`                | 5                                      | 함수당 이웃 수(1–20)                                                         |
| `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS`      | 20                                     | 이 수를 넘으면 블록을 생략하는 함수 수                                        |
| `LOOM_CODE_NAMESPACE`, `LOOM_AGENT`     |                                        | 코드 네임스페이스, 호출자의 에이전트 이름(기본값 `grep-shim`)                 |
| `LOOM_CONFIG_FILE`, `LOOM_API_KEY_FILE` | `~/.loom/config.json`, `~/.loom/token` | 환경 변수로 지정하지 않았을 때 URL, 네임스페이스, 토큰을 읽어 오는 위치        |

<a id="server-side-codeenclosing"></a>

## 서버 측: `code.enclosing`

shim은 하나의 연산 위에 얹은 얇은 클라이언트입니다. `code.enclosing`(`POST /v1/code.enclosing`, 뷰어)은 `locations: ["src/F.cpp:12", …]`(최대 200개)를 받아 위치마다 입력 순서대로 결과를 하나씩 반환하며, 해당 줄이 파일 스코프이거나 경로가 인덱싱되지 않았다면 `symbol == ""`입니다. 해결된 모든 행에는 `symbol_id`가 들어 있습니다.

- `include_body: true`(`token_budget`와 함께 사용, 기본값 4000)는 각 상위 함수의 전체 본문을 행에 넣습니다. 본문은 distinct 함수마다 한 번만 가져오고, 예산이 소진될 때까지 처음 등장한 순서대로 채우며, 나머지 행에는 붙여 넣어 쓸 수 있는 `next`(`code.symbol(symbol_id=…)`)가 담깁니다. shim은 이 옵션을 끈 상태로 둡니다.
- `neighbours: true`(`neighbour_k`와 함께 사용, 기본값 5)는 그래프 블록의 데이터를 반환합니다. distinct 상위 함수마다 항목 하나(최대 20개, 더 많이 해결되면 `anchors_capped`)이며 `callers`, `callees`(컴파일러 티어 우선, 리포지토리 밖의 심볼과 신뢰도 0.8 미만의 파서 추측은 제외), `tests`, `module`, 그리고 `epistemic` / `causes` envelope가 들어 있습니다.

`code.symbol`도 단일 위치에 대해 `name: "src/F.cpp:12"`를 받을 수 있으며, Python SDK는 `agent.code.enclosing([...], include_body=True)`를 제공합니다. [코드 인덱스 연산](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)을 참고하세요.

<a id="limits"></a>

## 제한 사항

- **병합된 코드만 해당됩니다.** 인덱스는 인덱싱한 브랜치만 알고 있으므로, 커밋되지 않은 수정 내용의 결과는 파일의 인덱싱된 버전을 기준으로 해결되거나 아예 해결되지 않습니다.
- **최선을 다하는 방식입니다.** 예상치 못한 상황에서는 원본 출력을 그대로 출력하며, 주석이 없는 줄이 grep이 실패했다는 뜻은 아닙니다.
- **검색은 그대로입니다.** 에이전트는 여전히 grep을 사용합니다. MCP 도구를 호출할 수 있는 에이전트라면 `code.search`, `code.callers`, `code.impact`를 직접 사용하는 것이 좋습니다.

<a id="related"></a>

## 관련 문서

- [코드 인덱스 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md).
- [Claude Code 플러그인](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md): 훅을 실행하는 에이전트를 위한 훅 기반 코드 shunt.
- [통합 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md).
