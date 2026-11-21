<!-- i18n: source=docs/getting-started/agent-setup.md blob=eb3a5968c01a25b666c59b8907efe2f2be226078 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md), which is authoritative.

<a id="set-up-loom-with-your-coding-agent"></a>

# Configure o Loom com seu agente de código

Esta página é para um agente de código, Claude Code ou Codex, que recebeu a tarefa de instalar o Loom na máquina em que está rodando e de provar que a instalação funciona. Ela diz o que executar, o que não fazer, o que cada verificação deve constatar e qual relatório entregar. Cada passo foi escrito para ser executado exatamente como está; os passos que uma pessoa precisa executar estão marcados.

Cada release anexa esta página como texto simples, com a própria versão e o próprio commit preenchidos:

```text
https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt
https://github.com/ClickHouse/loom/releases/download/v<version>/agent-setup.txt
```

<a id="pins"></a>

## Fixações

<!-- pins -->

Esta cópia não indica nenhuma versão (release), portanto seus comandos não funcionam como estão. Siga o `agent-setup.txt` da versão que você foi instruído a instalar, no qual todo `<version>` e `<commit>` abaixo está preenchido; se nenhuma versão foi indicada, siga o da mais recente:
`https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt`.

<!-- /pins -->

<a id="rules"></a>

## Regras

- Não use `sudo` e não instale nem altere pacotes do sistema. O Docker com o plugin compose
  já deve funcionar para o seu usuário, e `curl`, `tar`, `python3`, Node.js 18 ou mais
  recente (os hooks do plugin rodam nele) e a sua própria CLI (`claude` ou `codex`) devem
  estar no `PATH`; se algum estiver faltando, pare e informe.
- Não defina nenhuma chave de provedor e não peça nenhuma. A stack sobe e todas as
  verificações abaixo passam sem ela.
- Antes do passo 1, pare e informe se alguma variável `LOOM_*` estiver definida no seu
  ambiente (o instalador, o plugin e os SDKs leem todas elas: uma porta, um endereço de
  bind, outro servidor), se `~/.loom` existir, ou se a sua CLI já conhecer um `loom`: um
  plugin (`claude plugin list`, `codex plugin list`), um marketplace
  (`claude plugin marketplace list`, `codex plugin marketplace list`) ou um servidor MCP
  (`claude mcp list`, `codex mcp list`). Alterar qualquer um deles é decisão da pessoa. Sem
  nenhum deles, a stack responde apenas em `localhost:7777` e o plugin se conecta a ela.
- Nunca enfraqueça uma verificação para fazê-la passar: não edite o script de verificação,
  não pule um passo, não mude o que uma verificação espera e não informe `PASS` para um
  passo que não foi executado. Uma linha `FAIL` é um relatório correto.

<a id="1-install-the-release"></a>

## 1. Instale a release

```sh
mkdir -p ~/loom-agent-setup
curl -fsSL "https://github.com/ClickHouse/loom/releases/download/v<version>/install.sh" \
  -o ~/loom-agent-setup/loom-install.sh && sh ~/loom-agent-setup/loom-install.sh \
  && rm ~/loom-agent-setup/loom-install.sh
```

O instalador verifica o SHA-256 do bundle, instala em `~/.loom`, inicia a stack
e aguarda o `/v1/health` do servidor. Se o comando falhar, o relatório é apenas a linha `install` dele; não prossiga. O [guia de início rápido](quickstart.md#1-boot-the-stack) descreve em detalhes tudo o que o
instalador faz.

<a id="2-install-the-plugin"></a>

## 2. Instale o plugin

O plugin vem do código-fonte da mesma release, então é compatível com o servidor:

```sh
curl -fsSL "https://github.com/ClickHouse/loom/archive/refs/tags/v<version>.tar.gz" \
  | tar -xz -C ~/loom-agent-setup
```

Mantenha `~/loom-agent-setup`: ele é a fonte do marketplace. Em seguida, para o Claude Code:

```sh
claude plugin marketplace add ~/loom-agent-setup/loom-<version>
claude plugin install loom@loom
```

ou, para o Codex:

```sh
codex plugin marketplace add ~/loom-agent-setup/loom-<version>
codex plugin add loom@loom
```

A stack instalada é de usuário único, então o plugin se conecta a `http://localhost:7777`
sem token. **A pessoa** agora inicia uma nova sessão do agente (Codex: uma execução interativa de
`codex`, na qual ela também aprova os hooks do plugin), entrega a ele este texto novamente e
diz "continue no passo 3". Os passos 3 e 4 são executados nessa sessão.

<a id="3-run-the-check"></a>

## 3. Execute a verificação

Salve este script como `loom-setup-check.py` e execute `python3 loom-setup-check.py`. Ele usa
apenas a biblioteca padrão do Python e se comunica com o Loom por HTTP. Ele grava no namespace
`agent-setup-check`, marca o que grava com a sua própria execução, pesquisa apenas as linhas dessa execução
e as exclui ao terminar.

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

O que cada verificação precisa constatar:

| Verificação | Passa quando                                                                                                                                                  |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server`    | A URL e o `LOOM_BIND_HOST` de `~/.loom/.env` são loopback, e `/v1/health` informa a versão e o commit fixados (a versão final, no caso de um release candidate). |
| `memory`    | Um fato gravado em uma sessão é o primeiro resultado de uma busca que não informa sessão, como seria na próxima sessão, com o seu próprio texto e uma similaridade numérica. |
| `knowledge` | Um documento adicionado com `knowledge.add` é o primeiro resultado de `knowledge.search`, com o seu próprio texto e uma similaridade numérica.                |
| `cleanup`   | A memória e o documento que o script gravou são excluídos.                                                                                                    |

Quando `server` falha, o script não grava nada e não executa nenhuma outra verificação.

<a id="4-check-the-agents-connection"></a>

## 4. Verifique a conexão do agente

Chame a sua ferramenta `loom_search` uma vez, com a consulta `agent setup check`, e execute
`claude plugin list` (ou `codex plugin list`). A linha será
`[PASS]  agent  loom_search answered` quando a ferramenta responder sem erro e a lista
mostrar `loom@loom` habilitado; caso contrário, será
`[FAIL]  agent  <the tool's error, or the plugin's status>`.

<a id="report"></a>

## Relatório

Informe as linhas do script, depois a linha `agent`, e então um único resumo que também conte a linha `agent`, no formato do `loom doctor`:

```text
  [PASS]  server     http://localhost:7777 serves 1.2.3 at 0123456789ab
  [PASS]  memory     a new session finds setup/check/orchard-04211 (similarity 0.91)
  [PASS]  knowledge  knowledge.search finds setup-check://irrigation-04211 (similarity 0.77)
  [PASS]  cleanup    deleted the memory and the document it wrote
  [PASS]  agent      loom_search answered

All 5 checks passed.
```

Em caso de falha, o resumo tem a forma `<n> check(s) failed: <names>`, e a primeira linha `FAIL` é a que deve ser corrigida. Uma etapa que interrompe a execução gera uma única linha e o seu resumo:

- uma regra que interrompe antes da etapa 1: `[FAIL]  setup  <what was missing or found>`;
- uma instalação com falha: `[FAIL]  install  <the command's last line>`;
- uma etapa 2 com falha: `[FAIL]  plugin  <the failed command's last line>`;

cada uma seguida de `1 check(s) failed: <its name>`.
