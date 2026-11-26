<!-- i18n: source=docs/index.md blob=173314858f057f518abf314eb98af3afff03c87b model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/index.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/index.md), which is authoritative.

<a id="clickhouse-loom-documentation"></a>

# ClickHouse Loom 문서

Loom은 AI 에이전트를 위한 메모리, 문서 및 코드 인덱스 서버로, ClickHouse에 데이터를 저장하며,
실시간 메모리와 지식 상태를 위해 [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md#why-two-stores)를 선택적으로 사용할 수 있습니다. 에이전트와 사용자는 MCP, HTTP, Python 및
TypeScript SDK, 그리고 대시보드를 통해 Loom에 접근합니다. 이 페이지는 모든 페이지를 읽는 순서대로 나열합니다.

![대시보드의 라이브러리: 환불 규칙 검색에 대해 인용된 구절로 답변한 화면](https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/document-search.webp)

<video src="https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/clip-assistant-answer.webm" controls autoplay loop muted playsinline width="100%" aria-label="Ask AI answering a question about refunds, with its numbered sources checked against Loom"><a href="https://github.com/ClickHouse/loom/blob/main/docs/assets/screenshots/clip-assistant-answer.webm">확인된 출처와 함께 답변하는 Ask AI (클립)</a></video>

<a id="start-here"></a>

## 시작하기

| 단계                     | 읽을 문서                                                                                                                                                                                 |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. 결정                  | [개요](getting-started/overview.md) → [Loom 평가하기](getting-started/evaluate.md)                                                                                                        |
| 2. 설치                  | [빠른 시작](getting-started/quickstart.md) → [배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)                                                                                                      |
| 3. 에이전트 연결         | [코딩 에이전트 연결하기](getting-started/connect-a-coding-agent.md) → [통합](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)                                                                      |
| 4. SDK 및 API            | [첫 번째 에이전트](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md) → [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) / [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)                                                            |
| 5. 네임스페이스 및 접근  | [ID와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) → [토큰과 권한](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)                                                                     |
| 6. 코드 인덱스           | [코드 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) → [리포지토리](features/code-index/repositories.md) → [코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)                        |
| 7. 운영                  | [모니터링](operations/monitoring.md) · [문제 해결](operations/troubleshooting.md) · [업그레이드](operations/upgrades.md) · [백업 및 복구](operations/backup-and-recovery.md) |

<a id="use-cases"></a>

## 사용 사례

필요별로 한 페이지씩 구성했으며, 각 페이지에서 Loom이 해당 필요에 무엇을 해주는지와 어디서 시작하면 되는지를 설명합니다:
[에이전트 메모리](use-cases/agent-memory.md) ·
[문서 검색과 RAG](use-cases/document-search.md) ·
[코딩 에이전트를 위한 코드 인덱스](use-cases/code-intelligence.md) ·
[언어별 코드 인덱스](use-cases/code-intelligence-by-language.md) ·
[코드와 연결된 메모리](use-cases/memory-linked-to-code.md) ·
[코딩 에이전트 모델 비용](use-cases/coding-agent-cost.md) ·
[멀티 에이전트 공유 컨텍스트](use-cases/multi-agent.md) ·
[자율 에이전트: 회상, 감사, 삭제](use-cases/autonomous-agents.md) ·
[실행, 트레이스, PR 이력](use-cases/runs-and-pr-history.md) ·
[ClickHouse 셀프 호스팅](use-cases/self-hosted-clickhouse.md) ·
[모든 사용 사례](use-cases/index.md).

<a id="1-getting-started"></a>

## 1. 시작하기

- [개요](getting-started/overview.md): Loom이 무엇인지, 어떤 경우에 적합한지, 사전 요구 사항.
- [포함된 기능](getting-started/whats-included.md): Loom의 주요 기능과 각 기능의 페이지.
- [빠른 시작](getting-started/quickstart.md): 로컬 스택을 처음부터 끝까지 구성하는 방법.
- [모델 및 API 키 구성](getting-started/models-and-keys.md): 에이전트 구독,
  서버 LLM, 임베딩, 단일 공급자 구성.
- [Loom 평가](getting-started/evaluate.md): 스모크 테스트, 워크로드 평가,
  재현 가능한 공개 벤치마크.
- [코딩 에이전트 연결](getting-started/connect-a-coding-agent.md): 플러그인 또는 MCP 진입점.
- [코딩 에이전트로 Loom 설정](getting-started/agent-setup.md): 고정된 릴리스를 설치하고
  동작을 검증하는 에이전트를 위한 문서이며, 정해진 형식의 보고서를 포함합니다.
- [첫 번째 에이전트](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): SDK 튜토리얼.

<a id="2-install-and-run-the-server"></a>

## 2. 서버 설치 및 실행

- [배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): 구성 형태, 환경, 프로바이더, ID 모드,
  프록시, 이미지, 프로덕션 체크리스트.
- [싱글 사인온](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md): 대시보드, 
  `loom login`, MCP 클라이언트를 위한 OIDC ID 프로바이더.
- [대시보드 없이 Loom 사용하기](guides/without-the-dashboard.md): 모든 대시보드 보기에 대응하는 `loom login`, `loom call`
  및 SDK 호출.
- [업그레이드](operations/upgrades.md) · [지원 버전](operations/supported-versions.md) ·
  [백업 및 복구](operations/backup-and-recovery.md) ·
  [모니터링](operations/monitoring.md) · [유지 관리](operations/maintenance.md) ·
  [런타임 설정](operations/runtime-settings.md) ·
  [문제 해결](operations/troubleshooting.md) ·
  [코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md) ·
  [N개 레플리카 실행](operations/replicas.md): 하나의 URL 뒤에 있는 동일한 Loom들 ·
  [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md): Postgres 메모리 권한 저장소 ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md): EKS 및 kind 오버레이가 포함된 Helm 차트 ·
  [ClickHouse Cloud 및 Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md): 두 관리형 서비스 위에서 동작하는
  차트 ·
  [네임스페이스를 다른 Loom으로 복사하기](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md) ·
  [사용량을 데이터 웨어하우스로 내보내기](operations/analytics-export.md)

<a id="3-connect-agents"></a>

## 3. 에이전트 연결

[통합](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md) ·
[MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) ·
[에이전트 지침](features/integrations/agent-instructions.md) ·
[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) ·
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) · [Cursor](features/integrations/cursor.md) ·
[VS Code](features/integrations/vscode.md) ·
[프레임워크](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md) ·
[Grep 셤](features/integrations/grep-shim.md) ·
[Langfuse](features/integrations/langfuse.md) ·
[ClickStack](features/integrations/clickstack.md) ·
[OKF](features/integrations/okf.md)

<a id="4-sdk-and-api"></a>

## 4. SDK와 API

[참조 색인](reference/index.md) · [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md) ·
[MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md) · [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) ·
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) · [환경 변수](reference/environment.md) ·
[호출 가능한 연산(op)](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md) · [구성](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) ·
[워커](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md) · [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md) · [오류](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md) ·
[상태](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) · [스키마](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md) ·
[추출기 계약](reference/extractor-contract.md). 연산과 필드는 릴리스마다 바뀔 수 있습니다.
[알 수 없는 필드와 제거된 필드](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)에서는 서버가 더 이상 읽지 않는 필드를
클라이언트가 어떻게 받게 되는지 설명합니다.

<a id="5-namespaces-access-and-the-memory-model"></a>

## 5. 네임스페이스, 접근 권한, 메모리 모델

- [아이덴티티와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md): 튜플, 역할,
  권한 부여(grant), 읽기 범위.
- [메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md): 행(row), 쓰기를 판단하는 방식, 시간, 이력,
  출처(provenance).
- [검색과 랭킹](concepts/retrieval-and-ranking.md): 레그(leg), 융합(fusion), 응답 보류(abstain),
  보정(calibration).

<a id="guides"></a>

## 가이드

[에이전트 연동](guides/agent-integration.md) ·
[헤드리스 봇](guides/headless-bots.md) ·
[토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) ·
[긴 실행 추적 및 재개](guides/trace-and-resume.md) ·
[네임스페이스 최적화](guides/optimize-namespace.md) ·
[조사 에이전트 메모리](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md) ·
[추출 파이프라인](guides/extraction-pipeline.md) ·
[성능 튜닝](guides/performance-tuning.md) · [디버깅](guides/debugging.md) ·
[Postgres에서 Loom 쿼리하기](guides/query-from-postgres.md) ·
[Loom으로 가르치기](guides/teaching.md)

<a id="features"></a>

## 기능

- 플레인: [네임스페이스와 공유](features/namespaces-and-sharing.md) ·
  [통합 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) · [지식](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md) ·
  [엔티티 그래프](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md) ·
  [대화와 세션](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [실행](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md) · [태스크와 의사결정](features/tasks-and-decisions.md) ·
  [대시보드](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)
- 메모리: [쓰기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md) ·
  [검색과 질문](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) ·
  [충돌과 거부](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [추출](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) ·
  [이력과 출처](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md) ·
  [보존과 삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) ·
  [절차와 학습 내용](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md) ·
  [워킹 메모리와 프로필](features/memory/working-memory-and-profiles.md)
- 관측성: [감사](features/observability/audit.md) ·
  [비용과 사용량](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md) ·
  [텔레메트리](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)
- 트래커: [개요](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) ·
  [레코드 계약](features/tracker/record-contract.md)

<a id="6-code-index"></a>

## 6. 코드 인덱스

[개요](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) ·
[리포지토리](features/code-index/repositories.md) ·
[최신성](features/code-index/freshness.md) ·
[기록과 PR](features/code-index/history-and-prs.md) ·
[호출 가능한 오퍼레이션(op)](features/code-index/operations.md) ·
[코드 인덱스 운영 런북](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)

<a id="architecture"></a>

## 아키텍처

[시스템 개요](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md) ·
[서버 레이아웃](architecture/server-layout.md) ·
[ClickHouse](architecture/clickhouse.md) ·
[메모리 아키텍처](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md) ·
[기준 스토리지 백엔드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md) ·
[셀](architecture/cells.md) ·
[스토리지 레이아웃](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-layout.md) ·
[코드 인덱스 아키텍처](architecture/code-index-architecture.md) ·
[문서 추출](architecture/document-extraction.md) ·
[문서 소스](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md) ·
[LLM 및 임베딩](architecture/llm-architecture.md) ·
[보안](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md) ·
[어시스턴트 설계](https://github.com/ClickHouse/loom/blob/main/docs/architecture/assistant.md) ·
[장애 및 복구](architecture/failure-and-recovery.md) ·
[롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)

<a id="benchmarks"></a>

## 벤치마크

[개요](benchmarks/overview.md) · [커버리지](benchmarks/coverage.md) ·
[방법론](benchmarks/methodology.md) ·
[실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md) · [결과](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[케이스 추가](benchmarks/adding-a-case.md) · [스킬 평가](benchmarks/skill-eval.md)

<a id="development"></a>

## 개발

기여자를 위한 문서: [저장소 가이드](https://github.com/ClickHouse/loom/blob/main/docs/development/repository-guide.md) ·
[로컬 개발](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md) ·
[테스트](development/testing.md) · [코드 생성](development/code-generation.md) ·
[마이그레이션](development/migrations.md) ·
[릴리스 절차](development/release-process.md) ·
[문서 스타일](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md) ·
[큐레이션 루프 설계](https://github.com/ClickHouse/loom/blob/main/docs/development/curation-loop-design.md) ·
[Loom 확장](development/extending-loom.md) ·
[오퍼레이션 추가](development/adding-an-operation.md) ·
[워커 추가](development/adding-a-worker.md) ·
[테이블 추가](development/adding-a-table.md) ·
[언어 지원 추가](development/adding-language-support.md) ·
[대시보드 설계](development/dashboard-design.md).
[CONTRIBUTING.md](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md)에서 풀 리퀘스트 워크플로를 다룹니다.

<a id="also"></a>

## 함께 보기

[용어집](glossary.md): Loom의 용어를 한곳에 정리했습니다. [자주 묻는 질문](faq.md): 가장 먼저 나오는 질문과 한 단락짜리 답변입니다. [`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt): 에이전트를 위해 생성한 문서 색인입니다. [`llms-full.txt`](https://loom.clickhouse.com/docs/llms-full.txt)에는 Loom이 무엇인지, 어떻게 시작하는지를 설명하는 페이지가 하나의 파일에 담겨 있습니다.
