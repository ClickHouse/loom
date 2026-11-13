<!-- i18n: source=docs/getting-started/quickstart.md blob=4175c372ea5183fffd31de1d59658aae09018c47 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# 빠른 시작

이 페이지는 노트북에서 Loom을 부팅하고 HTTP, Python, MCP로 첫 메모리를 저장하는 방법을 설명합니다. API 키와 계정이 필요하지 않습니다. 릴리스된 스택을 새로 설치하면 이미지에 포함된 작은 로컬 모델로 임베딩하므로 네트워크 없이도 검색이 의미 있는 결과를 반환합니다(업그레이드한 설치는 `.env`에 지정된 임베더를 그대로 유지합니다). LLM은 가짜이므로 추출과 `memory.ask` 응답은 아무 결과도 만들지 않습니다. 체크아웃한 저장소의 `.env`는 둘 다 가짜 프로바이더를 사용합니다.

**기본 Docker 설치는 단일 사용자용입니다.** 로그인이 없고, localhost에서만 접근할 수 있으며, 기본 `dev/local` 네임스페이스를 사용합니다. 멀티 유저 모드를 켜지 않아도 한 사람이 여러 에이전트를 연결할 수 있습니다. **팀 배포도 OSS에서 지원되며**, 인증된 계정과 네임스페이스 접근 제어를 제공합니다. [멀티 유저 모드로 시작](#start-in-multi-user-mode)하거나 [배포 → 아이덴티티 모드](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)를 따르세요. 두 모드 모두 동일한 Loom 기능을 지원하며, 실제 LLM과 임베딩을 사용하려면 어느 모드에서든 프로바이더 설정이 필요합니다.

<a id="prerequisites"></a>

## 사전 요구 사항

| 필요 항목                      | 용도                                                                     |
| ------------------------------ | ------------------------------------------------------------------------ |
| compose 플러그인이 있는 Docker | 스택: ClickHouse와 Loom 서버.                                            |
| Python 3.12 이상               | 기여자 전용 `loom` CLI. SDK는 3.11+를 지원합니다.                        |
| Node.js 20 이상                | TypeScript SDK (선택 사항).                                              |
| 사용 가능한 포트 `7777`        | Loom. 체크아웃 개발 환경에서는 ClickHouse도 `8123`과 `9000`으로 노출됩니다. |
| `jq`                           | 4단계에서 `curl` 출력을 읽을 때 사용 (선택 사항).                        |

<a id="1-boot-the-stack"></a>

## 1. 스택 부팅

<a id="fast-path-released-image-no-clone"></a>

### 빠른 방법: 릴리스된 이미지, 클론 없음

사전 요구 사항: Docker Desktop 또는 Compose 2.20 이상이 설치된 Docker Engine. Windows에서는 `docker`가 동작하는 WSL 2 터미널에서 명령을 실행하세요. 해당 배포판에 WSL 통합을 켠 Docker Desktop이나, 그 안에 설치한 Docker Engine이면 됩니다. 설치 프로그램은 POSIX 셸 스크립트입니다.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

이 명령은 설치 프로그램을 현재 디렉터리에 저장한 다음 그 파일을 실행합니다. 따라서 홈 디렉터리나 본인만 쓸 수 있는 다른 디렉터리에서 실행하세요. 공유 디렉터리에서는 다른 사람이 실행 전에 `loom-install.sh`를 바꿔치기하거나 다른 파일을 가리키게 할 수 있습니다. 다운로드가 실패하면 curl의 오류와 0이 아닌 종료 코드로 중단됩니다. 설치가 실패한 뒤에는 보고된 문제를 해결하고 전체 명령을 다시 실행하세요. 설치 프로그램은 최신 GitHub Release에서 `loom-stack.tar.gz`를 내려받아 SHA-256을 검증하고, Compose와 ClickHouse 설정을 `~/.loom` 아래에 설치합니다. 그런 다음 버전이 일치하는 `ghcr.io/clickhouse/loom:X.Y.Z`와 `ghcr.io/clickhouse/loom-extractor:X.Y.Z`(문서 추출기 서비스) 이미지를 가져오고, `http://localhost:7777/v1/health`가 응답할 때까지 기다립니다. 서버 이미지에는 API, 워커, 마이그레이션, 서버 루트에서 제공되는 빌드된 React 대시보드가 들어 있어 Python이나 Node를 따로 설치할 필요가 없습니다. Python, TypeScript, Go 컴파일러 인덱서가 포함되어 활성화되어 있으며, `amd64`에서는 C++ 인덱서(`scip-clang`)도 마찬가지입니다. TypeScript 인덱서로 인덱싱한 저장소는 의존성이 자동으로 설치됩니다. 두 이미지 모두 Linux `amd64`와 `arm64`를 지원하며, Apple Silicon의 Docker Desktop도 포함됩니다.

Loom이 응답하면 설치 프로그램은 다음을 수행합니다.

1. **메모리를 확인합니다.** 새로 설치하면 테스트 메모리를 저장하고, 다시 읽은 뒤 삭제합니다. 이로써 쓰기와 읽기가 동작함을 보여 주며 살아 있는 메모리는 남지 않습니다(삭제된 버전은 Recently deleted에 표시됩니다). 이 확인이 통과한 뒤의 재실행이나 업그레이드에서는 건너뜁니다.
2. **로그인이 켜져 있는지 확인합니다.** 로그인이 켜져 있으면 에이전트를 연결하지 않고 토큰을 어디서 받는지 안내합니다.
3. **코딩 에이전트 연결을 제안합니다.** 터미널에서는 발견한 에이전트마다 묻습니다. Claude Code와 Codex는 플러그인으로, VS Code는 `code --add-mcp`로 연결합니다. `LOOM_CONNECT`는 질문을 대신하며 터미널이 없는 실행에서도 동작하고, 이름을 지정한 클라이언트만 확인합니다. 값은 `claude-code`, `codex`, `vscode`의 쉼표 목록, `all`, 또는 단독으로 쓴 `none`이며, 그 밖의 값이면 설치가 중단됩니다. 터미널도 `LOOM_CONNECT`도 없으면 어떤 에이전트도 확인하거나 변경하지 않습니다. 이전에 설정한 플러그인이나 MCP 서버(플러그인, `claude mcp add`, `codex mcp add`, `loom install`로 설정한 것)는 그대로 둡니다. Claude Code 플러그인의 경우 설치 프로그램이 이 설치를 가리키게 하는 명령을 출력합니다. Claude Code의 플러그인은 PATH에 있는 Node.js 18 이상을 실행하므로, 없으면 설치 프로그램은 플러그인을 설치하지 않고 MCP 서버만을 위한 `claude mcp add` 줄을 출력합니다. Codex의 플러그인은 nvm 같은 곳에서도 Node.js를 찾으므로 설치되며, `codex mcp add` 줄이 함께 출력됩니다. Codex의 플러그인은 항상 `http://localhost:7777`을 사용하므로, 다른 곳에 설치한 경우에는 대신 설정 페이지 링크를 안내합니다. VS Code는 원격 세션이나 WSL에서는 제안하지 않습니다. `LOOM_CONNECT`가 지정했지만 PATH에 없는 클라이언트는 이름을 알려 줍니다.
4. **대시보드를 엽니다.** 새로 설치한 데스크톱 환경에서 열며, `LOOM_NO_OPEN=1`이면 건너뜁니다.

WSL에서 실행하면 이 단계들은 WSL 안에서 동작합니다. Windows에서 실행되는 에이전트는 Windows에서 [연결 가이드](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)로 연결하고, 설치 프로그램이 출력하는 대시보드 주소는 Windows 브라우저에서 여세요.

Ctrl-C는 어느 단계에서든 실행을 멈춥니다. 에이전트 CLI가 플러그인용으로 시작한 클론은 백그라운드에서 끝까지 진행될 수 있습니다.

스택은 자체 ClickHouse 컨테이너를 실행합니다. 이미 운영 중인 ClickHouse(자체 관리 서버 또는 Keeper가 있는 26.4 이상의 ClickHouse Cloud 서비스)를 쓰려면 그 URL을 전달하세요. URL에 비밀번호가 없으면 설치 프로그램이 비밀번호를 묻고, `loom` 데이터베이스와 읽기 전용 reader 사용자를 만들거나 관리자가 실행해야 할 SQL을 출력합니다([ClickHouse 선택](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)).

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

각 릴리스의 `install.sh`는 해당 릴리스를 설치합니다. 따라서 `latest`가 제공하는 사본은 최신 릴리스를 설치하고, 아래의 릴리스 전용 URL은 항상 그 릴리스를 설치합니다. 더 새로운 설치 위에 이전 릴리스의 사본을 실행하면, 더 새로운 데이터 위에서 이전 Loom과 ClickHouse를 시작하지 않도록 설치를 거부합니다. 의도적으로 다운그레이드하려면 [설치 프로그램 설치 다운그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install)를 따르세요. 릴리스를 고정하려면 해당 릴리스의 URL을 사용하세요.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

시작하기 전에 모든 것을 직접 확인하려면 파일을 내려받아 검사하세요.

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

출력된 내용이 올바르다고 판단되면 검증한 번들로 설치하세요.

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

설치 프로그램에 전달한 `LOOM_PORT`, `LOOM_BIND_HOST`, `LOOM_IMAGE_REPOSITORY`, `LOOM_EXTRACTOR_IMAGE_REPOSITORY`, `LOOM_CLICKHOUSE_IMAGE`는 설치 디렉터리의 `.env`에 기록됩니다. 그래서 이후 그 디렉터리에서 실행하는 `docker compose` 명령과 설치 프로그램 재실행이 같은 포트와 이미지를 사용합니다. 루프백이 아닌 `LOOM_BIND_HOST`는 Loom을 이 호스트 밖으로 공개하며, 이 경우 서버는 `LOOM_PUBLIC_BASE_URL`과 로그인 없이는 시작을 거부합니다. `LOOM_SKIP_START=1`로 설치하고, `.env`에 이 값들을 설정한 뒤(아래 참고), `LOOM_SKIP_START=1` 없이 설치 명령을 다시 실행하세요.

각 릴리스는 빌드 시점에 최신이던 ClickHouse 릴리스를 실행합니다. 같은 Loom 버전에 대해 설치 명령을 다시 실행하면 그 ClickHouse가 유지되고, 더 새로운 Loom 버전을 설치하면 ClickHouse도 옮겨 갑니다. 단 `.env`가 `LOOM_CLICKHOUSE_IMAGE`를 고정한 경우는 예외입니다(릴리스를 다시 따르려면 그 줄을 삭제하세요). 설치 명령을 다시 실행하면 관리되는 Compose/설정 파일을 업데이트하고 컨테이너를 재조정합니다. `~/.loom/.env`와 이름이 지정된 Docker 볼륨은 보존하며, 추출기의 URL과 토큰이 없는 파일에는 이를 추가합니다. 데이터를 삭제하지 않고 중지하려면 다음을 실행하세요.

```bash
docker compose --project-directory "$HOME/.loom" down
```

`--volumes`는 되돌릴 수 없는 초기화를 의도할 때만 추가하세요.

<a id="try-it-in-github-codespaces"></a>

### GitHub Codespaces에서 체험하기

[![GitHub Codespaces에서 열기](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

저장소의 dev container는 내 컴퓨터에 아무것도 설치하지 않고 codespace에서 최신 릴리스를 실행합니다. 위의 설치 프로그램으로 Loom을 설치하고, 시작하고, Claude Code를 연결합니다. 처음 시작할 때는 약 3 GB의 이미지를 내려받으며 몇 분이 걸립니다. 생성 로그의 마지막 줄들이 출력하는 주소에서 대시보드를 여세요(`Loom is running in the
codespace. Dashboard: https://<codespace name>-7777.app.github.dev`, 즉 **Ports** 탭의 7777 포트 포워딩 주소). 그 위에 있는 설치 프로그램의 `http://127.0.0.1:7777/`은 codespace 자체의 주소입니다. codespace의 터미널에서 `cd ~ && claude`를 실행하세요(Loom 자체 작업용 지침이 있는 체크아웃 밖에서 실행합니다). 로그인한 뒤 "Remember that I never deploy on Friday"라고 말하고, 새 `claude` 세션에서 "When can I deploy?"라고 물어보세요.

이 체험판은 로그인 없이 단일 사용자로 실행됩니다. 브라우저에서 7777 포트는 비공개로 포워딩되며, 그 주소는 내 GitHub 계정에만 응답합니다. 비공개로 유지하세요. 공개로 바꾸거나 조직에 비공개로 공유하면, 로그인 없는 서버를 공유 대상 모두에게 여는 셈입니다. codespace 안의 에이전트(예: 그 안의 `claude`)는 `http://localhost:7777`을 사용합니다. 대시보드의 연결 안내 줄은 대신 포워딩된 주소를 표시하는데, 이 주소는 GitHub 로그인이 필요하며 codespace 안의 에이전트에는 그 로그인이 없습니다. 데스크톱 VS Code에서 codespace를 열면 이 포트가 노트북의 `localhost:7777`로도 포워딩되는데, 이 경우 로컬 설치와 마찬가지로 노트북의 모든 프로그램에 열려 있고 노트북의 에이전트도 그곳에서 접근할 수 있습니다. 이 포워딩에는 그 로컬 포트가 비어 있어야 하므로, 로컬 Loom이 있다면 먼저 중지하세요. 대시보드는 브라우저 주소에서 사용하세요. 노트북의 `localhost:7777`에서는 페이지가 열리지만 서버가 그 요청을 거부해 데이터가 표시되지 않으며, 포워딩된 주소만 이 서버 자체의 페이지입니다.

설치는 `/workspaces/.loom`에 있으므로, codespace를 재빌드해도 `.env`와 Compose 프로젝트는 유지됩니다. 다만 메모리는 재빌드가 보존하지 않을 수 있는 Docker 볼륨에 저장되며, 재빌드하면 그 위에서 최신 릴리스의 설치 프로그램도 실행되어 그 릴리스로 옮겨 갑니다. 이 문서가 `~/.loom` 또는 `"$HOME/.loom"`이라고 쓴 곳에서 codespace는 `/workspaces/.loom`을 사용합니다. Codespaces는 7777 포트가 열리자마자 포트를 열겠느냐고 제안합니다. 대시보드는 생성 로그에 "Loom is running in the codespace"가 출력된 뒤에 응답합니다. 로그가 대신 오류로 끝나면 codespace의 터미널에서 `sh .devcontainer/start-loom.sh`를 실행하세요. 첫 실행에서 설정된 내용은 유지됩니다.

<a id="start-in-multi-user-mode"></a>

### 다중 사용자 모드로 시작

첫 부팅부터 계정과 로그인이 있는 릴리스 이미지 신규 설치를 하려면, 컨테이너를 시작하지 않고 스택 파일만 준비하세요.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Docker를 시작하기 전에 `~/.loom/.env`를 편집하세요. 기존 identity 및 auth 항목을 교체하고 가입 설정을 추가합니다.

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

생성된 `COMPOSE_PROJECT_NAME`과 `LOOM_JWT_SECRET`은 그대로 두세요. 그런 다음 스택을 가져와 시작합니다(자체 ClickHouse를 쓴다면 설치 프로그램이 안내한 대로 먼저 `ch-setup`을 실행하세요).

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

[상태 확인](#check-the-stack)이 통과하면 `http://localhost:7777/`을 열고 가입하세요. SDK와 MCP 클라이언트에는 토큰이 필요합니다. 아래 예제의 단일 사용자 기본값 `dev/local`이 아니라, 내 계정에 부여된 org와 네임스페이스를 사용하세요.

이는 다중 사용자 인증을 켜는 것이지 공개 네트워킹이 아닙니다. 스택은 여전히 localhost에 바인딩됩니다. 팀과 공유하기 전에 [운영자 접근](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)과 [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls)를 설정하세요. [실제 모델과 API 키](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)도 함께 설정해야 합니다. 다중 사용자 모드를 선택한다고 가짜 LLM이 대체되지는 않습니다.

이미 실행 중인 단일 사용자 스택은 대신 [전환 안내](#2-open-the-dashboard)를 따르세요. 컨테이너를 다시 만들어도 기존 데이터 볼륨은 보존됩니다.

<a id="contributor-path-build-a-checkout"></a>

### 기여자 방법: 체크아웃 빌드

사전 요구 사항: Docker Compose 2.34 이상. 체크아웃의 compose 파일이 ClickHouse를 매일 가져오는 데 필요합니다.

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up`은 세 가지를 수행합니다.

1. git이 추적하지 않는 두 파일을 만듭니다. `.env.example`에서 `.env`를, 템플릿에서 `deploy/clickhouse-config/named_collections.xml`을 만듭니다. 어느 쪽도 덮어쓰지 않으며, 추출기 토큰이 없는 기존 `.env`에는 토큰을 추가합니다.
2. dev compose 스택을 `--build`로 실행합니다.
3. 서버가 준비될 때까지 `http://localhost:7777/v1/health`를 폴링하며 보류 중인 마이그레이션 수를 출력합니다. `--timeout`(300초) 후에는 마지막 health 응답 본문과 로그 줄을 보여 주고 포기합니다.

기여자의 첫 부팅은 이미지를 빌드하고 모든 스키마 마이그레이션을 적용하므로 몇 분을 예상하세요. 그런 다음 `loom up`은 대시보드 URL(`http://localhost:7777/`), 첫 메모리를 저장하는 `curl` 호출과 Python 스니펫, 그리고 Claude Code를 연결하는 줄을 출력합니다. 인증이 켜져 있으면 writer 토큰도 발급해 그 스니펫에 넣습니다.

| 플래그       | 동작                         |
| ------------ | ---------------------------- |
| `--rebuild`  | 코드 변경 후 다시 빌드합니다. |
| `--no-build` | 캐시된 이미지를 재사용합니다. |
| `--open`     | 대시보드를 엽니다.           |
| `--port`     | 다른 Loom 포트를 사용합니다. |
| `--env-file` | 다른 env 파일을 지정합니다.  |

실행 중인 스택에서 `loom up`을 다시 실행하면 health만 다시 확인합니다. `make dev`는 CLI 없이 같은 일을 하며 다음을 실행합니다.

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

`compose.yaml`이 먼저 오므로 Compose는 `${VAR:-default}` 치환을 위해 저장소 루트의 `.env`를 읽습니다. `--env-file .env`는 그것을 명시할 뿐입니다. `loom down`은 스택을 중지하고 볼륨을 보존합니다. `loom down --delete-data`는 생성된 Compose 프로젝트 이름을 입력해야만 이 설치의 이름이 지정된 볼륨만 영구 삭제하며, `--yes`는 비대화형 확인입니다. Makefile 타깃도 같습니다. `make down`은 데이터를 보존하고 `make down-all`은 삭제합니다.

<a id="what-the-stock-env-boots"></a>

### 기본 `.env`가 부팅하는 것

체크아웃과 릴리스 번들의 `.env` 템플릿은 모두 단일 사용자 모드를 선택합니다. 릴리스 번들은 `LOOM_IDENTITY_PROVIDER=single-user`를 명시적으로 설정하고, 체크아웃은 비워 두고 `LOOM_AUTH_REQUIRED=false`로부터 같은 모드를 추론합니다.

| 설정                                                                         | 값            | 효과                                                                      |
| ---------------------------------------------------------------------------- | ------------- | ------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (또는 비어 있음)  | `single-user` | 로그인이 없습니다. 기본 네임스페이스는 `dev/local`입니다. SDK와 MCP 호출에 토큰이 필요 없습니다. |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | 가짜 LLM      | 추출, 엔티티 태깅, `memory.ask` 합성이 아무것도 만들지 않습니다.          |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (릴리스 번들)      | bge-small     | 이미지에 포함된 모델 파일로 CPU에서 시맨틱 검색을 합니다.                 |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (체크아웃) | 가짜 임베더   | 검색이 문자 n-gram으로 순위를 매깁니다. 연결 확인에는 충분하지만 재현율 확인에는 부족합니다. |

두 설치 프로그램 모두 고유한 `COMPOSE_PROJECT_NAME`, 무작위 JWT 서명 비밀 값, 무작위 추출기 토큰, `LOOM_BIND_HOST=127.0.0.1`을 생성합니다. 따라서 별도의 클론이 ClickHouse 볼륨을 모르는 사이에 공유하지 않으며, Loom과 ClickHouse는 이 컴퓨터에서만 접근할 수 있습니다.

실제 프로바이더를 쓰려면 **Settings → Models & keys**를 열고, 프로바이더와 모델을 선택하고, API 키를 입력한 다음 **Test & apply**를 클릭하세요. OpenAI API 키 하나로 LLM과 임베딩을 모두 처리할 수 있습니다. Docker를 다시 시작할 필요는 없습니다. 임베딩을 변경하면 검증된 재빌드가 실행되며, 진행 상황과 재시도는 대시보드에 표시됩니다.

환경 변수 기반 배포를 원하면 `~/.loom/.env`(릴리스 스택) 또는 `.env`(체크아웃)의 프로바이더 줄을 교체하세요. `OPENAI_API_KEY`만 설정해서는 아무것도 바뀌지 않습니다. 기본 프로바이더 줄이 프로바이더를 명시적으로 지정하기 때문입니다. 로컬 또는 가짜 임베더에서 다른 것으로 옮기면 저장된 내용이 다시 임베딩됩니다.

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Claude, OpenAI, 호스팅 임베딩, 구독 전용 클라이언트, LLM 프로바이더가 하나뿐인 배포는 [모델과 API 키 설정](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)을 따르세요. 릴리스 스택의 `.env`를 편집한 뒤에만 `docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom`으로 적용하세요. 단순 재시작은 컨테이너의 환경을 다시 읽지 않습니다. 고급 설정은 [배포 → 프로바이더](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)에 있습니다.

<a id="check-the-stack"></a>

### 스택 확인

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health`는 ClickHouse에 접근할 수 있고 `code_tier_failing`(저장소의 코드 인덱스 티어 실패)을 제외한 어떤 문제도 오류가 아닐 때 `status: ok`를 반환합니다. `fake_embedding`, `fake_llm`, `providers_degraded`는 현재 어떤 모드인지 알려 주고, `problems`는 각 문제와 해결 방법을 나열하며, `schema.pending`은 적용되지 않은 마이그레이션을 나열합니다.
- `loom doctor`는 서버 측 점검마다 한 줄씩 출력합니다. `--json`은 JSON을 출력합니다. 소스 체크아웃 스택에서는 `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`를 실행하세요.

<a id="2-open-the-dashboard"></a>

## 2. 대시보드 열기

`http://localhost:7777/`은 `single-user` 모드에서 대시보드로 바로 열립니다.
멀티 유저 모드에서는 먼저 가입하거나 로그인해야 합니다. `single-user` 모드에서는
대시보드와 에이전트의 기본값이 `dev/local`을 사용합니다(`LOOM_DEFAULT_NAMESPACE`로
네임스페이스 이름을 바꿀 수 있습니다). 이 모드는 편의를 위한 모드이며 사용자 격리 경계가 아닙니다. 서버에 접근할 수 있는 사람은 누구나 서버의 데이터와 관리 기능에 접근할 수 있습니다. localhost 전용으로만 사용하세요.

대신 가입 기능을 사용해 보려면(`trust-network`: 이메일과 비밀번호를 사용하며, 네임스페이스 이름을 가장 먼저 등록한 사람이 해당 네임스페이스의 관리자가 됩니다) `~/.loom/.env`(릴리스 스택) 또는 `.env`(체크아웃)의 다음 항목을 교체하세요.

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

`single-user`를 명시적으로 선택한 경우에는 `LOOM_AUTH_REQUIRED=true`만 설정해서는 충분하지 않습니다. 이 모드는 인증을 꺼 둔 상태로 유지합니다. 설치 프로그램이 생성한 `LOOM_JWT_SECRET`과 기존 볼륨은 그대로 두고, 서버를 다시 생성하여 환경 변수를 불러오세요.

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

그러면 대시보드는 **Sign up** 탭이 있는 로그인 페이지로 열리며, 모든 SDK 및 MCP 호출에는 토큰이 필요합니다. 아이덴티티 모드는 [아이덴티티와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes)에서 확인할 수 있습니다. `trust-network`를 지정하면 루프백이 아닌 `LOOM_BIND_HOST`도 가입이 열린 상태로 시작할 수 있으며, 이 경우 해당 포트에 접근할 수 있는 누구나 가입할 수 있습니다. 팀이 서버에 접근할 수 있게 만들기 전에, 운영자 접근은 [배포 가이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)를, [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls)는 해당 문서를 따르세요.

<a id="3-get-a-token"></a>

## 3. 토큰 발급받기

싱글 유저 모드에서는 토큰이 선택 사항입니다. 그래도 하나 발급해 두면 나중에 인증을 켰을 때 스크립트가 계속 동작합니다. 단, 스크립트가 `org`(또는 빈 값), `namespace`, `agent`를 직접 지정해야 합니다. SDK가 자격 증명을 찾아 보내는 경우([Python SDK → 자격 증명](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)처럼) 자격 증명을 보내는 호출에는 이 값들이 하나도 자동으로 채워지지 않으며, 빈 `org`는 토큰 자체의 값이 됩니다.

- **대시보드:** **Settings → Tokens → New token**. 기본적으로 토큰은 사용자가 속한 모든 네임스페이스와 리포지토리를 읽을 수 있고(제한된 리포지토리는 직접 추가한 경우에만), 컬렉션은 읽을 수 없으며, 현재 있는 네임스페이스에 저장합니다. _Only what you choose_ 를 선택하면 체크한 네임스페이스, 컬렉션, 리포지토리로 범위가 제한됩니다(처음에는 아무것도 선택되어 있지 않습니다). 체크한 항목은 나열된 순서대로 검색되며, 행의 화살표로 순서를 바꾸거나 데스크톱에서는 행의 그립을 드래그해 바꿀 수 있습니다. 결과 화면에는 토큰이 한 번만 표시되며, MCP 클라이언트나 스크립트용 설정도 함께 나옵니다.
- **CLI, 운영자로서:** 서명 시크릿을 가지고 있는 서버 컨테이너 안에서 실행합니다.

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  머신 밖으로 나가는 토큰에는 `--ttl-days 30`을 추가하세요. `loom mint-token --help`에서 범위 지정 옵션(`--namespace-role`, `--read-scope`, `--traffic-class`)을 확인할 수 있습니다.

- **스스로 가입하는 봇** (trust-network 전용):
  `loom init --agent nightly-triage --server-url http://localhost:7777`을 실행하면 네임스페이스 `agent-nightly-triage`가 만들어지고, writer 토큰이 `~/.loom/token`에, URL이 `~/.loom/config.json`에 저장되며, 클레임 코드가 출력됩니다. 사람이 `/oauth/agent-claim`에서 자신의 비밀번호로 이 코드를 한 번 입력하면 소유권을 가져갈 수 있습니다.

SDK, CLI, 플러그인이 찾는 위치에 토큰을 저장하세요.

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

클레임, 역할, 권한 부여는 [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)에서 확인하세요.

<a id="4-first-memory-over-http"></a>

## 4. HTTP로 첫 메모리 만들기

모든 연산은 JSON 본문을 담은 `POST /v1/<op>` 요청입니다. 설치 프로그램이나 체크아웃으로 설치한 기본 `.env`의 새 설치본은 단일 사용자 모드입니다. 토큰이 없고, `org`나 `namespace`를 지정하지 않는 일상적인 메모리 또는 검색 호출(`memory.set`, `memory.get`, `memory.search` 등)은 설치본 자체의 org `dev`, 네임스페이스 `local`에서 실행됩니다. 이는 MCP와 대시보드가 사용하는 값과 같습니다. `agent`를 지정하지 않은 `memory.set`은 에이전트 `http`로 기록되고, 검색은 모든 에이전트를 읽습니다. 따라서 아래 호출에는 각 호출 고유의 필드만 있으면 됩니다([해당 연산](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)). 토큰 없이 호출했을 때 401 `AUTH_INVALID`가 반환되면 인증이 켜져 있는 것입니다. 이 경우 `-H "Authorization: Bearer $LOOM_API_KEY"`를 추가하고 계정의 `org`, `namespace`, `agent`를 지정하세요. 토큰이 비어 있는 헤더는 거부됩니다.

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.get -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window"}' | jq '{found, value}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"query":"when can we deploy?"}' | jq '.results[0].content_excerpt'
```

`memory.set` 응답에는 `memory_id`, `content_sha256`, `status`가 들어 있습니다.
`memory.get`은 키 하나를 다시 읽어 옵니다. `found`는 해당 키에 유효한 값이 있는지를 나타내고, `value`는 기록된 그대로의 값입니다. 검색 응답에는 `results`와 `abstain`이 들어 있으며, 후자는 가장 좋은 결과가 답변의 근거로 삼을 만큼 충분히 강한지를 나타냅니다. 검색은 대시보드에서 추가한 메모리를 포함해 네임스페이스 안의 모든 에이전트의 메모리를 읽습니다. 특정 에이전트의 메모리만 읽으려면 `agent`를 지정하세요.
`GET /v1/operations`는 모든 연산을 나열하며, <http://localhost:7777/docs>에서는 브라우저로 각 연산을 직접 시험해 볼 수 있습니다. 요청과 응답 형식은 [HTTP API 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)에 있습니다.

<a id="5-first-memory-from-python"></a>

## 5. Python으로 첫 메모리 만들기

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

아직 PyPI에 올라오지 않은 릴리스는 해당 릴리스의 에셋으로 설치합니다
([GitHub Release에서 설치하기](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)).

이 예제는 `org`와 `namespace`를 명시하므로, 3단계에서 만든 토큰이 있든 없든 동작합니다. SDK는 토큰을 찾으면 함께 전송합니다. 단일 사용자 서버에서는 토큰 없이 호출할 때 이 둘을 생략해도 됩니다. `Agent(agent="quickstart", base_url=...)`도 `dev`/`local`에서 실행됩니다.

```python
import asyncio

from loom import Agent


async def main():
    async with Agent(
        org="dev",
        namespace="local",
        agent="quickstart",  # names the writer in the audit log
        base_url="http://localhost:7777",
    ) as a:  # sends a credential it finds; none needed in single-user mode
        await a.memory.set("standup_time", "Daily standup is at 9:30.")
        row = await a.memory.get("standup_time")  # None when the key is unset
        print(row["value"] if row else "unset")
        for hit in await a.memory.search("when is standup?"):
            print(hit.content_excerpt)


asyncio.run(main())
```

SDK가 서버와 자격 증명을 찾는 방식은
[Python SDK → 자격 증명](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)에 있습니다.
TypeScript SDK는 `npm install @clickhouse/loom`으로 설치합니다
([클론에서 설치하기](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)).

클라이언트 인터페이스는 [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)와
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) 레퍼런스에 정리되어 있습니다.

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. 코딩 에이전트로 첫 메모리 만들기

설치 프로그램이 에이전트를 이미 연결했다면 이 섹션의 마지막 문단으로 건너뛰세요.

Claude Code에서는 Loom 플러그인을 설치합니다. 플러그인은 MCP 도구 위에 훅(세션 시작 시 컨텍스트 로드, 세션 종료 시 캡처)과 스킬을 추가합니다:

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

이 머신의 단일 사용자 서버를 사용한다면, Claude Code가 플러그인 설정을 물을 때 기본 서버 URL을 그대로 두고 토큰은 비워 두세요.

플러그인 없이 MCP 항목을 직접 추가할 수도 있습니다:

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

두 방법 모두 별도의 토큰 없이 이 머신의 단일 사용자 서버에 연결됩니다.

인증이 켜져 있다면 **Settings → Tokens**에서 토큰을 만들고 **Connect** 아래의 **Claude Code**를 선택하세요. 대화상자에 플러그인용 설정과 토큰이 포함된 `claude mcp add` 항목용 설정이 표시됩니다. 그런 다음 다음을 진행합니다.

1. `~/.loom/config.json`을 대화상자의 `config.json` 블록과 토큰이 저장될 네임스페이스(`"namespace": "<namespace>"`)로 덮어쓰세요. 단일 사용자 세션에서 파일에 남긴 `org` 또는 `namespace`는 토큰보다 우선하며, 이 값이 없으면 플러그인은 `*`가 아닌 토큰의 첫 번째 네임스페이스나 `coding`에 기록합니다. 이전에 설정한 `code_repo`나 `agent`가 있다면 다시 추가하세요.
2. `LOOM_API_KEY`(3단계에서 설정했을 수 있습니다: `unset LOOM_API_KEY`)와 플러그인의 `token` 옵션은 비워 두거나 새 토큰으로 설정하세요. 플러그인은 두 값을 그 파일보다 먼저 읽습니다
   ([구성](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)).
3. 토큰 없이 만든 `claude mcp add` 항목은 대화상자의 명령을 실행하기 전에 제거하세요(`claude mcp remove loom -s user`). 대화상자의 명령은 이미 존재하는 이름을 거부합니다.

`loom install --client <name>`은 Claude Desktop, Claude Code, Codex, Cursor, VS Code, Cline, Windsurf, Zed용 MCP 항목을 작성합니다. 플래그를 생략하면 발견한 모든 클라이언트를 연결합니다. Codex와 Cursor는 [코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)에서 다룹니다.

새 세션을 시작하고 _"Remember that I never deploy on Friday."_ 라고 말해 보세요. 에이전트가 `loom_memory_set`을 호출합니다. 새 세션에서 _"When can I deploy?"_ 라고 물으면 에이전트가 `loom_search`를 호출하고 저장된 사실을 바탕으로 답합니다. 저장하고 검색할 시점은 모델이 도구 설명을 보고 판단하며, 별도의 트리거 단어는 없습니다.

<a id="7-see-it-in-the-dashboard"></a>

## 7. 대시보드에서 확인하기

| 페이지            | 표시 내용                                                    |
| ----------------- | ------------------------------------------------------------ |
| `/memories`       | 네임스페이스의 메모리를 작성자와 시간과 함께 보여줍니다.     |
| `/admin/activity` | 감사 로그: 어떤 에이전트가 어떤 키를 언제 기록했는지 보여줍니다. |
| `/admin/usage`    | 작업별, 에이전트별 비용을 보여줍니다.                        |

나머지는 [대시보드](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)에서 확인할 수 있습니다.

<a id="optional-index-a-repository-on-the-released-stack"></a>

## 선택 사항: 릴리스된 스택에서 리포지토리 인덱싱하기

이 스택에서는 대시보드에서 리포지토리를 추가하면 `SANDBOX_UNAVAILABLE`로 거부됩니다.
[리포지토리 작업 샌드박스](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox)가 필요한데,
Docker의 기본 격리가 이를 막기 때문에 이 스택은 샌드박스를 꺼 둡니다. 신뢰할 수 있는 리포지토리만
등록하세요. 빌드 단계(TypeScript 설치, Go 모듈 다운로드, CMake)는 샌드박스 없이 `loom` 컨테이너에서
실행되며, 이 컨테이너의 환경에는 JWT 시크릿과 프로바이더 키가 들어 있습니다.

`LOOM_CODE_REPOS_JSON`은 모든 리포지토리를 JSON 배열로 담은 한 줄입니다
([항목 구성하기](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)).
`~/.loom/.env`에서 해당 줄의 주석을 해제하고(없으면 추가하고) 수정하세요.
`dev`는 단일 사용자 스택이 실행되는 org이자, 다중 사용자 계정이 가입하는 org입니다(`LOOM_SELF_SIGNUP_ORG`).
이 값을 바꿨다면 여기와 아래 `--org`에 계정의 org를 사용하세요.

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom은 리포지토리의 빌드 파일(`go.mod`, `package.json`, `pyproject.toml`, 루트의 `CMakeLists.txt` 등)을 보고
  컴파일러 티어 인덱서를 고릅니다. 대신 `scip_producer`로 하나를 직접 지정할 수도 있습니다
  ([컴파일러 티어](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)).
  C/C++는 구성된 빌드 트리도 필요하므로, 빌드 트리가 없으면 C/C++ 리포지토리는
  파서 티어만 적용됩니다.
- 비공개 리포지토리라면 GitHub 토큰을 추가하고(`.env`의 `LOOM_CODE_GITHUB_TOKEN` 또는 항목의
  `github_token`) `"public":true`를 빼세요. 이 값은 호스트에서 공개된 리포지토리용이며, org의 누구나
  **Repositories**에서 바로 리더로 인덱스에 참여할 수 있게 합니다. 첫 `loom code init` 전에 결정하세요.
  이 명령은 `public`을 보고 네임스페이스의 참여 정책을 정하며, 이후 `public`을 수정해도
  정책은 바뀌지 않습니다(메인테이너는 `code.repo.set_access`로 변경합니다).

그런 다음 `loom` 컨테이너를 다시 만들고 첫 인덱싱을 실행합니다.

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed`는 릴리스된 스택의 로컬 임베더에서 실행됩니다. 가짜 임베더를 쓰는 경우(체크아웃의
`.env`, 또는 여전히 `fake-ngram-*`를 지정한 업그레이드된 설치) 거부하고 종료합니다. 그래도 연결을
시험하려면 `--allow-fake-embedder`를 추가하세요. 나중에 **Settings → Models & keys**에서 모델을
선택하면 코드 인덱스가 다시 임베딩됩니다. `.env`를 변경한 뒤에는 별도의 재임베딩이 필요하며,
자세한 내용은 [모델과 키](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)를 참고하세요.

다중 사용자 모드에서는 등록된 리포지토리에 메인테이너가 없으므로, `"public":true`가 없으면 리더의
참여 요청을 승인할 사람이 없어 대기 상태로 남습니다. `loom code init` 후에 운영자로서 컨테이너에서
발급한 토큰으로 누군가에게 접근 권한을 부여하세요. `admin`은 이후 리더를 승인하는 메인테이너로 만들고,
`viewer`는 읽기만 허용합니다. subject는 계정이 가입할 때 쓴 네임스페이스(로그인 핸들이자 토큰의
subject)입니다. 자체 가입 이메일은 검증되지 않으므로, 이메일에 부여한 권한으로는 아무도 들어올 수 없습니다.

```bash
ADMIN_TOKEN=$(docker compose --project-directory "$HOME/.loom" exec -T loom loom mint-token --subject operator --org dev --namespaces code-myrepo --roles admin --ttl-days 1)
curl -s http://localhost:7777/v1/grants.create -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"dev","namespace":"code-myrepo","subject":"your-namespace","role":"admin"}'
```

<a id="optional-ask-a-question-about-a-document"></a>

## 선택 사항: 문서에 대해 질문하기

```bash
loom demo
```

`loom demo`는 이 저장소의 `AGENTS.md`를 업로드하고, `knowledge.ingest`로 수집한 뒤(스택의 레퍼런스 추출기가 파싱합니다) `knowledge.synthesize`로 문서에 대한 질문을 던져 답변과 그 답변이 인용한 청크를 출력합니다. 가짜 LLM을 사용하는 경우에는 대신 상위 `knowledge.search` 결과를 출력합니다. `--file`과 `--question`으로 직접 가진 문서와 질문을 지정할 수 있습니다.

<a id="common-problems"></a>

## 자주 발생하는 문제

| 증상                                                                          | 원인과 해결 방법                                                                                                                                                                                                                                                             |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages`가 사실(fact)을 기록하지 않음                              | LLM이 가짜(fake) 모델입니다. [모델과 키](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)를 따라 LLM을 설정하세요.                                                                                                                                                                                    |
| 검색 결과가 제멋대로임                                                        | 임베더가 가짜입니다(`.env`의 `fake-ngram-*`). [모델과 키](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)의 안내대로 바꾸면 저장된 데이터가 다시 임베딩됩니다. 모델을 바꾸려고 볼륨을 삭제하지 마세요.                                                                               |
| 인증을 켠 뒤 401 `AUTH_INVALID` 발생                                          | 토큰이 없거나 다른 `LOOM_JWT_SECRET`으로 서명된 토큰입니다. [토큰 발급](#3-get-a-token)의 Compose 명령으로 다시 발급하세요.                                                                                                                                                   |
| 대시보드가 503을 반환함                                                       | 서버를 소스에서(`make server`) 실행했는데 프런트엔드가 빌드되지 않았습니다. `make frontend-build`를 실행하세요.                                                                                                                                                                             |
| 릴리스 스택이 정상 상태가 되지 않음                                           | `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`를 실행하세요. [문제 해결](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)을 참고하세요.                                                                                                                             |
| 리포지토리를 추가하면 `SANDBOX_UNAVAILABLE`이 반환됨                          | 스택이 리포지토리 작업 샌드박스 없이 실행 중입니다. `LOOM_CODE_REPOS_JSON`으로 리포지토리를 인덱싱하세요([리포지토리 인덱싱](#optional-index-a-repository-on-the-released-stack)).                                                                                           |
| 설치 프로그램이 `7777` 포트가 사용 중이라고 함                                | 다른 프로그램이나 다른 Loom이 해당 포트에서 대기 중입니다. 그것을 중지하거나, `sh loom-install.sh` 앞에 `LOOM_PORT=7778`을 붙여 설치 명령을 다시 실행하세요. `.env`에 포트가 유지됩니다.                                                                                                    |
| 설치 프로그램이 `7777` 포트가 응답하지 않는다고 함                            | 어떤 프로그램이 응답 없이 포트를 점유하고 있거나, `LOOM_BIND_HOST`, `LOOM_PORT` 또는 방화벽 때문에 이 호스트가 포트에 접근하지 못합니다.                                                                                                                                                       |
| 설치 프로그램이 `7777` 포트를 확인하지 못함                                   | curl이 종료 코드가 나타내는 이유로 실패했습니다. `LOOM_BIND_HOST`와 `LOOM_PORT`를 확인하세요.                                                                                                                                                                                      |
| WSL의 Docker Desktop에서 설치가 `Ports are not available`로 중단됨           | Windows가 포트를 점유하거나 예약해 두었고, WSL 내부의 설치 프로그램 검사로는 이를 알 수 없습니다. `netsh interface ipv4 show excludedportrange protocol=tcp`로 예약된 범위를 확인한 뒤, `sh loom-install.sh` 앞에 해당 범위를 벗어난 `LOOM_PORT`를 붙여 설치 명령을 다시 실행하세요. |
| 클론한 저장소에서 `8123` 또는 `9000` 포트가 이미 사용 중임                    | ClickHouse 호스트 포트를 다시 매핑하세요([로컬 스택](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)).                                                                                                                                                                       |
| 설치 프로그램이 이미지를 가져오지 못함                                        | Docker가 레지스트리에 직접 접근해야 합니다. 프록시 뒤에 있다면 Docker 데몬에 [프록시 설정](https://docs.docker.com/engine/daemon/proxy/)을 지정하세요.                                                                                                                               |

<a id="next"></a>

## 다음 단계

- [Loom 평가하기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md): 이 스모크 테스트를 워크로드별 품질, 지연 시간, 비용 평가로 발전시킵니다.
- [코딩 에이전트 연결하기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md): 이 스택에 Claude Code, Codex 또는 Cursor를 연결합니다.
- [첫 에이전트 만들기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): SDK로 ID, abstain 처리, 세션, 컨텍스트 팩, 실행을 다룹니다.
- [배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): 팀을 위해 Loom을 운영합니다.
