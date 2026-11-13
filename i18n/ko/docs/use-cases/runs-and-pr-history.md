<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# 에이전트 실행, 트레이스, PR 히스토리

Loom은 코딩 에이전트가 수행한 작업을 실행(run)과 검색 가능한 세션 대화로 기록하고, 이를 코드 히스토리와 연결합니다. 실행에는 목표와 결과가 담기며, Claude Code 플러그인이 세션의 턴 사용량을 보고하는 경우(기본적으로 켜져 있음) 모델 비용도 함께 기록됩니다. 히스토리에는 병합된 풀 리퀘스트, 리뷰 코멘트, GitHub 이슈가 포함되고, 트래커 소스를 연결하면 인시던트와 CI 실행도 포함됩니다. 대화 기록, PR 히스토리, 트래커는 각각 켜기 전까지 꺼져 있습니다. 이렇게 하면 에이전트나 사람이 이전에 어떤 시도를 했는지, 무엇을 결정했는지, 어떤 변경이 회귀를 일으켰는지 물어볼 수 있습니다.

<a id="what-you-get"></a>

## 제공 기능

- **Runs.** `run.begin`과 `run.complete`는 작업의 목표, 상태, 에이전트가 참고한 메모리, 그리고 에이전트가 기록한 내용을 저장합니다. [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **대화와 세션 브리프.** 기록된 세션은 검색할 수 있으며, 세션 브리프를 사용하면 다음 세션에서 작업을 이어갈 수 있습니다. 네임스페이스 소유자가 `conversation.set_logging(enabled=true)`로 켜기 전까지는 기록이 꺼져 있습니다. [대화와 세션](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [장시간 에이전트 실행 추적 및 재개](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **PR 이력.** [코드 인덱스 페이지](code-intelligence.md)에 설명된 대로 심볼, 경로 또는 라인의 이력, 리뷰 브리프, PR 이전 중복 검사, bisect 후보를 제공합니다.
- **트래커 레코드.** 중복 이슈 검사, 이슈와 PR 간 링크, 불안정한 테스트(flaky test)를 팀 자체 ClickHouse에서 실시간으로 읽어옵니다. `LOOM_TRACKER_ORG`와 등록된 트래커 소스가 필요합니다. [트래커 플레인](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **트레이스와 비용.** Loom은 OpenTelemetry를 내보내고 작업별 자체 모델 사용 비용을 기록합니다. Langfuse와 ClickStack은 선택적 싱크입니다.
  [텔레메트리](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [비용 및 사용량](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## 측정 결과

공개 ClickHouse 이슈에서 `tracker.duplicate_check`는 기록된 중복 104건 중 91건(0.875)에서 더 오래된 이슈를 가장 높은 순위에 올렸고, 링크가 기록되지 않은 이슈 100건 중 2건을 중복으로 판정했습니다. 이 결과는 하나의 트래커에 대한 것이며, 해당 이슈들(스택 트레이스, 오류 코드, 일관되게 연결된 중복)은 다른 많은 트래커의 이슈보다 처리하기 쉬운 편입니다.
[공개 ClickHouse 이슈의 중복 검사](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## 시작하기

[코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)에서는 기록을 켠 뒤 세션을 캡처하는 플러그인을 설치합니다. [코드 인덱스](code-intelligence.md)를 사용하려면 저장소를 추가하고 `LOOM_CODE_INGEST_ENABLED=true`를 설정해야 합니다.

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 실험적(experimental) 기능으로 표시되어 있습니다.
