<!-- i18n: source=docs/development/testing.md blob=abc5f046c927795cb7b9c4d6a81dc5bc34b9cc85 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md), which is authoritative.

<a id="testing"></a>

# 테스트

테스트가 어느 스위트에 속하는지, 각 스위트를 실행하는 데 무엇이 필요한지, 환경이 갖춰지지 않았을 때 테스트가 어떻게 건너뛰는지, CI가 어떤 게이트를 강제하는지, 그리고 테스트가 고정해 둔 결과물을 어떻게 다시 생성하는지를 설명합니다.

<a id="the-suites"></a>

## 테스트 스위트

| 스위트                                          | 경로                                                                 | 필요 조건                                                                     | 로컬 실행                                                      | CI                                                             |
| ----------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------- |
| 단위 테스트                                     | `tests/unit/`                                                        | 외부 의존성 없음, ClickHouse 불필요                                           | `make test-unit` (`pytest bench -q`도 함께 실행)               | `unit`은 여러 부분으로 나눠 무작위 순서로 실행, `unit-coverage`는 커버리지 ≥ 40% |
| 벤치 하네스                                     | `bench/` (`test_*` 파일)                                             | 외부 의존성 없음                                                              | `pytest bench -q`                                              | `unit-coverage`                                                |
| 통합 테스트                                     | `tests/integration/`                                                 | 마이그레이션된 ClickHouse와 실행 중인 서버                                    | `LOOM_BASE_URL`을 지정한 `make test-integration`               | `integration`                                                  |
| 분석기 코퍼스                                   | `tests/analyzer/`                                                    | 마이그레이션된 ClickHouse (서버 불필요)                                       | `pytest tests/analyzer -q`                                     | `integration`                                                  |
| 코드 레인 코퍼스                                | `tests/code_lanes/`, `tests/unit/test_code_lanes_corpus.py`로 게이트 | 외부 의존성 없음                                                              | `python -m tests.code_lanes.corpus`                            | `unit`                                                         |
| 대시보드                                        | `frontend/src/**/*.test.tsx`                                         | Node                                                                          | `npm --prefix frontend run test` (vitest, axe 포함)            | `frontend-vitest`                                              |
| 대시보드 엔드 투 엔드                           | `frontend/e2e/*.spec.ts`                                             | Node, Chromium (`npx playwright install chromium`)                            | `npm --prefix frontend run e2e`                                | `frontend-e2e`                                                 |
| 플러그인 훅                                     | `integrations/*/tests/*.test.mjs`                                    | Node                                                                          | `node --test integrations/claude-code-plugin/tests/*.test.mjs` | `agent-plugins`                                                |
| 프레임워크가 extra인 Python SDK 어댑터          | `sdk/python/tests/`                                                  | `uv`, 각 어댑터 extra를 `.venv-sdk-adapters/` 아래 별도 venv에 설치           | `make test-sdk-adapters`                                       | `sdk-python-adapters`                                          |
| TypeScript SDK                                  | `sdk/typescript/`                                                    | Node                                                                          | `npm --prefix sdk/typescript run check`                        | `sdk-typescript`                                               |
| 벤치마크와 게이트                               | `bench/*.py`, `make bench-*`                                         | **실제** 프로바이더를 사용하는 서버                                           | [벤치마크 실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)                      | 실행하지 않음 (CI 임베더는 가짜)                               |

**벤치마크를 CI에서 제외하는 이유.** CI는 `LOOM_EMBEDDING_MODEL=fake-ngram-1024`와
`LOOM_LLM_PROVIDER=fake`로 실행되므로, 여기서 나온 recall 수치는 실제 검색 성능에 대해 아무것도 말해 주지 않으며, 가짜 임베딩에서 통과한 품질 게이트는 게이트가 없는 것보다 나쁩니다. CI에서 실행하려면 저장소에 유료 프로바이더 시크릿을 두어야 합니다. 게이트(`make bench-quick`, `bench-quality-gate`, `bench-code-intel`)는 추출 프롬프트, 기본 모델 또는 임베더를 변경하기 전에 실제 프로바이더를 사용하는 배포 환경을 대상으로 직접 실행합니다.

<a id="unit-tests"></a>

## 단위 테스트

- **ClickHouse를 사용하지 않습니다.** 단위 테스트는 클라이언트를 스텁으로 대체합니다. 실제 SQL 시맨틱이 필요한 테스트는 통합 테스트 또는 분석기 테스트 모음에 속합니다.
- **무작위 순서, 병렬 실행.** CI는 코어당 워커 프로세스 하나로 `-n auto -p randomly` 옵션을 사용해 테스트 모음을 실행하고 시드를 가장 먼저 출력합니다. 따라서 테스트는 다른 어떤 테스트 뒤에서도, 다른 어떤 테스트와 나란히 실행되어도 통과해야 합니다. [`tests/README.md`](https://github.com/ClickHouse/loom/blob/main/tests/README.md#rules-every-test-keeps)에 규칙과, 시드와 로그로 실패를 재현하는 방법이 있습니다.
- **비동기 테스트는 `await asyncio.sleep(...)`으로 대기합니다.** `time.sleep`은 절대 사용하지 않습니다.
- **실제 시계 기준 날짜.** 고정된 날짜를 사용하는 픽스처를 오래됨 검사가 `now()`와 비교하면, 그 날짜가 임계값을 넘어 오래되기 전까지만 통과합니다. 검사가 실제 시계와 비교하는 날짜를 하드코딩하지 말고, 코드의 시간 접점(time seam)을 통해 시계를 고정하십시오.

<a id="integration-tests"></a>

## 통합 테스트

통합 테스트 모음은 CI의 `integration` 작업과 같은 방식으로 실행 중인 서버를 대상으로 동작합니다.

```bash
bash dev/ci_start_clickhouse.sh                      # or the dev stack's ClickHouse
make migrate
LOOM_INSTANCE_LABEL=dev:$USER:tests LOOM_BACKGROUND_WORKERS=off make server &
LOOM_BASE_URL=http://localhost:7777 make test-integration
```

- `tests/conftest.py`는 저장소의 `.env`를 테스트 프로세스에 불러옵니다(없는 변수만 채웁니다). 그래서 테스트와 서버가 같은 provider를 선택합니다. 한 임베더로 기록된 행은 다른 임베더로 읽을 때 보이지 않습니다.
- 직접 내보낸 토큰이 없으면 각 테스트는 `LOOM_API_KEY`에 와일드카드 org 작성자 토큰을 받습니다(`agent` 픽스처가 이를 사용합니다). `mint_test_token` 픽스처는 테스트 자체 org용 토큰을 발급합니다. 두 토큰 모두 테스트 프로세스의 `LOOM_JWT_SECRET`으로 서명하며, 이 값은 서버의 값과 일치해야 합니다.
- 서버는 `LOOM_BACKGROUND_WORKERS=off`로 실행하세요. 워커를 검증하는 테스트는 자신의 네임스페이스를 대상으로 `run_pass`를 프로세스 내에서 직접 호출합니다. 실행 중인 supervisor의 워커에는 네임스페이스 필터가 없어서 이러한 테스트의 행을 가져가 버릴 수 있습니다. `knowledge_ingest`처럼 모든 테넌트가 공유하는 큐에서 한 번에 항목 하나를 처리하는 워커는, 테스트 자신의 항목을 한 번의 pass가 실행하듯 실행하여 검증합니다(`ingest_run.take_and_run`).
- `tests/integration/test_document_object_store_live.py`는 `LOOM_TEST_S3_ENDPOINT`에 S3 호환 저장소도 필요하며(CI는 로컬 저장소를 실행합니다. `.github/workflows/ci.yml` 참고), 없으면 건너뜁니다.
- `tests/integration/test_knowledge_ingest_live.py`의 게시 테스트는 원본을 문서 추출기로 보내는 자체 작업을 실행합니다. 따라서 SQL과 TOML에 라우팅된 추출기가 필요하며(`LOOM_DOCUMENT_EXTRACTOR_URL` 또는 `LOOM_DOCUMENT_EXTRACTORS_JSON`과 해당 토큰. [구성 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) 참고), 없으면 건너뜁니다. `tests/integration/test_knowledge_inspect_ingest.py`는 또한 `knowledge.inspect`를 통해 서버에 추출기가 구성되어 있는지 검증하므로, 서버에도 같은 설정을 지정하세요. CI는 각 파트의 서버를 시작하기 전에 참조 추출기를 시작합니다(`.github/workflows/ci.yml`, [실행 방법](https://github.com/ClickHouse/loom/blob/main/extractor/README.md)).
- `tests/integration/test_pg_authority.py`에는 Postgres 권한(authority)으로 동작하는 서버가 필요합니다. `dev/ci_start_postgres.sh`로 Postgres를 시작하고, `make migrate`를 실행한 뒤, 서버와 pytest 모두에 `LOOM_STORAGE_AUTHORITY=postgres`와 `LOOM_PG_DSN=postgres://loom:loomdev@localhost:5432/loom`(스크립트의 데이터베이스이며, `LOOM_PG_HOST_PORT`로 포트를 바꿀 수 있습니다)을 설정하세요. 이 테스트는 권한 코드도 테스트 프로세스에서 실행하기 때문입니다. `LOOM_PG_STANDBY=0`(동기 standby 없음)으로 시작한 Postgres를 쓸 때는 pytest 환경에도 `LOOM_PG_STANDBY=0`을 지정해야 standby 상태 점검 테스트를 건너뜁니다. ClickHouse 권한 서버에서는 API만 사용하는 테스트를 제외한 나머지가 모두 건너뛰어집니다.
- `tests/integration/test_pg_clickhouse_live_ch.py`는 `pg_clickhouse`가 설치된 Postgres에서 [SQL 인터페이스](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)를 읽습니다. `dev/ci_start_pg_clickhouse.sh`로 시작하고(호스트 네트워크, 포트 5433을 사용하므로 Linux Docker 호스트가 필요합니다. 컨테이너가 127.0.0.1에 게시된 ClickHouse에 접속합니다. `LOOM_PGCH_POSTGRES`와 `LOOM_PGCH_VERSION`으로 이미지를 고릅니다), 해당 ClickHouse의 `LOOM_CH_*`와 함께 `LOOM_PGCH_DSN=postgres://app:appdev@127.0.0.1:5433/app`을 설정하세요. 설정이 없으면 건너뜁니다. 끝난 뒤에는 컨테이너를 삭제하세요(`docker rm -f loom-pgch` 또는 지정한 `LOOM_PGCH_CONTAINER`).
- CI는 `LOOM_LATENCY_BUDGET_MULTIPLIER=10`을 설정합니다. 공유 러너는 노이즈가 커서 기본 op별 예산으로는 위반이 발생하기 때문입니다.
- 셀프 가입에 의존하는 테스트는 `LOOM_SELF_SIGNUP=open`이 없는 CI에서 건너뜁니다. 발급한 토큰을 사용하는 편이 좋습니다.
- **티어.** `tests/integration/conftest.py`는 빠른 로컬 실행에서 제외할 수 있는 네 가지 마커를 등록합니다(`-m "not slow and not destructive"`). CI는 디렉터리 전체를 파트로 나누어 실행합니다(아래 참고). `slow`는 일부러 실제 시간을 소모하고(리스 만료, 재시도 소진), `destructive`는 병합을 중단시키거나 `OPTIMIZE`를 실행하거나 스코프 전체를 삭제하며, `provider`는 실제 임베더나 LLM이 필요하고 가짜 구현에서는 건너뛰며, `recovery`는 장애를 일으킨 뒤 복구 경로를 검증합니다.
- **파트.** `LOOM_TEST_SHARD=k/n`은 n개 파트 중 k번째 파트의 테스트 파일만 수집합니다. 한 파트는 파일 전체를 담으므로 모듈 픽스처는 한 파트에서만 만들어집니다. 파일은 `tests/shard_weights.json`에 기록된 파일별 소요 시간(초)에 따라 배분되며, 목록에 없는 파일은 테스트 함수 수에 기록된 테스트 함수당 소요 시간을 곱한 값으로, 기록이 없으면 하나당 1초로 계산합니다. 이는 모든 통합 테스트 파일에 동일하게 적용됩니다(`tests/_shards.py`). CI는 스위트를 네 파트로 나누어 각각 별도의 스택에서 실행하며, 격리 게이트 파트가 따로 있습니다. 파트 k의 실패는 `LOOM_TEST_SHARD=k/4 make test-integration`으로 재현됩니다.
- **실패 진단.** 실패한 테스트의 보고서에는 마지막 Loom 요청 25개(메서드, URL, 상태, `X-Request-Id`, 지연 시간)가 포함되며 JWT, bearer 토큰, 시크릿은 가려집니다. 요청 ID로 해당 서버 로그 줄을 찾을 수 있습니다.
- **정리.** `unique_org(label)`(`tests/integration/_resources.py`)은 `it-<label>-<8 hex>`를 발급합니다. 세션이 끝나면 발급된 모든 org의 행을 `org` 열이 있는 각 테이블에서 삭제하고 남은 행을 셉니다. 남은 행이 있으면 실행이 실패합니다. `LOOM_IT_KEEP_ROWS=1`을 지정하면 디버깅을 위해 행을 보존합니다.

<a id="framework-adapters"></a>

## 프레임워크 어댑터

프레임워크가 SDK extra인 SDK 어댑터는 해당 프레임워크를 `uv.lock`에 포함하지 않으므로, `make test-sdk-adapters`는 단위 테스트(`sdk/python/tests/test_<module>.py`)를 위해 각 프레임워크를 `.venv-sdk-adapters/` 아래의 별도 venv에 설치합니다(두 프레임워크가 하나의 패키지를 서로 호환되지 않게 고정할 수 있기 때문입니다). `make test-sdk-adapter-<extra>`는 그중 하나만 실행합니다. 통합 테스트(`tests/integration/test_sdk_<framework>_*.py`)는 프레임워크가 없으면 건너뜁니다. 통합 테스트를 실행하려면 해당 extra의 요구 사항을 `.venv`에 설치하고
(`uv pip install --python .venv/bin/python -r <(python scripts/gates/sdk_extra_requirements.py langchain)`)
[통합 테스트](#integration-tests)에서와 같이 서버를 대상으로 실행합니다. `uv sync`를 실행하면 프레임워크가 다시 제거됩니다.

<a id="how-tests-skip"></a>

## 테스트가 건너뛰는 방식

환경이 갖춰지지 않은 테스트는 이유를 밝히고 건너뛰며, 변수가 없다는 이유로 실패하지 않습니다. `os.environ.get(...) or pytest.skip(...)`를 사용하고, `os.environ[...]`는 사용하지 않습니다. 공용 fixture도 이 규칙을 따릅니다. `control_plane_url`은 `LOOM_BASE_URL`이 없으면 건너뜁니다. `LOOM_BASE_URL`이 설정되어 있는데 `/v1/health`가 200으로 응답하지 않으면 서버가 없는 것이 아니라 서버가 고장난 것이므로, 해당 테스트는 URL과 오류를 남기며 실패합니다.
`require_real_embedder`는 가짜 임베더로 실행 중인 서버에서 검색 품질 테스트를 건너뜁니다.
분석기 스위트(`migrated_ch`)는 `LOOM_CH_*` 주소에서 응답하는 ClickHouse가 없거나 대상의 마이그레이션이 뒤처져 있으면, 주소와 해결 방법을 이유에 적어 건너뜁니다. 반면 `CI`에서는 작업이 ClickHouse를 시작하고 먼저 마이그레이션을 실행하므로, 같은 상황에서 건너뛰지 않고 실패합니다.

<a id="the-analyzer-statement-corpus"></a>

## 애널라이저 구문 코퍼스

Loom의 ClickHouse 구문은 여러 빌더가 다양한 플래그에 따라 만들어 내는 Python 문자열입니다.
단위 스텁은 어떤 문자열이든 받아들이기 때문에, 필터링된 컬럼을 가리는 별칭, `WITH` 없이 읽은 CTE, 서브쿼리 안에서 사용된 바깥쪽 별칭, 드라이버가 바인딩할 수 없는 파라미터 타입 같은 문제가 서버까지 도달하고, 오류가 아니라 행이 누락되는 형태로 조용히 실패하는 경로가 됩니다.
`tests/analyzer/`는 모든 빌더를 각 축에 걸쳐 실행하고, 구문을 형태별로 중복 제거한 뒤, 마이그레이션이 적용된 빈 테이블에 대해 각각 `EXPLAIN QUERY TREE`로 실행합니다. 이렇게 하면 위의 문제를 모두 몇 밀리초 안에 걸러낼 수 있습니다. `shapes.txt`는 커밋된 리뷰 대상 파일입니다. 구문의 형태를 바꾸는 풀 리퀘스트는 diff에 새로운 head를 보여 줍니다.

```bash
pytest tests/analyzer -q                              # what CI runs
python -m tests.analyzer.corpus --write               # regenerate shapes.txt (review the diff)
python -m tests.analyzer.corpus --case erasure --errors
```

[애널라이저 README](https://github.com/ClickHouse/loom/blob/main/tests/analyzer/README.md)는 케이스 형식, 마이그레이션이 적용된 대상이 필요한 이유, 그리고 `LOOM_CH_CAPTURE_STATEMENTS`로 캡처한 프로덕션 구문 형태를 가져오는 방법을 다룹니다.

<a id="ci-gates"></a>

## CI 게이트

대부분의 게이트는 단위 테스트이므로 `make test-unit`이 실행합니다. 나머지는 `make lint` 단계입니다.
각 게이트는 실패했을 때 무엇을 실행해야 하는지를 알려 줍니다.

| 게이트                            | 위치                                                                                 | 실패 조건                                                                                                                                                                                     |
| ------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 공개 문구                     | `tests/unit/test_public_copy.py`                                                      | 트리가 인접 제품, 사람, 내부 호스트나 네임스페이스를 언급하거나, 논문이나 설계 기록을 인용함. 공개 페이지가 경쟁사를 언급함                                                                                                                  |
| 문서 링크                       | `tests/unit/test_doc_links.py`                                                        | 루트 Markdown, `docs/`, 통합 README의 상대 링크나 `#anchor`가 해석되지 않음                                                                                                         |
| 설정 레퍼런스              | `tests/unit/test_settings_reference.py`, `settings_reference.py --check`              | 생성된 구성 레퍼런스가 설정 소스와 다르거나, 필드에 행이 없음                                                                                                 |
| 문서 레퍼런스                  | `tests/unit/test_docs_reference.py`, `docs_reference.py --check` (`make lint`)        | `docs/reference/` 아래 생성된 영역이 오래되었거나, op나 렌더러에 행이 없음                                                                                                           |
| 기본 env와 문서 일치          | `tests/unit/test_stock_env_matches_docs.py`                                           | 퀵스타트에 명시된 기본 `.env` 값이 `.env.example`과 어긋나거나, 문서화된 compose 명령에 `--env-file .env`가 없음                                                                 |
| 함수 길이                 | `scripts/gates/function_length_gate.py` (lint)                                        | 파일에 지나치게 큰 함수가 추가되거나, 목록에 있는 함수가 기준선보다 커짐                                                                                                               |
| mypy 래칫                    | `scripts/gates/mypy_gate.py` (`make typecheck`)                                       | 어떤 파일이든 strict mypy 오류가 `scripts/gates/mypy_baseline.json`보다 많음                                                                                                                   |
| 시크릿                         | `scripts/gates/check_no_secrets.py` (lint), `tests/unit/test_no_secrets_guard.py`     | 추적되는 파일에 자격 증명 형태의 값이 있음                                                                                                                     |
| 대시보드 접근성         | `biome lint frontend/src` (`make lint`, CI 작업 `format`)                              | 대시보드 `.tsx`가 접근성 위반을 추가함                                                                                                                     |
| 주체 간 유출         | `tests/integration/test_cross_principal_leakage.py`                                   | 카나리 nonce를 사용해 다른 테넌트로 실행한 읽기 op가 nonce를 반환하거나 500을 반환함. 페이로드는 각 op의 입력 모델에서 합성되므로, 새 읽기 op는 추가되는 날 바로 검사됨           |
| 워커 레지스트리 스냅샷        | `tests/unit/test_worker_registry_snapshot.py`, `snapshots/worker_registry.json`       | `defaults`, `all_on`, `production` 프로필에서 활성화된 워커 집합, 순서, 주기 또는 데드라인이 바뀜                                                                                                              |
| 워커 레지스트리 정합성          | `tests/unit/test_worker_registry_parity.py`                                           | `/v1/admin/workers.status`가 나열하는 워커와 슈퍼바이저가 실행하는 워커가 다름                                                                                                   |
| 마이그레이션 추가 전용       | `tests/unit/test_migrations_additive.py`                                              | 마이그레이션이 테이블이나 컬럼을 삭제하거나 이름을 바꾸거나, 선언된 재구축 밖에서 테이블을 교환함                                                                                                 |
| 마이그레이션 번호               | `tests/unit/test_migration_numeric_order.py`                                          | 두 마이그레이션 파일이 같은 번호를 사용함                                                                                                                                             |
| 스키마 검사                    | `schema-check` 작업: strict 드라이 런 후 `make schema-check`                          | 원장에 저장소에 없는 파일이 있거나, `schemas/schema.sql`이 모든 마이그레이션을 재생한 결과와 다름                                                                                                 |
| CLI 표면 스냅샷            | `tests/unit/test_cli_surface_snapshot.py`, `snapshots/cli_tree.json`, `cli_help.json` | `loom` 파서, 인자, 기본값 또는 `--help` 텍스트가 바뀜                                                                                                                                  |
| op 도달 범위 집계                 | `tests/unit/test_op_reach_census.py`, `op_reach_census.py --check`                    | op에 원시 HTTP 외의 도달 경로가 없는데 experimental이 아니거나, op에 대시보드 호출 지점도 `server/operations/dashboard.py` 항목도 없거나, `reach_manifest.py`가 오래됨 |
| op 표면                     | `tests/unit/test_op_surfaces.py`                                                      | op의 MCP 라우팅, 숨겨진 op 거부 또는 `server/operations/dashboard.py` 항목이 도달 범위와 일치하지 않음                                                                           |
| MCP 목록 예산              | `tests/unit/test_mcp_listing_budget.py`                                               | 기본 `tools/list`가 토큰 상한을 넘거나, 모델이 호출할 수 없는 도구를 가리킴                                                                                                               |
| 설정 접근                 | `tests/unit/test_settings_attribute_access.py`                                        | 코드가 `getattr(settings, "<field>", default)`로 설정을 읽음                                                                                                                              |
| 생성된 SDK 및 플러그인 사본 | `sdk_py_gen.py`, `sdk_ts_gen.py`, `agent_plugin_sync.py` `--check` (lint)             | 생성된 파일이 소스와 다름 ([코드 생성](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md))                                                                                                             |
| 포매팅                      | `format` 작업, 그리고 `lint`의 `ruff format --check` (로컬에서는 `make format-check`)      | ruff, Biome, Prettier, Taplo 또는 shfmt가 파일을 변경하게 됨                                                                                                                      |

의도한 변경에 맞춰 스냅샷을 다시 생성하려면 다음을 실행합니다.

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
LOOM_UPDATE_WORKER_SNAPSHOT=1 python -m pytest tests/unit/test_worker_registry_snapshot.py
python scripts/gates/function_length_gate.py --update   # only ever lowers the baselines
python scripts/gates/mypy_gate.py --update
```

래칫 기준선은 절대 올라가지 않습니다. `--update`는 더 낮은 수치만 기록하므로 부채가 파일 사이를 이동할 수 없습니다.

<a id="the-ci-workflow"></a>

## CI 워크플로

`.github/workflows/ci.yml`은 `main` 또는 `release/X.Y` 라인으로 향하는 모든 풀 리퀘스트와 두 브랜치에 대한 푸시에서 실행되며, Python 3.12(`sdk-python-package`는 다른 버전도 추가함)와 Node 22를 사용합니다. 풀 리퀘스트에 더 새로운 푸시가 올라오면 해당 풀 리퀘스트에서 실행 중인 워크플로는 취소됩니다. `main` 또는 릴리스 라인에 대한 모든 푸시는 각자의 동시성 그룹에서 끝까지 실행되므로, 한 머지 커밋의 실패가 다음 커밋에 가려지지 않습니다. 모든 액션은 전체 커밋 SHA로 고정하고(버전은 주석에 표기), 워크플로는 잡에 읽기 전용 저장소 접근 권한만 부여합니다(`permissions: contents: read`).

| 잡                     | 실행 내용                                                                                                                                                                                                                                                                                                                                                                                    |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `clickhouse`           | `dev/clickhouse_release.sh`가 지정한 ClickHouse 릴리스를 확인하여, `unit`, `schema-check`, `integration`이 같은 릴리스를 테스트하도록 합니다                                                                                                                                                                                                                                                 |
| `lint`                 | `ruff check`, `ruff format --check`, `sdk_py_gen.py`, `sdk_py_client_gen.py`, `sdk_py_admin_gen.py`, `agent_plugin_sync.py`, `settings_reference.py`, `settings_schema.py`, `docs_reference.py`, `op_reach_census.py`의 `--check`, SDK에 대한 `mypy --strict`, mypy 래칫, 함수 길이 게이트와 시크릿 게이트                                                                                    |
| `unit`                 | `pytest tests/unit`을 여섯 부분(`LOOM_TEST_SHARD`)으로 나누어 각각 별도의 러너에서, 모든 코어를 사용해 무작위 순서로 커버리지와 함께 실행합니다                                                                                                                                                                                                                                              |
| `unit-coverage`        | 각 부분의 커버리지를 합산해(하한 40%) 모듈별 하한을 확인한 뒤, `pytest bench`와 범용 통합 샘플(`integrations/generic/test.sh`)을 실행합니다. 이 잡의 `shard-weights` 아티팩트는 유닛 파일별 실행 시간(초)이며, 부분 간 편차가 커지면 `tests/shard_weights.json`으로 커밋합니다                                                                                                              |
| `sandbox`              | 실제 bubblewrap 아래에서 적대적 저장소 잡 샌드박스 스위트를 실행하며, 건너뛰면 실패합니다(`LOOM_TEST_REQUIRE_SANDBOX=1`)                                                                                                                                                                                                                                                                    |
| `schema-check`         | 새 ClickHouse(`dev/ci_start_clickhouse.sh`), 모든 마이그레이션, 엄격한 드라이런, `make schema-check`                                                                                                                                                                                                                                                                                         |
| `ts-typecheck`         | 대시보드 빌드(`tsc -b` + Vite)와 번들 예산(`npm run budget`)                                                                                                                                                                                                                                                                                                                                 |
| `format`               | 고정된 버전의 Biome, Prettier, Taplo, shfmt 검사와 `frontend/src`에 대한 Biome 접근성 린트                                                                                                                                                                                                                                                                                                   |
| `helm`                 | 모든 오버레이에 대한 `helm lint --strict`와 kubeconform, `deploy/okf-sync/cronjob.yaml`에 대한 kubeconform, 차트가 거부해야 하는 값, 그리고 `scripts/gates/helm_extractor_isolation.py`, `helm_metadata_egress.py`, `helm_assistant_isolation.py`                                                                                                                                              |
| `agent-plugins`        | 플러그인 훅과 grep shim에 대한 `node --test`, Cursor 훅 샘플(`integrations/cursor/test.sh`), 두 플러그인과 shim의 모든 훅, 라이브러리, `bin/` 헬퍼 스크립트에 대한 구문 검사, grep shim의 진입점이 실행 가능한지 확인하는 검사                                                                                                                                                              |
| `frontend-vitest`      | vitest(유닛, 컴포넌트, axe)를 별도 러너에서 여러 부분으로 나누어 실행합니다                                                                                                                                                                                                                                                                                                                  |
| `frontend-e2e`         | Playwright를 별도 러너에서 여러 부분으로 나누어 실행합니다                                                                                                                                                                                                                                                                                                                                   |
| `integration`          | 각 부분이 자체 ClickHouse, 마이그레이션, 참조 추출기, 기동된 서버(가짜 프로바이더, 워커 꺼짐)를 가지고 실행됩니다. 번호가 붙은 부분은 `pytest tests/integration`을 나누어(`LOOM_TEST_SHARD`) 실행하고, 격리 게이트 부분은 프로바이더 스테이징 테스트, 인증 활성 게이트, `pytest tests/analyzer`를 실행합니다                                                                                  |
| `integration-postgres` | 메모리, 지식, 격리, 멱등성, 삭제, 복원 ID 스위트와 인증 활성 게이트를 `LOOM_STORAGE_AUTHORITY=postgres`인 서버를 대상으로, 동기 스탠바이가 있는 Postgres 16과 18에서 실행합니다([Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md))                                                                                                                         |
| `mixed-version`        | `scripts/gates/mixed_version.py`: 하나의 ClickHouse 위에 N-1과 N을 나란히 빌드합니다. N-1은 `HEAD~1`이며, 풀 리퀘스트에서는 머지 커밋의 첫 번째 부모(머지 시점의 대상 브랜치), 푸시에서는 직전 커밋입니다                                                                                                                                                                                      |
| `sdk-typescript`       | `sdk_ts_gen.py --check`, `npm run check`(린트, 타입, 버전, README 예제, 테스트), `npm run check:dist`                                                                                                                                                                                                                                                                                         |
| `sdk-python-adapters`  | `make test-sdk-adapters`: 프레임워크가 SDK extra인 각 어댑터에 대해, `uv.lock` 바깥에서 해당 프레임워크가 설치된 전용 venv로 테스트를 실행하고, 해당 모듈과 extra가 필요 없는 어댑터(`__init__.py`, `langgraph.py`, `strands.py`)에 `mypy --strict`를 실행합니다. Python 3.12에서, 그리고 모든 직접 요구 사항(SDK 자체, extra, pytest)을 하한 버전으로 둔 3.11에서 실행합니다 |
| `sdk-python-package`   | uv로 SDK wheel을 빌드해 깨끗한 venv에 설치하고 `scripts/gates/sdk_wheel_smoke.py`를 실행합니다. Python 3.11, 3.12, 3.14에서 실행하며, 선언된 모든 의존성을 하한 버전으로 둔 3.11과 3.14에서도 실행합니다                                                                                                                                                                                      |

`.github/workflows/plugin-platforms.yml`은 변경이 `integrations/`, 플러그인 마켓플레이스, `.gitattributes` 또는 워크플로 자체를 건드릴 때 실행됩니다. Linux(x86_64, arm64, Alpine), macOS(Apple silicon, Intel), Windows(x86_64, arm64), WSL 2(Ubuntu, Node 18)에서 플러그인 및 grep shim 스위트를 실행한 다음, 체크아웃에서 플러그인을 설치한 실제 Claude Code와 Codex 세션을 실행합니다(Claude Code 플러그인 아래의 `tests/e2e/agent-session.mjs`: 로컬 가짜 서버가 모델 API와 Loom 양쪽에 응답). 또한 Ubuntu와 macOS 러너에서는 에이전트의 `PATH`에서 `node`를 제외하고, Ubuntu, macOS, Windows 러너에서는 `node`가 전혀 없는 상태(모든 훅이 턴을 막지 않고 실패함)로, Windows에서는 Git Bash 없이도 실행합니다.

`.github/workflows/codeql.yml`은 `main` 또는 `release/X.Y` 라인으로 향하는 풀 리퀘스트, 해당 브랜치에 대한 푸시, 매주, 그리고 요청 시 CodeQL을 실행합니다. Python, JavaScript/TypeScript, 워크플로 각각에 대해 `codeql (<language>)` 체크가 하나씩 있습니다. 결과는 코드 스캐닝 경고(저장소의 Security 탭)로 남고 메인테이너가 분류하며, 이 체크는 머지를 막지 않습니다. 제외되는 경로와 그 이유는 워크플로 헤더에 있습니다.

`.github/workflows/scorecard.yml`은 `main`에 대한 푸시, 매주, 그리고 요청 시 OpenSSF Scorecard 검사를 실행합니다. README 배지에 표시되는 점수를 게시하고 결과를 코드 스캐닝에 추가하며, 풀 리퀘스트에서는 실행되지 않고 머지를 막지 않습니다.

`.github/workflows/docs-site.yml`은 `docs/`, `mkdocs.yml`, `llms.txt`, `llms-install.md`, `scripts/docs_site/`, `scripts/codegen/page_summary.py` 또는 워크플로 자체를 건드리는 `main`으로의 풀 리퀘스트와 `main`에 대한 모든 푸시에서 `mkdocs build --strict`로 문서 사이트를 빌드하므로, 깨진 링크나 앵커는 `docs site / build` 체크를 실패시킵니다. 저장소 변수 `DOCS_SITE_ENABLED`가 `true`인 동안에만 `main`에서 사이트를 GitHub Pages에 게시합니다.

다른 브랜치 위에 쌓인 풀 리퀘스트는 `main` 또는 `release/X.Y` 라인으로 대상을 변경하기 전까지 체크가 실행되지 않으며, 머지 충돌이 있는 풀 리퀘스트도 마찬가지입니다. 푸시 후 "no checks reported"가 보인다면 대개 둘 중 하나가 원인입니다.

<a id="related"></a>

## 관련 문서

- [로컬 개발](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md), [코드 생성](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md),
  [마이그레이션](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)
- [벤치마크 방법론](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)
