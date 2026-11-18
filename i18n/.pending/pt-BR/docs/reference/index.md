<!-- i18n: source=docs/reference/index.md blob=fb131cfc15bed6b11b4ba0dd63862e173de7bcf3 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md), which is authoritative.

<a id="reference"></a>

# Referência

As páginas de referência dizem o que existe e o que cada opção significa. As razões por trás de um design estão em
[Conceitos](../concepts/memory.md) e [Arquitetura](../architecture/system-overview.md).
Os procedimentos estão nas seções de [guias](../guides/agent-integration.md) e
[operações](../operations/deployment.md).

<a id="generated-from-the-code"></a>

## Gerado a partir do código

Estas páginas, ou as tabelas nelas, são renderizadas a partir do código-fonte que as define. Cada
gerador tem um modo `--check` que o `make lint` executa, de modo que uma página que se desvia do código faz o
CI falhar. Edite a fonte e gere novamente; nunca edite à mão uma tabela gerada.

| Página                              | O que ela lista                                                                                                              | Fonte                                                                                | Gerador                                        |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------- |
| [Configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) | Cada configuração `LOOM_*`, seu valor padrão e significado, por área.                                                        | os comentários acima dos campos em `server/settings/`                                | `python scripts/codegen/settings_reference.py` |
| [Operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)        | Cada operação, agrupada, com função, tipo, listagem `all` do MCP e flag de auditoria; além de como o registro funciona.      | `server.operations.registry.ALL_OPERATIONS`                                          | `python scripts/codegen/docs_reference.py`     |
| [Processos de segundo plano](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md) | Cada processo em segundo plano, seu interruptor, valor padrão e cadência; além de como o supervisor os executa.               | `server/workers/registry.py` (`_WORKERS`)                                            | `python scripts/codegen/docs_reference.py`     |
| [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)                     | Cada comando e subcomando `loom` com suas flags; além de onde a CLI é executada e os comandos comuns.                        | a árvore de parsers do `loom` (`tests/unit/snapshots/cli_tree.json`)                 | `python scripts/codegen/docs_reference.py`     |
| [Erros](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)                | Cada código de erro com que o servidor responde; além do envelope, das regras de nova tentativa e das correções para os códigos comuns. | `server/core/errors.py` e cada literal `"code"` em `server/`                         | `python scripts/codegen/docs_reference.py`     |
| [Situação](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)             | Quantas operações de cada grupo são estáveis, cada operação experimental e cada configuração booleana que carrega como falsa. | `maturity` da operação no registro e os campos booleanos de `server/settings/catalog.py` | `python scripts/codegen/docs_reference.py`     |
| [Esquema](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)               | Cada tabela e view materializada com engine, chaves, TTL e índices; além das convenções de colunas.                          | `schemas/schema.sql`                                                                 | `python scripts/codegen/docs_reference.py`     |

Nas páginas do `docs_reference.py`, somente as regiões entre os marcadores
`<!-- BEGIN GENERATED: … -->` e `<!-- END GENERATED: … -->` são geradas; a prosa
ao redor é escrita à mão. As tabelas de operações dentro dos pacotes do SDK (`sdk/python/loom/ops.py`,
`sdk/typescript/src/generated/ops.ts`) também são geradas, por
`scripts/codegen/sdk_py_gen.py` e `sdk_ts_gen.py`; veja
[Geração de código](../development/code-generation.md).

<a id="written-by-hand"></a>

## Escritas à mão

| Página                                        | O que ela cobre                                                                                                                                                    |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)                     | Todas as rotas (ops, descoberta, health, streams, login, OAuth, painel, operador), a ordem dos gates, cabeçalhos, campos desconhecidos, idempotência, paginação, limites de taxa. |
| [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)                               | O endpoint `/mcp`, autenticação e OAuth, sessões e identidade, a lista de ferramentas e toolsets, resultados, erros, a exportação do action-schema.                |
| [SDK Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)                 | Instalação, construção, chamada de ops, formatos tipados, sessões e ferramentas, adaptadores de frameworks, erros, novas tentativas.                               |
| [SDK TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md)         | Instalação, clientes e opções, ops tipadas, sessões e ferramentas, paginação, o adaptador do Strands, erros, novas tentativas.                                     |
| [Variáveis de ambiente](environment.md)     | Variáveis lidas fora do modelo de configurações: chaves de provedores, a imagem, arquivos compose, a CLI, os SDKs e os scripts do operador.                        |
| [Superfície SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)             | As views somente leitura `sql_v1_*` sobre os dados de um tenant: colunas, regra de compatibilidade, o que elas omitem, atualização dos dados, `loom sql-access`.  |
| [Contrato do extrator](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md) | Versão 1 do contrato HTTP que um serviço extrator expõe: capacidades, extração, códigos de erro, os campos e as regras do artefato.                                |

As páginas escritas à mão citam apenas coisas que existem na árvore e apontam para o código-fonte que descrevem.

<a id="which-page-answers"></a>

## Qual página responde…

- "Como esta op se chama, e quem pode chamá-la?" → [Operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md).
- "O que esta configuração faz?" → [Configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md).
- "O que este código de erro significa, e devo tentar novamente?" → [Erros](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).
- "Esta op é estável ou experimental, e esta configuração vem ativada por padrão?" → [Situação](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
- "Qual header, qual rota, como faço a paginação?" → [API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md).
- "Por que meu cliente MCP não lista uma ferramenta?" → [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).
- "Este worker está em execução, e como faço para ativá-lo?" → [Processos worker](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md).
- "Meu cliente vai quebrar no próximo deploy?" → [campos desconhecidos e removidos](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields) e [como descobrir o que um deployment serve](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves).
- "O que há nesta tabela?" → [Esquema](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md).
- "O que `loom <command>` aceita?" → [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md).
