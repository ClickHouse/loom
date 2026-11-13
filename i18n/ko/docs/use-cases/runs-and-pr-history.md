<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# 에이전트 실행, 트레이스, PR 이력

Loom은 코딩 에이전트가 수행한 작업을 실행(run)과 검색 가능한 세션 대화로 기록하고, 이를 코드 이력과 연결합니다. 실행에는 목표와 결과가 담기며, Claude Code 플러그인이 세션의 턴 사용량을 보고하면(기본으로 켜져 있음) 모델 비용도 함께 담깁니다. 이력에는 병합된 pull request, 리뷰 코멘트, GitHub 이슈가 포함되고, tracker 소스가 있으면 인시던트와 CI 실행도 포함됩니다. 대화 기록, PR 이력, tracker는 각각 켜기 전까지 꺼져 있습니다. 그러면 에이전트나 사람이 이전에 무엇을 시도했는지, 무엇을 결정했는지, 어떤 변경이 회귀를 일으켰는지 물어볼 수 있습니다.

<a id="what-you-get"></a>

## 제공 기능

- **실행(Runs).** `run.begin`과 `run.complete`는 작업의 목표, 상태, 에이전트가 참고한 메모리, 에이전트가 기록한 내용을 저장합니다. [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **대화와 세션 브리프.** 기록된 세션은 검색할 수 있고, 세션 브리프를 통해 다음 세션이 작업을 이어갈 수 있습니다. 기록은 네임스페이스 소유자가 `conversation.set_logging(enabled=true)`로 켜기 전까지 꺼져 있습니다. [Conversations and sessions](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [Trace and resume long agent runs](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **PR 이력.** 심볼, 경로, 라인의 이력, 리뷰 브리프, PR 전 중복 확인, bisect 후보를 제공합니다. 자세한 내용은 [코드 인덱스 페이지](code-intelligence.md)를 참고하세요.
- **Tracker 레코드.** 중복 이슈 확인, 이슈와 PR의 연결, 불안정한(flaky) 테스트를 팀의 ClickHouse에서 실시간으로 읽어 옵니다. `LOOM_TRACKER_ORG`와 등록된 tracker 소스가 필요합니다. [Tracker plane](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **트레이스와 비용.** Loom은 OpenTelemetry를 내보내고 작업별 자체 모델 지출을 기록합니다. Langfuse와 ClickStack은 선택적 싱크입니다.
  [Telemetry](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [Cost and usage](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## 측정 결과

공개 ClickHouse 이슈에서 `tracker.duplicate_check`는 기록된 중복 104건 중 91건(0.875)에서 더 오래된 이슈를 첫 번째로 올렸고, 연결 기록이 없는 이슈 100건 중 2건을 중복으로 판정했습니다. 이 결과는 하나의 tracker에 대한 것이며, 해당 이슈들(스택 트레이스, 에러 코드, 일관되게 연결된 중복)은 많은 tracker의 이슈보다 다루기 쉽습니다.
[Duplicate check on public ClickHouse issues](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## 시작하기

[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)는 기록이 켜지면 세션을 캡처하는 플러그인을 설치합니다. [코드 인덱스](code-intelligence.md)를 쓰려면 저장소를 추가하고 `LOOM_CODE_INGEST_ENABLED=true`를 설정해야 합니다.

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 실험적(experimental)으로 표시되어 있습니다.
