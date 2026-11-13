<!-- i18n: source=docs/getting-started/quickstart.md blob=2dcc795de559021866a2affe24bef8e4e8f41f81 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# 빠른 시작

이 페이지는 노트북에서 Loom을 부팅하고 HTTP, Python, MCP로 첫 번째 메모리를 저장하는 방법을 설명합니다. API 키도 계정도 필요하지 않습니다. 릴리스된 스택을 새로 설치하면 이미지에 포함된 작은 로컬 모델로 임베딩하므로, 네트워크 없이도 검색이 의미 있는 결과를 반환합니다(업그레이드한 설치는 `.env`에 지정된 임베더를 그대로 유지합니다). LLM은 가짜(fake)이므로 추출과 `memory.ask` 답변은 아무 결과도 만들지 않습니다. 체크아웃한 저장소의 `.env`는 두 가지 모두 가짜 provider를 사용합니다.

**기본 Docker 설치는 단일 사용자용입니다.** 로그인이 없고, localhost에서만 접근할 수 있으며, 기본 네임스페이스는 `dev/local`입니다. 멀티 유저 모드를 켜지 않고도 한 사람이 여러 에이전트를 연결할 수 있습니다. **OSS에서도 팀 배포를 지원하며**, 인증된 계정과 네임스페이스 접근 제어를 사용할 수 있습니다. [멀티 유저 모드로 시작](#start-in-multi-user-mode)하거나 [Deployment → Identity modes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)를 참고하세요.
두 모드 모두 동일한 Loom 기능을 지원합니다. 어느 모드에서든 실제 LLM과 임베딩을 사용하려면 provider 설정이 필요합니다.

<a id="prerequisites"></a>

## 사전 요구 사항

| 필요 항목                      | 용도                                                                           |
| ------------------------------ | ------------------------------------------------------------------------------ |
| compose 플러그인이 포함된 Docker | 스택 실행: ClickHouse와 Loom 서버.                                             |
| Python 3.12 이상               | 기여자 전용 `loom` CLI. SDK는 3.11 이상을 지원합니다.                          |
| Node.js 20 이상                | TypeScript SDK (선택 사항).                                                    |
| 사용 가능한 포트 `7777`        | Loom. 체크아웃 개발 환경에서는 ClickHouse도 `8123`과 `9000`에서 노출됩니다.    |
| `jq`                           | 4단계의 `curl` 출력 읽기 (선택 사항).                                          |

<a id="1-boot-the-stack"></a>

## 1. 스택 실행하기

<a id="fast-path-released-image-no-clone"></a>

### 빠른 경로: 릴리스된 이미지, clone 없음

사전 요건: Compose 2.20 이상이 포함된 Docker Desktop 또는 Docker Engine.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

이 명령은 설치 프로그램을 현재 디렉터리에 저장한 뒤 그 파일을 실행하므로, 홈 디렉터리나 본인만 쓸 수 있는 다른 디렉터리에서 실행하세요. 여러 사용자가 쓰는 디렉터리에서는 다른 사람이 실행 전에 `loom-install.sh`를 교체하거나 다른 파일을 가리키게 할 수 있습니다. 다운로드가 실패하면 curl의 오류 메시지와 0이 아닌 종료 코드와 함께 중단됩니다. 설치가 실패한 경우에는 출력된 문제를 해결한 뒤 전체 명령을 다시 실행하세요. 설치 프로그램은 최신 GitHub Release에서 `loom-stack.tar.gz`를 내려받아 SHA-256을 검증하고, Compose 및 ClickHouse 설정을 `~/.loom` 아래에 설치합니다. 그런 다음 대응하는 `ghcr.io/clickhouse/loom:X.Y.Z`와 `ghcr.io/clickhouse/loom-extractor:X.Y.Z`(문서 추출기 서비스) 이미지를 pull하고 `http://localhost:7777/v1/health`가 응답할 때까지 기다립니다. 서버 이미지에는 API, 워커, 마이그레이션, 그리고 서버 루트에서 제공되는 빌드된 React 대시보드가 들어 있으므로 Python이나 Node를 따로 설치할 필요가 없습니다. Python, TypeScript, Go 컴파일러 인덱서가 포함되어 있고 기본으로 활성화됩니다. TypeScript 인덱서로 인덱싱한 리포지토리는 의존성이 자동으로 설치됩니다. 두 이미지 모두 Apple Silicon의 Docker Desktop을 포함해 Linux `amd64`와 `arm64`를 지원합니다.

스택은 자체 ClickHouse 컨테이너를 실행합니다. 이미 운영 중인 ClickHouse, 즉 자체 관리 서버나 ClickHouse Cloud 서비스(26.4 이상, Keeper 포함)를 사용하려면 해당 URL을 전달하세요. URL에 비밀번호가 없으면 설치 프로그램이 비밀번호를 물어보고, `loom` 데이터베이스와 읽기 전용 reader 사용자를 만들거나, 관리자가 실행해야 하는 SQL을 출력합니다
([ClickHouse 선택하기](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

각 릴리스의 `install.sh`는 해당 릴리스를 설치합니다. 따라서 `latest`가 제공하는 사본은 최신 릴리스를 설치하고, 아래의 릴리스별 URL은 항상 그 릴리스를 설치합니다. 더 새로운 설치 위에서는 이전 릴리스의 사본이 설치를 거부합니다. 더 새로운 데이터에 이전 Loom과 ClickHouse를 시작하지 않기 위해서입니다. 의도적으로 다운그레이드하려면 [설치 프로그램으로 설치한 환경 다운그레이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install)를 따르세요.
릴리스를 고정하려면 해당 릴리스의 URL을 사용하세요:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

시작하기 전에 모든 내용을 직접 확인하려면 파일을 내려받아 검사하세요:

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

출력 내용이 올바르면 검증한 번들로 설치하세요:

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

설치 프로그램에 전달한 `LOOM_PORT`, `LOOM_BIND_HOST`, `LOOM_IMAGE_REPOSITORY`, `LOOM_EXTRACTOR_IMAGE_REPOSITORY`, `LOOM_CLICKHOUSE_IMAGE`는 설치 디렉터리의 `.env`에 기록됩니다. 따라서 이후 그 위치에서 실행하는 `docker compose` 명령과 이후의 설치 프로그램 실행은 같은 포트와 이미지를 사용합니다. loopback이 아닌 `LOOM_BIND_HOST`는 Loom을 이 호스트 밖으로 공개하며, 이 경우 서버는 `LOOM_PUBLIC_BASE_URL`과 로그인 설정이 없으면 시작을 거부합니다. `LOOM_SKIP_START=1`로 설치하고 `.env`(아래 참조)에 해당 값을 설정한 다음, `LOOM_SKIP_START=1` 없이 설치 명령을 다시 실행하세요.

각 릴리스는 빌드 시점의 최신 ClickHouse 릴리스를 실행합니다. 따라서 같은 Loom 버전에 대해 설치 명령을 다시 실행하면 ClickHouse가 유지되고, 더 새로운 Loom 버전을 설치하면 ClickHouse도 함께 바뀝니다. 단, `.env`가 `LOOM_CLICKHOUSE_IMAGE`를 고정해 둔 경우는 예외입니다(릴리스를 다시 따라가려면 그 줄을 삭제하세요). 설치 명령을 다시 실행하면 관리되는 Compose/설정 파일을 갱신하고 컨테이너를 재조정합니다. `~/.loom/.env`와 이름이 지정된 Docker 볼륨은 보존되며, 추출기의 URL과 토큰이 없는 파일에는 이를 추가합니다. 데이터를 삭제하지 않고 중지하려면:

```bash
docker compose --project-directory "$HOME/.loom" down
```

`--volumes`는 되돌릴 수 없는 초기화를 의도한 경우에만 추가하세요.

<a id="try-it-in-github-codespaces"></a>

### GitHub Codespaces에서 체험하기

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

리포지토리의 dev container는 로컬 머신에 아무것도 설치하지 않고 codespace에서 최신 릴리스를 실행합니다. 위의 설치 프로그램으로 Loom을 설치하고, 시작한 뒤, Claude Code를 연결합니다. 처음 시작할 때는 약 3 GB의 이미지를 내려받으며 몇 분이 걸립니다. 생성 로그의 마지막 줄에 출력되는 주소로 대시보드를 여세요(`Loom is running in the
codespace. Dashboard: https://<codespace name>-7777.app.github.dev`, 즉 **Ports** 탭의 7777 포트 포워딩 주소). 그 위에 나오는 설치 프로그램의 `http://127.0.0.1:7777/`은 codespace 자체의 주소입니다. codespace 터미널에서 `cd ~ && claude`를 실행하고(Loom 자체 작업용 지침이 있는 체크아웃 밖에서 실행하기 위해서입니다), 로그인한 뒤 "Remember that I deploy only on Tuesdays"라고 말해 보세요. 새 `claude` 세션에서 "When do I deploy?"라고 물어보면 됩니다.

이 체험 환경은 로그인 없이 단일 사용자로 실행됩니다. 브라우저에서 7777 포트는 비공개로 포워딩되며, 그 주소는 본인의 GitHub 계정에게만 응답합니다. 반드시 비공개로 유지하세요. 공개 또는 조직 비공개로 바꾸면 로그인 없는 서버가 공유 대상 모두에게 열립니다. codespace 안의 에이전트(예: codespace의 `claude`)는 `http://localhost:7777`을 사용합니다. 대시보드의 연결 안내 줄에는 대신 포워딩된 주소가 표시되는데, 이 주소는 GitHub 로그인이 필요하며 codespace 안의 에이전트에게는 그 로그인이 없습니다. 데스크톱 VS Code로 codespace를 열면 포트가 노트북의 `localhost:7777`로도 포워딩되며, 이 경우 로컬 설치와 마찬가지로 노트북의 모든 프로그램에 열려 있고 노트북의 에이전트도 그곳으로 접근할 수 있습니다. 포워딩에는 해당 로컬 포트가 비어 있어야 하므로 로컬 Loom이 있다면 먼저 중지하세요. 대시보드는 브라우저 주소에서 사용하세요. 노트북의 `localhost:7777`에서는 페이지는 열리지만 서버가 요청을 거부하므로 데이터가 표시되지 않습니다. 이 서버 자체의 페이지는 포워딩된 주소뿐입니다.

설치는 `/workspaces/.loom`에 있으므로 codespace를 다시 빌드해도 `.env`와 Compose 프로젝트는 유지됩니다. 다만 메모리가 저장된 Docker 볼륨은 다시 빌드할 때 유지되지 않을 수 있습니다. 다시 빌드하면 그 위에서 최신 릴리스의 설치 프로그램이 실행되어 해당 릴리스로 이동합니다. 이 문서에서 `~/.loom` 또는 `"$HOME/.loom"`이라고 쓴 곳은 codespace에서는 `/workspaces/.loom`을 사용합니다. Codespaces는 7777 포트가 열리자마자 이를 열도록 안내하지만, 대시보드는 생성 로그에 "Loom is running in the codespace"가 출력된 뒤에 응답합니다. 대신 로그가 오류로 끝나면 codespace 터미널에서 `sh .devcontainer/start-loom.sh`를 실행하세요. 첫 실행에서 설정된 내용은 그대로 유지됩니다.

<a id="start-in-multi-user-mode"></a>

### 다중 사용자 모드로 시작하기

첫 부팅부터 계정과 로그인을 사용하는 새 릴리스 이미지 설치를 하려면, 컨테이너를 시작하지 않고 스택 파일만 준비하세요:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Docker를 시작하기 전에 `~/.loom/.env`를 편집하세요. 기존의 identity 및 auth 항목을 교체하고 가입 설정을 추가합니다:

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

생성된 `COMPOSE_PROJECT_NAME`과 `LOOM_JWT_SECRET`은 그대로 두세요. 그런 다음 스택을 pull하고 시작합니다(자체 ClickHouse를 사용한다면 설치 프로그램의 안내대로 먼저 `ch-setup`을 실행하세요):

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

[상태 확인](#check-the-stack)을 통과하면 `http://localhost:7777/`을 열고 가입하세요. SDK와 MCP 클라이언트에는 토큰이 필요합니다. 아래 예제의 단일 사용자 기본값 `dev/local`이 아니라, 본인 계정에 부여된 org와 namespace를 사용하세요.

이는 다중 사용자 인증을 켜는 것이지 네트워크를 공개하는 것이 아닙니다. 스택은 여전히 localhost에 바인딩됩니다. 팀과 공유하기 전에 [운영자 접근](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)과 [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls)를 설정하세요. [실제 모델과 API 키](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)도 함께 설정하세요. 다중 사용자 모드를 선택한다고 해서 가짜 LLM이 대체되지는 않습니다.

이미 실행 중인 단일 사용자 스택은 대신 [전환 안내](#2-open-the-dashboard)를 따르세요. 컨테이너를 다시 만들어도 기존 데이터 볼륨은 보존됩니다.

<a id="contributor-path-build-a-checkout"></a>

### 기여자 경로: 체크아웃 빌드하기

사전 요건: Docker Compose 2.34 이상. 체크아웃의 compose 파일이 ClickHouse를 매일 pull하려면 이 버전이 필요합니다.

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up`은 세 가지 일을 합니다:

1. git이 추적하지 않는 두 파일을 만듭니다. `.env.example`로부터 `.env`를, 템플릿으로부터 `deploy/clickhouse-config/named_collections.xml`을 만듭니다. 둘 중 어느 것도 덮어쓰지 않으며, extractor 토큰이 없는 기존 `.env`에는 토큰을 추가합니다.
2. `--build`로 dev compose 스택을 실행합니다.
3. 서버가 준비될 때까지 `http://localhost:7777/v1/health`를 폴링하며, 대기 중인 마이그레이션 수를 출력합니다. `--timeout`(300초)이 지나면 마지막 health 응답 본문과 로그 줄을 보여 주고 포기합니다.

처음 기여자 부팅은 이미지를 빌드하고 모든 스키마 마이그레이션을 적용하므로 몇 분 정도 걸립니다. 이후 `loom up`은 대시보드 URL(`http://localhost:7777/`), 첫 메모리를 저장하는 `curl` 호출과 Python 스니펫, 그리고 Claude Code를 연결하는 줄을 출력합니다. 인증이 켜져 있으면 writer 토큰도 발급해 이 스니펫에 넣어 줍니다.

| 플래그       | 동작                         |
| ------------ | ---------------------------- |
| `--rebuild`  | 코드 변경 후 다시 빌드합니다. |
| `--no-build` | 캐시된 이미지를 재사용합니다. |
| `--open`     | 대시보드를 엽니다.           |
| `--port`     | 다른 Loom 포트를 사용합니다. |
| `--env-file` | 다른 env 파일을 지정합니다.  |

실행 중인 스택에서 `loom up`을 다시 실행하면 health만 다시 확인합니다. `make dev`는 CLI 없이 같은 일을 하며 다음을 실행합니다:

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

`compose.yaml`이 먼저 오면 Compose는 `${VAR:-default}` 치환을 위해 리포지토리 루트의 `.env`를 읽으며, `--env-file .env`는 이를 명시적으로 밝힐 뿐입니다. `loom down`은 스택을 중지하고 볼륨은 보존합니다. `loom down --delete-data`는 생성된 Compose 프로젝트 이름을 직접 입력한 뒤에만 이 설치의 이름 있는 볼륨만 영구 삭제합니다. `--yes`는 비대화형 확인입니다. Makefile 타깃도 같은 동작을 합니다. `make down`은 데이터를 보존하고 `make down-all`은 삭제합니다.

<a id="what-the-stock-env-boots"></a>

### 기본 `.env`로 부팅되는 구성

체크아웃과 릴리스 번들의 `.env` 템플릿은 모두 단일 사용자 모드를 선택합니다.
릴리스 번들은 `LOOM_IDENTITY_PROVIDER=single-user`를 명시적으로 설정하고, 체크아웃은 이를 비워 두고 `LOOM_AUTH_REQUIRED=false`로부터 같은 모드를 추론합니다.

| 설정                                                                         | 값            | 효과                                                                      |
| ---------------------------------------------------------------------------- | ------------- | ------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (또는 비어 있음) | `single-user` | 로그인 없음. 기본 namespace는 `dev/local`. SDK와 MCP 호출에 토큰이 필요 없습니다. |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | 가짜 LLM      | 추출, 엔티티 태깅, `memory.ask` 합성이 아무 결과도 만들지 않습니다.       |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (릴리스 번들)         | bge-small     | 이미지에 포함된 모델 파일로 CPU에서 시맨틱 검색을 수행합니다.             |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (체크아웃) | 가짜 임베더   | 문자 n-gram으로 검색 순위를 매깁니다. 연결 확인에는 충분하지만 재현율 확인에는 맞지 않습니다. |

두 설치 방식 모두 고유한 `COMPOSE_PROJECT_NAME`, 무작위 JWT 서명 시크릿, 무작위 extractor 토큰, 그리고 `LOOM_BIND_HOST=127.0.0.1`을 생성합니다. 따라서 서로 다른 clone이 ClickHouse 볼륨을 모르는 사이에 공유하지 않으며, Loom과 ClickHouse는 이 컴퓨터에서만 접근할 수 있습니다.

실제 provider를 사용하려면 **Settings → Models & keys**를 열어 provider와 모델을 선택하고 API 키를 입력한 뒤 **Test & apply**를 클릭하세요. OpenAI API 키 하나로 LLM과 임베딩을 모두 처리할 수 있습니다. Docker 재시작은 필요 없습니다. 임베딩을 변경하면 검증된 재구축이 실행되며, 진행 상황과 재시도는 대시보드에 표시됩니다.

대신 환경 변수 기반으로 배포하려면 `~/.loom/.env`(릴리스 스택) 또는 `.env`(체크아웃)의 provider 줄을 교체하세요. `OPENAI_API_KEY`만 설정해서는 아무것도 바뀌지 않습니다. 기본 provider 줄이 provider를 명시적으로 지정하고 있기 때문입니다. local 또는 가짜 임베더에서 다른 것으로 바꾸면 저장된 내용이 다시 임베딩됩니다.

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Claude, OpenAI, 호스팅 임베딩, 구독 전용 클라이언트, LLM provider가 하나뿐인 배포는 [모델과 API 키 설정](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)을 따르세요.
릴리스 스택의 `.env`를 편집한 뒤에만 `docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom`으로 적용하세요. 단순 재시작은 컨테이너의 환경 변수를 다시 읽지 않습니다. 고급 설정은 [Deployment → Providers](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)에 있습니다.

<a id="check-the-stack"></a>

### 스택 확인하기

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health`는 ClickHouse에 접근할 수 있고 `code_tier_failing`(리포지토리의 코드 인덱스 tier 실패)을 제외한 어떤 문제도 오류가 아닐 때 `status: ok`를 반환합니다. `fake_embedding`, `fake_llm`, `providers_degraded`는 현재 어떤 모드인지 알려 주고, `problems`는 각 문제와 해결 방법을 나열하며, `schema.pending`은 적용되지 않은 마이그레이션을 나열합니다.
- `loom doctor`는 서버 측 점검마다 한 줄씩 출력합니다. `--json`을 쓰면 JSON으로 출력합니다. 소스 체크아웃 스택에서는 `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`를 실행하세요.

<a id="2-open-the-dashboard"></a>

## 2. 대시보드 열기

`http://localhost:7777/`은 `single-user` 모드에서 대시보드로 바로 열립니다.
multi-user 모드에서는 먼저 가입하거나 로그인해야 합니다. `single-user` 모드에서는
대시보드와 에이전트 기본값이 `dev/local`을 사용합니다(`LOOM_DEFAULT_NAMESPACE`로 네임스페이스 이름을
바꿀 수 있습니다). 이 모드는 편의를 위한 모드이며 사용자 격리 경계가 아닙니다. 서버에 접근할 수 있는 사람은 누구나
서버의 데이터와 관리 기능에 접근할 수 있습니다. localhost에서만 사용하세요.

대신 가입 기능을 사용해 보려면(`trust-network`: 이메일과 비밀번호를 사용하며, 네임스페이스 이름을 처음 선점한 사람이 해당 네임스페이스의 admin이 됩니다), `~/.loom/.env`(릴리스 스택) 또는 `.env`(체크아웃)의 다음 항목을 교체하세요.

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

`single-user`를 명시적으로 선택한 상태에서 `LOOM_AUTH_REQUIRED=true`만 설정하는 것으로는 충분하지 않습니다.
이 모드는 인증을 꺼 둔 상태로 유지합니다. 설치 프로그램이 생성한
`LOOM_JWT_SECRET`과 기존 볼륨은 그대로 두고, 서버를 다시 생성하여 환경 변수를 불러오세요.

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

이후 대시보드는 **Sign up** 탭이 있는 로그인 페이지로 열리며, 모든 SDK 및 MCP 호출에는 토큰이 필요합니다. identity 모드는 [Identities and
namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes)에서 확인할 수 있습니다. `trust-network`를 지정하면 loopback이 아닌 `LOOM_BIND_HOST`도 가입이 열린 상태로 시작할 수 있으므로, 해당 포트에 접근할 수 있는 사람은 누구나 가입할 수 있게 됩니다. 팀이 서버에 접근할 수 있게 하기 전에 운영자 접근에 대해서는 [배포 가이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)를, [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls)에 대해서는 해당 문서를 따르세요.

<a id="3-get-a-token"></a>

## 3. 토큰 발급받기

single-user 모드에서는 토큰이 선택 사항입니다. 그래도 토큰을 발급해 두면 나중에 인증을 켰을 때 스크립트가 계속 동작합니다. 단, 스크립트가 `org`(또는 빈 값), `namespace`, `agent`를 직접 지정해야 합니다. SDK처럼 자격 증명을 찾아서 함께 보내는 호출([Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials) 참고)은 이 세 값 중 어느 것도 자동으로 채워 주지 않으며, 빈 `org`는 토큰 자체의 값이 됩니다.

- **Dashboard:** **Settings → Tokens → New token**. 기본적으로 토큰은 사용자가 속한 모든 namespace와 repository를 읽을 수 있고(제한된 repository는 직접 추가한 경우에만), collection은 읽을 수 없으며, 현재 있는 namespace에 저장합니다. _Only what you choose_ 를 선택하면 체크한 namespace, collection, repository로만 범위가 제한됩니다(처음에는 아무것도 선택되어 있지 않습니다). 체크한 항목은 나열된 순서대로 검색되며, 순서는 각 행의 화살표로 바꾸거나 데스크톱에서는 행의 그립을 드래그해서 바꿀 수 있습니다. 결과 화면에는 토큰이 한 번만 표시되며, MCP 클라이언트나 스크립트용 설정도 함께 보여 줍니다.
- **운영자가 CLI로 발급:** 서명 시크릿을 가진 서버 컨테이너 안에서 실행합니다.

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  내 머신 밖에서 사용할 토큰이면 `--ttl-days 30`을 추가하세요. `loom mint-token --help`를 실행하면 범위 지정 옵션(`--namespace-role`, `--read-scope`, `--traffic-class`)을 볼 수 있습니다.

- **스스로 등록하는 bot** (trust-network 전용):
  `loom init --agent nightly-triage --server-url http://localhost:7777`을 실행하면 namespace `agent-nightly-triage`가 만들어지고, writer 토큰이 `~/.loom/token`에, URL이 `~/.loom/config.json`에 저장되며, claim code가 출력됩니다. 사람이 `/oauth/agent-claim`에서 자신의 비밀번호로 이 코드를 한 번 입력하면 소유권을 가져갈 수 있습니다.

SDK, CLI, 플러그인이 토큰을 찾는 위치에 저장하세요.

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

claim, role, grant에 관한 내용은 [Tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)를 참고하세요.

<a id="4-first-memory-over-http"></a>

## 4. HTTP로 첫 메모리 만들기

모든 operation은 JSON 본문을 담은 `POST /v1/<op>` 요청입니다. 설치 프로그램이나 체크아웃으로 설치한 기본 `.env`의 새 설치본은 단일 사용자 모드입니다. 토큰이 필요 없으며, `org`나 `namespace`를 지정하지 않은 일상적인 메모리·검색 호출(`memory.set`, `memory.get`, `memory.search` 등)은 설치본 자체의 org `dev`, namespace `local`에서 실행됩니다. MCP와 대시보드도 같은 값을 사용합니다. `agent`를 지정하지 않은 `memory.set`은 agent `http`로 기록되고, 검색은 모든 agent를 읽습니다. 따라서 아래 호출에는 각 호출 고유의 필드만 있으면 됩니다([해당하는 op](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)). 토큰 없이 호출했을 때 401 `AUTH_INVALID`가 반환되면 인증이 켜져 있는 것입니다. 이 경우 `-H "Authorization: Bearer $LOOM_API_KEY"`를 추가하고 계정의 `org`, `namespace`, `agent`를 지정하세요. 토큰이 비어 있는 헤더는 거부됩니다.

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.get -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window"}' | jq '{found, value}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"query":"when can we deploy?"}' | jq '.results[0].content_excerpt'
```

`memory.set` 응답에는 `memory_id`, `content_sha256`, `status`가 들어 있습니다.
`memory.get`은 키 하나를 다시 읽어 옵니다. `found`는 해당 키에 유효한 값이 있는지를 나타내고, `value`는 기록된 그대로의 값입니다. 검색 응답에는 `results`와 `abstain`이 들어 있으며, 후자는 가장 좋은 결과가 그것을 근거로 답할 만큼 충분히 강한지를 나타냅니다. 검색은 대시보드에서 추가한 메모리를 포함해 namespace 안의 모든 agent의 메모리를 읽습니다. 특정 agent의 메모리만 읽으려면 `agent`를 지정하세요.
`GET /v1/operations`는 모든 operation을 나열하고, <http://localhost:7777/docs>에서는 브라우저로 각 operation을 직접 시험해 볼 수 있습니다. 요청·응답 형식은 [HTTP API 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)에 있습니다.

<a id="5-first-memory-from-python"></a>

## 5. Python으로 첫 메모리 만들기

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

아직 PyPI에 올라오지 않은 릴리스는 해당 릴리스의 asset으로 설치합니다
([GitHub Release에서 설치하기](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)).

이 예제는 `org`와 `namespace`를 명시하므로 3단계에서 만든 토큰이 있든 없든 동작합니다. SDK는 토큰을 찾으면 함께 전송합니다. 단일 사용자 서버에서는 토큰 없이 호출할 때 이 둘을 생략해도 됩니다. `Agent(agent="quickstart", base_url=...)`도 `dev`/`local`에서 실행됩니다.

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
[Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)를 참고하세요.
TypeScript SDK는 `npm install @clickhouse/loom`으로 설치합니다
([clone에서 설치하기](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)).

클라이언트 인터페이스는 [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)와
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) 레퍼런스에서 확인할 수 있습니다.

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. 코딩 에이전트로 첫 메모리 만들기

Claude Code를 사용한다면 Loom 플러그인을 설치하세요. 플러그인은 MCP 도구 위에 hook(세션 시작 시 컨텍스트 로드, 종료 시 캡처)과 skill을 추가합니다.

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

이 머신의 단일 사용자 서버를 사용한다면, Claude Code가 플러그인 설정을 물을 때 기본 서버 URL을 그대로 두고 토큰은 비워 두세요.

플러그인 없이 MCP 항목을 직접 추가할 수도 있습니다.

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

두 방법 모두 별도의 토큰 없이 이 머신의 단일 사용자 서버에 연결됩니다.

인증을 켠 경우 **Settings → Tokens**에서 토큰을 만들고 **Connect** 아래의 **Claude Code**를 선택하세요. 대화상자에 플러그인용 설정과 `claude mcp add` 항목용 설정이 토큰이 포함된 형태로 표시됩니다. 그런 다음 다음을 진행합니다.

1. `~/.loom/config.json`을 대화상자의 `config.json` 블록과 토큰이 저장될 네임스페이스(`"namespace": "<namespace>"`)로 덮어씁니다. 단일 사용자 세션이 파일에 남겨 둔 `org` 또는 `namespace`는 토큰보다 우선하며, 이 값이 없으면 플러그인은 `*`가 아닌 토큰의 첫 번째 네임스페이스 또는 `coding`에 기록합니다. 이전에 설정했던 `code_repo`나 `agent`가 있다면 다시 추가하세요.
2. `LOOM_API_KEY`(3단계에서 export했을 수 있으니 `unset LOOM_API_KEY`를 실행하세요)와 플러그인의 `token` 옵션은 비워 두거나 새 토큰으로 설정하세요. 플러그인은 해당 파일보다 이 둘을 먼저 읽습니다
   ([configuration](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)).
3. 토큰 없이 만든 `claude mcp add` 항목은 삭제한 뒤(`claude mcp remove loom -s user`) 대화상자의 명령을 실행하세요. 이 명령은 이미 존재하는 이름을 거부합니다.

`loom install --client <name>`은 Claude Desktop, Claude Code, Codex, Cursor, VS Code, Cline, Windsurf, Zed용 MCP 항목을 작성합니다. 플래그를 생략하면 찾은 모든 클라이언트에 연결합니다. Codex와 Cursor는 [코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)에서 다룹니다.

새 세션을 시작하고 _"나는 금요일에는 절대 배포하지 않는다는 점을 기억해."_ 라고 말해 보세요. 에이전트가 `loom_memory_set`을 호출합니다. 새 세션에서 _"언제 배포할 수 있어?"_ 라고 물으면 에이전트가 `loom_search`를 호출해 저장된 사실을 바탕으로 답합니다. 모델은 도구 설명을 보고 저장과 검색 시점을 판단하며, 별도의 트리거 단어는 없습니다.

<a id="7-see-it-in-the-dashboard"></a>

## 7. 대시보드에서 확인하기

| 페이지            | 표시 내용                                                  |
| ----------------- | ---------------------------------------------------------- |
| `/memories`       | 네임스페이스의 메모리와 작성자, 작성 시간.                 |
| `/admin/activity` | 감사 로그: 어떤 에이전트가 어떤 키를 언제 작성했는지.      |
| `/admin/usage`    | 작업별, 에이전트별 비용.                                   |

나머지 내용은 [대시보드](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)에서 확인할 수 있습니다.

<a id="optional-index-a-repository-on-the-released-stack"></a>

## 선택 사항: 릴리스된 스택에서 저장소 인덱싱하기

이 스택에서는 대시보드에서 저장소를 추가하면 `SANDBOX_UNAVAILABLE` 오류와 함께 거부됩니다.
저장소 추가에는 [repository-job sandbox](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox)가 필요한데, Docker의 기본 격리 설정이 이를 막기 때문에 이 스택은 sandbox를 꺼 둡니다. 신뢰할 수 있는 저장소만 등록하세요. 해당 저장소의 빌드 단계(TypeScript 설치, Go 모듈 다운로드, CMake)는 sandbox 없이 `loom` 컨테이너에서 실행되며, 이 컨테이너의 환경에는 JWT 시크릿과 provider 키가 들어 있습니다.

`LOOM_CODE_REPOS_JSON`은 모든 저장소를 JSON 배열로 담은 한 줄입니다
([항목 설정](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)).
`~/.loom/.env`에서 이 줄의 주석을 해제하고(없으면 추가하고) 수정하세요.
`dev`는 단일 사용자 스택이 실행되는 org이자, 다중 사용자 계정이 가입하는 org(`LOOM_SELF_SIGNUP_ORG`)입니다. 이 값을 바꾸었다면 여기와 아래 `--org`에 계정의 org를 사용하세요.

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom은 저장소의 빌드 파일(`go.mod`, `package.json`, `pyproject.toml`, 루트의 `CMakeLists.txt` 등)을 보고 compiler tier 인덱서를 고릅니다. 대신 `scip_producer`로 인덱서를 직접 지정할 수도 있습니다([Compiler tier](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)).
  C/C++는 설정된 빌드 트리도 필요하므로, 빌드 트리가 없으면 C/C++ 저장소에는 parser tier만 적용됩니다.
- 비공개 저장소라면 GitHub 토큰(`.env`의 `LOOM_CODE_GITHUB_TOKEN` 또는 항목의 `github_token`)을 추가하고 `"public":true`를 제거하세요. 이 옵션은 호스트에서 공개된 저장소용이며, org의 모든 사용자가 **Repositories**에서 즉시 reader로 인덱스에 참여할 수 있게 합니다. 첫 `loom code init` 전에 결정하세요. 이 명령은 `public` 값으로 namespace의 참여 정책을 설정하며, 이후 `public`을 수정해도 정책은 바뀌지 않습니다(maintainer는 `code.repo.set_access`로 변경합니다).

그런 다음 `loom` 컨테이너를 다시 생성하고 첫 인덱싱을 실행합니다.

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed`는 릴리스된 스택의 로컬 embedder에서 실행됩니다. fake embedder를 사용하는 경우(체크아웃한 소스의 `.env`이거나, 여전히 `fake-ngram-*`을 지정한 업그레이드된 설치) 이 명령은 거부하고 종료합니다. 그래도 구성을 시험해 보려면 `--allow-fake-embedder`를 추가하세요. 나중에 **Settings → Models & keys**에서 모델을 선택하면 코드 인덱스가 다시 임베딩됩니다. `.env`를 변경한 경우에는 별도로 다시 임베딩해야 하며, 방법은 [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)에 설명되어 있습니다.

다중 사용자 모드에서는 목록에 등록한 저장소에 maintainer가 없으므로, `"public":true`가 없으면 reader의 참여 요청을 승인할 사람이 없어 대기 상태로 남습니다. `loom code init` 후에 operator 권한으로, 컨테이너에서 발급한 토큰을 사용해 누군가에게 접근 권한을 부여하세요. `admin`은 해당 사용자를 maintainer로 만들어 이후 reader를 승인할 수 있게 하고, `viewer`는 읽기만 허용합니다. subject는 계정이 가입할 때 사용한 namespace(로그인 핸들, 즉 해당 계정 토큰의 subject)입니다. 자체 가입 이메일은 검증되지 않으므로 이메일에 부여한 권한으로는 아무도 접근할 수 없습니다.

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

`loom demo`는 이 저장소의 `AGENTS.md`를 업로드하고, `knowledge.ingest`로 수집한 뒤(스택의 reference extractor가 파싱합니다), `knowledge.synthesize`로 문서에 대한 질문을 던져
답변과 답변이 인용한 chunk를 출력합니다. fake LLM을 사용하면 대신 상위 `knowledge.search` 결과를 출력합니다. `--file`과 `--question`으로 직접 가진 문서와 질문을 지정할 수 있습니다.

<a id="common-problems"></a>

## 자주 발생하는 문제

| 증상                                              | 원인과 해결 방법                                                                                                                                                                               |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages`가 fact를 쓰지 않음            | LLM이 가짜(fake) 모델입니다. [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)를 따라 LLM을 설정하세요.                                                                                                  |
| 검색 결과가 제멋대로임                            | embedder가 가짜입니다(`.env`의 `fake-ngram-*`). [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)에 안내된 대로 바꾸면 저장된 데이터가 다시 임베딩됩니다. 모델을 바꾸려고 볼륨을 삭제하지 마세요. |
| 인증을 켠 뒤 401 `AUTH_INVALID`                   | 토큰이 없거나 다른 `LOOM_JWT_SECRET`으로 서명된 토큰입니다. [Get a token](#3-get-a-token)의 Compose 명령으로 토큰을 다시 발급하세요.                                                            |
| 대시보드가 503을 반환함                           | 서버를 소스에서(`make server`) 실행했는데 빌드된 프런트엔드가 없습니다. `make frontend-build`를 실행하세요.                                                                                     |
| 릴리스 스택이 healthy 상태가 되지 않음            | `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`를 실행하세요. [Troubleshooting](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)도 참고하세요.                                  |
| 저장소를 추가하면 `SANDBOX_UNAVAILABLE`이 반환됨  | 스택이 저장소 작업용 샌드박스 없이 실행 중입니다. `LOOM_CODE_REPOS_JSON`으로 저장소를 인덱싱하세요([Index a repository](#optional-index-a-repository-on-the-released-stack)).                    |
| 설치 프로그램이 포트 `7777`이 사용 중이라고 함    | 다른 프로그램이나 다른 Loom이 해당 포트에서 대기하고 있습니다. 해당 프로그램을 중지하거나, 설치 명령을 다시 실행할 때 `sh loom-install.sh` 앞에 `LOOM_PORT=7778`을 붙이세요. `.env`에 포트가 유지됩니다. |
| 설치 프로그램이 포트 `7777`이 응답하지 않는다고 함 | 어떤 프로그램이 응답 없이 포트를 점유하고 있거나, `LOOM_BIND_HOST`, `LOOM_PORT` 또는 방화벽 때문에 이 호스트에서 해당 포트에 접근할 수 없습니다.                                                    |
| 설치 프로그램이 포트 `7777`을 확인하지 못함       | curl이 종료 코드가 나타내는 이유로 실패했습니다. `LOOM_BIND_HOST`와 `LOOM_PORT`를 확인하세요.                                                                                                  |
| clone한 저장소에서 포트 `8123` 또는 `9000`이 사용 중임 | ClickHouse 호스트 포트를 다시 매핑하세요([local stack](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)).                                                                                  |
| 설치 프로그램이 이미지를 pull하지 못함            | Docker가 레지스트리에 직접 접근할 수 있어야 합니다. 프록시 환경에서는 Docker 데몬에 [프록시 설정](https://docs.docker.com/engine/daemon/proxy/)을 지정하세요.                                    |

<a id="next"></a>

## 다음 단계

- [Evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md): 이 스모크 테스트를 워크로드별 품질, 지연 시간, 비용 평가로 확장합니다.
- [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md): 이 스택에 Claude Code, Codex 또는 Cursor를 연결합니다.
- [Your first agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): SDK를 사용한 ID, abstain 처리, 세션, 컨텍스트 팩, 실행을 다룹니다.
- [Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): 팀을 위해 Loom을 운영합니다.
