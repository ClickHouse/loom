<!-- i18n: source=docs/features/integrations/agent-instructions.md blob=66910fd5b55982ca9d0f8f554d606e94b2c0f60c model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md), which is authoritative.

<a id="agent-instructions"></a>

# Instrucciones para agentes

Un bloque corto en el archivo de instrucciones de un agente le indica cuándo cargar el contexto de Loom, guardar un hecho y usar el índice de código en lugar de hacer grep o leer archivos. Esta página ofrece ese bloque una sola vez y el archivo desde el que lo lee cada cliente; pégalo, completa sus dos espacios de nombres a partir de `code.repos` (más abajo) y súbelo al repositorio con un commit.

Los plugins de Claude Code y Codex ya hacen esto mediante sus hooks y skills ([Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#skills), [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#skills)); el bloque es para todos los demás clientes, y para Claude Code o Codex conectados sin el plugin. Nombra las herramientas que lista el toolset `coding`, así que conecta el cliente con `?toolset=coding`: `loom install --client <name> --toolset coding` para los clientes que cubre, o bien `?toolset=coding` en la URL ([clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)).

<a id="the-block"></a>

## El bloque

Con los [hooks de Cline](https://github.com/ClickHouse/loom/blob/main/integrations/cline/README.md), reemplaza en tu
copia el párrafo del bloque que empieza por "Every task:" por "Every task: make these two calls.", de modo que ambas llamadas sigan siendo obligatorias. Cline muestra el texto de un hook al modelo solo después de la
herramienta para la que se ejecutó, así que el primer mensaje de contexto llega después de la primera llamada a una herramienta de la tarea, y el bloque de archivo de una edición llega después de la edición.

```markdown
## Loom: memory and code index

Loom is connected as an MCP server; its tools are named `loom_*`. A tool you do not see
listed runs through `loom_op` with the op's dotted name: on a connection without the
coding toolset, `loom_code_symbol` is `code.symbol` and `loom_working_memory_set` is
`working_memory.set`. `loom_op` with `{"op": "help", "query": "<words>"}` finds them.

Every task: make these two calls unless a Loom hook already put Loom's context in this
session (the Claude Code and Codex plugins do). Without one they are not optional.

- At the start of each task, before planning: `loom_context_pack` with the task as
  `query`. It returns the decisions, constraints and memories that bear on it.
- Before editing a file: `loom_op` with `op` set to `memory.for_file` and `arguments`
  holding `path` (the file's repo-relative path) and `extra_namespaces`
  (`["<team-namespace>"]`). Follow every constraint and decision it returns.

Memory

- To recall something mid-task, call `loom_search` with the topic, file or error. Treat
  `abstain` as "nothing relevant". A hit whose `code_state` is `changed`, `unresolved`
  or `missing` is about code that changed or went away since it was written: check the
  code before relying on it.
- When the user states a decision, preference, constraint or correction, save it with
  `loom_memory_set` right away: one plain-text fact per call under a readable key
  (`<scope>/<kind>/<id>`), dates as calendar dates. Never save a guess, your reasoning or
  the conversation.
- A rule the user states for the whole team about a file: `loom_memory_set` with
  `namespace` `<team-namespace>`, the file in `files` and the tag `constraint`.
- A fact that changed: `loom_memory_update` on the same key, in the namespace the row
  lives in, never a second copy. A team rule or decision someone else wrote is not yours
  to update: follow the refusal's `hint`.
- Your plan and checklist for this task: `loom_working_memory_set`, not `loom_memory_set`.
- Before reporting done, one pass over what should outlive the session.

Code

- This repository's code namespace is `<code-namespace>`; pass it as `namespace` on
  every `loom_code_*` call.
- Where X is defined: `loom_code_symbol`. Who calls X: `loom_code_callers`. A concept
  with no exact keyword: `loom_code_search`. An exact string: `loom_code_grep`. What is
  in a file: `loom_code_outline`, then read one span. What a change breaks:
  `loom_code_impact`.
- Grep your own uncommitted files and repositories the index does not cover. When a code
  tool answers `abstain: true`, grep instead of retrying with synonyms.
- A code answer's `lessons` are memories about its top files: follow them, and check the
  code of one whose `code_state` is `changed`, `unresolved` or `missing` before relying on
  it.
```

`code.repos` lista los repositorios que el token puede leer o solicitar unirse, una fila por rama indexada:

```bash
curl -s -X POST https://loom.example.com/v1/code.repos \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"repo": "<owner>/<name>", "fields": ["branch", "primary_namespace", "readable"]}'
```

Reemplaza `<code-namespace>` por el `namespace` de la fila de la rama en la que está confirmada esta copia del archivo, y `<team-namespace>` por su `primary_namespace` (el de la rama troncal, donde viven las reglas de archivo y las decisiones del equipo); para la rama troncal, ambos son el mismo. Un repositorio sin fila (no indexado, o porque `code.repos` responde que la inteligencia de código está desactivada), o cuya fila indica `readable: false` (un repositorio que este token no puede leer, listado para que puedas solicitar acceso o adoptarlo), usa el bloque sin `extra_namespaces`, sin la línea de reglas del equipo y sin la sección Code.

<a id="where-each-client-reads-it"></a>

## Dónde lee cada cliente el bloque

El bloque es el mismo en todos los clientes (salvo el cambio de un párrafo en los hooks de Cline descrito arriba); solo cambian el archivo y su encabezado. La mayoría de estos clientes también leen `AGENTS.md`, así que conviene mantener una sola copia cuando un único archivo sirve a varios de ellos; un cliente que lee dos archivos con el bloque lo ve dos veces, lo que consume contexto pero no cambia nada más.

| Cliente                                                        | Archivo                                                                | Encabezado antes del bloque                                                  |
| -------------------------------------------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Codex sin el plugin, y cualquier agente que lea `AGENTS.md`    | `AGENTS.md` en la raíz del repositorio                                 | ninguno                                                                      |
| Zed                                                            | el primer archivo de instrucciones que encuentra (abajo)               | ninguno                                                                      |
| Claude Code sin el plugin                                      | `CLAUDE.md` en la raíz del repositorio (abajo)                         | ninguno                                                                      |
| Cursor                                                         | `.cursor/rules/loom.mdc`                                               | `---`, `description: Loom memory and code index`, `alwaysApply: true`, `---` |
| Devin para escritorio (Windsurf)                               | `.devin/rules/loom.md` (`.windsurf/rules/loom.md` en una compilación de Windsurf) | `---`, `trigger: always_on`, `---`                                |
| Cline                                                          | `.clinerules/loom.md`                                                  | ninguno                                                                      |
| VS Code (GitHub Copilot)                                       | `.github/copilot-instructions.md`                                      | ninguno                                                                      |

Zed lee solo el primer archivo de instrucciones que encuentra (`.rules`, `.cursorrules` y otros antes que `AGENTS.md`), así que el bloque va en ese archivo, o en `AGENTS.md` cuando el repositorio no tiene ninguno.

Claude Code no lee `AGENTS.md`: si el repositorio ya tiene uno, pon el bloque en `AGENTS.md` y haz que `CLAUDE.md` lo importe con la línea `@AGENTS.md`.

Cada línea del encabezado va en su propia línea. Por ejemplo, el archivo de Cursor empieza así:

```markdown
---
description: Loom memory and code index
alwaysApply: true
---

## Loom: memory and code index
```

Un archivo `AGENTS.md`, `CLAUDE.md` o de Copilot que ya exista conserva su contenido: añade el bloque como una sección más.

<a id="why-these-rules"></a>

## Por qué estas reglas

Nada en una sesión sin hooks provoca una consulta de memoria, y una herramienta de memoria incluida en una lista es una opción entre muchas, así que el bloque hace que cargar contexto forme parte de cada tarea, como lo hacen los hooks de los plugins.

Cada línea es la forma abreviada de lo que enseñan los plugins, o de lo que incluyen las respuestas de Loom:

- cargar contexto al inicio de una tarea y antes de una edición: los hooks de inicio de sesión y de contexto de archivo de los plugins, que llaman a `context.pack` y `memory.for_file`;
- consultar y guardar los hechos declarados a medida que aparecen: la skill
  [`loom-memory`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-memory/SKILL.md);
- el índice de código antes que grep, y grep donde el índice no puede responder: la skill
  [`loom-code`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-code/SKILL.md), que el desvío de código de los plugins hace cumplir;
- leer las memorias que incluye una respuesta de código: sus `lessons`
  ([memorias vinculadas al código](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)), que una sesión con plugin recibe en las mismas respuestas.

La última línea de memoria del bloque representa la revisión de fin de tarea; para detallarla, copie sus preguntas de la [guía de integración de agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#7-follow-the-memory-skills-review) debajo del bloque. Las skills aportan el detalle que el bloque omite: los campos del recibo, documentos frente a memorias, las reglas de parada. Las skills `loom` y `loom-code` se pueden instalar junto al bloque para obtener ese detalle ([agentes de programación](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#coding-agents)); `loom` cubre el ciclo de memoria, las formas de escritura, los recibos y la revisión de fin de tarea. No tienen hooks, por lo que no sustituyen las dos llamadas del bloque.

<a id="related"></a>

## Relacionado

- [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md): cómo conectar cada cliente.
- [Elegir un conjunto de herramientas](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset).
- [Integraciones](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md).
