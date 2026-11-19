<!-- i18n: source=docs/architecture/haystack-adapter.md blob=c2cfa8a3d7cfb7c046a16c39b584aa93d1ea0420 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md), which is authoritative.

<a id="haystack-document-store-and-retriever"></a>

# Haystack 문서 저장소와 리트리버

선택 사항인 Haystack 3.5 어댑터는 동기식 Loom SDK를 통해 문서 저장소 프로토콜과 네이티브 검색 컴포넌트를 구현합니다. 코어 SDK와 서버는 이 프레임워크에 의존하지 않습니다. 각 HTTP 호출은 SDK가 관리하는 클라이언트 수명을 가지며, 어댑터는 서버, 로컬 데이터베이스, 캐시, 영속적인 백그라운드 프로세스를 남기지 않습니다.

<a id="invariants"></a>

## 불변 조건

저장소는 각 네이티브 문서 id를 저장소별 접두사 아래의 해시된 소스 URI에 매핑합니다. 일치하는 태그는 목록 조회와 검색의 범위를 지정합니다. 메타데이터, 임베딩, 바이너리 필드를 포함한 네이티브 문서 전체 직렬화는 문서의 원본 파일 바이트에 저장됩니다. 청크 메타데이터에는 작은 형식 마커만 들어 있으며, 텍스트는 문서 콘텐츠로 인덱싱되고 텍스트가 없으면 id가 인덱싱됩니다. Loom의 일반적인 문서 쓰기, 출처(provenance), 네임스페이스 권한 부여, 버전 관리, 삭제 경로가 모든 쓰기와 삭제에 적용됩니다. 문서는 사용자 전용이 아니라 네임스페이스 단위로 공유됩니다.

개수 계산과 필터링은 커서 페이지를 통해 저장소 전체를 불러옵니다. 네이티브 필터 헬퍼가 비교와 불리언 논리를 적용합니다. 검색은 네임스페이스, 태그, URI 경계를 명시하여 최대 100개의 청크를 조회하고, 전체 문서를 불러온 뒤 소스 URI 기준으로 중복을 제거하며 각 문서에서 가장 높은 순위의 히트를 유지합니다. 이러한 작업에는 요청 간 스냅샷이 없습니다. 동시에 쓰기가 일어나면 Loom이 충돌을 반환할 수 있습니다. 중복 확인과 다중 문서 작업은 트랜잭션이 아닙니다.

직렬화된 연결에는 Haystack 환경 변수 Secret 참조가 포함되며, 원시 Secret 토큰은 직렬화가 거부됩니다. 명시적인 엔드포인트 URL에는 내장된 시크릿을 넣을 수 없습니다. 어댑터는 Haystack의 파이프라인 역직렬화 신뢰 정책을 그대로 유지하며, 신뢰할 모듈을 등록하거나 안전하지 않은 역직렬화를 선택하지 않습니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `sdk/python/loom/integrations/haystack.py`: `LoomDocumentStore`와 `LoomRetriever`.
- `sdk/python/tests/test_haystack.py`: 네이티브 문서 저장소 및 파이프라인 동작.
- `tests/integration/test_sdk_haystack.py`: 실제 환경에서의 문서 영속화 및 검색.
