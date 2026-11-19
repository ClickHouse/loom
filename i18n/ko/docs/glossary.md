<!-- i18n: source=docs/glossary.md blob=8dd4bdacf4b7cf0e994f5fcf1af3353f19f26540 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md), which is authoritative.

<a id="glossary"></a>

# 용어집

Loom의 용어를 알파벳순으로 정리하며, 각 용어는 한 번만 정의합니다. 코드 식별자는 백틱으로 표시합니다.

**Abstain** — 가장 좋은 결과가 임베더에 맞게 보정된 abstain 기준 아래일 때, `memory.search`가 가장 가까운 약한 일치 항목 대신 이유 코드와 함께 `abstain=true`를 반환하는 동작입니다.
"저장된 것이 없음"과 "관련 없는 항목이 0.3점을 받음"을 구분합니다.
[검색과 순위 지정](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)을 참고하세요.

**Actor** — 누가 행을 썼는지 나타내는, 서버가 기록하는 컬럼입니다. `human:<subject>` 또는 `process:<subject>` 형태이며, 종류는 토큰에서 가져옵니다. 요청 본문에서는 읽지 않습니다.
[ID와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)를 참고하세요.

**Agent** — ID 튜플의 세 번째 요소로, 쓰고 읽는 프로그램의 이름입니다(`claude-code`, `support-bot`). 작성자를 기록할 뿐 읽기 경계는 아닙니다. 네임스페이스의 모든 구성원은 모든 에이전트의 행을 읽습니다.

**Attestation** — 행이 검증되었음을 기록하는 `verified_*` 컬럼입니다. 편집해도 이 컬럼은 그대로 남고 오래된 것으로 표시되며, 읽는 쪽은 증명된 콘텐츠 해시를 비교해 신뢰 여부를 판단합니다. [이력과 출처](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)를 참고하세요.

**Audit log** — `loom.audit_log`: 감사 대상 작업마다 콘텐츠 해시가 있는 행 하나를 두며, 이 행들은 해시로 연결된 시간별 체크포인트로 묶입니다. 행에는 콘텐츠가 아닌 해시가 담깁니다.
[감사](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)를 참고하세요.

**Belief revision** — 메모리 수명 주기를 바라보는 설계 관점입니다: Expand(`memory.set`), Revise(`memory.update_by_id`), Contract(`memory.delete`), Reinforce(`memory.pin`), Refuse(`blacklist.add`). [쓰기가 판정되는 방식](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#how-a-write-is-judged)을 참고하세요.

**Blacklist** — 일치하는 쓰기를 거부하도록 등록된 부분 문자열 또는 정규식입니다(`422 BLACKLISTED`). `sweep=true`를 지정하면 기존에 일치하는 항목도 tombstone 처리합니다.

**Chunk** — 지식 문서나 소스 파일의 일부로, 검색이 문서 전체가 아닌 해당 구절을 반환할 수 있도록 따로 저장하고 임베딩합니다.

**Code link** — 메모리가 언급하는 정의나 파일로의 링크이며, 이름 변경과 편집을 따라갑니다. 상태는 `fresh`, `moved`, `changed`, `unresolved`, `missing` 중 하나입니다. 아직 trunk에 없는 코드 이름은 `memory_code_links_pending_days`(14일)까지 기다립니다.
[코드에 연결된 메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)를 참고하세요.

**Code namespace** — git 저장소 하나를 인덱싱해 담는 `code` 종류의 네임스페이스로, 청크, 심볼, 엣지, 커밋, PR 이력을 포함합니다.
[코드 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)를 참고하세요.

**Commit coverage** — 모든 인덱스 티어가 특정 커밋을 처리했는지 여부입니다. `code.index_status(commit=...)`가 이를 알려주고, `code.coverage.watch`는 참이 되면 알립니다. [최신성](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)을 참고하세요.

**Compiler tier** — 파서 티어가 아닌 컴파일러 수준 인덱서(SCIP)에서 도출한 코드 인덱스 사실로, 타입 정보로 해석한 호출 엣지와 참조를 말합니다.

**Conflict** — 기존 메모리와 의미상 가까운 쓰기입니다(코사인 ≥ `LOOM_CONFLICT_THRESHOLD`). 두 행 모두 `conflicted`로 표시되고 서로 연결되며, 사람이나 에이전트가 해결합니다. [충돌과 거부](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)를 참고하세요.

**Consolidation** — 같은 주제에 대해 조각난 메모리를 병합하고 계보(`memory_belief_sources`, `support_type='derived_from'`)를 기록하는 백그라운드 작업입니다.

**Content hash** — 메모리 값의 `content_sha256`입니다. 정확히 반복된 쓰기를 중복 제거하고, `if_match_sha256` 사전 조건을 뒷받침하며, 감사 행의 기준이 됩니다.

**Context pack** — `context.pack`: 작업 메모리, 프로필, 관련 메모리, 절차, 결정을 한정된 크기로 묶어 세션 시작 시 한 번 불러오는 번들입니다.
[통합 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#contextpack)을 참고하세요.

**Dashboard** — 서버 루트에서 제공하는 사람용 웹 애플리케이션으로, 메모리, 활동, 소스, 검토 대기열, 작업, 저장소, 설정을 다룹니다. [대시보드](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)를 참고하세요.

**Decision** — 선택을 기록하는 일급 레코드(`decision.record`)로, 근거와 기각된 대안, 이후에는 결과(`decision.outcome`)를 담습니다.
[작업과 결정](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)을 참고하세요.

**Entity edge** — `loom.entity_edges`에 있는 두 엔티티 사이의 가중치가 있는 동시 출현 링크로, 유효 구간을 가지며 `facts.traverse`가 순회하는 그래프입니다.
[엔티티 그래프](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)를 참고하세요.

**Erasure saga** — `erasure.request`가 실행하는, 재개 가능하고 실패 시 닫히는(fail-closed) 단계 순서로, 모든 테이블(및 설정된 외부 백엔드)에서 대상자의 데이터를 제거하고 잔여 데이터 조사로 끝납니다. [보존과 삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)를 참고하세요.

**Extraction** — 대화 메시지를 LLM과 품질 필터로 후보 메모리로 바꾸는 과정입니다(`memory.set_from_messages`).
[추출](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)을 참고하세요.

**Extractor stamp** — 코드 인덱스 추출기 모듈의 코드 토큰에 대한 해시입니다. 이 값이 바뀌면 인덱싱된 모든 저장소를 다시 청크로 나누며, 주석과 docstring은 포함되지 않습니다.
[코드 인덱스 아키텍처](architecture/code-index-architecture.md)를 참고하세요.

**FINAL** — 읽는 시점에 `ReplacingMergeTree` 테이블을 키별 최신 버전으로 합치는 ClickHouse 수정자입니다. Loom은 `loom.memory`를 이 수정자로 읽습니다.
[ClickHouse](architecture/clickhouse.md)를 참고하세요.

**Fence** — `task.claim`이 임대(lease)와 함께 반환하는 토큰입니다. 이후의 `task.progress`, `task.heartbeat`, `task.complete`, `task.fail` 호출은 이 토큰을 제시해야 하므로, 임대가 만료된 요청자는 쓸 수 없습니다.

**Grant** — 주체에게 네임스페이스에 대한 역할을 부여하는 행입니다(`grants.*`).
[토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)를 참고하세요.

**Identity tuple** — 모든 행에 있는 `(org, namespace, agent, user_id, session_id)`입니다. 앞의 세 개는 필수이고, 뒤의 두 개는 기본값이 `'-'`입니다.
[ID와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)를 참고하세요.

**`ignored_args`** — 서버가 사용하지 않은 요청 필드(알 수 없거나 폐기된 필드)를 알려주는 응답 필드로, 클라이언트가 실패 없이 변경 사항을 파악할 수 있게 합니다.

**Inbox** — 네임스페이스가 소유하는, 이름이 있는 작업 대기열입니다(`task.*`, `inbox.*`).
[작업과 결정](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)을 참고하세요.

**Knowledge document** — `knowledge.add`로 추가하고 검색을 위해 청크로 나눠 임베딩한 문서입니다. 메모리와는 다르며, 믿음이 아니라 참고 자료입니다.
[지식 문서](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)를 참고하세요.

**Learning** — 사람의 결정을 기다리며 `loom.learnings_queue`에 있는 후보입니다(`learnings.list`, `learnings.review`). 여러 작성자가 뒷받침하는 사실은 공유 계층으로, 대화에서 시드된 사실은 네임스페이스 메모리 행으로, 봇이 자신에 대해 말한 규칙은 네임스페이스의 지식으로 승격됩니다.

**`log_comment`** — Loom이 호출한 op, 워커 또는 CLI 명령으로 채우는 ClickHouse 쿼리 설정으로, `system.query_log`가 모든 문장의 출처를 알 수 있게 합니다.

**`maturity`** — op가 선언하는 `stable`/`experimental` 필드입니다. core와 atomic 툴셋 어느 쪽에도 나열되지 않은 experimental op는 `all` 목록에서 빠지지만, 이름으로는 여전히 호출할 수 있습니다. [MCP 툴셋](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)을 참고하세요.

**MCP tool** — op를 Model Context Protocol로 투영한 것으로, 점을 밑줄로 바꿔 `loom_<op>`(`loom_memory_set`)로 이름 붙이며, `loom_search`, `loom_op` 같은 몇 가지 합성 도구가 추가됩니다. [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)를 참고하세요.

**Memory** — `loom.memory`의 행 하나: 키, 값, ID 튜플, 타입, 태그, 유효 기간, 출처로 구성됩니다. [메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)를 참고하세요.

**Memory key** — 네임스페이스와 에이전트 안에서 메모리를 가리키는 읽기 쉬운 식별자로, 관례상 `<scope>/<kind>/<id>` 형식입니다. 기존 키에 쓰면 그 키를 대체합니다.

**Memory type** — 메모리의 종류로 `semantic`, `episodic`, `procedural`, `preference`, `profile` 등이 있으며, 타입에 따라 임베딩 여부와 검색 방식이 정해집니다.

**Mentor hint** — Loom이 결과에 덧붙이거나 `mentor.brief`로 제공하는 짧은 사용 제안으로, 에이전트가 잘 활용하지 못하는 plane이나 op를 알려줍니다.

**Namespace** — 저장, 공유, 접근의 단위로, 조직 안의 이름 있는 작업 공간입니다. 종류는 `memory`, `code`, `bench`입니다.
[네임스페이스와 공유](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)를 참고하세요.

**Operation (op)** — 레지스트리(`server.operations.registry.ALL_OPERATIONS`)에 선언된 항목 하나로, `POST /v1/<op>`, MCP 도구, SDK를 통해 제공됩니다.
[op 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)를 참고하세요.

**Org** — 최상위 테넌트이며 ID 튜플의 첫 번째 요소입니다.

**Pinned** — `memory.pin`으로 표시한 메모리입니다. 고정된 메모리와 충돌하는 쓰기는 `409 PINNED_CONFLICT`로 거부됩니다.

**Plane** — 고유한 op와 테이블을 가진 제품 영역으로, 메모리, 지식, 대화, 그래프, 코드, 트래커, 실행과 작업, 감사가 있습니다.

**Procedure** — 재사용할 수 있는 방법 안내(`procedure.add` / `match`)로, `procedural` 타입의 메모리로 저장됩니다. [절차와 learning](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)을 참고하세요.

**Profile** — 에이전트나 사용자의 기본 컨텍스트(`profile.*`)로, `context.pack`이 가장 먼저 불러옵니다.

**Projection** — 쓰기에서 파생된 행(임베딩, 그래프 엣지, 요약)입니다.
실패한 projection은 기록되고 재시도됩니다(`ops.projection_failures.*`).

**Read scope** — 하나의 네임스페이스 안에서 읽기를 제한하는 `read_scope`로, `namespace`(모든 행, 기본값), `own_user`, `own_session` 중 하나입니다. 토큰의 클레임은 상한이며, 요청은 이를 좁힐 수는 있어도 넓힐 수는 없습니다.
[ID와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#read-scope-inside-a-namespace)를 참고하세요.

**Record** — 등록된 레코드 스키마(`memory_schema.*`)로 검증된 구조화 메모리입니다. 트래커 레코드와 혼동하지 마세요.

**ReplacingMergeTree (RMT)** — 대부분의 Loom 테이블을 뒷받침하는 ClickHouse 엔진입니다. 새 버전 행이 병합 시점이나 `FINAL` 읽기 시점에 이전 행을 대체하므로, 모든 쓰기 경로는 모든 컬럼을 그대로 이어서 기록해야 합니다. [ClickHouse](architecture/clickhouse.md)를 참고하세요.

**Residue census** — 삭제 saga의 마지막 단계로, 읽기 경로가 접근하는 모든 테이블에서 범위 안의 살아 있는 행 수를 셉니다. 잔여 데이터가 하나라도 있으면 saga는 실패합니다.

**Role** — 권한이 높아지는 순서로 `viewer`, `writer`, `admin`, `org_admin`, `platform_admin`이 있습니다. 각 op는 필요한 최소 역할을 선언합니다.

**Run** — `run.begin`과 `run.complete`로 묶이는 에이전트 실행 한 번으로, 무엇을 불러오고 썼는지 기록합니다. [실행](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)을 참고하세요.

**Session** — 대화 또는 작업 기간으로, ID 튜플의 다섯 번째 요소입니다. **session brief**(`session.brief_*`)는 나중에 재개할 수 있도록 저장한 요약입니다.

**Shared layer** — `(org, namespace, memory_key)`마다 하나의 표준 행이 있으며, 에이전트가 쓴 행에서 승격됩니다(`memory.promote` / `memory.demote`, 테이블 `loom.shared`).
다른 계층의 "비공개"는 승격되지 않았다는 뜻이지, 숨겨져 있다는 뜻이 아닙니다.

**Source class** — 서버가 기록하는 행의 출처 분류로, `asserted`, `extracted`, `observed`, `derived`, `imported`, `external` 중 하나입니다. 요청으로는 등급을 낮추는 것만 가능합니다.

**Tier** — 코드 인덱스 사실을 만든 추출기(파서 또는 컴파일러)를 가리키며, 응답에는 어느 티어가 뒷받침하는지 표시됩니다.

**Tombstone** — 메모리가 삭제되었음을 표시하는 더 높은 버전의 행입니다. `memory.restore`로 유예 기간 안에 되돌릴 수 있으며, `memory.delete`는 물리적으로 아무것도 삭제하지 않습니다.

**Toolset** — 클라이언트가 요청하는 MCP 목록(`core`, `coding`, `atomic`, `tasks`, `tracker`, `readonly`, `all`)입니다. 무엇이 나열되는지를 바꿀 뿐, 토큰이 호출할 수 있는 범위는 바꾸지 않습니다.

**Tracker** — 이슈 트래커의 레코드(이슈, PR, CI 실행)를 설정된 소스에서 읽어 중복 확인, bisect, 테스트 이력에 사용하는 plane입니다.
[트래커](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)를 참고하세요.

**Trust mode** — inbox 소유자가 게시자에 대해 설정하는 수락 정책으로, Loom은 이를 저장만 하고 스스로 바꾸지 않습니다.

**Valid time** — 메모리의 `valid_at` / `valid_to`(엔티티 엣지에서는 `valid_from` / `valid_to`)로, 사실이 현실에서 참이었던 기간입니다. Loom은 유효 시간만 기록하며 이중 시간(bi-temporal)은 아닙니다.

**Version** — `ReplacingMergeTree`가 가장 큰 값을 유지하는, 단조 증가하는 컬럼입니다. 삭제를 포함해 메모리에 대한 모든 쓰기는 새 버전이 됩니다.

**Worker** — `server/workers/registry.py`(`_WORKERS`)에 행으로 등록된, 감독되는 백그라운드 루프입니다. [워커](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)를 참고하세요.

**Working memory** — 현재 작업을 위한 스크래치패드 블록(`working_memory.*`)으로, 임베딩하거나 순위를 매기지 않으며 `context.pack`이 영구 메모리보다 먼저 불러옵니다.
[작업 메모리와 프로필](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)을 참고하세요.
