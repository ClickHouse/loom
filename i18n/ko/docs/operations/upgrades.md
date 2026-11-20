<!-- i18n: source=docs/operations/upgrades.md blob=8caf8c789d2fc39ab89d9830d45a7e1229be2051 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md), which is authoritative.

<a id="upgrades"></a>

# 업그레이드

업그레이드는 이미지(그리고 상태를 보관하지 않는 문서 추출기의 이미지)를 교체하고, 새 빌드에 포함된 마이그레이션을 적용합니다. 그 외의 상태는 이동하지 않습니다. 영속적인 데이터는 모두 ClickHouse에 있으며(`LOOM_DOCUMENT_OBJECT_STORE=s3`로 기록된 원본 문서의 경우 문서 버킷에도 있습니다), 코드 미러는 그대로 마운트됩니다. 이 페이지는 운영자를 위한 내용입니다. 클라이언트가 알 수 없는 필드와 제거된 필드를 어떻게 처리하는지는 [HTTP API 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)에 있습니다. 어떤 릴리스가 수정 사항을 받는지, 그리고 어떤 SDK가 어떤 서버와 호환되는지는 [지원되는 버전](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md)에서 확인하세요.

<a id="how-migrations-run"></a>

## 마이그레이션 실행 방식

저널, 번호 지정, 러너 지시문, 마이그레이션 작성 규칙은
[마이그레이션](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)에 있습니다. 운영자가 알아야 할 내용은 다음과 같습니다.

- **부팅 시.** 서버는 준비 상태를 보고하거나 워커를 시작하기 전에 대기 중인 파일을 적용합니다
  (`LOOM_BOOT_MIGRATE=true`, 기본값). 어떤 컨테이너의 프로세스든 모두
  `loom._migration_lock`의 `KeeperMap` 락을 획득하므로 각 파일은 한 번만 적용됩니다. Keeper가 없는 서버는
  거부됩니다.
- **프로바이더 재구축.** 대기 중인 파일이 있는 부팅은 먼저 프로바이더 참여자로 등록한 다음, 프로바이더 구성 락(Keeper 키 하나, 해당 확인 동안에만 유지) 아래에서 완료되지 않은 프로바이더 재구축이 없는지 확인하고, 있으면 마이그레이션을 거부합니다:
  `Finish the provider rebuild before applying schema migrations`. 그 후 파일은 마이그레이션 락만 잡은 상태에서 적용됩니다. 그 사이에 시작된 재구성은 부팅 중인 프로세스가 이를 확인(acknowledge)할 때까지 대기하므로, 재구축이 마이그레이션과 나란히 실행되는 일은 없습니다.
- **마이그레이션 또는 재구성 중에 종료된 컨테이너**는 다른 컨테이너를 잠시 동안만 막습니다.
  마이그레이션 락은 보유자가 갱신을 멈추면(`ttl_s`) 인계됩니다.
  프로바이더 락(TTL 60초, 20초마다 갱신)은 만료되고 보유자의 참여자 행도 만료되면 인계됩니다. 즉 60초 동안 하트비트가 없고(10초마다 하나) 어느 노드의 `system.processes`에도 해당 id가 태그된 쿼리가 없을 때입니다. 함께 부팅하는 컨테이너는 실패하는 대신 프로바이더 락을 최대 120초 기다립니다. 보유자에게 참여자 행이 없는 락은 해당 프로세스가 중지되었음을 확인한 뒤 `loom provider-lock clear --holder <holder>`로 해제합니다. 보유자가 갱신하거나 하트비트를 보내거나 쿼리를 실행 중이면 이 명령은 거부됩니다.
- **뮤테이션.** 파일은 뮤테이션이 모두 끝난 후에야 기록됩니다. 뮤테이션이 실패하거나
  `LOOM_MIGRATE_MUTATION_WAIT_S`(기본값 600)를 넘겨 실행되면 해당 파일이 실패하고,
  로그에 파일 이름이 표시된 채 부팅이 중단됩니다.
- **키 변경.** 정렬 키나 파티션 키를 바꾸는 마이그레이션은 빈 테이블이면 직접 재구축하고, 행이 있는 테이블에서는 부팅을 실패시킵니다. 배포 전에 해당 테이블에 `scripts/ops/table_rebuild.py`를 실행하세요. 엄격한 드라이 런(dry run)이 이를 잡아냅니다.
- **새로 설치.** 새로 설치와 업그레이드는 하나의 경로입니다. 서버는 원장에 없는 것을 모두 재실행합니다. 빈 데이터베이스에서는 저널 전체이며 몇 분이 걸립니다.
- **중단된 원장.** 베이스라인이 대체하는 파일들의 중간에서 원장이 멈춘 데이터베이스는 거부됩니다(`stranded`). 먼저 해당 베이스라인 이전의 빌드로 마이그레이션하세요.
- **번호가 바뀐 파일.** 저장소에 더 이상 파일이 없는 원장 행은
  `ledger-only`이며, `--strict`는 이 경우 1로 종료합니다. 단, 기록된 SHA-256이 현재 파일의 것과 같으면 예외입니다. 이는 새 번호로 바뀐 같은 파일입니다. 러너는 이를 이름 변경으로 보고하고, 대기 중이면 다시 실행하지 않고 새 이름만 기록하며, `--strict`는 통과합니다. 이전 이름으로 실행된 후 수정된 파일은 SHA-256이 다르므로 ledger-only로 남습니다. 새 이름은 대기 중인 파일로 실행되고, 이전 행은 직접 제거합니다
  ([문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)).
  그때까지 `/v1/health`는 이전 행을 `schema.ledger_only` 아래에 표시합니다.

CLI로 같은 러너를 직접 실행할 수 있습니다.

```bash
loom migrate --status              # applied and pending; changes nothing
loom migrate --dry-run --strict    # pending files; exit 1 on ledger-only rows (not renames) or checksum drift
loom migrate                       # apply pending migrations
loom migrate --target N            # apply through file number N only
```

`make migrate`, `make migrate-status`, `make migrate-dry`는 체크아웃된 저장소에서 같은 모듈을 실행합니다.

<a id="upgrade-procedure"></a>

## 업그레이드 절차

1. 릴리스의 [GitHub 릴리스](https://github.com/ClickHouse/loom/releases) 노트에서 운영자가 해야 할 단계(새 설정, 배포에서 실행해야 하는 백필)와 클라이언트에 보이는 와이어 변경 사항을 확인합니다.
2. 새 빌드의 체크아웃에서 프로덕션 ClickHouse를 대상으로 `loom migrate --dry-run --strict`를 실행합니다. 이 명령은 부팅 시 적용될 항목을 나열하고, 원장(ledger)에 문제가 있으면 중단합니다([문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)).
   릴리스에 테이블 재구축이 명시되어 있으면 지금 해당 테이블에 대해 `scripts/ops/table_rebuild.py`를 실행합니다. Postgres 권한 방식(authority) 배포가 지식 문서 헤드를 Postgres에 유지하는 빌드로 옮겨 가는 경우, 지금 직접 `LOOM_RETENTION_DOCUMENTS_DAYS`를 해제합니다. 새 빌드는 이 값이 설정되어 있으면 부팅을 거부하며, 해당 권한 방식에서는 어떤 보존 TTL도 `loom.documents`에 적용되지 않고 네임스페이스의 `retention_days`도 `loom.chunks`에 적용되지 않습니다
   ([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md), 규칙 38). 세션을 표시하고 org 트리거(규칙 47)를 가진 빌드로 옮겨 가는 [셀](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)은 먼저 `LOOM_PG_DSN`이 풀러(pooler)를 거치지 않고 Postgres를 직접 가리키게 하며([Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)), 준비된 뒤 `LOOM_CELL`을 해제해 되돌려진 이후의 셀은 먼저 정리합니다
   ([문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)). 새 빌드는 프로세스를 그 상태에 따라 제한합니다. 이미 준비된 이후의 셀에서는 새 빌드의 첫 마이그레이션 부팅이 org 트리거를 생성하므로, 그곳에서 계속 실행 중인 이전 파드는 교체될 때까지 테넌트 org 행의 모든 삽입이 거부됩니다(호출자에게는 매핑되지 않은 오류). 따라서 셀은 한 번에 롤아웃합니다.
3. 새 이미지를 배포합니다. 처음 부팅한 컨테이너가 마이그레이션을 적용하고, 나머지는 이를 기다린 뒤 대기 중인 항목이 없음을 확인합니다.
4. 트래픽을 보내기 전에 `/v1/health`가 `status: ok`와 빈 `schema.pending`을 보고할 때까지 기다립니다. [무중단 쌍(zero-downtime pair)](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)은 2~4단계를 대신 수행하지만, 2단계에서 언급한 설정 해제는 예외입니다. `redeploy.sh`는 사전 점검에서 strict dry run을 실행하고, 새 색상이 준비된 뒤에만 전환합니다.
5. 릴리스에 백필이 있으면 실행합니다([유지보수](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#backfills-and-repairs)).
6. 릴리스에 플러그인 변경이 언급되어 있으면 클라이언트 플러그인을 업데이트합니다. 훅은 클라이언트 머신에서 실행되며 클라이언트의 플러그인 관리자를 통해서만 바뀝니다(Claude Code는 `claude plugin update loom@loom`). 서버 배포로는 바뀌지 않습니다.
7. 마지막 이전 레플리카가 중지되면, 롤아웃 중에 완료된 삭제(erasure)를 다시 검증합니다: `loom erasure-re-verify --completed-since <when step 3 began> --resume`. 이전 레플리카의 saga는 새 빌드가 추가한 테이블을 알지 못합니다
   ([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md), 규칙 14). 이 명령은 고아 행 링크만 삭제합니다. 즉 각 삭제 범위에서 `entity_triple_rows`와 `entity_edge_rows`의 행 중, 해당 메모리에 유효한 행이 없고 삭제 census 이후에 기록된 행도 없는 것입니다. `--resume` 없이 실행하면 개수만 세고, 남아 있으면 종료 코드 1로 끝납니다. 삭제 이후에 기록된 데이터는 그대로 유지됩니다.
8. 이전 빌드가 지원하지 못하는 기능은 모든 레플리카가 새 빌드를 실행한 뒤에만 켭니다. `memory_extract_agent_learnings`는 이전 빌드가 `loom.learnings_supporters`를 삭제하지 않기 때문이고
   ([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md), 규칙 23), `LOOM_DOCUMENT_OBJECT_STORE=s3`는 이전 빌드가 버킷 행의 빈 `data`를 읽고 다운로드에 `410 BLOB_GONE`으로 응답하기 때문이며(규칙 9), `LOOM_PG_PAYLOAD_CLEAR_ENABLED`는 이전 빌드가 늦은 전달에 스탬프를 찍지 않기 때문에 새 빌드가 단독으로 서비스한 뒤 정리 기간(clearing horizon) 하나가 지나기 전에는 켜지 않습니다(규칙 43).
9. `LOOM_CODE_ASK_MEMORY_NAMESPACE`가 지금 또는 이전 어느 시점에든 설정된 배포가, 캐시된 `code.ask` 답변이 인용한 메모리를 명시하지 않는 빌드(`086_code_answer_memory_scope.sql` 이전)에서 명시하는 빌드로 옮겨 가는 경우(롤백 후 다시 롤 포워드하는 경우 포함): 새 빌드가 단독으로 서비스하고 프로바이더 재구축이 실행 중이지 않을 때 `TRUNCATE TABLE <LOOM_CH_DATABASE>.code_answers`(기본값은 `loom.code_answers`)로 답변 캐시를 비웁니다. 이전 빌드의 답변과 tombstone은 메모리를 명시하지 않고, 롤아웃 중 이전 빌드가 실행한 삭제나 redact는 이 테이블을 알지 못했으므로
   ([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md), 규칙 36) 어떤 saga도 그 내용을 찾을 수 없습니다. 재구축은 테이블의 복사본을 그 위에 게시하므로, truncate 도중에 실행 중이던 재구축은 행을 되살립니다. truncate는 복제 테이블의 모든 레플리카에서 파트를 즉시 제거하며 기다릴 mutation이 없고, loom 사용자에게 해당 권한이 있습니다. 비워진 질문은 다음에 질문할 때 각각 한 번의 합성 비용이 듭니다.
10. 사용자 삭제가 사용자 메모리의 통합 요약이 사용한 항목까지 포함하는 빌드로 옮겨 가는 경우(롤백 후 다시 롤 포워드하는 경우 포함): 새 빌드가 단독으로 서비스하면, 그 이전에 `purge` 없이 요청된 모든 사용자 삭제(`loom.erasure_requests`의 `user_id` 행, 90일 보관)를 다시 요청합니다. 새 빌드가 완료한 것도 포함합니다. 이전 빌드의 saga는 요약의 이력, 청크, 벡터, 그래프 행, 공유 복사본을 남겼습니다
    ([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md),
    규칙 37).
    새 saga는 그 이후 사용자가 기록한 것도 함께 삭제하므로, 유효한 메모리 행이 없는 사용자에게, 또는 그것이 필요한 경우에만 요청합니다. 이전 빌드가 완료한 사용자 purge도 같은 행을 남겼고, 사용자의 키가 사라졌으므로 이후의 사용자 삭제로는 찾을 수 없습니다. `shared.tombstone` 이후까지 진행된 purge는 어느 빌드가 완료했든 사용자 자신의 메모리에 대한 공유 복사본의 검색 행도 남겼습니다. 이전 빌드가 `agent="*"`로 승격한 공유 복사본은 원본 에이전트로 `*`를 명시하므로, 복사 원본 사용자의 어떤 삭제도 그것을 찾지 못할 수 있습니다. 이를 나열해 하나씩 검토하고, 삭제된 사용자의 텍스트를 담은 것은 `memory.demote`로 처리합니다:

    ```sql
    SELECT org, namespace, memory_key FROM loom.shared FINAL
    WHERE tombstoned_at IS NULL AND arrayExists(x -> splitByChar('/', x)[3] = '*', promoted_from)
    ```

11. Postgres 권한 방식 배포가 지식 문서 헤드를 Postgres에 유지하는 빌드(`002_knowledge_documents.sql`)로 옮겨 가는 경우(롤백 후 다시 롤 포워드하는 경우 포함): 새 빌드가 단독으로 서비스하면, 새 빌드가 처음 서비스한 이후 이전 빌드의 레플리카가 서비스하는 동안 완료된 모든 삭제를 다시 실행합니다. 이전 빌드의 삭제는 헤드나 전달(delivery)을 제거하지 않습니다. 삭제된 문서의 텍스트는 삭제가 다시 실행될 때까지 `loom.knowledge_heads`에 남고, 삭제가 남긴 전달은 전달 워커가 다시 게시합니다
    ([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md), 규칙 38). 보류된(parked) 전달을 재시도하기 전에 수행합니다.
12. Postgres 권한 방식 배포가 지식 수집 작업(`005_knowledge_jobs.sql`) 또는 데이터셋 카탈로그(`007_knowledge_datasets.sql`)를 Postgres에 유지하는 빌드로 옮겨 가는 경우(롤백 후 다시 롤 포워드하는 경우 포함): 새 빌드가 단독으로 서비스하면, 새 빌드가 처음 서비스한 이후 이전 빌드의 레플리카가 서비스하는 동안 완료된 모든 삭제를 다시 실행합니다. 이전 빌드의 삭제는 `loom.knowledge_jobs`의 작업도, `loom.knowledge_datasets`의 카탈로그 행도 제거하지 않습니다. 새 빌드의 워커는 그런 작업을 `loom.knowledge_ingestions`로 다시 투영해 실행하고, 카탈로그 행은 삭제가 다시 실행될 때까지 남습니다([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md), 규칙
    40 및 42). 11단계의 실행이 헤드, 작업, 카탈로그 행을 모두 다루므로 삭제마다 한 번이면 충분합니다. 또한 그 기간에 이전 빌드가 응답한 `knowledge.delete`와 `knowledge.bulk_delete`도 다시 요청합니다. 이들은 소스의 데이터셋을 `loom.datasets`에서만 제거했습니다.
13. 모든 네임스페이스에서 문서 소스를 유지하는 빌드로 옮겨 가는 경우(롤백 후 다시 롤 포워드하는 경우 포함): 새 빌드가 단독으로 서비스하면, 이전 빌드의 `namespace.delete`, `collection.delete` 또는 네임스페이스 삭제가 활성 상태로 남긴 소스를 폐기합니다([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md), 규칙 44). 삭제된 네임스페이스의 소스:

    ```sql
    SELECT s.org, s.namespace, s.name
    FROM (SELECT org, namespace, name FROM loom.knowledge_sources FINAL WHERE active) AS s
    INNER JOIN (SELECT org, namespace FROM loom.namespaces FINAL WHERE NOT active) AS n
        ON s.org = n.org AND s.namespace = n.namespace
    ```

    그리고 네임스페이스 삭제가 완료되기 전에 등록되었지만 폐기되지 않은 소스(이전 빌드가 실행한 삭제이거나, 새 빌드의 삭제가 실행되는 동안 이전 빌드가 추가한 소스):

    ```sql
    SELECT s.org, s.namespace, s.name, e.id
    FROM (
        SELECT org, namespace, name, active, created_at, updated_at
        FROM loom.knowledge_sources FINAL
    ) AS s
    INNER JOIN (
        SELECT id, org, completed_at,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    ) AS e ON s.org = e.org AND s.namespace = e.namespace
    WHERE s.created_at <= e.completed_at AND (s.active OR s.updated_at > e.completed_at)
    ```

    첫 번째 쿼리의 경우 나열된 각 소스를 제거하거나(`knowledge.source.remove`), 새 빌드로 해당 네임스페이스를 다시 삭제하면 폐기됩니다. 두 번째 쿼리의 경우 나열된 각 소스를 `knowledge.source.remove`와 `delete_documents=true`로 제거하면 소스가 다시 기록한 문서가 폐기되며(이미 폐기된 소스도 받아들이므로, 네임스페이스를 나중에 삭제한 경우에도 사용할 수 있습니다), 소스에서 추출한 결정 행은 `memory.delete`로 삭제합니다. 새 빌드의 네임스페이스 삭제는 두 가지를 모두 수행하지만 첫 삭제 이후 해당 네임스페이스에 기록된 모든 것도 함께 삭제하므로, 그 이후 아무도 기록하지 않은 네임스페이스에만 다시 요청합니다. 삭제 이후에 등록된 소스는 나열되지 않습니다. 삭제가 그 소스를 다룬 적이 없기 때문입니다. 두 번째 목록은 한 번만 처리하는 작업 목록입니다. 위와 같이 제거한 소스는 제거 행이 삭제보다 새롭기 때문에 계속 나열됩니다.

    이전 빌드가 시작하고 새 빌드가 끝낸 네임스페이스 삭제는 여기서 할 일이 없습니다. 최종 census가 소스가 다시 기록한 문서와 결정을 찾아 해당 단계를 다시 열기 때문에, saga는 그것들이 사라진 뒤에야 완료됩니다.

14. 네임스페이스 삭제가 `loom.shared`까지 미치는 빌드로 옮겨 가는 경우(롤백 후 다시 롤 포워드하는 경우 포함): 새 빌드가 단독으로 서비스하면, 이전 빌드의 네임스페이스 삭제가 남긴 승격된 행을 정리합니다([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md),
    규칙 45). 아래 쿼리는 원장(90일 보관)에 있는 `namespace` 또는 `namespace_list` 삭제가 다룬 네임스페이스 중 승격된 행이 있는 것을 나열하며, purge 없는 마지막 삭제(`erased_at`)와 purge가 있는 마지막 삭제(`purged_at`)가 census를 실행한 시각을 함께 보여 줍니다. 아래 문장은 그 시각(초) 이전에 승격된 행을 대상으로 하므로, census 이후에 승격된 행은 남고, census와 같은 초 안에서 더 먼저 승격된 행도 남을 수 있습니다. `promoted_at`의 초 단위 값으로는 이를 구분할 수 없기 때문이며, 새 빌드의 단계는 자신이 실행한 삭제에서 그런 행을 이미 처리했습니다.
    purge는 `namespace_list`만 수행하며, 이전 빌드의 `namespace` 행에는 요청된 플래그가 들어 있을 수 있습니다.

    ```sql
    SELECT org, namespace,
           maxIf(counted_at, NOT purged) AS erased_at,
           maxIf(counted_at, purged) AS purged_at
    FROM (
        -- The census's time: a row promoted after it was never in the erasure's reach.
        SELECT org, ifNull(verified_at, completed_at) AS counted_at,
               scope_type = 'namespace_list' AND purge AS purged,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    )
    -- Only a namespace that holds a promoted row has anything to clear.
    WHERE (org, namespace) IN (SELECT org, namespace FROM loom.shared)
    GROUP BY org, namespace
    ```

    각 항목에 대해, 삭제의 census 이전에 승격된 모든 버전(같은 키의 나중 승격 때문에 `FINAL`에서 가려진 버전 포함)을 대상으로 새 빌드의 단계가 실행하는 것을 실행합니다. `purged_at`이 설정된 경우 삭제합니다:

    ```sql
    ALTER TABLE loom.shared DELETE
    WHERE org = '<org>' AND namespace = '<namespace>' AND promoted_at < '<purged_at>'
    SETTINGS mutations_sync = 2
    ```

    `erased_at`이 설정된 경우 남아 있는 유효한 행을 tombstone 처리합니다:

    ```sql
    ALTER TABLE loom.shared UPDATE tombstoned_at = toDateTime('<the time you run it>')
    WHERE org = '<org>' AND namespace = '<namespace>' AND tombstoned_at IS NULL
      AND promoted_at < '<erased_at>'
    SETTINGS mutations_sync = 2
    ```

    이 설정은 각 문장이 모든 레플리카에서 끝날 때까지 기다립니다. 시각은 단계가 바인딩하는 방식과 같이 리터럴로 작성합니다. 복제 테이블은 `now()` 같은 비결정적 함수를 호출하는 mutation을 거부하기 때문입니다. 원장의 90일보다 이전에 요청된 네임스페이스 삭제는 나열되지 않으며, 그것이 남긴 승격된 행은 유효한 네임스페이스의 행과 구분할 수 없으므로 이전 빌드가 남긴 상태 그대로 유지됩니다.

15. 조직(org)을 선언하는 모든 CLI 명령(코드 인덱스 명령, 임포트)이 자신의 배포를 확인하는 빌드로 이동하는 경우: 어느 권한 주체(authority)에서든 해당 ClickHouse 사용자에게 `loom.storage_authority`에 대한 `SELECT`가 필요하며, 명령은 다른 권한 주체를 기록한 배포에 대해서는 실행을 거부합니다. Postgres 권한 주체에서는 `LOOM_PG_DSN`도 필요하며, 셀 상태를 읽습니다(`LOOM_CELL`이 설정되지 않은 경우, 별도의 세션에서 읽습니다). `LOOM_CELL`이 설정된 경우, 어떤 조직에도 쓰지 않지만 셀의 저장소를 변경하는 명령(`loom settings set`, `loom secrets reseal`, `loom code compact` 등)이나 접근 권한을 취소하는 명령도 같은 방식으로 자신의 배포를 확인하고, 별도의 세션에서 셀의 `first` 행을 읽습니다. 해당 명령이 접속하는 Postgres 역할에 다음을 부여하십시오:
    - `LOOM_CELL`이 설정되지 않은 경우: 스키마 `loom`에 대한 `USAGE`, `loom.cell_first`, `loom.tenancy_mode`, `loom.authority`에 대한 `SELECT`, `loom.cell_self`에 대한 `SELECT (updated_at)`, `loom.tenancy`에 대한 `SELECT (since)`(컬럼 단위 권한은 핑거프린트와 조직 정보를 역할이 접근할 수 없게 합니다), 그리고 `LOOM_PG_POOL_MAX_SIZE`에 동시에 실행되는 이러한 명령의 수를 곱한 값 이상의 `CONNECTION LIMIT`(각 명령은 쓰기를 위해 자신의 풀을 따로 엽니다. 상태 읽기는 한도가 가득 찬 경우 잠시 재시도한 뒤 거부합니다);
    - `LOOM_CELL`이 설정된 경우: 위와 동일하되 `loom.cell_self`와 `loom.tenancy`에는 전체에 대한 `SELECT`를 부여하고, 첫 번째 셀이 `recording` 상태일 때 새 조직을 기록하려면 `loom.tenancy`에 대한 `INSERT`와 `loom.tenancy_mode`에 대한 `UPDATE (since)`를 부여합니다. 셀이 연결될 때 `LOOM_CELL_ROUTER_TOKEN`으로 이 둘을 모두 회수하십시오. 이 명령의 문서 버킷 설정(`LOOM_DOCUMENT_S3_BUCKET`, `LOOM_DOCUMENT_S3_PREFIX`)은 서버의 설정과 같아야 합니다.

    컬럼 단위 권한만 가진 역할이 `recording` 단계를 지난 셀에 쓰기를 시도하면 `LT001`이 아니라 권한 오류(SQLSTATE 42501)로 거부됩니다.

16. 첫 번째 셀을 연결(attach)하기 전에, 모든 CLI 및 CI 이미지는 이 상태 읽기를 포함한 빌드를 실행해야 하며, 조직에 쓰는 모든 CLI에는 `LOOM_CELL`이 설정되어 있어야 합니다. CLI는 하트비트를 보내지 않으므로 연결 단계에서 이를 확인할 수 없으며, 이전 빌드의 CLI는 `enforced` 셀에서 거부되지 않습니다([셀](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#failure-modes)).

17. 첫 번째 셀의 프로세스가 타이머에 따라 라우터에서 자신의 셀 상태를 읽는 빌드로 이동하는 경우: 어느 셀의 프로세스보다 먼저 모든 라우터 레플리카(`loom router serve`)가 이 빌드를 실행해야 합니다. 준비된 첫 번째 셀의 각 프로세스는 첫 셀 상태 읽기부터 `serving`을 읽을 때까지 라우터의 상태 경로에 요청하며, 이전 빌드의 라우터는 이러한 호출마다 디렉터리를 읽습니다. 즉 서버 프로세스당 `min(LOOM_INSTANCE_HEARTBEAT_SECONDS, LOOM_TENANCY_MODE_CACHE_S)`마다 약 한 번 읽으므로, 기본값에서 워커 8개를 가진 파드 1,000개라면 초당 1,600번 읽습니다. 이 빌드의 라우터는 레플리카당 `LOOM_ROUTER_DIRECTORY_CACHE_S`마다 한 번의 읽기로 이를 처리합니다([셀](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#the-heartbeats-keys)).

마이그레이션이 적용되는 동안에도 이전 빌드는 계속 서비스합니다. 모든 마이그레이션은 추가적(additive)이기 때문입니다. 두 빌드가 나란히 서비스할 수 있게 하는 규칙은 [롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)에 있습니다.

<a id="rollback"></a>

## 롤백

이전 이미지를 다시 배포합니다. 쌍으로 배포한 경우에는 `./redeploy.sh --rollback`을 사용합니다. 어시스턴트 턴 학습 내용(assistant-turn learnings)이 없는 빌드로 롤백하기 전에 `memory_extract_agent_learnings`를 끄고, `loom.learnings_supporters`의 행과 `loom.learnings_queue`의 `assistant_turn` 행을 삭제합니다
([추출](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#rules-the-assistant-states-about-itself)).
이전 빌드는 승격된 봇 규칙을 해당 봇에만 제공하지 않고 네임스페이스의 모든 에이전트에 제공합니다. 이를 막으려면 롤백하기 전에 `knowledge.delete`로 `source-kind:assistant_turn` 태그가 붙은 문서를 삭제합니다. 그렇지 않으면 더 넓은 범위로 제공되는 것을 감수해야 합니다.
네임스페이스 가이드가 생긴 이후에 이전 빌드가 실행한 네임스페이스 삭제는 해당 네임스페이스의 가이드를 남깁니다. 더 새로운 빌드가 다시 서비스를 제공하면 삭제를 다시 실행합니다
([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md), 규칙 24). 이전 빌드가 실행한 네임스페이스 삭제는 해당 네임스페이스의 승격된 행을 활성 상태로 남깁니다. 더 새로운 빌드가 다시 서비스를 제공하면 업그레이드 절차의 14단계에서 이 행을 확인할 수 있습니다(규칙 45).
봇 토큰 최댓값이 없는 빌드로 롤백하면 모든 봇의 토큰이 한 시간 동안 유지되며, 그 빌드가 저장하는 봇은 최댓값으로 3600을 유지합니다. 더 새로운 빌드가 서비스를 제공하면 더 짧거나 긴 값을 다시 적용합니다
([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md), 규칙 26).
이전 빌드가 실행한 사용자 삭제는 사용자의 메모리 요약이 반영한 내용을 남깁니다. 더 새로운 빌드가 서비스를 제공하면 10단계와 같이 `purge` 없이 실행된 삭제를 각각 다시 요청합니다. 완료된 purge는 다시 찾을 수 없습니다(규칙 37).
카탈로그 행 중 하나라도 문서 버킷의 객체를 가리키는 동안에는 문서 버킷을 추가한 빌드보다 이전으로 롤백하지 마십시오. 더 오래된 빌드는 해당 다운로드에 `410 BLOB_GONE`으로 응답하고, 삭제 시 행만 지울 뿐 객체에는 접근하지 못하며, 정리 작업(sweep)이 버킷을 순회하지 않습니다([원본이 저장되는 위치](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#where-originals-are-stored)).
`code.ask`에 메모리 네임스페이스가 바인딩된 상태에서 `086_code_answer_memory_scope.sql` 이전 빌드로 롤백한 후에는 9단계처럼 응답 캐시를 두 번 비웁니다. 한 번은 더 오래된 빌드가 단독으로 서비스를 제공할 때, 다른 한 번은 더 새로운 빌드가 다시 서비스를 제공할 때입니다. 마이그레이션은 이미 원장에 기록되어 있으므로 롤포워드 시 두 번째 비우기를 알려 주는 적용 작업은 없습니다.
롤백하기 전에 `LOOM_PG_PAYLOAD_CLEAR_ENABLED`를 끄고, 더 새로운 빌드가 다시 단독으로 서비스를 제공한 뒤 정리 기준 시점(clearing horizon) 하나가 지나기 전에는 다시 켜지 마십시오. 정리 패스가 한 번 실행된 후에는 `010_revision_delivered_at.sql`을 포함한 빌드로만 `loom reconcile`을 실행하고, 그 이후에 해당 빌드로 배포를 시작하며, 더 새로운 빌드에 정리된 리비전에 대해 가져갈 수 있는 전달(delivery)이 남아 있지 않을 때만 롤백합니다. 다음 쿼리가 0을 반환하면 그 상태입니다(규칙 43):

```sql
SELECT count(*) FROM loom.memory_deliveries d
JOIN loom.memory_revisions r USING (org, namespace, seq)
WHERE r.row IS NULL AND NOT d.settled AND d.lease_until <> 'infinity';
```

0이 될 때까지는 더 새로운 빌드가 해당 전달을 처리하도록 둡니다. 이전 빌드는 보류된(parked) 전달을 가져가지 않으므로 보류된 전달은 그대로 두어도 됩니다. 이전 빌드가 서비스를 제공하는 동안 재시도는 정리된 리비전을 제외하며
([Postgres 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#what-the-boot-checks)), `loom reconcile`은 실행하지 않습니다. 실행하면 reconcile의 홀드가 이전 빌드에 해제되기 때문입니다.
준비된 셀(`loom.cell_first` 행이 있는 셀)을 [셀](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md) org 트리거가 없는 빌드로 롤백하기 전에, 더 새로운 빌드의 모든 파드를 중지한 다음 [LOOM_CELL이 설정되지 않은 프로세스는 차단됩니다](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)의 트리거 삭제만 실행합니다. 상태 트랜잭션은 실행하지 않습니다(셀은 준비된 상태로 유지됩니다). 롤백이 끝나기 전에 더 새로운 파드가 마이그레이션을 켠 채 부팅되면 트리거 삭제를 다시 실행합니다. 이전 빌드의 세션은 테넌시 마커를 보내지 않으므로, 더 새로운 빌드가 트리거와 `loom.tenancy` 가드를 만든 뒤에는 셀에 테넌시 행이 없는 org에 대한 쓰기가 모두 거부되고(attach 이전의 이후 셀에서는 모든 테넌트 org), 새로 배치된 org의 테넌시 행도 거부되며, 그 밖의 쓰기에는 트리거의 상태 읽기 비용이 발생합니다.
되돌릴 마이그레이션은 없습니다. 이전 빌드는 더 새로운 스키마를 읽고 씁니다. 이전 빌드의 버전이 `loom.memory`와 다른 `ReplacingMergeTree` 메모리 테이블에 쓸 때는 자신이 알지 못하는 컬럼을 그대로 유지합니다
([롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)).

다운 마이그레이션은 없습니다. 이전 스키마로 돌아가려면 업그레이드 전에 만든 백업을 복원합니다([백업 및 복구](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)).

<a id="downgrading-an-installer-install"></a>

### 설치 프로그램으로 설치한 환경 다운그레이드

각 설치 프로그램 번들에는 빌드에 사용된 ClickHouse 릴리스가 지정되어 있습니다. 따라서 이전 Loom 버전을 설치하면 더 새로운 ClickHouse가 기록한 볼륨에서 이전 ClickHouse도 실행되며, 이 경우 볼륨을 읽지 못할 수 있습니다. Loom만 롤백하려면 더 새로운 ClickHouse를 유지합니다. 이전 설치 프로그램을 실행하기 전에(실행 즉시 스택이 시작됩니다) 현재 ClickHouse가 실행 중인 이미지를 설치 디렉터리의 `.env`에 고정합니다. 다음 명령은 그 파일에 넣을 줄을 출력하며, 이전의 `LOOM_CLICKHOUSE_IMAGE` 줄이 있으면 대체합니다.

```bash
dir=${LOOM_INSTALL_DIR:-$HOME/.loom}
echo "LOOM_CLICKHOUSE_IMAGE=$(docker inspect -f '{{.Config.Image}}' \
  "$(docker compose --project-directory "$dir" ps -q clickhouse)")"
```

그런 다음 이전 릴리스의 설치 프로그램을 버전을 지정해 실행합니다. 더 새로운 설치 환경에 이전 설치 프로그램을 실행하면 지정하지 않은 경우 거부되기 때문입니다.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/vX.Y.Z/install.sh -o loom-install.sh && LOOM_VERSION=X.Y.Z sh loom-install.sh && rm loom-install.sh
```

홈 디렉터리 또는 [본인만 쓸 수 있는 다른 디렉터리](../getting-started/quickstart.md#fast-path-released-image-no-clone)에서 실행합니다.

설치 프로그램은 `.env`를 유지하므로 이후 업그레이드에서도 이 고정 설정이 ClickHouse를 유지합니다. 더 새로운 Loom 버전을 설치하기 전에 `LOOM_CLICKHOUSE_IMAGE` 줄을 삭제하고, 셸에 `LOOM_CLICKHOUSE_IMAGE`를 export한 상태로 두지 마십시오. 설치 프로그램은 전달받은 값을 `.env`에 다시 기록합니다. 둘 다 되돌리려면 업그레이드 전에 만든 백업을 복원합니다.

<a id="what-stays-the-same-across-an-upgrade"></a>

## 업그레이드 후에도 유지되는 것

| 업그레이드 전후                             | 유지되는 사항                                                                                                       | 보장하는 수단                             |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| 새 스키마에서 동작하는 이전 빌드            | 이전 빌드가 알던 모든 테이블을 계속 읽고 쓰므로, 롤백에 스키마 단계가 필요하지 않습니다                             | `tests/unit/test_migrations_additive.py`  |
| 이미 적용된 마이그레이션 파일               | 수정하거나 삭제하지 않으며, 정정은 새 파일로 합니다                                                                 | 원장 체크섬; `--dry-run --strict`         |
| 서버가 제거한 필드를 보내는 클라이언트      | 요청을 처리하며, 해당 필드는 `ignored_args`에 명시됩니다                                                            | `server/transport/arg_check.py`           |
| 이전 빌드와 새 빌드의 동시 서비스           | 롤아웃 이후에야 켜지는 기능(8단계)을 제외하면, 새 컬럼을 포함해 각 빌드가 상대 빌드가 쓴 데이터를 읽습니다          | `make test-mixed-version`                 |

업그레이드로 오퍼레이션과 필드가 변경되거나 제거될 수 있습니다. 배포 환경이 제공하는 항목은 기능 카탈로그
([HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves))로 확인하고, 두 빌드 사이에 바뀐 내용은 `scripts/codegen/wire_changelog.py <previous> <new>`
([코드 생성](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md#wire-changelog))로 나열할 수 있습니다.

<a id="related"></a>

## 관련 문서

- [배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair): 업그레이드를 교체 방식으로 만드는 쌍
- [마이그레이션](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md): 새 마이그레이션이 따르는 규칙
- [롤링 업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md): 빌드 N-1과 N에 대한 규칙
- [문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)
