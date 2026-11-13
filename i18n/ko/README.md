<!-- i18n: source=README.md blob=36077b9f40bb26ba178b2e6394cef8521b563f48 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/README.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/README.md), which is authoritative.

<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>AI 에이전트를 위한 공유 메모리, 문서 검색, 코드 인텔리전스.</b><br>
  Claude Code, Codex, Cursor, 그리고 직접 만든 에이전트를 위한 도구입니다.
</p>

<p align="center">
  <a href="https://pypi.org/project/clickhouse-loom/"><img alt="PyPI의 Python SDK" src="https://img.shields.io/pypi/v/clickhouse-loom?label=python%20sdk"></a>
  <a href="https://www.npmjs.com/package/@clickhouse/loom"><img alt="npm의 TypeScript SDK" src="https://img.shields.io/npm/v/%40clickhouse%2Floom?label=typescript%20sdk"></a>
  <a href="https://github.com/ClickHouse/loom/blob/main/LICENSE"><img alt="라이선스: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://glama.ai/mcp/servers/ClickHouse/loom"><img alt="MCP 서버 점수" src="https://glama.ai/mcp/servers/ClickHouse/loom/badges/score.svg"></a>
</p>

<p align="center">
  <!-- languages -->
  <a href="../../README.md">English</a> · <a href="../zh-CN/README.md">简体中文</a> · <a href="../ja/README.md">日本語</a> · 한국어 · <a href="../pt-BR/README.md">Português (Brasil)</a> · <a href="../es/README.md">Español</a> · <a href="../ru/README.md">Русский</a>
  <!-- /languages -->
</p>

<p align="center">
  <a href="#what-loom-is-for">Loom의 용도</a> ·
  <a href="#measured-results">벤치마크</a> ·
  <a href="#get-started">시작하기</a> ·
  <a href="#why-loom">기능</a> ·
  <a href="#integrations">통합</a> ·
  <a href="https://clickhouse.github.io/loom/">문서</a>
</p>

![공유된 에이전트 메모리, 고정된 사실, 충돌로 표시된 항목을 보여 주는 Loom 대시보드](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-memories.png)

Loom은 에이전트에 장기 공유 메모리를 제공하는 오픈 소스 셀프 호스팅 MCP 서버입니다. 한 에이전트가 세션에서 배운 내용을 다음 에이전트나 도구가 찾아 쓸 수 있습니다.

에이전트는 MCP, HTTP, Python, TypeScript로 연결합니다. Docker로 셀프 호스팅하거나 [Helm 차트를 사용해 Kubernetes에](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) 배포할 수 있습니다. 에이전트 컨텍스트와 인덱스 데이터는 [ClickHouse](docs/use-cases/self-hosted-clickhouse.md)에 저장되며, 배포 시 라이브 메모리와 지식 상태용으로 [최초 부팅 시 Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)를 선택할 수도 있습니다. 대시보드에서는 메모리, 리포지토리, 작업, 실행을 한곳에서 볼 수 있습니다.

<a id="what-loom-is-for"></a>

## Loom의 용도

- **코딩 에이전트 간에 공유되는 장기 메모리**: 세션 시작 시 컨텍스트를 불러오고 세션이 진행되는 동안 내용을 캡처하는 플러그인을 제공합니다.
  [에이전트 메모리](docs/use-cases/agent-memory.md) ·
  [코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **에이전트 애플리케이션용 메모리**: Python 또는 TypeScript SDK로 사용자별 메모리를 제공합니다. [첫 에이전트 만들기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)
- **멀티 사용자 MCP 메모리 서버**: 네임스페이스, 역할, 토큰을 지원합니다.
  [멀티 사용자 모드](docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [네임스페이스와 공유](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
- **저장소 코드 인덱스**: 코딩 에이전트가 정의, 호출자, 변경 영향도를 조회할 수 있습니다. [코드 인덱스](docs/use-cases/code-intelligence.md)
- **코딩 에이전트의 모델 비용 절감**: 아래의 [측정 결과](#measured-results)를 참고하세요.
  [코딩 에이전트 비용](docs/use-cases/coding-agent-cost.md)
- **인용을 포함한 문서 검색(RAG)**: extractor 서비스가 변환하는 PDF, `.docx`, `.pptx` 파일도 지원합니다. [문서 검색](docs/use-cases/document-search.md)
- **멀티 에이전트 워크플로를 위한 공유 컨텍스트**: 공유 메모리, 작업 인박스, 결정 사항을 제공합니다. [멀티 에이전트](docs/use-cases/multi-agent.md)
- **자율 에이전트를 위한 메모리, 회상, 감사, 삭제.**
  [자율 에이전트](docs/use-cases/autonomous-agents.md)
- **에이전트 작업 기록**: 검색 가능한 대화(녹화가 켜져 있을 때), 이어서 작업할 수 있는 세션 브리프, 목표·결과·모델 비용(제공된 경우)이 담긴 실행 기록, PR 및 트래커 이력을 제공합니다.
  [실행 및 PR 이력](docs/use-cases/runs-and-pr-history.md)

모든 사용 사례와 다른 도구가 더 적합한 경우는 [사용 사례](docs/use-cases/index.md)에서 확인하세요.

Loom은 단일 에이전트의 단기 메모리를 위한 인프로세스 라이브러리가 아니며, 에이전트 프레임워크도 아닙니다.
[Loom이 맞지 않는 경우](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)

<a id="measured-results"></a>

## 측정 결과

| 평가                       | Loom 사용 | Loom 미사용 | 효율 향상                         | 범위                                |
| -------------------------- | --------- | ----------- | --------------------------------- | ----------------------------------- |
| 코딩 에이전트 답변 품질    | **0.84**  | 0.78        | 모델 비용 **20% 절감**            | 질문 24개 × 3회 짝지은 실행         |
| 코딩 에이전트 파일 위치 특정 | **96.4%** | 91.4%       | 모델 비용 **21% 절감**            | 질문 36개 × 10회 짝지은 실행        |
| 메모리 vs 전체 기록 답변   | **94%**   | 90%         | 리더 프롬프트 토큰 **91% 감소**   | 짝지은 LongMemEval-S 질문 100개     |

두 코딩 설정 모두 ClickHouse 코드베이스에서 Claude Code(`claude-sonnet-5`)를 사용했습니다.
비용은 Loom의 서버 측 모델 호출, 호스팅, 인덱싱을 제외한 세션 전체의 모델 사용 비용입니다. 답변 품질은
검증된 답변의 핵심 요점을 얼마나 포함하는지를 측정하며, 모순이 있으면 감점합니다.
파일 위치 특정은 에이전트가 답변 끝에 지목하도록 요청받은 최대 세 개의 파일 중에 올바른 파일이 포함되어 있는지를 집계합니다.

메모리 행은 동일한 `gpt-5` 리더를 사용해 검색된 메모리와 전체 대화 기록을 비교합니다.
토큰 절감량에는 메모리 구축 비용이 포함되지 않으며, 정확도 94%와 90%의 차이는 기술적(descriptive) 수치일 뿐 통계적으로 입증된 것은 아닙니다.

**별도로, Loom의 `memory.ask` 구성은 LongMemEval-S 전체 500개 질문에서 답변 정확도 96.6%를 기록했습니다.**
이는 위의 고정 리더 비교와 달리, 자체적으로 구성된 답변 모델을 사용한 제품 평가입니다.

결과는 테스트한 워크로드에 한정됩니다.

[프로토콜 및 재현 가능한 아티팩트](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[상세 결과](#benchmark-details) ·
[내 워크로드에서 평가하기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)

<a id="get-started"></a>

## 시작하기

<a id="1-choose-your-docker-setup"></a>

### 1. Docker 설정 선택

- **개인용(기본값):** 로그인 없음, localhost에서만 접근 가능, `dev/local`
  네임스페이스를 사용합니다. 한 사람이 여러 에이전트를 연결할 수 있습니다.
- **팀용:** 인증된 계정과 네임스페이스 접근 제어를 사용합니다.
  [첫 부팅부터 멀티 유저 모드로 시작하기](docs/getting-started/quickstart.md#start-in-multi-user-mode).

두 모드 모두 구성된 LLM과 임베딩을 포함해 동일한 Loom 기능을 지원합니다.
싱글 유저 모드는 기능이 축소된 에디션이 아닙니다. 데이터를 삭제하지 않고
[기존 설치를 다른 모드로 전환](docs/getting-started/quickstart.md#2-open-the-dashboard)할 수도 있으며,
새 계정에 기존 네임스페이스에 대한 접근 권한을 부여할 수 있습니다.

아무것도 설치하지 않고 사용해 보려면 codespace에서 여세요.

[![GitHub Codespaces에서 열기](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
([실행되는 내용과 접근 가능한 대상](docs/getting-started/quickstart.md#try-it-in-github-codespaces))

기본 개인용 설치는 Docker가 설치되어 있어야 합니다(Windows에서는 `docker`가 동작하는 WSL 2
터미널에서 실행합니다. WSL 통합을 켠 Docker Desktop이나 해당 배포판에 설치된 Docker Engine을 사용합니다).

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

홈 디렉터리 또는 본인만 쓸 수 있는 다른
[디렉터리](docs/getting-started/quickstart.md#fast-path-released-image-no-clone)에서 실행하세요.
설치 프로그램은 Loom과 ClickHouse를 시작하고, 마이그레이션을 적용하며, 대시보드 URL인
**http://localhost:7777/**을 출력합니다. 새로 설치한 경우에는 이어서 메모리가 저장되고 다시 읽히는지 확인합니다.
터미널에서는 발견한 코딩 에이전트(Claude Code, Codex,
VS Code)를 연결할지 묻고, 새로 설치한 경우 대시보드를 엽니다. 이미지에는 서버,
워커, 대시보드, Python·TypeScript·Go 컴파일러 인덱서가 포함되며, `amd64`에서는 C++ 인덱서도 포함됩니다.
TypeScript 의존성은 자동으로 준비됩니다. [빠른 시작 및 고정 릴리스](docs/getting-started/quickstart.md)

설치 위치는 `~/.loom`입니다(`LOOM_INSTALL_DIR`로 다른 디렉터리를 지정할 수 있습니다). 이곳에
Compose 파일과 `~/.loom/.env`의 시작 설정이 있습니다. 설치 명령을 다시 실행하면
제자리에서 업그레이드되며 `.env` 설정은 유지됩니다.

싱글 유저 모드는 localhost에서만 사용하세요. 공유 접근이 필요하면 TLS를 포함해
[팀 배포 가이드](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)를 따르세요.

<a id="2-configure-models-and-api-keys"></a>

### 2. 모델과 API 키 구성

**기본 설치는 키나 네트워크 없이도 부팅됩니다.** 스토리지, 대시보드,
구조적 코드 인덱싱, 시맨틱 검색이 동작하며, 이미지에 포함된 작은 로컬 임베딩 모델을 사용합니다.
fake LLM은 AI가 생성한 답변을 제공하지 않습니다.

| 구성 요소 | 실제 사용을 위한 구성 | 구성하지 않은 경우 |
| ---------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| 임베딩 | 프로바이더, 임베딩 모델, 차원, API 키. | `local`(CPU의 bge-small): 시맨틱 검색은 되지만 호스팅 모델보다 품질이 낮습니다. |
| LLM | 프로바이더, 모델, API 키. | `fake-echo-1`: 의미 있는 추출, 요약, 답변을 제공하지 않습니다. |

**Settings → Models & keys**를 열어 두 프로바이더와 모델을 선택하고
키를 입력한 다음 **Test & apply**를 클릭하세요. Docker를 재시작할 필요는 없으며, 임베딩
공간을 변경하면 기존 인덱스를 다시 빌드합니다. 대시보드의 임베딩 선택지는
호스팅 프로바이더입니다. `local`을 유지하면서 LLM을 추가하려면 대신 `~/.loom/.env`에서
LLM 항목을 설정하세요
([모델과 키](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#claude-for-the-llm)).

**OpenAI API 키 하나로 두 역할을 모두 처리할 수 있습니다.** Claude는 별도의 임베딩 프로바이더가 필요합니다.
OpenAI, Gemini, OpenAI 호환 임베딩 엔드포인트를 지원합니다. 에이전트의
CLI 로그인이나 구독은 Loom의 프로바이더 키를 대신하지 않습니다.

환경 변수로 구성하려면 `~/.loom/.env`를 수정한 다음 서버를 다시 생성하세요.

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

키만으로는 고정된 모델(로컬 임베더와 fake LLM)이 바뀌지 않으므로 프로바이더와
모델도 함께 설정하세요.
대시보드에 저장된 설정이 `.env`보다 우선합니다. 프로바이더별 구성 방법과 상태 확인은
[모델과 API 키](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)를 참고하세요.

**나머지 설정은 모두 한 파일에 있습니다.** [`loom.example.yaml`](https://github.com/ClickHouse/loom/blob/main/loom.example.yaml)에는
모든 설정이 기본값과 설명, 영역별로 나열되어 있습니다. ClickHouse 연결,
타임아웃과 예산, 속도 제한, 워커, 기능을 켜고 끄는 `*_enabled` 스위치 등이 있습니다. 각 설정은 `~/.loom/.env`에
`LOOM_<NAME>`으로 지정하고 서버를 다시 생성하면 적용됩니다. 설정 파일을 마운트하는 배포 환경에서는 이 파일의
사본에 설정할 수 있습니다
([설정을 읽는 방식](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#how-settings-are-read)). `live`로 표시된 설정은
Loom이 실행 중일 때도 변경할 수 있습니다. `loom settings set <name> <value>` 또는 Admin → Deployment
settings를 사용하세요. `loom settings list --changed`는 기본값과 다른 설정과
각 값의 출처를 보여 줍니다([런타임 설정](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)).

<a id="3-connect-your-agents"></a>

### 3. 에이전트 연결

설치 프로그램이 에이전트를 연결했다면 아래 명령은 건너뛰세요. 개인용 설치를 실행 중인 머신에서는
플러그인에 토큰이 필요하지 않습니다.

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

새 세션을 시작하면 첫 세션이 `http://localhost:7777`에 연결됩니다. Codex에서는
`codex`를 대화형으로 한 번 실행하고 플러그인의 훅을 승인하세요. 승인하기 전에는 도구만
동작합니다. 에이전트에게 기억해 둘 만한 내용을 알려 주고, 다음 세션에서 그에 대해 물어보세요.

개인용 설치를 실행 중인 머신의 Cursor에는 아래 버튼으로
[로컬 MCP 항목](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools)을 추가할 수 있으며, 토큰이 필요하지 않습니다.

[![Cursor에 Loom 추가](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

개인용 설치를 실행 중인 머신의 VS Code 에이전트 모드에는 아래 버튼으로
[로컬 MCP 항목](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server)을
추가할 수 있으며, 토큰이 필요하지 않습니다.

[![VS Code에 Loom 추가](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![VS Code Insiders에 Loom 추가](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

팀 서버의 경우 Claude Code에는 `--config server_url=… --config token=…`을 전달하거나,
[코딩 에이전트 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)에 설명된 대로
Codex가 서버를 가리키도록 설정하세요. 토큰은 **Settings → Tokens**에서 생성합니다. Cursor와 다른 MCP 클라이언트,
그리고 [SDK](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)도 해당 문서에서 다룹니다.

<a id="why-loom"></a>

## Loom을 사용하는 이유

- **이어지는 메모리.** 결정과 발견 사항을 에이전트와 세션 간에 공유합니다. 각 사실에는 작성자가 기록되고, 버전 이력과 충돌 검토를 통해 수정 내역을 추적할 수 있으며, 네임스페이스는 임의의 시점으로 롤백할 수 있습니다.
  [메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **근거를 갖춘 코드 인텔리전스.** 정의, 호출자, 변경 영향 범위, 관련 테스트를 찾고, 코드를 커밋, 풀 리퀘스트, 이슈와 연결합니다. 결과에는 리비전, 소스 위치, 완전성 정보가 함께 표시됩니다. [코드 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- **근거 기반 검색.** 접근이 허용된 메모리, 문서, 대화, 트래커 기록을 검색합니다. 답변에는 출처가 인용되며, 신뢰도가 낮은 검색은 답변을 보류할 수 있습니다.
  [검색](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- **멀티 에이전트 인수인계.** 임대(lease), 재시도, 검토 규칙, 결과를 갖춘 영속적인 작업 인박스를 통해 작업을 할당합니다. 워크플로 실행은 사용자의 에이전트가 맡고, Loom은 인수인계를 보존합니다. [작업 큐](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **에이전트 데이터에 대한 통제.** 네임스페이스 접근, 출처, 보존, 감사 기록, 검증된 삭제를 관리합니다. 컨텍스트는 ClickHouse에서 계속 쿼리할 수 있습니다.
  [보안](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

<a id="integrations"></a>

## 통합

| 통합                                                                                                                                                                                                                                                                                   | 연결 대상                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md), [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md), [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md) 및 [기타 MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) ([모든 통합](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)) | 기존 에이전트 워크플로에서 메모리와 코드 인텔리전스를 사용할 수 있습니다.                 |
| [Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md), [TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) 및 [HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)                                                                                                                                                         | 직접 만든 에이전트와 애플리케이션.                                             |
| [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)                                                                                                                                                                                                                                          | 요청 트레이스, 모델 비용, 검색 점수, 프롬프트 버전 귀속. |
| [ClickStack / HyperDX](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)                                                                                                                                                                                                                            | ClickHouse에 저장되는 OpenTelemetry 트레이스, 로그, 메트릭. |

Langfuse와 ClickStack은 선택적 통합이며, Docker 스택에 포함된 서비스가 아닙니다.

<a id="benchmark-details"></a>

## 벤치마크 상세

<a id="detailed-retrieval-reader-and-code-search-results"></a>

### 검색, 리더, 코드 검색 상세 결과

| 평가                                                  | 결과                                                                                               |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| LongMemEval-S 근거 검색 (500개 질문)                  | 처음 30개 메모리 안에 뒷받침하는 세션이 하나 이상 있는 질문이 **98.6%**.                           |
| LongMemEval-S 고정 `gpt-5` 리더 (500개 질문)          | 상위 200개 메모리 기준 답변 정확도 **92.0%**.                                                      |
| LoCoMo (전체 10개 대화)                               | 답변 정확도 **89.6%**; 적대적 질문에 대한 올바른 응답 거부 **90.8%**.                              |
| MemoryAgentBench 사실 질문 샘플 (500개 질문)          | 생성된 답변 기준 SubEM **99.0%**; 토큰 중복 F1 **95.7%**.                                          |
| 개념 기반 코드 검색 (36개 질문)                       | 정답 파일 재현율 5위 내 **91.2%** / 10위 내 **97.2%**; MRR **0.91**.                               |
| SWE-bench Verified 파일 위치 특정 (200개 이슈)        | 처음 10개 안에 변경된 파일이 있는 이슈: **82%** (BM25는 **57%**); 처음 5개 안에서는 **76.5%**.     |
| 트래커 중복 탐지 (104개 쌍)                           | 기록된 이전 중복 항목을 1위로 올린 비율 **87.5%**.                                                 |

이 수치들은 서로 다른 작업을 측정한 것이며, 하나의 종합 점수가 아닙니다. SWE-bench는 이슈 해결이 아니라 파일 위치 특정을 측정합니다. [결과 원장](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)에는 채점 정의, 소규모 샘플 평가, 부진하거나 부정적인 결과, 전체 프로토콜, 모델, 산출물이 포함되어 있습니다.

[방법론](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[적용 범위와 알려진 한계](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) ·
[벤치마크 실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)

<a id="documentation-and-contributing"></a>

## 문서 및 기여

공유 에이전트 컨텍스트, 코드 인텔리전스, 권한 기반 검색을 시작하려면
[문서](https://clickhouse.github.io/loom/)를 참고하세요. 배포 요구 사항은
[사전 요구 사항](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites)을 참고하세요.

[배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [자주 묻는 질문](https://github.com/ClickHouse/loom/blob/main/docs/faq.md) ·
[기여하기](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) · [보안 취약점 신고](https://github.com/ClickHouse/loom/blob/main/SECURITY.md) ·
[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)

Apache-2.0 · [라이선스](https://github.com/ClickHouse/loom/blob/main/LICENSE) · [고지 사항](https://github.com/ClickHouse/loom/blob/main/NOTICE)
