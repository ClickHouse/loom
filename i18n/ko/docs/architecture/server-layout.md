<!-- i18n: source=docs/architecture/server-layout.md blob=a488f9e3096ee9ff0f09303b9a382a9ce950bac7 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md), which is authoritative.

<a id="server-layout"></a>

# 서버 레이아웃

`server/`는 도메인별로 구성됩니다. 각 제품 플레인은 와이어 선언, 핸들러, 순수 로직, ClickHouse 접근, 백그라운드 루프를 담은 패키지 하나를 소유하며, 임포트가 향할 수 있는 방향은 테스트로 고정되어 있습니다. 이 페이지는 새 모듈을 어디에 둘지 정하고, 컨트랙트 테스트에서 발생하는 임포트 오류를 해석하는 데 쓰는 지도입니다.

<a id="packages"></a>

## 패키지

| 패키지                        | 담당 범위                                                                                                                                                                                                   | 임포트 가능 대상                                                                                            |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `server/main.py`              | 앱 구성: lifespan, 라우터, 워커, 트랜스포트. `server/boot.py`, `server/health.py`, `server/wiring.py`, `server/worker_main.py`, `server/__init__.py`와 함께 루트에 둘 수 있는 유일한 모듈                  | 모든 것                                                                                                     |
| `server/boot.py`              | lifespan의 부팅 및 종료 단계: 직렬화된 부팅 마이그레이션, 리더의 트래커 네임스페이스 행, 폐기 스냅샷과 키링, 슈퍼바이저 리스 루프, 쓰기 드레인                                                                 | 모든 것                                                                                                     |
| `server/health.py`            | `GET /v1/health`: 다중 티어 상태 응답(ClickHouse, 프로바이더, 부팅 스냅샷, 보정, 요청 제한기, 요청 시 워커와 관측 가능성)                                                                                    | 모든 것                                                                                                     |
| `server/wiring.py`            | `wire()`: 각 플랫폼 연결 지점에 도메인 구성 요소를 전달(인증 게이트의 네임스페이스 디렉터리, 상태 게이지의 사실 정보). `main.py`, CLI, 테스트 스위트에서 실행                                                 | 모든 것                                                                                                     |
| `server/worker_main.py`       | 워커 자식 프로세스의 컴포지션 루트(`python -m server.worker_main`): 서빙 lifespan 없이 슈퍼바이저와 그 워커가 사용하는 것                                                                                  | 모든 것                                                                                                     |
| `server/core/`                | 공통 인프라: ClickHouse 클라이언트(`ch`), `errors`, 예산, 날짜/시간, 해싱, 명명, 커서, 멱등성, 요청 제한, 벡터 저장 정책, `schema/`(마이그레이션과 DDL 도구)                                                | `core`, `settings`                                                                                          |
| `server/settings/`            | `catalog.py`에 선언된 모든 설정. 영역별로 한 섹션이며 `Settings`로 구성됨                                                                                                                                   | `core`, `settings`                                                                                          |
| `server/operations/`          | 레지스트리 코어: `Operation`과 `Role`(`base.py`), 평탄화된 레지스트리(`registry.py`: `ALL_OPERATIONS`, `OPERATIONS_BY_NAME`), `toolsets.py`, 읽기 프로젝션, 도달 범위 집계                                   | `core`, 플랫폼 패키지, 모든 도메인의 `api`                                                                  |
| `server/transport/`           | HTTP와 MCP: 수락 처리, 요청 바인딩, 목록, 프라이머                                                                                                                                                          | `core`, 플랫폼 패키지. 도메인의 `api`, `app`, `domain`(`store`나 `workers`는 불가). `cli`는 불가           |
| `server/cli/`                 | `loom` 명령, 외부 어댑터                                                                                                                                                                                    | 모든 것                                                                                                     |
| `server/workers/`             | 워커 레지스트리(`registry.py`), 슈퍼바이저, 패스 컨텍스트, 상태와 그 저장소, 첫 패스 스케줄링, 비용 이벤트. 워커 루프는 각 도메인에 있음                                                                    | `core`, 플랫폼 패키지. 도메인의 `workers`(레지스트리의 모듈 문자열)                                         |
| `server/observability/`       | 원장, 이벤트, 감사 행, 메트릭, 트레이싱 내보내기                                                                                                                                                            | `core`, 플랫폼 패키지                                                                                       |
| `server/auth/`                | 토큰 발급과 검증, 역할 및 네임스페이스 게이트, 요청별 신원                                                                                                                                                  | `core`, 플랫폼 패키지                                                                                       |
| `server/embedding/`, `llm/`   | 교체 가능한 임베더와 LLM 프로바이더                                                                                                                                                                         | `core`, 플랫폼 패키지                                                                                       |
| `server/clients/`, `storage/` | 외부 HTTP(공유 풀, GitHub), 보존 조항                                                                                                                                                                       | `core`, 플랫폼 패키지                                                                                       |
| `server/memory/`              | 메모리 플레인: `memory.*`, 검색, ask, 추출, 사실, 절차, 프로필, 작업 메모리, 컨텍스트 팩, 충돌, 블랙리스트, `loom_search`                                                                                   | [레이어](#layers) 참조                                                                                      |
| `server/code/`                | 코드 플레인: `code.*`, 인덱스(`app/index/`, `domain/index/`, `store/index/`), 파서(`domain/extract/`), 수집 및 컴파일러 티어 워커                                                                           | [레이어](#layers) 참조                                                                                      |
| `server/tracker/`             | 트래커 네임스페이스: `tracker.*`, 중복 검사, 라이브 소스 리더와 그 클라이언트, loom 자체의 트래커 테이블                                                                                                    | [레이어](#layers) 참조                                                                                      |
| `server/knowledge/`           | 지식 문서와 청크, 대화, 저장소 문서                                                                                                                                                                         | [레이어](#layers) 참조                                                                                      |
| `server/erasure/`             | 삭제 사가, 그 집계, 연합 백엔드, 복구 워커                                                                                                                                                                  | [레이어](#layers) 참조                                                                                      |
| `server/tasks/`               | 작업 인박스, 실행 원장, 그 깨우기 및 웹훅 루프                                                                                                                                                              | [레이어](#layers) 참조                                                                                      |
| `server/identity/`            | 네임스페이스, 권한 부여, 기록 정책, 읽기 범위, 제한된 접근, 대시보드 페이지와 호출자 API(`/v1/me/*`)                                                                                                        | [레이어](#layers) 참조                                                                                      |
| `server/insights/`            | 운영자용 화면: `ops.*`, `cost.*`, `agents.*`, `audit.list`, `ui.vitals`, `mentor.brief`, `/v1/admin/*` 라우트                                                                                               | [레이어](#layers) 참조                                                                                      |
| `server/assistant/`           | 어시스턴트: LibreChat 서비스 사용자와 에이전트 정의. 부팅 시 설정됨                                                                                                                                         | [레이어](#layers) 참조                                                                                      |

플랫폼 패키지는 `core`와 위에 나열된 그 외의 비도메인 패키지인 `settings`, `operations`,
`transport`, `cli`, `workers`, `observability`, `auth`, `embedding`, `llm`, `clients`,
`storage`입니다. `core`와 `settings`를 제외한 각 패키지는 다른 모든 플랫폼 패키지를
임포트할 수 있으며, 표에 명시된 경우를 제외하고는 어떤 패키지도 도메인 패키지를
임포트하지 않습니다.

<a id="layers"></a>

## 레이어

도메인 패키지에는 최대 다섯 개의 하위 패키지가 있습니다. 모듈은 주된 책임이 속한 곳에 두며, 작은 도메인은 레이어 수가 더 적습니다.

| 레이어     | 담는 내용                                                                                                                        | import 가능 대상                                                                                                                                                        |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api/`     | 와이어: Pydantic 입력 및 출력 모델(`models*.py`)과 도메인의 op 그룹에 대한 `Operation` 선언(`ops*.py`)                           | `core`, `operations`, `settings`; 모든 도메인의 `domain`과 `api`; `Operation`이 바인딩하는 핸들러를 위한 자신의 `app`                                                   |
| `app/`     | 핸들러와 오케스트레이션: op 또는 워커 패스가 I/O를 수행하며 순서대로 하는 일                                                     | 자신의 `api`, `app`, `domain`, `store`; 다른 도메인의 `app`, `domain` 및 와이어 모델(`api/models*.py`); `core`와 `transport`, `cli`를 제외한 모든 플랫폼 패키지         |
| `domain/`  | 순수 로직, 타입이 지정된 값과 정책: ClickHouse도 네트워크도 없음                                                                 | `core`(I/O 모듈인 `ch`, `schema`, `idempotency`, `ratelimit_shared`, `namespace_write_marks` 제외), `settings`, 모든 도메인의 `domain`                                   |
| `store/`   | ClickHouse 리포지토리와 행 빌더: 도메인이 자신의 테이블에 실행하는 모든 구문                                                     | `core`, `settings`, `observability`, `storage`, 자신의 `store`, 모든 도메인의 `domain`                                                                                  |
| `workers/` | 백그라운드 루프, 레지스트리 행마다 모듈 하나                                                                                     | `app`과 같되 `api` 대신 `workers`를 사용; 다른 도메인의 `app`, `domain` 및 와이어 모델                                                                                  |

`domain/` 모듈 하나는 프로세스를 시작합니다. `server/code/domain/extract/bounded_parse.py`는 `domain/`의 모든 tree-sitter 파서가 호출하며, 각 소스를 먼저 리소스 제한이 걸린 로컬 자식 프로세스에서 파싱합니다.

의도적으로 없는 두 가지 연결이 있습니다. `store/`는 `app/`을 호출하지 않습니다. 리포지토리는 값을 받아 행을 반환하므로, app을 가짜 store로 테스트할 수 있습니다. 그리고 어떤 도메인도 다른 도메인의 `store/`를 읽지 않습니다. 해당 도메인의 `app/`을 호출하거나 `domain/` 타입을 읽으며, 따라서 테이블의 소유자만이 그 테이블에 대한 구문을 작성하는 유일한 주체입니다. 다른 도메인의 핸들러를 호출하려면 `app/`과 `workers/`는 그 도메인의 와이어 모델(`api/models*.py`)을 import할 수 있지만, `api/ops*.py`는 절대 import하지 않습니다. `app/`은 와이어 모델만 import하고 `ops` 모듈은 import하지 않으므로, `Operation` 선언이 import 순환 없이 핸들러를 바인딩할 수 있습니다.

<a id="the-import-contract"></a>

## import 계약

`tests/unit/test_package_contracts.py`는 `ast`로 `server/` 아래의 모든 모듈을 파싱하고, 각 import를 양쪽 끝의 패키지와 레이어를 기준으로 분류합니다. 분류 대상은 최상위 import, `if`/`try` 아래에 중첩된 import, 함수 안의 import, `TYPE_CHECKING` 아래의 import입니다. 위의 규칙이 허용하지 않는 import가 있으면 테스트는 import하는 모듈과 import되는 모듈을 함께 보고하며 실패합니다.

현재 계약을 위반하는 import는 `tests/unit/package_contract_allowlist.txt`에 모듈 경로 기준 `importer -> imported` 형식으로 나열되어 있습니다. 이 목록은 줄어들기만 합니다. 허용된 쌍이 더 이상 나타나지 않는 경우에도 테스트가 실패하므로, import를 고치면 해당 줄을 삭제해야 합니다.

두 번째 검사가 함께 실행됩니다. `server.transport`를 import할 수 있는 것은 `server/transport/`, `server/cli/`, `server/main.py`뿐입니다(`tests/unit/test_transport_layering.py`). 어떤 도메인 모듈도 `server.cli`를 import하지 않습니다.

<a id="adding-a-module"></a>

## 모듈 추가하기

1. 모듈이 담당하는 테이블, op 그룹 또는 플레인이 속한 도메인을 고릅니다. 모든 플레인을 담당하는 모듈(ClickHouse 헬퍼, 해싱 규칙)은 `core/`에 둡니다. 두 도메인을 담당하는 모듈은 그 모듈이 읽는 행이 속한 도메인에 두고, 다른 도메인이 이를 호출합니다.
2. 계층은 누가 호출하는지가 아니라 모듈이 하는 일을 기준으로 고릅니다. I/O를 하거나 무언가를 await하는 `async def`는 `app/` 또는 `store/`에 두고, 인자만으로 결정되는 함수는 `domain/`에 둡니다.
3. 패키지 안에서 모듈이 담고 있는 내용을 기준으로 이름을 짓고, 경로에 있는 단어를 반복하지 않습니다. `server/erasure/app/erasure_saga.py`가 아니라 `server/erasure/app/saga.py`로 짓습니다.
4. 패키지의 `__init__.py`에는 그 패키지가 무엇을 담당하는지 설명하는 한 줄짜리 docstring을 둡니다. 이름을 다시 내보내는(re-export) 경우는 해당 패키지의 공개 인터페이스일 때뿐입니다(`server/memory/store/`, `server/code/store/index/`).
5. 백그라운드 루프는 `<domain>/workers/`에 있는 모듈 하나와, 전체 모듈 경로를 적은 `server/workers/registry.py`의 행 하나로 구성됩니다.
6. mock 또는 monkeypatch 대상, 레지스트리 행, 커버리지 하한선은 모듈을 경로로 지정합니다. 재내보내기(re-export)를 거치지 말고 모듈의 실제 경로로 작성합니다.
7. 서버가 함께 배포하는 파일(마이그레이션, `schema.sql`, 프런트엔드 빌드)은 `server.core.paths`를 통해 찾고, `__file__`의 부모 디렉터리 수를 세는 방식은 쓰지 않습니다(`tests/unit/test_repo_paths.py`).

<a id="extractor-modules"></a>

## Extractor 모듈

`extractor_stamp.STAGE_MODULES`에 나열된 파일은 코드 인덱스의 버전 스탬프입니다.
스탬프는 각 파일의 이름과 코드 토큰을 해시하며, import는 가져오는 패키지가 아니라 바인딩하는 이름으로 계산합니다. 스탬프가 바뀌면 모든 코드 네임스페이스의 인덱스를 다시 도출합니다. 이 파일들은 `server/code/domain/extract/`에 `extractor_stamp.py`와 함께 있으며, 이 스탬프 모듈은 자신의 디렉터리에서 이 파일들을 읽고, 파일 이름은 그대로 유지합니다. 언어 레인 모듈은 그 옆에 있습니다. 모듈을 다른 패키지로 옮겨도 스탬프는 그대로이지만, 이름을 바꾸거나, 형식을 다시 지정하거나(`ruff format`은 이 파일들을 제외합니다), 코드를 변경하면 스탬프가 바뀝니다.

<a id="related"></a>

## 관련 문서

- [시스템 개요](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md) — 컨트롤 플레인의 구성 요소와 요청 흐름.
- [코드 인덱스 아키텍처](code-index-architecture.md) — extractor 스탬프와 재인덱싱.
- [요청 경로](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md#request-path) — 요청이 전송 계층, 레지스트리, 핸들러를 거치는 경로.
