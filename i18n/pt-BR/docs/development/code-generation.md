<!-- i18n: source=docs/development/code-generation.md blob=3f6e27200eccce850139d9114282249d0644e35f model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md), which is authoritative.

<a id="code-generation"></a>

# Geração de código

Vários arquivos do repositório são derivados do código: as tabelas de operações e os tipos de wire dos SDKs, as páginas de referência, as cópias do plugin do Codex, o manifesto de alcance das operações (op reach manifest) e o dump do schema. Cada um é commitado, de modo que o leitor o vê sem executar nada, e os geradores indicados abaixo têm um modo `--check` que sai com código 1 quando o arquivo commitado difere do que seria gerado. `make lint` executa cada uma dessas verificações, e o CI as executa nos jobs `lint` e `sdk-typescript` e na suíte de testes unitários, de modo que um arquivo desatualizado falha com o comando a executar indicado na mensagem de falha.

**Por que commitar arquivos gerados.** Um arquivo gerado que só é construído sob demanda fica invisível na revisão: uma mudança no modelo de entrada de uma operação alteraria os tipos do SDK sem um diff que alguém leia. Commitá-los coloca o efeito de uma mudança ao lado da própria mudança, e a verificação `--check` impede que os dois divirjam. O custo é uma etapa de regeneração na mudança que altera a fonte.

<a id="the-generators"></a>

## Os geradores

Todos ficam em [`scripts/codegen/`](https://github.com/ClickHouse/loom/blob/main/scripts/codegen) e compartilham
`scripts/codegen/common.py` (a raiz do repositório, os fatos que uma tabela de SDK carrega por op,
formatação com ruff, uma escrita atômica e o protocolo de escrever ou verificar).

| Script                      | Escreve                                                                                                                                                                                                                | A partir de                                                                                                                                                                                           | `--check` em `make lint`   |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `sdk_py_gen.py`             | `sdk/python/loom/ops.py`: `OpName`, a tabela `OPS` (idempotência, papel, maturidade, orçamento de latência) e um `TypedDict` por modelo de entrada e de saída                                                          | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | sim                        |
| `sdk_py_client_gen.py`      | `sdk/python/loom/client.py`: `OpsClient`, um método por op (`agent.client.tracker.source_add`), tipado com os nomes de `ops.py`                                                                                        | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | sim                        |
| `sdk_py_admin_gen.py`       | `sdk/python/loom/admin_routes.py`: `AdminRoutes`, um método por rota `/v1/admin/*` (`agent.deployment.routes.settings_set`)                                                                                            | os routers `/v1/admin/*`: o método, o caminho, o controle de acesso e a docstring de cada rota                                                                                                        | sim                        |
| `sdk_ts_gen.py`             | `sdk/typescript/src/generated/ops.ts`: uma interface por modelo, a tabela `OPS`, o tipo `OpMap`                                                                                                                        | `server.operations.registry.ALL_OPERATIONS` (os JSON Schemas dos modelos)                                                                                                                             | sim                        |
| `agent_plugin_sync.py`      | as cópias do plugin do Codex dos scripts de hook (`hooks/*.mjs`), da biblioteca compartilhada (`lib/*.mjs`), das skills e do ícone da listagem (`assets/`), idênticas byte a byte, com o bit de execução              | `integrations/claude-code-plugin/`                                                                                                                                                                    | sim                        |
| `settings_reference.py`     | a [referência de configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) gerada                                                                                                                                                   | `server/settings/catalog.py`: a variável de ambiente, o tipo, o valor padrão e o comentário acima de cada campo                                                                                       | sim                        |
| `settings_schema.py`        | `schemas/loom-settings.schema.json`, o JSON Schema do arquivo de configurações YAML                                                                                                                                    | os tipos, limites, valores padrão e comentários dos campos de configurações, sem os segredos                                                                                                          | sim                        |
| `settings_example.py`       | `loom.example.yaml`, o modelo do arquivo de configurações: todas as configurações exceto os segredos, comentadas no valor padrão sob sua descrição, por área                                                           | os campos, valores padrão, comentários e marcas live do catálogo de configurações                                                                                                                     | sim                        |
| `docs_reference.py`         | as regiões geradas de `docs/reference/`: `operations`, `workers`, `cli`, `errors`, `schema`, `status_ops`, `status_switches`, `admin_routes` (a [tabela de rotas do operador](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#operator-routes)) | o registro, `server/workers/registry.py` (`_WORKERS`), `tests/unit/snapshots/cli_tree.json`, `server/core/errors.py`, `schemas/schema.sql`, `server/settings/catalog.py`, os routers `/v1/admin/*`      | sim                        |
| `op_reach_census.py`        | `server/operations/reach_manifest.py` (`--write-manifest`); caso contrário, uma tabela de censo                                                                                                                        | o registro e os toolsets do MCP                                                                                                                                                                       | sim                        |
| `wire_changelog.py`         | nada; imprime o que mudou para os clientes entre duas revisões                                                                                                                                                         | o catálogo de capacidades construído em cada revisão                                                                                                                                                  | não                        |
| `gen_py_library_methods.py` | `server/code/domain/extract/py_library_methods.py`                                                                                                                                                                     | a biblioteca padrão do interpretador em execução                                                                                                                                                      | não (depende do interpretador) |
| `push_langfuse_prompts.py`  | `server/observability/langfuse_prompts.lock.json` (`--write`)                                                                                                                                                          | as constantes de prompt no servidor; envia-as ao Langfuse                                                                                                                                             | não                        |

A referência de configuração é renderizada a partir de comentários, então o comentário acima de uma configuração é
a sua documentação: edite o comentário, não a página. As páginas de `docs/reference/` são
prosa escrita à mão em volta de regiões `<!-- BEGIN GENERATED: <name> -->` … `<!-- END GENERATED: <name> -->`;
só o texto entre um par de marcadores é reescrito, de modo que uma página pode explicar a
tabela que carrega.

<a id="sdk_py_gen-and-sdk_ts_gen"></a>

### sdk_py_gen e sdk_ts_gen

O SDK Python continua recebendo e devolvendo dicts; o arquivo gerado adiciona os nomes, de modo que um editor
completa `agent.call("memory.search", …)` e um verificador de tipos enxerga os nomes dos campos sem
uma camada de modelos mantida à mão. Os dicts são `total=False`, com `Required[...]` nos campos que o
servidor exige. O gerador de TypeScript transforma o JSON Schema de cada modelo em interfaces
(enums e consts viram uniões de literais, `anyOf` vira união, os `$defs` compartilhados são extraídos uma única vez), de modo que
`client.call("memory.search", input)` é tipado de ponta a ponta. Execute-os após qualquer mudança no
registro de uma operação ou em seus modelos:

```bash
python scripts/codegen/sdk_py_gen.py
python scripts/codegen/sdk_py_client_gen.py
python scripts/codegen/sdk_ts_gen.py
```

Os dois SDKs expõem cada op como um método no mesmo lugar: o grupo é o primeiro segmento do nome da op,
o método é o restante unido por `.`, em snake_case no Python
(`agent.client.tracker.source_add`) e em camelCase no TypeScript (`loom.tracker.sourceAdd`).
`tests/unit/test_sdk_parity.py` falha quando as duas tabelas de ops, ou os dois nomes de método de
qualquer op, divergem.

<a id="agent_plugin_sync"></a>

### Sincronização dos plugins de agente

Os dois plugins de agentes de código diferem apenas em seus manifestos e na ligação dos hooks
(`hooks/hooks.json`). O plugin do Codex carrega cópias em vez de referências `../`, para que cada
plugin seja um diretório autocontido que um marketplace possa baixar sozinho. Depois de adicionar um
script de hook ou uma skill ao plugin do Claude Code, execute a sincronização e faça commit das cópias; ligue
um novo hook ao `hooks.json` do Codex à mão se o Codex deve executá-lo.

<a id="op_reach_census"></a>

### Censo de alcance das ops

Toda operação declara uma maturidade e precisa ter um caminho de alcance além de um `POST` HTTP
bruto, ou ser declarada `experimental`. Os tipos de alcance (`server/operations/reach.py`,
`REACH_KINDS`) são as listagens do MCP (`mcp_core`, `mcp_atomic`, `mcp_all`, derivadas ao vivo
de `server/operations/toolsets.py`), um método de SDK (`sdk_method`), um exemplo distribuído ou
script de operador (`sdk_example`), um hook de plugin ou integração (`plugin_hook`) e um ponto de chamada
do painel (`dashboard`). Os quatro últimos vêm da varredura da árvore, que um servidor implantado não carrega, então são commitados como `reach_manifest.STATIC_REACH`.
`--check` garante a regra, que toda op sem ponto de chamada no painel está em
`server/operations/dashboard.py` (com o motivo de não estar), e que o manifesto commitado
corresponde à árvore; `--write-manifest` o atualiza. `--call-counts --days 30` adiciona
a contagem de chamadas de cada op a partir de `loom.agent_events` (o único modo que se conecta ao ClickHouse),
e `--owners` indica o commit que introduziu a declaração de cada op experimental.

<a id="wire-changelog"></a>

### Changelog do protocolo

```bash
python scripts/codegen/wire_changelog.py <rev_a> <rev_b>           # markdown
python scripts/codegen/wire_changelog.py --since-hours 24 [rev_b]   # rev_a = rev_b's ancestor N hours back
python scripts/codegen/wire_changelog.py <rev_a> <rev_b> --json
python scripts/codegen/wire_changelog.py --snapshot <rev> > snap.json
python scripts/codegen/wire_changelog.py --diff a.json b.json
```

Ele materializa cada revisão com `git archive`, importa seu registro em um interpretador
filho e imprime as operações e os campos de requisição e resposta adicionados ou
removidos, além de mudanças de papel, orçamento e camada do MCP. Sua saída vai para as notas de versão
([processo de release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes)); a seção "o que mudou para os clientes"
é gerada, não escrita.

<a id="gen_py_library_methods"></a>

### Métodos da biblioteca Python

O conjunto de nomes de métodos da biblioteca padrão que o extrator Python usa fica congelado na árvore,
para que uma atualização do interpretador não mova as arestas de chamada. Regenerá-lo altera um módulo
do extrator, o que move o carimbo de versão do índice de código e refaz os chunks de todo namespace indexado; revise o diff e regenere somente quando for essa a intenção.

<a id="push_langfuse_prompts"></a>

### Envio de prompts ao Langfuse

O servidor vincula uma geração de LLM a uma versão de prompt do Langfuse buscando o hash de conteúdo
do prompt no lockfile commitado, sem nenhuma chamada em tempo de execução. Sem `--write`,
o script planeja contra o lockfile offline; `--write` envia os prompts alterados (exige
`LOOM_LANGFUSE_URL` e `LOOM_LANGFUSE_ADMIN_TOKEN`) e reescreve o lockfile. Um teste
unitário falha enquanto o lockfile não tiver um prompt registrado.

<a id="the-schema-dump"></a>

## O dump do schema

`schemas/schema.sql` é gerado por `scripts/ops/dump_schema.py` a partir de um ClickHouse com
todas as migrations aplicadas:

```bash
make schema-dump    # python -m scripts.ops.dump_schema
make schema-check   # --check: exit 1 with the head of the diff; writes nothing
```

Ele é abordado em [migrações](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md#schema-dump-and-check), pois acompanha
cada alteração de migration e deve ser gerado com a versão do ClickHouse fixada pelo CI.

<a id="snapshots-that-are-tests-not-generators"></a>

## Snapshots que são testes, não geradores

A superfície da CLI (`tests/unit/snapshots/cli_tree.json`, `cli_help.json`) e o registro de workers (`tests/unit/snapshots/worker_registry.json`) são fixados por testes unitários e regenerados por uma variável de ambiente na execução dos testes
([testes](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md#ci-gates)). A referência da CLI é renderizada a partir de `cli_tree.json`, então, depois de regenerar o snapshot da CLI, execute também `docs_reference.py`.

<a id="before-you-push"></a>

## Antes de fazer o push

```bash
make lint            # every --check above, plus ruff, the gates and Biome's dashboard accessibility rules
```

Quando houver uma falha, a mensagem indica o script a executar; execute-o, revise o diff e faça o commit do arquivo regenerado na mesma mudança que contém a sua fonte.

<a id="related"></a>

## Relacionados

- [Adicionando uma operação](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-an-operation.md): a op, o handler, as tabelas
  do SDK
- [Estilo da documentação](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md#generated-pages): quais páginas são
  geradas
- [Testes](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)
