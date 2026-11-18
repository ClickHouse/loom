<!-- i18n: source=docs/architecture/google-adk-adapter.md blob=6c709e61102ee7413e9b4764bfd5c80f4511830e model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md), which is authoritative.

<a id="google-adk-memory-service"></a>

# Google ADK 메모리 서비스

Google ADK 어댑터는 Loom의 Python 및 TypeScript SDK를 통해 프레임워크의 메모리 서비스를 구현합니다. 프레임워크 패키지는 선택적 의존성입니다. 지식 문서는 해당 네임스페이스의 모든 독자와 공유되므로, 모든 애플리케이션은 별도의 Loom 네임스페이스를 사용합니다.

세션 쓰기는 활성 상태인 사용자 및 모델 이벤트에서 텍스트를 추출하고(도구 페이로드와 thought는 제외), 제한된 크기의 배치로 `memory.set_from_messages`를 호출합니다. 두 SDK 모두 비어 있거나, 센티널 값이거나, 공백이 덧붙은 사용자 id는 어떤 호출보다 먼저 거부합니다. 각 호출에는 최종 사용자와, 사용자 및 네이티브 세션 id에서 파생한 세션이 포함됩니다. Python은 설치된 프레임워크의 증분 이벤트, 직접 메모리, 삭제 메서드도 구현합니다. Python 삭제 id에는 키, 세션, 작성자, 정확한 버전이 담기며, 이 id로 `memory.delete_version`을 직접 호출합니다. 존재하지 않거나 다시 쓰인 버전은 아무 동작도 하지 않으며, 다른 작성자의 행은 선택되지 않습니다. 메모리 검색 히트의 추가 응답 필드 `agent`는 보관된 행의 식별 정보에서 가져오며, 보류된 페이지도 포함합니다. 작성자나 버전을 알 수 없는 히트에는 삭제 id가 없습니다. 이전 클라이언트는 추가 필드를 무시하지만, 정확한 ADK 삭제를 쓰려면 이 필드를 제공하는 서버가 필요합니다. 이로 인해 영속 컬럼이나 마이그레이션이 추가되지는 않습니다. TypeScript는 작성자 범위의 참고용 id를 반환하며, 설치된 프레임워크 인터페이스에는 삭제 메서드가 없습니다.

검색은 먼저 바인딩된 사용자에 대해 `memory.search`를 호출한 다음, 남은 결과 슬롯을 네임스페이스 범위의 `knowledge.search` 결과로 채웁니다. 지식은 비공개 사용자 메모리가 아니라 공유되는 애플리케이션 컨텍스트입니다. 두 SDK는 같은 다이제스트 인코딩을 사용하며, 프레임워크의 네이티브 타임스탬프 표현은 경계에서 변환됩니다. 서비스 인스턴스 하나는 애플리케이션 하나에 바인딩됩니다. 서로 신뢰하지 않는 애플리케이션은 네임스페이스 권한 부여(grant)로 격리합니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `sdk/python/loom/integrations/google_adk.py`: `LoomMemoryService`.
- `sdk/typescript/src/integrations/google-adk.ts`: `LoomMemoryService`.

<a id="invariants"></a>

## 불변 조건

서버는 일반적인 쓰기 경로를 통해 행위자와 소스 출처 정보를 제공합니다. 어댑터는 데이터베이스, 스키마, 프로바이더를 추가하지 않습니다. 기존의 메모리 삭제, 지식 삭제, 롤링 업그레이드 규칙이 그대로 적용됩니다. 시맨틱 추출은 사실을 수정할 수 있으므로, 이 서비스는 정확한 대화 기록 저장소가 아닙니다. 지식 결과에는 사용자 메모리 id가 없으며 메모리 서비스를 통해 삭제할 수 없습니다.
