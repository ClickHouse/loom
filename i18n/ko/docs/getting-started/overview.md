<!-- i18n: source=docs/getting-started/overview.md blob=6fbe75840c341cccfe02d72afa8d5bbb4dfb7809 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md), which is authoritative.

<a id="overview"></a>

# 개요

Loom은 AI 에이전트를 위한 메모리, 문서, 대화, 엔티티 그래프, 코드 인덱스를 ClickHouse의 행으로 저장하는 서버이며, 라이브 메모리와 지식 상태용으로 [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)를 선택적으로 사용할 수 있습니다.
에이전트와 사용자는 MCP, HTTP, Python 및 TypeScript SDK, 대시보드를 통해 Loom에 접근합니다. Loom은 Apache-2.0 라이선스로 배포됩니다. 이 페이지는 Loom이 사용 목적에 맞는지 판단하는 데 도움을 줍니다.

<a id="what-it-does"></a>

## 기능

- **지속되는 메모리.** 에이전트가 저장한 사실은 이후 세션과 다른 도구에서도 그대로 남으며, 키 하나에 주장 하나씩 이력, 출처, 유효성과 함께 보관됩니다. 한 사람의 메모리일 수도 있고, 여러 에이전트와 사람이 공유하는 메모리일 수도 있습니다.
- **쓰기 시점 검사.** 거의 같은 내용은 병합하고, 모순은 표시하며, 고정된 사실에 대한 쓰기는 거부합니다.
- **답하지 않을 줄 아는 검색.** 사용 중인 임베더에 맞게 보정된 기준을 통과하는 결과가 없으면 `memory.search`는 사유 코드와 함께 `abstain=true`를 반환합니다.
- **감사와 삭제.** 메모리를 변경하는 모든 쓰기는 콘텐츠 해시가 포함된 감사 행을 남깁니다. `erasure.request`는 읽기 경로가 접근하는 모든 테이블에 대한 잔여 데이터 점검으로 끝납니다.
- **이력을 갖춘 코드 인덱스.** 저장소에 대한 검색, 심볼, 호출자, 영향 범위, 리뷰 댓글이 포함된 PR 이력, bisect 후보를 제공하며, 각 답변에는 근거가 된 커밋과 인덱스 티어가 명시됩니다. 트래커 소스를 연결하면 이슈 트래커에 대해서도 중복 확인, 이슈↔PR 연결, 불안정한(flaky) 테스트를 제공합니다.

메모리와 코드 인덱스는 하나의 서버 위에서 서로 분리된 영역입니다. 둘 중 하나만 써도 되고 둘 다 써도 됩니다.
메모리에는 서버와 writer 토큰이 필요합니다. 코드 인덱스에는 추가로 저장소 등록(`LOOM_CODE_REPOS_JSON`, 또는 [저장소 작업 샌드박스](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox)가 실행되는 환경에서는 대시보드의 **Repositories → Add repository**)와 `LOOM_CODE_INGEST_ENABLED=true`가 필요합니다. [Claude Code 및 Codex 플러그인](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)은
둘 다 사용합니다.

<a id="where-namespaces-fit"></a>

## 네임스페이스의 위치

- **org**는 테넌트입니다.
- **네임스페이스**는 org 안에서 공유와 접근의 단위입니다. 배포에는 보통 팀 또는 프로젝트마다 하나, 격리되어야 하는 봇마다 하나, 사람마다 `personal-<slug>` 하나(처음 사용할 때 생성되며 해당 사용자의 MCP 클라이언트의 기본값이 됩니다), 인덱싱된 브랜치마다 `code-<repo>[-<branch>]` 하나가 있습니다.
- 네임스페이스의 모든 멤버는 그 안의 모든 행을 읽을 수 있습니다.
- `agent`는 행을 작성한 주체를 기록하며, 경계가 아닙니다.
- `user_id` / `read_scope=own_user`는 하나의 봇 네임스페이스 안에서 최종 사용자를 구분합니다.
- 접근 권한은 네임스페이스에 대한 역할(viewer/writer/admin)이며, 토큰에 담기거나 grant로 추가됩니다.

[식별자와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)에서 전체 모델을 확인할 수 있습니다.

<a id="when-loom-fits"></a>

## Loom이 적합한 경우

- 여러 AI 도구를 사용하며, 그중 어느 도구에든 알려 준 내용을 개인 네임스페이스에서 모든 도구가 공유하기를 원하는 경우.
- 여러 도구와 사람(Claude Code, Codex, Cursor, 지원 봇, 대시보드를 사용하는 사람)이 같은 메모리를 필요로 하는 경우.
- 에이전트가 알고 있는 내용에 SQL을 사용하고 싶은 경우. 사실, 트레이스, 비용, 감사 행은 모두 ClickHouse 테이블입니다.
- 이미 ClickHouse를 운영하고 있거나 운영할 수 있는 경우. ClickHouse는 Loom에 필요한 유일한 저장소이며, 라이브 메모리와 지식 상태에는 [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)를 선택적으로 사용할 수 있습니다.
- 자신 있게 틀린 답보다 답변 보류나 충돌 표시가 나은 경우.
- 이력, 출처, 감사 또는 검증 가능한 삭제가 필요한 경우.
- 코딩 에이전트에 저장소 인덱스가 필요한 경우.

<a id="when-it-does-not-fit"></a>

## 맞지 않는 경우

| 상황                                                         | 맞지 않는 이유                                              | 대신 고려할 것                                  |
| ------------------------------------------------------------ | ----------------------------------------------------------- | ----------------------------------------------- |
| 에이전트 하나, 프로세스 하나, 짧게 유지되는 메모리           | 공유하는 것이 없다면 서버와 데이터베이스는 오버헤드입니다   | 인프로세스 메모리 라이브러리                    |
| 핫 패스에서의 1밀리초 미만 키 조회                           | 모든 읽기가 데이터베이스 쿼리입니다                         | 자체 저장소 앞단의 캐시                         |
| 전체 그래프 분석 또는 긴 경로                                | 순회는 홉당 한 번의 제한된 쿼리이며, 최대 네 홉까지입니다   | `loom entity-export`로 데이터를 공급받는 그래프 저장소 |
| 에이전트 프레임워크가 필요한 경우                            | Loom은 에이전트 루프, 프롬프트, 모델을 실행하지 않습니다    | Loom을 메모리로 사용하는 에이전트 프레임워크    |
| 별도 설정 없이 바로 제공되는 규정 준수 보장                  | 삭제와 감사는 엔지니어링 기본 요소입니다                    | 이러한 기본 요소를 활용하는 자체 규정 준수 프로세스 |
| 백업 계획 없이 단일 노드 ClickHouse OSS에서 운영             | 복제와 특정 시점 복구가 없습니다                            | ClickHouse Cloud                                |
| 고정된 API                                                   | 작업과 필드가 릴리스마다 바뀝니다                           | 릴리스를 고정하고 `ops.capabilities`를 확인     |

유효성은 유효 시간(`valid_at`, `valid_to`)만 다룹니다. Loom이 무엇을 언제 기록했는지는 두 번째 시간 축이 아니라 버전 이력에 남습니다. 일부 기능은 운영자가 켜야 합니다: 코드 인덱스, 트래커 소스, OAuth, 텔레메트리 내보내기, 그리고 Langfuse와 ClickStack에서의 삭제입니다.

<a id="what-each-property-costs"></a>

## 각 속성의 비용

| 속성                              | 메커니즘                                                                                                | 비용                                                                                                                          |
| --------------------------------- | ------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 단일 엔진 (ClickHouse가 기준)     | 벡터, 어휘, 그래프, 필터, 감사, 삭제가 모두 동일한 행에 대한 SQL입니다                                  | 모든 읽기는 쿼리입니다. 읽기 시 버전 관리되는 행에 `FINAL` 비용을 지불합니다. 파생 행(엔티티, 요약)은 워커를 거쳐 나중에 반영됩니다. |
| 약한 일치 대신 답변 보류          | 임베더 자체의 코사인 유사도 값에서 부팅 시 도출한 게이트 (`LOOM_ADAPTIVE_THRESHOLDS`)                   | 약하지만 올바른 일치 중 일부는 거부됩니다. 요청마다 `min_similarity` 또는 `floor_mode`를 설정하세요.                          |
| 변하는 사실                       | 각 쓰기마다 병합, 중복, 충돌, 고정 판정을 수행하고, 추가 전용 `loom.memory_versions`를 사용합니다       | 각 쓰기는 이웃 항목과 비교합니다. 바꿔 쓴 문장은 누군가 해결할 때까지 검토 대기열에서 기다릴 수 있습니다.                     |
| 설명 가능한 순위                  | `explain=true`인 `memory.search`는 각 결과의 점수를 항목별로 보여 주며, 결과에는 `source_class`와 `verified`가 포함됩니다 | `explain`이 기본적으로 꺼져 있는 `top_k` 15 초과에서는 `explain=true`로 인해 각 결과가 더 커집니다.                           |
| 접근 제어가 있는 공유             | 모든 행에 붙는 ID 튜플. 토큰과 권한 부여에 네임스페이스 역할이 담깁니다                                 | 토큰을 발급하고 교체하며 역할을 부여해야 합니다. 주체 간 유출 방지 테스트 모음이 CI에서 모든 읽기 op를 검증합니다.           |
| 확인할 수 있는 삭제               | 잔여물 집계로 끝나는, 재개 가능한 사가. 외부 백엔드는 실패 시 차단(fail closed)됩니다                    | 삭제는 여러 단계로 이루어진 작업입니다. 삭제를 확인하지 못하는 백엔드가 있으면 사가가 `failed` 상태로 남아 수동 에스컬레이션이 필요합니다. |
| 최신성을 갖춘 코드 답변           | 파서 및 컴파일러(SCIP) 티어. 답변에 `as_of.commit`과 오래됨 플래그가 포함됩니다                         | 대규모 저장소는 인덱싱에 수 시간이 걸립니다. 컴파일러 티어에는 툴체인이 필요합니다.                                           |

품질에 대한 주장은 직접 측정해야 합니다. 저장소에는 벤치마크 하니스와 공개된 수치의 원본 결과가 포함되어 있습니다 ([결과](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)). 기본값을 신뢰하기 전에 실제 provider를 사용해 배포 환경에서 게이트를 실행하세요
([실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="decision-checklist"></a>

## 결정 체크리스트

오른쪽 열에 해당하는 답이 두 개 이상이면 Loom은 적합한 도구가 아닐 가능성이 높습니다.

| 질문                                                                      | Loom이 적합함 | Loom이 부적합함         |
| ------------------------------------------------------------------------- | ------------- | ----------------------- |
| 둘 이상의 도구 또는 에이전트가 메모리를 공유해야 합니까?                  | 예            | 아니요, 프로세스 하나   |
| ClickHouse를 실행하고 서비스를 운영할 수 있습니까?                        | 예            | 아니요, 관리형을 원함   |
| 데이터가 자체 인프라 안에 있어야 합니까?                                  | 예            | 특별한 선호 없음        |
| 확신에 찬 오답보다 답변 보류(abstain)나 충돌 표시가 더 낫습니까?          | 예            | 항상 무언가를 반환해야 함 |
| 이력, 출처, 감사 또는 검증 가능한 삭제가 필요합니까?                      | 예            | 아니요                  |
| 핫 패스가 1밀리초 미만의 키 조회입니까?                                   | 아니요        | 예                      |
| 몇 단계 이동이 아니라 전체 그래프 분석이 필요합니까?                      | 아니요        | 예                      |
| 릴리스 간 API 변경을 감수하거나 특정 버전에 고정할 수 있습니까?           | 예            | 아니요                  |
| 실제 임베더와 LLM 비용을 지불하고 자신의 데이터로 벤치마크할 수 있습니까? | 예            | 아니요                  |

<a id="prerequisites"></a>

## 사전 요구 사항

| 필요한 것                                         | 용도                                                                                                                                                                                                                                                           |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse (평가에는 OSS, 프로덕션에는 Cloud)     | 모든 기능. [ClickHouse 요구 사항](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#clickhouse-requirements)을 참고하세요.                                                                                                                                                                |
| Postgres 16+ (선택 사항)                          | [Postgres 메모리 권한](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)에서 사용하는 라이브 메모리와 지식 상태. 배포를 처음 시작할 때 선택하며, `LOOM_PG_DURABILITY=local`이 아니면 동기 스탠바이가 필요합니다([요구 사항](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#requirements)). |
| Docker, Compose 2.20 이상                         | 릴리스된 로컬 스택(한 줄 설치 스크립트, Windows에서는 [WSL 2에서](quickstart.md#fast-path-released-image-no-clone))와 서버 이미지.                                                                                                                   |
| Python 3.12+                                      | 기여자 전용 `loom up`, 서버 개발, 운영자 CLI 작업.                                                                                                                                                                                          |
| 임베딩 제공자                                     | 실제 검색: OpenAI, Gemini 또는 릴리스된 스택이 사용하는 로컬 CPU 임베더.                                                                                                                                                                             |
| LLM 제공자                                        | 추출, `memory.ask` 합성, `code.ask`: OpenAI 또는 Anthropic.                                                                                                                                                                                           |
| 토큰과 네임스페이스                               | 접근 권한. [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)를 참고하세요.                                                                                                                                                                                               |
| 코드 인덱스용                                     | 리포지토리에 대한 Git 접근 권한, PR 기록을 위한 `LOOM_CODE_GITHUB_TOKEN`, 그리고 첫 인덱싱에 필요한 시간.                                                                                                                                                         |

<a id="next"></a>

## 다음 단계

[Loom 평가하기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)에서는 제품 스모크 테스트와 실제 품질 평가를 구분합니다. [빠른 시작](quickstart.md)에서는 로컬 스택을 실행합니다.
[배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)에서는 팀 단위로 Loom을 운영하는 방법을 설명합니다. [문서 맵](https://github.com/ClickHouse/loom/blob/main/docs/index.md)에는 모든 페이지가 읽는 순서대로 나열되어 있습니다.
