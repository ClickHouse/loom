<!-- i18n: source=docs/operations/replicas.md blob=f4c8729ef4e795f5b6ab89dcd363a0bf9c30aad0 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md), which is authoritative.

<a id="running-n-replicas"></a>

# N개 레플리카 실행하기

모든 Loom 프로세스는 완전한 레플리카입니다. 하나의 ClickHouse 서비스를 하나의 URL 뒤에 두고 필요한 만큼 실행할 수 있으며, 고정 세션(sticky session)은 필요하지 않습니다. 각 프로세스는 모든 op를 처리하고, 모든 토큰을 검증하며, 공유 서비스에 기록합니다. 배포당 한 번만 실행해야 하는 작업은 ClickHouse 리스를 보유한 프로세스에서 실행되고, 그 프로세스가 중지되면 다른 프로세스로 넘어갑니다.

| 배포당 한 번                | 실행 위치                                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| 스키마 마이그레이션        | 보류 중인 마이그레이션이 있는 빌드를 가장 먼저 부팅한 프로세스가 migrate 리스를 잡고 실행하며, 나머지는 끝날 때까지 기다립니다 |
| 백그라운드 워커            | `workers_supervisor` 리스를 보유한 프로세스 ([배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md))                         |
| 배포 구성 게시             | 같은 리스 보유자 (아래 참조)                                                                                       |

각 프로세스는 자체 git 미러(히스토리 읽기 시 없는 미러는 클론합니다. `LOOM_CODE_HISTORY_CLONE_ON_READ`), 자체 캐시, 자체 속도 제한 버킷을 유지합니다.

<a id="one-env-for-all-of-them"></a>

## 모든 레플리카에 같은 env 사용

모든 레플리카를 같은 env와 `LOOM_DEPLOYMENT=1`로 시작하십시오.

```bash
LOOM_DEPLOYMENT=1
LOOM_CH_HOST=...                 # the one ClickHouse service
LOOM_CH_PORT=8443
LOOM_CH_USER=...
LOOM_CH_PASSWORD=...
LOOM_JWT_SECRET=...              # the same root secret on every replica
LOOM_EMBEDDING_MODEL=...         # the same model and width on every replica
LOOM_EMBEDDING_DIM=...
OPENAI_API_KEY=...               # each replica makes its own provider calls
LOOM_TRUSTED_PROXIES=...         # the load balancer, so per-client limits see the client
```

`LOOM_JWT_SECRET`은 kid `default`로 서명하고, 다른 모든 서명 키와 저장된 모든 자격 증명을 봉인합니다. 따라서 값이 하나이면 모든 토큰이 모든 레플리카에서 동작합니다. 서명 키, 폐기 목록, 권한 부여, 서비스 계정은 공유 서비스의 행으로 저장되며, 키 교체나 폐기는 `LOOM_JWT_KEYRING_REFRESH_SECONDS`와 `LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS` 안에 모든 레플리카에 반영됩니다.

<a id="configuration-drift"></a>

## 구성 드리프트

각 프로세스는 `LOOM_INSTANCE_HEARTBEAT_SECONDS`(기본 10초)마다 `loom.instances`에 하트비트 행을 기록합니다. 행에는 빌드, MUST_MATCH 설정의 핑거프린트(임베딩 공간: `LOOM_EMBEDDING_PROVIDER`, `_MODEL`, `_DIM`, `_BASE_URL`, `_SEND_DIMENSIONS`), 루트 시크릿의 핑거프린트, 시작 시각이 담깁니다. 세 번의 간격 동안 갱신되지 않은 행이나 프로세스가 종료 시 중지됨으로 표시한 행은 활성 상태가 아닙니다.

리스 보유자는 자신의 구성을 `loom.deployment_config`에 게시합니다. 게시되는 내용은 MUST_MATCH 설정과 루트 시크릿의 HMAC 핑거프린트입니다. 모든 프로세스는 부팅 시와 하트비트마다 이 스냅샷과 자신을 비교합니다. 루트 시크릿이 다르거나(해당 프로세스의 토큰과 저장된 자격 증명을 다른 곳에서 열 수 없습니다) MUST_MATCH 값이 다르면(벡터가 다른 공간에 놓이게 됩니다) 드리프트입니다. 이 경우 프로세스는 준비 상태가 아니므로 로드 밸런서가 트래픽을 보내지 않고, `/v1/health`는 차이점과 함께 `config_drift` 문제를 보고합니다. 준비 상태 해제는 새 연결만 막기 때문에, 프로세스는 모든 API, MCP, 로그인 호출에도 `503 CONFIG_DRIFT`로 응답하고 연결을 닫습니다. 그러면 클라이언트의 재시도는 일치하는 레플리카로 향합니다. 비교 대상 값은 env의 값입니다. 대시보드에서 하는 모델 전환은 배포 전체에 적용되므로(모든 레플리카가 ClickHouse에서 읽습니다) 드리프트가 아니며, 그 이후 같은 env로 시작한 레플리카는 준비 상태가 됩니다.
`/v1/health`의 `deployment_config`는 마지막 검사 결과와 활성 호스트를 보여 줍니다.

리스 보유자는 자신과 드리프트된 스냅샷 위에는 게시하지 않습니다. 잘못된 루트 시크릿이나 임베딩 모델을 가진 레플리카가 리스를 얻더라도 준비 상태가 되지 않으며, 게시된 env를 쓰는 레플리카는 계속 준비 상태로 유지됩니다. 자신과 일치하는 스냅샷은, 저장된 스냅샷을 실행 중인 다른 활성 레플리카가 없을 때에만 다시 씁니다. 따라서 롤아웃 중 N-1과 N의 서로 다른 형태의 스냅샷이 계속 번갈아 덮어쓰이는 일은 없습니다.

**MUST_MATCH 설정이나 `LOOM_JWT_SECRET`을 변경하는 경우**에는 롤링 방식으로 적용할 수 없습니다. 새 값을 쓰는 레플리카는 준비 상태가 아닌데 이전 값을 쓰는 레플리카가 서비스하기 때문입니다. 모든 레플리카를 중지하고, env를 변경하고, 새 env로 `loom deployment publish --yes`를 실행한 뒤(변경 내용이 나열됩니다) 다시 시작하십시오(임베딩이 바뀌면 전체 재임베딩이 이어집니다, `loom reembed`). 시크릿을 변경한 뒤에는 토큰을 다시 발급하고 자격 증명을 다시 저장하십시오.

`LOOM_DEPLOYMENT`가 없는 프로세스(같은 서비스에 연결된 브랜치 또는 A/B 서버)도 하트비트는 기록하지만 게시하지 않고, 검사 대상도 아니며, 레플리카로 계산되지 않습니다.

<a id="rate-limits"></a>

## 속도 제한

설정된 상한은 배포 전체에 적용됩니다. 각 레플리카의 몫은 상한을 `loom.instances`에 활성 행이 있는 호스트 수로 나눈 값이므로, 레플리카를 추가하거나 제거하면 하트비트 한 번 안에 모든 버킷의 크기가 조정됩니다. `LOOM_REPLICA_COUNT`는 그 호스트 수의 하한이며, 행을 읽을 수 없을 때 사용하는 값이기도 합니다. 한 호스트의 웹 워커들은 하나의 버킷 테이블을 공유합니다 ([배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)).

<a id="code-repositories"></a>

## 코드 저장소

모든 프로세스는 `LOOM_CODE_REPOS_REFRESH_SECONDS`(기본 30초)마다 `loom.code_repos`를 다시 읽으므로, 한 레플리카에서 추가하거나 제한한 저장소는 그 시간 안에 모든 레플리카에 적용됩니다.

git 히스토리를 제공하지 않아야 하는 호스트(미러를 둘 디스크가 없는 엣지)는 `LOOM_GIT_HISTORY=none`을 설정합니다. 그러면 blame, 커밋 확인, PR 히스토리는 히스토리 없이 응답합니다.

<a id="stopping-a-replica"></a>

## 레플리카 중지

정상적으로 중지하십시오(`docker stop`, SIGTERM). 프로세스는 자신의 하트비트 행을 중지됨으로 표시하고 프로바이더 참가자 행을 폐기합니다. 강제 종료하면 둘 다 남으며, 하트비트 행은 세 번의 간격이 지나면 더 이상 계산되지 않습니다. 같은 컨테이너에서 재시작한 프로세스는 이전 프로세스의 참가자 행을 스스로 폐기합니다. 다른 곳에서 프로바이더 재구성이 죽은 프로세스의 행을 기다리고 있다면, 해당 프로세스가 사라진 것을 확인한 뒤 `/v1/health`가 보고한 `participant_id`로 폐기하십시오(하트비트가 최신이거나 쿼리가 아직 실행 중인 프로세스의 행은 명령이 거부합니다).

```bash
loom participant retire --participant <participant_id> --yes
```
