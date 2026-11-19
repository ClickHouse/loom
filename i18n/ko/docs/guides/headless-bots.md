<!-- i18n: source=docs/guides/headless-bots.md blob=7e255a8b2f49ab5ea38bb7947e4bde837a8a6323 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md), which is authoritative.

<a id="headless-bots"></a>

# 헤드리스 봇

헤드리스 봇은 토큰을 붙여 넣어 줄 사람도, 조용히 발생한 실패를 알아차릴 사람도, "이걸 기억해 줘"라고 말할 사람도 없는 상태로 실행됩니다. 이 가이드는 봇의 ID, 연결 확인, 세 가지 런타임, 봇이 기록하는 내용, 그리고 응답이 없어진 봇을 진단하는 방법을 다룹니다.

<a id="1-give-the-bot-an-identity"></a>

## 1. 봇에 ID 부여하기

`loom` CLI는 서버 이미지에 포함되어 있으며, 저장소를 클론한 뒤 `pip install -e .`로 설치합니다.
SDK 패키지(`clickhouse-loom`)에는 CLI가 없습니다.

**셀프 가입을 사용하는 경우.** `LOOM_SELF_SIGNUP=open`으로 설정된 서버에서는 봇이 스스로 가입할 수 있습니다.

```bash
loom init --agent nightly-triage --server-url https://<loom> --purpose "Files the nightly failures."
```

`loom init`은 다음을 수행합니다.

1. 네임스페이스 `agent-nightly-triage`를 생성합니다(`--purpose`가 네임스페이스 설명이 됩니다).
2. 서비스 계정 주체에 해당 네임스페이스의 `writer` 권한을 부여합니다.
3. 토큰을 `~/.loom/token`(모드 0600)에 쓰고, URL, 네임스페이스, 조직을
   `~/.loom/config.json`에 씁니다(`--token-path`와 `--config-path`로 위치를 바꿀 수 있습니다).
4. 클레임 코드와 클레임 페이지 주소(`/oauth/agent-claim`)를 출력합니다. 사람이 그
   페이지에서 코드를 한 번 입력하고 자신의 비밀번호를 설정하면, 이후에는 네임스페이스 이름과 그 비밀번호로 해당 네임스페이스의 관리자로 로그인합니다. 코드로는 로그인할 수 없으며, 같은 코드로 두 번째 클레임을 시도하면 거부됩니다. 봇의 토큰은 계속 사용할 수 있습니다.

`--ttl-days`는 토큰 유효 기간을 설정합니다(기본값 365). `--org`는 서버의 셀프 가입 조직이 아닌 다른 조직을 지정합니다.

**CI에서는** 파일을 쓰는 대신 토큰을 출력하여 시크릿으로 저장합니다. `--no-files`를 사용하면 토큰이 출력의 첫 번째 줄에 나옵니다.

```bash
loom init --agent ci-reviewer --server-url https://<loom> --no-files | head -n 1 > token.txt
```

`--json`을 사용하면 가입 결과가 토큰 없이 JSON으로 출력됩니다. `--json --no-files`를 함께 사용하면 토큰이 그 JSON 안에 포함되며, 토큰이 출력되는 곳은 그 JSON뿐입니다.

**셀프 가입을 사용하지 않는 경우**에는 운영자가 토큰을 발급하거나, 사람이 **Settings → Tokens**에서 봇의 네임스페이스만 읽을 수 있는 토큰을 생성합니다.

```bash
loom mint-token --subject ci-reviewer --org <org> --namespaces agent-ci --roles writer \
  --service-account --ttl-days 30 --save
```

역할, 유효 기간, 교체에 대해서는 [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)를 참고하세요.

<a id="2-check-the-connection-before-the-first-run"></a>

## 2. 처음 실행하기 전에 연결 확인하기

```bash
LOOM_BASE_URL=https://<loom> LOOM_API_KEY="$(cat token.txt)" loom doctor --client
```

`loom doctor --client`는 플러그인 훅과 SDK가 하는 방식과 같게 서버와 토큰을 확인하고, 각각 어디에서 가져왔는지 알려 줍니다. `/v1/health`와 `namespace.list_mine`을 호출해 보고, 토큰이 봇의 네임스페이스에 쓸 수 있는지도 확인합니다. 문제가 있으면 해결 방법을 알려 주는 `FAIL` 줄로 표시되고 종료 코드는 0이 아니므로, 이 명령은 CI 게이트로 사용할 수 있습니다.

```yaml
- run: loom doctor --client --server-url ${{ vars.LOOM_BASE_URL }} --token ${{ secrets.LOOM_API_KEY }}
```

`--json`은 검사 결과와 문제 목록을 JSON으로 출력하므로, 이를 파싱하는 파이프라인에서 사용할 수 있습니다.

<a id="3-connect-the-runtime"></a>

## 3. 런타임 연결

<a id="claude-agent-sdk-or-claude--p"></a>

### Claude Agent SDK 또는 `claude -p`

파일을 통해 Loom을 MCP 서버로 마운트하고 해당 도구만 허용합니다. 헤드리스 실행에는 대화형 OAuth가 없으므로 bearer 토큰을 헤더에 실어 보냅니다.

```bash
cat > loom.mcp.json <<EOF
{"mcpServers": {"loom": {"type": "http",
  "url": "https://<loom>/mcp?toolset=coding",
  "headers": {"Authorization": "Bearer $LOOM_API_KEY"}}}}
EOF
claude -p --mcp-config loom.mcp.json --strict-mcp-config \
  --allowedTools "mcp__loom__*" \
  --output-format stream-json --verbose \
  "Triage the failures in the last nightly run; record each root cause you find."
```

| 플래그                          | 이유                                                                                                 |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `--strict-mcp-config`           | 사용자의 설정이 아니라 파일에 명시된 서버만 봇이 사용하도록 합니다.                                  |
| `--allowedTools "mcp__loom__*"` | 모든 Loom 도구를 사전 승인하여 권한 확인 프롬프트가 실행을 막지 않도록 합니다.                       |
| `--output-format stream-json`   | 첫 번째 메시지는 세션의 `system/init`입니다(아래 참고). `claude -p`에서는 이 옵션과 함께 `--verbose`가 필요합니다. |
| `--bare`                        | 사용자의 훅, 플러그인, `CLAUDE.md`를 건너뛰어 동일한 동작을 보장합니다.                              |

> **경고:** 연결 상태를 기준으로 실행을 확인하세요. `system/init` 메시지는 `mcp_servers[]`를 나열하며 각 항목에 `status`가 있습니다. `loom`이 `connected`일 때만 실행을 신뢰하세요. 그렇지 않으면 모델은 메모리 없이 실행된 것입니다.

도구 목록은 `?toolset=`으로 선택합니다. 사용 가능한 툴셋은 [MCP 툴셋](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)에 나와 있습니다.

`claude -p`를 실행하는 사용자에게 Loom 플러그인이 설치되어 있으면 해당 훅도 함께 실행됩니다
([Claude Code 플러그인](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)). 플러그인의 멘토 힌트는 플러그인 자체 서버의 도구(`mcp__plugin_loom_loom__*`)만 따르며, `--mcp-config`로 지정한 `loom` 항목은 따르지 않습니다.

<a id="codex-codex-exec"></a>

### Codex `codex exec`

```bash
export LOOM_API_KEY="$(cat token.txt)"
codex exec --json \
  -c 'mcp_servers.loom.url="https://<loom>/mcp?toolset=coding"' \
  -c 'mcp_servers.loom.bearer_token_env_var="LOOM_API_KEY"' \
  "Summarise what we decided about retries; record any decision that is missing."
```

Codex는 `codex`가 시작되는 환경에서, 항목에 지정된 변수로부터 bearer 토큰을 읽습니다. Codex 플러그인이 설치되어 있으면 해당 훅도 여기서 실행됩니다
([Codex 플러그인](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)).

<a id="the-sdk-in-the-bots-own-process"></a>

### 봇의 자체 프로세스에서 사용하는 SDK

코딩 에이전트가 아니라 코드로 동작하는 봇이라면 MCP를 건너뛰고 오퍼레이션을 직접 호출합니다.

```python
from loom import Agent

async with Agent(org="acme", namespace="agent-nightly-triage", agent="nightly-triage") as a:
    hits = await a.memory.search("flaky tests in the ingest suite", top_k=5)
    await a.memory.set(
        "flaky/ingest_retry_test",
        "Times out against the S3 mock.",
        tags=["failure", "ingest"],
    )
    ans = await a.memory.ask("which suites flaked this week?")
```

`Agent`는 서버를 `base_url=` 또는 `LOOM_BASE_URL`에서, 토큰을 `api_key=`, `LOOM_API_KEY`, `~/.loom/token` 순서로 읽습니다. 그 밖의 진입점은 다음과 같습니다.

- `agent.tools()`는 Loom의 동사(`loom_remember`, `loom_recall`, `loom_ask`, `loom_offload`, `loom_fetch`, `loom_note`)를 하나의 `execute()`와 함께 도구 정의로 모델에 제공합니다.
- `agent.session(id)`는 에이전트 프레임워크가 보관하는 단위로, 재개, 추가, 플러시, 종료를 지원합니다.
- LangGraph 그래프는 `LoomStore`(`loom.integrations.langgraph`)를 `compile(store=...)`에 전달하고, Strands 에이전트는 `LoomMemoryStore`(`loom.integrations.strands`)를 사용합니다.
- [`examples/claude_agent_loop.py`](https://github.com/ClickHouse/loom/blob/main/examples/claude_agent_loop.py)는 Anthropic SDK를 사용하는 최소한의 루프입니다(`ANTHROPIC_API_KEY` 필요).

[Python SDK 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)와
[프레임워크 통합](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)을 참고하세요.

<a id="4-decide-what-the-bot-writes"></a>

## 4. 봇이 무엇을 기록할지 결정하기

플러그인은 코딩 세션을 자동으로 캡처합니다. 플러그인 바깥의 봇은 무엇을 기록할지 스스로 결정합니다.
요약하면 다음과 같습니다. 대화 내용 전체가 아니라 결정과 실패를 기록하고, 단계마다 한 번이 아니라 작업 전에 한 번만 회상하며, 완료된 실행의 대화 내용은 `memory.set_from_messages`에 넘기고, 실행이 참고한 항목은 `run.complete(memories_used=[...])`로 보고합니다.
각 선택에 대한 자세한 내용은 [에이전트 통합](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#3-decide-what-to-write)에서 다룹니다.

<a id="5-when-the-bot-is-quiet"></a>

## 5. 봇이 응답하지 않을 때

봇이 실행되는 머신에서 `loom doctor --client`를 실행하세요. 일반적인 원인을 각각 한 줄씩 보고합니다.

| 증상                                  | 원인                                                                                                |
| ------------------------------------- | --------------------------------------------------------------------------------------------------- |
| 읽기와 쓰기가 다른 서버로 전송됨      | `LOOM_BASE_URL`이 `~/.loom/config.json`과 일치하지 않음                                             |
| 모든 호출에서 401 발생                | 토큰이 만료되었거나 교체됨                                                                          |
| 모든 쓰기에서 403 발생                | 봇이 쓰는 네임스페이스에서 해당 토큰이 `viewer` 권한임                                              |
| 캡처가 나타나지 않음                  | 백그라운드 flusher가 캡처를 전달하지 못함: 임시 디렉터리에 `loom-flush-*.json` 파일이 남아 있음     |

<a id="related"></a>

## 관련 문서

- [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)
- [에이전트 통합](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)
- [MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- [요청 디버깅](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
