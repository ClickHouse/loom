<!-- i18n: source=docs/operations/analytics-export.md blob=241a7492fe019249ba054a278a0059bcb928b7e0 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md), which is authoritative.

<a id="exporting-usage-to-a-data-warehouse"></a>

# 데이터 웨어하우스로 사용량 내보내기

`loom export-analytics`는 UTC 기준 하루치 사용량과 네임스페이스 스냅샷을 Parquet 파일로 S3 버킷에 기록합니다. 엔티티당 하루에 파일 하나씩 기록하며, 데이터 웨어하우스가 이를 로드합니다. Helm 차트는 이 명령을 CronJob으로 하루에 한 번 실행합니다(`values.yaml`의 `analyticsExport`). 테넌트의 메모리를 내용과 함께 조회하거나 요청 원장을 그 자리에서 조회하려면 [SQL 인터페이스](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)를 대신 사용하세요.

```bash
loom export-analytics --bucket analytics-landing --prefix loom \
  --role-arn arn:aws:iam::123456789012:role/loom-analytics-writer \
  --expected-bucket-owner 123456789012
```

이름을 해시할 때 사용하는 시크릿인 `LOOM_ANALYTICS_EXPORT_NAME_KEY`가 필요합니다([해시된 이름](#hashed-names) 참조).

`--day YYYY-MM-DD`로 다른 날짜를 내보낼 수 있으며, 기본값은 어제(UTC)입니다. 아직 끝나지 않은 날짜는 거부됩니다.

<a id="files"></a>

## 파일

```text
<prefix>/<entity>/<yyyy>/<mm>/<dd>/<entity>-<yyyy>-<mm>-<dd>.parquet    entity: usage, namespaces
```

각 업로드에는 `If-None-Match: *`가 포함되므로 객체는 절대 덮어쓰이지 않습니다. 같은 날을 두 번째로 실행하면 첫 번째 실행의 파일이 그대로 유지되고, 각 파일마다 `already there, kept`가 출력됩니다. 실패했거나 반복된 실행은 그대로 다시 시도할 수 있습니다. 특정 날짜를 다시 내보내려면 먼저 해당 객체를 삭제하세요.

| Entity       | 행 단위                                                                                                             | 컬럼                                                                                                                                                                                                                                     |
| ------------ | ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `usage`      | 해당 날짜의 `org`, `namespace`, `agent`, `operation`, `kind`, `transport`, `traffic_class` 조합마다 한 행이며, 출처는 `loom.agent_events`입니다 | `day`, `ops`, `errors`, `server_errors`, `slow`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`, `cost_usd`, `bytes_written`, `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`, `users`, `sessions`, `snapshot_at` |
| `namespaces` | `loom.namespaces`에 등록되었거나 유효한 메모리 행을 가진 `org`, `namespace` 조합마다 한 행                           | `registered`, `kind`, `visibility`, `active`, `created_on`, `live_memories`, `last_memory_on`, `day`, `snapshot_at`                                                                                                                      |

`usage`의 `kind`는 요청이면 `request`, 워커 실행이면 `worker`, 지연된 `memory.ask` 일관성 검사이면 `verdict`, 지연된 충돌 정산이면 `settle`입니다. 모든 `/v1/me/*` 경로는 하나의 `operation`인 `me`입니다. 원장은 해당 행에 요청 경로를 이름으로 붙이는데, 이 경로에는 호출자가 보낸 임의의 문자열이 들어갈 수 있기 때문입니다. `users`와 `sessions`는 해당 행 안에서의 고유 개수이며, 이름을 지정하지 않은 호출의 `-`는 제외합니다. 행들을 합산하면 한 사용자가 행마다 한 번씩 계산됩니다. `slow`와 지연 시간 분위수는 SLI 롤업의 값입니다. 롤업이 읽는 `ok` 요청(SLO 클래스가 있는 operation)만 계산하며, `slow`는 지연 시간 예산을 넘긴 요청입니다(예산이 0이면 모든 요청이 slow가 됩니다). 해당하는 요청이 없는 행의 지연 시간은 0입니다. `/v1/me/*` 경로, 지연된 판정, 워커 실행은 어느 쪽에도 포함되지 않습니다. `namespaces` 파일은 `--day`와 관계없이 `snapshot_at` 시점의 상태입니다. 과거 날짜를 백필하면 그날의 usage와 오늘의 namespaces를 얻게 됩니다. 등록되지 않은 네임스페이스는 `registered = false`이며 레지스트리 컬럼이 NULL입니다. `created_on`과 `last_memory_on`은 시각이 아니라 날짜입니다. 해시된 개인 네임스페이스 옆에 정확한 시각이 있으면 그 시각에 가입했거나 기록한 사람과 대조될 수 있기 때문입니다. 행은 이름 자체가 아니라 해시된 이름 기준으로 정렬됩니다.

<a id="what-never-leaves-loom"></a>

## Loom 밖으로 나가지 않는 데이터

컬럼은 허용 목록(allowlist)으로 관리됩니다(`server/insights/store/analytics_export.py`, 통합 테스트가 각 엔티티의 정확한 컬럼 목록을 고정합니다). 버킷에 있는 파일은 [삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) 대상에서 벗어나므로, 내보내기에는 메모리 내용이나 저장된 개인 식별자가 포함되지 않습니다. 메모리 키와 값, `args`, `user_id`, `session_id`, `subject`, `owner_email`, 요청 id가 없습니다. 사용자와 세션은 개수로만 내보냅니다.

<a id="hashed-names"></a>

## 해시된 이름

조직(org), 네임스페이스, 에이전트 이름은 사람을 특정할 수 있습니다. 개인 네임스페이스는 소유자의 이메일을 따라 이름이 정해지고, 셀프 가입은 OS 사용자 이름과 호스트로 에이전트의 네임스페이스와 이름을 만들며, MCP 클라이언트는 자체적으로 에이전트의 이름을 지정합니다. 따라서 `org`, `namespace`, `agent`는 항상 비밀 키로 계산한 해당 이름의 참조 SipHash-2-4-128 값을 소문자 16진수로 나타낸 형태로 내보내집니다.

각 이름은 개별적으로 해시되므로, 같은 문자열은 어디에 나타나든 같은 해시가 됩니다. 네임스페이스 이름은 모든 조직에서 같은 해시가 되고, 이름이 같은 조직, 네임스페이스, 에이전트는 해시를 공유합니다. 따라서 웨어하우스는 키 없이도 이러한 행을 연결할 수 있습니다. 예를 들어 두 조직에 있는 동일한 개인 네임스페이스를 연결할 수 있습니다.

- 키는 `LOOM_ANALYTICS_EXPORT_NAME_KEY`에 설정하는 32자 이상의 임의 문자열입니다. 이보다 짧거나 비어 있는 키, 또는 앞뒤에 공백이 있는 키가 설정되어 있으면 내보내기가 실행되지 않습니다. `--from-file`로 만든 Secret은 파일의 개행 문자를 그대로 유지하므로, 모든 해시가 그 개행 문자를 포함한 키로 계산되기 때문입니다.
- 16바이트 SipHash 키는 해당 문자열(UTF-8)의 SHA-256 앞 16바이트입니다. 해시는 ClickHouse의 `sipHash128ReferenceKeyed`이며, 어떤 SipHash-2-4-128 라이브러리든 이 16바이트로 같은 값을 재현할 수 있습니다. 따라서 키를 가진 시스템은 자체 ID를 해시하여 조인에 사용할 수 있습니다. ClickHouse는 키를 두 개의 워드로 받으며, 각각 8바이트 절반을 리틀 엔디언으로 읽습니다. ID는 Loom이 저장하는 것과 같은 문자열(UUID나 숫자의 `toString`)로 해시해야 합니다. 타입이 있는 컬럼에 SipHash를 적용하면 이진 형태를 해시하므로 절대 일치하지 않습니다.

  ```sql
  WITH SHA256({key:String}) AS d
  SELECT lower(hex(sipHash128ReferenceKeyed(
      (reinterpretAsUInt64(substring(d, 1, 8)), reinterpretAsUInt64(substring(d, 9, 8))),
      toString(tenant_id)))) AS hashed_tenant_id
  FROM tenants
  ```

- 해시는 익명 값이 아니라 가명입니다. 키를 가진 사람은 알고 있는 이름에서 해시를 다시 계산할 수 있습니다. Loom에서 네임스페이스를 삭제해도 이미 버킷에 있는 해당 행은 제거되지 않습니다.
- 해시는 키가 유지되는 동안 안정적입니다. 키를 바꾸면 모든 해시가 바뀌며, 이미 내보낸 날짜는 이전 파일을 그대로 유지합니다. 새 키로 해당 날짜를 다시 내보내기 전에 이전 파일을 삭제하십시오.
- 키를 사용하는 쿼리는 키를 기록에 남깁니다. 키를 파라미터로 바인딩하든 리터럴로 작성하든 마찬가지입니다. ClickHouse는 바인딩된 값을 `system.query_log`의 쿼리 텍스트에 치환해 넣고 디버그 및 트레이스 로그에도 기록합니다. 웨어하우스에서는 조인을 실행하는 서버에서 이러한 로그를 읽을 수 있는 대상을 제한하거나, 아무 SipHash-2-4-128 라이브러리로 ClickHouse 밖에서 ID를 해시하십시오. Loom 쪽에서는 내보내기의 키 워드가 Loom의 ClickHouse의 같은 로그에 남지만, 그 로그를 읽을 수 있는 사람은 이미 이름을 읽을 수 있습니다.

<a id="credentials"></a>

## 자격 증명

이 명령은 프로세스의 기본 AWS 자격 증명을 사용하며, ClickHouse 작업을 시작하기 전에 한 번 확인합니다. `--role-arn`을 지정하면 해당 역할을 assume하고, 지정하지 않으면 STS에 자격 증명의 소유자를 질의합니다(`GetCallerIdentity`). 자격 증명이 없거나 만료되었거나 역할을 assume할 수 없으면 데이터를 읽은 뒤가 아니라 이 단계에서 실패합니다. `s3:PutObject` 권한이 없거나, 버킷이 잘못되었거나, 버킷 소유자가 `--expected-bucket-owner`와 다른 경우에는 첫 번째 업로드에서 실패합니다. `--role-arn`을 지정하면 업로드할 때마다 역할을 다시 assume하고 그 자격 증명으로 씁니다. writer 역할을 assume하는 방식은 다른 계정의 버킷에 적합합니다. 버킷이 속한 계정이 writer 역할에 `s3:PutObject`를 부여하고, writer 역할은 export 자체의 ID만 신뢰합니다. export에는 버킷에 대한 읽기, 목록 조회, 삭제 권한이 필요하지 않습니다. 고객 관리형 KMS 키로 암호화된 버킷은 writer 역할에 해당 키의 `kms:GenerateDataKey` 권한도 필요하며, 없으면 모든 업로드가 거부됩니다. `--expected-bucket-owner`를 지정하면 다른 계정이 버킷을 소유한 경우 S3가 쓰기를 거부합니다.

프로세스에 `AWS_DEFAULT_REGION` 또는 AWS 설정 파일로 지정된 리전이 이미 있는 경우가 아니라면 `--region`(버킷의 리전이며, 차트에서는 `analyticsExport.region`)을 지정하세요. 여기서 boto3는 `AWS_REGION`을 읽지 않습니다.

<a id="on-kubernetes"></a>

## Kubernetes에서

```yaml
analyticsExport:
  enabled: true
  bucket: analytics-landing
  prefix: loom
  roleArn: arn:aws:iam::123456789012:role/loom-analytics-writer
  expectedBucketOwner: "123456789012"
  nameKeySecret:
    name: loom-analytics-export-key # key LOOM_ANALYTICS_EXPORT_NAME_KEY
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::210987654321:role/loom-analytics-export
```

```bash
kubectl create secret generic loom-analytics-export-key \
  --from-literal=LOOM_ANALYTICS_EXPORT_NAME_KEY="$(openssl rand -hex 32)"
```

내보내기 파드만 해당 Secret을 읽습니다. 차트는 `nameKeySecret.name`이 없으면 렌더링을 거부하며, 키를 모든 web 및 workers 파드에 전달하게 되는 다음 설정도 거부합니다: `env`, `extraEnv` 또는 `secrets.extra`에 있는 `LOOM_ANALYTICS_EXPORT_NAME_KEY`, `settingsFile`(모든 파드가 마운트하는 ConfigMap)에 있는 `analytics_export_name_key`, 그리고 릴리스의 Secret(`secrets.existingSecret` 또는 차트 자체의 Secret)이거나 `envFrom` Secret인 `nameKeySecret.name`. 차트는 이러한 Secret의 내용을 볼 수 없으므로 키를 그 안에 넣지 마십시오.

차트는 `<fullname>-analytics-export`라는 이름의 ServiceAccount와 같은 이름의 CronJob을 만듭니다. 여기서 `<fullname>`은 차트의 전체 이름입니다. `fullnameOverride`가 설정되어 있으면 그 값이고, 없으면 릴리스 이름에 차트 이름(`nameOverride`, 없으면 `loom`)이 포함된 경우 릴리스 이름이며, 그 외에는 `<release>-<chart name>`입니다. 이름이 52자 이내로 유지되도록 앞의 35자까지만 사용합니다(`kubectl get sa`로 확인할 수 있습니다). 작업은 UTC 00:30(`schedule`)에 한 번에 하나씩 실행되며, 재시도는 최대 3회이고 모든 시도를 합친 제한 시간은 3시간입니다. 각 구문은 20분까지 실행될 수 있고 ClickHouse 클라이언트가 하나를 재시도할 수도 있으므로, 느린 날에는 시도 횟수가 4회보다 적을 수 있습니다. 거부(종료 코드 2)는 재시도하지 않고 Job을 즉시 실패시킵니다. 이 파드는 web 파드의 `podLabels`, `podAnnotations`, `affinity`를 따르지 않습니다.
web 및 workers 파드는 자체 ServiceAccount를 유지하므로, writer 역할을 가정(assume)할 수 있는 역할은 내보내기 파드에만 바인딩됩니다. IRSA를 사용하는 경우 해당 역할의 신뢰 정책은 `system:serviceaccount:<namespace>:<fullname>-analytics-export`를 지정합니다. `roleArn`이 설정되어 있으면 이 역할의 유일한 권한은 `roleArn`에 대한 `sts:AssumeRole`이며, `roleArn`이 비어 있으면 직접 쓰기를 수행하므로 버킷에 대한 `s3:PutObject`(고객 관리형 KMS 키를 사용하는 경우 `kms:GenerateDataKey`도)가 필요합니다. EKS Pod Identity를 사용하는 경우 역할을 ServiceAccount에 바인딩하고 `annotations`는 비워 두십시오.

작업은 web 파드와 마찬가지로 릴리스의 Secret, 설정, `extraEnv`, `extraVolumes`를 사용해 ClickHouse를 읽으며, ClickHouse에는 아무것도 쓰지 않습니다. 실행이 실패하면 실패한 Job으로 표시됩니다. CronJob의 마지막 성공 실행이 하루 넘게 지났을 때 알림을 설정하십시오.

모든 시도가 실패했거나 시작 기한(`startingDeadlineSeconds`, 1시간)을 놓친 날은 나중에 자동으로 내보내지지 않으며, 다음 실행은 그 전날만 내보냅니다. 해당 날짜는 CronJob에서 만든 Job으로 내보내십시오. 이렇게 하면 내보내기와 같은 ServiceAccount와 플래그로 실행되며, 명령에 `--day`를 추가합니다.

```bash
kubectl create job --from=cronjob/<fullname>-analytics-export analytics-export-backfill \
  --dry-run=client -o yaml > backfill.yaml
# add `- --day=YYYY-MM-DD` to the container's command in backfill.yaml, then:
kubectl apply -f backfill.yaml
```

사용량 파일은 해당 날짜의 것이고, 네임스페이스 파일은 실행 시점의 스냅샷입니다.
