<!-- i18n: source=docs/features/runs.md blob=7aa894eee29c65b040ebfbad315df18b437034ae model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md), which is authoritative.

<a id="runs"></a>

# 실행

실행(run)은 "이슈 1234 분류"와 같은 작업 하나를 한 번 수행하는 단위입니다. `run.begin`이 실행을 열고 `run_id`를 반환합니다. 에이전트는 쓰기 작업(`memory.set`, `memory.update`, `observe.record`)에 이 id를 함께 전달합니다. `run.complete`는 상태, 에이전트가 참고한 메모리, 그리고 선택 사항으로 회고(reflection)를 받아 실행을 닫습니다. 읽기 작업(`memory.search`, `memory.ask`)에서 본문의 `run_id`는 해당 실행 자체의 행만 걸러냅니다. 따라서 읽기에는 대신 다른 방식으로 레이블을 붙입니다. HTTP에서는 `X-Loom-Run-Id` 헤더(Python SDK에서는 `run_scope`)를, MCP에서는 세션 자체의 실행을 사용합니다. 성공한 실행이 참고한 메모리는 다음번에 더 높은 순위로 노출되고, 실패한 실행이 참고한 메모리는 그동안 얻은 가중치를 잃습니다.

<a id="what-a-run-records"></a>

## 실행이 기록하는 내용

실행 행은 `loom.agent_runs`에 있습니다. `run.get`이 보여 주는 대부분의 값은 읽는 시점에 다른 plane에서 파생됩니다.

| 필드                               | 저장 또는 파생 | 출처                                                                                                                                                    |
| ---------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `goal`, `user_request`, `model`, … | 저장           | `run.begin`                                                                                                                                             |
| `status`, `final_response`         | 저장           | `run.complete` (그 전까지는 `active`)                                                                                                                   |
| `memories_used`                    | 저장           | `run.complete`: 에이전트가 의존했다고 직접 밝힌 내용                                                                                                    |
| `grade`, `grade_source`, …         | 저장           | 실행이 종료된 후의 `run.grade`                                                                                                                          |
| `memories_written`                 | 파생           | 실행의 `run_id`를 가진 `loom.memory`의 행                                                                                                               |
| `recalled`, `recalled_keys`        | 파생           | `run_id`에 대한 검색 트레이스 로그와 `run.complete(recalled_keys)`의 합집합                                                                             |
| `observations`                     | 파생           | `run_id`가 태그된 `observe.record`의 에피소드 행                                                                                                        |
| `turns`, `cost_usd`                | 파생           | 실행이 열려 있는 동안 발생한 세션의 턴(`loom.agent_turn_usage`에 기록됨, Claude Code 플러그인 또는 `cost.record_turns`를 사용하는 에이전트가 기록)       |
| `budget_usd`, `budget_seconds`     | 저장           | `run.begin` (0 = 예산 없음)                                                                                                                             |
| `elapsed_seconds`, `over_budget`   | 파생           | 서버 시계 기준으로 실행 시작부터 종료(또는 현재)까지의 시간과 `cost_usd`를 예산과 비교하여 계산                                                         |

`LOOM_SEARCH_TRACE_LOG_ENABLED`가 꺼져 있고 `recalled_keys`도 전송되지 않았다면 `recalled`는 `-1`입니다.

<a id="budgets"></a>

## 예산

`run.begin`은 선택 항목인 `budget_usd`(모델 지출)와 `budget_seconds`(실행에 걸릴 수 있는 시간)를 받습니다. `run.get`은 이 값을 `cost_usd`, `elapsed_seconds`와 비교하여 실행이 초과한 항목을 `over_budget`에 나열합니다(`cost`, `time`, 또는 둘 다). Loom은 에이전트를 실행하지 않으므로, 예산은 에이전트를 실행하는 쪽에 보내는 신호일 뿐 실행을 중단시키지 않습니다.

실행의 지출은 실행이 열려 있는 동안 해당 세션에서 발생한 턴의 비용이며, 각 턴의 고유 시각(대화 기록의 `turn_at`)을 기준으로 합산합니다. 따라서 `budget_usd`에는 `session_id`가 필요합니다(없으면 422). 비용과 마찬가지로 소수점 6자리까지 유지하므로 가장 작은 값은 $0.000001입니다. 시간은 클라이언트가 보고한 `duration_ms`와 관계없이 서버 시계를 기준으로 실행 시작부터 종료까지 계산합니다. 예산은 실행의 첫 `run.begin`에서 설정하세요.

세션에서 활성 실행이 예산을 초과한 것을 처음 발견한 `run.get`에는 멘토 힌트 `run_over_budget`도 함께 담기며, 플러그인이 이를 조언으로 모델에 전달합니다. 종료된 실행도 `over_budget`은 보고하지만 힌트는 받지 않습니다. 다른 멘토 힌트와 마찬가지로 세션당 한 번만 전달되며, 세션을 보내지 않는 호출자(SDK, 일반 HTTP)는 네임스페이스와 웹 프로세스마다 하나를 공유하므로, 힌트가 아니라 `over_budget`을 기준으로 판단하세요.

제한 사항:

- **프롬프트 사이에서만 확인됩니다.** Claude Code 플러그인은 프롬프트가 끝날 때 해당 프롬프트의 턴을 전송하므로(에이전트가 `cost.record_turns`로 직접 전송할 수도 있습니다), 비용 예산은 프롬프트 사이에서 확인되고, 단일 헤드리스 프롬프트(`claude -p`)의 지출은 끝난 뒤에야 나타납니다. 에이전트 자신의 `run.complete` 이후에 도착한 턴도 계속 합산됩니다. 호출의 비용은 호출이 반환된 뒤에야 알 수 있으므로, 실행은 어떤 곳에서 감지하기 전에 마지막 턴의 비용만큼 `budget_usd`를 넘어설 수 있습니다.
- **클라이언트의 시계.** 이 구간은 클라이언트의 턴 시각을 서버의 실행 시각과 비교하므로, 시계가 몇 분 어긋나면 실행 경계의 턴이 잘못 분류됩니다. 턴은 전송될 때의 시각을 유지하므로, 다시 전송한 턴은 처음 속했던 실행에 그대로 남습니다. 시각 없이(또는 해석할 수 없는 시각으로) 전송한 턴은 `message_id`가 기록된 시각을 사용하며, 새 턴이면 현재 시각을 사용합니다. 처음 전송 후 1초도 안 되어 다시 전송하면 현재 시각으로 기록될 수 있습니다. `cost.record_turns`는 서버 시계보다 5초를 넘게 앞선 시각의 턴을 `turns_dated_ahead`로 응답하므로, 턴을 직접 전송하는 호출자는 시계가 빠른지 확인할 수 있습니다. Claude Code 플러그인은 응답을 읽지 않고 전송합니다.
- **하나의 세션, 여러 실행.** 한 세션에서 동시에 열린 실행은 같은 턴을 봅니다. 중첩된 실행은 양쪽 모두에 합산되며, 비용 예산이 각각 필요한 동시 워커는 각자의 세션이 있어야 합니다.
- **가격 정보가 없는 모델.** 가격표에 없는 모델의 턴은 비용이 0이며, `run.get`은 이런 턴을 `unpriced_turns`로 집계하고 `budget_usd`에는 합산하지 않습니다.
- **MCP.** MCP 도구는 `budget_seconds`만 제공합니다. 모델은 턴이 기록되는 세션 id를 알지 못하기 때문입니다.
- **재시도.** 다시 전송한 `run.begin`은 다른 재시도와 마찬가지로 실행의 행을 교체합니다. 다시 지정하지 않은 세션과 예산은 유지하고, 0이 아닌 새 예산은 기존 예산을 대체하며, 0은 기존 값을 유지합니다.
  새 `budget_usd`는 `session_id`와 함께 보내야 하며(없으면 422), 새 `budget_seconds`는 다른 값이 필요 없습니다.
  실행을 다른 세션으로 옮기는 재전송 begin은 이전 세션의 턴을 기준으로 설정된 비용 예산을 제거하고 시간 예산은 유지하며, 이를 `next`에 알려 줍니다. 예산을 제거하는 방법은 이것뿐입니다.
  `run.begin`이 보낸 것과 다른 `run_id`로 응답했다면 그 `run_id`를 다시 보내거나 같은 `session_id`를 보내세요. 그렇지 않으면 재시도가 새 실행을 엽니다.

<a id="recalled-is-not-relied-on"></a>

## 불러왔다고 해서 의존한 것은 아니다

`run.complete`에는 두 개의 목록이 함께 전달됩니다.

- **`memories_used`** — 에이전트가 실제로 의존한 키입니다. 각 키에는 `used` 사용 이벤트가 기록되며, 결과가 긍정적이면 `evidence` 이벤트가, 부정적이면 `harm` 이벤트가 추가로 기록됩니다. `outcome`의 기본값은 `status`에서 정해집니다. `completed`는 긍정, `failed`는 부정, `cancelled`는 없음입니다.
- **`recalled_keys`** — 실행 중에 Loom의 도구가 제공한 키입니다(Claude Code와 Codex 플러그인은 `loom_search`와 `memory.get` 결과에서 이 키를 따로 보관해 두었다가 세션이 끝날 때 전송합니다). 이 키는 `run.get`을 위해 기록될 뿐, 보상에는 반영되지 않습니다.

반환되었지만 사용되지 않은 행은 아무런 보상도 받지 못합니다. 검색에 적중한 모든 행에 보상을 주면 가장 먼저 노출된 항목에 보상을 주는 셈이 되기 때문입니다.
`usage_fold` 워커는 이 이벤트들을 강화 항으로 합산하며, [메모리 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)은 이 값을 기준으로 순위를 매깁니다. `harm` 이벤트는 얻은 가산점을 회수하되 해당 행의 작성 시점 기본값을 하한으로 하므로, 한 번의 나쁜 실행으로 행이 관련도 아래로 밀려나거나 삭제되는 일은 없습니다.

`memories_used`에 지정된 프로시저 키는 프로시저 원장에도 행이 추가됩니다(경로는 비어 있고 `ok`는 결과에서 가져옵니다). `procedure.record_run`을 직접 호출하는 에이전트는 실행이 두 번 집계되지 않도록 해당 키를 `memories_used`에서 빼야 합니다. [프로시저와 학습 내용](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)을 참고하세요.

<a id="reflections"></a>

## 회고

`run.complete(reflection=...)`는 `what_worked`, `what_failed`, `assumptions`, `lessons`, `improvements`(각각 최대
20개 항목), `confidence`, 그리고 교훈의 근거가 되는 `evidence` 키를 기록합니다. 이 내용은 `reflection:<run_id>` 아래에 `reflection` 유형의 메모리 하나로 저장됩니다.

- 임베딩되므로 이후 실행에서 `memory.search(memory_type="reflection")`으로 찾을 수 있지만,
  필터 없는 검색과 `memory.ask`에서는 제외됩니다.
- `what_failed`와 `lessons`에는 `evidence` 키가 하나 이상 필요합니다(없으면 422가 반환됩니다).
- `expires_in_days`가 지나면 만료됩니다(기본값 90, `0`은 만료 없음). 단, `memory.verify`나 긍정적인 결과가 있으면 만료가 갱신됩니다.
- `role`(planner, executor, critic 또는 직접 정한 이름)을 지정해 시작한 실행은 회고에 `role:<role>` 태그를 붙이므로, 같은 역할의 에이전트가 `memory.search(role=...)`를 호출하면 이 회고가 우선적으로 검색됩니다.

<a id="what-happens-to-the-rows-a-run-wrote"></a>

## 실행이 기록한 행은 어떻게 되는가

`run.complete`의 `retain`은 해당 실행(run) 아래에서 기록된 메모리의 처리 방식을 결정합니다.

| `retain`         | 효과                                                                                                                                          |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `keep` (기본값)  | 아무것도 변경되지 않습니다.                                                                                                                   |
| `demote`         | 해당 메모리의 신뢰도가 즉시 낮아집니다.                                                                                                       |
| `expire`         | `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS`(기본값 7)일 뒤의 만료 시점이 `on_expire=demote`와 함께 설정되며, `memory_expiry_sweep` 워커가 이를 적용합니다. |

고정(pinned), 검증(verified), 승격(promoted), 잠금(locked), 에피소딕(episodic), 리플렉션(reflection) 행은 모드와 관계없이 유지되며, 어떤 모드도 행을 삭제 표시(tombstone)하지 않습니다. 기본값은 `LOOM_MEMORY_RUN_COMPLETE_RETAIN`에서 가져옵니다. MCP 세션의 암묵적 실행은 이를 종료하는 전송 계층이 작업의 성과를 판단할 수 없으므로 항상 유지(keep)됩니다.

<a id="graded-runs"></a>

## 평가된 실행

`status`는 실행이 끝났음을 알려 주고, 평가(grade)는 그 결과가 유지되었는지를 알려 줍니다. 평가는 CI가 통과하거나 리뷰어가 풀 리퀘스트를 거절하는 시점, 즉 며칠 뒤에 도착하는 경우가 많습니다. `run.grade`는 `grade`(`pass` 또는 `fail`), `source`(`maintainer`, `ci`, `reviewer` 등 자유 텍스트), `reason`, `ref`(URL, finding key, check run id)를 받으며, 하나의 `run_id` 또는 하나의 `session_id`(예: `pr_1234`)에 속한 종료된 모든 실행을 대상으로 지정합니다. 다시 평가하면 이전 판정을 대체하고, 같은 source가 같은 판정을 내리면 아무것도 바뀌지 않습니다. 진행 중인 실행은 건너뜁니다.

평가된 실행의 `memories_used`에 있는 모든 메모리는 그 평가에 반영되어 집계됩니다. 이 집계값은 `memory.proposals`가 시험 중인 각 사실(`memory.set(proposed=true)` 행) 옆에 보여 주는 값입니다. Loom이 이 값으로 제안을 결정하는 경우는 네임스페이스 소유자가 `proposal_policy.mode`를 `auto`로 설정했을 때뿐이며, 어느 모드에서든 작성자는 `memory.proposal_decide`로 직접 결정할 수 있습니다. [메모리 쓰기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)를 참고하세요.

`run.list(grade="ungraded")`는 아직 판정을 받지 못한 종료된 실행을 나열합니다.

MCP 세션은 요청하지 않아도 암묵적 실행(implicit run)을 갖습니다. 실행 범위를 가지는 첫 도구 호출이 실행을 시작하고, `DELETE /mcp` 또는 `mcp_session_run_sweep` 워커가 이를 종료합니다([MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md) 참고). 스윕은 `environment='mcp'`이고 `metadata.implicit`가 true이며 id가 `mcp-` 뒤에 16진수 40자가 붙은 형태(전송 계층만 만들 수 있는 형태)인 활성 실행을 종료합니다. 종료 조건은 해당 실행의 토큰으로 이루어진 호출이(토큰을 기록하지 않는 실행이라면 누구의 호출이든) `LOOM_MCP_SESSION_RUN_IDLE_SECONDS` 안에 도달하지 않았을 때입니다. 이 실행은 호출자의 기본 네임스페이스에 속하므로, 그 실행을 읽는 쪽에서 보이는 내용도 그 네임스페이스 안에 머뭅니다. 실행의 goal은 `mcp session (<agent>)`라는 레이블이며 호출의 텍스트는 절대 들어가지 않습니다(읽기 호출의 쿼리는 호출자가 읽을 수 있는 어느 네임스페이스든 대상으로 삼을 수 있기 때문입니다). 또한 `memories_written`에는 그 네임스페이스의 쓰기만 나열됩니다(다른 네임스페이스의 쓰기에도 해당 `run_id`는 여전히 붙습니다). 종료는 아무것도 판정하지 않습니다. outcome은 `none`이고, 서버의 기본 retain 모드가 무엇이든 그 메모리는 유지됩니다. 따라서 두 종료가 경합하더라도(두 호스트의 스윕, 또는 `DELETE`) 최악의 경우 같은 완료 기록이 두 번 쓰일 뿐입니다.

실행이 여러 웹 프로세스에 걸치는 방식은 다음과 같습니다. 실행 id는 호출자의 org, 토큰의 subject, `Mcp-Session-Id`의 해시이므로, 서버의 모든 프로세스가 같은 세션에 대해 같은 실행을 가리키고, 다른 토큰은 같은 세션 id를 보내더라도 그 실행을 지정할 수 없습니다. 프로세스는 세션의 첫 번째로 허용된 실행 범위 호출에서 실행을 등록하고, begin 행을 백그라운드에서 씁니다(대기하는 insert는 ClickHouse Cloud에서 그 호출을 수 초간 붙잡아 둘 수 있습니다). 게이트가 거부한 호출은 실행 행을 쓰지 않습니다. 행을 찾지 못한 두 프로세스가 각각 첫 begin을 쓰면, 더 이른 시작과 그 소유자가 우선합니다(첫 begin의 행 버전은 시작 시점부터 거꾸로 세며, 이후의 모든 쓰기가 이를 대체합니다). 각 프로세스는 캐시된 실행을 원장(ledger)과 대조해 최대 1분에 한 번 확인하고, 그동안 `DELETE`나 스윕이 실행을 종료했다면 다시 엽니다. `DELETE`는 어느 프로세스에 도달하든 id로 실행을 종료하며, 종료 시에는 누가 닫든 그 id로 쓰인 모든 쓰기를 집계합니다. begin 행이 아직 기록되기 전에 다른 프로세스에 도달한 `DELETE`는 종료할 대상을 찾지 못하고, 이후 begin이 열린 실행으로 기록되면 스윕이 그 실행이 유휴 상태가 된 뒤 종료합니다. 짧은 재시도로는 수 초가 걸리는 begin을 감당할 수 없고, 실행을 만든 적 없는 모든 세션의 `DELETE`를 지연시키기 때문입니다. 다시 열린 실행은 원래의 `started_at`을 유지하므로, 스윕이 종료한 뒤 나중에 다시 열린 실행의 `duration_ms`는 중간의 공백을 포함해 세션의 첫 호출부터 계산됩니다. 삭제(erasure)로 행이 지워져 확인 시 행이 전혀 없으면 그 시점에 시작하는 새 실행을 시작하므로, 삭제 이전 날짜의 실행이 되살아나지 않습니다.

<a id="operations"></a>

## 작업

| 작업           | 역할   | 기능                                                                                                                                                                                                                                                                                |
| -------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run.begin`    | writer | 실행을 시작합니다: `goal`(필수), `user_request`, `expected_outcome`, `run_id`(비어 있거나 `-`이면 새로 생성), `session_id`, `role`, `model`, `metadata`. 같은 `run_id`를 다시 보내면 해당 행을 교체하며, MCP 전송 방식의 형태(`mcp-` 뒤에 16진수 40자리)인 id에는 별도의 id가 부여됩니다. |
| `run.complete` | writer | 실행을 한 번 종료합니다: `status`, `memories_used`, `recalled_keys`, `outcome`, `reflection`, `retain`. 멱등이 아니므로 두 번 완료하면 사용 이벤트가 두 번 추가됩니다.                                                                                                              |
| `run.get`      | viewer | 실시간 파생 정보와 함께 실행 하나를 가져옵니다.                                                                                                                                                                                                                                    |
| `run.list`     | viewer | 실행 목록의 한 페이지를 가져옵니다. `agent`, `session_id`, `status`, `grade`, `q`, `memory_activity`로 필터링합니다(`with`는 메모리를 활용했거나 기록한 실행만, `without`은 메모리 활동이 없는 실행만 남기며, 필터는 `limit`보다 먼저 적용됩니다). |
| `run.grade`    | writer | 종료된 실행 또는 세션의 실행들에 `pass` / `fail` 등급을 매깁니다.                                                                                                                                                                                                                  |

`run.list`는 `order_by`(시작 시각, 소요 시간, 활용한 메모리 수를 각각 오름차순 또는 내림차순으로 지정하며, 기본값은 최신순인 `started_desc`)와 `offset`(최대 100,000)으로 페이지를 나눕니다. `q`는 목표, 실행 id 또는 에이전트에 해당 텍스트가 포함된 실행을 대소문자 구분 없이 남깁니다. `total`은 필터에 일치하는 모든 실행의 수이며, 첫 페이지 이후의 가득 찬 페이지에서는 해당 페이지만으로 알 수 없으므로 null입니다.

전체 필드 목록은 [작업 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#run)에 있습니다.
`memory.outcome_run`은 키를 지정하는 대신 일괄로 등급을 매기는 호출자를 위해, 실행 중 호출된 모든 메모리에 대해 helped / hurt 결과를 하나씩 기록합니다.

<a id="example"></a>

## 예시

Python SDK의 `run.scope`는 진입 시 실행(run)을 열고 종료 시 닫습니다. 블록 안에서 `memory.ask` 답변이 인용한 키와 `used()`에 전달된 키는 `memories_used`가 됩니다. 예외가 발생하면 실행은 `failed`(결과는 부정)로 닫힌 뒤 예외가 그대로 전파됩니다. 블록 안의 쓰기에는 해당 실행의 id가 함께 기록됩니다.

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    async with agent.run.scope(goal="Answer ticket 4411: refund policy for annual plans") as run:
        answer = await agent.memory.ask("what is the refund policy for annual plans?")
        run.used("policy/refunds/annual")  # relied on beyond what the answer cited
        run.final_response = answer["answer"]

    replay = await agent.run.get(run.run_id)
    print(replay["run"]["status"], replay["memories_written"], replay["recalled"])

    # Later, once the customer confirmed the answer was right:
    await agent.run.grade("pass", run_id=run.run_id, source="support-lead")
```

HTTP를 사용할 때 수명 주기는 두 번의 호출로 이루어집니다.

```bash
curl -sX POST "$LOOM_BASE_URL/v1/run.begin" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","agent":"triage-bot","goal":"Answer ticket 4411"}'
# → {"run_id":"…","status":"active","started_at":"…","next":"…"}

curl -sX POST "$LOOM_BASE_URL/v1/run.complete" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","run_id":"…","status":"completed",
       "memories_used":["policy/refunds/annual"]}'
```

`Agent(auto_run=True)`(또는 클라이언트 환경의 `LOOM_AUTO_RUN=on`)를 사용하면 첫 번째 메모리 플레인 호출 시점에 실행을 지연 생성하고, 이후의 모든 호출을 그 실행에 연결하며, `Agent`가 닫힐 때 결과 `none`으로 실행을 완료하므로 어떤 것에도 점수를 더하거나 차감하지 않습니다. 이 옵션은 기본적으로 꺼져 있습니다. 실행 중의 쓰기는 해당 실행으로 범위가 한정되어, 다른 실행의 행을 다시 서술한 내용은 병합되지 않고 `duplicate`로 저장되기 때문입니다.

<a id="settings"></a>

## 설정

| 설정                                                          | 효과                                                                                                                  |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `LOOM_MEMORY_RUN_COMPLETE_RETAIN`                             | 호출에서 `retain` 모드를 지정하지 않았을 때 사용하는 모드(`keep`)입니다.                                                |
| `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS`                        | `retain=expire`가 만료 시점을 얼마나 먼 미래로 설정하는지를 정합니다(7일).                                              |
| `LOOM_SEARCH_TRACE_LOG_ENABLED`                               | 모든 검색을 해당 `run_id`로 기록하며, `recalled`는 이 기록에서 도출됩니다. 쿼리 텍스트를 로그에 남기므로 기본값은 꺼짐입니다. |
| `LOOM_USAGE_FOLD_ENABLED`                                     | 사용됨 / 근거 / 피해 이벤트를 랭킹으로 변환하는 `usage_fold` 워커입니다.                                                |
| `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`, `…_HALF_LIFE_DAYS` | 의존 이력이 검색에서 행의 순위를 얼마나, 얼마 동안 끌어올리는지를 정합니다.                                             |

[설정: 중복 제거와 만료](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#dedup-and-expiry),
[워커](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#workers),
[메모리 검색](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)를 참고하세요.

<a id="limits"></a>

## 제한

- `memories_used`와 `recalled_keys`는 각각 최대 500개의 키를 받으며, `goal`은 최대 2,000자까지 허용됩니다.
- `memories_used`의 키 중 활성 상태의 행으로 확인되지 않는 키는 아무런 효과가 없습니다. 응답의 `used_applied`는 확인된 키의 개수입니다.
- `run.complete`는 멱등이 아닙니다. 완료를 재시도하면 사용 이벤트가 중복 집계되므로 한 번만 완료하세요.
- SDK의 `run.scope`는 원장을 최선의 노력(best-effort)으로 기록합니다. `run.complete`가 실패해도 블록 자체의 예외가 가려지지 않으며, 완료에 실패한 실행은 `active` 상태로 남습니다.
- 실행에 `session_id`가 없거나 클라이언트가 턴 사용량을 보고하지 않으면 `turns`와 `cost_usd`는 0입니다. 이 값은 실행이 열려 있는 동안 이루어진 턴만 집계합니다.

<a id="related"></a>

## 관련 문서

- [장시간 실행 추적 및 재개](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) — `run_id`를 키로 하는 작업 메모리의 체크포인트와, 실행이 수행한 작업을 추적하는 방법.
- [검색 및 질문](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) — 강화(reinforcement)가 순위 결정에 반영되는 방식.
- [작업과 결정](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md) — `decision.outcome`은 결정을 내린 실행이 근거로 삼은 메모리에 기여도를 부여합니다.
- [메모리 아키텍처](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md) — 사용 이벤트 폴드(usage-event fold)와 그 기반이 되는 테이블.
