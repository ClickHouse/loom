<!-- i18n: source=docs/features/tracker/record-contract.md blob=88cada3e321cc9c82e54440ec12b93a202fcc39a model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md), which is authoritative.

<a id="tracker-record-contract"></a>

# 트래커 레코드 계약

`tracker.*` op와 `code.*`, `loom_search`의 트래커 구간은 팀의 이슈, 풀 리퀘스트, 인시던트를 읽기 전용 자격 증명으로 팀 자체 ClickHouse에서 실시간으로 읽습니다. Loom은 레코드를 복사하지 않습니다. 모든 문장은 하나의 논리 모델인 **계약**, 즉 아래의 테이블과 컬럼을 기준으로 작성됩니다.

팀은 두 가지 방법 중 하나로 스키마를 연결합니다.

| 모드 | 방법 |
| ---- | ---- |
| 뷰 | 팀이 계약의 테이블 및 컬럼 이름으로 뷰를 만듭니다. Loom은 이를 있는 그대로 읽습니다. |
| 매핑 | Loom이 자격 증명으로 읽을 수 있는 테이블을 나열하고 매핑을 제안합니다. 확정된 매핑은 데이터로 저장되며, 원격에는 아무것도 생성되지 않습니다. |

매핑은 계약 앞에 놓이는 프로젝션일 뿐이므로, 모든 소스에 대해 동일한 문장, 테스트, 개인정보 보호 조건이 실행됩니다. 코드로 된 계약은 `server/tracker/domain/contract.py`(`REQUIRED`, `OPTIONAL`, `FEATURES`, `ALIASES`)이고, 매핑은 `server/tracker/domain/mapping.py`이며, 테이블별로 읽는 컬럼은 `server/tracker/domain/columns.py`의 `COLUMNS_READ`입니다.

<a id="adding-a-tracker-source"></a>

## 트래커 소스 추가

1. **`readonly = 2`로 Loom용 읽기 전용 ClickHouse 사용자를 만듭니다.** `readonly = 1`은 Loom이 모든 읽기마다 보내는 쿼리별 설정을 거부하므로 어떤 읽기도 실행되지 않습니다 (`CREDENTIAL_READONLY_MODE`).

   ```sql
   CREATE USER loom_ro IDENTIFIED BY '…' SETTINGS readonly = 2;
   GRANT SELECT ON system.columns TO loom_ro;
   -- views mode: the views only
   GRANT SELECT ON loom_tracker.* TO loom_ro;
   -- mapping mode instead: each mapped table
   GRANT SELECT ON tracker_db.tickets TO loom_ro;
   ```

2. **views 모드에만 해당: 뷰를 만듭니다.** `github_issues`, `github_prs`, `edges`와 데이터가 있는 선택 테이블을 포함하는 데이터베이스(예: `loom_tracker`)를 만듭니다. 뷰는 `DEFINER = CURRENT_USER SQL SECURITY DEFINER`로 만들어야 하며, 그러면 `loom_ro`에는 뷰에 대한 `SELECT`만 있으면 되고 뷰 뒤에 있는 테이블에 대한 권한은 필요 없습니다. 일반 뷰는 읽는 사용자 자신의 권한으로 읽기 때문에 모든 읽기가 `ACCESS_DENIED`로 실패합니다. 등록 검사가 이를 잡아내고 해결 방법을 알려 줍니다. ClickHouse 밖의 소스는 `PostgreSQL`, `MySQL` 또는 `S3` 엔진 테이블 뒤에 둘 수 있습니다. `tracker.contract`는 모든 테이블과 컬럼, 각각에 담기는 내용, 테이블별 빈 뷰 템플릿을 반환하며, 항목 `id`를 위한 `loom_uuid5(name)` SQL 함수도 포함합니다.

3. **소스를 등록합니다.** 조직에 로그인한 구성원은 누구나 대시보드의 _Add a tracker_ 흐름([트래커 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#adding-a-tracker-from-the-dashboard))이나 아래 op로 등록할 수 있으며, 등록한 사람이 트래커 네임스페이스의 첫 `admin`이 됩니다. 아무것도 기록하기 전에 Loom은 호스트가 공개 주소로 확인되는지(`HOST_REFUSED`), 자격 증명이 읽기 전용인지(`CREDENTIAL_NOT_READONLY`, `CREDENTIAL_READONLY_MODE`), 연결할 수 있는지(`SOURCE_UNREACHABLE`), 스키마나 매핑이 계약을 충족하는지(`CONTRACT_FAILED`)를 검사합니다. 읽기 전용이란 `SHOW GRANTS`와 `SHOW GRANTS FINAL`이 나열하는 모든 접근 유형(부여된 역할의 유형 포함)이 `SELECT`, 데이터베이스·테이블·컬럼·딕셔너리의 `SHOW`, `dictGet`, `CREATE TEMPORARY TABLE` 중 하나이고, 어떤 권한도 `WITH GRANT OPTION`이나 `WITH ADMIN OPTION`으로 부여되지 않았으며(중첩된 역할도 마찬가지), 그 외의 유형은 거부된다는 뜻입니다. 사용자에게 부여된 모든 역할은 기본 역할이어야 검사가 전체 체인을 볼 수 있습니다. `SHOW GRANTS FINAL`을 받아들이지 않는 서버는 거부됩니다(`SOURCE_UNREACHABLE`). `dry_run: true`이면 스키마가 실패할 때 대신 보고서가 반환되며, 실패한 테이블마다 `CREATE OR REPLACE VIEW` 초안이 포함됩니다.

   | Op                           | 동작                                                                                                                                                                   |
   | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `tracker.contract`           | 연결하기 전에 계약과 뷰 템플릿을 반환합니다.                                                                                                                           |
   | `tracker.source.inspect`     | 연결해서 읽을 수 있는 모든 테이블을 컬럼과 함께 나열하고 제안 매핑을 제공합니다. 아무것도 기록하지 않습니다.                                                           |
   | `tracker.source.add`         | `name`, `host`, `port` (8443), `secure` (true), `database`, `user`, `password`, `namespaces`, 선택 `mapping`; `dry_run: true`는 모든 검사를 실행하고 아무것도 기록하지 않습니다. |
   | `tracker.source.set_mapping` | 등록된 소스의 매핑을 교체합니다(`dry_run`도 가능). `mapping: null`은 views 모드로 되돌립니다.                                                                          |
   | `tracker.source.check`       | 계약 검사를 지금 다시 실행합니다.                                                                                                                                      |
   | `tracker.source.rotate`      | 자격 증명을 교체합니다.                                                                                                                                                |
   | `tracker.source.remove`      | 소스와 그 네임스페이스를 폐기합니다(`confirm`).                                                                                                                        |
   | `tracker.source.list`        | 조직의 모든 소스: 계약 판정, 브레이커 상태, 네임스페이스, 각 네임스페이스에서의 호출자 지위. 자격 증명은 절대 반환되지 않습니다.                                       |

   `namespaces`의 각 항목은 `{namespace, repos, restricted, join_policy}`입니다. `repos`는 네임스페이스가 읽을 수 있는 `source_id`의 `Org/repo` 접두사이며(나열되지 않은 저장소는 읽지 않습니다), `restricted`는 이를 비공개 기록으로 만들고, `join_policy`는 표준 네임스페이스에서 `open`(기본값) 또는 `approval`입니다. 셀프 서비스 등록에는 `LOOM_TRACKER_ORG`가 필요하며 기본적으로 켜져 있습니다(`LOOM_TRACKER_SELF_SERVICE_ENABLED`, 조직당 최대 `LOOM_TRACKER_SELF_SERVICE_MAX_SOURCES`개, 기본값 10. 로컬 개발용 ClickHouse에는 `LOOM_TRACKER_SELF_SERVICE_ALLOW_PRIVATE_HOSTS=true`). 비밀번호는 암호화되어 저장되며 반환되지 않습니다. 이름으로 `inspect`하려면 소스의 네임스페이스 중 하나에 대한 `admin`이 필요합니다. `set_mapping`, `rotate`, `remove`는 소스의 모든 네임스페이스가 읽는 내용을 바꾸므로 모든 네임스페이스에 대해 같은 권한이 필요합니다. 표준 네임스페이스는 `admin` 수준의 게이트가 결정하고, 제한된 네임스페이스는 토큰이 해당 네임스페이스를 지정할 필요가 없다는 점을 제외하면 제한 게이트의 규칙을 따릅니다. 등록자의 권한이 취소된 뒤에는 소스를 등록한 것만으로는 충분하지 않습니다([제한된 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces)). 플랫폼 관리자가 아닌 사용자의 대시보드 세션은 `writer` 토큰이므로 비공개(제한된) 네임스페이스가 있는 소스를 변경하거나 폐기할 수 없습니다. 이를 하려면 운영자가 `loom mint-token --roles admin`으로 발급하는 `admin` 역할의 토큰이 필요합니다. 페이지는 해당 버튼을 이유와 함께 비활성화해서 보여 주며, 최종 판단은 서버가 합니다.

   운영자는 대신 같은 소스를 구성에서 설정할 수도 있습니다([구성](#configuration)). 같은 이름이면 구성에 있는 항목이 등록된 항목보다 우선하며, 구성에서만 변경할 수 있습니다(`SOURCE_FROM_CONFIG`).

4. **Loom이 네임스페이스의 디렉터리 행을 만듭니다.** `kind = tracker`, 비공개이며, 제한된 네임스페이스는 `classification = restricted`, 표준 네임스페이스는 `join_policy`가 `open`(또는 `approval`), 제한된 네임스페이스는 `approval`입니다.

5. **독자가 참여합니다.** 열린 트래커 네임스페이스에서 `role: "viewer"`로 `namespace.join`을 호출하면 서비스 계정을 포함해 즉시 승인됩니다. 제한된 네임스페이스는 해당 관리자의 승인을 기다리며, 독자의 토큰이 네임스페이스를 지정해야 합니다([ID와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)).

6. **트래커를 저장소에 연결하면** 해당 체크아웃에서 작업하는 에이전트가 트래커를 읽을 수 있습니다. `tracker_namespaces`와 함께 `code.repo.set_source`(저장소 `admin`)를 호출하거나, 대시보드의 저장소 카드를 사용하거나, 운영자 자신의 저장소에 한해 `LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON`을 사용합니다. 저장소는 여러 트래커를 순서대로 연결할 수 있으며, 첫 번째가 기본 트래커이고 그 번호가 저장소의 이슈 및 PR 번호가 됩니다([트래커 연결](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#linking-a-tracker-to-a-repository)). `tracker.binding`은 코드 네임스페이스가 호출자에게 무엇으로 해석되는지 보고합니다.

<a id="configuration"></a>

## 구성

같은 항목의 운영자 형식은 다음과 같습니다.

```bash
LOOM_TRACKER_ORG=acme
LOOM_TRACKER_SOURCES_JSON='{
  "sources": {
    "widgets": {"host": "abc.clickhouse.cloud", "port": 8443, "secure": true, "database": "loom_tracker"}
  },
  "namespaces": {
    "tracker-widgets":         {"source": "widgets", "repos": ["acme/widgets"]},
    "tracker-widgets-private": {"source": "widgets", "repos": ["acme/widgets-private"], "restricted": true}
  }
}'
LOOM_TRACKER_CREDENTIALS_JSON='{"widgets": {"user": "loom_ro", "password": "…"}}'
LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON='{"code-widgets*": ["tracker-widgets", "tracker-helpdesk"]}'
```

자격 증명은 맵과 분리된 별도의 변수에 두므로, 비밀 값 없이 맵을 보여 주고 diff를 낼 수 있습니다. 바인딩 값은 트래커 네임스페이스 하나이거나 기본 항목을 맨 앞에 둔 순서 있는 목록입니다. 키는 운영자 소유 리포지토리의 코드 네임스페이스 이름에 대한 glob입니다(셀프 서비스 리포지토리는 해당 관리자가 연결합니다). 관리자의 `code.repo.set_source` 연결은 해당 리포지토리에 대해 맵을 대체합니다. 트래커 네임스페이스는 `LOOM_TRACKER_ORG`에 속하며, 이 값이 비어 있으면 어떤 트래커 네임스페이스도 제공되지 않습니다.

<a id="source-entry-options"></a>

### 소스 항목 옵션

소스 항목에는 다음 옵션도 넣을 수 있습니다.

- `"mapping"`: 저장된 매핑([매핑 모드](#mapping-mode))입니다. 없으면 views 모드입니다.
- `"checks": {"from": "<table or table function>", "repo": "Org/repo"}`: 트래커의 CI 결과가 있는 위치로, `tracker.flaky`와 `tracker.test_signal`이 읽습니다. 확인 순서는 이 항목, 그다음 소스 자체의 `checks` 테이블, 마지막으로 `LOOM_TRACKER_CHECKS_FROM` / `LOOM_TRACKER_CHECKS_REPO`입니다(둘 다 기본값은 비어 있음: CI 체크 없음). 뷰가 아니라 테이블 함수를 지정하십시오. 뷰의 필터는 스캔 후 원격 측에서 실행됩니다. `repo`가 없을 때 리포지토리가 하나뿐인 네임스페이스는 그 리포지토리를 사용합니다.
- `"bisect": {"code_prefixes": ["src/"]}`: bisect에서 소스 코드로 취급할 경로입니다. 없으면 docs, tests, CI, examples, benchmarks 아래가 아닌 모든 경로가 해당됩니다. 트래커가 없는 리포지토리도 Loom 자체의 PR 이력을 기반으로 bisect합니다(이 경우 응답에 `candidates_from: "code_history"`가 표시됩니다).
- `"columns": {"<table>": {"<contract column>": "<your column>"}}`: 사용자의 테이블이 다른 이름으로 가지고 있는 계약 컬럼입니다. 예: `{"github_issues": {"triage_bisect_outcome": "bisect_result"}}`. 실제 컬럼은 그것이 대응하는 컬럼과 동일하게 검사되며, 일반 식별자만 허용됩니다.

규칙:

- 알 수 없는 소스를 지정한 네임스페이스는 제공되지 않습니다(부팅 시 오류 한 줄이 출력되며, 추측하지 않습니다).
- 각 소스는 자체 연결, 서킷 브레이커, 시간 예산을 가지므로 한 팀의 느린 서비스가 다른 팀의 브레이커를 작동시키지 않습니다.
- 등록된 소스(`loom.qa_sources`)는 `LOOM_TRACKER_SOURCES_REFRESH_SECONDS`(60)마다 다시 읽으며, 같은 프로세스에서 쓰기를 하면 즉시 갱신됩니다.

모든 트래커 설정은 [구성 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#tracker)에 있습니다.

<a id="public-and-private"></a>

## 공개와 비공개

트래커 네임스페이스는 **standard** 또는 **restricted** 중 하나이며, Loom이 구문 단위로 이를 강제합니다.

- standard 네임스페이스의 모든 구문에는 `is_private = 0`이 붙고, 비공개 데이터만 담은 테이블(`core_incidents`, `support_cases`, `slack_threads`)은 읽기 전에 거부됩니다.
- restricted 네임스페이스는 이 조건을 해제하고 자체 `repos`만 읽으며, restricted 네임스페이스 규칙의 적용을 받습니다. 즉 해당 네임스페이스를 명시한 토큰과 직접 부여된 권한이 필요합니다.

세 가지 구성이 가능합니다.

- `is_private` 컬럼이 있는 뷰 세트 하나를 두 네임스페이스가 함께 읽는 구성(읽기 전용 사용자에게 `is_private`에 대한 행 정책을 적용하면 ClickHouse 쪽에서 검사가 추가됩니다)
- 사용자 두 명과 뷰 세트 두 개를 두고, 그중 한쪽에는 공개 행만 담아 두 개의 소스로 등록하는 구성
- restricted 네임스페이스만 단독으로 쓰는 구성

<a id="the-contract"></a>

## 계약

타입은 **계열**별로 비교합니다. `String`, `LowCardinality(String)`, `Nullable(String)`, `FixedString`, `Enum`은 하나의 계열입니다. `DateTime`, `DateTime64`, `Date`는 하나의 계열입니다. `UInt*`와 `Int*` 타입, `Bool`은 하나의 계열입니다. `Array(x)`는 `x`를 기준으로 비교합니다. 다른 저장소 위에 만든 뷰는 코덱과 널 허용 여부가 달라도 되지만, 형태는 달라서는 안 됩니다. 뷰 템플릿은 각 계열을 가장 넓은 타입(`UInt64`, `DateTime64(3)`, …)으로 작성합니다.

<a id="required-tables"></a>

### 필수 테이블

이 테이블이 없는 소스는 서비스되지 않습니다.

**`github_issues`**: 이슈당 한 행.

| 컬럼                                                                                         | 계열          | 의미                                                                                                                    |
| -------------------------------------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_issue:' \|\| source_id)` (비공개 행은 `github_issue_private:`); `edges`가 사용하는 키       |
| `source_id`                                                                                  | String        | `Org/repo#number`; 저장소는 트래커 네임스페이스에 등록된 것이어야 합니다                                                |
| `number`                                                                                     | UInt          | 이슈 번호                                                                                                               |
| `url`, `title`, `body`, `author`                                                             | String        | 사람이 열 수 있는 링크, 모든 검색이 읽는 제목, 중복 확인이 읽는 본문, 이슈를 연 사람                                    |
| `labels`, `assignees`                                                                        | Array(String) |                                                                                                                         |
| `primary_component`, `severity_level`                                                        | String        | 팀의 분류 체계; 없으면 `''`                                                                                             |
| `state_reason`                                                                               | String        | `completed`, `not_planned`, `duplicate` 또는 `''`                                                                       |
| `affected_versions`, `fix_versions`                                                          | Array(String) | 이슈가 영향을 주는 버전, 수정이 포함된 버전                                                                             |
| `is_flaky_test`, `is_crash_ci`, `is_performance`                                             | UInt          | 0/1 플래그; 알 수 없으면 `0`                                                                                            |
| `has_test`                                                                                   | UInt          | 연결된 테스트가 있음; 알 수 없으면 NULL                                                                                 |
| `error_code`                                                                                 | UInt          | 보고서가 언급하는 서버 오류 코드; 없으면 `0`                                                                            |
| `is_private`                                                                                 | UInt          | 비공개 저장소 행이면 `1`                                                                                                |
| `created_at`, `updated_at`, `closed_at`                                                      | DateTime      | `closed_at`은 열려 있는 동안 epoch 0                                                                                    |
| `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | String        | 트리아지 봇의 판정; 트래커에 없으면 `''`로 제공                                                                         |
| `triage_reproduces_on`, `triage_clean_on`                                                    | Array(String) | 마찬가지로 `[]`로 제공                                                                                                  |

**`github_prs`**: 풀 리퀘스트당 한 행.

| 컬럼                                                                                         | 계열          | 의미                                                                       |
| -------------------------------------------------------------------------------------------- | ------------- | -------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_pr:' \|\| source_id)` (`github_pr_private:`) |
| `source_id`, `number`, `url`, `title`, `body`, `author`                                      | 위와 동일     |                                                                            |
| `pr_number`                                                                                  | UInt          | `number`와 같음                                                            |
| `primary_component`, `severity_level`, `is_private`, `created_at`, `updated_at`, `closed_at` | 위와 동일     |                                                                            |
| `state`                                                                                      | String        | `open`, `closed` 또는 `merged`                                             |
| `is_merged`, `has_test`, `review_requests_test`                                              | UInt          |                                                                            |
| `test_types`, `reviewers`, `src_components`                                                  | Array(String) |                                                                            |
| `ci_result`, `changelog_category`, `minor_version`, `base_branch`                            | String        |                                                                            |
| `is_backport_pr`, `is_cherrypick_pr`, `is_sync_pr`                                           | UInt          | 파생 PR; 활동 조회에서는 제외됩니다                                        |
| `upstream_pr_number`                                                                         | UInt          | 백포트가 반복하는 PR; 없으면 `0`                                           |
| `additions`, `deletions`, `time_to_merge_days`                                               | UInt          |                                                                            |

**`edges`**: 항목 간 링크.

| 컬럼                   | 계열     | 의미                                                                                                      |
| ---------------------- | -------- | --------------------------------------------------------------------------------------------------------- |
| `from_id`, `to_id`     | UUID     | 위와 같은 항목 id                                                                                         |
| `from_type`, `to_type` | String   | `github_issue`, `github_pr`, `github_issue_private`, `github_pr_private`, `core_incident`, `support_case` |
| `rel_type`             | String   | `fixes`, `related_to`, `duplicate_of`, `caused_by`, `backport_of`, `reverts`, …                           |
| `created_at`           | DateTime |                                                                                                           |

Loom은 항목의 `id`를 로컬에서 계산하고 그 값으로 `edges`를 조인하므로, 뷰는 정확히 같은 값을 만들어야 합니다. ClickHouse에는 `uuid5()`가 없습니다. `tracker.contract`가 반환하는 `loom_uuid5` 함수는 RFC 4122 버전 5 방식을 SQL로 구현한 것이며, Python의 `uuid.uuid5`와 같은 결과를 냅니다.

<a id="optional-tables"></a>

### 선택 테이블

테이블이 없으면 그 테이블이 필요한 op는 `not_provided`로 응답합니다. 테이블은 있지만 최소 컬럼(`contract.py`의 `OPTIONAL`) 중 하나가 빠져 있으면 `incomplete`로 보고하고 없는 것으로 취급합니다.

| 테이블                                       | 최소 컬럼                                                                                                                                                     |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `core_incidents`                             | `id`, `source_id`, `title`, `body`, `primary_component`, `severity_level`, `stack_functions Array(String)` (스택 프레임 매칭), `error_code`, `created_at`      |
| `support_cases`                              | `id`, `source_id`, `title`                                                                                                                                    |
| `slack_threads`                              | `source_id`, `title`, `body`                                                                                                                                  |
| `component_risk_snapshot`                    | `computed_at`, `component`                                                                                                                                    |
| `release_items`                              | `release_version`, `issue_number`                                                                                                                             |
| `release_health_snapshot`                    | `computed_at`, `release_version`                                                                                                                              |
| `version_history`, `version_history_private` | `commit_sha`, `version`                                                                                                                                       |
| `checks`                                     | `check_name`, `test_name`                                                                                                                                     |
| `cloud_instances`                            | `instance_id`                                                                                                                                                 |
| `rel_instance_mentions`                      | `from_id`, `instance_id`                                                                                                                                      |
| `slack_incidents`                            | `incident_id`                                                                                                                                                 |

`core_incidents`, `support_cases`, `slack_threads`는 제한된 네임스페이스를 통해서만 읽습니다.

<a id="feature-columns"></a>

### 기능 컬럼

필수 테이블의 선택 컬럼으로, 있음 또는 없음으로 보고됩니다. 소스에 없는 기능 컬럼은 별칭을 읽고, 별칭도 없으면 기본값을 읽습니다([별칭](#aliases) 참조). 이를 읽는 구문은 해당 컬럼이 없는 행처럼 응답하며, 기능은 계속 없음 상태로 남습니다.

| 기능         | 컬럼                                                | 활성화되는 것                                                             |
| ------------ | --------------------------------------------------- | ------------------------------------------------------------------------- |
| `bisect`     | `github_prs.diff`, `github_prs.affected_file_paths` | bisect 후보 및 원인 커밋 스캔                                             |
| `merge_time` | `github_prs.merged_at`                              | 병합 시간 사실                                                            |
| `vector`     | `github_issues.embedding`, `github_prs.embedding`   | 보고만 됨; `tracker.similar`는 Loom이 직접 계산한 임베딩을 사용              |

<a id="aliases"></a>

### 별칭

소스에 계약 컬럼이 없더라도, `contract.py`의 `ALIASES`가 소스가 가진 컬럼으로 만든 허용 표현식을 그 컬럼에 대해 지정하고 있으면 계약을 충족합니다. 검사는 별칭이 쓰는 컬럼을 계열별로 확인하고 `aliased`로 보고합니다. 구문은 계속 계약의 이름을 읽는데, 테이블 참조가 별칭을 추가하는 프로젝션으로 대체되기 때문입니다(쿼리 분석기가 구문이 읽는 컬럼만 남기고 나머지를 제거합니다). 자체 컬럼이 없는 별칭은 **기본값**(`defaulted`)입니다. 모든 행이 `''`, `0`, `[]` 또는 `NULL`로 읽히며, 보고서에도 그렇게 표시되므로 기본값이 실제 데이터로 오인되지 않습니다.

| 테이블                                           | 컬럼                                                                                         | 허용 형식(순서대로)                                                        |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `github_issues`                                  | `severity_level`                                                                             | `severity`                                                                 |
| `github_issues`                                  | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)` (JSON `metadata` 컬럼), 없으면 `NULL`   |
| `github_issues`                                  | `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | `''`                                                                       |
| `github_issues`                                  | `triage_reproduces_on`, `triage_clean_on`                                                    | `[]`                                                                       |
| `github_prs`                                     | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)`, 없으면 `toUInt8(tests_added > 0)`      |
| `github_prs`                                     | `upstream_pr_number`                                                                         | `ifNull(toUInt32(metadata.original_pr_number.:Int64), 0)`, 없으면 `0`      |
| `github_prs`                                     | `review_requests_test`                                                                       | `NULL`                                                                     |
| `github_prs`                                     | `test_types`, `src_components`                                                               | `[]`                                                                       |
| `github_prs`                                     | `merged_at`                                                                                  | `if(is_merged = 1, closed_at, toDateTime64(0, 3))`                         |
| `github_prs`, `core_incidents`                   | `severity_level`                                                                             | `''`                                                                       |
| `qa_bugs` (답변 경로의 교차 소스 뷰)             | `severity_level`, `has_test`                                                                 | `severity`; `toUInt8(metadata.has_test.:Bool)`, 없으면 `NULL`              |

계약에 나열되지 않은 이름 변경은 여전히 누락된 컬럼으로 처리됩니다. 소스 항목의 `columns` 맵([소스 항목 옵션](#source-entry-options))에 선언하거나, `contract.py`에 `Alias`를 추가해 모든 소스에서 다른 표기를 허용하세요.

<a id="mapping-mode"></a>

## 매핑 모드

매핑은 각 논리 테이블마다 팀의 어느 테이블을 읽을지, 그리고 각 논리 컬럼마다 해당 테이블의 컬럼들에 대한 표현식을 지정합니다.

```json
{
  "version": 1,
  "tables": {
    "github_issues": {
      "source": "tracker_db.tickets",
      "final": true,
      "columns": {
        "source_id": { "concat": [{ "lit": "acme/widgets#" }, "ticket_no"] },
        "number": "ticket_no",
        "title": "summary",
        "body": "description",
        "created_at": "opened_at",
        "state_reason": { "lower": "resolution" }
      }
    }
  }
}
```

- **표현식은 SQL이 아니라 데이터입니다**: 실제 테이블에 있는 컬럼 이름(또는 `{"col": …}`), `{"lit": …}`, 또는 `concat`, `coalesce`, `eq`, `in`, `has`, `cast`(`[expr, family]`), `lower`, `digits`(키의 끝 숫자: `PROJ-1234` → `1234`) 중 하나입니다. Loom은 식별자는 백틱으로, 리터럴은 따옴표로 감싸 렌더링하고, 각 표현식을 구문이 읽는 타입 계열로 변환한 뒤 읽기 전용 자격 증명으로 실행합니다.
- `source`는 `db.table`이며, 트래커의 데이터베이스에 있는 테이블이면 `table`만 써도 됩니다. `final: true`는 테이블을 `FINAL`로 읽습니다(Replacing, Collapsing, Aggregating, Summing 엔진에 권장).
- Loom은 매핑된 `source_id`에서 각 항목의 `id`를 만들고, 매핑된 `from_ref` / `to_ref`(`Org/repo#number`)에서 엣지의 `from_id` / `to_id`를 만듭니다. 이들은 직접 매핑하지 않습니다.
- 매핑하는 각 테이블에는 핵심 컬럼이 필요합니다. `github_issues`는 `source_id`, `number`, `title`, `created_at`, `github_prs`는 `source_id`, `pr_number`, `title`, `created_at`, `edges`는 `from_ref`, `to_ref`, `from_type`, `to_type`, `rel_type`, `core_incidents`는 `source_id`, `title`, `created_at`입니다. 매핑에는 반드시 `github_issues`가 포함되어야 합니다. 매핑하지 않은 컬럼은 기본값을 쓰거나 다른 필드에서 파생되며(PR의 `state`는 병합 및 종료 필드에서 파생), 보고서에 그 목록이 표시됩니다. 매핑에 이름이 없는 논리 테이블은 빈 테이블로 읽힙니다.
- 보고서의 기능(`items`, `duplicate_check`, `duplicate_calibration`, `pull_requests`, `links`, `severity_board`, `bisect`, `incidents`, `ci_checks`)은 매핑된 데이터로 Loom이 무엇에 답할 수 있는지를 알려 줍니다.

읽는 시점에 각 논리 테이블 참조는 매핑된 프로젝션으로 대체되므로, 모든 소스에 대해 동일한 구문, 테스트, 개인정보 보호 조건이 실행됩니다.

<a id="checking-a-source"></a>

## 소스 확인

```bash
loom tracker-contract-check                           # every configured source
loom tracker-contract-check --source widgets          # one source
loom tracker-contract-check --namespace tracker-widgets
loom tracker-contract-check --json
```

서버는 각 프로세스에서 첫 트래커 읽기 시 동일한 확인을 수행합니다. 소스 자체의 자격 증명으로 스키마를 읽고(`system.columns`, 또는 이를 볼 수 없는 경우 테이블마다 `DESCRIBE`), 그 결과를 `LOOM_TRACKER_CONTRACT_TTL_S`(3,600 s) 동안 캐시합니다. 실패한 결과는 60 s 동안만 캐시하므로, 수정된 뷰는 1분 안에 반영됩니다. 확인에 실패한 소스는 모든 읽기에 대해 누락된 항목을 명시한 `tracker_unavailable`로 응답하며, 부분적인 응답은 절대 반환하지 않습니다. `tracker.source.check`는 필요할 때 확인을 즉시 실행합니다.

<a id="related"></a>

## 관련 문서

- [Tracker 개요](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md): op가 답하는 내용, 연결, 대시보드 흐름
- [히스토리와 PR](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md): tracker를 읽는 코드 플레인 op
- [구성 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#tracker)
- [작업 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#tracker)
