<!-- i18n: source=docs/features/integrations/librechat.md blob=3d7289bf0f5983a2073a44b60b4de3a1579b409d model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/librechat.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/librechat.md), which is authoritative.

<a id="librechat"></a>

# 리브레챗

리브레챗 v0.8.8(MIT), [어시스턴트 툴셋](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#the-assistant-toolset)을 제공하는 Loom 서버, 그리고 사용자 한 명당 Loom `viewer` 토큰 하나가 필요합니다.

리브레챗은 네임스페이스를 조사하기 위한 선택적 채팅 클라이언트입니다. 네임스페이스에 무엇이 들어 있는지, 어떻게 사용되는지, 그리고 메모리, 문서, 코드에 대해 출처가 표시된 답변을 확인할 수 있습니다. 모델은 로그인한 사용자 본인의 토큰으로 MCP를 통해 Loom의 어시스턴트 툴을 호출합니다. 메모리, 문서, 검색, 진단 규칙에 대한 권한은 계속 Loom이 가지며, 리브레챗은 채팅만 담당합니다. 대시보드의 [Ask AI 패널](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md#ask-ai)은 모델이나 별도 저장소 없이, 시작 질문이 읽는 카드로 같은 점검 및 튜닝 질문에 답합니다.

설정, 로컬 compose 파일, 검증 하네스는 [`integrations/librechat/`](https://github.com/ClickHouse/loom/blob/main/integrations/librechat/README.md)에 있습니다. 다른 리브레챗 버전은 검증되지 않았습니다. 아래의 설정 키와 에이전트 API는 v0.8.8이 읽는 것들입니다.

<a id="how-a-call-reaches-loom"></a>

## 호출이 Loom에 도달하는 방식

```text
browser -> LibreChat (agent, the person's stored Loom token) -> POST /mcp?toolset=assistant
        -> Loom: verify the token, run the read under its org, namespaces and role
```

- **전송.** `type: streamable-http`인 `mcpServers` 항목 하나가 `/mcp?toolset=assistant`를 가리킵니다. 쿼리 문자열은 Loom까지 전달되며, 사용자의 도구 목록은 어시스턴트 목록 외에는 아무것도 포함하지 않습니다.
- **신원 위임.** 이 항목은 `customUserVars` 필드 `LOOM_TOKEN`을 선언하고 `Authorization: Bearer {{LOOM_TOKEN}}`을 전송합니다. 각 사용자는 LibreChat의 MCP 설정에 자신의 토큰을 입력합니다. LibreChat은 이 토큰을 사용자별로 저장하며 배포의 `CREDS_KEY`로 암호화하고(컬렉션 `pluginauths`), 해당 헤더로 사용자마다 별도의 MCP 연결을 엽니다. 공유 토큰은 없습니다. 토큰을 입력하지 않은 사용자에게는 LibreChat이 연결을 열지 않고(`missing_custom_user_vars`) Loom 도구도 표시하지 않습니다. 모델은 헤더를 보지도 설정하지도 못하므로, 프롬프트에 사용자 id를 적어도 아무것도 바뀌지 않습니다.
- **역할.** 토큰은 `--roles viewer`로 발급하세요. 어시스턴트 toolset은 읽기 외의 모든 op를 이미 거부하며, viewer 토큰을 쓰면 URL이 변경되더라도 같은 제한이 유지됩니다. `--ttl-days`를 짧게 설정하면 LibreChat 데이터베이스가 유출되었을 때 노출되는 범위가 제한됩니다.
- **모델이 받는 것.** Loom의 `initialize` instructions(`serverInstructions: true`일 때), 어시스턴트 도구의 이름, 설명, 스키마, 그리고 [어시스턴트 toolset](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#the-assistant-toolset)이 반환하는 각 도구의 전체 결과를 받습니다. 여기에는 토큰이 읽을 수 있는 행의 키, 버전, 콘텐츠 해시, 신원이 포함됩니다. ClickHouse 자격 증명, SQL 도구, 그 밖의 연결은 받지 않습니다.
- **원장.** Loom은 모든 호출을 사용자의 토큰 subject 아래에 consumer `loom-assistant`로 기록합니다. 따라서 `agent_events`에서 누가 채팅을 통해 무엇을 요청했는지 확인할 수 있고, `namespace.audit`은 이러한 읽기를 사용량 집계에서 제외합니다.

LibreChat v0.8.8이 함께 제공하지만 이 구성에서 사용하지 않는 옵션과 그 이유는 다음과 같습니다. `apiKey: {source: admin}`은 모든 사용자가 공유하는 하나의 키입니다. `{{LIBRECHAT_OPENID_ACCESS_TOKEN}}`은 LibreChat 자체의 로그인 토큰을 전달하는데, Loom은 양쪽이 같은 ID 공급자를 신뢰할 때만 이를 검증합니다. `{{LIBRECHAT_BODY_*}}` 헤더는 클라이언트가 제어하는 요청 본문 값을 담습니다.

<a id="tenant-boundaries"></a>

## 테넌트 경계

| 경우                                                  | 동작                                                                                                                                                                                                                                                                                                                                                                                                        |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 다른 조직의 네임스페이스에 대한 질문                  | 모델이 다른 네임스페이스만 지정하는 경우로 테스트했습니다. 조직은 토큰에서 결정되고, 호출은 `isError`와 `NAMESPACE_NOT_ACCESSIBLE`로 응답하며, 모델은 접근 권한이 없다고 보고합니다. 다른 조직의 행은 모델에 전달되지 않았습니다. 다른 조직까지 함께 지정하는 호출은 MCP에서 `isError`와 `ORG_MISMATCH`로 응답합니다. 이 경우는 실행해 보지 않았고, 직접 경로의 HTTP 대응 응답(`AUTH_INSUFFICIENT`, 403)만 확인했습니다. |
| 폐기되었거나 만료된 토큰                              | Loom은 MCP 요청에 `unauthenticated: token jti '…' is revoked`로 응답합니다. LibreChat은 연결을 재시도하고, 이후 도구 호출이 실패하며 모델이 이를 알려 줍니다.                                                                                                                                                                                                                                              |
| 저장된 토큰이 없는 사용자                             | LibreChat은 Loom 연결을 열지 않고 Loom 도구도 나열하지 않습니다(`missing_custom_user_vars`).                                                                                                                                                                                                                                                                                                                |
| 다른 LibreChat 사용자와 공유된 에이전트               | 이 구성에서는 공유가 꺼져 있어 실행해 보지 않았습니다. LibreChat은 메시지를 보내는 사용자의 `customUserVars`를 읽으므로(`packages/api/src/mcp/auth.ts`의 `getUserMCPAuthMap`), 공유된 에이전트는 해당 사용자의 토큰으로 Loom을 호출하게 됩니다.                                                                                                                                                               |
| 프롬프트가 쓰기, 더 넓은 네임스페이스 또는 토큰을 요구하는 경우 | 어시스턴트 도구 세트에는 쓰기 도구가 없고, 목록에 없는 도구는 모두 거부합니다. 네임스페이스는 토큰이 결정합니다.                                                                                                                                                                                                                                                                                            |

LibreChat 자체의 접근 제어(에이전트 및 MCP 서버 공유, 역할)는 UI에서 누가 에이전트를 볼 수 있는지를 관리합니다. 이것은 Loom 권한 부여가 아니며, 각 호출에 담긴 토큰이 Loom 권한 부여입니다.

<a id="tool-errors-streaming-and-cancellation"></a>

## 도구 오류, 스트리밍, 취소

- 거부되었거나 실패한 호출은 도구의 오류 텍스트로 모델에 돌아오며, 모델은 이를 그대로 인용합니다. LibreChat은 토큰 자체는 기록하지 않고, 토큰의 `jti`와 함께 오류 줄을 로그에 남깁니다.
- **스트리밍.** 채팅 UI는 LibreChat의 재개 가능한 생성 스트림(`/api/agents/chat/stream/:streamId`)을 통해 스트리밍합니다. 에이전트 API(`/api/agents/v1/chat/completions`)는 OpenAI 방식의 청크를 스트리밍하지만, 클라이언트가 압축을 허용하면 응답을 압축합니다. 압축된 스트림은 답변이 완료된 뒤에야 도착하므로, 증분 토큰이 필요한 클라이언트는 `Accept-Encoding: identity`를 보내야 합니다. 에이전트 API는 MCP 도구를 실행 내부에서 실행하며 이에 대한 `tool_calls` 청크를 보내지 않습니다. 스트림에는 콘텐츠와 추론 델타만 들어 있고, 모델의 각 단계에서 나온 텍스트가 단계 사이에 아무것도 없이 하나의 답변으로 이어집니다.
- **취소.** UI 경로에서는 생성이 브라우저 연결과 별개로 서버 측에서 실행됩니다. 따라서 탭을 닫아도 생성은 멈추지 않으며, 도구 호출도 계속 Loom에 도달합니다. 중지 버튼(`/api/agents/chat/abort`)을 누르면 생성이 멈춥니다. 에이전트 API에서는 연결을 닫으면 실행이 멈춥니다. 한 번의 프로브에서, 에이전트의 첫 도구 호출 전에 연결을 끊자 이후 20 s 동안 Loom 호출이 없었습니다. 이 프로브(`run --cancel-probe`)는 답변이 시작되고 `--cancel-after`초(1) 뒤에 연결을 끊고, Loom 호출을 끊기 전에 시작되었는지 후에 시작되었는지로 분류합니다. 각 결과가 무엇을 의미하는지는 `qualify.py`의 `probe_line`에 설명되어 있습니다.

<a id="what-librechat-stores"></a>

## LibreChat이 저장하는 데이터

Loom의 삭제 saga는 LibreChat의 데이터베이스까지 미치지 않습니다. Loom에서 사용자나 메모리를 삭제해도 LibreChat이 가진 사본은 모두 남습니다. LibreChat이 메모리의 두 번째 저장소가 되어서는 안 되므로, 이 구성에서는 LibreChat의 메모리 기능을 끄고 검색 인덱스를 두지 않습니다.

| 데이터                                  | 위치 (MongoDB 컬렉션 또는 볼륨)                   | 테넌트 콘텐츠 포함 여부                                                                           | 이 구성에서의 보존 기간                                                                                                                                                  | 삭제 방법                                                                                                                     |
| --------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| 대화                                    | `conversations`                                   | 제목 (제목 생성은 꺼져 있음)                                                                      | `generalChatRetention`(720 h) 이후 `expiredAt`, TTL 인덱스                                                                                                              | 사용자가 채팅 삭제, 계정 삭제, TTL                                                                                            |
| 메시지 및 모든 도구 호출의 출력         | `messages` (`content[].tool_call.output`)         | 예: 질문, 답변, 각 도구가 반환한 Loom 결과 전체                                                   | 720 h 이후 `expiredAt`, TTL 인덱스                                                                                                                                       | 위와 동일                                                                                                                     |
| 도구 호출 기록                          | `toolcalls`                                       | 도구 출력                                                                                         | `expiredAt`, TTL 인덱스                                                                                                                                                  | 계정 삭제, TTL                                                                                                                |
| 토큰 사용량                             | `transactions`                                    | 아니요: 메시지별 토큰 수                                                                          | 유지                                                                                                                                                                     | 계정 삭제                                                                                                                     |
| 사용자별 Loom 토큰                      | `pluginauths`, `CREDS_KEY`로 암호화               | 자격 증명                                                                                         | 사용자가 제거할 때까지                                                                                                                                                   | MCP 설정에서 제거, 계정 삭제, Loom에서 `loom revoke-token`으로 폐기                                                           |
| 업로드된 파일                           | `files` 및 `uploads` 볼륨                         | 예, 사용자가 업로드하는 경우                                                                      | `retentionMode: all`에서 `expiredAt`, 정리 작업이 저장된 파일을 삭제한 다음 레코드를 삭제 (`packages/data-schemas/src/schema/file.ts`, `packages/api/src/files/sweep.ts`) | 사용자가 파일 삭제, 계정 삭제, 정리 작업                                                                                      |
| 에이전트                                | `agents`                                          | 에이전트의 지침, 여기에 조직과 네임스페이스가 명시될 수 있음                                      | 삭제할 때까지                                                                                                                                                            | 사용자가 에이전트 삭제 (`DELETE /api/agents/:id`), 계정 삭제                                                                  |
| 에이전트 API 키                         | `agentapikeys`, 해시로 저장                       | 베어러 자격 증명: 사용자의 에이전트를 실행하며, 따라서 저장된 Loom 토큰으로 Loom을 읽음           | 삭제할 때까지                                                                                                                                                            | 사용자가 키 삭제 (`DELETE /api/api-keys/:id`), 계정 삭제, 새 키 발급을 막으려면 `interface.remoteAgents`를 끔                 |
| LibreChat 메모리                        | `memoryentries`                                   | 없음: `memory.disabled: true`, 쓰기 거부                                                          | 기록하지 않음                                                                                                                                                            | -                                                                                                                             |
| 메시지 검색 인덱스                      | 없음: Meilisearch를 배포하지 않음, `SEARCH=false` | -                                                                                                 | -                                                                                                                                                                        | -                                                                                                                             |
| 서버 로그                               | `logs` 볼륨 및 컨테이너 출력                      | 요청 id, 사용자 id, 토큰 `jti`가 포함된 오류. 토큰과 메시지 본문은 없음                           | 하루에 파일 하나, 14일 보관 (`packages/data-schemas/src/config/winston.ts`)                                                                                              | 파일 삭제                                                                                                                     |

- `retentionMode: all`은 임시 채팅뿐 아니라 모든 채팅에 만료 시점을 부여합니다. 이 설정은 설정 이후에 생성된 채팅에만 적용되며, 기존 행은 `expiredAt`을 그대로 유지합니다 (LibreChat의 `librechat.example.yaml`에 있는 주석 참조).
- 계정 삭제(`DELETE /api/user/delete`, `ALLOW_ACCOUNT_DELETION=false`가 아니면 허용)는 해당 사용자의 대화, 메시지, 도구 호출, 트랜잭션, 저장된 키, 파일, 에이전트, 에이전트 API 키, 메모리를 삭제합니다 (LibreChat v0.8.8의 `api/server/controllers/UserController.js`).
- **제한 사항: 이전 채팅에는 앞선 도구 호출이 반환한 내용이 남아 있습니다.** 기존 채팅에서 후속 질문을 하면 도구 출력을 포함한 이전 메시지가 모델에 다시 전달되며, 모델은 Loom을 다시 호출하지 않고 이를 바탕으로 답할 수 있습니다. 토큰을 폐기하거나 행을 삭제해도 이를 막을 수 없습니다. 사용자의 접근 권한을 폐기하거나 행을 삭제한 뒤에는 해당 사용자의 LibreChat 채팅을 삭제하거나(또는 보존 기간이 지나 만료되도록 두고) 새 채팅을 시작하십시오. 현재 토큰과 행에 대해 확인되는 것은 새 도구 호출뿐입니다. 이는 LibreChat이 매 턴마다 채팅 기록을 함께 전송하기 때문에 발생하는 동작이며, 테스트하지는 않았습니다.
- LibreChat의 MongoDB 백업은 Loom의 백업 및 번들 인벤토리에 포함되지 않습니다. 이를 백업하는 운영자는 백업에도 동일한 보존 기간을 적용해야 합니다.

<a id="qualification"></a>

## 검증

`integrations/librechat/qualification/qualify.py`는 레이블이 붙은 케이스 집합에 두 번 답합니다. 한 번은 모델을 사용해 LibreChat의 에이전트 API를 거치고, 다른 한 번은 모델 없이 HTTP로 같은 읽기를 수행하며 이때 어시스턴트 도구 세트가 강제하는 인자(`memory.search`에 `auto_touch: false`와 `explain: false`)를 사용합니다. 인벤토리, 점검, 튜닝 케이스는 대시보드의 시작 카드가 수행하는 읽기이며, 메모리 케이스의 `memory.search`는 그에 포함되지 않습니다. 이 스크립트는 두 개의 조직을 시드하고, 사람마다 토큰을 발급하고, 그중 하나를 폐기한 뒤, 각 답변을 채점합니다.

- **correct:** 답변에 금지된 텍스트가 없고, 종류별로 다음을 만족합니다. 메모리, 인벤토리, 점검, 튜닝 케이스는 레이블이 붙은 값을 포함합니다. 거부 케이스는 접근이 거부되었거나 실패했다고 말합니다. 기권 케이스는 해당 사실이 저장되어 있지 않다고 말합니다. 금지된 텍스트는 교차 테넌트 케이스에서는 다른 조직의 카나리아 값, 폐기된 토큰 케이스에서는 그 사람 본인의 답(`friday`), 그 밖의 일부 케이스에서는 오래되었거나 잘못된 값입니다. Loom의 어시스턴트 경로에서는 빌드가 제공하지도 문서화하지도 않은 op나 설정의 이름을 언급하는 어떤 종류의 답변이든(`unknown_names`) 오답입니다.
- **citation valid:** 답변이 인용한 모든 메모리 키가 그 사람의 네임스페이스에 존재하고, 키 옆에 적힌 버전이 해당 행의 현재 버전이어야 합니다. 키의 이력에 대한 질문(`cite_history`)이라면 이전 버전 중 하나여도 됩니다. Loom의 어시스턴트 경로에서 메모리 인용은 `[memory:<key>@v<version>]`입니다. 그 밖의 경로에서는 `[help:]` 인용 바깥의 키 형태 텍스트가 모두 인용으로 간주되므로, 이 경로에서는 문서의 예시 키를 그대로 인용한 도움말 답변이 잘못된 메모리 인용으로 집계됩니다. 아래의 도움말 수치는 어시스턴트 경로에서 얻은 것입니다.
- **tool calls:** 답변하는 동안 Loom이 그 사람의 subject로 원장에 기록한 `mcp` 행입니다.
- **latency and time to first token:** 처음부터 끝까지의 시간과 첫 스트리밍 델타까지의 시간입니다. 하네스 자체 에이전트(`setup`)에서 첫 델타는 답변이 아니라 모델이 첫 도구 호출 전에 쓰는 한 줄짜리 서두입니다. Loom의 에이전트(`setup-bridge`)는 서두를 쓰지 않도록 지시받습니다.

| 목표(LibreChat 경로)                                    | 값            | v0.8.8, 14개 케이스, `claude-haiku-4-5-20251001`에서 측정                 |
| ------------------------------------------------------- | ------------- | ------------------------------------------------------------------------- |
| 정답                                                    | 90% 이상      | 14개 중 14개                                                              |
| 유효한 인용                                             | 100%          | 9개 중 9개                                                                |
| 기권해야 하는 케이스 중 실제로 기권한 비율              | 100%          | 2개 중 2개, 잘못된 기권 없음                                              |
| 교차 테넌트 및 폐기 케이스가 거부되고 유출 없음         | 100%          | 2개 중 2개                                                                |
| 종단 간 지연 시간 p95                                   | 10초 이하     | 6.8초(p50 3.2초)                                                          |
| 첫 스트리밍 델타(서두) p95                              | 2초 이하      | 0.6초                                                                     |
| 질문당 도구 호출                                        | 3회 이하      | 최대 3회, 총 16회                                                         |
| 답변당 모델 비용, 모든 프롬프트 토큰을 정가로 계산      | $0.05 이하    | 평균 $0.029, 최대 $0.064(`abstain-cfo`, 검색 세 번)는 목표를 넘음         |

LibreChat 수치는 메모리, 인벤토리, 점검, 튜닝, 거부 케이스와 메모리 기권(`abstain-wifi`, `abstain-cfo`)을 합친 14개 케이스(`data-history`와 `data-connected-agents` 제외)를 한 번 실행하고, 현재의 `cases.json`에 대해 `qualify.py rescore`로 채점한 결과입니다. 직접 경로 수치는 새로 시드한 환경에서 그 경로만 따로 실행한 이후의 결과(`qualify.py run --native-only`)입니다. 아래의 문서 케이스는 별도로 실행했습니다.

직접 경로는 같은 14개 케이스에 읽기를 한 번씩만 수행하고 p50 0.08초, p95 0.24초로 모두 정확히 답했습니다. 모델은 자연어 질문과 서술형 답변을 제공하는 대신 질문마다 몇 초의 시간과 비용이 들며, 모델을 호출할 때마다 나열된 모든 도구의 스키마가 함께 전송됩니다. 어시스턴트 목록 기준으로 약 11,000 프롬프트 토큰이고, 두 번 호출하는 답변은 약 25,000 토큰입니다.

<a id="document-questions"></a>

### 문서 질문

문서 케이스는 세 번째 네임스페이스 `acme-library`에 대해 질문합니다. 이 네임스페이스에서는 담당자가 `viewer` 토큰만 가집니다. 문서는 텍스트 경로인 `knowledge.upsert`로 작성되며, 중첩된 제목이 있는 Markdown 런북, 제목이 없는 일반 텍스트 계약서, SQL 테이블 정의, TOML 파일, 그리고 두 번 작성해서 최신 리비전에는 이전 리비전에 없던 값이 들어 있는 정책 문서로 구성됩니다. 레이블에는 각 답변의 값과 그 값을 명시한 섹션이 적혀 있고, 두 질문은 어느 문서에도 없는 사실을 묻습니다.

답변은 문서 구절을 `[source_uri § Section > Subsection]` 형태로 인용합니다. 이는 `knowledge.search` 히트의 `section`에 담긴 breadcrumb입니다. 여기에 히트가 가진 `revision`(해당 구절이 작성된 리비전)을 `, revision 3`처럼 덧붙입니다. 제목이 없는 문서(계약서, SQL 파일)는 `source_uri`만으로 인용합니다. 서버에게 모든 문서는 일반 텍스트이며, 하네스는 형식을 보내지 않고 어떤 케이스도 추출을 거치지 않습니다. 텍스트 경로에서는 TOML의 `# comment` 줄이 Markdown 제목으로 취급되므로, TOML 파일의 섹션은 곧 그 주석들입니다.

- **recall@k:** 질문을 쿼리로, 케이스의 `top_k`를 사용한 직접 경로의 `knowledge.search` 히트 중에서 레이블이 붙은 `(source_uri, section)`의 순위입니다. 모델이 직접 수행한 검색은 채점하지 않습니다. 그 결과는 LibreChat 안에 남기 때문입니다.
- **citation valid:** 시드된 서버에 대해 채점기의 viewer 토큰으로, 모델 없이 확인합니다. 문서가 그 사람의 네임스페이스에 살아 있고, 명시된 섹션이 그 문서의 제목 중 하나이며(`knowledge.outline`), 명시된 리비전이 최신 리비전이고(`knowledge.get`), 인용된 텍스트(하위 섹션을 포함한 섹션 또는 문서 전체)에 레이블이 붙은 값 중 하나가 들어 있어야 합니다. 숫자가 아닌 리비전(`revision unknown`)은 리비전을 명시하지 않은 것으로 봅니다.
- **labeled section cited:** 유효한 인용이 레이블이 붙은 섹션을 정확히 명시해야 합니다. 같은 값을 담은 상위 섹션은 유효하지만 집계되지 않습니다.
- **abstention:** 답변이 해당 사실이 문서에 없다고 말합니다. `knowledge.search`는 가장 가까운 구절을 반환할 뿐 기권 신호가 없으므로, 직접 경로는 아무것도 찾지 못한 경우에만 기권합니다.

| v0.8.8, `claude-haiku-4-5-20251001`, `text-embedding-3-large`에서 측정 | LibreChat                                  | 직접 경로                |
| ---------------------------------------------------------------------- | ------------------------------------------ | ------------------------ |
| 답할 수 있는 질문 중 정답                                              | 7개 중 7개                                 | 7개 중 7개, 최상위 히트  |
| 레이블이 붙은 섹션이 1위 / 상위 5위 이내                               | -                                          | 7개 중 7개 / 7개 중 7개  |
| 유효한 문서 인용                                                       | 8개 중 8개                                 | -                        |
| 레이블이 붙은 섹션 인용                                                | 7개 중 7개                                 | 7개 중 7개(최상위 히트)  |
| 리비전을 명시한 인용                                                   | 8개 중 0개                                 | -                        |
| 답할 수 없는 질문 중 기권                                              | 2개 중 2개, 잘못된 기권 없음               | 2개 중 0개               |
| 종단 간 지연 시간 p50 / p95                                            | 4.9초 / 6.4초                              | 0.32초 / 0.48초          |
| 도구 호출                                                              | 질문 9개에 21회, 각각 최대 3회             | 질문당 읽기 한 번        |
| 모델 비용, 모든 프롬프트 토큰을 정가로 계산                            | $0.294, 답변당 평균 $0.033, 최대 $0.043    | 없음                     |

다섯 개의 문서는 작은 코퍼스이므로, 레이블이 붙은 모든 섹션이 1위라는 결과는 breadcrumb이 히트에 도달한다는 것을 보여줄 뿐 대규모에서도 검색이 유지된다는 뜻은 아닙니다. 어떤 인용도 리비전을 명시하지 않았습니다. 측정한 빌드의 히트에는 리비전이 없었고, 모델은 답할 수 없는 두 질문에서만 `knowledge.get`을 읽었습니다.

<a id="use-cases"></a>

### 사용 사례

케이스 집합에는 Loom의 사용 사례마다 최소 한 개의 질문이 있습니다. 네임스페이스에 무엇이 있고 어떻게 쓰이는지(데이터 도구), 그리고 Loom이 어떻게 동작하고 그 안에서 무언가를 어떻게 하는지(서버가 함께 제공하는 문서를 읽는 도움말 도구)에 대한 질문입니다. 각 도움말 케이스에는 답변이 명시해야 하는 값과 그 값을 담은 페이지가 레이블로 붙어 있고, 레이블을 검증하기 위한 올바른 예시 답변과 잘못된 예시 답변이 함께 있습니다. 답변이 해당 값들을 명시하고 부정하지 않으면(각 케이스의 `forbid`) 위의 알 수 없는 이름 규칙 아래에서 정답으로 채점합니다. 문서에 답이 없는 케이스는 답변이 그렇다고 말하고, 어느 문서 페이지에도 쓰이지 않은 `LOOM_` 이름을 쓰지 않아야 정답입니다. 여기에는 이름 그대로인 경우와 페이지가 자리표시자로 쓰는 계열의 구성원인 경우가 모두 포함되며, 존재하지 않는 설정을 부정하기 위해서만 언급한 답변도 오답으로 채점합니다. 레이블은 답변 텍스트에 대한 패턴이므로 양쪽으로 틀릴 수 있습니다. 레이블이 붙은 값을 명시하면서 그 값에 대해 틀린 주장을 덧붙인 답변이 통과할 수 있고, 표현이 다른 올바른 답변이 실패할 수 있습니다. 정답 점수는 답변이 페이지에 적힌 내용을 담고 있다는 뜻입니다. 도움말 인용은 그 페이지가 서버의 문서에 있고(`help.get`) 그 섹션이 서버 자체 검사가 쓰는 규칙에 따라 해당 페이지의 섹션 중 하나일 때 유효합니다. 이 케이스들은 Loom의 어시스턴트 경로(`qualify.py setup-bridge`)를 거치므로, 각 답변에는 질문자 줄과 브리지 자체의 인용 검사도 함께 포함됩니다.

| 사용 사례                         | 케이스 | 정답     | 잘못된 인용 없음    |
| --------------------------------- | ------ | -------- | ------------------- |
| U1 저장된 사실 조회               | 7      | 7개 중 7 | 7개 중 7            |
| U2 문서의 내용                    | 10     | 10개 중 9 | 10개 중 10         |
| U3 네임스페이스에 있는 것         | 4      | 4개 중 4 | 4개 중 4            |
| U4 사용량 집계와 추세             | 3      | 3개 중 3 | 3개 중 3            |
| U5 비용과 지출                    | 1      | 1개 중 1 | 1개 중 1            |
| U6 메모리 더 잘 쓰기              | 4      | 4개 중 4 | 4개 중 2            |
| U7 사실의 이력                    | 2      | 2개 중 2 | 2개 중 2            |
| U8 엔티티 그래프                  | 1      | 1개 중 1 | 1개 중 1            |
| U9 코드 인덱스                    | 1      | 1개 중 1 | 1개 중 1            |
| U10 충돌과 거부                   | 1      | 1개 중 1 | 1개 중 1            |
| U11 공유와 컬렉션                 | 1      | 1개 중 1 | 1개 중 1            |
| U12 내 역할과 변경 권한자         | 1      | 1개 중 1 | 1개 중 1            |
| U13 코딩 에이전트 연결            | 1      | 1개 중 1 | 1개 중 1            |
| U14 토큰                          | 1      | 1개 중 1 | 1개 중 1            |
| U15 모델과 키                     | 1      | 1개 중 1 | 1개 중 0            |
| U16 상태 문제                     | 1      | 1개 중 1 | 1개 중 1            |
| U17 검색이 이 결과를 반환한 이유  | 1      | 1개 중 1 | 1개 중 1            |
| U18 실행                          | 1      | 1개 중 1 | 1개 중 1            |
| U19 에이전트 간 작업              | 1      | 1개 중 1 | 1개 중 1            |
| U20 트래커                        | 1      | 1개 중 1 | 1개 중 1            |
| U21 보존과 삭제                   | 1      | 1개 중 1 | 1개 중 1            |
| U22 대화 기록에서의 추출          | 1      | 1개 중 1 | 1개 중 1            |
| U23 작업 메모리                   | 1      | 1개 중 1 | 1개 중 1            |
| U24 통합                          | 1      | 1개 중 1 | 1개 중 1            |
| U25 설정의 역할                   | 1      | 1개 중 1 | 1개 중 1            |
| U26 시작하기                      | 1      | 1개 중 1 | 1개 중 1            |
| U27 배포 운영                     | 1      | 1개 중 1 | 1개 중 1            |
| U28 거부와 기권                   | 5      | 5개 중 5 | 5개 중 5            |
| 전체                              | 56     | 56개 중 55 | 56개 중 53        |

v0.8.8에서 `claude-haiku-4-5`로 측정했으며, 서버는 Loom 자체의 모델 작업에 가짜 임베딩과 가짜 LLM을 사용했습니다. 56개 케이스를 모두 실행하는 데 모든 프롬프트 토큰을 포함해 정가 기준 약 $3이 들고, 답변당 평균은 $0.053입니다. 이 수치는 14개 케이스(메모리 세 개, 문서 아홉 개, 도움말 두 개)를 한 번의 실행에서, 나머지 42개를 전체 실행에서 가져온 것입니다. 에이전트 정의는 `integrations/librechat/agent.yaml`과 질문자의 역할에 관한 한 문장이 다르고, 전체 실행에서는 첫 단계의 검색 표현도 다릅니다. 열두 개의 답변은 메모리, 문서, 도움말 페이지를 인용하지 않습니다. 거부 두 개, 기권 세 개, SQL 분석 세 개(대신 해당 구문을 인용함), 그리고 인용할 행이 없는 집계에서 나온 개수 질문 네 개입니다. 나머지 답변은 모두 인용합니다. 틀린 경우는 다음과 같습니다.

- `doc-cache-entries`는 네임스페이스의 문서 중 하나(`config/cache.toml`)에 있는 값을 묻는데, 답변이 대신 Loom 자체 문서를 검색해서 그 값을 찾지 못합니다. 질문과 Loom의 설정이 같은 단어를 공유하기 때문입니다.
- 두 개의 추천 답변이 Loom의 작성 가이드인 `mentor.guide`를 문서 페이지로 인용하는데, 그런 페이지가 없으므로 하네스가 잘못된 인용으로 표시합니다. 이 가이드에는 인용 형식이 없습니다.
- 한 답변이 해당 페이지에 없는 섹션을 명시합니다.

이 모델에서는 답변이 실행마다 달라집니다. 사람 본인의 데이터에 대한 질문이 Loom 문서와 단어를 공유하는 경우(속도 제한, 웹훅 재시도 등)에는 가끔 문서를 근거로 답하기도 합니다. 그래서 에이전트의 지침은 첫 단계에서 메모리, 문서, 도움말 검색을 함께 수행하도록 요구합니다.

<a id="the-deployed-assistant"></a>

## 배포된 어시스턴트

Loom은 LibreChat 자체를 헤드리스로, Loom이 시드한 에이전트 하나와 함께 실행할 수도 있습니다. [어시스턴트 설계](https://github.com/ClickHouse/loom/blob/main/docs/architecture/assistant.md#deployment)가 설명하는 구성입니다. 이 LibreChat에는 아무도 로그인하지 않습니다. 각 답변의 도구는 Loom이 그 답변을 위해 발급한 토큰으로 읽으며, 이 토큰은 요청의 `parent_message_id`로 전달됩니다. 에이전트의 MCP 서버는 이 토큰을 `Authorization` 헤더로 Loom에 다시 보냅니다(`deploy/librechat/assistant.librechat.yaml`).

Compose를 사용한다면 `compose.yaml` 옆의 `.env`에 LibreChat의 시크릿, 모델 키, LibreChat를 가리키는 Loom의 주소를 추가한 다음, 스택이 이미 실행 중인 프로파일과 함께 `assistant` 프로파일을 시작합니다.

```bash
echo "LIBRECHAT_CREDS_KEY=$(openssl rand -hex 32)" >> .env
echo "LIBRECHAT_CREDS_IV=$(openssl rand -hex 16)" >> .env
echo "LIBRECHAT_JWT_SECRET=$(openssl rand -hex 32)" >> .env
echo "LIBRECHAT_JWT_REFRESH_SECRET=$(openssl rand -hex 32)" >> .env
echo "LIBRECHAT_ANTHROPIC_API_KEY=<your key>" >> .env
echo "LOOM_ASSISTANT_LIBRECHAT_URL=http://librechat:3080" >> .env
```

Anthropic 대신 같은 머신의 모델 서버로 키 없이 답변하려면 `LOOM_ASSISTANT_PROVIDER=local`로 설정하고, `LOOM_ASSISTANT_MODEL`은 도구 호출을 받는 모델로, `LOOM_ASSISTANT_ANSWER_CEILING_USD=0`으로 설정합니다. 가격이 없는 모델의 답변은 그렇지 않으면 하루 비용 한도 중 어시스턴트 몫에서 $0.10씩 차감되며, 기본값에서는 하루 다섯 번의 답변에 해당합니다. LibreChat는 `LIBRECHAT_LOCAL_LLM_BASE_URL`(기본값 `http://host.docker.internal:11434/v1`, Docker 호스트의 Ollama)로 서버에 접속합니다. 작은 모델이 포기해야 하는 것과 답변 비용은 [내 머신의 모델 서버](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#a-model-server-on-your-machine-no-api-key)를 참고하세요. 설치 프로그램의 Ollama 프리셋으로 설치했다면 이 세 가지 설정이 이미 있으므로 어시스턴트는 로컬 모델로 답변하며 `LIBRECHAT_ANTHROPIC_API_KEY`가 필요 없습니다. 대신 Claude로 답변하려면 `LOOM_ASSISTANT_PROVIDER`와 `LOOM_ASSISTANT_MODEL`을 제거하고 `LOOM_ASSISTANT_ANSWER_CEILING_USD`를 기본값으로 되돌리세요.

설치된 스택은 `COMPOSE_PROFILES`에 적힌 프로파일을 실행합니다(자체 ClickHouse를 쓰면 `bundled`, 직접 준비한 ClickHouse를 쓰면 없음). 여기에 `assistant`를 추가한 다음 시작합니다.

```bash
sed -i.bak 's/^COMPOSE_PROFILES=\(.\{1,\}\)$/COMPOSE_PROFILES=\1,assistant/; s/^COMPOSE_PROFILES=$/COMPOSE_PROFILES=assistant/' .env && rm .env.bak
docker compose up -d
```

`sed`는 `install.sh`가 쓴 형태의 줄, 즉 따옴표가 없고 뒤에 주석이 없는 줄을 전제로 읽습니다. 스택은 `docker compose --profile bundled --profile assistant down`으로 중지하세요. 설치 프로그램이 출력하는 `--profile bundled down`은 LibreChat를 실행 상태로 남기며, `bundled`를 함께 지정하면 전환 후 남아 있던 번들 ClickHouse도 중지됩니다.
설치 명령을 다시 실행해 ClickHouse를 전환하면(`--clickhouse`, `--bundled`) `COMPOSE_PROFILES`가 다시 쓰입니다. 그 뒤에 `sed`를 다시 실행하세요.

체크아웃에서는 `make dev`로 이 빌드를 실행하며, 이때 `--profile bundled`가 `COMPOSE_PROFILES`를 대신합니다. 어시스턴트 프로파일은 그 옆에 함께 지정하세요. 어시스턴트는 그 스택에서만 실행됩니다. `make server`와 `make dev-cloud`는 같은 `.env`를 읽지만 `librechat`에 접근하지 못하므로, 사용하기 전에 `LOOM_ASSISTANT_LIBRECHAT_URL`을 제거하세요. `make down`은 `--profile bundled`만 지정하므로 LibreChat는 계속 실행됩니다. `up -d --build` 자리에 `down`을 넣은 같은 명령으로 중지하세요.

```bash
LOOM_BUILD_COMMIT=$(git rev-parse --short HEAD) \
  docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml \
    --profile bundled --profile assistant up -d --build
```

부팅 시 Loom은 서비스 사용자를 등록하고, 에이전트 API 키와 `integrations/librechat/agent.yaml`의 에이전트를 만든 뒤, 이를 암호화하여 `loom.assistant_service`에 저장합니다. 이미 설정된 행이 있는 상태로 부팅하면 저장된 키로 LibreChat가 그 둘을 아직 가지고 있는지 확인합니다. 시도가 실패한 때부터 성공할 때까지 `/v1/health`에는 `assistant_service_failing`이 표시됩니다. 차트의 `assistant.enabled`는 Kubernetes에서 같은 일을 합니다.

본인 자격으로 Loom을 통해 질문합니다. 채팅 경로는 OpenAI의 채팅 요청을 받아 그 청크로 답하며, 스트리밍과 일괄 응답을 모두 지원하고, 지정한 네임스페이스에 대한 `viewer` 권한이 필요합니다.

```bash
curl -N "$LOOM_URL/v1/assistant/chat/completions" \
  -H "Authorization: Bearer $LOOM_TOKEN" -H "X-Loom-Namespace: my-namespace" \
  -d '{"stream": true, "messages": [{"role": "user", "content": "Which agent writes the most?"}]}'
```

에이전트는 MCP의 `loom_assistant_ask`(이름으로 호출하며, 어떤 툴셋에도 나열되지 않음), HTTP의 `assistant.ask`, SDK가 생성하는 `ops.assistant.ask` / `loom.assistant.ask`로 질문 하나를 합니다. 이들은 답변을 한 번에 돌려줍니다. 답변은 최대 `LOOM_ASSISTANT_ANSWER_TTL_S`(600초)까지 걸릴 수 있어 SDK의 기본 요청 타임아웃 60초보다 길고, 다시 전송되지 않습니다. 따라서 호출에 그만큼 긴 타임아웃(Python은 `timeout_s=600`, TypeScript는 `{ timeoutMs: 600_000 }`)을 지정하세요. 먼저 타임아웃되는 호출은 서버가 끝까지 처리하고 비용을 청구하는 답변을 남깁니다. 각 답변은 모델 지출과 함께 `assistant.ask`로 원장에 기록되며, 그 도구 호출에는 답변의 id가 `run_id`로 붙습니다.

검증은 같은 케이스를 Loom의 어시스턴트 경로(`POST /v1/assistant/chat/completions`)로 실행하며, 각각 해당 사용자 본인의 토큰을 사용합니다. 따라서 Loom이 각 답변의 토큰을 위임하고 시드한 에이전트를 실행합니다. `setup-bridge`는 답변의 토큰이 유효한 권한 부여에서 위임되므로 각 사용자에게 자신의 네임스페이스에 대한 `viewer`를 부여합니다. 전체 실행에는 약 $3가 들며, 대부분 `acme-handbook`의 하루 모델 비용 한도에서 나갑니다(56개 케이스 중 45개가 여기에 질문합니다). 어시스턴트는 한도의 절반까지 쓸 수 있으므로 실행하는 동안 한도를 높이세요(서버의 `LOOM_DEFAULT_DAILY_COST_CAP_USD=20` 또는 네임스페이스의 `daily_cost_cap_usd`). 그렇지 않으면 마지막 케이스들이 `RATE_LIMITED`로 거부됩니다.

```bash
Q=integrations/librechat/qualification/qualify.py
python $Q seed
python $Q setup-bridge
python $Q revoke
python $Q run --cancel-probe
```

케이스 중에는 분석 케이스 세 개(네임스페이스에 대한 `insights.query`이며, 그중 하나는 모든 조직의 개수를 요청하지만 답변은 다른 테넌트의 행을 언급하면 안 됩니다)와, 일부러 엉망으로 작성한 네임스페이스 `acme-notes`에 대한 추천 케이스 두 개(해시 이름 키, 두 번 인코딩된 JSON 값, 메모리로 저장된 6.5 KB 런북)가 있습니다. 표는 `data-history`와 `data-connected-agents`를 제외한 데이터 케이스를 측정합니다.

| v0.8.8에서 측정, 28개 케이스, `claude-haiku-4-5`, 가짜 임베더 | 배포된 에이전트에 직접 질문 |
| ----------------------------------------------------------------- | --------------------------------------------------------------- |
| 정답                                                   | 28개 중 28개, 한 번의 실행에서                                            |
| 분석 케이스 / 추천 케이스                             | 3개 중 3개 / 2개 중 2개                                             |
| 테넌트 간, 취소된 권한, 다른 조직 SQL 케이스, 유출 없음            | 3개 중 3개                                                          |
| 답변을 보류해야 하는 케이스 중 실제로 보류한 것                                     | 4개 중 4개, 잘못된 보류 없음                                |
| 유효한 메모리 인용                                            | 16개 중 13개                                                        |
| 유효한 문서 인용                                          | 9개 중 8개                                                          |
| 엔드투엔드 지연 p50 / p95                                      | 3.4 s / 10.7 s, 추천 케이스는 10.7 s와 14.5 s      |
| 질문당 도구 호출                                           | 최대 6회(추천은 감사, 멘토, 가이드를 읽음) |
| 답변당 모델 비용, 모든 프롬프트 토큰을 정가로 계산           | 평균 $0.033, 최대 $0.048                                         |
| 답변 도중 클라이언트 연결이 끊긴 뒤의 Loom 호출                  | 0                                                               |
| LibreChat의 로그나 데이터베이스에 있는 답변 토큰 또는 서비스 비밀번호 | 없음                                                            |
| LibreChat가 저장한 것                                             | `transactions`만: 메시지, 대화, 도구 호출은 없음   |

같은 케이스를 Loom의 채팅 경로로 각자의 토큰을 사용해 질문한 결과: 28개 중 25개가 정답이었고, 나머지 3개는 네임스페이스가 기본 한도 $1 중 어시스턴트 몫인 절반을 소진한 뒤 `RATE_LIMITED`로 거부되었습니다(한도를 $20로 두면 3개 모두 정답). 답변당 평균 $0.027, 최대 $0.047, p95 9.4 s입니다.

모델의 답변은 실행마다 달라지므로 28개 케이스를 한 번 실행한 결과는 표본이지 한계가 아닙니다. 유효하지 않은 메모리 인용은 답변이 줄여 쓴 해시 키 두 개와 대체안으로 제안한 키 하나이며, 유효하지 않은 문서 인용은 계약서에 없는 섹션을 가리킵니다. 가짜 임베더는 문자 겹침으로 순위를 매기므로, 이 수치는 배포된 경로와 그 토큰, 지침이 동작한다는 것을 보여 줄 뿐 검색 순위가 어떤지는 보여 주지 않습니다.
