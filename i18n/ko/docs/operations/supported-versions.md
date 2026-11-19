<!-- i18n: source=docs/operations/supported-versions.md blob=32631f7ef1ce0afc4871e89ec501bcad37732e46 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md), which is authoritative.

<a id="supported-versions"></a>

# 지원 버전

릴리스 N과 N−1은 수정을 받으며, `X.Y` 버전의 SDK는 `X.Y` 또는 `X.(Y−1)` 버전의 서버와 함께 동작합니다. Loom은 서버, 두 이미지, 웹 앱, 두 SDK, 루트 skills, Helm 차트의 `appVersion`에 하나의 버전을 사용합니다([릴리스 프로세스](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md)). 이 페이지는 코딩 에이전트 플러그인과, 무언가를 제거하는 변경을 어떻게 공지하는지도 다룹니다.

<a id="release-lines"></a>

## 릴리스 라인

| 라인 | 받는 항목                                                                                         |
| ---- | ------------------------------------------------------------------------------------------------- |
| N    | 가장 최신 마이너 릴리스(`X.Y`): 모든 수정. 기다릴 수 없는 경우 패치 릴리스(`X.Y.(Z+1)`)로 제공                |
| N−1  | 그 이전 마이너 릴리스: 보안 수정과 데이터 손실 또는 잘못된 응답에 대한 수정을 패치로 제공                 |
| N−2  | 없음: N 또는 N−1로 업그레이드하세요                                                               |

마이너 릴리스는 약 2주마다 `main`에서 만들어지고, 패치는 해당 라인의 `release/X.Y` 브랜치에서 만들어지며 수정은 먼저 `main`에 병합됩니다. 업그레이드는 [업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)를 따르며, 인접한 두 버전은 한 버전이 다른 버전을 대체하는 동안 나란히 서비스됩니다([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)).

<a id="sdks-and-servers"></a>

## SDK와 서버

SDK(PyPI의 `clickhouse-loom`, npm의 `@clickhouse/loom`)는 모든 릴리스와 함께 서버와 같은 버전으로 게시됩니다. npm의 `latest`는 가장 높은 정식 버전을 가리키고, 이전 라인에 대한 패치는 `release-X.Y` dist-tag를 사용합니다. 더 오래된 서버를 대상으로 할 때는 다음과 같습니다.

- SDK가 보낸 필드를 더 오래된 서버가 알지 못하면 요청은 처리되고 응답의 `ignored_args`에 해당 필드가 표시됩니다. 단, 상태를 변경하는 op에서 서버가 자신의 필드를 잘못 쓴 것으로 판단하는 키(비슷한 이름, 동의어, 필드 이름의 일부 단어)는 `422 UNKNOWN_ARGUMENT`로 거부되며 아무것도 기록되지 않습니다
  ([알 수 없는 필드와 제거된 필드](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)).
- 더 오래된 서버가 제공하지 않는 operation은 `404`로 응답하며, SDK는 이를 `NotFound`로 발생시킵니다(Python `loom.errors.NotFound`, TypeScript `NotFound`). 따라서 처리할 수 없는 서버에 대한 호출이 조용히 성공하는 일은 없습니다. 같은 오류가 행이 없는 경우에도 쓰이므로, 둘을 구분해야 하는 클라이언트는 시작할 때 `ops.capabilities`를 읽습니다
  ([배포 환경이 제공하는 기능 확인](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)).

메이저 릴리스(`X.0`)는 마지막 `(X−1).Y` 서버에 대한 이 규칙을 깨뜨릴 수 있으며, `X.0` 버전의 SDK에 필요한 사항은 릴리스 노트에 적혀 있습니다.

더 오래된 SDK는 서버가 그 SDK가 호출하는 기능을 계속 제공하는 한 더 새로운 서버에서도 계속 동작하며, 제거는 아래 규칙을 따릅니다.

<a id="coding-agent-plugins"></a>

## 코딩 에이전트 플러그인

Claude Code와 Codex 플러그인은 자체 버전을 가지며, 훅이 지원하는 가장 오래된 서버를 명시합니다(`lib/server-version.mjs`의 `MIN_SERVER_VERSION`). 더 오래된 서버를 대상으로 하는 세션은 이를 알리는 `loom:` 줄 하나와 함께 시작되고, 플러그인은 가능한 범위에서 계속 동작합니다([Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)). Claude Code 플러그인에는 Claude Code 2.1.139 이상이 필요하며, 이 버전은 셸 없이 훅의 `args`를 실행하는 첫 버전입니다.

<a id="removing-something"></a>

## 제거 공지

제거는 릴리스 풀 리퀘스트에서 직접 작성하여, 제거가 이루어지는 릴리스의 바로 이전 마이너 릴리스 노트의 "What changed for clients" 아래에 공지됩니다. 제거 전까지 서버는 이전 필드나 op를 계속 제공합니다. 제거 후에는 제거된 요청 필드도 여전히 허용되며 `ignored_args`에 표시되므로, 이를 보내는 클라이언트는 실패 없이 변경 사실을 알게 됩니다. 제거된 op는 `404`(`NotFound`)로 응답하며, 이는 `ops.capabilities`로 행이 없는 경우와 구분할 수 있습니다.

<a id="release-artifacts"></a>

## 릴리스 아티팩트

모든 릴리스 이미지는 실행되는 아키텍처에서 빌드되고, 수정 가능한 치명적 취약점이 있는지 스캔되며, 출처 증명(provenance)과 SBOM과 함께 게시됩니다. 각 이미지의 멀티 아키텍처 인덱스에는 릴리스 워크플로의 keyless 서명이 포함되어 있으며, [컨테이너 이미지](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#container-images)에 `cosign verify` 명령이 나와 있습니다. GitHub Release의 각 파일도 같은 방식으로 서명되어 옆에 `<file>.sigstore.json`으로 제공되며, [릴리스 파일](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#release-files)에 `cosign verify-blob` 명령이 나와 있습니다.
