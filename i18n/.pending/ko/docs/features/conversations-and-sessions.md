<!-- i18n: source=docs/features/conversations-and-sessions.md blob=b2079f3bf2ad65d5e3c485f2587db24b35257342 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md), which is authoritative.

<a id="conversations-and-sessions"></a>

# 대화와 세션

Loom은 세 종류의 세션 상태를 각각 별도의 플레인에 보관합니다:

| 플레인 | 보관 내용 | 임베딩 및 검색 | 기록 주체 |
| ------------------ | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| 대화 플레인 | 스레드의 원문 턴 (`loom.conversations`) | 사용자와 어시스턴트의 턴 및 압축 요약, `conversation.search`로 검색 | `conversation.append`, 플러그인의 캡처 훅, `loom ingest-transcript` |
| 세션 브리프 | 하나의 작업 흐름에 대한 압축된, 재개 가능한 상태 (`loom.session_briefs`) | 아니요 | `session.brief_save`, 압축 및 세션 종료 시점의 코딩 플러그인 |
| 메모리 | 발화 내용에서 추출한 지속적인 사실 | 예, `memory.search`로 검색 | `memory.set`, `memory.set_from_messages` |

턴을 저장해도 메모리가 생성되지는 않습니다. 대화를 사실로 변환하려면 [`memory.set_from_messages`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)로 전송하세요. 소유자는 추출된 메모리를 잃지 않고도 대화 기록을 끌 수 있으며, 브리프는 회상 시 사실과 경쟁하지 않습니다.

<a id="conversations"></a>

## 대화

<a id="recording-is-the-owners-choice"></a>

### 기록은 소유자가 선택합니다

`conversation.append`는 네임스페이스에서 대화 로깅이 켜져 있을 때만 턴을 저장합니다. 이 스위치(`loom.namespaces`의 `conversation_logging_enabled`)는 기본적으로 꺼져 있고 실패 시 닫히는(fail-closed) 방식으로 동작합니다. 행이 없는 네임스페이스는 기록되지 않습니다. 소유자는 `conversation.set_logging(enabled=true)`(admin) 또는 대시보드의 대화 페이지에 있는 **Record conversations** 스위치로 이를 켭니다.

로깅이 꺼져 있으면 append는 받아들여지지만 아무것도 보관되지 않습니다. 응답은 `stored: false, reason: "logging_disabled"`를 반환합니다. 클라이언트는 소유자의 선택을 무시할 수 없습니다. 임베딩이 실행되는 동안 턴이 삭제되면 append는 늦게 도착한 벡터를 건너뛰고 `stored: false, reason: "deleted"`로 응답합니다.

<a id="writing-turns"></a>

### 턴 쓰기

`conversation.append`는 `thread_id`, `role`(`user`, `assistant`, `system` 또는 `tool`), `content`를 받고, 선택적으로 `msg_id`, `reasoning`(별도 컬럼에 보관되는 모델의 추론), `user_id`, `run_id`, `metadata`, `tokens`를 받습니다. 스레드는 첫 append에서 생성됩니다. `conversation.create`는 `title`, `metadata`, `run_id`를 미리 지정할 때만 사용합니다.

- **`msg_id` 기준 멱등성.** `(thread_id, msg_id)`가 이미 기록된 턴은 다시 쓰지 않습니다(`reason: "duplicate"`). `msg_id`가 없으면 서버가 스레드, 역할, 내용의 해시로 하나를 만들어 내므로, 턴을 다시 기록하는 클라이언트가 대화록을 두 배로 만들지 않습니다.
- **user 및 assistant 턴은 임베딩됩니다.** 처음 2,048자까지 임베딩되며, 임베딩 행이 이 텍스트를 보관하므로 재임베딩은 벡터를 만든 텍스트에서 시작합니다. 이전 빌드가 기록한 행은 240자를 보관하며, 재임베딩은 그 텍스트로 수행됩니다. `system` 및 `tool` 턴은 압축(compaction)의 요약을 제외하고 색인되지 않은 채 대화록에 남습니다.
- **임베딩은 fail-open입니다.** 임베딩이 실패해도 턴은 저장되며, 오류는 해당 임베딩 행에 기록됩니다. 재시도는 없습니다. 이 턴은 벡터 검색에서 제외되고 부분 문자열 폴백으로만 찾을 수 있습니다.
- **high로 판단되는 턴은 임베딩되지 않습니다.** 텍스트에 high 형태(붙여넣은 API 키, `password=` 할당, 카드 번호, SSN, 명시된 PIN)가 있는 턴은 작성된 그대로 저장되지만 임베딩 행이 생기지 않고, 텍스트가 임베더로 전송되지도 않습니다. `conversation.search`는 벡터 검색이든 부분 문자열 폴백이든 이런 턴을 반환하지 않습니다. `conversation.threads`는 마지막 턴이 이런 경우 빈 발췌문으로 나열하며(high로 판단되는 제목도 빈 값이 됩니다), 발췌문에 표시되는 240자와 그 뒤 1,024자를 검사합니다. 제목 생성기는 이런 턴을 모델로 보내지 않습니다. 처음 여덟 개의 user 및 assistant 턴이 모두 high로 판단되거나 생성된 제목이 high로 판단되는 스레드는 제목 없이 남고, 제목 생성기가 다시 요청하지 않도록 메타데이터에 `title_refused`로 표시됩니다. 그런 턴이 그보다 적은 스레드는 표시 없이 일반 턴을 기다립니다. `conversation.update` 또는 `conversation.create`로 새 `metadata`를 주면 표시가 대체되며, 제목 생성기는 한 번 더 요청한 뒤 스레드에 다시 표시합니다. `conversation.get`과 `conversation.list`는 여전히 해당 스레드 안에서 그 턴을 제공합니다([민감도](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)).

<a id="saving-single-messages"></a>

### 개별 메시지 저장

채팅 클라이언트는 네임스페이스의 대화록을 기록하지 않고도 사용자가 메시지 하나를 나중에 검색하도록 저장하게 할 수 있습니다. `conversation.save_message`(writer)가 이 옵트인이고, `conversation.search_saved_messages`(viewer)가 그 검색입니다. 저장된 메시지는 대화 행이며, 사용자의 개인 컬렉션에 있는 문서가 아닙니다([공유](https://github.com/ClickHouse/loom/blob/main/docs/architecture/sharing.md#saved-messages)).

클라이언트는 모든 레플리카가 압축 중에 저장된 상태를 보존할 때만 메시지를 저장해야 합니다. [롤링 업그레이드 규칙](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md#the-rules)을 참고하세요.

이 기능은 `conversation.append` 및 `conversation.search`와 여섯 가지 면에서 다릅니다.

- **Record conversations 스위치가 적용되지 않습니다.** 스위치(또는 `conversation.set_logging`)로 기록을 껐더라도 저장은 보관되며, 저장이 기록을 켜지는 않습니다.
- **메시지에 플래그를 지정하며, 레이블을 바꾸지 않습니다.** 대화록 행과 그 임베딩 행에는 `is_saved`와 `saved_at`(사용자가 메시지를 저장한 시각)이 들어갑니다. 대화록에 이미 있는 메시지는 제자리에서 플래그가 지정되며, `run_id`와 내용은 기록된 그대로 유지됩니다.
- **검색은 사용자별입니다.** `conversation.search_saved_messages`는 지정한 하나의 네임스페이스에서 `user_id`가 소유하고 플래그가 지정된 메시지만 반환합니다. `conversation.search`는 기록된 모든 턴의 순위를 매기고 기본적으로 호출자의 모든 네임스페이스에 걸쳐 확장됩니다.
- **검색은 하이브리드입니다.** 벡터 경로는 메시지의 처음 2,048자를 임베딩하고, BM25 경로는 전체 텍스트를 읽으므로 정확한 용어(티켓 ID, 테이블 이름)나 긴 저장 답변 깊은 곳의 구문도 순위에 오릅니다. `search_mode="hybrid"`(기본값)는 두 경로를 역순위 결합(reciprocal rank)으로 합치고, `semantic`과 `lexical`은 한 경로만 실행하며, `lexical_weight`(0–1)는 BM25 경로의 가중치를 조절합니다. BM25 결과의 발췌문은 일치한 용어를 중심으로 구성됩니다. 검색 대상은 한 사용자의 저장된 메시지이므로 통계는 쿼리마다 계산됩니다.
- **한 사용자에게 고정된 토큰이 여기서 동작합니다.** 하나의 자격 증명으로 여러 사용자를 처리하는 통합은 `--read-scope own_user`(한 사용자를 지정하려면 `--read-user-id`)로 토큰을 발급합니다. 그러면 저장 메시지 op는 해당 사용자로만 동작합니다. 다른 `user_id`를 지정한 본문은 403으로 거부되고, 모든 문장에 토큰의 사용자가 적용됩니다. 최종 사용자 토큰은 `namespace: "@me"`로 이 op를 호출합니다([공유](https://github.com/ClickHouse/loom/blob/main/docs/architecture/sharing.md#saved-messages)). 대화 테이블에는 `session_id`가 없으므로 `own_session` 토큰은 거부됩니다. 다른 모든 대화 op(`append`, `get`, `list`, `search`, `threads` 등)는 스레드가 여러 사용자를 섞고 제한할 사용자 축이 없으므로 이런 토큰에서는 계속 거부됩니다.
- **소유권, 삭제, 역할은 저장할 때마다 확인됩니다.** 다른 `user_id` 아래에 저장된 메시지를 저장하면 `indexed: false, reason: "owner_mismatch"`로 응답합니다. 삭제 표시(tombstone)된 메시지는 `reason: "deleted"`로 응답하고, 저장된 system 또는 tool 턴은 임베딩되지 않으므로 `reason: "unsupported_role"`로 응답합니다. 텍스트가 high로 판단되는 메시지도 임베딩되지 않으며 `reason: "high_sensitivity"`로 응답하고, 새 메시지는 저장되지 않습니다. 이전 빌드가 저장한 메시지는 `conversation.search_saved_messages`의 벡터 또는 키워드 결과에 나타나지 않으며, `conversation.list_saved_messages`는 이를 빈 발췌문으로 나열합니다(`conversation.threads`처럼 표시되는 `conversation_search_excerpt_chars` 문자와 그 뒤 1,024자를 검사합니다). 이 확인은 이미 저장된 메시지에도 적용됩니다.

저장과 저장 취소는 append 임베딩 쓰기, 압축 정리, 스레드 삭제와 갱신 가능한 스레드 리스를 공유합니다. append와 압축은 리스를 얻기 전에 provider 작업을 마칩니다.

저장된 메시지의 소유자는 대화록 행이 결정합니다. 저장은 변경 작업 자체에서 아무도 소유하지 않았거나 호출자가 소유한 행만 차지하므로, 두 사용자가 소유자 없는 메시지 하나를 저장하면 먼저 한 쪽이 차지하고 나머지는 `owner_mismatch`로 응답받습니다. 임베딩 행은 대화록 행이 호출자의 것으로 다시 읽힌 뒤에만 플래그가 지정됩니다.

저장은 안전하게 재시도할 수 있습니다. 새 메시지를 동시에 두 번 저장해도 대화록 행은 하나만 저장되는데, 삽입에 메시지 식별자에서 파생된 중복 제거 토큰이 들어 있기 때문입니다. 각 호출은 메시지를 다시 읽고 중단되거나 동시에 실행된 쓰기가 남긴 것을 복구합니다. 누락된 임베딩 행을 쓰고, 마지막 임베딩이 실패했다면 다시 임베딩하며, 아직 플래그가 없는 행에 플래그를 지정합니다. 임베딩이 실패한 저장은 메시지를 저장된 상태로 보관하고 `indexed: false, reason: "embed_failed"`로 응답하며, 다시 저장하면 임베딩을 재시도합니다. 동시 저장 이후에 임베딩이 도착하는 대화록 append는 자신의 임베딩 행에 플래그를 지정합니다.

`conversation.list_saved_messages`(viewer)는 쿼리 없이 한 사용자의 저장된 메시지를 최근에 저장한 순으로 페이지 단위로 읽습니다(`limit` 최대 200, 불투명한 `cursor`, 선택적 `conversation_id`). `ts`는 사용자가 메시지를 저장한 시각이며 대화록의 메시지 시각과는 무관합니다. 저장 타임스탬프 없이 기록된 행은 메시지 타임스탬프를 사용합니다. 이것은 "Saved" 패널의 첫 화면이며, 사용자가 입력하면 검색이 결과를 좁힙니다. `conversation.saved_status`(viewer)는 `conversation_id` 하나와 최대 500개의 `message_ids`를 받아 그중 이 사용자가 저장한 것을 응답하므로, 클라이언트는 대화를 열 때 한 번의 호출로 모든 메시지의 저장 상태를 표시할 수 있습니다.

`conversation.unsave_message`(writer)는 저장된 메시지를 소유자의 검색에서 철회합니다. 네임스페이스가 대화록을 기록하는 경우, 소유자가 기록을 선택했으므로 턴은 플래그만 해제된 채 대화록에 남습니다. 기록하지 않는 경우에는 저장 때문에만 존재했으므로 턴이 경량 삭제로 제거됩니다(`removed: true`). 임베딩은 이전 버전이 다시 나타나지 않도록 tombstone을 유지합니다. 호출자의 사용자가 저장하지 않았거나 다른 사용자가 저장한 메시지는 `found: false`로 응답하며, 어느 쪽인지는 알려 주지 않습니다. 저장 취소는 멱등적이고, 다시 저장하면 메시지가 복원됩니다. 사용자 단위 삭제(erasure)는 그 사용자의 나머지 대화 행과 함께 저장된 메시지를 제거합니다.

<a id="reading-and-searching"></a>

### 읽기와 검색

| 작업                   | 역할   | 동작                                                                                                                       |
| ---------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------- |
| `conversation.threads` | viewer | 네임스페이스의 최근 스레드를 최신순으로 반환합니다(`window_days`, `limit`, 최대 100,000까지의 `offset`, 모든 에이전트는 `agent="*"`). |
| `conversation.get`     | viewer | 스레드 하나의 메타데이터를 반환합니다. `include_messages=true`이면 턴을 페이지 단위로 반환합니다(끝부분은 `order="desc"`).                              |
| `conversation.list`    | viewer | 스레드의 턴을 커서 페이지네이션으로 반환하며, 선택적으로 `run_id`로 거릅니다.                                                                |
| `conversation.search`  | viewer | 임베딩된 턴에 대한 벡터 검색이며, 선택적으로 스레드, 실행 또는 역할로 거릅니다.                                                                  |
| `conversation.update`  | writer | 스레드의 `title`, `metadata` 또는 `run_id`를 변경합니다.                                                                         |

`conversation.search`는 코사인 유사도로 턴의 순위를 매기고, `LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY`(0.30) 미만은 제외하며, 기본적으로 호출자의 모든 네임스페이스에 걸쳐 확장됩니다(`namespace_scope="all_mine"`). 임베더를 사용할 수 없으면 부분 문자열 일치로 폴백하여 op가 계속 응답합니다. 결과에는 턴의 처음 `LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS`자(기본 240)가 표시되며, 턴 전체는 `conversation.get` 또는 `conversation.list`로 읽습니다. 이 op는 MCP에서 숨겨져 있으며, 에이전트는 [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)의 대화 섹션을 사용합니다.

`memory.recall`은 다른 읽기입니다. 추출 과정에서 읽은 턴(`loom.memory_evidence`에 보관)을 검색하며, 이는 대화 플레인이 기록하는지와 무관하게 존재합니다. [검색과 질문](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)을 참고하세요.

<a id="compacting-and-deleting"></a>

### 압축과 삭제

- **`conversation.compact`**(writer, LLM provider 필요)는 마지막 `keep_recent`(기본 10)개 이전의 가장 오래된 턴 중 요약기의 프롬프트에 들어가는 턴을 요약하고, 요약을 임베딩된 `system` 턴 하나로 기록합니다. 이 턴의 `metadata.compacted_from`에는 요약된 `msg_id`가 나열되며, 원본 턴과 그 임베딩은 tombstone 처리됩니다. 그러면 `conversation.search`는 그 자리에서 요약을 찾습니다. 같은 스레드의 다른 에이전트가 같은 텍스트로 여전히 활성 상태로 보유한 턴은(턴 검색이 색인하는 대로) 임베딩을 유지합니다(아래 delete 참고). 새 턴이 `min_to_compact`(기본 4)개 미만인 범위는 그대로 둡니다. 이전 요약은 `min_to_compact`와 `messages_summarized` 어느 쪽에도 포함되지 않습니다. 범위 시작 부분의 요약은 별도 허용량으로 프롬프트에 합쳐지며, 남은 허용량에 들어가지 않는 요약은 활성 상태로 남습니다. 실패한 실행이 남긴 요약(해당 턴이 모두 여전히 활성 상태이며 범위 안에 있고 첫 턴의 `ts`에 위치한 것)은 읽지 않고 대체됩니다. 범위 더 안쪽의 요약은 다른 턴처럼 프롬프트 예산을 쓰며 그 턴과 함께 대체됩니다. high로 판단되는 턴은 범위에서 제외됩니다. 활성 상태로 남고 요약기로 전송되지 않으므로, 범위의 첫 턴에 놓이는 요약이 스레드에서 그 턴보다 앞에 올 수 있습니다. `conversation.save_message`로 저장된 턴도 요약되거나 tombstone 처리되지 않으며, 요약과 최근 턴 사이에 원문 그대로 남습니다. 임베딩할 수 없는 요약은 아무것도 압축하지 않으며(`reason: "embed-failed"`), 어차피 high로 판단되는 요약도 임베딩되지 않으므로 마찬가지입니다(`reason: "summary-high-sensitivity"`). `embed-failed`와 달리 이 거부는 재시도할 가치가 없습니다. 턴이 활성 상태인 동안 범위는 그대로이므로, 재시도하면 같은 요약기 호출 비용을 내고 같은 요약을 얻을 가능성이 큽니다. 저장된 턴이나 high로 판단되는 턴만 있는 범위는 `reason: "nothing-to-compact"`로 응답합니다.
- **`conversation.delete`**(admin)는 한 스레드의 턴, 스레드 행, 임베딩을 tombstone 처리하며, `confirm_thread_id`는 `thread_id`와 같아야 합니다. 같은 스레드에서 다른 에이전트도 기록한 턴은, 그 에이전트가 검색이 색인하는 같은 텍스트(user 또는 assistant 턴, 또는 요약)의 활성 사본을 보유하는 동안 임베딩을 유지합니다. 두 사본이 하나의 임베딩 행을 공유하기 때문이며, 행은 그 사본의 에이전트, 역할, 실행, 사용자를 이어받아 삭제한 에이전트의 검색에서는 사라집니다(압축도 동일하게 동작합니다). 정확히 240자인 행(이전 빌드가 자른 것이거나 그 길이의 턴)은 처음 2,048자가 해당 에이전트의 턴과 일치하는 사본이 있을 때만 유지되며, 대조할 메시지 행이 남아 있지 않으면 tombstone 처리됩니다. delete는 또한 해당 턴에 대해 자기 에이전트가 더 이상 활성 메시지를 보유하지 않는 스레드의 행도 마무리합니다. 삭제했거나 압축한 것(이전 빌드가 일부를 남겼습니다), 또는 보존 TTL이 만료시킨 것이 그 대상입니다. 개인이나 네임스페이스의 데이터를 모든 곳에서 지우려면 [`erasure.request`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)를 사용하세요.

tombstone 처리된 턴은 `erasure.export`를 제외한 모든 읽기에서 걸러지며, 이 op는 `include_tombstoned: false`로 요청하지 않는 한 이를 반환합니다. 운영자가 테이블 TTL(`LOOM_RETENTION_CONVERSATIONS_DAYS`, 0 = 영구 보관)을 설정한 경우에만 디스크에서 제거됩니다. 이 TTL은 `loom.conversations`에만 적용됩니다. 텍스트를 최대 2,048자까지 보관하는 턴의 임베딩 행에는 TTL이 없습니다. 메시지가 만료되고 나면, 해당 스레드의 전체 스레드 삭제가 이를 턴의 활성 사본에 넘기거나 tombstone 처리합니다. 삭제 요청(erasure request)은 이를 tombstone 처리하며, `purge`를 지정한 `namespace_list` 삭제에서만 제거합니다. `erasure.export`는 임베딩 행을 반환하지 않으므로, TTL이 만료시킨 턴의 발췌문은 대상자와 함께 지워지지만 그들의 내보내기에는 없습니다.

<a id="thread-titles"></a>

### 스레드 제목

`conversation_titler` 워커는 제목 없는 스레드(`conversation.create` 없이 append로 만들어진 스레드)에 LLM이 작성한 짧은 제목을 붙이며, 대시보드의 대화 페이지가 이 제목을 앞세웁니다. 대화를 기록하는 네임스페이스에서, 최근 `LOOM_CONVERSATION_TITLER_WINDOW_HOURS`(168)시간 안에 활동했고 턴이 `LOOM_CONVERSATION_TITLER_MIN_TURNS`(4)개 이상인 스레드를 대상으로 하며, 한 번에 최대 `LOOM_CONVERSATION_TITLER_MAX_THREADS_PER_PASS`(10)개를 처리합니다. 기본적으로 켜져 있으며(`LOOM_CONVERSATION_TITLER_ENABLED`), 가짜 LLM에서는 아무 동작도 하지 않습니다.

<a id="capturing-sessions-from-clients"></a>

## 클라이언트에서 세션 캡처하기

| 클라이언트             | 턴이 Loom에 전달되는 방식                                                                                                                                                                                                                                                    |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code 플러그인 | Stop 훅(`hooks/loom-conversation.mjs`)이 세션 트랜스크립트에서 새 턴을 하나씩 추가하며, `msg_id`는 트랜스크립트 uuid입니다. `LOOM_CAPTURE_CONVERSATIONS=false`로 끌 수 있습니다. 네임스페이스의 기록 판정을 캐시해 두었다가 기록이 꺼져 있는 동안에는 추가를 건너뜁니다. |
| Codex 플러그인       | 동일한 캡처 방식입니다. [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)를 참고하세요.                                                                                                                                                                                                                 |
| Cursor             | Cursor의 대화 종료 후 훅이 실행하는 `integrations/cursor/cursor-session-hook.sh`가 트랜스크립트를 `loom ingest-transcript`에 넘깁니다.                                                                                                                                |
| 기타 에이전트       | 내보낸 트랜스크립트에 `integrations/generic/ingest.sh`를 사용합니다.                                                                                                                                                                                                            |
| 모든 MCP 클라이언트     | 기본 `core` 목록에는 `conversation.append`가 없으며, 모델이 턴을 기록하도록 유도하는 것도 없습니다. 트랜스크립트 훅으로 캡처하거나 `loom_conversation_append`를 나열하는 `?toolset=all`을 마운트하세요.                                                     |
| 직접 작성한 코드      | `conversation.append` 또는 아래의 Python SDK `Session`을 사용합니다.                                                                                                                                                                                                            |

`loom ingest-transcript --namespace NS --file session.md`는 `# user` / `# assistant` 섹션이
번갈아 나오는 Markdown 파일 또는 `{role, content}` 객체의 JSON 배열을 읽어
각 턴을 추가합니다(`--dry-run`은 턴 수만 세고, `--thread-id`는 고정 id를 지정합니다).

플러그인은 세션 종료 시 세션의 턴을 `memory.set_from_messages(defer=true)`에도 넘기므로,
트랜스크립트 기록 여부와 관계없이 사실이 추출됩니다. 또한 `observe.record` /
`observe.crystallize`를 통해 도구 동작을 에피소드 관찰로 기록합니다
([추출](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) 참고). 훅별 연결 방법은
[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)에 있습니다.

<a id="session-briefs"></a>

## 세션 브리프

세션 브리프는 하나의 작업 흐름에서 재개할 수 있는 상태입니다. 목표, 상태, 결정, 막다른 길, 완료한 일, 미해결 질문, 다음 단계, 주요 파일, 제약 사항, 포인터로 구성됩니다. 컴팩션 이후, 다음 날, 다른 클라이언트, 또는 팀원이 읽습니다. 임베딩되지 않으며 `memory.search`가 반환하지도 않습니다. 네임스페이스를 읽을 수 있는 에이전트라면 누구든 이어서 작업할 수 있습니다.

| 작업                   | 역할   | 동작                                                                                                                   |
| ---------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------- |
| `session.brief_save`   | writer | `events`(마지막 저장 이후에 일어난 일), 에이전트가 작성한 `sections`, 또는 둘 모두로 브리프를 수정(또는 새로 시작)합니다. |
| `session.brief_get`    | viewer | `brief_key`로 브리프를 불러오고, 없으면 세션이 가장 최근에 쓴 브리프(`from_session`), 그것도 없으면 `project`의 가장 최근 브리프를 불러옵니다. |
| `session.brief_list`   | viewer | 네임스페이스의 브리프를 최신순으로 한 줄씩 보여 줍니다(`mine_only`는 호출자의 브리프만). |
| `session.brief_delete` | writer | 작성자가 잊어 달라고 요청하면 브리프를 삭제합니다. |

**주소 지정.** `brief_key`가 비어 있으면 `auto/<project>/<branch>`가 되므로, 같은 작업 흐름의 모든 세션이 같은 브리프를 수정합니다. 둘 다 알 수 없으면 `session/<session_id>`가 됩니다. 키는 `^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$` 패턴과 일치해야 합니다. 한 사용자(또는 세션)로 범위가 제한된 토큰은 브리프를 `~<user>~<key>`(또는 `~<user>~<session>~<key>`)로 저장하고 `<key>`로 보여 줍니다. 네임스페이스 전체 범위의 토큰은 이 접두사가 붙은 상태로 목록을 보며, 같은 접두사로 조회하거나 삭제할 수 있습니다. 범위가 제한된 토큰은 `~`로 시작하는 키를 보낼 수 없습니다.

**수정.** `events`(종류는 `user`, `assistant`, `edit`, `command`, `error`, `read`, `search`, `tool`, `note`, `summary`이며 호출당 최대 4,000개)를 포함한 저장은 서버에서 요약됩니다. `LOOM_SESSION_BRIEF_LLM`이 켜져 있으면(기본값) `LOOM_SESSION_BRIEF_LLM_MODEL`에 지정된 모델이 `LOOM_SESSION_BRIEF_WINDOW_CHARS` 글자 단위의 윈도우마다 한 번씩 브리프를 수정하며, 저장 한 번당 최대 `LOOM_SESSION_BRIEF_MAX_WINDOWS`개의 윈도우를 처리합니다. 해당 모델의 키가 없으면 배포 환경의 LLM이 작성하고, LLM이 전혀 없으면 결정적 빌더가 목표, 상태, 파일, 오류, 포인터를 작성합니다(결정과 막다른 길은 없음). 응답의 `author`는 `llm`, `deterministic`, `model`(호출한 에이전트가 섹션을 작성함) 중 하나입니다. 운영자의 [추출 정책](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#operator-extraction-policy)이 적용되면 빌더는 실행되지 않으며, events에서 클라이언트 요약도 가져오지 않습니다. 모델 호출이 실패한 윈도우는 건너뛰고 나머지 윈도우는 계속 수정합니다. 호출할 모델이 없는 상태(설정된 모델이 없거나, `use_llm: false`이거나, 첫 윈도우를 수정하기 전에 조직의 모델 호출이 꺼진 경우)에서 events만 저장하면 아무것도 쓰지 않고 `changed: false`, `reason: "not-revised"`로 응답합니다. 일부 윈도우를 수정한 뒤에 조직의 모델 호출이 꺼지면, 이미 수정된 윈도우는 유지되고 그 이후로는 수정하지 않습니다. events만 저장했는데 모든 모델 호출이 실패했거나, 읽지 않은 스위치 때문에 보류되었거나, 파싱에 실패하면 클라이언트가 다시 보낼 수 있도록 503으로 거부됩니다. 에이전트의 `sections`도 함께 보내는 저장은 두 경우 모두 기존 브리프 위에 해당 섹션을 덮어쓰며, events에서는 아무것도 가져오지 않습니다.
상태 섹션은 수정할 때마다 교체되고, 결정, 막다른 길, 완료 항목은 누적됩니다. 모든 수정본은 `loom.session_brief_revisions`에 보관되며, `brief_get`에서 `revision=N`으로 하나를 읽을 수 있습니다. 이전 저장과 같은 세션 및 `event_cursor`를 반복하는 저장은 재시도로 간주되어 아무것도 쓰지 않습니다. `if_revision`을 쓰면 저장이 compare-and-set으로 동작합니다(불일치 시 409).

**복원은 사용자가 선택합니다.** 코딩 플러그인은 컴팩션 시점과 세션 종료 시점에 클라이언트의 모델 토큰을 쓰지 않고 브리프를 저장하며, 세션 시작 프라이머는 저장된 브리프의 이름만 알려 줍니다. 브리프는 사용자가 요청할 때 `loom-session` 스킬이나 대시보드의 대화 페이지에 있는 브리프 섹션을 통해 불러옵니다. `brief_get(full=true)`는 클라이언트 자체의 마지막 컴팩션 요약을 원문 그대로 브리프 아래에 덧붙입니다. 브리프는 스스로 로드되지 않습니다. 어떤 작업 흐름을 재개할지는 사용자만 알기 때문입니다.

<a id="example"></a>

## 예시

Python SDK의 `Session`은 이러한 op를 하나의 `(user, session)` 범위에 바인딩합니다. `append`는 턴을 대화 기록(transcript)에 쓰고, `flush_every`턴마다 `memory.set_from_messages`에도 씁니다. `resume`은 해당 범위에 대한 `context.pack`에 대화 기록의 마지막 부분을 더한 것입니다.

```python
from loom import Agent

async with Agent("dev", "local", "support-bot", base_url="http://localhost:7777") as bot:
    s = bot.session("thread-42", user_id="u1")
    resumed = await s.resume(query="resume the billing migration")
    await s.append(
        {"role": "user", "content": "We moved invoices to the new ledger."},
        {"role": "assistant", "content": "Noted; I'll update the export job."},
    )
    print(s.transcript_stored)  # False when the namespace does not record
    await s.close()  # flushes the buffered turns to extraction

    await bot.briefs.save(
        project="billing",
        branch="ledger-move",
        sections={
            "goal": "Move invoices to the new ledger",
            "next_steps": ["Update the export job"],
        },
    )
```

`close()` 시점의 추출에서 사실을 찾으려면 실제 LLM 제공자가 필요합니다. 기본 가짜 제공자를 사용하면 턴은 그대로 기록됩니다.

<a id="settings"></a>

## 설정

| 설정                                                                        | 효과                                                                                                                                                                      |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY`                                   | `conversation.search`의 코사인 하한값입니다 ([지식 → 대화](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)).                                                     |
| `LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS`                                    | 검색 결과나 저장된 메시지 목록에서 한 턴을 얼마나 보여줄지 정하며, 실시간으로 적용됩니다 ([지식 → 대화](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)).                        |
| `LOOM_CONVERSATION_TITLER_*`                                                | 제목 생성 워커의 스위치, 실행 주기, 윈도우, 패스당 상한입니다 ([지식 → 대화](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)).                                  |
| `LOOM_RETENTION_CONVERSATIONS_DAYS`, `LOOM_TIERING_CONVERSATIONS_WARM_DAYS` | 대화 기록(transcript)에 대한 선택적 TTL 및 웜 볼륨 이동 설정입니다 ([보존](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#retention), [스토리지 티어링](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#storage-tiering)). |
| `LOOM_SESSION_BRIEF_LLM`, `…_LLM_MODEL`, `…_WINDOW_CHARS`, `…_MAX_WINDOWS`  | 브리프를 요약하는 방식을 정합니다 ([컨텍스트](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#context)).                                                                                              |
| `LOOM_MCP_SESSION_RUN_IDLE_SECONDS`                                         | 호출 없이 이 시간이 지나면 MCP 세션의 암묵적 실행을 종료합니다 ([MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#mcp)).                                                           |

<a id="limits"></a>

## 제한 사항

- **"append했는데 아무것도 없습니다."** 해당 네임스페이스에서 기록이 꺼져 있어, append가 `stored: false`로 응답한 경우입니다. `conversation.set_logging`으로 기록을 켜십시오.
- **대화 기록은 마이닝되지 않습니다.** 기록된 턴은 스스로 메모리가 되지 않습니다.
- **`conversation.search`는 벡터 우선입니다.** 임베딩이 포착하지 못하는 정확한 표현은 순위가 낮게 나올 수 있습니다. `memory.recall`은 추출 단계에서 읽은 턴을 대상으로 어휘 검색과 시간 범위 읽기를 수행합니다.
- **브리프에는 크기 제한이 있습니다.** 렌더링된 브리프는 약 12,000자 이내로 유지됩니다. 각 목록 섹션에도 상한이 있어(예: 결정 20개, 막다른 길 12개, 다음 단계 8개), 오래된 항목은 밀려납니다.
- **브리프는 잠금이 아닙니다.** 같은 브랜치의 두 세션은 같은 브리프를 수정합니다. 수정이 서로 끼어들면 안 되는 경우에는 `if_revision`을 전달하십시오.

<a id="related"></a>

## 관련 문서

- [추출](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) — 대화 기록을 메모리로 변환하는 방법.
- [작업 메모리와 프로필](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md) — 플러그인도 함께 기록하는 "어디까지 했는지" 스크래치패드 블록.
- [실행](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md) — 세션의 쓰기와 관찰 내용이 합류하는 실행 원장.
- [통합 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) — `loom_search`의 대화 섹션.
- [추적과 재개](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) — 오래 실행되는 작업을 재개하는 방법.
- [메모리 아키텍처](../architecture/memory-architecture.md) — 내부에서 본 대화 테이블과 브리프 테이블.
