<!-- i18n: source=docs/architecture/openai-agents-sessions.md blob=96269df2f0037fdcb16f6a101e85bf9aafedef73 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), which is authoritative.

<a id="openai-agents-conversation-sessions"></a>

# OpenAI Agents 대화 세션

`LoomSession`은 Loom 에피소드 메모리 위에서 Python과 TypeScript의 네이티브 세션 인터페이스를 구현합니다. 기존 도구는 대화와 별개로 사실을 대화 이후까지 보존할 수 있습니다. 프레임워크 패키지는 선택적 의존성으로 유지됩니다.

각 항목은 네이티브 JSON, 타임스탬프와 무작위 값으로 이루어진 키, 최종 사용자, 그리고 사용자와 네이티브 세션 id에서 파생된 세션을 담은 `memory.set` 호출 한 번입니다. 단일 쓰기는 SDK 병합을 거치지 않으므로 동시에 반복된 항목도 각각 별도로 남습니다. 에피소드 행은 의미 추출이나 중복 병합 없이 반복된 항목을 그대로 보존합니다. Python과 TypeScript는 네이티브 항목 형식이 다르므로 서로 다른 파생 세션 접두사를 사용합니다.

읽기는 `memory.list`를 페이지 단위로 순회하고, 겹치는 키를 중복 제거하며, 누락되었거나 잘린 값은 `memory.get`으로 채웁니다. 항목은 키 순서대로 반환되며, 제한 값을 요청하면 가장 최근 항목이 유지됩니다. 서버 도착 시각으로는 쓰는 쪽에서 생성한 키의 범위를 정할 수 없으므로, 양수 제한이 있는 읽기도 전체 기록을 스캔합니다. TypeScript 어댑터는 생성 시점에 검증된 사용자를 스냅샷으로 보관합니다. 네이티브 이미지와 파일 바이트 페이로드는 저장소에서 JSON 배열로 보관되고 `Uint8Array`로 반환되며, 일반 JSON과 미디어 정보는 변하지 않습니다. 삭제에는 사용자와 파생 세션이 함께 전달됩니다. `pop_item`은 가장 최근 항목을 삭제하고, `clear_session`은 나열된 기록을 삭제합니다. 동시에 일어나는 추가나 비우기는 각각 개별 서버 작업이며, 대화 전체에 걸친 트랜잭션이 아닙니다.
항목이 채우기 전에 사라지면 읽기는 불완전한 네이티브 항목을 반환하지 않고 실패합니다. 키는 쓰는 쪽의 시계를 사용하므로, 동시에 쓰는 쪽들은 서버 시퀀스를 공유하지 않습니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `sdk/python/loom/integrations/openai_agents.py`: `LoomSession`.
- `sdk/typescript/src/integrations/openai-agents.ts`: `LoomSession`.

<a id="invariants"></a>

## 불변 조건

에이전트의 조직, 네임스페이스, 이름이 각 기록의 범위를 정합니다. 사용자를 생략하면 공유 네임스페이스 행에 쓰고, 사용자를 명시하면 해당 사용자의 기록을 선택합니다. 서버가 행위자와 출처 정보를 제공합니다. 일반 메모리 삭제, 삭제 처리(erasure), 롤링 업그레이드 규칙이 그대로 적용됩니다. 로컬 기록 데이터베이스나 스키마는 추가되지 않습니다.
