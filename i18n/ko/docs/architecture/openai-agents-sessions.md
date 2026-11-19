<!-- i18n: source=docs/architecture/openai-agents-sessions.md blob=d96f44f5824749f9ff8d8d20e70fb23370c26d8b model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), which is authoritative.

<a id="openai-agents-conversation-sessions"></a>

# OpenAI Agents 대화 세션

`LoomSession`은 Loom 에피소드 메모리 위에서 네이티브 Python 및 TypeScript 세션 인터페이스를 구현합니다. 기존 도구는 대화와 별도로 사실을 계속 보관할 수 있습니다. 프레임워크 패키지는 선택적 의존성으로 유지됩니다.

각 항목은 네이티브 JSON, 타임스탬프와 난수로 이루어진 키, 최종 사용자, 그리고 사용자와 네이티브 세션 ID에서 파생된 세션을 담은 `memory.set` 호출 한 번에 해당합니다. 단일 쓰기는 SDK 병합을 거치지 않으므로 동시에 반복되는 항목도 별개로 유지됩니다. 에피소드 행은 의미 추출이나 중복 병합 없이 반복된 항목을 그대로 보존합니다. Python과 TypeScript는 네이티브 항목 형식이 다르기 때문에 서로 다른 파생 세션 접두사를 사용합니다.

읽기는 `memory.list`를 통해 페이지 단위로 가져오고, 겹치는 키를 중복 제거하며, 누락되었거나 잘린 값은 `memory.get`으로 보충합니다. 항목은 키 순서대로 반환되며, 제한을 요청하면 가장 최근 항목이 유지됩니다. 서버 도착 시각으로는 작성자가 생성한 키의 범위를 정할 수 없으므로, 양수 제한이 있는 읽기도 전체 기록을 스캔합니다. TypeScript 어댑터는 생성 시점에 검증된 사용자의 스냅샷을 저장합니다. 네이티브 이미지 및 파일 바이트 페이로드는 저장소에서 JSON 배열로 보관되고 `Uint8Array`로 반환되며, 일반 JSON과 미디어 정보는 변경되지 않습니다. 삭제는 사용자와 파생 세션을 함께 전달합니다. `pop_item`은 최신 항목을 삭제하고, `clear_session`은 나열된 기록을 삭제합니다. 동시에 일어나는 추가나 초기화는 각각 개별 서버 작업이며, 대화 전체에 걸친 트랜잭션이 아닙니다.
보충 전에 항목이 사라지면 읽기는 불완전한 네이티브 항목을 반환하는 대신 실패합니다. 키는 작성자의 시계를 사용하므로, 동시에 쓰는 작성자들은 서버 시퀀스를 공유하지 않습니다.

두 구현 모두 생성 시점과 각 콜백에서 애플리케이션에 이미 바인딩된 보호 패밀리를 유지합니다. Python은 콜백의 컨텍스트에서 활성 상태인 기여자도 유지합니다. 클라이언트를 다시 바인딩해도 이전 기여자를 제거할 수 없습니다. 네이티브 사용자 및 세션 ID는 저장된 기록을 선택할 뿐, 보호 패밀리를 설정하지는 않습니다. 논리적인 대화 패밀리마다 세션 인스턴스를 하나씩 사용하고, 해당 대화를 복원할 때는 애플리케이션이 소유한 동일한 보호 캡슐을 다시 여세요.

네이티브 기록의 읽기와 추가는 유지된 모든 기여자 아래에서 자동 작업으로 실행됩니다. 표시된 응답은 값이 반환되기 전에 모든 기여자를 보호합니다. 표시된 첫 기록 응답이 완전한 경우에는 해당 항목이 그대로 반환될 수 있으며, 이후의 페이지 이동, 보충, 항목 쓰기는 억제됩니다. 표시된 항목 쓰기는 다음 항목 전에 배치를 중단합니다. 명시적인 pop 및 clear 작업은 보호 이후에도 계속 사용할 수 있으며, 유지된 기여자 아래에서 계속 응답을 관찰합니다. 모델이 선택한 도구는 명시적 작업의 의미를 그대로 유지합니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `sdk/python/loom/integrations/openai_agents.py`: `LoomSession`.
- `sdk/typescript/src/integrations/openai-agents.ts`: `LoomSession`.

<a id="invariants"></a>

## 불변 조건

에이전트의 조직, 네임스페이스, 이름이 각 기록의 범위를 정합니다. 사용자를 생략하면 공유 네임스페이스 행에 기록하고, 사용자를 명시하면 해당 사용자의 기록을 선택합니다. 서버가 작업자와 출처 정보를 제공합니다. 일반 메모리 삭제, 소거, 롤링 업그레이드 규칙이 그대로 적용됩니다. 로컬 기록 데이터베이스나 스키마는 추가되지 않습니다.
