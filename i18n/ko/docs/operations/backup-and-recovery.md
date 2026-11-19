<!-- i18n: source=docs/operations/backup-and-recovery.md blob=02de0a5c915dc18bf4a94189075292e0ca3d2cc0 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md), which is authoritative.

<a id="backup-and-recovery"></a>

# 백업 및 복구

Loom 백업은 ClickHouse 백업이며, 카탈로그 행이 문서 버킷의 객체를 참조하는 동안에는 문서 버킷도 포함하고, Postgres가 메모리 상태의 기준 저장소인 구성에서는 Postgres 백업도 포함합니다.
ClickHouse는 원본의 카탈로그(`loom.document_blobs`, 기본 저장소에서는 원본의 바이트도 포함), 토큰 서명 키(`loom.signing_keys`), 관리자 런타임 오버라이드(`loom.runtime_overrides`)를 보관합니다. 다음 항목은 ClickHouse 밖에 있습니다.

- `LOOM_JWT_SECRET`, 루트 시크릿입니다. 저장된 자격 증명과 서명 키를 봉인하므로, 이 값 없이 복원한 백업은 둘 중 어느 것도 열 수 없습니다. env 설정과 함께 보관하세요.
- `var/code_repos` 아래에 있는 코드 인덱스의 git 미러입니다. ClickHouse에 기록된 원격 저장소의 캐시입니다. 미러가 없는 호스트는 다시 클론합니다.
- `LOOM_DOCUMENT_S3_BUCKET`, `LOOM_DOCUMENT_OBJECT_STORE=s3`인 동안 기록된 원본의 바이트를 보관합니다(다시 전환해도 그대로 남아 있습니다). ClickHouse는 각 객체의 위치를 보관합니다.

<a id="choosing-a-backup"></a>

## 백업 방식 선택

| 방법                                                                    | 대상                                                      | 일관된 스냅샷                | 용도                                                                                                         |
| ----------------------------------------------------------------------- | --------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------ |
| ClickHouse Cloud 백업                                                   | 서비스의 모든 테이블                                      | 예 (서비스 단위)             | Cloud에서의 재해 복구                                                                                        |
| `BACKUP DATABASE loom TO Disk(...)` / `S3(...)`, 또는 `clickhouse-backup` | 모든 `loom.*` 테이블                                      | 예                           | 자체 관리형 OSS에서의 재해 복구                                                                              |
| `BACKUP DATABASE loom_telemetry TO ...`                                 | SLI 롤업 (`loom_telemetry.sli_minute`)                    | 예                           | 원장을 다시 접어(refold) 복구할 수 있는 31일을 넘어 SLO 이력을 보관                                          |
| `dev/backup_loom.py` 이식 가능한 덤프                                   | 모든 Loom 테이블 (아래 참조)                              | 테이블 단위, 테이블 간에는 아님 | 배포, 조직 또는 네임스페이스 이전 (원본이 버킷에 있는 경우: [아래](#the-portable-dump)), 콜드 복사본          |
| `namespace.snapshot`                                                    | 하나의 네임스페이스의 큐레이션된 메모리, 타입, 권한, 감사 로그 말미 | 한 번의 호출                 | API를 통한 네임스페이스 복사 또는 동결                                                                       |

<a id="the-portable-dump"></a>

## 이식 가능한 덤프

`dev/backup_loom.py`는 테이블마다 `JSONEachRow` 파일 하나를 쓰고, 이어서 `manifest.json`을 씁니다. 매니페스트에는 원본 데이터베이스와, 테이블별로 파일, 행 수, 바이트 크기, SHA-256, `CREATE` 문(또는 덤프를 중단시킨 오류)이 담깁니다. 종료 코드 1은 테이블 하나가 실패했다는 뜻이고(`complete: false`), 종료 코드 2는 인자가 잘못되었거나 ClickHouse 대상이 없거나 출력 디렉터리에 이미 백업이 있다는 뜻입니다.

이 도구는 `system.tables`에서 읽은 데이터베이스의 모든 MergeTree 계열 테이블을 덤프합니다. 마이그레이션 러너의 `_` 접두사 테이블, `.inner` 테이블, KeeperMap 리스(데이터가 아니라 실시간 조정 정보), 그리고 `loom migrate`가 만드는 뷰는 제외합니다. 매니페스트의 `backup_version`은 3입니다. `--org`는 `org` 컬럼이 있는 모든 테이블을 필터링하고, 컬럼이 없는 테이블(`idempotency`, `signing_keys` 등)은 건너뜁니다. 이런 테이블에는 모든 org의 행이 들어 있습니다.

`dev/restore_loom.py`는 모든 파일을 매니페스트와 대조해 검증하고(크기, 행 수, SHA-256), 계획을 출력합니다. 그다음 기록된 `CREATE` 문으로 대상 데이터베이스와 없는 테이블을 만들고, 각 파일을 해당 테이블의 스테이징 복사본에 로드한 뒤 스테이징된 파트를 테이블에 attach합니다. attach는 materialized view를 실행하지 않으므로, 복원된 행이 백업이 함께 복원하는 테이블에 다시 복사되지 않습니다(그렇지 않으면 `memory_versions`에 모든 버전이 두 번 들어갑니다). 뷰는 라이브 데이터베이스에 대한 쓰기에서는 계속 동작합니다. `loom migrate`가 준비한 데이터베이스(뷰가 있음)나 임시 데이터베이스(기록된 DDL로 테이블 생성)로 복원하십시오. 코드화된 벡터 테이블(기록된 `CREATE`에 `Quantized(`가 있음)이 들어 있는 백업은, 대상의 코덱 설정을 읽을 수 없거나 `enable_quantized_codec`을 켤 수 없는 경우 `--dry-run`을 포함해 아무것도 쓰기 전에 거부됩니다. 켤 수 있는 경우에도 `allow_suspicious_codecs`를 켤 수 없으면 해당 테이블은 `Quantized` 뒤의 일반 코덱을 잃습니다
([제한된 설정](../architecture/clickhouse.md#gated-clickhouse-settings)). insert는 `optimize_on_insert = 0`으로 실행되므로, `ReplacingMergeTree` 키가 중복된 행을 포함해 덤프된 모든 행이 그대로 들어가고, 테이블은 자체 머지에서 이를 합칩니다.
각 테이블은 attach 전에 `… attaching <table>`을, attach 후에 `✓`를 출력합니다. 실행이 중간에 끊기면 마지막 줄이 그 시점에 처리 중이던 테이블을 알려 줍니다. `--into-nonempty`로 다시 실행하기 전에 해당 테이블의 행 수를 확인하십시오. 그대로 다시 실행하면 그 테이블에 행이 한 번 더 추가됩니다.

매니페스트에는 insert로 실행되는 각 materialized view의 소스 테이블과 대상 테이블이 기록됩니다. attach는 뷰를 실행하지 않으므로, 복원은 뷰의 한쪽 끝만 포함하는 선택을 거부합니다. `memory_versions`(및 `memory_code_link_queue`) 없이 `memory`만 복원하면 `memory.history`, `memory.redact`, `namespace.rollback`이 복원된 버전을 보지 못하게 되고, `code_chunks` 없이 `code_term_freq`만 복원하면 코드를 다시 수집할 때 그 테이블이 다시 채워집니다. `documents`와 `document_blobs`를 잇는 뷰는 없지만, 복원은 한쪽만 선택하는 것도 거부합니다. 원본이 없는 문서는 모든 다운로드에 410으로 응답하고, 문서가 없는 원본은 원본 정리 작업이 삭제하는 대상입니다. 두 도구 모두 `documents`를 다른 어떤 테이블보다 먼저 처리하므로, `--into-live`로 워커가 실행 중이어도 정리 작업이 그 원본을 가리키는 문서보다 먼저 복원된 원본 행을 보는 일은 없습니다(버킷의 객체는 아래를 참고하십시오). 양쪽 끝을 모두 선택하거나, 대상에 이미 그 행이 있다면 `--allow-view-split`을 전달하십시오. `dev/backup_loom.py --exclude t1,t2`는 패스가 다시 만드는 테이블과, 뷰가 그 테이블에서 채우는 모든 테이블을 함께 제외합니다(`code_chunks`를 제외하면 `code_term_freq`와 `code_doc_stats`도 제외됩니다). `restore_loom.py --exclude`는 백업의 일부 테이블을 복원에서 제외합니다.

문서 원본(`document_blobs`)은 바이너리입니다. 덤프는 이를 원시 바이트로 쓰고 `output_format_json_validate_utf8 = 0`을 직접 설정하므로, UTF-8을 검증하는 프로필이 있어도 원본이 대체되지 않습니다. 바이너리 원본은 덤프에서 원래 크기의 약 1.6배를 차지합니다.

`LOOM_DOCUMENT_OBJECT_STORE=s3`로 기록된 행에는 객체의 위치만 들어 있고, 바이트는 버킷에 있습니다. 버킷은 자체 버저닝이나 복제로 백업하십시오. 복원할 때는 같은 이름의 버킷으로(각 행에 버킷 이름이 있습니다), 그 객체를 가리키는 ClickHouse 백업 이후에 복원하거나, 둘 다 복원될 때까지 `LOOM_DOCUMENT_BLOB_GC_ENABLED=false`로 두십시오. 복원된 객체가 이전 수정 시각을 유지할 수 있고(복원 방식에 따라 다릅니다), 그러면 아직 `document_blobs` 행이 들어오지 않은 객체를 버킷 순회가 삭제하기 때문입니다.

버저닝된 버킷은 삭제된 모든 객체를 noncurrent 버전으로 보관하고, 복제는 기본적으로 삭제를 복제하지 않습니다. 따라서 지워지거나 정리된 원본은 수명 주기 규칙(`NoncurrentVersionExpiration`, 복제본의 만료 포함)이 제거할 때까지 버킷(또는 그 복제본)에 남습니다. 이 규칙의 기간이, 행에 대해 ClickHouse 백업의 보존 기간이 하는 역할처럼, 지워진 원본을 여전히 복원할 수 있는 기간입니다.

덤프에는 이런 행의 원본 바이트가 들어 있지 않으므로 옮길 수 없습니다. 다른 배포로 복원된 행은 여전히 원본 버킷을 가리키고, 지우기는 버킷과 접두사 밖이라며 이를 거부하며, 원본의 행이 사라지면 원본의 정리 작업이 그 객체를 삭제합니다. 또한 원본의 버킷과 접두사를 가리키도록 설정된 대상은 대상 카탈로그가 가리키지 않는 원본의 모든 객체를 정리해 버립니다. 문서를 대상에 다시 업로드해서 옮기거나, 원본을 중지한 상태에서 배포 전체를 옮기십시오.

스테이징 복사본은 해당 테이블의 엔진을 그대로 반복합니다. 단, `Replicated`가 아닌 데이터베이스에 있는 복제 엔진은 같은 매개변수의 로컬 형태가 됩니다(복사본이 테이블의 Keeper 경로를 다시 차지하는 일을 막기 위해서입니다). 이후 attach가 파트를 복제합니다.
복원이 만드는 복제 테이블은 백업 자체의 데이터베이스에서만, 그리고 그 경로로 복제하는 것이 더 이상 없을 때에만 기록된 Keeper 경로를 유지합니다(라이브 복제본이 남아 있으면 거부합니다). 다른 데이터베이스에서는 독립적인 경로를 받습니다. 서버의 `default_replica_path` 아래 새 UUID를 쓰거나, Replicated 데이터베이스에서는 그 데이터베이스 자체의 경로를 씁니다. 그 기본값에 필요한 매크로가 서버에 없으면 거부합니다.
각 실행은 스테이징 테이블 이름을 `_restore_<run id>_<table>`로 정하고 실행 ID를 가장 먼저 출력하며, 각 실행은 자신의 테이블만 삭제합니다. 한 데이터베이스로의 복원(예: org 두 개)은 한 번에 하나씩 실행됩니다. 실행은 `default._loom_restore_lock_<db>`를 점유하고(다른 실행의 점유를 최대 5분 기다립니다), 대상을 다시 확인한 뒤, 이전 실행이 남긴 상태에 아래의 거부 규칙을 적용합니다. 그래서 같은 범위를 두 번째로 복원하면 첫 번째 행에 합쳐질 행을 attach하는 대신 거부합니다. 점유는 `default`의 테이블이므로 자체 관리 클러스터에서는 같은 서버를 거치는 실행끼리만 순서가 정해집니다. 모든 복원을 한 서버로 향하게 하십시오. 비정상 종료한 실행은 스테이징 테이블과 점유를 남깁니다. 그 실행의 프로세스가 남아 있지 않다면 `restore_loom.py --database <db> --drop-staging <run id>`로 삭제하십시오.
insert 단계 전에는 아무것도 쓰지 않으며, `--dry-run`은 계획까지만 보여 주고 멈춥니다. 다음은 거부됩니다.

- `--into-live` 없이 라이브 데이터베이스(`loom` 또는 서버 설정이 지정한 데이터베이스)로 복원하는 경우
- `--into-nonempty` 없이, 백업 범위에서 이미 행이 있는 테이블로 복원하는 경우. insert는 행을 추가하므로 일반 MergeTree 테이블(`audit_log`, `agent_events`)에는 모든 행이 두 번 들어갑니다.

두 도구 모두 서버와 같은 방식으로 ClickHouse 대상을 결정하며(환경 변수나 `.env`의 `LOOM_CH_*`, 각각 `--host`, `--port`, `--user`, `--database`, `--secure`로 재정의 가능), 호스트가 지정되지 않으면 실행을 거부하고, 비밀번호는 `LOOM_CH_PASSWORD`에서만 읽습니다.

```bash
python dev/backup_loom.py --out-dir ./backups/$(date +%F) [--org acme] [--tables memory,namespaces]
python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --dry-run
python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --skip-idempotency
python dev/restore_loom.py --in-dir ./backups/<date> --into-live \
  --tables memory,memory_versions,memory_code_link_queue   # one end of a view needs the other
./scripts/ops/backup_drill.sh [--full-restore]   # round-trip check into a scratch database
```

`scripts/ops/backup_drill.sh`는 라이브 데이터베이스를 임시 디렉터리에 덤프하고 매니페스트를 행 수 스냅샷과 대조합니다. `--full-restore`를 주면 임시 데이터베이스 `<db>_drill`로 복원하고, 모든 테이블의 행 수를 비교한 뒤, 종료할 때 임시 데이터베이스를 삭제합니다. 같은 키의 행을 합치는 테이블(`ReplacingMergeTree`, `AggregatingMergeTree` 등)은 attach 직후 임시 데이터베이스에서 머지될 수 있습니다. 머지가 실행된 뒤라면 행이 더 적어도 경고이고, 그렇지 않으면 실패입니다. 이 스크립트는 라이브 데이터베이스를 읽기만 합니다. 드릴은 일정에 따라 실행하십시오.

덤프는 테이블 단위이며 테이블 간 스냅샷이 아닙니다. 클러스터 간에 데이터를 옮기는 용도로 사용하고, 재해 복구에는 사용하지 마십시오.

<a id="verifying-the-audit-log-after-a-restore"></a>

## 복원 후 감사 로그 검증

`python dev/verify_audit_chain.py [--org X] [--ts-since YYYY-MM-DD] [--json]`은 모든 `audit_log` 행의 콘텐츠 해시를 다시 계산하며, 불일치가 있으면 1로, 검증을 수행하지 못했을 때(잘못된 인수, 대상 없음, 쿼리 실패)는 2로, 그 외에는 0으로 종료합니다(행이 없으면 통과로 처리합니다). 불일치 보고서는 해당 행을 파티션, 타임스탬프, 요청 ID로 식별하며, 행의 actor, subject, 네임스페이스는 절대 표시하지 않습니다. `audit_verifier` 워커는 동일한 행별 검사를 일정에 따라 실행합니다. 이 검사는 행 간 해시 체인이 아니라 행별 변조 탐지입니다. 매시간 생성되는 체크포인트는 별도로 체인으로 연결됩니다([감사](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)).

<a id="undoing-changes-inside-a-namespace"></a>

## 네임스페이스 내부의 변경 되돌리기

이 operation들은 ClickHouse 복원 없이 잘못된 쓰기, 오작동하는 에이전트, 잘못된 import를 되돌립니다. 각 operation은 하나의 네임스페이스에 한정됩니다. `namespace.snapshot`, `namespace.restore`, `namespace.rollback`은 MCP `all` 목록에 없습니다. 이름으로 직접 호출하거나 HTTP로 호출하거나, [대시보드](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)의 Health → Repairs 탭을 사용하세요. 네임스페이스 관리자는 이 탭에서 스냅샷을 다운로드하고, 복원하고, 롤백할 수 있습니다.

| 작업                 | 역할                                                   | 동작                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.restore`     | `WRITER`                                               | tombstone 처리된 키 하나를 되살립니다                                                                                                                                                                                                                                                                                                                                      |
| `namespace.snapshot` | `ADMIN`                                                | 네임스페이스의 메모리, 타입, 권한 부여, 감사 로그 말미(`include_audit_tail_n`, 기본값 1000)를 JSON 페이로드로 고정합니다. `include_embeddings=false`(기본값)이면 대상이 자체 모델로 다시 임베딩할 수 있습니다                                                                                                                                                           |
| `namespace.restore`  | 새 대상은 `VIEWER`, 기존 대상은 `ADMIN` | 스냅샷 페이로드를 `target_namespace`에 기록합니다(선택적으로 `rebind_agent`). `snapshot_id` 기준으로 멱등입니다. 행은 `version`, `content_sha256`, 증명 컬럼을 유지하므로 `verified_by`가 그대로 남습니다. 컬럼에 담을 수 없는 페이로드 값(UUID가 아닌 메모리 id)은 422로 거부됩니다. 컬렉션은 거부됩니다. 컬렉션의 스냅샷은 일반 네임스페이스로 복원합니다 |
| `namespace.rollback` | `ADMIN`                                                | 모든 메모리 head(또는 `memory_keys`)를 `as_of` 시점의 버전으로 되돌립니다. 이전 버전을 새 head로 복구하고, 그 이후에 생성된 키는 tombstone 처리합니다. 기본은 dry run입니다(`dry_run=false`로 적용). 호출당 최대 `max_keys`개까지 처리하며(`truncated`는 다시 호출해야 할 때를 알려 줍니다)                                                                                            |

Postgres authority에서는 복원 시, 삭제(erasure)로 대상에서 제거된 메모리 중 [삭제 증거](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)가 남아 있는 것은 제외하고 `counts.erased_skipped`에 집계합니다. 이 증거는 삭제가 해당 메모리를 제거한 네임스페이스만 다룹니다. 따라서 삭제 이전의 스냅샷을 다른 곳에 복원하면 삭제된 메모리가 되살아납니다. 이런 복원 후에는 같은 삭제(그 scope)를 다시 실행하세요.

`namespace.rollback`과 `memory.history`는 `loom.memory_versions`를 읽습니다. 이것은 `loom.memory`에 대한 모든 insert를 기록하는 append-only 미러입니다(병합 시 대체된 버전은 `loom.memory` 자체에서 제거됩니다). 롤백은 다음과 같이 동작합니다.

- 네임스페이스의 이력보다 오래되었거나 미래인 `as_of`는 거부합니다.
- 하드 삭제된 키는 건너뜁니다(`skipped_no_head`).
- 복구된 head의 재임베딩은 임베딩 워커에 맡깁니다.

```bash
AS_OF=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)   # GNU date; the instant before the bad writes
curl -s -XPOST http://localhost:7777/v1/namespace.rollback -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","as_of":"'"$AS_OF"'"}' | jq '{dry_run, reinstate, remove, truncated}'
```

<a id="under-the-postgres-authority"></a>

## Postgres가 권위 저장소일 때

`LOOM_STORAGE_AUTHORITY=postgres`일 때 라이브 메모리, 그 리비전 저널, 지식 헤드, 수집 작업, 데이터셋 카탈로그는 Postgres에 있으며([권위 저장소](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)), 배포 하나에 백업할 저장소는 Postgres와 ClickHouse 두 개입니다.

| 저장소     | 단독으로 보유하는 것                                                                                                                                       | RPO                                                                                                                                                                                                                                                                                                                                                                                                  | 백업 방식                                                      |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| Postgres   | 모든 메모리 헤드와 리비전, 지식 헤드, 수집 작업, 데이터셋 카탈로그, 멱등성 응답                                                                            | 동기식 스탠바이로 장애 조치(failover)해도 확인된 쓰기는 0; 특정 시점 복구(point-in-time restore)는 그 시점 이후의 데이터를 잃음                                                                                                                                                                                                                                                                        | HA 관리자 또는 서비스의 베이스 백업과 WAL 아카이브             |
| ClickHouse | 검색 문서와 벡터, 지식 문서와 긴 메모리 값의 청크, Postgres가 유지하는 범위를 넘어선 기록, 분석, 트레이스, 감사 행                                         | ClickHouse만 보유한 데이터는 마지막 백업 이후 경과 시간; Postgres가 보유한 메모리 리비전과 지식 문서는 다시 게시되지만 그 청크와 데이터셋의 행은 그렇지 않음: `loom reconcile`은 다시 청크로 나눌 각 지식 소스를, 20개를 넘으면 그 네임스페이스(`loom rechunk`)를, 그리고 다시 수집할 각 소스를 알려주며, 긴 메모리 값은 다시 설정될 때까지 발췌문으로 검색됨 | 위의 방법들                                                    |

복구에 걸리는 시간은 저장소 자체의 복구 시간에 `loom reconcile`을 더한 것이며, 이 명령은 복원된 저장소가 다른 저장소의 기록에서 잃은 것을 다시 실행합니다.

<a id="restoring-one-store"></a>

### 저장소 하나 복구하기

`LOOM_PG_PAYLOAD_CLEAR_ENABLED`가 켜진 상태에서 ClickHouse를 복구하기 전에, 페이로드 정리를 중지하고 [Postgres 저널과 ClickHouse 복구](#the-postgres-journal-and-a-clickhouse-restore)에 설명된 대로 백업을 선택하십시오. 정리 패스가 한 번 실행된 뒤에는 `010_revision_delivered_at.sql`을 포함한 빌드로 3단계의 `loom reconcile`을 실행하고, 4단계에서도 그 빌드로 배포를 시작하십시오(롤링 업그레이드 [규칙 43](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md#the-rules)). 그다음 진행합니다.

1. 배포의 모든 서버와 워커를 중지합니다.
2. 백업에서 저장소를 복구하고, 백업 자체의 메타데이터에서 백업 시각을 기록해 둡니다(ClickHouse는 `BACKUP`의 시작 시각, Postgres는 복구 목표 시점).
3. `loom reconcile --restored clickhouse --point <time>`(또는 `--restored postgres`)을 실행하면 다시 실행할 내용이 출력됩니다. 그다음 같은 명령을 `--apply`와 함께 실행합니다.
4. 배포를 시작합니다.
5. ClickHouse 복구 후에는 reconcile이 출력한 각 `loom rechunk`를 실행하고, "to ingest again" 아래에 나열된 각 소스를 다시 업로드하고 수집합니다. 지식 문서의 청크와 데이터셋의 행은 ClickHouse에만 있기 때문입니다.

[셀](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)의 Postgres에는 셀이 테넌시 행을 가지지 않은 조직의 행을 거부하는 org 트리거와, 스스로 표시하지 않는 세션의 테넌시 행을 거부하는 `loom.tenancy` 가드가 있습니다. 셀로의 논리적 복구는 이런 세션에 해당합니다. 셀의 모든 프로세스를 중지한 상태에서 마커를 붙여 한 번의 명령으로 `PGOPTIONS='-c loom.tenancy_checked=on' pg_restore ...`를 실행한 다음, 다른 파드보다 먼저 `LOOM_BOOT_MIGRATE`를 켠 파드 하나를 시작하십시오. 기존 스키마에 대한 데이터 전용 `pg_restore --disable-triggers`는 마지막에 모든 트리거를 origin 트리거로 활성화하는데, 그러면 `loom.cell_first`의 트리거 두 개가 `session_replication_role = replica`에서 꺼진 상태가 됩니다. 그 파드의 부팅이 이를 `ALWAYS`로 되돌리며, 그때까지 `loom doctor`가 해당 트리거를 알려줍니다.

Helm 차트([Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md))에서 릴리스 이름이 `loom`일 때:

1. 중지합니다: `kubectl scale deploy/loom --replicas=0`, `workers.enabled`이면 `deploy/loom-workers`도 중지합니다. 분석 내보내기가 켜져 있으면 일시 중단합니다:
   `kubectl patch cronjob loom-analytics-export -p '{"spec":{"suspend":true}}'`.
2. 관리형 서비스는 새 서비스로 복구됩니다. 파드가 시작되지 않도록 `helm upgrade ... --set replicaCount=0 --set workers.replicaCount=0 --set autoscaling.enabled=false`로 릴리스가 새 서비스를 가리키게 합니다(새 `clickhouse.host` 또는 `postgres.host` 포함). 시점은 서비스가 해당 백업에 대해 나열하는 시작 시각이거나 Postgres 복구 목표 시점입니다.
3. 아래 Job은 릴리스의 이미지를 실행합니다. 정리 패스가 한 번 실행되었고 릴리스가 `010_revision_delivered_at.sql`이 없는 빌드라면, 먼저 `helm upgrade ... --set image.tag=<a build that carries it>`(또는 `image.digest`, 설정되어 있으면 이것이 우선)를 2단계의 새 호스트와 3개의 `--set` 오버라이드와 함께 실행해 파드가 시작되지 않게 하고, 4단계에서도 그 이미지를 유지하십시오. reconcile은 웹 파드 자체의 스펙으로 만든 Job에서 실행합니다. 이렇게 하면 릴리스의 환경, Secret, Postgres CA, 고유한 컴포넌트 레이블(NetworkPolicy는 이를 선택하지만 Service는 그렇지 않음)을 갖고 프로브는 없습니다.

   ```bash
   kubectl get deploy/loom -o json | jq '{apiVersion: "batch/v1", kind: "Job",
     metadata: {name: "loom-reconcile"}, spec: {backoffLimit: 0, template: {
     metadata: {labels: (.spec.selector.matchLabels
       | .["app.kubernetes.io/component"] = "reconcile")},
     spec: (.spec.template.spec | .restartPolicy = "Never" | .containers |= [.[0]
       | del(.livenessProbe, .readinessProbe, .startupProbe)
       | .command = ["loom", "reconcile", "--restored", "clickhouse", "--point", "<time>"]])}}}' \
     | kubectl apply -f -
   kubectl logs -f --pod-running-timeout=10m job/loom-reconcile
   kubectl wait --for=condition=complete --timeout=10s job/loom-reconcile
   ```

   Postgres 복구 후에는 `"postgres"`를 사용하십시오. Job이 아직 활성 상태인데 `wait`가 실패하면(`kubectl get job loom-reconcile -o jsonpath='{.status.active}'`가 `1`을 반환하고, 로그 스트림이 끊기면 `logs -f`가 일찍 끝남) 아직 실행 중이라는 뜻이므로 로그를 다시 따라가십시오. Job이 실패했다면(`.status.failed`가 `1`) 0이 아닌 값으로 종료된 것입니다. 출력을 읽고 Job을 삭제한 뒤 다시 실행하십시오. 드라이 런이 깨끗하게 끝나면 `"--apply"`를 덧붙여 실행합니다.

4. 다시 시작합니다: 릴리스의 values 파일로 `helm upgrade`를 실행하되, 2단계의 새 호스트와 3단계에서 설정했다면 `image.tag` 또는 `image.digest`를 포함하고, 2단계의 3개의 `--set` 오버라이드는 빼며, 분석 내보내기를 재개합니다. reconcile이 출력한 각 `loom rechunk`를 `kubectl exec deploy/loom -- loom rechunk ...`로 실행하고, "to ingest again" 아래에 나열된 각 소스를 다시 업로드하고 수집합니다.

`loom reconcile --apply`는 다음을 수행합니다.

- 다른 저장소가 시점 이후에 완료된 것으로 기록한 모든 삭제(erasure)를 다시 실행합니다(원장은 초 단위로 보관하므로 시점의 해당 초 전체부터). 이때 census는 수행하지 않으며(다른 저장소는 삭제 이후 사용자가 쓴 것을 보유함), 복원된 사본에서 시점 이전에 이미 실행된 것으로 보이는 단계는 제외합니다. 그 단계란 시점 이전에 기록된 마지막 ClickHouse 체크포인트의 단계(`BACKUP`은 시작 시점부터 테이블을 차례로 복사하므로 그 삭제만 복사된 모든 테이블에 반영되어 있으며, 병합되어 사라진 행이 있으면 모든 단계를 실행함)에서 census가 다시 연 단계를 뺀 것, 또는 각 단계가 커밋된 트랜잭션에서 원장 행이 기록하는 Postgres 단계입니다(이전 빌드의 프로세스가 실행한 단계는 기록되지 않으므로 다시 실행됨). 이런 단계를 다시 실행하면 그 이후 사용자가 쓴 것을 삭제하게 됩니다. 복원된 원장 행은 모든 단계가 실행되면 completed가 됩니다. 그때까지 ClickHouse 복구 후에는 "replay after a restore pending"과 함께 `failed`로 읽히며, 복구 워커도 `loom erasure-re-verify --resume`도 이 행을 실행하지 않습니다. 복원된 행이 시점 이전에 completed였거나 이전 실행이 완료한 삭제는 다시 실행하지 않습니다. 재생 응답(replay answers) 단계는 모든 삭제가 그렇듯 두 저장소 모두에서 조직 전체의 재생 캐시를 삭제합니다. 종료 상태가 없는 삭제는 복구 워커에 맡기며, 그 체크포인트는 복구가 되돌린 단계를 다시 연 채로 다시 기록됩니다. ClickHouse 복구 후에 복원된 행이 completed 또는 abandoned로 나타나는 삭제는 대신 Postgres 원장 행에 그렇게 표시됩니다. 재개된 saga는 다른 것과 마찬가지로 census를 포함해 실행되며, 그 이후 범위 안에서 사용자가 쓴 것을 셉니다. 시점 이후에 완료되었지만 Postgres 원장 행이 완료를 놓친 삭제도 마찬가지인데, 이 경계 사례는 복구 패스가 요청 후 10분 뒤의 첫 패스에서(완료 후가 더 늦다면 완료 후에) 처리합니다. abandoned 상태인 것은 절대 실행하지 않습니다(`loom erasure-re-verify`가 담당합니다).
- ClickHouse 복구 후: 먼저 시점 이후 커밋된 모든 Postgres 리비전과 지식 헤드(작성자의 리스와 insert의 한계, 즉 시점 이전에 할당된 쓰기가 그 이후에 커밋될 수 있는 여유분을 뺀 것), 그리고 ClickHouse가 더 오래된 버전으로 보유한 모든 Postgres 헤드를 아직 기한이 되지 않은 전달로 보류합니다. 그다음 삭제를 실행하되 Postgres 단계는 건너뜁니다(이미 유지되고 있으며 다시 실행하면 삭제 이후 사용자가 쓴 것을 삭제하게 됨). 보류한 전달을 게시하고, 마지막으로 시점 이후 Postgres가 기록한 모든 편집(redaction)을 다시 실행하여 ClickHouse가 복원한 것과 그것을 인용하는 캐시된 응답 및 선례를 지웁니다.
- Postgres 복구 후: 복원된 사본에서 삭제의 Postgres 단계와 재생 응답 단계를 실행하고, 시점 이후 ClickHouse가 기록한 모든 편집을 실행합니다. 편집된 버전이 다시 사본의 라이브 헤드가 된 경우(그것을 대체한 쓰기가 손실됨) 그 자리에서 지웁니다. 시점 이후에 쓴 것은 모든 특정 시점 복구와 마찬가지로 손실됩니다. ClickHouse에는 그 사본이 여전히 남아 있지만, Postgres에 일치하는 헤드가 없으면 검색은 이를 제공하지 않습니다.

데이터셋의 행은 ClickHouse에만 있으며 수집만이 이를 만듭니다. 따라서 ClickHouse 복구 후에는 시점 이후에 행이 쓰인 데이터셋(게시, 또는 열 타입을 바꾼 승인)이 행이 누락된 채로 읽힙니다. 그 카탈로그 행은 Postgres에 있어 해당 행들을 지목하기 때문입니다. `loom reconcile`은 드라이 런에서도 시점 이후에 카탈로그 행이 쓰인 각 소스를 다시 업로드하고 수집하도록 알려줍니다. 여기에는 남아 있던 행 위에 승인이나 메타데이터 편집이 덮어쓴 행도 포함됩니다.

지식 수집 작업에는 별도의 단계가 필요 없습니다. 작업을 읽을 때는 Postgres의 행과 ClickHouse의 행 중 더 새로운 것을 사용하며, Postgres가 보유한 작업은 큐가 제시할 때 다시 투영됩니다. ClickHouse 복구 시점 이후에 끝난 작업은 다시 제시되지 않으므로 ClickHouse를 읽는 `knowledge.inspect`는 그 행이 만료될 때까지 이를 나열하지 않습니다.

끝나지 않은 재생은 ClickHouse 행을 "replay after a restore pending"과 함께 `failed`로 남깁니다. 배포를 시작하기 전에, 그리고 reconcile보다 앞선 빌드로 롤백하기 전에 `loom reconcile`이 0으로 종료될 때까지 다시 실행하십시오. 그 빌드의 복구 워커는 해당 행을 census와 함께 재개하게 됩니다.

이 명령은 아무것도 쓰기 전에 다음 경우에 거부합니다. 시점이 현재보다 늦은 경우, 워커가 워커 리스(`LOOM_SUPERVISOR_LEASE_TTL_S`) 기간 두 번 이내(합쳐서 기본값 1분)에 패스를 보고한 경우, 시점이 `LOOM_ERASURE_LEDGER_DAYS`보다 오래된 경우(ClickHouse의 원장 읽기는 89일을 유지), 그리고 `--restored both`인 경우입니다. 두 복구 시점 이후에 이루어진 삭제와 편집은 어느 저장소에도 없으므로 감사 내보내기에서 다시 발행해야 합니다. 두 원장 모두 삭제를 요청된 시각으로 보관하므로, 해당 경계에서 삭제가 실행될 수 있는 최장 시간 이내의 시점은 경계 이전에 요청되고 시점 이후에 완료된 삭제를 놓칠 수 있습니다. 백업은 원장의 보관 일수보다 그만큼 더 짧게 유지하십시오. 쓰기 전에 코드 저장소 레지스트리를 읽으며, 읽기가 실패하면 거부합니다. 실행하는 모든 단계는 멱등이므로, 중단된 reconcile이나 1로 종료된 reconcile은 배포를 시작하기 전에 처음부터 다시 실행하십시오. 보류된 전달은 실행이 완료될 때까지 `GET /v1/health?include=postgres`에 parked로 나열됩니다.

<a id="what-is-and-is-not-recoverable"></a>

## 복구할 수 있는 것과 없는 것

| 상황                                                       | 복구 가능 여부 | 방법                                                                                                                                                                                     |
| ---------------------------------------------------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.delete` 또는 `memory.clear`로 삭제 표시(tombstone)된 메모리 | 가능           | `memory.restore`, 또는 삭제 이전 시점으로 `namespace.rollback`                                                                                                                           |
| 이후 버전으로 덮어쓰인 메모리                              | 가능           | `memory.history`로 버전을 확인하고 `namespace.rollback`으로 한 버전을 복원                                                                                                               |
| `purge: true`로 수행한 삭제, 또는 purge된 bench 네임스페이스 | 불가능         | purge는 완전 삭제이며 복원 기간이 없습니다. 그 이전에 만든 백업만 사용할 수 있습니다                                                                                                      |
| 삭제 표시(tombstone)로 처리된 삭제 (`purge` 없음)          | 최종으로 간주  | saga의 census가 모든 읽기 경로에서 해당 범위가 사라졌음을 확인했습니다. 되살리면 삭제 요청을 취소하는 셈이므로, 삭제 자체가 실수였을 때만 백업에서 복원하십시오                          |
| 이미지를 롤백한 후, 더 새로운 빌드의 스키마                | 필요 없음      | 마이그레이션은 추가형이므로 이전 빌드가 더 새로운 스키마를 읽을 수 있습니다 ([업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#rollback))            |
| 이전 스키마                                                | 백업에서 복원  | down 마이그레이션이 없습니다. 업그레이드 이전에 만든 백업을 복원하십시오                                                                                                                 |
| 단일 노드 ClickHouse OSS 서버의 디스크                     | 백업에서 복원  | 복제와 특정 시점 복구(point-in-time recovery)가 없습니다. 노드는 자체 디스크까지만 내구성을 가집니다                                                                                     |
| 코드 인덱스 테이블                                         | 재구축 가능    | `loom code backfill`이 git 미러에서 다시 생성합니다 ([코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index))               |
| 선택적으로 설정한 보존 TTL로 제거된 행                     | 불가능         | TTL 삭제는 최종적입니다. 그 이전에 만든 백업만 사용할 수 있습니다                                                                                                                        |

단일 자체 관리 OSS 노드에서는 `BACKUP DATABASE`(또는 `clickhouse-backup`)를 호스트 외부 스토리지로 예약 실행하십시오. ClickHouse Cloud는 서비스 자체를 복제하고 백업하므로, 그곳의 이식 가능한 덤프는 내구성 확보용이 아니라 데이터 이전용입니다.

<a id="the-postgres-journal-and-a-clickhouse-restore"></a>

## Postgres 저널과 ClickHouse 복원

권위 있는 메모리 상태를 Postgres가 소유하는 구성(Postgres authority)에서는 `loom.memory_revisions`가 모든 리비전을 보관합니다. ClickHouse를 복원하면 백업 이후에 커밋된 버전은 저널에는 있지만 복원된 `memory_versions`에는 없습니다. 이 버전들의 내용은 페이로드(`row`)가 저널에 남아 있는 동안에만 보존됩니다. `LOOM_PG_PAYLOAD_CLEAR_ENABLED`를 켜면 `pg_payload_clear` 워커가 사용 가능한 모든 백업에 들어 있는 버전의 페이로드를 비웁니다([페이로드 비우기](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md#payload-clearing)). ClickHouse를 백업하지 않는 경우에는 이 설정을 꺼 두십시오. 그러면 저널이 모든 페이로드를 유지합니다.

**기준 시간(horizon).** `LOOM_CH_BACKUP_INTERVAL_MINUTES`를 ClickHouse 백업이 시작되는 주기로 설정합니다. 패스는 버전이 ClickHouse에 전달된 지 기준 시간보다 오래된 경우에만 해당 버전을 비웁니다. 기준 시간은 `LOOM_PG_PAYLOAD_RESTORE_HORIZON_MINUTES`입니다.

- 0(기본값)이면 주기의 두 배에 60을 더한 값입니다. 이 경우 가장 최근 백업이 실패했더라도 그 이전 백업이 일정대로 시작되었다면 복원 시 한 단계 이전 백업으로 돌아갈 수 있습니다.
- 주기에 60을 더한 값보다 작거나, 삽입 한계에 60을 더한 값보다 작을 수 없습니다. 더 낮게 설정한 값은 둘 중 큰 값으로 올라갑니다.
- 주기에 60을 더한 값 이상이고 주기의 두 배에 60을 더한 값 미만인 값은, 가장 최근 백업이 일정대로 시작된 경우에 한해 그 백업만 포함합니다.

두 설정 모두 LIVE입니다. 변경 사항은 프로세스가 오버라이드를 다시 읽은 뒤(`LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS`마다) 첫 번째 패스부터 적용됩니다. 이미 실행 중인 패스는 시작할 때의 기준 시간을 유지합니다.

**여유 시간이 필요한 이유.** 60분은 백업 시작 시각만으로는 드러나지 않는 다음 요소를 감안한 값입니다.

- 백업이 읽는 레플리카가 삽입을 받은 레플리카보다 뒤처지는 경우
- Postgres와 백업 호스트 사이의 시계 오차

비워진 모든 버전은 비우기가 중단되기 전 기준 시간보다 더 이전에 전달된 것입니다. 따라서 그 시점보다 기준 시간에서 60분을 뺀 시간 이내에 시작된 백업은, 지연과 시계 오차의 합이 60분 미만인 동안 비워진 모든 버전을 포함합니다. 늦게 시작된 백업은 자신의 시작 시각에 그 사실이 드러나며, 아래 4단계에서 이를 확인합니다. 가장 작은 기준 시간에서는 가장 최근 백업이 일정대로 시작되었을 때 통과하고, 늦게 시작된 백업은 통과하지 못할 수 있습니다. `BACKUP ... ON CLUSTER`는 모든 샤드를 한 번에 처리합니다. 노드별 도구를 사용한다면 백업의 시작 시각을 가장 먼저 시작한 노드의 시작 시각으로 계산하십시오.

**복원 전에 비우기를 중단하십시오.** 백업을 고르기 전에, ClickHouse에 백업에 없는 버전이 아직 남아 있는 동안 다음을 수행합니다.

1. 배포 env에서 `LOOM_PG_PAYLOAD_CLEAR_ENABLED=false`로 설정합니다. 이 설정은 LIVE가 아니며, `loom.runtime_overrides`를 복원해도 다시 켜지지 않습니다.
2. 해당 env로 모든 Loom 프로세스를 재시작합니다. 다시 시작될 수 있는 중지된 인스턴스는 제거하거나 새 env로 다시 생성합니다.
3. 오케스트레이터에서 실행 중이거나 시작될 수 있는 모든 Loom 프로세스에 이 스위치가 명시적으로 `false`로 설정되어 있는지 확인합니다. 설정하지 않은 채로 두면 스위치 값은 dotenv 파일(`LOOM_ENV_FILE`, 기본값은 작업 디렉터리의 `.env`)이나, 환경 또는 해당 dotenv가 지정한 설정 파일(`LOOM_SETTINGS_FILE`)에서 가져오며, 둘 중 어느 쪽이든 이 스위치를 켤 수 있습니다.
4. 그런 다음에야 백업을 고르며, 위 3단계의 확인 시점(비우기가 중단된 시각)보다 기준 시간에서 60분을 뺀 시간 이내에 시작된 백업을 선택합니다. 기준 시간을 낮춘 적이 있다면, 최근 기준 시간 동안 적용된 값 중 가장 작은 기준 시간을 사용합니다. 기본 기준 시간에서는 일정대로 시작되었다면 가장 최근 백업 또는 그 이전 백업이 해당됩니다. 이보다 오래된 백업에는 페이로드가 이미 비워진 버전이 없을 수 있으며, 그 경우 해당 버전의 내용은 유실됩니다.
5. 스위치는 [하나의 저장소 복원](#restoring-one-store)의 4단계에서 배포가 다시 시작될 때 다시 켜고, 그 전에는 켜지 마십시오. 그 시점에는 `loom reconcile --apply`가 해당 시점 이후 커밋된 모든 리비전에 대해 전달 대기 상태를 다시 만들어 두었고, 리비전은 전달 대기 중인 동안 페이로드를 유지합니다. 먼저 `LOOM_CH_BACKUP_INTERVAL_MINUTES`와 `LOOM_PG_PAYLOAD_RESTORE_HORIZON_MINUTES`를 백업 일정과 대조하십시오. 복원으로 백업 시점의 오버라이드가 돌아왔으므로 그 이후 변경한 값은 다시 적용합니다. 복원 후에도 ClickHouse에 없는 버전은 페이로드를 유지하며, 그 버전에 도달하는 각 패스는 이를 `missing_in_clickhouse`에 집계하므로 이 값이 0보다 큰 상태로 남을 수 있습니다. [하나의 저장소 복원](#restoring-one-store)의 4단계에서 `010_revision_delivered_at.sql`이 포함된 빌드를, 그것이 없는 빌드를 서비스하던 배포에서 시작하는 경우, 해당 빌드가 단독으로 서비스한 뒤 비우기 기준 시간 하나가 지날 때까지 스위치를 꺼 둡니다([규칙 43](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md#the-rules)).

[무중단 이중화 구성](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)에서는 다음과 같이 합니다.

1. `.env`를 수정합니다.
2. `origin/main`보다 뒤처지지 않은 체크아웃에서 `./redeploy.sh --force`를 실행합니다.
3. 재배포의 성공 여부와 관계없이, `docker inspect -f '{{.Config.Env}}'` 결과에 `LOOM_PG_PAYLOAD_CLEAR_ENABLED=false`가 나타나지 않는 모든 `loom-*-workers` 컨테이너를 실행 중이든 종료되었든 중지하고 `docker rm`으로 제거합니다. 웹 컨테이너는 워커를 실행하지 않으며, 이전 색상의 워커 컨테이너가 없으면 `--rollback`은 현재 워커를 유지합니다.
4. 그 결과 워커 컨테이너가 하나도 남지 않으면 원인을 해결한 뒤 2단계를 다시 실행합니다.

이 단계를 건너뛰고 복원하면 백업과 복원 사이에 커밋된 버전의 내용이 유실될 수 있으며, 해당 버전의 메타데이터는 저널에 남습니다.

<a id="the-rollback-colour"></a>

## 롤백 컬러

[무중단 페어](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)에서 `redeploy.sh`는 이전 컬러의 컨테이너(`loom-<colour>`, `loom-<colour>-workers`)를 제거하지 않고 중지하며, 이전 이미지에 `loom:prev` 태그를 붙입니다. `./redeploy.sh --rollback`은 해당 컨테이너를 시작하고 상태 검사를 수행한 뒤 프록시를 다시 이전 컬러로 전환합니다. 이전 컬러의 컨테이너가 없으면 롤백은 "the previous build is gone"이라는 메시지와 함께 거부됩니다. 이 경우 정상 동작이 확인된 커밋을 대신 재배포하십시오.

> **경고.** 페어를 실행하는 호스트에서는 `docker container prune`이나 `docker system prune`을 절대 실행하지 마십시오. 중지된 롤백 컬러가 삭제됩니다. 디스크 공간은 `docker builder prune`과 `docker image prune`으로 확보하십시오. 임시 컨테이너에 `loom-blue` 또는 `loom-green`이라는 이름을 붙이지 마십시오. 이름으로 컨테이너를 삭제하면 컬러 하나가 삭제됩니다.

롤백 후에도 롤포워드되었던 컬러의 웹 컨테이너는 사용자가 중지할 때까지 계속 실행됩니다(롤백이 출력하는 `docker stop -t 75 loom-<colour>`). 해당 컬러의 workers 컨테이너는 전환 전에 이미 중지되었으므로 workers 리스는 롤백된 컬러가 가지고 있습니다. 롤백된 컬러에 workers 컨테이너가 없는 경우에만 롤포워드되었던 컬러가 workers를 계속 실행합니다.

<a id="moving-a-deployment"></a>

## 배포 이전

같은 ClickHouse(그리고 `LOOM_STORAGE_AUTHORITY=postgres`일 때는 Postgres)를 가리키거나, 이전했거나 복원한 ClickHouse를 가리키는 새 Loom 프로세스는 모든 기능을 되돌려 받습니다. Postgres가 권위 있는 저장소인 구성에서는 어느 저장소든 복사하기 전에 원본을 중지하세요. 한 저장소를 다른 시점에 만든 백업에서 복원하는 것은 [단일 저장소 복원](#restoring-one-store)에 해당합니다. 다음 항목을 함께 옮기세요.

- ClickHouse: 같은 서비스, Keeper 상태가 포함된 복사본, 또는 `loom migrate`로 준비한 ClickHouse에 복원한 `dev/backup_loom.py` 덤프
- Postgres(`LOOM_STORAGE_AUTHORITY=postgres`일 때): 같은 서비스 또는 복사본
- env 설정(`.env` 또는 컨테이너 환경)
- `LOOM_JWT_SECRET`. 이 값이 없으면 어떤 토큰도 검증되지 않으며, 저장된 자격 증명과 서명 키도 열리지 않습니다.
- 카탈로그 행 중 하나라도 해당 버킷의 객체를 가리키는 동안은 같은 이름과 접두사의 `LOOM_DOCUMENT_S3_BUCKET`: 새 호스트에서 접근하는 같은 버킷, 또는 다른 엔드포인트에 있는 같은 이름의 버킷(`LOOM_DOCUMENT_S3_ENDPOINT_URL`은 행의 위치에 포함되지 않습니다). 모든 행에는 버킷 이름이 기록되어 있으며 Loom에는 이를 다시 쓰는 도구가 없습니다. 다른 이름으로 복사하면 모든 다운로드와 삭제가 이전 버킷을 계속 가리킵니다.

나머지는 모두 ClickHouse에서 복구됩니다. ClickHouse에 저장된 문서 원본, 서명 키, 런타임 오버라이드, supervisor 리스, 폐기 목록, 로그인 시도 횟수, 그래프 탐색 무효화 표시가 여기에 해당합니다. Git 미러는 다음 수집 과정에서, 또는 새 호스트에서 처음 이력을 읽을 때 기록된 원격 저장소로부터 다시 clone됩니다.

제한적인 예외:

- 이벤트 스트림(예산 위반)은 `SIGTERM` 시 비워집니다. `SIGKILL`이나 OOM이 발생하면 해당 텔레메트리는 최대 한 번의 flush 주기 분량만 유실되며, 감사 행이나 메모리 행은 유실되지 않습니다.
- 제공자 재구성은 종료된 프로세스가 있으면, 같은 호스트의 프로세스가 그 종료를 확인하거나 Kubernetes에서는 workers 리더가 해당 파드가 사라진 것을 확인할 때까지 대기합니다. 그 외의 경우, 호스트까지 사라진 프로세스(새 호스트로 이전하는 경우)는 `loom participant retire --participant <id> --yes`로 직접 폐기합니다([제공자 재구성](../architecture/provider-reconfiguration.md)).
- 속도 제한 버킷의 충전 상태는 호스트별입니다. 몇 초 안에 다시 채워지므로 새 호스트는 가득 찬 버킷으로 시작합니다.

<a id="related"></a>

## 관련 문서

- [업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md): 롤백 절차와 추가형 마이그레이션 규칙
- [유지보수](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#erasure): 삭제 saga와 그 복구
- [이력과 출처](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md): `memory.history`가
  반환하는 내용
- [장애와 복구](../architecture/failure-and-recovery.md): 구성 요소에 장애가 발생했을 때
  시스템이 동작하는 방식
