<!-- i18n: source=docs/reference/cli.md blob=ff91f4468115d6d9b0cb51b887479b6922e69b72 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md), which is authoritative.

<a id="cli-reference"></a>

# CLI 레퍼런스

`loom`은 Loom 배포를 실행하고, 유지 관리하고, 연결하기 위한 단일 명령줄 진입점입니다. 서버 패키지의 `loom` 콘솔 스크립트이며(`pyproject.toml`, `[project.scripts]` → `server.cli:main`), 파서 트리는 `server/cli/app.py`에 있고 명령 계열별 모듈은 `server/cli/` 아래에 하나씩 있습니다. 이 페이지 끝의 표는 해당 트리에서 생성됩니다.

<a id="where-it-runs"></a>

## 실행 위치

| 위치                                               | 방법                                                               | 용도                                                                                                   |
| -------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| 서버 이미지 내부                                   | `docker exec loom-server loom <command>` (웹 컨테이너)             | 배포 환경 자체 설정을 사용하는 운영자 명령: `doctor`, `migrate`, `mint-token`, `reembed`.              |
| 분리 배포의 workers 컨테이너 내부                  | `docker exec loom-workers loom <command>`                          | 웹 티어에서 분리해 실행하려는 장시간 유지보수 작업: `code backfill`, `code stats`.                     |
| 체크아웃                                           | 저장소 루트에서 `pip install -e .`를 실행한 뒤 `loom <command>`    | `up`, `install`, `demo`, 그리고 로컬 스택을 대상으로 한 개발.                                          |

컨테이너 내부에서 설정은 컨테이너의 환경 변수입니다. 체크아웃에서는 `loom`이 먼저 작업 디렉터리의 `.env`(또는 `LOOM_ENV_FILE`이 지정한 파일, 비워 두면 사용 안 함)를 compose의 `env_file`과 같은 방식으로 환경에 불러오며, 셸에서 이미 export한 변수는 덮어쓰지 않습니다. 이는 공급자 키에서 중요합니다. 키 없이 체크아웃에서 임베딩 명령을 실행하면 가짜 임베더로 대체되어 사용할 수 없는 벡터가 기록됩니다.

명령은 두 종류로 나뉩니다.

- **운영자 명령**은 `server/settings/`를 읽고 ClickHouse와 직접 통신합니다
  (`migrate`, `reembed`, `code backfill`, `revoke-token`, 백필과 복구 명령). 배포 환경의 자격 증명으로 실행되므로, 해당 자격 증명이 있는 곳에서 실행하세요.
- **클라이언트 명령**은 URL과 토큰으로 실행 중인 서버와 HTTP로 통신합니다 (`install`, `init`, `demo`, `memory-search`, `ingest-transcript`, `export-okf`, `import-okf`). 이 명령들은 `--server-url`(`demo`에서는 `--base-url`)을 받고, `init`을 제외하면 `--token`도 받으며, 플러그인 훅의 순서대로 값을 결정합니다. URL은 `LOOM_BASE_URL`, 그다음 `~/.loom/config.json`의 `url`, 없으면 `http://localhost:7777`입니다. 토큰은 `LOOM_API_KEY`, 그다음 `~/.loom/config.json`의 `token`, 그다음 `~/.loom/token`(`LOOM_API_KEY_FILE`로 위치 변경)입니다. `LOOM_CONFIG_FILE`은 설정 파일의 위치를 변경합니다.
  에이전트(워크로드 토큰이 있는 `LOOM_CLIENT_ID`)의 경우 `--token` 또는 `LOOM_API_KEY`만 사용하고 저장된 토큰은 사용하지 않으며, 둘 다 없으면 거부합니다(종료 코드 2). `loom install`은 `--token`이 무엇이든 거부하고, `loom init`은 아무것도 가입시키기 전에 거부하며, `loom call`은 워크로드 토큰을 직접 교환합니다.

`loom mint-token`에는 `LOOM_JWT_SECRET`만 필요하며, 토큰을 로컬에서 서명합니다.

**세션 명령**(`login`, `logout`, `whoami`, `call`, `token`)은 Python SDK 자체의 명령(`loom.cli`, SDK만 설치된 경우 `python -m loom …`)이므로, 이 명령을 쓰려면 `loom`에 `pip install clickhouse-loom`이 필요합니다. `loom login [--server URL]
[--profile NAME] [--no-browser] [--org ORG [--wait S]]`는 일회용 코드로 로그인하며, `--org`를 쓰면 라우터의 조회에서 지정된 해당 조직의 [셀](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)에 로그인합니다. `loom call <op|/path> [key=text|key:=json …] [--data JSON] [--org] [--namespace]`는 그 세션으로 모든 operation 또는 라우트에 접근하고, `loom token create|list|revoke`는 CI와 에이전트용 본인 토큰을 생성, 조회, 폐기합니다. [대시보드 없이 Loom 사용하기](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md)를 참고하세요.

<a id="conventions"></a>

## 규칙

**재작성 명령은 기본적으로 드라이 런입니다.** 저장된 행을 재작성하는 명령은 `--apply`를 지정하지 않으면 쓰게 될 내용을 보고하기만 합니다. `--dry-run`도 사용할 수 있으며 기본 동작을 명시적으로 나타냅니다. 두 플래그는 함께 쓸 수 없습니다(`server/cli/_apply.py`, `add_apply_flags`). 드라이 런이 성공하면 stderr의 마지막 줄에 `dry run: nothing was written; re-run with --apply to write`가 출력되므로, 명령의 `--json` 출력은 stdout에 단독으로 남습니다. 일부 명령은 같은 개념을 자체 플래그로 표현합니다(`loom entity-edges-invalidate-junk`는 `--dry-run`(기본값) 또는 `--apply`를 받고, `loom reembed --dry-run`은 쓰지 않고 행 수만 셉니다). 각 명령의 플래그는 아래 표에 정리되어 있습니다.

**모든 쓰기에는 주체가 명시됩니다.** 명령이 수행하는 쓰기에는 actor `process:cli:<command>`가 기록됩니다(`server/memory/domain/provenance.py`, `cli_actor`). 따라서 출처 정보(provenance)와 감사 로그를 통해 어떤 명령이 행을 썼는지 알 수 있습니다.

**모든 쿼리는 출처가 표시됩니다.** 명령이 실행하는 ClickHouse 쿼리에는 `log_comment` 태그 `cli.<command>[.<subcommand>]`가 붙고, 명령이 자체 `--traffic-class`를 받지 않는 한 읽기는 `background` 트래픽 클래스에서 실행됩니다. `loom serve`는 예외입니다. 이 명령은 서버 자체이므로, 서버의 요청과 워커 패스가 스스로 태그를 붙입니다.

**`--json`** 을 제공하는 명령에서 이 플래그를 쓰면 기계가 읽을 수 있는 결과 하나를 stdout에 출력하고, 사람이 읽는 보고서는 stderr에 남깁니다.

<a id="common-commands"></a>

## 자주 쓰는 명령

<a id="run-and-check-a-deployment"></a>

### 배포 실행 및 확인

```bash
loom up                          # from a checkout: create .env, boot Loom, its extractor and ClickHouse, print how to connect
docker exec loom-server loom doctor          # pre-boot checks + the /v1/health problem list, each with its fix
docker exec loom-server loom doctor --json   # the same, machine-readable
loom doctor --client             # also examine this machine as a coding-agent client
docker exec loom-server loom migrate --status              # applied and pending migrations
docker exec loom-server loom migrate --dry-run --strict    # what a boot would apply; exit 1 on ledger drift
```

`loom up`은 `/v1/health`가 응답할 때까지 기다리고(최대 `--timeout`, 기본값 300 s), 인증이 켜져 있으면 writer 토큰을 발급하며, 대시보드 URL과 첫 호출 예제를 출력합니다. `--no-build`는 캐시된 이미지를 재사용하고, 이미 health에 응답하는 스택은 `--rebuild`를 지정하지 않는 한 그대로 둡니다. `loom doctor`는 검사에 하나라도 실패하면 종료 코드 1로 끝납니다. `loom serve`는 로컬 사용을 위해 uvicorn 아래에서 컨트롤 플레인을 실행합니다(이미지는 `scripts/serve.sh`를 통해 이를 시작합니다). 마이그레이션과 업그레이드는 [업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)에서, 상태 확인은 [모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)에서 다룹니다.

<a id="tokens"></a>

### 토큰

```bash
# A writer token for one namespace, valid 30 days, printed on stdout
docker exec loom-server loom mint-token --subject my-bot --org dev \
  --namespaces local --roles writer --ttl-days 30

# A scoped token: per-namespace roles, in read-priority order
loom mint-token --subject triage --org acme \
  --namespace-role priv=admin --namespace-role pub=viewer --json

# Retire one token, or every token of a subject issued before an instant
docker exec loom-server loom revoke-token --jti <jti> --reason "leaked"
docker exec loom-server loom revoke-token --subject my-bot --issued-before 2030-01-01T00:00:00Z
```

영구 폐기에는 만료 시각을 지정할 수 없으며, 그 이전에 폐기된 토큰을 복원하도록 범위를 좁힐 수도 없습니다. 기존 기준 시각과 같거나 그 이후의 기준 시각을 선택하거나, 영구 폐기 시각에 토큰 시계 오차 허용치를 더한 값을 사용하십시오.

`--save`는 토큰을 출력하는 대신 `~/.loom/token`(또는 지정한 경로)에 씁니다. `--ttl-days`가 없으면 토큰은 만료되지 않으며 `loom revoke-token`으로만 폐기할 수 있습니다. `--read-scope own_user`는 각 요청이 지정한 `user_id`로 읽기를 제한하고, `--consumer NAME`은 [컨슈머 태그](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#the-consumer-tag)를 자격 증명에 바인딩합니다. [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)를 참고하십시오.

<a id="connect-a-client"></a>

### 클라이언트 연결

```bash
loom install --client cursor --token "$LOOM_API_KEY" --server-url https://loom.example.com
loom install --client claude-code --toolset coding+tracker --dry-run
loom init --agent triage-bot --server-url https://loom.example.com   # self-signup: a namespace + token
```

`loom install`은 클라이언트 설정에 MCP 서버 항목을 쓰거나 `claude mcp add` / `codex mcp add`를 실행합니다. `--client`를 지정하지 않으면 발견한 모든 지원 클라이언트에 연결합니다. [MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)를 참고하십시오. `loom init`은 토큰을 `~/.loom/token`에, 서버·조직·네임스페이스를 `~/.loom/config.json`에 저장합니다. 이 파일은 클라이언트 명령과 플러그인 훅이 읽습니다.

<a id="re-embed-after-changing-the-embedder"></a>

### 임베더 변경 후 재임베딩

```bash
docker exec loom-server loom reembed --dry-run          # count rows whose embedding_model is not the configured one
docker exec loom-server loom reembed --namespace support
docker exec loom-server loom reembed --delete-old        # re-embed, then delete the off-model rows
```

Loom은 스스로 재임베딩하지 않습니다. `LOOM_EMBEDDING_MODEL`을 변경한 후에는 `loom reembed`를 실행하십시오. 이 명령은 저장된 콘텐츠에서 벡터를 다시 생성하고 나머지 모든 컬럼은 그대로 유지합니다. `--in-database <named collection>`은 `aiEmbed`(ClickHouse 26.7 이상)를 사용해 ClickHouse 내부에서 테이블마다 하나의 `INSERT … SELECT`로 임베딩합니다. 고객 또는 ID 헤더가 설정되어 있거나 추론 게이트웨이가 필수인 경우에는 거부됩니다. `--resume`은 이전 실행의 `resume:` 줄이 가리키는 행 다음부터 이어서 실행합니다. [유지보수](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)를 참고하십시오.

<a id="index-a-repository"></a>

### 저장소 인덱싱

```bash
docker exec loom-workers loom code init --org acme --namespace code-myrepo
docker exec loom-workers loom code backfill --org acme --namespace code-myrepo --embed
docker exec loom-workers loom code stats --org acme --namespace code-myrepo
```

`code backfill`은 이어서 실행할 수 있습니다. `--phase index`는 파싱, 청킹, 심볼과 엣지 도출을 수행하고, `--phase embed`는 대기 중인 벡터를 채우며, `--embed`는 둘을 한 번에 실행합니다. 가짜 임베더로는 임베딩을 거부하며, `--allow-fake-embedder`(개발 전용)를 지정해야 합니다. `--org`와 `--namespace`의 기본값은 각각 `LOOM_CODE_ORG`와 `LOOM_CODE_NAMESPACE`입니다. 전체 절차, 컴파일러 티어, PR 이력 백필은 [코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)에서 다룹니다.

<a id="export-and-import"></a>

### 내보내기 및 가져오기

```bash
loom export-okf --namespace support --out ./support-okf            # a namespace as a Markdown bundle
loom okf-lint ./support-okf --strict                               # offline lint; exit 2 on a critical finding or, with --strict, a warning
loom import-okf ./support-okf --namespace support-copy
loom export-actions --format cedar -o loom.cedarschema             # the registry as a policy schema
```

`export-okf`는 `sensitivity=high`이거나 민감도를 알 수 없는 행을 `--include-high`를 지정하지 않는 한 제외합니다. 미러 저장소는 네임스페이스의 권한 부여 범위보다 더 넓은 독자층에 노출되기 때문입니다. 그런 행에 연결된 원시 청크와 다이제스트, 그리고 높은 민감도의 형태(자격 증명, 카드 번호, SSN 또는 명시된 PIN)를 담은 원시 텍스트도 그 행과 함께 내보내지 않습니다. `--include-high`는 이들을 내보내며, 서버는 내보낸 각 항목을 감사합니다. [OKF](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md)와 [액션 스키마 내보내기](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#exporting-the-action-schema)를 참고하십시오.

<a id="diagnose-and-repair"></a>

### 진단 및 복구

`loom report`는 `loom doctor` 출력과 함께 버그 양식의 Environment 필드를 출력하며, `--open`은 버전이 채워진 양식을 엽니다. `loom drops`는 네임스페이스의 메모리 수집 누락 원장을 출력합니다. `loom erasure-re-verify`는 실패한 삭제 saga를 다시 검사합니다. 백필 및 복구 명령(`integrity-repair`, `reconcile-status`, `repair-value-bytes`, `entity-edges-invalidate-junk`, `rechunk`, `lexical-stats-rebuild` 등)과 각각을 실행해야 하는 시점은 [유지보수](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)와 [문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)에 있습니다.

<a id="reading-the-table"></a>

## 표 읽는 방법

각 행은 하나의 명령 또는 하위 명령입니다. 전체 호출 형식, 도움말 텍스트의 첫 문장, 그리고 인자를 보여 줍니다. 위치 인자는 `<name>` 형태로, 옵션은 긴 형식으로 표시합니다. `loom <command> --help`는 전체 텍스트, 기본값, 선택 가능한 값을 출력합니다. 생성기(`scripts/codegen/docs_reference.py`, `render_cli`)는 커밋된 파서 스냅샷 `tests/unit/snapshots/cli_tree.json`을 읽습니다. `tests/unit/test_cli_surface_snapshot.py`는 이 스냅샷과 `tests/unit/snapshots/cli_help.json`이 실제 파서와 동일하도록 유지하므로, 새 플래그를 추가하면 두 스냅샷과 이 페이지를 다시 생성하기 전까지 CI가 실패합니다.

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
python scripts/codegen/docs_reference.py
```

<a id="commands"></a>

## 명령

<!-- BEGIN GENERATED: cli -->
<!-- Generated by `python scripts/codegen/docs_reference.py`; edit the source, not this region. -->
<!-- prettier-ignore-start -->

| 명령 | 요약 | 인자 및 플래그 |
| --- | --- | --- |
| `loom code` | 코드 인텔리전스 플레인 작업. |  |
| `loom code init` | bare clone과 초기 상태를 생성합니다. | `--org`, `--namespace` |
| `loom code backfill` | 전체 트리 인덱싱(재개 가능). | `--org`, `--namespace`, `--phase`, `--embed`, `--include-poisoned`, `--allow-fake-embedder`, `--no-fetch`, `--limit`, `--commit`, `--paths-file`, `--concurrency`, `--edges-second-pass`, `--no-edges-second-pass`, `--dialect-folder-pass`, `--embedding-model-suffix`, `--context-callees`, `--callers-from` |
| `loom code reconcile` | 내용 기반 최신성 검사: 청크가 브랜치 헤드에서의 마지막 변경보다 오래된 경로를 찾으며, --heal은 해당 경로를 다시 인제스트합니다(워터마크는 변경하지 않음). | `--org`, `--namespace`, `--heal`, `--limit`, `--max-commits`, `--no-fetch`, `--show` |
| `loom code compact` | 키당 물리적 사본 수가 --min-ratio에 도달하면 코드 벡터 테이블에 OPTIMIZE ... FINAL을 실행하고 병합이 끝날 때까지 기다립니다. | `--table`, `--min-ratio`, `--apply`, `--dry-run`, `--timeout-s` |
| `loom code reembed` | 네임스페이스의 라이브 청크를 두 번째 임베딩 모델의 레이블로 기록합니다: 데이터베이스 내부 처리, 재개 가능, 체크아웃 불필요. | `--org`, `--namespace`, `--embedding-model`, `--source-model`, `--prefix`, `--batch` |
| `loom code vector-twin` | ClickHouse 내부에서 네임스페이스의 청크 벡터에 대한 Matryoshka 접두사 트윈을 채웁니다: 재임베딩 없음, 재개 가능. | `--org`, `--namespace`, `--embedding-model`, `--all-namespaces` |
| `loom code crossref` | SQL/셸 테스트 -> 구현 C++ 참조 패스(code_test_map method=content_reference를 기록하며, 멱등). | `--org`, `--namespace` |
| `loom code coverage-import` | 구성된 소스의 가장 최근 테스트별 라인 커버리지 실행을 가져옵니다(code_test_map method=coverage와 code_coverage_files를 기록). | `--org`, `--namespace`, `--dry-run` |
| `loom code export-pairs` | 임베더/리랭커 파인튜닝을 위해 실제 트래픽(trace x outcome 조인)에서 (쿼리 -> 사용된 단위, 하드 네거티브) 검색 쌍을 내보냅니다. | `--org`, `--namespace`, `--days`, `--out` |
| `loom code backfill-prs` | GitHub의 병합된 PR 이력. | `--org`, `--namespace`, `--months`, `--max-prs` |
| `loom code repack` | 최근 병합된 PR의 컨텍스트 팩을 현재 팩 형식으로 다시 생성합니다. | `--org`, `--namespace`, `--days`, `--limit`, `--dry-run` |
| `loom code describe` | PageRank 상위 청크에 대한 LLM 문맥 한 줄 설명. | `--org`, `--namespace`, `--top-pct`, `--limit`, `--suffix`, `--concurrency` |
| `loom code answers-purge` | 캐시된 code.ask 답변을 툼스톤 처리합니다(합성 모델 업그레이드 후에는 더 약한 모델의 오래된 답변이 계속 제공될 수 있으며, 캐시는 인용된 파일이 변경될 때만 스스로 무효화됩니다). | `--llm-model`, `--all`, `--apply`, `--dry-run`, `--org`, `--namespace` |
| `loom code scip-full` | 컴파일러 티어 전체 재인덱싱을 수동으로 실행합니다(code_scip_full 워커가 기본적으로 매주 실행하며, 병합별 델타의 TU 한도 저하와 모든 드리프트를 복구합니다). | `--checkout`, `--jobs`, `--keep-delta`, `--apply-delta`, `--org`, `--namespace` |
| `loom code scip-lock-path` | 이 호스트에서 해당 항목의 컴파일러 티어 잠금 파일 경로를 출력합니다. | `--org`, `--namespace` |
| `loom code entry-lock` | 항목의 컴파일러 티어 잠금(이 호스트의 파일과 배포의 리스) 아래에서 명령을 실행합니다: 다른 패스가 잠금을 보유하면 75로 종료하고, 실행 중 리스를 잃으면 3으로 종료합니다(명령은 중지됨). | `--org`, `--namespace`, `--tier`, `<command>` |
| `loom code scip-delta` | 컴파일러 티어 델타 패스를 수동으로 한 번 실행합니다(code_scip_delta 워커와 같은 코드): 구성된 저장소마다 git_head와 scip_commit을 비교하고, 변경된 TU에 scip-clang을 실행한 뒤 적용합니다. |  |
| `loom code entries` | 구성된 저장소마다 한 줄: org namespace checkout lock-file(잠금 경로가 확인됨, 수동 실행은 `loom code entry-lock`을 통해 잠금을 획득). |  |
| `loom code warm` | 질문 목록(한 줄에 질문 하나, 또는 'question' 필드가 있는 JSONL)으로 code.ask 답변 캐시를 미리 채웁니다. | `<file>`, `--org`, `--namespace` |
| `loom code scip-retry` | 실패한 SCIP 배치를 pending으로 재설정합니다. | `--org`, `--namespace`, `<batch_id>` |
| `loom code backfill-reviews` | code_pr_history에 이미 있지만 저장된 리뷰 댓글이 없는 병합된 PR의 인라인 리뷰 댓글(최신순, PR당 GitHub 요청 1회). | `--org`, `--namespace`, `--prs` |
| `loom code backfill-assignees` | 이미 저장된 병합된 PR에 대해 code_pr_history.assignees(저장소가 리뷰어로 지정한 사람)를 채우며, 최신 닫힌 PR부터 요청당 100개씩 탐색합니다. | `--org`, `--namespace`, `--pages` |
| `loom code backfill-items` | 특정 날짜 이후 업데이트된 code_gh_items(이슈 + PR, 모든 상태)를 요청당 100개씩, 가장 오래된 업데이트부터 채우며 gh_items_cursor를 이동합니다. | `--org`, `--namespace`, `--since`, `--pages` |
| `loom code stats` | 워터마크와 라이브 행 수. | `--org`, `--namespace` |
| `loom code export-matrix` | 병합된 PR의 공동 변경에서 도출한 유지보수 매트릭스: '이 파일이 변경되면 보통 이 파일들도 함께 변경된다'를 근거와 함께 지침 파일(AGENTS.md / copilot-instructions.md)용 Markdown으로 출력합니다. | `--org`, `--namespace`, `--prs`, `--min-together`, `--min-conf`, `--max-partners`, `--max-rows`, `--out`, `--json` |
| `loom code lint-instructions` | 경로나 심볼 참조가 클론의 헤드에서 더 이상 확인되지 않는 에이전트 지침 파일(CLAUDE.md, AGENTS.md, copilot-instructions, .claude/.md, skills). | `--org`, `--namespace`, `--file`, `--glob`, `--symbols`, `--all`, `--json`, `--fail-on-stale` |
| `loom decisions` | 문서에서 추출한 결정 레코드. |  |
| `loom decisions extract` | 지식 문서 하나를 결정 레코드로 추출합니다: 모델이 문서를 통째로 읽으며(LOOM_MEMORY_DECISION_EXTRACT_WINDOW_CHARS를 넘으면 전체 섹션 단위의 윈도우로 읽음, 이미지만 있는 제목은… | `--org`, `--namespace`, `--source-uri`, `--repo`, `--into`, `--max-per-section`, `--window-chars`, `--apply`, `--dry-run` |
| `loom knowledge` | 지식 문서와 그 소스. |  |
| `loom knowledge sync` | 저장소 네임스페이스의 문서 소스에 대해 동기화 패스를 지금 한 번 실행합니다: 각 소스의 변경된 파일을 나열하고(--full이면 모든 파일), 변경된 파일은 문서로 기록하며 전체 목록에 더 이상 없는 문서는 폐기합니다… | `--org`, `--namespace`, `--source`, `--full` |
| `loom knowledge source` | 실행 중인 서버를 통한 네임스페이스의 문서 소스. |  |
| `loom knowledge source list` | 네임스페이스의 소스, 항목, 동기화 상태를 나열합니다. | `--namespace`, `--org`, `--server-url`, `--token` |
| `loom knowledge source add` | 링크나 항목으로 소스를 등록하며, 저장하기 전에 자격 증명으로 확인합니다(knowledge.source.add). | `--namespace`, `--org`, `--server-url`, `--token`, `--name`, `--kind`, `--link`, `--item-json`, `--include`, `--credential-file`, `--dry-run` |
| `loom knowledge source update` | 소스의 항목을 추가·제거하거나 파일 종류를 변경하며, 추가된 항목은 저장된 자격 증명으로 확인합니다(knowledge.source.update). | `--namespace`, `--org`, `--server-url`, `--token`, `--name`, `--link`, `--item-json`, `--include`, `--remove-link`, `--remove-item-json` |
| `loom knowledge migrate-originals` | 문서, 업로드 또는 인제스트에 필요하지만 아직 ClickHouse에 보관된 문서 원본을 LOOM_DOCUMENT_S3_BUCKET으로 옮깁니다: 각각 자체 객체로 복사하고 해시를 검증한 뒤 전환하며, 다음으로 보호됩니다… | `--org`, `--namespace`, `--dry-run` |
| `loom export-okf` | 네임스페이스를 OKF v0.2 번들(Markdown + frontmatter)로 렌더링합니다. Loom 메모리를 git에서 검토할 수 있게 만든 투영입니다. | `--server-url`, `--token`, `--org`, `--namespace`, `--out`, `--title`, `--include-tombstoned`, `--limit`, `--dry-run`, `--no-evidence`, `--no-links`, `--include-high` |
| `loom import-okf` | OKF v0.2 번들의 개념을 memory.set을 통해 네임스페이스에 기록합니다. | `<path>`, `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--session-id`, `--keep-sessions`, `--verify`, `--dry-run`, `--no-evidence`, `--no-lint`, `--force-import`, `--no-links`, `--prune` |
| `loom okf-lint` | OKF 번들 디렉터리를 오프라인으로 린트합니다. 미러 저장소의 CI가 실행하는 컴파일 게이트입니다. | `<path>`, `--json`, `--strict` |
| `loom up` | 로컬 스택(Loom, 문서 추출기, ClickHouse)을 시작하고 연결 방법을 출력합니다: 템플릿에서 .env와 ClickHouse 설정을 만들고, docker compose up --build를 실행하며, 다음을 기다립니다… | `--root`, `--file`, `--env-file`, `--no-build`, `--rebuild`, `--port`, `--timeout`, `--subject`, `--org`, `--namespace`, `--open` |
| `loom down` | 데이터를 보존한 채 로컬 스택을 중지합니다. --delete-data는 확인을 거쳐 초기화를 수행합니다. | `--root`, `--file`, `--env-file`, `--delete-data`, `-y` |
| `loom ch-setup` | Loom용으로 자체 관리 ClickHouse 또는 ClickHouse Cloud 서비스를 준비합니다: loom 데이터베이스와 읽기 전용 reader 사용자를 만들거나, 관리자를 위한 SQL을 출력합니다. | `--print-sql`, `--print-analyst-sql` |
| `loom participant` | 프로바이더 변경이 기다리는 Loom 프로세스. |  |
| `loom participant retire` | 종료하지 않고 죽은 프로세스의 프로바이더 참여자를 폐기합니다(해당 프로세스의 /v1/health에 있는 `participant_id`). | `--participant`, `--yes` |
| `loom deployment` | 모든 레플리카가 스스로를 대조하는 구성. |  |
| `loom deployment publish` | 이 환경의 구성을 배포의 구성으로 게시하며, 루트 시크릿이나 LOOM_EMBEDDING_MODEL/DIM을 의도적으로 교체합니다. | `--yes`, `--check` |
| `loom settings` | 적용 중인 모든 설정값과 라이브 설정의 오버라이드. |  |
| `loom settings list` | 각 설정의 값, 출처, 라이브 여부. | `--prefix`, `--changed`, `--json` |
| `loom settings set` | 배포 전체에 대해 라이브 설정을 오버라이드합니다. 재시작 후에도 유지됩니다. | `<name>`, `<value>`, `--expect` |
| `loom settings reset` | 오버라이드를 제거하여 환경 변수 또는 기본값으로 되돌립니다. | `<name>`, `--expect` |
| `loom settings history` | 기록된 변경을 최신순으로 표시: 변경한 사람, 변경 전후의 값. | `<name>`, `--limit`, `--json` |
| `loom secrets` | ClickHouse에 암호화되어 저장된 자격 증명과 서명 키. |  |
| `loom secrets reseal` | LOOM_JWT_SECRET_PREVIOUS로만 열리는 항목을 LOOM_JWT_SECRET으로 다시 봉인합니다. | `--dry-run`, `--check` |
| `loom signing-key` | ClickHouse에 보관되는, LOOM_JWT_SECRET 외의 토큰 서명 키. |  |
| `loom signing-key rotate` | 키를 추가합니다. 모든 프로세스가 로드한 뒤에 서명에 사용됩니다. | `--algorithm`, `--activate-in` |
| `loom signing-key retire` | 키('default' 포함)가 토큰을 검증하지 못하도록 중단합니다. | `<kid>` |
| `loom signing-key record-root` | 의도적으로 교체한 뒤, 이 LOOM_JWT_SECRET을 배포의 루트 시크릿으로 기록합니다(이전 시크릿으로 봉인된 키는 계속 사용할 수 없음). |  |
| `loom signing-key list` | 키를 나열합니다. |  |
| `loom provider-lock` | 프로바이더 구성 잠금. |  |
| `loom provider-lock clear` | 프로세스가 중지된 보유자의 프로바이더 구성 잠금을 해제합니다(갱신, 하트비트 또는 실행 중인 쿼리가 있으면 거부됨). | `--holder` |
| `loom export-analytics` | UTC 기준 하루치 사용량과 네임스페이스 스냅샷을 Parquet으로 S3에 기록하며, 이름은 LOOM_ANALYTICS_EXPORT_NAME_KEY로 해시됩니다. | `--bucket`, `--prefix`, `--day`, `--role-arn`, `--region`, `--expected-bucket-owner` |
| `loom sql-access` | sql_v1_* 뷰를 통해 테넌트의 메모리, 대화, 요청 원장을 읽는 ClickHouse 사용자를 만들거나, 범위를 조정하거나, 삭제합니다. |  |
| `loom sql-access grant` | 사용자가 정확히 이 범위만 읽도록 하며, 없으면 생성하고 비밀번호를 한 번만 출력합니다. | `--user`, `--org`, `--namespaces`, `--all`, `--rotate` |
| `loom sql-access revoke` | 사용자의 범위를 회수한 뒤 사용자를 삭제합니다. | `--user` |
| `loom sql-access list` | 부여된 모든 범위 행을 SQL 사용자별로 표시합니다. |  |
| `loom reembed` | embedding_model이 구성된 임베더와 다른 벡터를 다시 임베딩합니다(LOOM_EMBEDDING_MODEL을 변경한 뒤 실행하며, Loom은 자동으로 재임베딩하지 않습니다). | `--org`, `--namespace`, `--batch-size`, `--in-database`, `--dry-run`, `--delete-old`, `--key-only`, `--resume` |
| `loom reextract` | 네임스페이스의 원본 증거(memory.set_from_messages 턴)를 실행 중인 서버의 쓰기 경로를 통해 다른 네임스페이스로 재생합니다. 저장된 대화록에 현재 추출기를 적용하며, 소스는… | `--org`, `--namespace`, `--into`, `--base-url`, `--token`, `--dates`, `--concurrency`, `--limit-sessions`, `--dry-run` |
| `loom deploy-delta` | 컬러 스왑이 바꾼 것: 양쪽 구간에서 배포의 원장 행(작업별 볼륨, 오류, p50/p95, 업스트림 비용)과 ClickHouse 구문 형태(실행 횟수, 바이트, 초). | `--swap-at`, `--window-min`, `--ch-os-user`, `--cluster`, `--top`, `--json` |
| `loom entity-export` | 네임스페이스의 엔티티 그래프(동시 출현 에지 + 트리플)를 외부 그래프 또는 RDF 도구용 GraphML이나 JSON-LD로 내보냅니다. | `--org`, `--namespace`, `--format`, `--output`, `--all` |
| `loom audit-namespace` | 네임스페이스가 사용되는 방식(namespace.audit): 정확한 키 읽기 대 검색, 해시 키, 이중 인코딩되거나 너무 큰 값, 수작업으로 만든 인덱스 행, 메모리로 저장된 리스, 버전 변동. | `--org`, `--namespace`, `--window-days`, `--samples`, `--json` |
| `loom mentor` | 한 에이전트에 대한 스토어의 조언(mentor.brief): 에이전트가 호출한 것, 네임스페이스가 제공하는 플레인, 근거가 있는 힌트, 과거 힌트가 얼마나 자주 따라졌는지. | `--org`, `--namespace`, `--agent`, `--window-days`, `--json` |
| `loom serve` | 컨트롤 플레인을 시작합니다(uvicorn). | `--host`, `--port`, `--log-level`, `--reload` |
| `loom migrate` | 구성된 CH에 스키마 마이그레이션을 적용합니다. | `--status`, `--dry-run`, `--strict`, `--target`, `--migrations-dir` |
| `loom reconcile-status` | 상태가 loom.memory와 달라진 memory_embeddings 행(풀이 보지 못한 superseded / conflicted / duplicate 표시)을 다시 내보내며, 식별 행(org, namespace, agent… | `--org`, `--namespace`, `--all-namespaces`, `--since-days`, `--exclude`, `--limit`, `--apply`, `--dry-run` |
| `loom period-digest` | 기간 요약을 작성합니다. 한 네임스페이스에서 각 식별자의 연속된 K개 세션 요약에 대한 파생 시맨틱 메모리 하나입니다. | `--org`, `--namespace`, `--sessions`, `--max-identities`, `--max-periods`, `--apply`, `--dry-run`, `--rewrite` |
| `loom demote-activity-narration` | 지속적인 결과 없이 도구 활동을 서술하는('User read lines 1-1000 of X', 'Claude ran a command on …') 네임스페이스의 추출된 사실을 찾아 강등(신뢰도 x0.25)하거나 다음을 통해 레이블을 지정합니다… | `--org`, `--namespace`, `--action`, `--classifier`, `--days`, `--limit`, `--apply`, `--dry-run` |
| `loom recalibrate` | 임베더를 다시 프로빙하고 네임스페이스별 보정을 즉시 실행합니다(그렇지 않으면 재보정 워커가 매주 실행). |  |
| `loom import-clickgap` | ClickGap(Detector→Validator→Reviewer) SQLite 내보내기를 Loom으로 가져옵니다(레슨 + 타입 엔티티 + 카테고리). | `--sqlite`, `--org`, `--namespace`, `--agent`, `--dry-run`, `--batch-size`, `--seed-recall-events` |
| `loom import-recall` | 외부 벡터 리콜 에이전트 메모리 SQLite 내보내기를 Loom으로 가져옵니다. | `--sqlite`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom import-brain` | Markdown 에이전트 'brain' 디렉터리를 Loom으로 가져옵니다: MEMORY.md(항상 주입되는 HOT 레이어) → profile.set 섹션, memory/contexts/*.md(필요 시 읽는 WARM 레이어) → knowledge.add 문서. | `--dir`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom claim-release` | 셀프 가입 클레임을 해제합니다: 권한을 취소하고, 소유자를 지우고, 비활성화합니다. | `--org`, `--namespace`, `-y` |
| `loom mint-token` | JWT를 로컬에서 발급합니다(LOOM_JWT_SECRET 필요). | `--subject`, `--org`, `--namespaces`, `--roles`, `--platform-admin`, `--namespace-role`, `--ttl-days`, `--save`, `--read-scope`, `--read-user-id`, `--traffic-class`, `--consumer`, `--service-account`, `--user-token`, `--json` |
| `loom revoke-token` | jti 또는 subject로 토큰 하나를 취소합니다(취소 모듈을 통해 loom.revoked_tokens에 기록하며, 구성된 ClickHouse가 필요). | `--jti`, `--subject`, `--list`, `--issued-before`, `--scim`, `--reason`, `--expires-at`, `--json` |
| `loom sso` | 비밀번호 계정을 싱글 사인온으로 이전합니다. |  |
| `loom sso adopt` | SSO 로그인이 --domain의 이메일을 가진 비밀번호 계정에 연결되도록 합니다. 이후 각 사용자는 ID 제공자로 로그인하여 자신의 계정, 네임스페이스, 데이터에 접근합니다. | `--domain`, `--org`, `--skip`, `--keep-tokens`, `--apply`, `--dry-run` |
| `loom sso link` | 한 사용자의 SSO 로그인이 다른 주소(개인 주소, +tag)로 등록된 비밀번호 계정에 연결되도록 합니다. | `--email`, `--org`, `--namespace`, `--apply`, `--dry-run` |
| `loom sso unlink` | 한 이메일의 연결을 해제합니다. 해당 권한 부여로는 더 이상 그 계정이 허용되지 않습니다. | `--email`, `--apply`, `--dry-run` |
| `loom sso password-link` | 싱글 사인온을 끈 뒤 사용할 수 있도록, 한 이메일의 SSO 계정 비밀번호를 설정하는 일회용 링크(LOOM_PASSWORD_LINK_TTL_SECONDS 동안 유효하며 기본값은 하루)를 출력합니다. | `--email`, `--org` |
| `loom bot` | 키 또는 CI의 ID로 로그인하는 봇과 CI 작업. |  |
| `loom bot create` | 봇을 등록합니다. 봇의 네임스페이스와 키(--generate-key 또는 --public-key), 그리고/또는 CI 신뢰(--issuer, --audience, --claim)를 지정합니다. | `--name`, `--org`, `--namespace-role`, `--generate-key`, `--public-key`, `--issuer`, `--audience`, `--claim`, `--jwks-uri`, `--max-token-age`, `--max-access-token-ttl`, `--max-agents`, `--agent-claim`, `--platform-admin`, `--replace` |
| `loom bot list` | 활성 상태이거나 비활성화된 모든 봇. |  |
| `loom bot disable` | 봇의 증명을 거부하고 봇이 가진 토큰을 폐기합니다. | `--name` |
| `loom doctor` | 부팅 전 설정 오류(Python 버전, 설정, JWT, CH 연결 가능 여부)를 진단하고, /v1/health가 전달하는 문제 목록을 각각의 해결 방법과 함께 출력합니다. | `--json`, `--client`, `--server-url`, `--token` |
| `loom report` | 버그 양식의 Environment 필드를 출력합니다. 버전과 `loom doctor` 검사 결과가 포함됩니다. | `--open` |
| `loom drops` | 네임스페이스의 메모리 수집 드롭 원장(loom.memory_drops)을 단계/사유별로 출력합니다. 파이프라인이 버리거나, 잘라내거나, 포기한 항목입니다. | `--namespace`, `--org`, `--hours` |
| `loom namespaces-gc` | 임시 네임스페이스를 나열(기본값)하거나 삭제합니다. 대상은 벤치 접두사 아래에서 완료된 벤치 실행 중 아무도 등록하지 않았고 --idle-hours 동안 아무도 쓰지 않은 것이며, 목록 단위의 삭제 saga 하나로 처리합니다… | `--org`, `--prefix`, `--namespace`, `--exclude-prefix`, `--idle-hours`, `--include-registered`, `--erase`, `--no-purge`, `--skip-external`, `--batch`, `--show`, `--requester`, `--worker-pass` |
| `loom erasure-re-verify` | 실패한 삭제 saga를 다시 검증합니다. 각 saga의 마지막 단계 이름이 붙은 사유와 새 잔여물 집계를 출력하며(기본값, 읽기 전용), --resume은 복구 워커가 멈춘 체크포인트부터 다시 실행합니다… | `--org`, `--id`, `--resume`, `--skip-external`, `--completed-since`, `--orphan-graph`, `--erase`, `--batch`, `--show`, `--requester` |
| `loom reconcile` | ClickHouse 또는 Postgres를 복원한 뒤, 복원 시점 이후 나머지 저장소(ClickHouse 또는 Postgres)가 기록한 삭제와 개인정보 보호를 위한 명시적 제거·마스킹(redaction)을 복원된 저장소에서 다시 실행하고, (ClickHouse 복원 후에는) 모든 항목을 다시 게시합니다… | `--restored`, `--point`, `--allow-live`, `--apply`, `--dry-run` |
| `loom router` | 배포의 셀 앞단에 있는 라우터. |  |
| `loom router serve` | 배치, 조회, 운영자 API를 제공하고 그 외의 요청은 셀로 라우팅합니다. | `--host`, `--port`, `--log-level` |
| `loom router migrate` | 디렉터리의 대기 중인 마이그레이션을 적용합니다. |  |
| `loom cell` | 라우터의 디렉터리를 통해 배포의 셀을 운영합니다. |  |
| `loom cell credential` | 라우터 자격 증명을 만들거나 셀의 자격 증명을 교체합니다. | `<action>`, `--cell`, `--next`, `--finish`, `--revert`, `--operator`, `--router` |
| `loom cell limit` | 셀이 새 조직을 받지 않도록 닫거나, 다시 열거나, 제한합니다. | `--cell`, `--closed`, `--open`, `--placements-max`, `--router` |
| `loom cell which` | 조직에 대한 조회 결과의 셀과 호스트를 출력합니다(배치되지 않았으면 첫 번째 셀). | `--org`, `--router` |
| `loom cell remove` | 배치 없이 연결된 셀의 디렉터리 행을 제거합니다. | `--cell`, `--router` |
| `loom cell prepare` | 이 셀을 라우터의 디렉터리에 등록하고 해당 행을 기록합니다. | `--cell`, `--host`, `--credential-sha256`, `--probe-url`, `--wait`, `--router` |
| `loom tracker-probe` | 트래커 소스와의 읽기 전용 첫 접촉: 자격 증명별 업스트림 테이블마다 개수와 최신 타임스탬프, 차단기 상태, 구성된 트래커 네임스페이스를 보여줍니다. |  |
| `loom tracker-contract-check` | 읽기 전용: 각 트래커 소스의 스키마가 레코드 계약(docs/features/tracker/record-contract.md)을 충족하는지 확인합니다. | `--source`, `--namespace`, `--json` |
| `loom tracker-dupcheck-fit` | 한 트래커 네임스페이스에 대해 tracker.duplicate_check의 보정된 신뢰도를 지금 다시 적합합니다. 등록 시점의 중복 정답을 재생하고, 적합하여 결과를 저장합니다. | `--namespace`, `--org`, `--negatives`, `--dry-run`, `--out` |
| `loom tracker-embed-reset` | 한 트래커 네임스페이스의 tracker_embed 패스 커서를 지워, 다음 패스가 구간을 다시 훑고 텍스트 레시피가 바뀐 모든 항목을 다시 임베딩하게 합니다. | `--namespace`, `--org`, `--kind`, `--apply`, `--dry-run` |
| `loom namespaces-adopt` | 라이브 메모리를 보유하지만 레지스트리 행이 없는 네임스페이스에 소유자 행을 부여합니다. 벤치 접두사 아래에서는 kind=bench, 그 외에는 memory이며, retention_days는 0이고 TTL은 없습니다. | `--org`, `--owner`, `--prefix`, `--namespace`, `--kind`, `--apply`, `--dry-run`, `--show` |
| `loom sensitivity-sweep` | 민감도 탐지기가 쓰기 시점에 레이블을 붙였어야 하지만 어떤 경로에서 건너뛴 라이브 행에 레이블을 붙입니다. | `--org`, `--namespace`, `--since-days`, `--limit`, `--show`, `--only-shape`, `--apply`, `--dry-run` |
| `loom integrity-repair` | 행 단위 무결성 게이지(벡터를 가진 dim-0 행, tombstoned_at/status 불일치, valid_to가 지난 활성 행)를 읽고, --apply를 쓰면 dim-0 행을 실제 차원으로 다시 내보내 검색이… | `--org`, `--namespace`, `--limit`, `--show`, `--apply`, `--dry-run` |
| `loom rechunk` | 저장된 콘텐츠에서 네임스페이스의 지식 문서를 현재 청커로 다시 청킹하고 다시 임베딩하여, 모든 문서가 섹션 메타데이터, 청크 컨텍스트, 문서 빈도를 갖게 합니다. | `--org`, `--namespace`, `--source-uri`, `--apply`, `--dry-run` |
| `loom export-actions` | 오퍼레이션 레지스트리에서 파생된 액션 스키마를 내보냅니다. loom 확장이 포함된 MCP tools/list 매니페스트(json) 또는 정책 게이트웨이가 표면을 통제하는 데 쓰는 Cedar 스키마(cedar)입니다. | `--format`, `--output` |
| `loom lexical-stats-rebuild` | 네임스페이스의 라이브 행에서 BM25 문서 빈도를 다시 계산합니다(용어 빈도 뷰는 삽입만 보므로, 대체와 삭제 표시가 오래된 어휘를 남깁니다). | `--org`, `--namespace`, `--no-knowledge`, `--no-memory` |
| `loom version` | 설치된 Loom 버전을 출력합니다. |  |
| `loom install` | Loom을 로컬 AI 클라이언트의 설정에 연결합니다. | `--client`, `--server-url`, `--token`, `--name`, `--toolset`, `--dry-run`, `--force` |
| `loom demo` | 처음 5분: 이 저장소의 AGENTS.md(또는 --file)를 `make ingest`와 같은 방식으로 지식 플레인에 수집하고, 질문을 던져 근거 행과 함께 답변을 출력합니다. | `--file`, `--question`, `--tags`, `--base-url`, `--token`, `--org`, `--namespace`, `--root`, `--search-only` |
| `loom init` | Loom 서버에 ID를 만듭니다. | `--agent`, `--server-url`, `--org`, `--purpose`, `--ttl-days`, `--token-path`, `--config-path`, `--no-files`, `--json` |
| `loom ingest-transcript` | 대화 기록 파일(markdown / JSON)을 하나의 스레드로 Loom의 대화 테이블에 파이프합니다. | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--thread-id`, `--file`, `--dry-run` |
| `loom memory-search` | 셸에서 memory.search를 실행합니다. | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--query`, `--top-k`, `--search-mode`, `--json-agent` |
| `loom set-host` | 이 배포의 Loom 호스트를 지정합니다. | `<emails>`, `--env-file`, `--append`, `--dry-run` |
| `loom login` | 일회용 코드로 어떤 브라우저에서든 Loom 서버에 로그인합니다. |  |
| `loom logout` | 서버에서 저장된 세션을 종료하고 이곳에서도 삭제합니다. |  |
| `loom whoami` | 저장된 세션이 어떤 사용자로 로그인하는지 보여줍니다. |  |
| `loom call` | 저장된 세션으로 임의의 오퍼레이션이나 API 라우트를 호출합니다(`loom call --help`). |  |
| `loom token` | CI와 에이전트를 위해 본인의 토큰을 만들고, 나열하고, 폐기합니다(`loom token --help`). |  |

<!-- prettier-ignore-end -->
<!-- END GENERATED: cli -->

<a id="related"></a>

## 관련 문서

- [운영 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md): 클라이언트 명령이 호출하는 op입니다.
- [유지 관리](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md): 재임베딩, 백필, 복구를 다룹니다.
- [코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md).
- [업그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md): 업그레이드 절차에서 `loom migrate`를 사용하는 방법을 설명합니다.
- [로컬 개발](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md).
