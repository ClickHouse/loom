<!-- i18n: source=docs/faq.md blob=d1e8cdcb41e8e749ecd46d194697f615642a3ca8 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/faq.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/faq.md), which is authoritative.

<a id="frequently-asked-questions"></a>

# 자주 묻는 질문

사람들이 가장 먼저 묻는 질문에 대한 짧은 답변입니다. 각 답변은 자세히 설명하는 페이지로 연결됩니다.

<a id="using-it"></a>

## 사용하기

<a id="what-is-the-fastest-way-to-try-loom"></a>

### Loom을 가장 빠르게 사용해 보는 방법은 무엇인가요?

이미 Loom 서버가 있다면 Claude Code 또는 Codex 플러그인을 설치하고 해당 서버를 가리키도록 설정하세요: [코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md). 노트북에서 서버를 실행하려면 `loom up`으로 몇 분 안에 서버를 띄울 수 있습니다: [빠른 시작](getting-started/quickstart.md).

<a id="how-do-i-give-claude-code-codex-or-cursor-long-term-memory"></a>

### Claude Code, Codex, Cursor에 장기 메모리를 부여하려면 어떻게 하나요?

세션 시작 시 컨텍스트와 캡처에는 Claude Code 또는 Codex 플러그인을 사용하세요. 그 외의 MCP 클라이언트는 `loom install --client <name>`으로 연결할 수 있습니다([MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) 참조). 서버는 [빠른 시작](getting-started/quickstart.md)에 따라 띄우세요. Cursor에는 MCP 항목과 `loom`, `loom-code` 스킬이 설정됩니다(세션 시작 시 컨텍스트를 위한 훅은 없으며, Cursor에서는 아직 테스트되지 않았습니다). 대화 내용 캡처는 별도의 `sessionEnd` 훅이 담당합니다. [코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)과 [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)를 참조하세요.

<a id="why-are-my-search-results-nonsense"></a>

### 검색 결과가 엉뚱하게 나오는 이유는 무엇인가요?

설치본이 가짜 임베더를 실행 중이기 때문입니다. 설치본의 `.env`(체크아웃의 것 또는 `~/.loom/.env`)에 `fake-ngram-*`이 지정되어 있으며, 이 벡터로는 검색이 의미를 갖지 못합니다. 임베더를 바꾸면 저장된 데이터가 다시 임베딩됩니다([모델과 키](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)). 릴리스된 스택을 새로 설치하면 이미 로컬 모델로 임베딩합니다. 호스팅 임베더는 표현을 바꾼 질문이 많은 경우 검색 품질이 더 좋습니다([프로바이더](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)).

<a id="why-does-memorysearch-say-abstain-instead-of-returning-something"></a>

### `memory.search`가 결과를 반환하지 않고 `abstain`이라고 답하는 이유는 무엇인가요?

사용 중인 임베더에 맞게 보정된 유사도 기준을 넘은 결과가 없었기 때문입니다. Loom은 가장 가까운 약한 일치를 돌려주는 대신 모른다고 답합니다. 이유는 응답에 포함되어 있습니다. [검색과 랭킹](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)을 참조하세요.

<a id="why-was-my-write-flagged-as-a-conflict-or-refused"></a>

### 내가 쓴 내용이 충돌로 표시되거나 거부된 이유는 무엇인가요?

기존 메모리와 의미가 가까운 쓰기는 `conflicted`로 표시되고 해당 메모리에 연결됩니다. 고정된 메모리와 가까운 쓰기는 `409 PINNED_CONFLICT`로 거부되며, 블랙리스트 패턴과 일치하는 쓰기는 `422 BLACKLISTED`를 받습니다. 충돌은 대시보드의 검토 큐 또는 `conflicts.resolve`로 해결하세요. [충돌과 거부](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)를 참조하세요.

<a id="can-i-use-loom-just-for-myself"></a>

### Loom을 혼자서만 사용할 수 있나요?

네. 로그인이 없고 네임스페이스가 하나뿐인 [로컬 스택](getting-started/quickstart.md)을 실행하고, [플러그인 또는 `loom install`](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)로 에이전트를 연결하세요. 로그인이 있는 서버에서는 각 사용자에게 `personal-<slug>` 네임스페이스가 생기며, MCP 클라이언트는 기본적으로 여기에 기록하고 권한을 부여받지 않은 다른 사람은 읽을 수 없습니다. 이렇게 하면 Claude Code에서 저장한 메모리를 Codex, Cursor, 대시보드에서도 사용할 수 있습니다.

<a id="can-two-agents-see-each-others-memories"></a>

### 두 에이전트가 서로의 메모리를 볼 수 있나요?

네임스페이스의 모든 멤버는 해당 행을 읽을 수 있습니다. 각 에이전트는 자신의 레이어에 기록하며, 관리자는 `memory.promote`로 메모리를 네임스페이스의 공유 레이어에 복사할 수 있습니다. 하나의 토큰으로 여러 네임스페이스를 읽으면서 하나의 네임스페이스에만 쓸 수도 있습니다. [ID와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)를 참조하세요.

<a id="when-should-i-use-knowledge-documents-instead-of-memories"></a>

### 메모리 대신 지식 문서를 써야 하는 경우는 언제인가요?

메모리는 수정할 수 있는 짧은 주장 하나입니다. 문서, 긴 페이지 또는 수 킬로바이트가 넘는 내용은 `knowledge.add`로 지식 영역에 넣으며, 이때 내용이 청크로 나뉘고 임베딩됩니다. [메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#what-goes-where)와 [지식 문서](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)를 참조하세요.

<a id="how-do-i-make-loom-forget-something"></a>

### Loom이 어떤 내용을 잊게 하려면 어떻게 하나요?

`memory.delete`는 메모리 하나를 삭제 표시(tombstone)하며, 유예 기간 동안은 복원할 수 있습니다. `erasure.request`는 사용자 또는 네임스페이스가 소유한 모든 것을 모든 테이블에서 제거하고, 남은 것이 없는지 검증합니다. 운영자가 보존 기간을 설정하지 않는 한 Loom은 자체 일정으로 아무것도 삭제하지 않습니다. [보존과 삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)를 참조하세요.

<a id="how-do-i-find-out-why-a-result-ranked-where-it-did"></a>

### 결과가 왜 그 순위가 되었는지 확인하려면 어떻게 하나요?

`memory.search`를 `explain=true`로 호출하세요. 각 결과의 `score_components`에는 랭킹 신호가 들어 있고, 리랭커를 거친 경우에는 순위 이동(`rerank_from`, `rerank_pos`, `rerank_delta`)이, 리랭커가 순서를 바꾼 경우에는 리랭커 이전의 자체 결합 점수(`rank_score_pre_rerank`)가 포함됩니다. `memory.search_trace`는 벡터 경로만 따로 점수화합니다. [검색과 질문](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)을 참조하세요.

<a id="can-loom-answer-questions-about-my-code"></a>

### Loom이 내 코드에 대한 질문에 답할 수 있나요?

네, 서버가 인덱싱한 저장소에 대해서 가능합니다. `code.search`, `code.symbol`, `code.callers`, `code.impact` 등을 사용할 수 있으며, 각 답변에는 어떤 커밋과 인덱스 티어를 근거로 했는지가 표시됩니다. [코드 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)와 [저장소 추가](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)를 참조하세요.

<a id="why-doesnt-my-coding-agent-use-the-code-index"></a>

### 코딩 에이전트가 코드 인덱스를 사용하지 않는 이유는 무엇인가요?

플러그인은 세션이 git 체크아웃 안에서 시작되고, 그 리모트가 읽을 수 있는 인덱싱된 저장소일 때만 인덱스를 연결합니다. 저장소의 체크아웃에서 세션을 시작하고, 대시보드의 **Repositories**(`<server>/repositories`)에 해당 저장소가 나열되어 있는지 확인하세요. 또한 [코드 인덱스가 사용되는 경우](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used)를 참조하세요.

<a id="running-it"></a>

## 실행하기

<a id="do-i-need-clickhouse-cloud"></a>

### ClickHouse Cloud가 필요한가요?

아니요. Loom은 ClickHouse OSS에서도 실행되며, 노트북 스택은 Docker로 ClickHouse OSS를 시작합니다. 단일 노드 OSS에는 복제와 특정 시점 복구(point-in-time recovery)가 없기 때문에, 프로덕션에는 Cloud를 사용하는 것을 권장합니다. [배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)를 참고하세요.

<a id="can-i-use-anthropic-or-gemini-models"></a>

### Anthropic이나 Gemini 모델을 사용할 수 있나요?

네. LLM 제공자는 OpenAI 또는 Anthropic을 사용할 수 있고, 임베딩은 OpenAI, Gemini 또는 로컬 CPU 모델에서 가져올 수 있습니다. 임베딩 모델을 변경하면 벡터 공간이 바뀌므로, 변경한 뒤에는 `loom reembed`를 실행하세요. [LLM과 임베딩](architecture/llm-architecture.md)을 참고하세요.

<a id="how-do-i-give-someone-access"></a>

### 다른 사람에게 접근 권한을 주려면 어떻게 하나요?

토큰을 발급(`loom mint-token` 또는 대시보드의 Settings → Tokens)하고 네임스페이스에 대한 역할을 부여하세요. [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)를 참고하세요.

<a id="something-is-wrong-where-do-i-start"></a>

### 문제가 생겼습니다. 어디서부터 확인해야 하나요?

서버에서는 `loom doctor`, Claude Code에서는 `/loom:loom-status`, 그리고 `/v1/health`를 확인하세요. 그다음 증상별로 구성된 [문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)을 참고하세요.

<a id="the-project"></a>

## 프로젝트

<a id="why-is-an-operation-missing-from-my-mcp-tool-list"></a>

### MCP 도구 목록에서 op가 빠져 있는 이유는 무엇인가요?

MCP 목록은 툴셋(`?toolset=` 또는 `LOOM_MCP_TOOLSET`, 기본값 `core`)에 따라 달라지며, 코드 도구는 `LOOM_MCP_CODE_TOOLS=true`일 때만 목록에 나타납니다. `all`에서는 core 목록에도 atomic 목록에도 없고 `experimental`로 표시된 op가 제외됩니다. 그래도 모든 op는 `loom_op`를 통해 이름으로 호출할 수 있으며(`readonly`에서는 읽기만 가능), HTTP와 SDK에서도 호출할 수 있습니다. [MCP 툴셋](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)을 참고하세요.

<a id="how-good-is-the-retrieval-and-how-do-you-know"></a>

### 검색 품질은 어느 정도이며, 어떻게 알 수 있나요?

Loom은 방법, 데이터셋, 정확한 명령과 함께 벤치마크 모음을 제공합니다. 커밋된 파일로 재현할 수 있는 결과만 기재합니다. [벤치마크](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)를 참고하세요.

<a id="how-is-loom-different-from-putting-a-vector-database-next-to-my-app"></a>

### Loom은 앱 옆에 벡터 데이터베이스를 두는 방식과 어떻게 다른가요?

기본 ClickHouse 권한 저장소에서는 하나의 ClickHouse 데이터베이스가 행, 벡터, 텍스트 인덱스, 그래프, 감사 및 삭제 상태를 모두 보관하므로 동기화할 것이 없습니다. [Postgres 메모리 권한 저장소](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md#why-two-stores)는 라이브 메모리와 지식 상태를 Postgres에 보관하며, 두 저장소의 동기화는 Loom이 직접 처리합니다. 각 쓰기는 저장된 내용과 대조하여 검증됩니다. [개요](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md)에서 각 속성의 비용과 Loom이 적합하지 않은 경우를 확인할 수 있습니다.
