<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=d7c22dbc73bf2a6dd64de2cb8471a3fafc9d76ba model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Microsoft Agent Framework 컨텍스트 프로바이더

`LoomContextProvider`는 비동기 Python SDK 위에서 프레임워크의 컨텍스트 프로바이더 인터페이스를 구현합니다. 선택적 extra는 테스트를 거친 프레임워크 마이너 시리즈만 허용하며, 코어 SDK와 서버는 프레임워크를 import하지 않습니다.

실행 전에 프로바이더는 최신 사용자 텍스트를 검색하고 일치하는 팩트를 프레임워크의 instructions에 추가합니다. 실행 후에는 새로운 사용자 및 어시스턴트 텍스트를 제한된 배치로 `memory.set_from_messages`에 보냅니다. 이 훅은 프레임워크 미들웨어 루프를 포함하여 턴마다 한 번 실행됩니다. 프레임워크는 자체 기록을 유지하며, 이 프로바이더는 기록 프로바이더를 대체하지 않고 지속되는 팩트를 저장합니다.

<a id="invariants"></a>

## 불변 조건

사용자와 프레임워크 세션 id에서 Loom 세션이 도출되므로, 서로 다른 두 사용자의 이름이 같은 세션이 추출된 청크를 공유하지 않습니다. 사용자를 생략하면 토큰이 허용하는 네임스페이스의 모든 메모리를 읽고 공유 행을 씁니다. 명시적인 사용자 id는 앞뒤에 공백을 둘 수 없습니다. 접근 권한은 네임스페이스 부여로 결정되며, 사용자 매개변수는 필터입니다.
서버는 actor 및 source 출처 정보를 제공하고, 일반적인 충돌, 삭제, 롤링 스키마 규칙을 적용합니다. 데이터베이스, 캐시, 로컬 기록은 추가되지 않습니다.
모델 응답 이후에 발생한 오류를 포함하여 오류는 프레임워크의 프로바이더 훅을 통해 전파됩니다. 동시 쓰기는 서버의 일반적인 충돌 규칙을 따릅니다.

<a id="conversation-protection"></a>

## 대화 보호

프로바이더 하나는 논리적 대화 계열 하나를 담당합니다. 생성 시점과 콜백 진입 시점에 존재하는 애플리케이션 소유의 보호 핸들을 유지하며, 여기에는 앰비언트 기여자와 이후에 바인딩되는 각 클라이언트 핸들이 포함됩니다. 네이티브 사용자 id와 세션 id는 요청을 라우팅할 뿐, 이 권한을 만들거나 대체하지 않습니다. 복원 시에는 프로바이더를 생성하기 전에 기존 캡슐을 다시 엽니다.

소유한 recall과 추출은 SDK의 자동 캡처 컨텍스트에서 실행됩니다. 트랜스포트는 자격 증명 확인 후 전송 전에 유지 중인 모든 계열을 다시 확인하고, 변환 전에 응답을 관찰하며, 표시된 응답을 내보내기 전에 모든 기여자를 영구적으로 표시합니다. 표시된 첫 번째 recall은 모델에 도달할 수 있습니다. 이후의 자동 작업은 거부되며, 각 추출 배치는 다음 배치가 시작되기 전에 확인됩니다. 모델 응답 이후의 거부는 네이티브 프로바이더 수명 주기를 통해 전파되며, 이미 나간 답변이나 도구의 효과를 되돌릴 수는 없습니다.
이 프로바이더는 capability 선언이나 복원된 호스트 검증을 추가하지 않습니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`.
- `sdk/python/tests/test_agent_framework.py`: 네이티브 프로바이더 및 에이전트 동작.
- `tests/integration/test_sdk_agent_framework_memory.py`: 실제 환경에서의 추출 및 recall.

[프레임워크 설정](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
