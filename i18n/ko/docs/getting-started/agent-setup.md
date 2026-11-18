<!-- i18n: source=docs/getting-started/agent-setup.md blob=eb3a5968c01a25b666c59b8907efe2f2be226078 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md), which is authoritative.

<a id="set-up-loom-with-your-coding-agent"></a>

# 코딩 에이전트로 Loom 설정하기

이 페이지는 Claude Code나 Codex 같은 코딩 에이전트가 자신이 실행되는 머신에 Loom을 설치하고, 설치가 제대로 동작하는지 증명해야 할 때 참고하는 문서입니다. 무엇을 실행해야 하는지, 무엇을 하지 말아야 하는지, 각 검사에서 무엇이 확인되어야 하는지, 그리고 어떤 보고를 해야 하는지를 설명합니다. 모든 단계는 그대로 실행할 수 있게 작성되어 있으며, 사람이 직접 해야 하는 단계는 따로 표시되어 있습니다.

각 릴리스에는 해당 버전과 커밋이 채워진 이 페이지가 일반 텍스트로 첨부됩니다.

```text
https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt
https://github.com/ClickHouse/loom/releases/download/v<version>/agent-setup.txt
```

<a id="pins"></a>

## 핀

<!-- pins -->

이 사본에는 릴리스가 지정되어 있지 않으므로 아래 명령은 그대로 실행되지 않습니다. 설치하라고 요청받은 릴리스의 `agent-setup.txt`를 따르세요. 그 파일에는 아래의 모든 `<version>`과 `<commit>`이 채워져 있습니다. 릴리스가 지정되지 않았다면 최신 릴리스의 파일을 따르세요:
`https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt`.

<!-- /pins -->

<a id="rules"></a>

## 규칙

- `sudo`를 사용하지 말고, 시스템 패키지를 설치하거나 변경하지 마세요. compose 플러그인이 포함된 Docker가 현재 사용자 권한으로 이미 실행 중이어야 하며, `curl`, `tar`, `python3`, Node.js 18 이상(플러그인의 훅이 Node.js로 실행됩니다), 그리고 사용하는 CLI(`claude` 또는 `codex`)가 `PATH`에 있어야 합니다. 하나라도 없으면 중단하고 보고하세요.
- 공급자 키를 설정하지 말고, 요구하지도 마세요. 스택은 키 없이도 부팅되며 아래의 모든 검사를 통과합니다.
- 1단계를 시작하기 전에 다음 중 하나라도 해당하면 중단하고 보고하세요. 환경에 `LOOM_*` 변수가 설정되어 있는 경우(설치 프로그램, 플러그인, SDK가 모두 이 변수를 읽습니다: 포트, 바인드 주소, 다른 서버), `~/.loom`이 존재하는 경우, 또는 사용 중인 CLI가 이미 `loom`을 알고 있는 경우입니다. 여기에는 플러그인(`claude plugin list`, `codex plugin list`), 마켓플레이스(`claude plugin marketplace list`, `codex plugin marketplace list`), MCP 서버(`claude mcp list`, `codex mcp list`)가 포함됩니다. 이들을 변경할지 여부는 사용자가 결정합니다. 해당하는 것이 없으면 스택은 `localhost:7777`에서만 응답하고 플러그인은 여기에 연결됩니다.
- 검사를 통과시키려고 검사 기준을 약화하지 마세요. 검사 스크립트를 수정하거나, 단계를 건너뛰거나, 검사가 기대하는 값을 바꾸거나, 실행되지 않은 단계에 `PASS`를 보고해서는 안 됩니다. `FAIL` 줄도 올바른 보고입니다.

<a id="1-install-the-release"></a>

## 1. 릴리스 설치

```sh
mkdir -p ~/loom-agent-setup
curl -fsSL "https://github.com/ClickHouse/loom/releases/download/v<version>/install.sh" \
  -o ~/loom-agent-setup/loom-install.sh && sh ~/loom-agent-setup/loom-install.sh \
  && rm ~/loom-agent-setup/loom-install.sh
```

설치 프로그램은 번들의 SHA-256을 검증하고 `~/.loom` 아래에 설치한 뒤 스택을 시작하며, 서버의 `/v1/health`가 응답할 때까지 기다립니다. 명령이 실패하면 보고 내용은 `install` 줄 하나뿐이며, 그 이후 단계로 진행하지 마세요. 설치 프로그램이 수행하는 작업의 전체 내용은 [퀵스타트](quickstart.md#1-boot-the-stack)에서 확인할 수 있습니다.

<a id="2-install-the-plugin"></a>

## 2. 플러그인 설치

플러그인은 서버와 동일한 릴리스의 소스에서 가져오므로 서버와 버전이 일치합니다.

```sh
curl -fsSL "https://github.com/ClickHouse/loom/archive/refs/tags/v<version>.tar.gz" \
  | tar -xz -C ~/loom-agent-setup
```

`~/loom-agent-setup`은 삭제하지 마세요. 마켓플레이스의 소스입니다. 이어서 Claude Code에서는 다음과 같이 실행합니다.

```sh
claude plugin marketplace add ~/loom-agent-setup/loom-<version>
claude plugin install loom@loom
```

Codex에서는 다음과 같이 실행합니다.

```sh
codex plugin marketplace add ~/loom-agent-setup/loom-<version>
codex plugin add loom@loom
```

설치된 스택은 단일 사용자용이므로 플러그인은 토큰 없이 `http://localhost:7777`에 연결합니다. 이제 **사용자**가 에이전트의 새 세션을 시작하고(Codex에서는 대화형 `codex` 실행이며, 여기서 플러그인의 훅도 승인합니다), 이 텍스트를 다시 전달한 뒤 "3단계부터 계속"이라고 말합니다. 3단계와 4단계는 그 세션에서 실행됩니다.

<a id="3-run-the-check"></a>

## 3. 점검 실행

이 스크립트를 `loom-setup-check.py`로 저장하고 `python3 loom-setup-check.py`를 실행합니다. Python 표준 라이브러리만 사용하며 HTTP로 Loom과 통신합니다. 네임스페이스 `agent-setup-check`에 데이터를 쓰고, 쓴 데이터에 자신의 실행을 나타내는 태그를 붙이며, 해당 실행의 행만 검색하고, 끝나면 그 행을 삭제합니다.

```python title="loom-setup-check.py"
import json
import os
import pathlib
import random
import sys
import urllib.error
import urllib.parse
import urllib.request

PIN_VERSION = "<version>"
PIN_COMMIT = "<commit>"
# Loom's own tests point the check elsewhere; an agent sets neither.
BASE = os.environ.get("SETUP_CHECK_URL", "http://localhost:7777")
WHO = {"org": os.environ.get("SETUP_CHECK_ORG", "dev"), "namespace": "agent-setup-check"}
AGENT = "agent-setup"
SESSION = "setup-1"
RUN = f"{random.randrange(100_000):05d}"
TAGS = [f"setup-check:{RUN}"]
FACT = f"The setup check orchard has {RUN} pear trees."
KEY = f"setup/check/orchard-{RUN}"
DOC = f"setup-check://irrigation-{RUN}"
NOTE = f"Irrigation valve {RUN} opens at five in the morning, every second day."
LOOPBACK = ("localhost", "127.0.0.1", "::1")
checks = []


def call(op, **body):
    req = urllib.request.Request(
        f"{BASE}/v1/{op}",
        data=json.dumps({**WHO, **body}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{op} answered {e.code}: {e.read()[:200].decode(errors='replace')}")


def check(name, probe):
    try:
        grade, detail = probe()
    except Exception as e:  # any failure is this check's FAIL line
        grade, detail = "FAIL", f"{type(e).__name__}: {e}"
    checks.append((name, grade, detail))
    return grade == "PASS"


def hit_problem(hits, field, wanted, text_field):
    """Why the first hit is not the row this run wrote, or None when it is."""
    if not hits["results"]:
        return "no hits"
    top = hits["results"][0]
    if top[field] != wanted:
        return f"the first hit is {top[field]}, not {wanted}"
    if RUN not in top[text_field]:
        return f"{wanted} came back without its text"
    return None


def bind_host():
    """LOOM_BIND_HOST from the installed stack's .env, or "" when it sets none."""
    env = pathlib.Path.home() / ".loom" / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    values = [line.split("=", 1)[1].strip() for line in lines if line.startswith("LOOM_BIND_HOST=")]
    return values[-1] if values else ""


def server():
    if PIN_VERSION.startswith("<"):
        return "FAIL", "this copy names no release: use the release's agent-setup.txt"
    if urllib.parse.urlsplit(BASE).hostname not in LOOPBACK:
        return "FAIL", f"{BASE} is not a loopback address"
    host = bind_host()
    if host not in ("", *LOOPBACK):
        return "FAIL", f"~/.loom/.env publishes Loom on {host}"
    with urllib.request.urlopen(f"{BASE}/v1/health", timeout=30) as resp:
        health = json.load(resp)
    version, commit = health.get("version"), health.get("build_commit")
    # A release candidate's server reports the final version it leads to.
    final = PIN_VERSION.split("-")[0]
    if (version, commit) != (final, PIN_COMMIT):
        return "FAIL", f"serves {version} at {commit}; the pins name {final} at {PIN_COMMIT}"
    return "PASS", f"{BASE} serves {version} at {commit[:12]}"


def memory():
    call("memory.set", agent=AGENT, memory_key=KEY, value=FACT, tags=TAGS, session_id=SESSION)
    # A new session reads with no session_id: it sees what the last one wrote.
    hits = call(
        "memory.search", query="how many pear trees does the setup check orchard have", tags=TAGS
    )
    problem = hit_problem(hits, "memory_key", KEY, "content_excerpt")
    if problem:
        return "FAIL", problem
    similarity = float(hits["results"][0]["similarity"])
    return "PASS", f"a new session finds {KEY} (similarity {similarity:.2f})"


def knowledge():
    call("knowledge.add", agent=AGENT, source_uri=DOC, title="Irrigation", content=NOTE, tags=TAGS)
    hits = call("knowledge.search", query="when does the irrigation valve open", tags=TAGS)
    problem = hit_problem(hits, "source_uri", DOC, "content")
    if problem:
        return "FAIL", problem
    similarity = float(hits["results"][0]["similarity"])
    return "PASS", f"knowledge.search finds {DOC} (similarity {similarity:.2f})"


def cleanup():
    gone = call("memory.delete", agent=AGENT, memory_key=KEY, session_id=SESSION)["found"]
    dropped = call("knowledge.delete", source_uri=DOC)["deleted"]
    if not (gone and dropped):
        return "FAIL", f"memory deleted: {gone}, document deleted: {dropped}"
    return "PASS", "deleted the memory and the document it wrote"


# Nothing is written to a server the pins or the loopback rule refuse.
if check("server", server):
    check("memory", memory)
    check("knowledge", knowledge)
    check("cleanup", cleanup)
width = max(len(name) for name, _, _ in checks)
for name, grade, detail in checks:
    print(f"  [{grade}]  {name:<{width}}  {detail}")
failed = [name for name, grade, _ in checks if grade == "FAIL"]
if failed:
    print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
    sys.exit(1)
print(f"\nAll {len(checks)} checks passed.")
```

각 점검이 확인해야 하는 내용:

| 점검        | 통과 조건                                                                                                                                                     |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server`    | URL과 `~/.loom/.env`의 `LOOM_BIND_HOST`가 루프백 주소이고, `/v1/health`가 고정된 버전(릴리스 후보라면 최종 버전)과 커밋을 반환합니다.                         |
| `memory`    | 한 세션에서 쓴 사실이, 다음 세션처럼 세션을 지정하지 않은 검색의 첫 번째 결과로 나오며, 자신의 텍스트와 숫자 유사도를 가집니다.                                |
| `knowledge` | `knowledge.add`로 추가한 문서가 `knowledge.search`의 첫 번째 결과로 나오며, 자신의 텍스트와 숫자 유사도를 가집니다.                                            |
| `cleanup`   | 스크립트가 쓴 메모리와 문서가 삭제됩니다.                                                                                                                     |

`server`가 실패하면 스크립트는 아무것도 쓰지 않고 다른 점검도 실행하지 않습니다.

<a id="4-check-the-agents-connection"></a>

## 4. 에이전트 연결 확인

`loom_search` 도구를 쿼리 `agent setup check`로 한 번 호출하고, `claude plugin list`(또는 `codex plugin list`)를 실행합니다. 도구가 오류 없이 응답하고 목록에 `loom@loom`이 활성화된 것으로 표시되면 `[PASS]  agent  loom_search answered` 줄이 출력되고, 그렇지 않으면 `[FAIL]  agent  <the tool's error, or the plugin's status>`가 출력됩니다.

<a id="report"></a>

## 보고

스크립트가 출력한 줄을 먼저 적고, 그다음 `agent` 줄을 적은 뒤, `agent` 줄까지 포함해 집계한 요약을 하나 적습니다. 형식은 `loom doctor`의 형식을 따릅니다.

```text
  [PASS]  server     http://localhost:7777 serves 1.2.3 at 0123456789ab
  [PASS]  memory     a new session finds setup/check/orchard-04211 (similarity 0.91)
  [PASS]  knowledge  knowledge.search finds setup-check://irrigation-04211 (similarity 0.77)
  [PASS]  cleanup    deleted the memory and the document it wrote
  [PASS]  agent      loom_search answered

All 5 checks passed.
```

실패한 경우 요약은 `<n> check(s) failed: <names>`로 표시되며, 처음 나오는 `FAIL` 줄이 가장 먼저 고쳐야 할 항목입니다. 실행을 중단시키는 단계는 한 줄과 그 요약으로 보고합니다.

- 1단계 전에 실행을 중단시키는 규칙: `[FAIL]  setup  <what was missing or found>`
- 설치 실패: `[FAIL]  install  <the command's last line>`
- 2단계 실패: `[FAIL]  plugin  <the failed command's last line>`

각 경우 뒤에는 `1 check(s) failed: <its name>`이 이어집니다.
