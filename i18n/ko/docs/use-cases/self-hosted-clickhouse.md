<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# ClickHouse 기반 셀프 호스팅 에이전트 메모리

Loom은 Docker 또는 Helm 차트를 사용하는 Kubernetes 등 사용자의 자체 인프라에서 실행되며, 에이전트 메모리, 문서, 대화, 코드 인덱스를 ClickHouse의 테이블로 저장합니다. 배포 시 첫 부팅 때 라이브 메모리와 지식 상태를 위해 Postgres를 선택할 수 있습니다. 데이터는 사용자의 저장소에만 저장되며(사용자가 설정한 호스팅 모델 제공자와 트레이스 싱크는 자신이 처리하는 데이터를 받습니다), 에이전트가 아는 내용은 SQL로 조회할 수 있습니다.

<a id="what-you-get"></a>

## 제공 기능

- **Docker 또는 Kubernetes.** 한 줄 설치 스크립트로 로컬 스택을 시작하며, Helm 차트는 하나의 URL 뒤에서 동일한 레플리카 N개를 실행합니다. [Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Running N replicas](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS 또는 ClickHouse Cloud.** [Choosing the ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud and Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **벡터, 텍스트, 감사를 위한 단일 저장소.** 기본 ClickHouse authority 환경에서는
  벡터 검색, 어휘 검색, 그래프 검색, 필터, 감사, 삭제가 모두 하나의 데이터베이스 안에서 SQL로 처리됩니다.
  [Storage authority](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **다중 사용자.** 조직, 네임스페이스, 역할, 토큰, 싱글 사인온을 지원합니다.
  [Identities and namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [Single sign-on](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **로컬 모델.** 배포된 스택은 번들로 제공되는 로컬 모델을 사용해 CPU에서 임베딩을 수행하며,
  임베딩 provider는 직접 운영하는 OpenAI 호환 엔드포인트도 사용할 수 있습니다.
  [Models and API keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## 측정 결과

키당 버전이 2.4개이고 3072차원 임베딩(행당 벡터 2개)을 사용하는 합성 메모리 부하에서, ClickHouse는 키당 약 13.6 KB를 사용했으며 그 대부분은 임베딩 행이 차지했습니다. 더 작은 임베더를 사용하면 사용량이 줄어들고, 행 수가 적은 파트는 병합되기 전까지 행당 용량이 다소 더 큽니다.
[Memory at scale](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## 시작하기

[빠른 시작](../getting-started/quickstart.md) · [사전 요구 사항](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) ·
[업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [백업 및 복구](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
