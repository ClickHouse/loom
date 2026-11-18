<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=6a5670ddd5050886dcda1b0bf590a83063c1e4aa model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Microsoft Agent Framework 컨텍스트 프로바이더

`LoomContextProvider`는 비동기 Python SDK 위에서 프레임워크의 컨텍스트 프로바이더 인터페이스를 구현합니다. 선택적 extra는 테스트를 거친 프레임워크 마이너 시리즈만 허용하며, 코어 SDK와 서버는 프레임워크를 import하지 않습니다.

실행 전에 프로바이더는 최신 사용자 텍스트를 검색하고, 일치하는 팩트를 프레임워크의 instructions에 추가합니다. 실행 후에는 새 사용자 텍스트와 에이전트 텍스트를 제한된 배치 단위로 `memory.set_from_messages`에 보냅니다. 이 훅은 프레임워크 미들웨어 루프가 있는 경우에도 턴당 한 번만 실행합니다. 프레임워크는 자체 기록을 유지하며, 이 프로바이더는 기록 프로바이더를 대체하지 않고 지속되는 팩트를 저장합니다.

<a id="invariants"></a>

## 불변 조건

사용자 ID와 프레임워크 세션 ID로 Loom 세션이 결정되므로, 서로 다른 두 사용자의 이름이 같은 세션이 추출된 청크를 공유하지 않습니다. 사용자를 생략하면 토큰이 허용하는 네임스페이스의 모든 메모리를 읽고 공유 행을 씁니다. 명시적인 사용자 ID는 앞뒤에 공백이 있으면 안 됩니다. 접근 권한은 네임스페이스 부여로 결정되며, 사용자 매개변수는 필터입니다.
서버는 행위자와 출처 정보를 제공하고, 일반적인 충돌, 삭제, 롤링 스키마 규칙을 적용합니다. 데이터베이스, 캐시, 로컬 기록은 추가되지 않습니다.
모델 응답 이후에 발생한 오류를 포함해, 오류는 프레임워크의 프로바이더 훅을 통해 전파됩니다. 동시 쓰기는 서버의 일반적인 충돌 규칙을 따릅니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`.
- `sdk/python/tests/test_agent_framework.py`: 네이티브 프로바이더와 에이전트 동작.
- `tests/integration/test_sdk_agent_framework_memory.py`: 실제 환경의 추출과 회상.

[프레임워크 설정](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
