<!-- i18n: source=docs/architecture/dify-adapter.md blob=34e577e662d3664872b0d8163d92fc018b884cd1 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md), which is authoritative.

<a id="dify-tools-adapter"></a>

# Dify 도구 어댑터

Dify 플러그인은 Python SDK를 통해 Loom을 호출하며 콘텐츠를 로컬에 저장하지 않습니다.
기본 제공 도구 세 가지는 각각 `memory.set`, `memory.search`, `knowledge.search`에 대응합니다.
선택 사항인 독립 실행형 패키지는 0.11 플러그인 SDK 마이너 시리즈와
Loom 1.x SDK를 필요로 하며, 서버와 코어 SDK는 Dify에 의존하지 않습니다.

<a id="invariants"></a>

## 불변 조건

제공자 자격 증명은 엔드포인트, 조직, 네임스페이스, 토큰, 에이전트를 묶어 지정합니다.
필수 양식 매개변수인 User ID는 모델의 인자와 별개로 최종 사용자를 지정합니다.
검색(recall)은 Loom 에이전트 전체에 걸쳐 해당 사용자의 메모리를 읽을 수 있으며, 문서는
네임스페이스 단위로 공유됩니다. 두 검색 작업 모두 `namespace_scope=this`로 고정됩니다. 서로 신뢰할 수 없는 애플리케이션은
네임스페이스를 분리하고 토큰 권한을 부여하여 제한합니다. Loom은 이 호출에도 평소와 같은
액터, 출처(provenance), 충돌 처리, 롤링 스키마, 삭제 규칙을 적용합니다.

HTTP 대상은 관리자가 제어합니다. 플러그인은 URL 형식을 확인하고
URL에 포함된 자격 증명, 쿼리, 프래그먼트를 거부합니다. 플러그인 러너가 접근할 수 있는 호스트는 배포 환경의 네트워크 정책으로 제한합니다. 요청에는 유한한 타임아웃이 있으며
시도는 한 번뿐입니다. 도구 오류에는 자격 증명이나 원격 응답의 세부 정보가 노출되지 않습니다.
플러그인은 데이터베이스, 캐시, 모델 제공자, 텔레메트리 경로를 추가로 만들지 않습니다.

Dify는 Python 스레딩을 gevent 그린렛으로 패치합니다. 각 요청은 현재 허브의 네이티브 스레드 풀 안에서
SDK 클라이언트를 생성, 호출, 종료하므로, 겹치는 클라이언트가 같은 네이티브 스레드에서 asyncio 루프를 실행하지 않습니다. 자격 증명은
해당 요청 안에서만 유지됩니다. SDK 호출은 최대 30초까지 대기하며, 풀 진입, 클라이언트 설정, 종료에는
호출 전체에 대한 데드라인 대신 각각 별도의 대기 시간이 적용됩니다.

Dify는 로컬 `.difypkg`에서 패키지를 설치합니다. 공식 CLI는 소스, 메타데이터, 런타임 요구 사항, 아이콘, 라이선스, README, 개인정보 처리 방침을 패키징합니다. 무시 파일은 테스트, 환경, 자격 증명, 빌드 상태를 제외합니다. CI는 가짜 SDK HTTP 전송 계층을 대상으로 기본 제공 도구를 호출하고, 루프백 HTTP로 겹치는 네이티브 호출을 확인하며, 기본 제공 도구 스키마를 검증하고 패키지를 빌드합니다.
라이브 테스트는 비공개 Loom URL과
토큰을 명시적으로 지정한 경우에만 실행되며, 플랫폼 실행은 이러한 SDK 테스트와 별도로 문서화되어 있습니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `integrations/dify/client.py`: 범위가 지정된 SDK 호출과 요청 실패 처리.
- `integrations/dify/provider/loom.py`: 자격 증명 검증.
- `integrations/dify/tools/`: 기본 제공 도구 구현과 스키마.
- `.github/workflows/dify-plugin.yml`: 테스트와 패키징.
