<!-- i18n: source=docs/features/integrations/okf.md blob=7d8ba9d5d921d351c46c4592c82a48277bb4f2cb model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md), which is authoritative.

<a id="okf-bundles"></a>

# OKF 번들

네임스페이스는 OKF(Open Knowledge Format) v0.2 번들로 렌더링할 수 있습니다. 번들은 메모리마다 YAML frontmatter가 있는 Markdown 파일 하나, 각 개념을 한 줄 설명과 함께 나열하는 디렉터리별 `index.md`, 변경 내역을 담은 `log.md`로 구성됩니다. CLI는 HTTP API를 사용하는 클라이언트 측 도구입니다. 번들은 diff로 비교하고, pull request에서 검토하고, 오프라인에서 읽을 수 있으며, 저장소는 계속 Loom이 담당합니다.

번들은 두 번째 저장소가 아니라 프로젝션입니다. 네임스페이스에서 다시 생성되며, 번들에서 수정한 내용은 일반 쓰기 경로(충돌 감지, 블랙리스트, 감사)를 통해 반영됩니다.

```bash
loom export-okf --namespace <ns> --out ./knowledge        # render
loom okf-lint ./knowledge                                 # offline check: exit 2 critical, 1 warnings, 0 clean
loom import-okf ./knowledge --namespace <ns> --dry-run    # plan the write-back
```

세 명령 모두 `--server-url`, `--token`(기본값은 `$LOOM_API_KEY`, 그다음 `~/.loom/token`), `--org`(기본값: 토큰의 `org` 클레임)를 받습니다. `okf-lint`는 이 중 어느 것도 필요하지 않습니다. 모든 플래그는 [CLI 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)에서 확인할 수 있습니다.

<a id="export"></a>

## 내보내기

`loom export-okf`는 읽기 전용입니다. 네임스페이스의 모든 라이브 행(모든 에이전트)을 `memory.list`로 페이지 단위로 읽고, `memory.bulk_get`으로 전체 값을 가져온 뒤, 계획된 파일을 `--out` 아래에 씁니다. 기존 개념 파일은 덮어쓰며, 디렉터리의 그 밖의 파일은 건드리지 않습니다. 매핑은 `server/memory/domain/okf_bundle.py`(순수 함수, 단위 테스트됨)에 있으며, `server/cli/okf.py`는 전송과 파일 시스템을 담당합니다.

| 메모리                             | 개념                                                                                                                                                                                                                                                                                                                                                           |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_key`                       | 파일 이름과 `loom.memory_key`: 키의 슬러그이며 160바이트로 제한됩니다. 다른 키가 같은 이름을 쓰고 있거나, 번들 파일 이름(`index`, `log`, `README`, `AGENTS`, 대소문자 무관) 또는 Windows 장치 이름(`nul`, `con`, `com1`)인 경우 키와 행 id의 `-<hash>`가 붙습니다                                                                                       |
| `memory_type`                      | 디렉터리(타입의 소문자 슬러그이며 파일 이름과 같은 방식으로 제한됩니다. `evidence`, `digests`, `staging`, `node_modules`, 번들 파일의 전체 이름(`index.md`, `log.md`) 또는 `nul` 같은 Windows 장치 이름인 경우 `type-<slug>`)와 `type`: `semantic` → Fact, `procedural` → Procedure, `preference` → Preference, `episodic` → Event, 그 외는 첫 글자를 대문자로 바꾼 값 |
| 값                                 | 본문: 텍스트는 그대로, JSON은 펜스 코드 블록으로                                                                                                                                                                                                                                                                                                               |
| `tags`                             | `tags`                                                                                                                                                                                                                                                                                                                                                         |
| 에이전트와 `created_at`            | `generated: {by: "agent:<agent>", at}`                                                                                                                                                                                                                                                                                                                         |
| 라이브 `verified_by` / `verified_at` | `verified: [{by, at}]` (사람과 프로세스의 증명만 해당)                                                                                                                                                                                                                                                                                                         |
| `status`                           | `active` → `stable`, `conflicted` → `draft`, 그 외는 `deprecated`                                                                                                                                                                                                                                                                                              |
| `expires_at`                       | `stale_after`, 검토 기한 날짜                                                                                                                                                                                                                                                                                                                                  |
| `source_resource_id`               | `sources: [{resource}]`                                                                                                                                                                                                                                                                                                                                        |
| Loom 고유의 모든 항목              | `loom:` 블록. `source_class`, `sensitivity`, `superseded_by`, `valid_to`, `content_sha256`을 포함합니다                                                                                                                                                                                                                                                        |

**링크.** 사람이 작성한 엣지(`memory.export_evidence kind=edges`로 읽는 네임스페이스의 타입 지정 `memory.link` 엣지와, 대체된 행이 암시하는 `supersedes` 체인)는 권위 있는 frontmatter `relates_to: [{path, kind}]`로 기록되며 본문의 "Related Concepts" 섹션에도 반영됩니다. 그 뒤에 공유 엔티티 링크가 이어지며, 개수가 제한되고 대칭적이어서 번들에는 검증기가 따라갈 수 있는 그래프가 생깁니다. 어디에도 링크되지 않은 개념은 고아로 남으며, CLI는 이를 숨기지 않고 보고합니다.

**출처 정보도 함께 이동합니다.** `loom:` 블록에는 행의 출처 티어가 담기므로, 왕복 변환을 거쳐도 추론이 직접 확인한 사실로 바뀌지 않습니다.

| 플래그                 | 효과                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--include-high`       | `sensitivity: high` 행, 그에 연결된 원본 청크와 다이제스트, 그리고 high 형태(자격 증명, 카드 번호, SSN, 명시된 PIN)를 담은 원본 텍스트도 내보냅니다. 서버는 공개된 항목마다 감사 기록을 남깁니다. `memory.bulk_get`은 보존 기간이 지난 `high` 행을 제공하지 않으므로, 해당 행은 이름만 알리고 제외됩니다. 기본값은 꺼짐입니다. 미러 저장소는 네임스페이스에 부여된 권한보다 더 넓은 독자를 대상으로 하기 때문입니다 |
| `--no-evidence`        | 개념만 내보냅니다. 번들이 기본으로 포함하는 원본 청크(`evidence/`)와 세션 다이제스트(`digests/`)는 건너뜁니다                                                                                                                                                                                                                                                                                         |
| `--no-links`           | 타입 지정 엣지를 건너뜁니다(공유 엔티티 링크는 계속 기록됩니다)                                                                                                                                                                                                                                                                                                                                       |
| `--include-tombstoned` | 소프트 삭제된 행도 `status: deprecated`로 내보냅니다. 목록이 전체를 담고 있지 않은 행(미리보기를 넘어서는 값, 또는 `--include-high` 아래의 `high` 행)은 이름만 알리고 제외됩니다                                                                                                                                                                                                                      |
| `--limit N`            | 가장 최근 행 최대 N개까지만 내보냅니다(스모크 실행용)                                                                                                                                                                                                                                                                                                                                                 |
| `--title`, `--dry-run` | 루트 인덱스 제목. 쓰지 않고 계획만 수행                                                                                                                                                                                                                                                                                                                                                               |

<a id="lint"></a>

## 린트

`loom okf-lint DIR`은 번들 디렉터리만 읽습니다. 서버도, ClickHouse도, 네트워크도 사용하지 않습니다. 각 결과에는 id, 경로, 메시지, 심각도가 포함됩니다(`server/memory/domain/okf_lint.py`).

| id                              | 심각도   | 의미                                                                                                 |
| ------------------------------- | -------- | ---------------------------------------------------------------------------------------------------- |
| `frontmatter`                   | critical | 개념에 frontmatter가 없거나, YAML을 파싱할 수 없거나, `type`이 비어 있거나, `tags` 값이 목록이 아닙니다 |
| `required_keys`                 | critical | `title`, `description`, `loom.memory_key`, `loom.memory_type` 중 하나가 없습니다                     |
| `relates_to`                    | critical | 엣지의 형식이 잘못되었거나 `.md` 대상이 번들에 존재하지 않습니다                                     |
| `index_missing` / `okf_version` | critical | 개념이 있는 디렉터리에 `index.md`가 없거나, 루트 인덱스에 `okf_version`이 선언되어 있지 않습니다     |
| `staging`                       | critical | `staging/` 디렉터리가 존재합니다. staging은 응답에 사용되지 않습니다                                 |
| `type_directory_mismatch`       | warning  | `loom.memory_type`이 파일이 위치한 폴더와 일치하지 않습니다                                          |
| `pointer_unmounted`             | warning  | `relates_to` 경로가 번들 밖의 개념을 가리키는 `loom://org/namespace/key` 포인터입니다                |
| `unknown_relation`              | warning  | `relates_to` 종류가 Loom의 표준 동사에 속하지 않습니다                                               |
| `edited_since_export`           | warning  | 본문의 해시가 더 이상 `loom.content_sha256`과 일치하지 않습니다(개념 또는 원본 청크를 직접 수정함)   |
| `derived_needs_confidence`      | warning  | `source_class: derived`인데 신뢰도가 1.0입니다. 추론이 사실처럼 기록되었습니다                       |
| `high_sensitivity_present`      | warning  | 민감도가 높은 행(`loom.sensitivity: high` 또는 `sensitivity:high` 태그)이 함께 이동했습니다          |
| `index_listing`                 | warning  | 해당 디렉터리 인덱스 또는 루트 인덱스에 나열되지 않은 개념이 있습니다                                |
| `drift`                         | warning  | 루트 인덱스의 `loom.corpus_digest`가 더 이상 개념 파일과 일치하지 않습니다                           |
| `orphans`                       | info     | 어디로도 링크하지 않고 어디에서도 링크되지 않는 개념(데이터의 특성이며 결함이 아닙니다)              |

종료 코드는 컴파일 단계에 필요한 방식을 따릅니다. critical 결과가 하나라도 있으면 **2**, 경고만 있으면 **1**, 문제가 없으면 **0**입니다. `--strict`를 사용하면 경고도 2로 종료합니다(orphans는 계속 정보성으로만 취급). `--json`은 기계가 읽을 수 있는 보고서를 출력합니다.

**코퍼스 다이제스트.** 루트 `index.md`에 `loom.corpus_digest`가 있으면 린트는 이를 다시 계산합니다. 개념 파일만 대상으로 번들 기준 상대 경로순으로 정렬하고, 각 파일의 SHA-256 16진수 다이제스트를 구분자 없이 이어 붙인 뒤, 그 문자열의 SHA-256을 구합니다. 값이 일치하지 않으면 `drift` 경고가 발생하며, 트리에 있는 개념 파일 수와 인덱스에 기록된 수를 함께 알려 줍니다.

<a id="import"></a>

## 가져오기

`loom import-okf DIR --namespace NS`는 먼저 lint를 실행하고, 치명적(critical) 결과가 있는 번들은 `--force-import`를 지정하지 않으면 거부합니다(`--no-lint`는 이 검사를 건너뜁니다). 그런 다음 각 개념을 `memory.set`으로 기록하며, ID는 토큰 또는 `--agent`, `--user-id`, `--session-id` 플래그에서 가져옵니다. 이 기록에는 충돌 감지, 블랙리스트, 감사 로그가 적용됩니다. 이후 `memory.link`로 `relates_to` 엣지를 다시 만듭니다(`--no-links`는 이 단계를 건너뜁니다).

기록하기 전에 가져오기는 해당 ID의 라이브 행을 읽고, 기록할 내용이 이미 행에 들어 있는 개념은 모두 건너뜁니다. 비교 대상은 값(`content_sha256`), 타입, 태그, 신뢰도, `valid_at`, `valid_to`, `source_trace`, `source_class`(선언된 클래스, 없으면 `imported`)이며, 번들에 엔티티가 있으면 엔티티도 비교합니다. 이미 라이브 상태인 엣지도 건너뜁니다. 같은 행에 해당하는 개념(하나의 키를 여러 작성자용으로 내보낸 경우)은 한 번만 기록하며, 가장 최근에 갱신된 것이 우선합니다. 동일한 행을 다시 보내면 강화(reinforcement)로 집계되므로, 같은 번들을 두 번 가져와도 두 번째에는 개념이나 엣지를 기록하지 않습니다. 단, 예외가 둘 있습니다. `sources:`가 있는 개념은 가져오기가 수행하는 읽기에서 그 소스가 반환되지 않으므로 실행할 때마다 기록됩니다. 대상이 다른 키로 병합하는 개념은 실행할 때마다 다시 전송되어 해당 행을 강화합니다(병합으로 집계됨). 마지막 줄에는 대상 행의 기록됨, 변경 없음, 병합됨, 실패 개수가 표시됩니다.

`source_trace`는 `okf:<path inside the bundle>`이므로 번들이 디스크 어디에 있든 값이 바뀌지 않습니다. 따라서 실행할 때마다 새 디렉터리로 내보내도 됩니다.

번들의 `evidence/`와 `digests/`는 `memory.import_evidence`로 다시 기록되며(`--no-evidence`는 이를 건너뜁니다), 가져오는 서버가 변경 여부와 관계없이 매번 다시 임베딩합니다. 따라서 예약된 가져오기에는 `--no-evidence`를 전달하세요.
텍스트가 높음(high)으로 판정되는 청크나 다이제스트(자격 증명, 카드 번호, SSN 또는 명시된 PIN), 또는 청크 경계에서 잘린 그 일부를 담고 있는 청크는 벡터 없이 기록되며 검색되지 않습니다
([민감도](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)).

| 플래그            | 효과                                                                                                                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--verify`        | frontmatter에 `human:` 검증이 있는 개념을 `memory.verify`로 다시 증명합니다. **가져오는 호출자로서** 수행합니다                                                                        |
| `--keep-sessions` | 각 개념을 `loom.session_id`에 있는 세션 아래에 기록하므로, 여러 세션에 있던 키는 여러 행으로 유지됩니다. `--session-id`는 대체값입니다                                                  |
| `--dry-run`       | 어떤 기록도 전송하지 않고, 대상의 라이브 행(이를 읽습니다)을 기준으로 기록 계획만 세웁니다                                                                                              |
| `--prune`         | 이 `--agent`와 토큰으로 이전에 가져오기하여 기록한 행(`okf:` `source_trace`) 중 키가 더 이상 번들에 없는 행을 `memory.delete_version`으로 삭제합니다. `--agent`가 필요합니다             |

`--prune`을 사용하여 네임스페이스로 가져오는 각 번들에는 고유한 `--agent`가 필요합니다. prune은 해당 에이전트의 행 중 `okf:` 트레이스가 있고 그 토큰으로 기록되었으며 번들에 없는 행을 삭제합니다. `--prune`은 개념이 없는 번들과, 개념 파일의 해시가 루트 인덱스의 `loom.corpus_digest`와 일치하지 않는 번들을 거부하므로, 직접 편집한 번들이 삭제를 일으키는 일은 없습니다. 루트 인덱스에 `loom.complete: true`가 없는 번들은 기록은 되지만 prune되지 않습니다. 내보내기는 모든 행을 읽었을 때만 이 표시를 기록합니다. 즉, 목록을 만든 뒤 `memory.keys`가 한 문 안에서 읽은 모든 행이 있고, 나열된 모든 행이 `memory.bulk_get`에서 반환된 경우입니다. `--limit`에서 멈췄거나, (삭제된 행을 번들에 유지하는) `--include-tombstoned`를 사용했거나, 이전 릴리스에서 내보낸 경우에는 기록하지 않습니다. 각 삭제는 가져오기의 목록이 `okf:` 트레이스와 함께 본 버전으로, 정확히 그 ID 아래에서 `memory.delete_version`을 통해 수행됩니다. 그 뒤에 다시 쓰였거나 `memory.lock`으로 잠긴 행은 유지되며, 이 op를 모르는 오래된 대상은 아무것도 삭제하지 않습니다. prune에는 빈 디렉터리로 내보내세요. 내보내기는 개념 파일을 덮어쓰지만 네임스페이스에서 사라진 키의 파일은 지우지 않으므로, 재사용한 `--out` 디렉터리는 더 이상 다이제스트와 일치하지 않습니다. [네임스페이스를 다른 Loom으로 복사하기](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md)는 내보내기와 가져오기를 일정에 따라 실행합니다.

`--verify`는 증명을 위조하지 않습니다. 서버가 토큰에서 행위자를 결정하며, 번들의 원래 검증자는 출처 기록을 위해 메모리의 태그에 남습니다.

<a id="ci-gate"></a>

## CI 게이트

`integrations/okf-ci/okf-lint.yml`은 번들을 보관하는 저장소를 위한 GitHub Actions 워크플로입니다.

1. 이 파일을 해당 저장소에 `.github/workflows/okf-lint.yml`로 복사합니다.
2. `LOOM_REF`를 40자리 Loom 커밋 SHA로 설정합니다. 작업은 그 외의 값을 거부합니다(파일은 이 값이 비어 있는 상태로 제공됩니다). 규칙은 Loom과 함께 버전 관리되며, 브랜치나 태그는 성공 표시가 유지되는 상태에서도 가리키는 대상이 바뀔 수 있기 때문입니다.
3. `OKF_ROOT`를 번들 디렉터리, 즉 루트 `index.md`가 있는 디렉터리로 설정합니다(기본값 `.`). 하나의 네임스페이스에 대한 번들일 수도 있고, 저장소가 가진 여러 네임스페이스에 대한 매트릭스일 수도 있습니다.

작업은 번들 옆에 `LOOM_REF` 시점의 Loom을 체크아웃하고 `uv run --frozen`으로 린트를 실행합니다. 따라서 패키지는 해당 커밋의 `uv.lock`에서 가져옵니다. 즉 하나의 `LOOM_REF`는 하나의 규칙 집합과 하나의 의존성 집합에 대응합니다. 린트는 `--json`으로 실행되며, 요약과 처음 40개의 결과(심각도, id, 경로 순으로 정렬)를 작업 로그에 출력하고, `okf-lint.json`을 아티팩트로 업로드하며, 종료 코드 2이거나 린트가 실행되지 않은 경우(그 외의 모든 종료 코드)에는 실패합니다. 경고는 통과하며, 검토자를 위해 보고서가 보관됩니다.

모든 액션은 전체 커밋 SHA로 고정되어 있으며, 버전은 주석에 적혀 있습니다. 워크플로는 `contents: read` 권한만 요청하고, 두 체크아웃 모두 토큰을 git 설정에 남기지 않습니다(`persist-credentials: false`). 린트 자체는 네트워크 연결을 열지 않으며 ClickHouse도 필요하지 않습니다. 각 검사에 대한 설명은 `integrations/okf-ci/README.md`에 있습니다.

<a id="run-the-gate-locally"></a>

### 게이트를 로컬에서 실행하기

같은 명령은 같은 결과를 냅니다.

```bash
git clone https://github.com/ClickHouse/loom .okf-lint-loom
git -C .okf-lint-loom checkout <LOOM_REF>
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT>          # human report
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT> --json   # the CI report
```

<a id="limits"></a>

## 제한 사항

- **번들은 스냅샷입니다.** 내보낸 이후 Loom에서 변경한 내용은 다음 내보내기 전까지 번들에 반영되지 않습니다. 오래된 번들을 편집하기 전에 다시 내보내십시오.
- **삭제는 `--prune`을 지정한 경우에만 전달되며,** 가져오기가 기록한 행에만 적용됩니다. 번들에서 제거된 엣지는 삭제되지 않습니다.
- **민감도가 높은 행은 `--include-high`를 지정하지 않는 한 내보내지 않습니다.** 행의 `sensitivity` 필드가 high이거나 `sensitivity:high` 태그가 있으면 높은 것으로 간주합니다. 내보내기에서 민감도를 읽지 못했거나 알 수 없는 수준으로 읽은 행도 내보내지 않습니다. 이러한 행에 연결된 원본 청크와 세션 다이제스트도 마찬가지입니다. 또한 행과의 연결 여부와 관계없이, 텍스트에 높은 민감도 형태(자격 증명, 카드 번호, SSN 또는 명시된 PIN)가 포함된 원본 청크와 세션 다이제스트도 내보내지 않습니다. 원본 텍스트에는 민감도 컬럼이 없으므로 `LOOM_MEMORY_SENSITIVITY_AUTODETECT` 설정과 관계없이 `memory.set`이 실행하는 탐지기가 판단합니다. 민감도가 높은 행이 내보내진 경우 lint가 경고합니다.

<a id="related"></a>

## 관련 문서

- [메모리 쓰기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md) 및
  [충돌과 거부](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md): 가져오기가 거치는 과정입니다.
- [기록 이력과 출처](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md).
- [엔티티 그래프](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md): `relates_to`가 담는 타입이 지정된 엣지입니다.
