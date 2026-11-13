<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# ClickHouse 기반의 셀프 호스팅 에이전트 메모리

Loom은 Docker 또는 Helm 차트를 사용하는 Kubernetes를 통해 사용자의 인프라에서 실행되며, 에이전트 메모리, 문서, 대화, 코드 인덱스를 ClickHouse의 테이블로 저장합니다. 배포 시 첫 부팅에서 라이브 메모리와 지식 상태용으로 Postgres를 선택할 수 있습니다. 데이터는 사용자의 저장소에만 저장되며(사용자가 구성한 호스팅 모델 공급자와 트레이스 싱크는 자신이 처리하는 데이터를 받습니다), 에이전트가 아는 내용은 SQL로 조회할 수 있습니다.

<a id="what-you-get"></a>

## 제공 기능

- **Docker 또는 Kubernetes.** 한 줄짜리 설치 프로그램이 로컬 스택을 시작하고, Helm 차트는 하나의 URL 뒤에서 동일한 N개의 레플리카를 실행합니다. [Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Running N replicas](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS 또는 ClickHouse Cloud.** [Choosing the ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud and Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **벡터, 텍스트, 감사를 위한 하나의 저장소.** 기본 ClickHouse authority 환경에서는 벡터 검색, 어휘 검색, 그래프 검색, 필터, 감사, 삭제가 모두 하나의 데이터베이스 안에서 SQL로 처리됩니다.
  [Storage authority](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **멀티 유저.** 조직, 네임스페이스, 역할, 토큰, 싱글 사인온을 지원합니다.
  [Identities and namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [Single sign-on](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **로컬 모델.** 릴리스된 스택은 번들로 포함된 로컬 모델로 CPU에서 임베딩을 수행하며, 임베딩 공급자는 사용자가 직접 실행하는 OpenAI 호환 엔드포인트도 받을 수 있습니다.
  [Models and API keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## 측정 결과

키당 버전이 2.4개이고 3072차원 임베딩(행당 벡터 2개)을 가진 메모리의 합성 부하는 ClickHouse에서 키당 약 13.6 KB를 사용했으며, 그 대부분은 임베딩 행이 차지했습니다. 더 작은 임베더는 더 적게 사용하며, 행 수가 적은 파트는 병합되기 전까지 행당 다소 더 많은 공간을 차지합니다.
[Memory at scale](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## 시작하기

[빠른 시작](../getting-started/quickstart.md) · [Prerequisites](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) ·
[Upgrades](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [Backup and recovery](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
