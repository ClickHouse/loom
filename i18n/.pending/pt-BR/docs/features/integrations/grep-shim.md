<!-- i18n: source=docs/features/integrations/grep-shim.md blob=1ce5e2990b99fc9c2e3b50a80f6b06f33fa415f0 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md), which is authoritative.

<a id="the-grep-shim"></a>

# O grep shim

O grep shim é um wrapper de `grep`/`rg` que acrescenta à linha de cada ocorrência a função que a contém, obtida do índice de código; ele exige Node 18 ou posterior, um token de visualização (viewer) e um repositório no [índice de código](../code-index/overview.md). Ele é voltado a agentes que usam apenas o grep do shell, ou que rodam onde os hooks não funcionam (`claude --bare` ignora o desvio de código do [plugin do Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)). O agente, o prompt e o harness permanecem inalterados.

```text
$ grep -rn "getSettingsRef().max_threads" src/Processors
src/Processors/QueryPlan/ReadFromMergeTree.cpp:1466:    ... max_threads ...   ⟵ DB::ReadFromMergeTree::selectRangesToRead
src/Processors/Executors/PipelineExecutor.cpp:298:      ... max_threads ...   ⟵ DB::PipelineExecutor::initializeExecution
loom: 2/2 hits scoped to their enclosing function via code.enclosing, index at c7a9fe7c9f, 180 ms
```

<a id="install"></a>

## Instalação

```bash
export PATH=/path/to/loom/integrations/grep-shim/bin:$PATH
export LOOM_BASE_URL=https://loom.example.com   # or ~/.loom/config.json {url, token}
export LOOM_API_KEY=<viewer token>              # or ~/.loom/token
export LOOM_CODE_NAMESPACE=code-myrepo
export LOOM_AGENT=pr-analyst                    # optional: names the caller in the ledger
```

`which grep` deve apontar para o shim. Execute o agente dentro do checkout que o namespace indexa: os caminhos são tornados relativos a `git rev-parse --show-toplevel`. Uma divergência de branch aparece como ocorrências não resolvidas, não como ocorrências erradas.

O shim executa o primeiro `grep` ou `rg` no `PATH` que não seja ele mesmo (`LOOM_GREP_REAL` e `LOOM_RG_REAL` forçam um caminho).

<a id="guarantees"></a>

## Garantias

- **Cada byte que a ferramenta real imprime é impresso, na mesma ordem.** A única alteração é um sufixo opcional `   ⟵ <qualified name>` nas linhas de correspondência cuja função envolvente o índice conhece, e um resumo de uma linha no stderr.
- **O código de saída é preservado** (grep: 0 com ocorrências, 1 sem ocorrências, 2 erro).
- **Dois formatos são anotados:** `path:line:text` (vários operandos, `-r` em um diretório, `-H`) e `line:text` de exatamente um operando de arquivo (`grep -n pattern file.cpp`, o formato em que o agente se estabiliza quando já conhece o arquivo; o caminho é o operando, então `-h` não causa problema ali). Todo o resto passa sem alterações: sem `-n`, `-l`, `-L`, `-c`, `-o`, `-z`, `-Z`, `-b`, `-q`, `--color=always` (rg: `-N`, `--json`, `--files`, `-0`), stdin, ou vários arquivos com `-h`.
- **Um limite rígido de latência.** `LOOM_GREP_SHIM_TIMEOUT_MS` (padrão 2500). Qualquer erro, um timeout, a ausência de token, ou mais de 8 MB de saída (`LOOM_GREP_SHIM_MAX_BYTES`) imprime a saída bruta.
- **Carga limitada no servidor.** Uma chamada `code.enclosing` para cada 200 ocorrências distintas, no máximo 5 em paralelo; 1,000 ocorrências são anotadas e o restante é impresso sem anotação.

<a id="the-dependency-graph-block"></a>

## O bloco do grafo de dependências

`LOOM_GREP_SHIM_GRAPH=on` (padrão off) acrescenta um bloco após a saída real: `[Related from loom dependency graph]`, depois uma linha por função envolvente com seu módulo, até `LOOM_GREP_SHIM_GRAPH_K` (5) chamadores, chamados e testes, e uma nota `(lower bound: …)` onde o grafo está incompleto. Cortar a partir da linha de cabeçalho devolve os bytes reais. O bloco usa a mesma chamada `code.enclosing`, o mesmo timeout e as mesmas regras de fail-open; ele é descartado acima de `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS` (20) funções ou quando a saída foi transmitida em streaming (acima de 8 MB).

<a id="switches"></a>

## Chaves de configuração

| Variável                                | Padrão                                 | Efeito                                                                                  |
| --------------------------------------- | -------------------------------------- | --------------------------------------------------------------------------------------- |
| `LOOM_GREP_SHIM`                        | on                                     | `off` faz do shim um simples pass-through                                               |
| `LOOM_GREP_SHIM_QUIET`                  | unset                                  | defina para suprimir a linha de resumo no stderr                                        |
| `LOOM_GREP_SHIM_TIMEOUT_MS`             | 2500                                   | o limite de latência                                                                    |
| `LOOM_GREP_SHIM_MAX_BYTES`              | 8 MB                                   | a saída além disso passa sem alterações                                                 |
| `LOOM_GREP_SHIM_GRAPH`                  | off                                    | `on` acrescenta o bloco do grafo de dependências                                        |
| `LOOM_GREP_SHIM_GRAPH_K`                | 5                                      | vizinhos por função (1–20)                                                              |
| `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS`      | 20                                     | número de funções acima do qual o bloco é descartado                                    |
| `LOOM_CODE_NAMESPACE`, `LOOM_AGENT`     |                                        | o namespace de código; o nome do agente chamador (padrão `grep-shim`)                   |
| `LOOM_CONFIG_FILE`, `LOOM_API_KEY_FILE` | `~/.loom/config.json`, `~/.loom/token` | de onde a URL, o namespace e o token são lidos quando o ambiente não os define          |

<a id="server-side-codeenclosing"></a>

## Lado do servidor: `code.enclosing`

O shim é um cliente fino sobre uma única operação. `code.enclosing` (`POST /v1/code.enclosing`, viewer) recebe `locations: ["src/F.cpp:12", …]` (até 200) e retorna um resultado por localização, na ordem da entrada, com `symbol == ""` quando a linha está no escopo do arquivo ou o caminho não está indexado. Toda linha resolvida traz `symbol_id`.

- `include_body: true` (com `token_budget`, padrão 4000) coloca o corpo inteiro de cada função envolvente na linha, obtido uma vez por função distinta e preenchido na ordem de primeira aparição até o orçamento se esgotar; as demais trazem um `next` pronto para colar (`code.symbol(symbol_id=…)`). O shim deixa essa opção desativada.
- `neighbours: true` (com `neighbour_k`, padrão 5) retorna os dados do bloco do grafo: uma entrada por função envolvente distinta (no máximo 20; `anchors_capped` quando mais foram resolvidas) com `callers`, `callees` (camada do compilador preferida; símbolos fora do repositório e palpites do parser com confiança abaixo de 0.8 são descartados), `tests`, `module` e o envelope `epistemic` / `causes`.

`code.symbol` também aceita `name: "src/F.cpp:12"` para uma única localização, e o SDK Python expõe `agent.code.enclosing([...], include_body=True)`. Veja [operações do índice de código](../code-index/operations.md).

<a id="limits"></a>

## Limites

- **Apenas código integrado.** O índice conhece a branch que indexa; uma ocorrência em uma edição não commitada é resolvida contra a versão indexada do arquivo, ou não é resolvida.
- **Melhor esforço.** Qualquer situação inesperada imprime a saída bruta; uma linha sem anotação não significa que o grep falhou.
- **Mesmas buscas.** O agente continua usando grep. Um agente que pode chamar ferramentas MCP deve usar `code.search`, `code.callers` e `code.impact` diretamente.

<a id="related"></a>

## Relacionados

- [Visão geral do índice de código](../code-index/overview.md).
- [Plugin do Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md): o desvio de código baseado em hooks para agentes que executam hooks.
- [Visão geral das integrações](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md).
