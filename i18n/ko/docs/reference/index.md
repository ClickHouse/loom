<!-- i18n: source=docs/reference/index.md blob=fb131cfc15bed6b11b4ba0dd63862e173de7bcf3 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md), which is authoritative.

<a id="reference"></a>

# 레퍼런스

레퍼런스 페이지는 무엇이 존재하며 각 옵션이 무엇을 의미하는지 설명합니다. 설계의 배경과 이유는
[개념](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)과 [아키텍처](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md)에서 다룹니다.
절차는 [가이드](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)와
[운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) 섹션에 있습니다.

<a id="generated-from-the-code"></a>

## 코드에서 생성됨

이 페이지들, 또는 페이지 안의 표는 해당 내용을 정의하는 소스에서 렌더링됩니다. 각 생성기에는 `make lint`가 실행하는 `--check` 모드가 있어, 코드와 어긋난 페이지는 CI에서 실패합니다. 소스를 수정한 뒤 다시 생성하고, 생성된 표를 직접 수정하지 마세요.

| 페이지                              | 나열하는 내용                                                                                                 | 소스                                                                                 | 생성기                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------- |
| [구성](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) | 모든 `LOOM_*` 설정과 기본값, 의미를 영역별로 나열합니다.                                                      | `server/settings/`의 필드 위에 있는 주석                                             | `python scripts/codegen/settings_reference.py` |
| [오퍼레이션](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)       | 모든 오퍼레이션을 그룹별로 나열하고 역할, 종류, MCP `all` 목록 포함 여부, 감사 플래그를 보여 줍니다. 레지스트리가 동작하는 방식도 설명합니다. | `server.operations.registry.ALL_OPERATIONS`                                          | `python scripts/codegen/docs_reference.py`     |
| [워커](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)             | 모든 백그라운드 워커와 해당 스위치, 기본값, 실행 주기를 나열합니다. 슈퍼바이저가 워커를 실행하는 방식도 설명합니다. | `server/workers/registry.py` (`_WORKERS`)                                            | `python scripts/codegen/docs_reference.py`     |
| [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)                     | 모든 `loom` 명령과 하위 명령, 그리고 각 플래그를 나열합니다. CLI가 실행되는 위치와 자주 쓰는 명령도 설명합니다. | `loom` 파서 트리 (`tests/unit/snapshots/cli_tree.json`)                              | `python scripts/codegen/docs_reference.py`     |
| [오류](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)               | 서버가 응답하는 모든 오류 코드를 나열합니다. 응답 envelope, 재시도 규칙, 자주 나오는 코드의 해결 방법도 설명합니다. | `server/core/errors.py` 및 `server/` 아래의 모든 `"code"` 리터럴                    | `python scripts/codegen/docs_reference.py`     |
| [상태](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)               | 각 오퍼레이션 그룹에서 안정(stable) 상태인 오퍼레이션의 수, 각 실험적(experimental) 오퍼레이션, 기본값이 false로 로드되는 모든 불리언 설정을 보여 줍니다. | 레지스트리의 op `maturity`와 `server/settings/catalog.py`의 불리언 필드              | `python scripts/codegen/docs_reference.py`     |
| [스키마](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)               | 모든 테이블과 materialized view를 엔진, 키, TTL, 인덱스와 함께 나열합니다. 컬럼 규칙도 설명합니다.            | `schemas/schema.sql`                                                                 | `python scripts/codegen/docs_reference.py`     |

`docs_reference.py` 페이지에서는 `<!-- BEGIN GENERATED: … -->`와 `<!-- END GENERATED: … -->` 마커 사이의 영역만 생성되며, 그 주변의 본문은 직접 작성한 것입니다. SDK 패키지 안의 op 표(`sdk/python/loom/ops.py`, `sdk/typescript/src/generated/ops.ts`)도 `scripts/codegen/sdk_py_gen.py`와 `sdk_ts_gen.py`로 생성됩니다. 자세한 내용은 [코드 생성](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md)을 참고하세요.

<a id="written-by-hand"></a>

## 직접 작성한 문서

| 페이지                                        | 다루는 내용                                                                                                                                                        |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)                     | 모든 라우트(op, 디스커버리, 상태 확인, 스트림, 로그인, OAuth, 대시보드, 운영자), 게이트 순서, 헤더, 알 수 없는 필드, 멱등성, 페이지네이션, 속도 제한. |
| [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)                               | `/mcp` 엔드포인트, 인증과 OAuth, 세션과 ID, 도구 목록과 툴셋, 결과, 오류, 액션 스키마 내보내기.                                                                   |
| [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)                 | 설치, 생성, op 호출, 타입이 지정된 형태, 세션과 도구, 프레임워크 어댑터, 오류, 재시도.                                                                             |
| [TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md)         | 설치, 클라이언트와 옵션, 타입이 지정된 op, 세션과 도구, 페이지네이션, Strands 어댑터, 오류, 재시도.                                                               |
| [환경 변수](https://github.com/ClickHouse/loom/blob/main/docs/reference/environment.md)                 | 설정 모델 밖에서 읽는 변수: 공급자 키, 이미지, compose 파일, CLI, SDK, 운영자 스크립트.                                                                           |
| [SQL 인터페이스](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)            | 테넌트 데이터에 대한 읽기 전용 `sql_v1_*` 뷰: 컬럼, 호환성 규칙, 노출하지 않는 항목, 최신성, `loom sql-access`.                                                    |
| [익스트랙터 계약](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md)     | 익스트랙터 서비스가 제공하는 HTTP 계약의 버전 1: 기능, 추출, 오류 코드, 아티팩트의 필드와 규칙.                                                                   |

직접 작성한 페이지는 소스 트리에 실제로 있는 것만 언급하며, 설명하는 소스로 연결되는 링크를 제공합니다.

<a id="which-page-answers"></a>

## 어느 페이지가 답하나요…

- "이 op의 이름은 무엇이고, 누가 호출할 수 있나요?" → [작업](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md).
- "이 설정은 무엇을 하나요?" → [설정](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md).
- "이 오류 코드는 무슨 뜻이고, 재시도해야 하나요?" → [오류](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).
- "이 op는 안정 버전인가요, 실험 단계인가요? 그리고 이 설정은 기본값으로 켜져 있나요?" → [상태](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
- "어떤 헤더와 어떤 라우트를 쓰고, 페이지 이동은 어떻게 하나요?" → [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md).
- "MCP 클라이언트에 도구가 표시되지 않는 이유는 무엇인가요?" → [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).
- "이 워커는 실행 중인가요, 그리고 어떻게 켜나요?" → [워커](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md).
- "다음 배포에서 내 클라이언트가 깨지나요?" → [알 수 없는 필드와 제거된 필드](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields) 및 [배포가 제공하는 항목 확인하기](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves).
- "이 테이블에는 무엇이 들어 있나요?" → [스키마](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md).
- "`loom <command>`는 어떤 인자를 받나요?" → [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md).
