<!-- i18n: source=docs/architecture/agno-adapter.md blob=3cda341b0d2ad39a80b6546d3002b103a90de53d model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md), which is authoritative.

<a id="agno-user-memory-database"></a>

# Agno 사용자 메모리 데이터베이스

Agno 어댑터는 동기식 Loom SDK를 통해 3.2 `BaseDb` 인터페이스의 사용자 메모리 메서드를 구현합니다. 마이너 버전이 고정된 선택적 의존성은 코어 SDK와 서버에 포함되지 않습니다. `MemoryManager`는 에이전트의 세션 데이터베이스와 별개로 이 데이터베이스를 받아들입니다. 메모리와 관련 없는 기능은 프레임워크 기본 저장소를 사용하지 않고 요청을 거부합니다.

<a id="invariants"></a>

## 불변 조건

각 어댑터는 사용자와 애플리케이션 저장소를 바인딩합니다. 모든 SDK 호출에는 해당 사용자와 그로부터 파생된 세션이 포함되며, 키에는 동일한 스코프 다이제스트와 네이티브 id 다이제스트가 들어 있습니다. 명시적으로 지정한 다른 사용자는 거부됩니다. 네이티브 사용자 메모리는 에피소드 Loom 행에 JSON으로 저장되며, 서로 다른 네이티브 레코드를 의미적으로 병합하지 않고 id, 메타데이터, 리비전을 그대로 보존합니다. 액터와 소스의 출처 정보는 Loom의 일반 쓰기 경로에서 가져옵니다. 일반 메모리 삭제, 소거, 롤링 스키마 규칙이 그대로 적용됩니다.

목록 조회는 개수가 제한되고 필요한 필드만 담은 페이지를 따라가며, 겹치는 항목은 중복을 제거하고, 잘린 값은 네이티브 객체로 변환하기 전에 다시 가져와 채웁니다. 콘텐츠, 토픽, 귀속 필터와 정렬, 페이지네이션, 통계는 이처럼 사용자/저장소 범위로 한정된 스냅샷에 대해 실행됩니다. 로컬 영구 상태는 없습니다. 동시에 수정하면 서버에 마지막으로 기록된 쓰기가 유지되며, 일괄 작업은 쓰기를 순차적으로 수행하는 방식이므로 실패 시 일부만 완료될 수 있습니다. 서로 신뢰하지 않는 애플리케이션은 네임스페이스 권한 부여로 격리합니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `sdk/python/loom/integrations/agno.py`: `LoomMemoryDb`.
- `sdk/python/tests/test_agno.py`: 네이티브 메모리 관리자 동작.
- `tests/integration/test_sdk_agno_memory.py`: 실제 환경에서의 메모리 스코프와 출처 정보.
