<!-- i18n: source=docs/development/migrations.md blob=bbea754e8750b1abe4dfb51af0636ead2246778b model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md), which is authoritative.

<a id="migrations"></a>

# 마이그레이션

이 페이지는 마이그레이션을 작성하는 규칙을 다룹니다. 배포 환경에서 마이그레이션을 적용하는 방법과 업그레이드 및 롤백 절차는 [업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#how-migrations-run)에 있습니다. [`schemas/README.md`](https://github.com/ClickHouse/loom/blob/main/schemas/README.md)는 `schemas/` 디렉터리의 구성을 안내하고 운영자용 도구를 나열합니다.

<a id="the-journal-and-the-ledger"></a>

## 저널과 원장

- 스키마는 `schemas/migrations/`에 있는 번호가 매겨진 SQL 파일의 저널이며, `server/core/schema/migrate.py`가 숫자 접두사 순서대로 적용합니다. `000_migrations.sql`은 원장을 만들고, `016_baseline.sql`은 시작 스키마이며, 그 이후의 모든 파일은 델타입니다.
- 적용된 각 파일은 내용의 SHA-256과 함께 `loom._migrations`에 기록됩니다. 다시 실행하면 새 파일만 적용됩니다. 내용이 바뀐 파일은 **체크섬 드리프트**이고, 저장소에 더 이상 없는 파일은 **원장 전용 행**입니다. `loom migrate --dry-run --strict`(CI 단계이자 배포 사전 점검)는 둘 중 어느 쪽이든 실패합니다. 텍스트는 그대로이고 이름만 바뀐 파일은 원장 전용 행이 아닙니다. SHA-256이 그 파일을 식별하므로, 다시 실행하지 않고 새 이름으로 기록됩니다.
- 모든 파일은 `loom.<table>`과 `loom_telemetry.<table>`을 대상으로 합니다. 러너는 각 구문을 전송할 때 이를 `LOOM_CH_DATABASE`와 `LOOM_CH_TELEMETRY_DATABASE`로 매핑하므로(`server/core/ch_names.py`), 체크섬은 작성된 그대로의 파일을 기준으로 계산됩니다. 데이터베이스는 리터럴 `'loom'`이 아니라 `currentDatabase()` 또는 바인딩된 이름으로 값을 비교합니다(`tests/unit/test_ch_names.py`). 이름이 `_`로 시작하는 테이블의 키나 TTL을 변경하거나, 그런 테이블의 이름을 바꾸거나 교환하는 파일은 [스토리지 레이아웃](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-layout.md)의 해당 행과 `tests/unit/test_storage_layout_rows.py`의 리셰이프 가드도 함께 갱신해야 합니다.

<a id="numbering"></a>

## 번호 부여

파일 이름은 `NNN_description.sql` 형식입니다. 러너는 이름이 아니라 정수 접두사를 기준으로 순서를 정하므로(`server/core/schema/journal.py`, `migration_order`), `1030_x.sql`은 `220_y.sql`보다 나중에 실행됩니다. 비어 있는 다음 번호를 사용하되 `main`과 열려 있는 pull request를 확인하고, 한 번 정한 번호는 유지하세요. 번호가 같은 파일 두 개도 적용되기는 하지만 접미사의 사전순으로 적용되며, 이는 누구도 의도하지 않은 순서입니다. 또한 리베이스로 파일 이름이 바뀌어도 다시 적용되는 것은 없습니다. `tests/unit/test_migration_numeric_order.py`는 번호가 겹치면 실패합니다.

<a id="rules-for-writing-a-migration"></a>

## 마이그레이션 작성 규칙

- **병합된 마이그레이션을 수정하거나 삭제하지 않습니다.** 각 배포의 원장(ledger)에 해당 마이그레이션의 해시가 들어 있습니다. 수정이 필요하면 새 파일로 작성합니다.
- **추가만 허용합니다.** 테이블, 컬럼, 인덱스, 프로젝션, 뷰는 추가할 수 있습니다. `server/`, `scripts/`, `bench/`, `dev/` 아래의 어떤 코드도 참조하지 않는 스킵 인덱스나 프로젝션은 삭제할 수 있습니다(이전 빌드는 최적화를 잃을 뿐 컬럼을 잃지는 않습니다). 코덱, 기본값, 테이블 설정을 변경하고, 타입을 넓히고, enum을 확장하는 것도 가능합니다. 테이블이나 컬럼을 삭제하거나 이름을 바꾸는 것, 테이블을 교체(exchange)하는 것은 절대 하지 않습니다. `tests/unit/test_migrations_additive.py`가 이를 강제합니다. 뷰와 테이블 중에서는 삽입 시점 materialized view에 대한 `DROP VIEW IF EXISTS`와 대체된 테이블에 대한 `DROP TABLE IF EXISTS`만 허용하며, 둘 다 `server/`, `scripts/`, `bench/`, `dev/` 아래의 어떤 파일도 해당 이름을 참조하지 않을 때만 가능합니다. 서비스 중인 빌드도, 운영자 도구(프로덕션 서비스를 대상으로 실행됨)도 이를 읽지 않기 때문입니다. 타입을 좁히는 것은 정규식으로 넓히는 것과 구별할 수 없으므로 리뷰에서 판단해야 합니다.
- **컬럼은 두 번의 릴리스에 걸쳐 폐기합니다.** 먼저 해당 컬럼을 더 이상 읽거나 쓰지 않는 코드를 담은 릴리스를 배포합니다. 서비스 중인 어떤 빌드도 그 컬럼을 참조하지 않게 된 후에야 삭제합니다.
- **모든 쓰기 경로가 새 컬럼을 포함해야 합니다.** `loom.memory` 또는 `loom.shared`에 컬럼을 추가할 때는 같은 변경에서 `server/memory/store/rows.py`의 행 빌더(`build_memory_version_row`, `build_embedding_row`)와 이 빌더에 데이터를 공급하는 모든 프로젝션을 함께 수정해야 합니다. 이 테이블들은 `FINAL`로 읽는 `ReplacingMergeTree(version)` 테이블이라, 컬럼을 빠뜨린 버전 행은 모든 읽기 측에서 해당 컬럼을 조용히 0으로 만듭니다. 새 `loom.memory` 컬럼은 같은 마이그레이션에서 `loom.memory_versions`와 `memory_versions_mv` 뷰의 `SELECT`에도 추가해야 하며, 그렇지 않으면 기록 이력에서 해당 컬럼이 누락됩니다(`tests/unit/test_memory_versions_schema.py`가 컬럼 목록을 비교합니다).
- **메모리 콘텐츠를 담을 수 있는 테이블**에는 삭제(erasure) saga 단계와 잔여 데이터 조사 행(`server/erasure/store/census.py`의 `_CENSUS`)도 필요합니다. 어떤 목록에도 없는 새 테이블이 있으면 `tests/unit/test_every_table_is_classified.py`가 실패합니다([테이블 추가하기](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-table.md)).
- **엔진에 이식 가능한 DDL을 작성합니다.** 벡터 컬럼은 `Array(BFloat16) CODEC(NONE)`으로 선언합니다. `Quantized` 코덱을 사용할 수 있는 러너에서는 코덱이 적용된 상태로 생성됩니다([제한된 설정](../architecture/clickhouse.md#gated-clickhouse-settings)). 엔진 이름은 일반 `MergeTree`를 사용하며, ClickHouse Cloud에서는 이를 `SharedMergeTree`로 실행합니다.

**추가만 허용하는 이유.** 마이그레이션이 실행되기 전에 서비스 중이던 빌드는 실행 후에도 자신이 알던 모든 테이블을 계속 읽고 쓸 수 있어야 합니다. 그래야 blue/green 롤백이 스키마 롤백이 아닌 프록시 전환으로 끝나고, 새 색상이 부팅 시 마이그레이션을 적용하는 동안 이전 색상이 계속 서비스할 수 있습니다. 그 대가는 여기서 치르게 됩니다. 컬럼 하나를 폐기하는 데 두 번의 릴리스가 필요하고, 키를 바꿀 때는 `ALTER` 대신 재빌드를 해야 합니다.

<a id="runner-directives"></a>

## Runner 지시문

마이그레이션 파일에는 runner가 읽는 주석 지시문을 넣을 수 있습니다.

| 지시문                              | 효과                                                                                      |
| ----------------------------------- | ----------------------------------------------------------------------------------------- |
| `-- loom: tolerate-error <code>`    | 파일이 지정한 서버 오류 하나를 견뎌 냅니다 (예: 일부 서버만 받아들이는 설정)              |
| `-- loom: rebuild-if-empty <table>` | `<table>`의 목표 정렬 키 또는 파티션 키를 선언합니다 (아래 참조)                           |
| `-- loom: baseline covers N`        | 이 파일은 번호가 N 이하이면서 저장소에 더 이상 없는 모든 파일을 대체합니다                |

**베이스라인.** 베이스라인의 번호는 N이며, 대체하는 파일 중 가장 최신 파일 뒤에 옵니다. 새 데이터베이스는 이 파일을 실행합니다. 원장(ledger)에 파일 N이 있는 데이터베이스는 이 파일을 실행하지 않고 기록만 하며, 대체된 행은 그대로 유지합니다. strict 모드는 이 행을 오류가 아니라 대체됨(superseded)으로 보고합니다. 따라서 스쿼시(squash)할 때 원장을 직접 수정할 필요가 없고, 스쿼시 이전의 빌드도 자신의 파일이 모두 적용된 상태로 확인할 수 있습니다. 원장에 대체된 파일 일부는 있지만 파일 N은 없는 데이터베이스는 중간에 멈춘 것입니다. `loom migrate`는 이를 거부하고(`stranded`) 아무것도 변경하지 않습니다. 베이스라인을 실행하면 없는 파일을 건너뛰게 되고, 기록만 하면 그 파일들의 스키마를 가진 것처럼 표시되기 때문입니다. 스쿼시 이전의 빌드로 누락된 파일을 적용한 다음 다시 마이그레이션하십시오. 스쿼시는 변경마다 하지 말고 릴리스 시점에 하십시오. 스쿼시는 `schemas/schema.sql`(유지되는 파일이 생성하는 객체를 뺀 덤프 본문)로 베이스라인을 만들고, 새 서버, 완전히 마이그레이션된 원장, 중간에 멈춘 원장에서 이를 검증합니다.

runner는 파일과 체크섬을 바꾸지 않고 적용 시점에 구문도 조정합니다. 서버가 금지하는 경우 실험적 NLP 플래그를 제거하고, `Quantized` 코덱을 쓸 수 있으면 이 코덱으로 벡터 컬럼을 만들며, `allow_suspicious_codecs`를 쓸 수 없는 경우에는 그 뒤에 연결된 범용 코덱을 제거합니다(`server/core/schema/vector_codec_ddl.py`). 또한 마이그레이션을 실행하는 사용자 소유로 모든 뷰를 만들고(`DEFINER = CURRENT_USER`: 덤프에는 자체 사용자가 지정되어 있는데 다른 서버에는 그 사용자가 없을 수 있습니다), 파일이 발행하는 모든 뮤테이션이 끝나기를 기다린 뒤 기록합니다(`LOOM_MIGRATE_MUTATION_WAIT_S`).

**사전 검사.** 파일만으로는 알 수 없는 데이터가 있을 때 실행되면 안 되는 파일은 `server/core/schema/prechecks.py`에 검사를 등록합니다. 키는 번호를 뺀 파일 이름이므로, 번호가 바뀐 파일도 검사를 유지합니다. runner는 드라이 런을 포함해 파일보다 먼저 검사를 실행합니다. 검사가 거부하면 해당 메시지와 함께 실행이 중단되고 그 파일은 아무것도 실행되지 않습니다. `097_mirror_names_reserved.sql`이 검사를 하나 사용합니다. 예약된 미러 형식의 네임스페이스가 등록되어 있거나 활성 행을 가지고 있으면 이를 거부합니다.

<a id="changing-a-sort-key-or-a-partition-key"></a>

## 정렬 키 또는 파티션 키 변경

ClickHouse는 둘 중 어느 것도 제자리에서 변경하지 않으므로, 마이그레이션은 목표 상태를 선언하고 테이블은 변경(alter)되지 않고 다시 빌드됩니다.

```sql
-- loom: rebuild-if-empty code_test_facets
CREATE TABLE loom.code_test_facets ( ...every column, unchanged... ) ... ORDER BY (...);
```

이 문장이 목표 DDL의 유일한 사본입니다. 러너(`server/core/schema/table_rebuild.py`)는 라이브 테이블의 `sorting_key`와 `partition_key`를 이 문장과 비교합니다. 일치하면 파일을 기록하고, 테이블이 비어 있으면(새로 설치한 경우) 그 자리에서 목표 DDL로 만든 쌍둥이 테이블과 교환하며, 행이 있는 테이블이면 도구 이름을 알려 주는 메시지와 함께 부팅이 실패합니다. 부팅은 행을 복사하지 않습니다. 데이터가 있는 배포에서는 운영자가 먼저 도구를 한 번에 테이블 하나씩 실행합니다.

```bash
python -P -m scripts.ops.table_rebuild --table code_test_facets --dry-run
python -P -m scripts.ops.table_rebuild --table code_test_facets --yes
```

변경을 수행하는 모든 동작(`--yes` 단독, `--rollback`, `--drop-pre-rebuild`)은 연결된 ClickHouse와 실행 계획을 출력하며, `--yes` 없이는 아무것도 변경하지 않습니다. 이 도구는 모든 행(FINAL 아님)을 제한된 배치로 `loom.<t>_rebuild`에 복사하고, 그동안 기록된 파트를 복사한 뒤, FINAL 기준의 행 수와 샘플 키들의 최신 버전을 확인하고, `EXCHANGE TABLES`로 교체합니다. 이전 테이블은 `--drop-pre-rebuild`를 실행하기 전까지 `--rollback`을 위해 `loom.<t>_pre_rebuild`로 보관됩니다. 복사하는 동안 `ALTER`가 라이브 테이블을 변경했다면 교체를 거부하고 쌍둥이 테이블을 그대로 둡니다. ClickHouse가 뮤테이션으로 실행하는 변경(컬럼 삭제, 이름 변경 또는 타입 변경, 인덱스 삭제, TTL)은 종료 코드 6, 메타데이터 시간만 바꾸는 변경(인덱스, 설정, 컬럼의 기본값 또는 코덱)은 종료 코드 3입니다. 컬럼을 추가하면 복사가 실패하거나(종료 코드 1, 교체 없음), 마지막 검증 단계 이후 종료 코드 3으로 끝납니다. 재빌드는 `ALTER`를 이어받지 않습니다. 목표 문장에 다시 반영한 뒤 `--restart`로 다시 실행하십시오. 또한 어떤 마이그레이션도 선언하지 않은 테이블은 거부합니다.
`loom migrate --dry-run --strict`는 선언된 테이블이 여전히 이전 레이아웃으로 행을 가지고 있으면 실패하므로, 배포 사전 점검에서 건너뛴 단계를 잡아냅니다.

목표는 테이블의 컬럼을 그대로 유지하며(따라서 교체는 추가적 변경입니다), 정렬 키는 이전 키의 컬럼을 순서만 바꿔 유지합니다. 이 키는 `ReplacingMergeTree`가 병합하는 기준이기도 합니다. 다른 컬럼이 포함된 키는 다른 행을 병합하게 되며, 그 이유와 함께 `tests/unit/test_migrations_additive.py`에 나열됩니다. 릴리스 노트에 재빌드를 운영자 단계로 명시하십시오([릴리스 프로세스](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes)).

<a id="postgres-migrations"></a>

## Postgres 마이그레이션

`schemas/pg_migrations/NNN_*.sql`도 같은 규칙을 따릅니다. 번호를 매기고, 추가만 하며, 병합된 뒤에는 수정하지 않습니다. 테이블에 대한 모든 `CREATE`, `ALTER`, `DROP`은 `loom.<table>`을 명시합니다. 단, 이름 변경의 새 이름은 예외이며, 여기서는 Postgres가 스키마를 받지 않습니다(`tests/unit/test_storage_layout_rows.py`). 부팅 시에는 Postgres advisory lock을 잡고 마이그레이션을 적용하며, 각 파일은 해당 ledger 행과 함께 하나의 트랜잭션에서 실행됩니다. 마이그레이션은 테이블 락을 최대 5초까지만 기다립니다(`lock_timeout`, 이후 재시도). 따라서 `ALTER`가 긴 트랜잭션 뒤에서 그 테이블의 모든 쿼리를 대기시키는 일은 없습니다. 빌드는 이후 빌드의 파일이 들어 있는 ledger도 그대로 사용하며, 이때 해당 파일 이름을 담은 경고를 남깁니다. 반면 빌드가 가지고 있지 않은 ledger 파일이 자신의 마지막 파일 번호 이하이거나, 체크섬이 바뀐 파일이 있으면 부팅을 거부합니다.

모든 파일이 하나의 트랜잭션에서 실행되므로, 마이그레이션은 큰 테이블에 인덱스를 만들지 않습니다. 빌드가 끝날 때까지 해당 테이블의 쓰기가 막혀 모든 커밋이 503으로 응답하기 때문입니다. 이런 인덱스는 트랜잭션 밖에서 `CREATE INDEX CONCURRENTLY`로 만들어야 하는데, advisory lock을 기다리는 부팅과는 교착 상태에 빠집니다(대기 중인 세션이 스냅샷을 쥐고 있어 빌드가 그 세션이 끝나기를 기다립니다). 따라서 이런 파일을 추가하는 변경은 `pg_try_advisory_lock`을 재시도하는 방식으로 락도 함께 잡습니다. 테이블에 처음 쓰기를 하는 릴리스가 그 파일을 포함하는 경우에는, 파일이 실행되는 어느 환경에서든 테이블이 작으므로 파일의 트랜잭션 안에서 인덱스를 만들어도 됩니다(`010_revision_delivered_at.sql`).

<a id="schema-dump-and-check"></a>

## 스키마 덤프와 검사

`schemas/schema.sql`은 모든 마이그레이션을 적용한 결과를 덤프한 파일입니다. 현재 스키마의 형태를 이해하려면 이 파일을 읽고, 직접 수정하지 마세요. 마이그레이션을 변경한 뒤에는 릴리스 CI가 테스트하는 새 ClickHouse(`clickhouse/clickhouse-server:latest`)를 대상으로 이 파일을 다시 생성합니다:

```bash
bash dev/ci_start_clickhouse.sh     # that image with Loom's server config
make migrate
make schema-dump                    # rewrites schemas/schema.sql only when the dump differs
make schema-check                   # exit 1 with the head of the diff; writes nothing
```

덤프에 실패하면 파일은 이전 상태 그대로 남습니다. ClickHouse 버전이 다르거나, `Quantized` 코덱 또는 그 `LZ4` 체인을 켤 수 없는 서버이거나, 재구축용 트윈 테이블이 있는 데이터베이스에서는 덤프 결과가 달라집니다. 이 때문에 고정된 컨테이너를 사용해야 합니다. 내부 테이블(`_migrations`, 이름이 `_` 또는 `.`로 시작하는 테이블)은 제외됩니다. CI의 `schema-check` 작업은 새 서버에서 모든 마이그레이션을 재실행하고, 엄격한 dry run을 수행한 다음 `make schema-check`를 실행합니다.

CI는 `latest`를 따르므로, DDL을 다르게 출력하는 새 ClickHouse 릴리스가 나오면 Loom 변경이 없어도 `schema-check`가 실패(red)합니다. 작업이 출력하는 diff를 확인하세요. 서식 차이일 뿐이라면 위의 네 명령을 실행하고(새 릴리스를 pull합니다) `schemas/schema.sql`만 따로 커밋합니다. 의미가 바뀐 경우(타입, 기본값, 엔진 설정)는 덤프로 덮어쓰기 전에 먼저 원인을 조사해야 하는 ClickHouse 동작 변경입니다. `LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D`를 지정하면 깨끗하게 덤프되었던 마지막 릴리스에서 위 명령을 실행해 비교할 수 있습니다.

<a id="checklist"></a>

## 체크리스트

1. 사용 가능한 다음 번호를 사용하고, 파일은 추가 방식(additive)으로 작성합니다.
2. 변경이 행 빌더, `loom.memory_versions`, 삭제 집계(erasure census), 분석기 케이스에 영향을 준다면 해당 항목을 업데이트합니다.
3. 로컬 ClickHouse에 대해 `make migrate`를 실행한 다음 `make schema-dump`를 실행하고, `schemas/schema.sql`을 마이그레이션과 함께 커밋합니다.
4. `make test-unit`(추가 방식, 번호 지정, 스키마 분류 테스트)과, 새로운 형태를 읽는 경로에 대한 통합 테스트 모음을 실행합니다.
5. 운영자가 수행해야 하는 단계(재빌드, 백필)가 있다면 릴리스 노트에 기록합니다.

<a id="related"></a>

## 관련 문서

- [업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md): 배포 환경에서 러너가 파일을 적용하는 방법
- [ClickHouse](../architecture/clickhouse.md)
- [스키마 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)
