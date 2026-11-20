<!-- i18n: source=docs/architecture/n8n-adapter.md blob=f2efbe08cacbe3e9b1bb2cb39161b71c20e2be32 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md), which is authoritative.

<a id="n8n-adapter"></a>

# n8n 어댑터

n8n 커뮤니티 패키지는 TypeScript SDK를 통해 대화 기록과 지속적인 사실을 Loom 배포 환경에 저장합니다. 이 노드들은 별도의 스토리지 서비스나 로컬 캐시를 추가하지 않습니다.

<a id="interfaces-and-storage"></a>

## 인터페이스와 스토리지

`LoomMemory.supplyData`는 프레임워크의 `BufferWindowMemory`를 반환하며, 이는 `BaseListChatMessageHistory`의 `LoomChatHistory` 구현이 뒷받침합니다. 메시지 직렬화에는 프레임워크의 저장 메시지 형식을 사용합니다. 윈도우는 모델에 전송되는 컨텍스트만 바꾸며, 이전 메시지는 이후의 읽기와 명시적인 삭제에 계속 사용할 수 있습니다.

각 메시지는 `episodic` 메모리이며, 키에 타임스탬프와 임의의 접미사가 포함됩니다. episodic 쓰기는 의미 기반 병합 없이 반복된 메시지를 그대로 보존합니다. Loom 에이전트의 조직, 네임스페이스, 에이전트 이름이 기록의 범위를 결정합니다. 나머지 식별 필드는 사용자와 워크플로/세션 식별자의 다이제스트로 채워집니다. 읽기는 `memory.list`를 페이지 단위로 순회하며, 프로젝션된 행을 필터링할 때 겹치는 페이지를 중복 제거하고, 키 기준으로 정렬하며, 잘린 값은 `memory.get`으로 가져옵니다. `clear`는 해당 항목만 삭제합니다. 여러 쓰기 주체가 동시에 작업할 때 한 턴 전체를 아우르는 트랜잭션은 없습니다.

Loom 액션 노드는 `memory.set`, `memory.search`, `knowledge.search`를 호출합니다. 이 노드의 입력으로 조직, 네임스페이스, 에이전트 이름, 선택적 사용자를 지정합니다. 서버는 토큰과 작업으로부터 출처 정보를 도출하고 네임스페이스 권한을 강제합니다.

<a id="packaging"></a>

## 패키징

이 패키지는 다른 커뮤니티 노드와의 런타임 의존성 충돌을 피하기 위해 SDK와 프레임워크 라이브러리를 번들에 포함합니다. 빌드 결과물에는 해당 라이브러리의 라이선스 파일이 포함됩니다. `n8n-workflow`는 호스트가 제공하는 피어 의존성입니다. 개발 의존성은 테스트에서 사용하는 인터페이스를 고정하며, Loom의 서버나 SDK 의존성에는 추가되지 않습니다.

이 패키지는 셀프 호스팅 n8n을 대상으로 하며, 해당 대상에 맞는 공식 node CLI 설정을 사용합니다. Cloud에서 검증되었다고 주장하지 않습니다. 릴리스 태그 워크플로는 패키지를 빌드하고 검사한 뒤 npm trusted publisher와 provenance를 사용해 게시하며, 저장소에는 레지스트리 토큰을 저장하지 않습니다.

<a id="invariants"></a>

## 불변 조건

- 어댑터는 n8n 자격 증명에 지정된 Loom URL로만 데이터를 전송합니다.
- 접근은 토큰 권한으로 승인되며, user 파라미터는 필터일 뿐 인가 수단이 아닙니다.
- 대화 삭제는 액션 노드가 생성한 지속적인 사실을 삭제하지 않습니다.
- SDK 또는 서버가 요청을 거부하면 노드가 실패하며, continue-on-fail이 설정된 경우에는 오류 항목이 됩니다.
- 어댑터는 테이블을 생성하지 않으며 Loom의 기존 삭제 경로를 사용합니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `integrations/n8n/nodes/LoomMemory/LoomChatHistory.ts`: 메시지 영속화.
- `integrations/n8n/nodes/LoomMemory/LoomMemory.node.ts`: 프레임워크 메모리 어댑터.
- `integrations/n8n/nodes/Loom/Loom.node.ts`: 지속적인 메모리 및 문서 액션.
- `.github/workflows/n8n-publish.yml`: 검사 및 trusted publishing.

[프레임워크 설정](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n)
