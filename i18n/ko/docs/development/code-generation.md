<!-- i18n: source=docs/development/code-generation.md blob=3f6e27200eccce850139d9114282249d0644e35f model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md), which is authoritative.

<a id="code-generation"></a>

# 코드 생성

저장소의 여러 파일은 코드에서 파생됩니다. SDK의 작업(operation) 테이블과 wire 타입, 레퍼런스 페이지, Codex 플러그인의 사본, op reach 매니페스트, 스키마 덤프가 그 예입니다. 각 파일은 커밋되어 있어 실행하지 않아도 내용을 볼 수 있으며, 아래에 표시된 생성기에는 `--check` 모드가 있어 커밋된 파일이 생성기가 쓸 내용과 다르면 종료 코드 1로 끝납니다. `make lint`는 이 검사를 각각 실행하고, CI는 `lint` 및 `sdk-typescript` 잡과 단위 테스트 스위트에서 이를 실행하므로, 최신이 아닌 파일은 실패 메시지에 실행할 명령이 명시된 채로 실패합니다.

**생성된 파일을 커밋하는 이유.** 필요할 때만 빌드되는 생성 파일은 리뷰에서 보이지 않습니다. 작업의 입력 모델을 바꾸면 SDK 타입이 바뀌지만, 아무도 읽지 않는 diff가 됩니다. 생성 파일을 커밋하면 변경의 영향이 변경 바로 옆에 나타나고, `--check` 게이트가 둘이 어긋나는 것을 막아 줍니다. 대신 소스를 바꾸는 변경에서 재생성 단계를 거쳐야 하는 비용이 듭니다.

<a id="the-generators"></a>

## 생성기

모두 [`scripts/codegen/`](https://github.com/ClickHouse/loom/blob/main/scripts/codegen)에 있으며 `scripts/codegen/common.py`를 공유합니다(저장소 루트, SDK 표가 op마다 담는 사실, ruff 포매팅, 원자적 쓰기, 쓰기 또는 검사 프로토콜).

| 스크립트                      | 쓰는 대상                                                                                                                                                                                                              | 입력                                                                                                                                                                                                  | `make lint`의 `--check`   |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `sdk_py_gen.py`             | `sdk/python/loom/ops.py`: `OpName`, `OPS` 표(멱등성, 역할, 성숙도, 지연 예산), 그리고 입력 모델과 출력 모델마다 하나씩의 `TypedDict`                                                                                    | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | 예                        |
| `sdk_py_client_gen.py`      | `sdk/python/loom/client.py`: `OpsClient`, op마다 메서드 하나(`agent.client.tracker.source_add`), `ops.py`의 이름으로 타입 지정                                                                                          | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | 예                        |
| `sdk_py_admin_gen.py`       | `sdk/python/loom/admin_routes.py`: `AdminRoutes`, `/v1/admin/*` 라우트마다 메서드 하나(`agent.deployment.routes.settings_set`)                                                                                         | `/v1/admin/*` 라우터: 각 라우트의 메서드, 경로, 게이트, docstring                                                                                                                                     | 예                        |
| `sdk_ts_gen.py`             | `sdk/typescript/src/generated/ops.ts`: 모델마다 인터페이스 하나, `OPS` 표, `OpMap` 타입                                                                                                                                | `server.operations.registry.ALL_OPERATIONS` (모델의 JSON Schema)                                                                                                                                      | 예                        |
| `agent_plugin_sync.py`      | Codex 플러그인의 훅 스크립트 사본(`hooks/*.mjs`), 공유 라이브러리(`lib/*.mjs`), 스킬, 목록용 아이콘(`assets/`). 실행 비트를 포함해 바이트 단위로 동일                                                                   | `integrations/claude-code-plugin/`                                                                                                                                                                    | 예                        |
| `settings_reference.py`     | 생성된 [설정 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)                                                                                                                                                                  | `server/settings/catalog.py`: 각 필드의 환경 변수, 타입, 기본값, 그리고 그 위의 주석                                                                                                                  | 예                        |
| `settings_schema.py`        | `schemas/loom-settings.schema.json`, YAML 설정 파일의 JSON Schema                                                                                                                                                      | 설정 필드의 타입, 범위, 기본값, 주석. 비밀 값은 제외                                                                                                                                                  | 예                        |
| `settings_example.py`       | `loom.example.yaml`, 설정 파일 템플릿: 비밀 값을 제외한 모든 설정을 영역별로, 설명 아래에 기본값 그대로 주석 처리하여 수록                                                                                             | 설정 카탈로그의 필드, 기본값, 주석, 라이브 표시                                                                                                                                                       | 예                        |
| `docs_reference.py`         | `docs/reference/`의 생성 영역: `operations`, `workers`, `cli`, `errors`, `schema`, `status_ops`, `status_switches`, `admin_routes`([운영자 라우트 표](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#operator-routes)) | 레지스트리, `server/workers/registry.py`(`_WORKERS`), `tests/unit/snapshots/cli_tree.json`, `server/core/errors.py`, `schemas/schema.sql`, `server/settings/catalog.py`, `/v1/admin/*` 라우터 | 예                        |
| `op_reach_census.py`        | `server/operations/reach_manifest.py`(`--write-manifest`). 그 외에는 조사 표를 출력                                                                                                                                    | 레지스트리와 MCP 툴셋                                                                                                                                                                                 | 예                        |
| `wire_changelog.py`         | 쓰지 않음. 두 리비전 사이에 클라이언트에 바뀐 내용을 출력                                                                                                                                                              | 각 리비전에서 빌드한 capability 카탈로그                                                                                                                                                              | 아니요                    |
| `gen_py_library_methods.py` | `server/code/domain/extract/py_library_methods.py`                                                                                                                                                                     | 실행 중인 인터프리터의 표준 라이브러리                                                                                                                                                                | 아니요(인터프리터에 의존) |
| `push_langfuse_prompts.py`  | `server/observability/langfuse_prompts.lock.json`(`--write`)                                                                                                                                                           | 서버의 프롬프트 상수. 이를 Langfuse에 푸시                                                                                                                                                            | 아니요                    |

설정 레퍼런스는 주석에서 렌더링되므로, 설정 위의 주석이 곧 그 설정의 문서입니다. 페이지가 아니라 주석을 수정하세요. `docs/reference/` 페이지는 `<!-- BEGIN GENERATED: <name> -->` … `<!-- END GENERATED: <name> -->` 영역을 둘러싼 직접 작성한 설명문으로 이루어져 있습니다. 마커 한 쌍 사이의 텍스트만 다시 쓰이므로, 페이지가 담고 있는 표를 설명할 수 있습니다.

<a id="sdk_py_gen-and-sdk_ts_gen"></a>

### sdk_py_gen과 sdk_ts_gen

Python SDK는 dict를 받아 dict를 돌려주는 방식을 유지합니다. 생성된 파일은 이름을 추가하므로, 편집기가 `agent.call("memory.search", …)`를 자동 완성하고 타입 검사기가 직접 관리하는 모델 계층 없이도 필드 이름을 알 수 있습니다. dict는 `total=False`이며, 서버가 요구하는 필드에는 `Required[...]`가 붙습니다. TypeScript 생성기는 각 모델의 JSON Schema를 인터페이스로 변환하므로(enum과 const는 리터럴 유니온으로, `anyOf`는 유니온으로, 공유 `$defs`는 한 번만 끌어올림) `client.call("memory.search", input)`이 처음부터 끝까지 타입 지정됩니다. op의 등록이나 모델을 바꿀 때마다 실행하세요.

```bash
python scripts/codegen/sdk_py_gen.py
python scripts/codegen/sdk_py_client_gen.py
python scripts/codegen/sdk_ts_gen.py
```

두 SDK 모두 모든 op를 같은 위치의 메서드로 노출합니다. 그룹은 op 이름의 첫 번째 세그먼트이고, 메서드는 나머지를 `.`로 이은 것이며, Python에서는 snake_case(`agent.client.tracker.source_add`), TypeScript에서는 camelCase(`loom.tracker.sourceAdd`)입니다. 두 op 표가 다르거나 어떤 op의 두 메서드 이름이 다르면 `tests/unit/test_sdk_parity.py`가 실패합니다.

<a id="agent_plugin_sync"></a>

### 에이전트 플러그인 동기화

두 코딩 에이전트 플러그인은 매니페스트와 훅 연결(`hooks/hooks.json`)만 다릅니다. Codex 플러그인은 `../` 참조 대신 사본을 담고 있어, 각 플러그인이 마켓플레이스가 단독으로 체크아웃할 수 있는 독립된 디렉터리가 됩니다. Claude Code 플러그인에 훅 스크립트나 스킬을 추가한 뒤에는 동기화를 실행하고 사본을 커밋하세요. Codex가 새 훅을 실행해야 한다면 Codex의 `hooks.json`에 직접 연결하세요.

<a id="op_reach_census"></a>

### op 도달 범위 조사

모든 operation은 성숙도를 선언해야 하며, 원시 HTTP `POST` 외의 도달 경로를 갖거나 `experimental`로 선언되어야 합니다. 도달 종류(`server/operations/reach.py`, `REACH_KINDS`)는 MCP 목록(`mcp_core`, `mcp_atomic`, `mcp_all`, `server/operations/toolsets.py`에서 실시간으로 도출), SDK 메서드(`sdk_method`), 함께 제공되는 예제 또는 운영자 스크립트(`sdk_example`), 플러그인 훅 또는 통합(`plugin_hook`), 대시보드 호출 지점(`dashboard`)입니다. 마지막 네 가지는 트리를 스캔해서 얻는데, 배포된 서버에는 트리가 없으므로 `reach_manifest.STATIC_REACH`로 커밋해 둡니다. `--check`는 이 규칙, 대시보드 호출 지점이 없는 모든 op가 `server/operations/dashboard.py`에 (없는 이유와 함께) 있어야 한다는 것, 그리고 커밋된 매니페스트가 트리와 일치한다는 것을 강제합니다. `--write-manifest`는 매니페스트를 갱신합니다. `--call-counts --days 30`은 `loom.agent_events`에서 가져온 각 op의 호출 횟수를 추가하며(ClickHouse에 연결하는 유일한 모드), `--owners`는 각 experimental op의 선언을 도입한 커밋을 알려 줍니다.

<a id="wire-changelog"></a>

### 와이어 변경 내역

```bash
python scripts/codegen/wire_changelog.py <rev_a> <rev_b>           # markdown
python scripts/codegen/wire_changelog.py --since-hours 24 [rev_b]   # rev_a = rev_b's ancestor N hours back
python scripts/codegen/wire_changelog.py <rev_a> <rev_b> --json
python scripts/codegen/wire_changelog.py --snapshot <rev> > snap.json
python scripts/codegen/wire_changelog.py --diff a.json b.json
```

각 리비전을 `git archive`로 풀어내고, 자식 인터프리터에서 그 레지스트리를 임포트한 뒤, 추가되거나 제거된 operation과 요청·응답 필드, 그리고 역할, 예산, MCP 티어의 변경을 출력합니다. 출력은 릴리스 노트([릴리스 절차](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes))에 들어가며, "클라이언트에 바뀐 내용" 섹션은 작성하는 것이 아니라 생성됩니다.

<a id="gen_py_library_methods"></a>

### Python 라이브러리 메서드 생성

Python 추출기가 사용하는 표준 라이브러리 메서드 이름의 집합은 트리에 고정되어 있어, 인터프리터를 업그레이드해도 호출 간선이 움직이지 않습니다. 이를 다시 생성하면 추출기 모듈이 바뀌고, 그러면 코드 인덱스의 버전 스탬프가 바뀌어 인덱싱된 모든 네임스페이스가 다시 청크로 나뉩니다. diff를 검토하고, 의도했을 때만 다시 생성하세요.

<a id="push_langfuse_prompts"></a>

### Langfuse 프롬프트 푸시

서버는 커밋된 lockfile에서 프롬프트의 콘텐츠 해시를 조회하여 LLM 생성을 Langfuse 프롬프트 버전에 연결하며, 런타임 호출은 없습니다. `--write` 없이 실행하면 스크립트는 오프라인으로 lockfile을 기준으로 계획만 세우고, `--write`를 주면 변경된 프롬프트를 푸시하고(`LOOM_LANGFUSE_URL`과 `LOOM_LANGFUSE_ADMIN_TOKEN`이 필요) lockfile을 다시 씁니다. 등록된 프롬프트가 lockfile에 없으면 단위 테스트가 실패합니다.

<a id="the-schema-dump"></a>

## 스키마 덤프

`schemas/schema.sql`은 모든 마이그레이션이 적용된 ClickHouse에서 `scripts/ops/dump_schema.py`로 생성됩니다.

```bash
make schema-dump    # python -m scripts.ops.dump_schema
make schema-check   # --check: exit 1 with the head of the diff; writes nothing
```

이 파일은 모든 마이그레이션 변경을 따라가며 CI가 고정한 ClickHouse 버전을 기준으로 생성해야 하므로, 자세한 내용은 [마이그레이션](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md#schema-dump-and-check) 문서에서 다룹니다.

<a id="snapshots-that-are-tests-not-generators"></a>

## 생성기가 아니라 테스트인 스냅샷

CLI 표면(`tests/unit/snapshots/cli_tree.json`, `cli_help.json`)과 worker 레지스트리(`tests/unit/snapshots/worker_registry.json`)는 단위 테스트로 고정되며, 테스트를 실행할 때 환경 변수로 다시 생성합니다
([테스트](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md#ci-gates)). CLI 레퍼런스는 `cli_tree.json`에서 렌더링되므로, CLI 스냅샷을 다시 생성한 뒤에는 `docs_reference.py`도 실행하세요.

<a id="before-you-push"></a>

## 푸시하기 전에

```bash
make lint            # every --check above, plus ruff, the gates and Biome's dashboard accessibility rules
```

실패하면 실행해야 할 스크립트의 이름이 표시됩니다. 해당 스크립트를 실행하고 diff를 검토한 다음, 재생성된 파일을 원본 소스와 같은 변경에 포함해 커밋하세요.

<a id="related"></a>

## 관련 문서

- [작업 추가하기](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-an-operation.md): op, 핸들러, SDK
  테이블
- [문서 스타일](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md#generated-pages): 어떤 페이지가
  생성되는지
- [테스트](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)
