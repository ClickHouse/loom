<!-- i18n: source=docs/development/release-process.md blob=c00327d2be768494a50aa34f8828ea5bff991faf model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md), which is authoritative.

<a id="release-process"></a>

# 릴리스 절차

Loom은 SemVer를 따릅니다. 서버, 두 SDK, 웹 앱, 루트 스킬(`loom-code`, `loom`), 이를 함께 배포하는 Cursor 플러그인과 Agent Plugins 패키지, 그리고 Gemini CLI 확장은 하나의 버전 `X.Y.Z`를 공유합니다. Claude Code 플러그인과 Codex 플러그인은 별도의 버전을 공유하며, 플러그인이 배포하는 파일(`hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, Codex의 `plugin-defaults.json`) 중 하나라도 동작이 바뀌면 이 버전을 올립니다. 설치된 클라이언트는 새 플러그인 버전을 통해서만 훅 변경을 받습니다. 플러그인의 `lib/server-version.mjs`에 있는 `MIN_SERVER_VERSION`은 플러그인 훅이 지원하는 가장 오래된 서버를 나타냅니다. 서버의 `GET /v1/health`가 더 오래된 `version`을 보고하면 SessionStart는 서버 업그레이드를 요청하는 `loom:` 한 줄을 출력하며, 버전을 읽을 수 없을 때는 아무것도 출력하지 않습니다. 이 값은 릴리스와 함께 올라갑니다([릴리스 진행](#cutting-the-release), 2단계).

<a id="where-the-version-lives"></a>

## 버전이 위치한 곳

`.venv/bin/python scripts/release/prepare.py bump X.Y.Z`는 Claude Code 및 Codex 플러그인을 제외한 아래 항목을 모두 릴리스 커밋에서 올리고, [릴리스 노트](#release-notes)를 작성합니다.

| 구성 요소                     | 파일                                                                                                                                                                                                                                                                                    |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 서버                          | `server/__init__.py` (`__version__`), `pyproject.toml`, `uv.lock`, `server.json`                                                                                                                                                                                                        |
| Python SDK                    | `sdk/python/pyproject.toml` 및 `sdk/python/loom/_version.py`                                                                                                                                                                                                                            |
| TypeScript SDK                | `sdk/typescript/package.json`, `sdk/typescript/package-lock.json`, `sdk/typescript/src/version.ts`                                                                                                                                                                                      |
| 웹 앱                         | `frontend/package.json` 및 `frontend/package-lock.json`                                                                                                                                                                                                                                 |
| 루트 스킬                     | `.claude-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `plugin.json`, 그리고 `skills/loom-code/SKILL.md`와 `skills/loom/SKILL.md`의 `metadata.version`                                                                                                                              |
| Gemini CLI 확장               | `gemini-extension.json`                                                                                                                                                                                                                                                                 |
| Helm 차트                     | `deploy/helm/loom/Chart.yaml`: `appVersion`(서버와 추출기 이미지의 기본 태그이자 차트의 `app.kubernetes.io/version` 레이블)과 차트 자체의 `version`. 메이저 또는 마이너 릴리스에서는 마이너를, 패치 릴리스에서는 패치를 올립니다                                                           |
| Claude Code 및 Codex 플러그인 | `integrations/claude-code-plugin/.claude-plugin/plugin.json`, `integrations/codex-plugin/.codex-plugin/plugin.json` (각자의 버전을 사용합니다. 플러그인이 포함하는 파일(`hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, Codex의 `plugin-defaults.json`)의 동작이 바뀌면 버전을 올립니다) |

서버는 버전을 `server/__init__.py`에서만 읽습니다. OpenAPI 문서, MCP `serverInfo`, `loom report`, `GET /v1/health`(`tests/unit/test_health_problems.py`가 검사)가 이 버전을 보고합니다. 다음 검사들이 버전 번호의 일관성을 유지합니다.

| 검사                                           | 검증 내용                                                                                                                             |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/unit/test_release_version.py`           | 모든 표면이 서버의 버전을 보고하며, `server.json`이 레지스트리 등록 정보의 형식을 유지함                                              |
| `npm run check:version` (TypeScript SDK)       | 네 가지 버전 출처가 서로 일치함                                                                                                       |
| `tests/unit/test_codex_plugin.py`              | 두 플러그인 매니페스트가 같은 버전을 가지며, `MIN_SERVER_VERSION`이 서버 버전보다 높지 않음                                           |
| `tests/unit/test_release_prepare.py`           | `prepare.py`가 올리는 모든 파일이 서버의 버전을 명시함                                                                                |
| `prepare.py check`, `check-tag`, `check-chart` | 릴리스의 `version` 작업([1단계](#what-the-release-workflow-does)): 모든 곳에서 하나의 버전, 후보 순서, 차트가 이동했는지 여부         |

같은 번호로 올리는 두 플러그인 풀 리퀘스트는 충돌 없이 병합되지만, 두 번째 것은 첫 번째로 이미 업데이트한 클라이언트에 아무것도 배포하지 않습니다. 버전을 올리기 전에 `main`의 플러그인 버전을 확인하세요.

<a id="release-notes"></a>

## 릴리스 노트

GitHub Release를 만드는 실행은 이전 최종 릴리스의 태그(`prepare.py previous`) 이후의 커밋으로 노트를 생성하므로, 후보 버전의 커밋에서 만드는 최종 릴리스에도 같은 내용이 들어갑니다. 재실행하거나 이후에 레지스트리 dispatch가 실행되어도 노트는 그대로 유지됩니다. 커밋으로는 알 수 없는 내용은 릴리스 풀 리퀘스트 설명의 "Release notes" 섹션에 적으며, 이렇게 하면 태그를 만들기 전에 검토할 수 있습니다. 이 섹션에는 풀 리퀘스트의 changelog 항목을 changelog 카테고리별로 묶어 넣고(항목이 필요 없는 카테고리의 풀 리퀘스트는 제외), 이어서 운영자 단계(새 설정, 백필, 테이블 재구축)와 클라이언트에 보이는 wire 변경을 적습니다. 제거된 모든 요청 필드와 폐기된 플래그도 포함합니다. 제거된 필드의 레지스트리는 따로 없으므로, 클라이언트는 릴리스 노트를 통해 제거 사실을 알게 됩니다. `prepare.py`는 릴리스보다 낮은 버전 중 가장 높은 최종 태그 이후의 squash 머지로부터 이 섹션을 `release-notes.md`(커밋되지 않음)에 작성합니다.

- 각 풀 리퀘스트의 changelog 항목을 템플릿의 순서에 따라 해당 카테고리 아래에 넣습니다. 항목이 비어 있으면 풀 리퀘스트 제목을 넣습니다. 설명에 카테고리가 하나로 지정되어 있지 않은 항목은 "Unclassified" 아래에 나열되며, 작성자가 알맞은 위치에 옮깁니다.
- 각 "Operator steps" 항목은 모든 풀 리퀘스트에서(항목이 필요 없는 카테고리의 풀 리퀘스트 포함) 작성된 그대로 해당 풀 리퀘스트 번호 아래에 넣습니다. 단계가 없다는 항목(`none`, `N/A`, `No operator steps.`)이나 템플릿 자체의 문구는 제외하며, 단계를 담고 있지 않은 나머지 항목은 작성자가 정리합니다.
- wire diff ([코드 생성](code-generation.md#wire-changelog)).

더 오래된 라인의 패치 릴리스에서 이미 배포된 수정은 다시 나열되며, 같은 범위에서 되돌린 풀 리퀘스트도 그 revert 옆에 함께 나열됩니다. 작성자가 이를 삭제합니다.

이 스크립트는 업스트림 저장소의 태그를 직접 가져오고, `gh`로 풀 리퀘스트를 읽으며, 서버의 레지스트리를 통해 wire diff를 읽습니다. 따라서 로그인된 `gh`와 프로젝트 환경(`uv sync`)이 필요합니다. `.venv/bin/python scripts/release/prepare.py notes X.Y.Z`는 아무것도 올리지 않고 섹션을 다시 출력합니다. 릴리스 풀 리퀘스트를 머지하기 직전에 리베이스한 뒤에 실행하고, 최종 태그를 만들기 전에 번호가 가장 높은 후보 버전의 커밋에서 다시 실행하여, 노트에 태그가 배포하는 모든 풀 리퀘스트가 반영되도록 합니다.

<a id="cutting-the-release"></a>

## 릴리스 만들기

1. 지난 한 주 안에 `main`에서 실행된 `.github/workflows/docker-smoke.yml`의 예약 실행이 통과했는지 확인합니다:

   ```bash
   gh run list -R ClickHouse/loom --workflow docker-smoke.yml --event schedule --branch main -L 1 --json conclusion,createdAt,headSha
   ```

   또는 `gh workflow run docker-smoke.yml -R ClickHouse/loom --ref main`으로 새 실행을 시작하고, `--event schedule` 대신 `--event workflow_dispatch`를 사용해 결과를 확인합니다. 이 워크플로는 릴리스 때와 마찬가지로 두 이미지를 모두 스캔하며, 예약 실행이 실패하면 해당 스케줄을 마지막으로 편집한 사람에게만 알림이 갑니다. 스캔이 실패하면 문제가 된 패키지가 표시됩니다. 이 경우 먼저 `main`에서 해당 패키지를 가져오는 고정 항목(`Dockerfile`의 `ARG` 또는 베이스 이미지 digest, 혹은 lock 파일)을 올립니다. 베이스 이미지 자체의 패키지는 새 베이스 이미지 digest로만 바뀌고, `apt-get install` 레이어가 추가한 패키지는 캐시를 쓰지 않는 빌드라면 언제든 바뀝니다.

2. `main`에서 브랜치를 만들어 `.venv/bin/python scripts/release/prepare.py bump X.Y.Z`를 실행하고, 그 변경 사항과 함께 `release-notes.md`를 풀 리퀘스트 설명의 [릴리스 노트](#release-notes) 섹션으로 넣어 풀 리퀘스트를 엽니다. 항목에 수정이 필요하면 노트를 편집합니다. `make schema-check`와 엄격한 마이그레이션 드라이 런을 포함해 CI가 통과해야 합니다.
   머지된 풀 리퀘스트의 changelog 항목에 다음 서버 릴리스가 필요하다고 되어 있으면, 같은 풀 리퀘스트에서 `integrations/claude-code-plugin/lib/server-version.mjs`의 `MIN_SERVER_VERSION`을 이 버전으로 올리고, 이를 Codex 플러그인에 동기화하며, 두 플러그인의 버전을 모두 올립니다. 클라이언트는 새 플러그인 버전을 통해서만 이 값을 받습니다.
3. 릴리스 풀 리퀘스트가 머지된 뒤 그 머지 커밋에 태그를 달고 태그를 푸시합니다. 릴리스 후보를 먼저 검증하려면 대신 그 커밋에 `vX.Y.Z-rc.1` 태그를 달고, 최종 태그는 나중에 번호가 가장 높은 후보의 커밋에 답니다(아래 참조):

   ```bash
   git tag vX.Y.Z <merge commit>
   git push origin vX.Y.Z
   ```

4. 태그를 푸시하면 `.github/workflows/release.yml`(아래 참조)이 실행되어 서버 및 extractor 이미지를 게시하고, 그런 다음에야 GitHub Release를 만들며, 이후 게시된 설치 프로그램을 외부에서 확인합니다(`release-check`). 5단계 전에 이 실행 결과를 확인합니다.
5. SDK와 MCP Registry 등록을 게시하려면 같은 워크플로를 해당 태그에서 수동으로 실행합니다:

   ```bash
   gh workflow run release.yml --ref vX.Y.Z
   ```

   이 실행의 `pypi` 및 `npm` 작업이 통과하면, 이전 릴리스에서의 업그레이드를 포함해 사용자가 설치하는 방식 그대로 SDK를 확인합니다:
   `gh workflow run release-check.yml -f version=X.Y.Z -f previous_version=<previous X.Y.Z>`.
   `previous_version`은 `version`보다 앞선, 게시된 릴리스여야 하며 `install.sh`와 `loom-stack.tar.gz`를 포함해야 합니다. 그런 릴리스가 없으면 생략합니다.

6. 최종 태그의 커밋에서 만든 릴리스 노트([릴리스 노트](#release-notes))를 GitHub Release의 자동 생성된 노트 위에 붙여 넣습니다.

릴리스 후보는 `vX.Y.Z-rc.N` 태그이며 GitHub 프리릴리스를 만듭니다. 릴리스는 그 밖의 다른 프리릴리스 태그를 거부합니다. 첫 번째 후보는 릴리스 풀 리퀘스트의 머지 커밋에 달며, 이 커밋의 파일은 `X.Y.Z`를 가리키므로 후보의 서버도 이미 `X.Y.Z`를 보고합니다. 수정 후의 다음 후보는 `main`의 이후 커밋에 답니다. 최종 `vX.Y.Z` 태그는 검증을 마친 뒤 번호가 가장 높은 후보의 커밋에 답니다. 최종 번들은 그 커밋에서 게시된 번들이 있는 번호가 가장 높은 후보의 ClickHouse 릴리스를 그대로 유지하며, 이미지는 같은 커밋에서 캐시 없이 다시 빌드되므로 OS 패키지가 검증한 이미지보다 최신일 수 있습니다. 최종 태그 이후에 후보 태그로 실행하면 거부됩니다. 최종 태그와 같은 커밋의 후보 태그로 실행하면 최종 태그를 실행하라는 안내가 나옵니다. 최종 태그의 커밋에서 번호가 가장 높은 후보보다 번호가 낮은 후보, 예를 들어 SDK를 게시하려고 실행한 이전 후보에는 실행할 것이 없으며 그 태그는 그대로 남습니다. 그 밖의 태그는 불필요한 태그로, 삭제하기 전까지 최종 태그의 이후 실행도 실패시킵니다. 차트의 `appVersion`도 `X.Y.Z`를 가리키므로, Kubernetes에서 검증할 때는 `image.tag`와 `extractor.image.tag`를 `X.Y.Z-rc.N`으로 설정합니다.

가장 높은 최종 버전만 저장소의 Latest 릴리스가 되고(`releases/latest`가 설치 프로그램을 제공합니다) `:latest`를 옮깁니다. 프리릴리스나 더 오래된 라인에 대한 패치는 둘 다 그대로 둡니다. 버전은 저장소의 태그에서 읽으므로, 배포하지 않을 최종 태그는 삭제해야 합니다. 그렇지 않으면 Latest가 그 태그보다 낮은 버전에 머뭅니다.

<a id="rehearsing-a-release"></a>

## 릴리스 리허설

드라이 런은 어떤 브랜치에서든 릴리스가 수행할 모든 빌드, 검증, 스모크 테스트를 실행하며, 아무것도 게시하지 않습니다. 이미지 푸시, GitHub Release, 레지스트리 업로드가 모두 일어나지 않습니다.

```bash
gh workflow run release.yml --ref <branch> -f dry_run=true
```

드라이 런은 `server/__init__.py`에 명시된 버전으로 실행되며, 아티팩트를 하루 동안 보관합니다(릴리스는 실행을 다시 수행할 수 있는 기간만큼인 30일 동안 보관합니다). 이미지는 ghcr.io 대신 실행 아티팩트로 전달되고, 레지스트리만 보관할 수 있는 출처(provenance) 및 SBOM 증명(attestation)은 포함되지 않으며, `docker-smoke`가 해당 아티팩트에서 이미지를 불러옵니다. `preflight` 잡은 누락된 [사전 요건](#before-the-first-release)을 실패로 처리하지 않고 각각 경고로 보고합니다. 게시된 릴리스에서만 실행되는 항목은 드라이 런에서 실행되지 않습니다. 해당 항목은 다음과 같습니다. `docker-tag`(각 이미지의 두 다이제스트 결합, Latest 판정, `:latest`), `sign`, GitHub Release와 해당 파일의 서명, 레지스트리 게시, `release-check`, 게시된 이미지 확인(`scripts/release/published_images.sh`)과 이 확인이 선택하는 빌드 생략 경로, 후보 순서 확인(`prepare.py check-tag`, 브랜치의 드라이 런에서는 알림과 함께 건너뜁니다), 그리고 해당 버전이 이미 다른 커밋에 태그되어 있을 때의 차트 확인입니다.

<a id="before-the-first-release"></a>

## 첫 릴리스 전에

릴리스는 공개 패키지를 게시하며, 레지스트리 버전은 다시 사용할 수 없으므로 워크플로는 비공개 저장소에서의 릴리스를 거부합니다. `preflight` 작업은 소유자만 부여할 수 있는 항목을 빌드 전에 몇 초 만에 확인하고, 누락된 각 항목에 대해 해결 방법을 알려 줍니다.

| 사전 요건                                                                        | 확인하는 곳                                          |
| -------------------------------------------------------------------------------- | ---------------------------------------------------- |
| 저장소가 공개 상태임                                                             | `preflight`                                          |
| `ghcr.io/clickhouse/loom`과 `loom-extractor`가 이 저장소에 Write 권한을 부여함   | `preflight` (열었다가 취소하는 업로드 세션)          |
| 설치 시 로그인 없이 가져오므로 두 패키지가 모두 공개 상태임                      | `preflight` (익명 pull 토큰)                         |
| `pypi` 환경과 `release.yml`용 PyPI trusted publisher                             | `pypi` 작업                                          |
| `npm` 환경과 `release.yml`용 npm trusted publisher                               | `npm` 작업                                           |
| `pypi` 및 `npm`과 동일한 보호 규칙을 가진 `mcp-registry` 환경                    | `mcp-registry` 작업                                  |
| 저장소의 아티팩트 보존 기간이 30일 이상임 (Settings > Actions)                   | 없음                                                 |
| `release/**` 브랜치가 `main`과 동일하게 보호됨 (필수 검사, 직접 푸시 금지)       | 없음                                                 |

ghcr.io 패키지는 푸시를 해야만 생성되며, 생성될 때는 비공개 상태입니다. 아직 어떤 릴리스도 푸시하지 않은 이미지의 경우 `preflight`는 실패하는 대신 경고를 표시하며, 해당 실행의 `docker-smoke` 작업은 이미지를 가져오지 못해 실패합니다. 패키지 설정에서 가시성을 공개로 변경한 다음 실패한 작업을 다시 실행하세요.

릴리스가 한 번도 서명되지 않았다면, 다음 태그는 릴리스 후보(`vX.Y.Z-rc.1`)로 만드세요. 해당 실행은 이미지를 서명하고 외부에서 검증하며, 후보 버전은 `:latest`를 옮기지 않으므로 문제가 생겨도 최종 버전이 아닌 후보 번호만 소모됩니다.

<a id="what-the-release-workflow-does"></a>

## 릴리스 워크플로가 하는 일

`.github/workflows/release.yml`은 한 번 빌드하고, 그 파일들을 그대로 검증한 다음, 그 파일들을 게시합니다.

**태그 푸시(`v*`) 시**:

1. `version`은 태그에서 버전을 가져오고(`vX.Y.Z` → `X.Y.Z`), `v*` 태그가 아니거나([드라이 런](#rehearsing-a-release)이 아닌 경우) `X.Y.Z` 또는
   `X.Y.Z-rc.N` 형식이 아닌 ref를 거부합니다. 다음 중 하나라도 거짓이면 실패합니다(`vX.Y.Z-rc.N`의 경우 `X.Y.Z` 기준):
   - `prepare.py check`: `server/__init__.py`가 `__version__ = "X.Y.Z"`로 되어 있고, `server.json`을 포함한 다른 모든
     버전 파일이 그 버전을 가리킵니다.
   - `prepare.py check-tag`: 후보 `vX.Y.Z-rc.N`은 `vX.Y.Z`가 태그되기 전에 있어야 하고, 최종 `vX.Y.Z`는 번호가 가장 높은 후보의 커밋에 있어야 합니다.
   - `prepare.py check-chart`: 차트의 버전이 이전 릴리스의 버전보다 높습니다.
2. `preflight`는 [사전 요구 사항](#before-the-first-release)이 없으면 실행을 실패시킵니다.
3. `clickhouse`는 Compose 번들이 실행하는 ClickHouse 릴리스를 정하고
   (`scripts/release/release_clickhouse.sh`), 그 릴리스의 새 서버를 마이그레이션한 뒤 `build`가 이를 고정하기 전에
   `make schema-check`를 실행하며, ClickHouse 다운그레이드를 거부합니다(8단계 참고).
   따라서 이런 실행은 이미지를 빌드하고 푸시하기 전에 멈춥니다
   ([멈추는 경우](#when-the-clickhouse-job-stops)). 릴리스는 다음 중 처음 해당하는 것입니다.
   - 이 버전의 게시된 번들이 이미 가리키는 릴리스. 따라서 한 버전에 대해 설치 명령을 다시 실행해도 ClickHouse는 바뀌지 않습니다
     (번들이 없는 릴리스는 새로 결정되는데, `github-release`가 번들을 교체하지 않으므로 아무도 그것을 설치했을 수 없기 때문입니다).
   - 디스패치의 `clickhouse_image`. 최종 버전에서는 같은 커밋의 후보들이 게시한 어떤 릴리스보다 우선해서 사용됩니다.
   - 최종 버전의 경우, 같은 커밋에서 번들이 게시된 번호가 가장 높은 후보가 소크(soak)된 릴리스
   - 지금 `clickhouse/clickhouse-server:latest`가 가리키는 릴리스(`dev/clickhouse_release.sh`)
4. `build`는 Python SDK 소스(PEP 440 형식, 후보는 `X.Y.ZrcN`)와 TypeScript SDK 소스에 버전을 기록하고,
   Python SDK wheel과 sdist, npm tarball(TypeScript SDK의 `check:dist` 이후),
   두 플러그인의 zip(`loom-claude-code-plugin-vX.Y.Z.zip`, `loom-codex-plugin-vX.Y.Z.zip`)을 빌드합니다. 또한
   소스가 없는 Compose 번들과 그 SHA-256 파일(해당 ClickHouse 릴리스를 명시), 이 버전을 설치하는 릴리스의 `install.sh`,
   그리고 이 버전과 커밋이 채워진 [에이전트 설정 계약](../getting-started/agent-setup.md)인
   `agent-setup.txt`를 빌드합니다(`scripts/release/agent_setup.py`).
5. `verify`는 바로 그 파일들을 검사합니다. 이름과 내장된 버전, 플러그인
   매니페스트, Claude Code zip에 테스트가 없는지, git이 기록한 실행 비트,
   번들이 `clickhouse` 잡이 확인한 ClickHouse 릴리스를 가리키는지,
   `install.sh`가 이 버전을 설치하고 `agent-setup.txt`가 이를 고정하는지를 확인하며,
   깨끗한 venv에서 wheel을 스모크 테스트합니다(`scripts/gates/sdk_wheel_smoke.py`).
6. `docker`는 `ghcr.io/clickhouse/loom`(태그의 커밋을 `LOOM_BUILD_COMMIT`으로 지정하며,
   `/v1/health`가 `build_commit`으로 보고하는 값입니다)과 `ghcr.io/clickhouse/loom-extractor`
   (문서 추출기 서비스)를 각각 linux/amd64와 linux/arm64용으로, 해당 아키텍처의 전용 러너에서
   빌드 캐시 없이(즉 그날의 패키지 아카이브 그대로) 빌드하고, 출처 증명(provenance)과 SBOM을 붙여 다이제스트로 푸시한 뒤 스캔합니다.
   `docker-tag`는 각 이미지의 두 다이제스트를 태그 아래로 묶으며, 네 개가 모두 통과하기 전에는 어느 이미지에도 태그를 붙이지 않습니다
   (여기서 실패하면 한 이미지만 태그된 채로 남을 수 있고, 다시 실행하면 둘 다 태그됩니다).
   이어서 `sign`은 `docker-tag`가 각 이미지에 대해 만든 인덱스에 다이제스트 기준으로 키 없는(keyless) 서명을 합니다
   (cosign을 사용하며 실행의 GitHub 신원을 쓰므로 인증서에 `release.yml`과 태그가 명시됩니다).
   이미지가 이미 나간 태그에 대한 실행은 아무것도 서명하지 않습니다. 그다음 모든 실행은 사용자가 하는 방식으로
   두 이미지의 서명을 검증하므로, GitHub Release, PyPI, npm, MCP Registry는
   확인되는 서명을 기다립니다([실패한 태그 또는 서명](#a-failed-tag-or-signature)).
7. `docker-smoke`는 해당 이미지로 번들을 설치하고, 번들이 가리키는 ClickHouse 릴리스에서
   amd64와 arm64로 실행하며, 서버 이미지가 태그의 커밋을 가리키는지 확인합니다. 그런 다음 릴리스의 `install.sh`와 번들을
   한 줄 명령으로 깨끗한 Debian, Ubuntu, Alpine 컨테이너에서 실행합니다
   (설치, 왕복 테스트, 재실행, 각 컨테이너 고유의 `sh`와 `awk`에서 거부되는 다운그레이드,
   그리고 이전 최종 릴리스의 설치 프로그램에서의 업그레이드. 해당 버전의 릴리스 이후 커밋에 대한 드라이 런에서는
   그 릴리스 자체의 설치 프로그램에서의 업그레이드입니다. `scripts/gates/installer_rehearsal.py`).
8. `github-release`는 두 이미지가 존재하고 검증된 뒤 GitHub Release를 한 번 만들며, 노트는
   이전 최종 릴리스 이후의 커밋에서 생성됩니다(`prepare.py previous`, `version`의 출력이며, 후보의 노트도 여기서 시작합니다).
   다시 실행하면 노트는 그대로 두고 릴리스를 갱신하며, 검증된 파일을 첨부합니다. 릴리스에 이미 있는 번들은 그대로 유지되고
   체크섬도 마찬가지입니다(릴리스에서 사라진 체크섬은 번들로부터 다시 작성됩니다). 에셋을 교체하면 먼저 삭제되는데,
   업로드가 실패하면 설치된 버전에서 번들이 사라지기 때문입니다. 새 번들은 가장 최근 릴리스 30개(프리릴리스 포함)의 번들보다
   오래된 ClickHouse 릴리스를 가리킬 수 없습니다. 그 버전으로 업그레이드하는 설치가 더 새로운 서버가 쓴 데이터 위에서
   더 오래된 서버를 시작하게 되기 때문입니다. 대신 잡이 실패하며, 디스패치에 `-f allow_clickhouse_downgrade=true`를 추가하면 예외입니다.
   이 잡은 한 번에 하나의 태그에 대해서만 실행되므로, 함께 릴리스되는 두 태그가 같은 오래된 번들에 대한 검사를 둘 다
   통과할 수는 없습니다. 대기하는 잡은 하나뿐입니다. 세 번째 태그의 게시가 대기 중인 잡을 취소하며, 취소된 잡은 직접 다시 실행해야 합니다
   (Re-run failed jobs). 여기서의 거부는 `docker-tag`가 해당 태그의 이미지 태그(그리고 가장 높은 최종 버전이면 `:latest`)를
   푸시한 뒤에 발생합니다. 다시 실행하면 실행의 입력이 유지되어 다시 실패하므로, 대신 태그를 디스패치하세요.
   그러면 SDK와 MCP Registry 목록도 게시됩니다. 오류가 가리키는 더 새로운 릴리스가 해당 태그의 `schemas/schema.sql`을 덤프할 수 있다면
   그 릴리스로 디스패치하거나:
   `gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`;
   다운그레이드를 받아들이고 디스패치합니다:
   `gh workflow run release.yml --ref vX.Y.Z -f allow_clickhouse_downgrade=true`.
   둘 다 택하지 않을 때는 `:latest`(옮겨졌다면)와, 최종 버전이면 `:X.Y`를 두 이미지 모두에서 이전 릴리스로 되돌리세요.
   [실패한 태그 또는 서명](#a-failed-tag-or-signature)과 같은 방식입니다.
9. `sign-files`는 릴리스가 지금 제공하는 모든 파일을 사용자가 하는 방식으로 내려받고
   (`scripts/release/download_release_files.sh`), 이번 실행의 빌드와 바이트가 동일한 파일마다 이미지와 같은 키 없는 서명으로
   Sigstore 번들 `<file>.sigstore.json`을 만들어 서명합니다(`scripts/release/sign_release_files.sh`).
   이전 실행에서 릴리스가 유지한 스택 번들과 체크섬은, 그 Sigstore 번들이 검증되면 그대로 유지됩니다.
   이번 실행의 빌드가 아닌 다른 파일이 있으면 잡이 아무것도 서명하지 않고 실패합니다
   ([실패한 태그 또는 서명](#a-failed-tag-or-signature)). 그런 다음 사용자가 하는 방식으로 각 파일을 검증합니다
   (`scripts/release/verify_release_files.sh`). 이 잡은 실행의 OIDC 토큰을 읽기 전용 contents 권한으로 자체 잡에서만 보유하며,
   `attach-signatures`가 만들어진 Sigstore 번들을 업로드합니다(유지된 번들은 그대로 둡니다. 에셋을 교체하면 먼저 삭제되기 때문입니다).
   디스패치는 파일을 다시 빌드하고 `github-release`가 그것들을 교체하므로, 디스패치 자체의 `sign-files`와
   `attach-signatures`가 끝나기 전까지는 태그 실행의 Sigstore 번들이 교체된 파일을 검증하지 못합니다.
10. `release-check`(태그 푸시 시)는 방금 게시된 것에 대해 `.github/workflows/release-check.yml`을 실행하며, 아무 계정에도 로그인하지 않습니다.
    github.com의 한 줄 명령을 같은 깨끗한 컨테이너에서 실행하고, 두 이미지를 익명으로 풀하며, 두 이미지와
    모든 릴리스 파일의 서명을 사용자처럼 검증합니다. 서명은 해당 태그의 `release.yml`에서 나온 것이어야 합니다
    ([컨테이너 이미지](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#container-images),
    [릴리스 파일](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#release-files)). `.sigstore.json`이 없는 파일은 실패합니다.
    릴리스 워크플로가 이미지나 파일에 서명하기 전에 게시된 릴리스에는 그 서명이 없으므로, 그 릴리스를 검사하면 `signatures` 잡이 실패합니다.

서버 이미지와 추출기 이미지는 GitHub Release보다 먼저 게시되므로, 릴리스 페이지에 표시되는
설치 프로그램이 없는 이미지를 가리키는 일은 없습니다.

**수동 디스패치 시**(태그 ref에 대한 `workflow_dispatch`), `release-check`를 제외한 같은 잡이 실행되며, 이미지가 이미 게시되어 있으면(아래 참고) 이미지를 빌드하지 않습니다. `build`는 TypeScript SDK의 `npm run check`(린트, 타입, 버전, README 예제, 테스트)도
실행합니다. 그다음 세 개의 게시 잡이 실행됩니다.

| 잡             | 게시 대상                                                        | 방식                                                                                                                                                                                                                                  |
| -------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pypi`         | `clickhouse-loom`(Python SDK만)을 PyPI에                         | 검증된 wheel과 sdist. trusted publishing(OIDC). PyPI에 이미 있는 파일은 건너뜀                                                                                                                                                        |
| `npm`          | `@clickhouse/loom`을 provenance와 함께 npm에                     | 검증된 tarball. 게시된 버전은 건너뜀. `latest`는 가장 높은 최종 버전에만, 프리릴리스는 `next`, 이전 라인의 패치는 `release-X.Y`. trusted publishing(OIDC)                                                                             |
| `mcp-registry` | `server.json`을 `io.github.ClickHouse/loom`으로 MCP Registry에   | 커밋된 파일이며, 그 버전은 `version` 잡이 태그와 대조해 검사. 고정된 `mcp-publisher`로 GitHub OIDC 로그인. 이미 등록된 버전과 프리릴리스는 건너뜀. `preflight`는 빌드 전에 비공개 저장소를 거부함                                      |

이미지는 이미 태그 실행에서 게시되어 있습니다. 이미지가 해당 버전을 가진 태그에 대한 디스패치는
이미지를 빌드하지도 태그하지도 않고(`scripts/release/published_images.sh`), 게시된 이미지를 스모크 테스트한 뒤
레지스트리로 넘어갑니다. 릴리스 이미지는 캐시 없이 빌드되므로, 다시 빌드하면 그 버전의 태그가 다른 바이트로 옮겨 갈 것입니다.

게시된 `X.Y.Z` 태그는 절대 옮기지 마세요. 게시된 이미지가 실행과 다른 커밋에서 빌드되었으면 `published_images.sh`가 실패하며,
수정하려면 새 버전이 필요합니다.

일부만 게시된 뒤의 재실행은 안전합니다. 각 게시 잡은 이미 존재하는 것을 건너뜁니다. 유일한 예외는
파일 서명이 실패한 태그 실행이며, 이는 [실패한 태그 또는
서명](#a-failed-tag-or-signature)에서 복구합니다.

**검증된 파일을 게시하는 이유.** 자체 아티팩트를 다시 빌드하는 잡은 검사되지 않은 바이트를 내보낼 수 있습니다. `verify`를 통과한 것을 게시하면 GitHub Release와 레지스트리가 같은 바이트를 담게 됩니다.

**서버가 PyPI에 없는 이유.** 서버는 라이브러리로 import되는 것이 아니라 ClickHouse에 대해 배포되며, 이미지로 제공됩니다. 루트 `pyproject.toml`에는 `Private :: Do Not Upload`가 표시되어 있고, PyPI에는 SDK만 있습니다. `pypi`와 `npm` 잡에는 이 저장소(그리고 그 GitHub 환경)에 등록된 trusted publisher가 필요합니다.

**SDK 게시가 수동인 이유.** 레지스트리 게시는 되돌릴 수 없습니다. PyPI는
같은 버전을 두 번 받지 않습니다. SDK를 디스패치할 때만 게시하면 릴리스 파일을 먼저 확인할 수 있습니다.
서버 이미지는 GitHub Release의 설치 프로그램의 전제 조건이므로 자동이며,
서명된 릴리스 태그를 만드는 것이 그 검토 관문입니다.

<a id="a-failed-tag-or-signature"></a>

### 실패한 태그 또는 서명

`docker-tag`가 실패하거나(한 이미지만 태그되고 다른 이미지는 안 됨) `sign`이 실패한 경우(예: Sigstore 장애):

- 이미지를 태그한 실행(태그 푸시, 또는 이미지를 빌드하고 태그한 디스패치)에서 "Re-run failed jobs"를 사용하세요. 그 실행 후 30일 이내여야 하며,
  실행의 아티팩트도 그만큼 보관됩니다(저장소의 아티팩트 보존 한도가 더 짧으면 그에 따릅니다). 그 시도는 `preflight` 결과와
  `docker-tag`의 다이제스트를 유지하므로, 같은 빌드에 태그를 붙이고 태그한 것에 서명합니다.
- 태그를 붙인 실행이 서명을 마친 뒤에는, 그 태그에서 실패한 디스패치나 두 번째 푸시는 자체의
  "Re-run failed jobs"나 새 디스패치로 복구됩니다.
- 다른 실행이 이미 태그를 붙인 뒤에는, 태그 전에 실패한 실행을 절대 다시 실행하지 마세요. 그 실행의 `preflight`는
  이미지를 보지 못했으므로 다시 빌드하여 게시된 태그를 옮기게 됩니다.
- "Re-run all jobs"가 태그를 붙인 실행의 시도를 대체했거나 30일이 지나면, 어떤
  실행도 그 빌드에 태그를 붙이거나 서명할 수 없습니다. 반쯤 태그된 버전에 대한 실행은 `preflight`에서 실패하고,
  게시된 태그에 대한 실행은 서명을 검증할 뿐 만들 수는 없습니다. 새 버전을 릴리스하세요.

그 버전이 나오기 전까지는 빌드의 이동 태그가 여전히 그 빌드를 가리킵니다. `:latest`(옮겨졌다면)와,
최종 버전이면 `:X.Y`를, 각각을 가지고 있던 이전 릴리스로 두 이미지 모두에서 되돌리세요. `X.Y.0`에는
`:X.Y`를 가졌던 이전 릴리스가 없으므로, `X.Y.1`이 나올 때까지 이 빌드를 가리킵니다. `<previous>`가 그 릴리스의 버전일 때,
그 릴리스의 서명이 검증된 경우에만 태그가 옮겨집니다.

```bash
digest=$(docker buildx imagetools inspect ghcr.io/clickhouse/<image>:<previous> \
  --format '{{json .Manifest}}' | jq -re .digest)
cosign verify "ghcr.io/clickhouse/<image>@$digest" \
  --certificate-identity https://github.com/ClickHouse/loom/.github/workflows/release.yml@refs/tags/v<previous> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null \
  && docker buildx imagetools create -t ghcr.io/clickhouse/<image>:<tag> "ghcr.io/clickhouse/<image>@$digest"
```

릴리스 워크플로가 서명하기 전에 게시되어 서명이 없는 `<previous>`의 경우, `digest=` 줄을 실행한 다음
`cosign verify … &&` 없이 `docker buildx imagetools create` 명령을 실행하세요. 어느 쪽이든 태그가 그 다이제스트를 가리키는지 확인하세요.

```bash
[ -n "$digest" ] && [ "$(docker buildx imagetools inspect ghcr.io/clickhouse/<image>:<tag> --format '{{json .Manifest}}' | jq -re .digest)" = "$digest" ] \
  || { echo "<tag> does not name $digest" >&2; false; }
```

`sign-files`나 `attach-signatures`가 실패하면(예: Sigstore나 GitHub API 장애) 릴리스에 그 파일을 검증하는 Sigstore 번들이 없는 파일이 남습니다.
릴리스의 스택 번들과 체크섬이 검증되는 Sigstore 번들을 가지고 있는 동안에는 이후 어떤 실행이든 다시 서명합니다. 실패한 실행에서
"Re-run failed jobs"를 하세요. 그 번들이 없다면, 즉 태그 실행 자체의 서명이 실패했다면, 아티팩트가 보관되는 동안(30일,
저장소의 아티팩트 보존 한도가 더 짧으면 그에 따름) 그리고 어떤 디스패치보다 먼저 태그 실행의 "Re-run failed
jobs"만이 서명할 수 있습니다. 디스패치, "Re-run all
jobs", 또는 태그의 두 번째 푸시는 스택 번들과 체크섬을 다시 빌드하지만 릴리스가 가진 것은 유지하며, 그러면 어떤 실행도 그것을 서명할 수 없습니다. 그 이후에는 새 버전을 릴리스하세요.

목록 조회나 다운로드에서 실패한 `release-check`는 "Re-run failed jobs"를 하세요. 최신 릴리스 실행의 `sign-files`와
`attach-signatures`가 통과한 뒤에도 검증되지 않는 Sigstore 번들을 발견했다면, 릴리스가 서명된 뒤에 변경되었다는 뜻이며
이는 보안 사고입니다
([SECURITY.md](https://github.com/ClickHouse/loom/blob/main/SECURITY.md)). 서명이 실패한 실행은 위에서 설명한 대로 복구하세요.

`sign-files`는 이번 실행의 빌드가 아닌 파일을 모두 거부합니다. 다만 Sigstore 번들이 검증되는, 유지된 스택 번들이나 체크섬은 예외입니다. 릴리스에 수동으로 첨부한 파일은 워크플로가 만들지 않는 것이라면 릴리스에서 삭제한 뒤 "Re-run failed jobs"를 실행합니다. Sigstore 번들이 없는, 유지된 스택 번들이나 체크섬은 서명된 적이 없는 것입니다(위 참조). 그 밖의 거부와, Sigstore 장애 상황이 아닌데 유지된 파일의 Sigstore 번들이 검증되지 않는 경우는 파일이 워크플로 밖에서 변경되었다는 뜻이며, 서명 실패가 아니라 보안 사고입니다.

<a id="when-the-clickhouse-job-stops"></a>

### clickhouse 잡이 중단되었을 때

`clickhouse`에서 중단된 실행은 아무것도 게시하지 않았습니다.

- **`latest`가 잘못된 릴리스를 가리키는 경우**: 정확한 버전을 지정해 태그를 디스패치합니다.
  `gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`.
  이미 게시된 번들은 자체 버전을 유지하며, 실행은 입력을 무시했다는 경고를 남깁니다.
- **해당 릴리스가 태그의 `schemas/schema.sql`을 덤프하지 않는 경우**: 그 릴리스에서 스키마를 다시 생성하고
  ([스키마 덤프](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md#schema-dump-and-check) 명령에
  `LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D`를 지정) 커밋합니다. 후보(candidate)라면 수정 커밋에 다음 `-rc.N`을 태그합니다. 최종 릴리스(번들을 게시한 후보가 해당 커밋에 없는 경우이며, 그런 경우에만 `latest`를 확인합니다)라면 태그를 삭제하고
  (`git push origin :refs/tags/vX.Y.Z`, 그리고 그 태그를 가져온 모든 클론에서 `git tag -d vX.Y.Z`. fetch는 태그를 제거하지 않기 때문입니다), 수정 커밋에 새 후보를 태그합니다. 어느 쪽이든 같은
  `-f clickhouse_image=...`로 새 후보를 디스패치합니다. 후보의 태그 푸시가 `latest`를 다시 확인하기 때문입니다. 소크(soak)를 거친 뒤 그 후보에 최종 태그를 붙이면 해당 릴리스가 유지됩니다.
- **소크 중 다운그레이드가 발생한 경우**: 후보의 릴리스를 그대로 가져가는 최종 릴리스도 검사하므로, 소크 중에 더 새로운 릴리스와 함께 이전 라인의 패치가 게시되면 실행이 중단됩니다. 해결 방법은 두 가지입니다.
  - 위와 같이 최종 릴리스의 태그를 삭제하고(아무것도 게시되지 않았습니다), 그 커밋에 새 후보를 태그한 뒤 소크를 거쳐 다시 최종 태그를 붙입니다. 후보의 태그 푸시가 `latest`를 확인하며, 그 값이 오류에 나온 릴리스보다 오래된 경우에는 그 릴리스를 지정해 후보를 디스패치합니다.
    `gh workflow run release.yml --ref vX.Y.Z-rc.N -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`
  - 또는 `-f allow_clickhouse_downgrade=true`로 최종 릴리스를 디스패치하고, 해당 패치를 설치하는 환경에서 다운그레이드를 감수합니다.

위의 디스패치는 모두 `pypi`와 `npm` 잡도 함께 실행하므로, 디스패치한 후보는 해당 환경이 승인되는 대로 SDK를 게시합니다. 이미지와 GitHub Release만 게시하려면 그 승인을 거부합니다.

<a id="installing-from-a-github-release"></a>

## GitHub Release에서 설치하기

레지스트리에 게시되지 않은 경우 릴리스 에셋에서 설치합니다.

```bash
gh release download vX.Y.Z --repo ClickHouse/loom --pattern 'clickhouse_loom-*.whl'
pip install ./clickhouse_loom-*.whl

gh release download vX.Y.Z --repo ClickHouse/loom --pattern 'clickhouse-loom-*.tgz'
npm install ./clickhouse-loom-*.tgz
```

코딩 에이전트 플러그인은 저장소의 마켓플레이스에서 설치합니다(`claude plugin marketplace add ClickHouse/loom`, `codex plugin marketplace add ClickHouse/loom`). zip 파일은 저장소에 접근할 수 없는 머신을 위한 것입니다.

<a id="listings"></a>

## 목록

에이전트와 사람은 다음 목록을 통해 Loom을 찾습니다. 각 목록은 이 저장소의 파일을 읽습니다.

| 목록                                           | 읽는 파일                                                                                                                                                                            | 업데이트 방식                                                                                                                                                                                                                                |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MCP Registry 및 이를 복사하는 디렉터리         | `server.json`: 메타데이터와 `https://{loom_host}/mcp`의 원격 서버, 그리고 선택 사항인 비밀 `Authorization` 헤더                                                                       | 디스패치된 각 최종 릴리스의 `mcp-registry` 작업                                                                                                                                                                                              |
| `clickhouse-loom`의 PyPI 페이지                | `sdk/python/pyproject.toml`의 `[project]` 메타데이터, 그리고 페이지 본문으로 쓰이는 `sdk/python/README.md`                                                                            | 디스패치된 각 릴리스의 `pypi` 작업. 페이지에는 가장 높은 최종 버전이 표시되며, 첫 최종 버전이 나오기 전에는 가장 높은 후보 버전이 표시됩니다                                                                                                   |
| `@clickhouse/loom`의 npm 페이지                | `sdk/typescript/package.json`의 메타데이터, 그리고 페이지 본문으로 쓰이는 `sdk/typescript/README.md`                                                                                  | 디스패치된 각 릴리스의 `npm` 작업. 페이지에는 `latest`로 태그된 버전이 표시되며, 이 작업은 가장 높은 최종 버전에만 이 태그를 설정합니다                                                                                                        |
| Claude 플러그인 디렉터리                       | `integrations/claude-code-plugin/.claude-plugin/plugin.json`(`displayName`, `description`, `keywords`, `icon`, `documentationUrl`, `supportUrl`, `privacyPolicyUrl`)과 README         | 개발자 포털에서 한 번 제출하며, 이후 버전은 추적 중인 브랜치에서 가져옵니다. 아이콘은 최초 제출 때만 읽습니다                                                                                                                                 |
| Claude Code 플러그인 추천                      | `.claude-plugin/marketplace.json`의 `loom` 항목에 있는 `relevance` 블록: `loom` 명령, Loom의 설정 파일, 매니페스트의 SDK 의존성                                                       | 기본 브랜치의 마켓플레이스에서 읽으며, 관리자가 마켓플레이스를 허용 목록에 추가한 곳에서만 표시됩니다(`pluginSuggestionMarketplaces`)                                                                                                         |
| Cursor 마켓플레이스                            | `.cursor-plugin/plugin.json`(`displayName`, `description`, `keywords`, `logo`, `variables`), `.cursor-plugin/marketplace.json`                                                       | 제출하지 않음. cursor.com/marketplace/publish에서의 제출은 수동으로 검토되며, 각 업데이트도 마찬가지입니다                                                                                                                                    |
| Awesome Copilot 마켓플레이스                   | 루트의 `plugin.json`과 `mcp.json`(Agent Plugins 패키지). 제출에 명시한 릴리스 태그와 커밋 기준                                                                                       | 제출하지 않음. 제출은 `github/awesome-copilot`의 외부 플러그인 이슈 양식을 통해 이루어지며, 이 양식이 패키지를 확인하고 Copilot CLI로 설치합니다. 새 버전은 해당 저장소의 `plugins/external.json`에 대한 풀 리퀘스트입니다                      |
| Codex 플러그인 디렉터리                        | `integrations/codex-plugin/.codex-plugin/plugin.json`(`interface`)                                                                                                                   | 플러그인 포털에서 제출합니다. 수명 주기 훅이 있는 플러그인은 여기에 등록할 수 없으므로, 공개 목록에는 훅을 제외한 빌드가 필요합니다                                                                                                           |
| 코딩 에이전트용 문서 색인                      | `context7.json`                                                                                                                                                                      | 저장소를 한 번 추가하면 기본 브랜치에서 갱신됩니다                                                                                                                                                                                           |
| Agent Skills 색인                              | `skills/loom-code`, `skills/loom`, 그리고 Claude Code 플러그인의 스킬. 기여자용 스킬은 프론트매터에 내부용으로 표시되어 있어 제공되지 않습니다                                         | `npx skills add ClickHouse/loom` 설치 수로 순위가 정해지며, 제출할 것은 없습니다                                                                                                                                                             |

`mcp-registry` 작업은 `mcp-registry` 환경에서 실행됩니다. 첫 디스패치 전에 `pypi` 및 `npm`과 같은 보호 규칙을 지정하세요. GitHub는 없는 환경을 보호 규칙 없이 생성합니다. 이 작업의 로그인에는 비밀 값이 필요하지 않습니다.

Claude 매니페스트나 마켓플레이스 항목을 변경한 후 푸시하기 전에, 플러그인에는 `claude plugin validate integrations/claude-code-plugin --profile directory --strict`를, 마켓플레이스에는 `claude plugin validate .`를 실행하세요. 이 명령은 디렉터리의 구조 검사를 수행하며, 플러그인 검사는 경고 없이 통과해야 합니다.

<a id="after-the-release"></a>

## 릴리스 이후

- 배포합니다([업그레이드](../operations/upgrades.md)). 릴리스 노트의 운영자 단계가 배포 체크리스트입니다.
- 클라이언트 플러그인은 서버 배포가 아니라 각 클라이언트의 플러그인 관리자를 통해 업데이트됩니다.

<a id="related"></a>

## 관련 문서

- [알 수 없는 필드와 제거된 필드](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields): 릴리스에서 제거된 필드를 클라이언트가 어떻게 보게 되는지
- [기여하기](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md)
