<!-- i18n: source=docs/getting-started/agent-setup.md blob=eb3a5968c01a25b666c59b8907efe2f2be226078 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md), which is authoritative.

<a id="set-up-loom-with-your-coding-agent"></a>

# Configura Loom con tu agente de programación

Esta página es para un agente de programación, Claude Code o Codex, al que se le pide instalar Loom en la máquina donde se ejecuta y demostrar que la instalación funciona. Indica qué ejecutar, qué no hacer, qué debe ver cada comprobación y el informe que debe entregar. Cada paso está escrito para ejecutarse tal cual; los pasos que debe realizar una persona están marcados.

Cada versión adjunta esta página como texto plano, con su propia versión y su propio commit ya rellenados:

```text
https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt
https://github.com/ClickHouse/loom/releases/download/v<version>/agent-setup.txt
```

<a id="pins"></a>

## Versiones fijadas

<!-- pins -->

Esta copia no indica ninguna versión, por lo que sus comandos no se ejecutan tal como están. Sigue el `agent-setup.txt` de la versión que se te pidió instalar, donde cada `<version>` y `<commit>` de abajo está completado; si no se indica ninguna versión, sigue el de la última:
`https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt`.

<!-- /pins -->

<a id="rules"></a>

## Reglas

- No uses `sudo` ni instales o cambies paquetes del sistema. Docker con el plugin compose
  ya debe funcionar para tu usuario, y `curl`, `tar`, `python3`, Node.js 18 o superior (los
  hooks del plugin se ejecutan con él) y tu propia CLI (`claude` o `codex`) deben estar en
  `PATH`; si falta alguno, detente e infórmalo.
- No configures ninguna clave de proveedor ni pidas una. El stack arranca y todas las
  comprobaciones siguientes pasan sin ella.
- Antes del paso 1, detente e informa si hay alguna variable `LOOM_*` definida en tu
  entorno (el instalador, el plugin y los SDK la leen: un puerto, una dirección de bind,
  otro servidor), si existe `~/.loom`, o si tu CLI ya conoce un `loom`: un plugin
  (`claude plugin list`, `codex plugin list`), un marketplace
  (`claude plugin marketplace list`, `codex plugin marketplace list`) o un servidor MCP
  (`claude mcp list`, `codex mcp list`). Decidir si se cambia alguno de ellos le
  corresponde a la persona. Si no hay ninguno, el stack responde solo en `localhost:7777`
  y el plugin se conecta a él.
- Nunca debilites una comprobación para que pase: no edites el script de comprobación, no
  omitas un paso, no cambies lo que espera una comprobación ni informes `PASS` en un paso
  que no se ejecutó. Una línea `FAIL` es un informe correcto.

<a id="1-install-the-release"></a>

## 1. Instalar la release

```sh
mkdir -p ~/loom-agent-setup
curl -fsSL "https://github.com/ClickHouse/loom/releases/download/v<version>/install.sh" \
  -o ~/loom-agent-setup/loom-install.sh && sh ~/loom-agent-setup/loom-install.sh \
  && rm ~/loom-agent-setup/loom-install.sh
```

El instalador verifica el SHA-256 del bundle, instala en `~/.loom`, inicia el stack
y espera a que `/v1/health` del servidor responda. Si el comando falla, el informe es únicamente su línea `install`;
no continúes. La [guía de inicio rápido](quickstart.md#1-boot-the-stack) explica con detalle
qué hace el instalador.

<a id="2-install-the-plugin"></a>

## 2. Instalar el plugin

El plugin viene del código fuente de la misma versión, por lo que coincide con el servidor:

```sh
curl -fsSL "https://github.com/ClickHouse/loom/archive/refs/tags/v<version>.tar.gz" \
  | tar -xz -C ~/loom-agent-setup
```

Conserva `~/loom-agent-setup`: es el origen del marketplace. Luego, para Claude Code:

```sh
claude plugin marketplace add ~/loom-agent-setup/loom-<version>
claude plugin install loom@loom
```

o para Codex:

```sh
codex plugin marketplace add ~/loom-agent-setup/loom-<version>
codex plugin add loom@loom
```

El stack instalado es de un solo usuario, por lo que el plugin se conecta a `http://localhost:7777`
sin token. **La persona** inicia ahora una nueva sesión del agente (Codex: una ejecución interactiva de
`codex`, donde también aprueba los hooks del plugin), le da este texto de nuevo y
le dice "continúa en el paso 3". Los pasos 3 y 4 se ejecutan en esa sesión.

<a id="3-run-the-check"></a>

## 3. Ejecutar la comprobación

Guarda este script como `loom-setup-check.py` y ejecuta `python3 loom-setup-check.py`. Usa
solo la biblioteca estándar de Python y se comunica con Loom por HTTP. Escribe en el espacio de nombres
`agent-setup-check`, etiqueta lo que escribe con su propia ejecución, busca solo las filas de esa ejecución
y las elimina al terminar.

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

Qué debe verificar cada comprobación:

| Comprobación | Pasa cuando                                                                                                                                                   |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server`    | La URL y el `LOOM_BIND_HOST` de `~/.loom/.env` son loopback, y `/v1/health` indica la versión fijada (la final de un release candidate) y el commit.         |
| `memory`    | Un dato escrito en una sesión es el primer resultado de una búsqueda que no indica sesión, tal como lo sería para la sesión siguiente, con su propio texto y una similitud numérica. |
| `knowledge` | Un documento añadido con `knowledge.add` es el primer resultado de `knowledge.search`, con su propio texto y una similitud numérica.                         |
| `cleanup`   | La memoria y el documento que escribió el script quedan eliminados.                                                                                           |

Cuando `server` falla, el script no escribe nada y no ejecuta ninguna otra comprobación.

<a id="4-check-the-agents-connection"></a>

## 4. Comprueba la conexión del agente

Llama una vez a tu herramienta `loom_search`, con la consulta `agent setup check`, y ejecuta
`claude plugin list` (o `codex plugin list`). La línea es
`[PASS]  agent  loom_search answered` cuando la herramienta responde sin error y la lista
muestra `loom@loom` habilitado; de lo contrario, la línea es
`[FAIL]  agent  <the tool's error, or the plugin's status>`.

<a id="report"></a>

## Informe

Indica las líneas del script, luego la línea `agent`, y después un resumen que cuente también la línea `agent`, con el formato de `loom doctor`:

```text
  [PASS]  server     http://localhost:7777 serves 1.2.3 at 0123456789ab
  [PASS]  memory     a new session finds setup/check/orchard-04211 (similarity 0.91)
  [PASS]  knowledge  knowledge.search finds setup-check://irrigation-04211 (similarity 0.77)
  [PASS]  cleanup    deleted the memory and the document it wrote
  [PASS]  agent      loom_search answered

All 5 checks passed.
```

Si algo falla, el resumen dice `<n> check(s) failed: <names>`, y la primera línea `FAIL` es la que hay que corregir. Un paso que detiene la ejecución ocupa una sola línea y su resumen:

- una regla que te detiene antes del paso 1: `[FAIL]  setup  <what was missing or found>`;
- una instalación fallida: `[FAIL]  install  <the command's last line>`;
- un paso 2 fallido: `[FAIL]  plugin  <the failed command's last line>`;

cada una seguida de `1 check(s) failed: <its name>`.
