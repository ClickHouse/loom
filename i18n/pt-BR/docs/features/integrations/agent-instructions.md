<!-- i18n: source=docs/features/integrations/agent-instructions.md blob=66910fd5b55982ca9d0f8f554d606e94b2c0f60c model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md), which is authoritative.

<a id="agent-instructions"></a>

# Instruções para agentes

Um bloco curto no arquivo de instruções de um agente diz a ele quando carregar o contexto do Loom, salvar um fato e usar o índice de código em vez de usar grep ou ler arquivos. Esta página traz esse bloco uma única vez, junto com o arquivo de onde cada cliente o lê; cole-o, preencha seus dois namespaces a partir de `code.repos` (abaixo) e faça o commit junto com o repositório.

Os plugins do Claude Code e do Codex já fazem isso por meio de seus hooks e skills ([Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#skills), [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#skills)); o bloco serve para todos os outros clientes, e para o Claude Code ou o Codex conectados sem o plugin. Ele cita as ferramentas listadas pelo toolset `coding`, então conecte o cliente em `?toolset=coding`: `loom install --client <name> --toolset coding` para os clientes que ele cobre, ou `?toolset=coding` na URL ([clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)).

<a id="the-block"></a>

## O bloco

Com os [hooks do Cline](https://github.com/ClickHouse/loom/blob/main/integrations/cline/README.md), substitua, na sua
cópia, o parágrafo do bloco que começa com "Every task:" por "Every task: make these two calls.", para que as duas chamadas continuem obrigatórias. O Cline mostra o texto de um hook ao modelo somente depois da
ferramenta para a qual ele foi executado, então o primer chega depois da primeira chamada de ferramenta da tarefa e o bloco de arquivo de uma edição chega depois da edição.

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

`code.repos` lista os repositórios que o token pode ler ou pedir para participar, uma linha por
branch indexada:

```bash
curl -s -X POST https://loom.example.com/v1/code.repos \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"repo": "<owner>/<name>", "fields": ["branch", "primary_namespace", "readable"]}'
```

Substitua `<code-namespace>` pelo `namespace` da linha da branch em que esta cópia do
arquivo está commitada e `<team-namespace>` pelo `primary_namespace` dela (o da branch principal, onde ficam as regras e decisões de arquivo da equipe); para a branch principal, os dois são
iguais. Um repositório sem linha (não indexado, ou em que `code.repos` responde que a inteligência de código está desativada), ou cuja linha diz `readable: false` (um repositório que este token
não pode ler, listado para que você possa pedir acesso ou adotá-lo), usa o bloco sem `extra_namespaces`, sem a linha de regra da equipe e sem a seção Code.

<a id="where-each-client-reads-it"></a>

## Onde cada cliente lê o bloco

O bloco é o mesmo em todos os lugares (exceto pela mudança de um parágrafo nos hooks do Cline
acima); só o arquivo e o cabeçalho mudam. A maioria desses clientes também lê
`AGENTS.md`, então mantenha uma única cópia quando um arquivo atender a vários deles; um cliente que lê
dois arquivos com o bloco o vê duas vezes, o que consome contexto, mas não muda nada.

| Cliente                                                        | Arquivo                                                                    | Cabeçalho antes do bloco                                                     |
| -------------------------------------------------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Codex sem o plugin, e qualquer agente que leia `AGENTS.md`     | `AGENTS.md` na raiz do repositório                                         | nenhum                                                                       |
| Zed                                                            | o primeiro arquivo de instruções que ele encontrar (abaixo)                | nenhum                                                                       |
| Claude Code sem o plugin                                       | `CLAUDE.md` na raiz do repositório (abaixo)                                | nenhum                                                                       |
| Cursor                                                         | `.cursor/rules/loom.mdc`                                                   | `---`, `description: Loom memory and code index`, `alwaysApply: true`, `---` |
| Devin Desktop (ou Windsurf)                                    | `.devin/rules/loom.md` (`.windsurf/rules/loom.md` em um build do Windsurf) | `---`, `trigger: always_on`, `---`                                           |
| Cline                                                          | `.clinerules/loom.md`                                                      | nenhum                                                                       |
| VS Code (GitHub Copilot)                                       | `.github/copilot-instructions.md`                                          | nenhum                                                                       |

O Zed lê apenas o primeiro arquivo de instruções que encontra (`.rules`, `.cursorrules` e outros
antes de `AGENTS.md`), então o bloco vai nesse arquivo, ou em `AGENTS.md` quando o repositório
não tem nenhum.

O Claude Code não lê `AGENTS.md`: se houver um no repositório, coloque o bloco em
`AGENTS.md` e faça o `CLAUDE.md` importá-lo com a linha `@AGENTS.md`.

Cada linha do cabeçalho vai em sua própria linha. O arquivo do Cursor, por exemplo, começa assim:

```markdown
---
description: Loom memory and code index
alwaysApply: true
---

## Loom: memory and code index
```

Um `AGENTS.md`, `CLAUDE.md` ou arquivo do Copilot já existente mantém seu conteúdo: adicione o bloco como
uma seção dele.

<a id="why-these-rules"></a>

## Por que estas regras

Nada em uma sessão sem hooks provoca uma recuperação de memória, e uma ferramenta de memória listada é uma escolha entre muitas, então o bloco faz o carregamento de contexto parte de cada tarefa, como fazem os hooks dos plugins.

Cada linha é a forma curta do que os plugins ensinam, ou do que as respostas do Loom trazem:

- carregar contexto no início de uma tarefa e antes de uma edição: os hooks de início de sessão e de contexto de arquivo dos plugins, que chamam `context.pack` e `memory.for_file`;
- recuperar e salvar fatos declarados à medida que surgem: a skill
  [`loom-memory`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-memory/SKILL.md);
- o índice de código antes do grep, e o grep onde o índice não consegue responder: a skill
  [`loom-code`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-code/SKILL.md), que o desvio de código (code shunt) dos plugins impõe;
- ler as memórias que uma resposta de código traz: seus `lessons`
  ([memórias vinculadas ao código](../../concepts/memory.md#memories-linked-to-code)), que uma sessão com plugin recebe nas mesmas respostas.

A última linha de memória do bloco representa a revisão ao fim da tarefa; para detalhá-la, copie suas perguntas do [guia de integração de agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#7-follow-the-memory-skills-review) para baixo do bloco. As skills trazem o detalhe que o bloco deixa de fora: os campos do recibo, documentos versus memórias, as regras de parada. As skills `loom` e `loom-code` podem ser instaladas junto ao bloco para esse detalhe ([agentes de código](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#coding-agents)); `loom` cobre o ciclo de memória, os formatos de escrita, os recibos e a revisão ao fim da tarefa. Elas não têm hooks, então não substituem as duas chamadas do bloco.

<a id="related"></a>

## Relacionados

- [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md): como conectar cada cliente.
- [Como escolher um conjunto de ferramentas](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset).
- [Integrações](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md).
