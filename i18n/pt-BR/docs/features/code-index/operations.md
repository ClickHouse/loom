<!-- i18n: source=docs/features/code-index/operations.md blob=0fe0101809c6f7360e6abcbf26ce81e8828343d5 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md), which is authoritative.

<a id="code-operations"></a>

# Operações de código

Esta página relaciona perguntas às ops `code.*`. Todos os argumentos e saídas estão na
[referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code) gerada, e na descrição MCP de cada op.

<a id="calling-a-code-op"></a>

## Chamando uma op de código

Todas as ops de leitura aceitam `org` / `namespace`. Quando uma chamada os omite, o transporte MCP usa
o header `X-Loom-Code-Scope: <org>/<namespace>` que os plugins de agentes de código enviam para o
checkout atual e, caso contrário, o namespace de código padrão do deployment
(`LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE`). Argumentos explícitos têm prioridade, e a autorização
é executada sobre o namespace informado em ambos os casos: um header que nomeia um namespace que o token não consegue
ler é recusado, nunca ampliado. Sem um padrão definido, uma chamada que não informa nenhum namespace é
recusada com um erro de validação.

Várias ops também aceitam `namespaces=[…]` (até 8) para consultar vários índices de branch de uma só vez.
Toda resposta que retorna resultados traz um `trace_run_id`; informe o que você fez com os
resultados por meio de `code.outcome` para que o ranking aprenda com isso.

A mesma chamada em cada superfície:

```bash
# HTTP
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.search" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"code-myrepo","query":"where are request quotas enforced?","top_k":5}'
```

MCP, a ferramenta `loom_code_search`:

```json
{ "query": "where are request quotas enforced?", "namespace": "code-myrepo", "top_k": 5 }
```

```python
# Python SDK
from loom import Agent

async with Agent("acme", "support", "review-bot") as agent:
    # agent.code.* sends no namespace: the server's default code namespace answers
    hits = await agent.code.search("where are request quotas enforced?")
    # any code namespace, through the generic call (fields win over the Agent's identity)
    hits = await agent.call(
        "code.search", namespace="code-myrepo", query="where are request quotas enforced?"
    )
    for u in hits["results"]:
        print(u["path"], u["start_line"], u["symbol"], u["tier"])
    await agent.code.outcome(hits["trace_run_id"], "used_in_answer")
```

**Listagem MCP.** O toolset `coding` lista `code.search`, `grep`, `enclosing`, `ask`,
`symbol`, `outline`, `callers`, `context_pack`, `impact`, `pr_check` e
`bisect_candidates`. O `core` lista apenas as oito primeiras com `LOOM_MCP_CODE_TOOLS=true`
(padrão desativado); o `tracker` lista `impact`, `pr_check` e `bisect_candidates`. Toda outra
op de código pode ser chamada pelo nome ou por meio de `loom_op`:
`loom_op {"op": "code.index_status", "arguments": {"namespace": "code-myrepo"}}`. As seguintes
são servidas apenas por HTTP e pelos SDKs: `code.outcome`, `code.refresh`, `code.reindex`,
`code.calibrate`, `code.context_packs`, `code.coverage.watch`, `code.coverage.unwatch`,
`code.scip_ingest`, `code.scip_status`. O nome da ferramenta MCP de uma op é `loom_` mais o nome dela
com os pontos trocados por sublinhados. Toolsets: [referência do MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).

**CLI.** Não há CLI de cliente para as ops de leitura. `loom code …` (`init`, `backfill`,
`reconcile`, `compact`, `reembed`, `backfill-prs`, `scip-full`, `scip-delta`, `warm`,
`describe`, `stats`, …) é a CLI de administração do servidor
([referência da CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md),
[operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)).

<a id="find"></a>

## Encontrar

| Op               | Use quando                                                                                                                                                                                                  |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.grep`      | você tem uma string exata. Literal por padrão, `regex=true` para RE2; `mode` = `matches`, `files`, `presence` (entre `namespaces`) ou `message` (uma mensagem impressa → a instrução que a imprime) |
| `code.symbol`    | você tem um nome: definições com assinatura e intervalo; `uses=true` acrescenta cada linha que o menciona, `near_paths` ordena várias definições pelos arquivos em que você está                          |
| `code.search`    | você tem uma pergunta em palavras. Recuperação híbrida de símbolo + lexical + vetorial; coloque em `pattern=` um identificador que você já tenha                                                          |
| `code.ask`       | você quer uma resposta sintetizada com citações (em cache por pergunta e invalidada quando um arquivo citado muda)                                                                                         |
| `code.files`     | você quer arquivos por nome, glob ou radical do nome base                                                                                                                                                   |
| `code.enclosing` | você tem localizações `path:line` (até 200) e quer a função que contém cada uma                                                                                                                             |
| `code.outline`   | você, de outro modo, leria um arquivo inteiro para ver o que há nele                                                                                                                                        |
| `code.map`       | você quer um mapa de orientação dos módulos do repositório                                                                                                                                                  |
| `code.trace`     | você tem um stack trace: cada frame resolvido para sua unidade                                                                                                                                              |

Um chamador com relógio próprio passa `deadline_ms` para `code.grep` ou `code.search`
(limitado a 200–60000; 0 significa nenhum, e, se não definido, vale `LOOM_CODE_SEARCH_DEADLINE_MS`):
a requisição para de iniciar novo trabalho quando o orçamento acaba e devolve o que tem,
`partial: true` com um `partial_reason` que nomeia o que não terminou (uma varredura do grep interrompida
pelo break-on-timeout do ClickHouse, uma perna da busca, rerank, HyDE), em vez de expirar por timeout.
Em `mode: "message"`, um padrão que não corresponde a nenhum template de instrução (uma linha de código em vez
de uma mensagem renderizada) passa por grep mais uma vez como literal e a resposta traz
`fallback: "literal"`.

**Exemplos.**

```bash
# Which release branches contain this string? One call, one row per namespace.
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.grep" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","pattern":"retry budget exhausted","mode":"presence","namespaces":["code-myrepo","code-myrepo-2-4"]}'
```

```python
d = await agent.code.symbol("QuotaLimiter::check", uses_brief=True, include_body=True)
located = await agent.code.enclosing(["src/quota/limiter.py:88", "src/api/routes.py:140"])
```

Campos de `code.search` que vale conhecer: `path_prefix`, `path_globs` / `exclude_globs`
(rg `-g`), `langs` (rg `-t`), `pattern` (restringe as duas pernas a chunks que o contêm),
`bodies: "none"` (apenas intervalos e assinaturas, a resposta mais barata), `token_budget`,
`expand` (um salto de LLM para ligar a redação aos identificadores) e `cursor` para a próxima página.

<a id="structure-and-change"></a>

## Estrutura e mudança

| Op                      | Use quando                                                                                                                                    |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.callers`          | quem chama isto, `depth` saltos; uma sobrescrita também informa os chamadores da declaração base                                              |
| `code.callees`          | o que isto chama, `depth` saltos                                                                                                              |
| `code.impact`           | raio de impacto de um símbolo, ou de `files` / um `diff`: chamadores, testes que alcançam a mudança, parceiros de co-change                   |
| `code.test_gate`        | quais testes uma mudança exige, e quais funções alteradas nenhum teste alcança                                                                |
| `code.tests_for`        | quais testes cobrem algo                                                                                                                      |
| `code.test_home`        | onde um novo teste deve ficar                                                                                                                 |
| `code.coverage_claim`   | "isto já está testado?"                                                                                                                       |
| `code.verify`           | verifica uma afirmação de forma fechada ("A chama B", "X sobrescreve Y") contra o índice                                                      |
| `code.verify_citations` | resolve toda citação `path:line` / de símbolo em um rascunho antes de enviá-lo                                                                |
| `code.path_guards`      | as condições que estão no caminho de chamada entre dois símbolos                                                                              |
| `code.examples`         | pontos de chamada reais de um símbolo                                                                                                         |
| `code.setting_facts`    | o que a árvore diz sobre uma configuração do ClickHouse (declaração, histórico de mudanças, randomização no CI); só faz sentido para uma árvore de código do ClickHouse |

`code.callers` / `code.callees` aceitam `min_confidence` para descartar arestas da camada do parser abaixo de
um grau (as arestas do compilador são sempre 1.0) e `include_ambiguous` para adicionar os pontos de chamada que o
resolvedor recusou, em um bloco separado `candidate_sites`. Leia `epistemic` e `causes`
na resposta ([overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#reading-an-answer)).

**Exemplo: impacto de um diff.**

```python
import subprocess

diff = subprocess.run(["git", "diff", "origin/main"], capture_output=True, text=True).stdout
r = await agent.code.impact(diff=diff, exclude_authors=["me"])
print(r["epistemic"], r.get("causes"))
for row in r.get("co_change_missing", []):
    print("usually changes with this diff:", row)
```

A forma com diff retorna os símbolos que os hunks sobrepõem, os testes que os alcançam e
`co_change_missing`; `files` sozinho trata toda função desses arquivos como alterada.
As duas formas retornam `owners`: autores de PR ponderados por recência sobre os caminhos
de origem, com `exclude_authors` (normalmente o próprio autor da mudança) removidos. O índice contém
código já integrado, então o diff é lido contra o head do branch; suas funções não commitadas só são resolvidas
quando já existem lá.

<a id="history-prs-and-releases"></a>

## Histórico, PRs e releases

Estas operações leem o plano de PRs que o worker `code_pr_history` preenche a partir do GitHub
(`LOOM_CODE_PR_HISTORY_ENABLED`). O histórico de um namespace de branch de release contém os PRs
mesclados nessa branch. Como o plano é preenchido, e bisect e backports em detalhe, estão
em [history-and-prs.md](history-and-prs.md).

| Op                       | Use quando                                                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code.context_pack`      | briefing de um PR (`pr_number`), de uma mudança que você está prestes a fazer (`task`) ou do repositório; `sections` mantém apenas os blocos em que você vai agir |
| `code.history`           | PRs mesclados que tocaram um símbolo ou um caminho                                                                                                |
| `code.symbol_history`    | PRs mesclados que alteraram um símbolo, entre duas releases                                                                                       |
| `code.blame_range`       | PRs mesclados por trás de um intervalo de linhas                                                                                                  |
| `code.review_brief`      | o que os revisores verificaram em um PR mesclado                                                                                                  |
| `code.conventions`       | as verificações de revisão recorrentes do repositório, a partir de um PR                                                                          |
| `code.pr_check`          | antes de abrir um PR: já existe um PR como este (mesclado, aberto ou fechado)?                                                                    |
| `code.bisect_candidates` | quais PRs mesclados podem ter introduzido um bug, a partir de frames, caminhos, símbolos e uma janela good/bad                                    |
| `code.backport_gap`      | quais branches de release ainda carregam o bug que uma correção removeu                                                                           |
| `code.backport_gaps`     | o relatório do conjunto todo de correções marcadas para backport que alguma branch não tem                                                        |
| `code.first_build`       | qual build de CI carregou um commit primeiro                                                                                                      |

**Exemplo: verificação de duplicidade antes de um PR.**

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.pr_check" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"code-myrepo","title":"Retry quota refresh on 503","paths":["src/quota/limiter.py"],"symbols":["QuotaLimiter::refresh"]}'
```

`verdict` é `duplicate_of` (um PR aberto está fazendo isso, ou um mesclado já fez), `related`
ou `none`; os itens trazem estado, pontuação e linhas de evidência. A verificação é heurística e não faz
nenhuma chamada ao GitHub; um título sozinho nunca é tratado como duplicata.

<a id="status-and-administration"></a>

## Status e administração

| Op                      | Papel  | Retorna                                                                                                                                                                  |
| ----------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `code.repos`            | viewer | entradas legíveis com atualidade, acesso e etapa de bootstrap; `repo` / `repos` restringem a um checkout (um fork do GitHub resolve para o índice do repositório pai)    |
| `code.index_status`     | viewer | marcas d'água, atrasos, camadas, `commit_coverage` ([freshness.md](freshness.md#is-one-commit-in-every-tier))                                                            |
| `code.coverage`         | viewer | arquivos, chunks, símbolos por camada, arestas por linguagem, e `gaps` — o que o índice não conhece                                                                      |
| `code.index_errors`     | viewer | arquivos em que o indexador falhou                                                                                                                                       |
| `code.recent_commits`   | viewer | commits ingeridos recentemente                                                                                                                                           |
| `code.context_packs`    | viewer | packs de PR armazenados                                                                                                                                                  |
| `code.outcome`          | viewer | registra o que você fez com um resultado (`opened_file`, `used_in_answer`, `fell_back_grep`, `ignored`)                                                                  |
| `code.coverage.watch`   | viewer | um evento de tarefa `code.coverage.ready` na sua inbox quando um commit passa a ser coberto; exige writer no namespace dono da inbox                                      |
| `code.coverage.unwatch` | viewer | remove uma observação pendente                                                                                                                                           |
| `code.scip_status`      | viewer | o estado de um delta da camada do compilador enviado                                                                                                                     |
| `code.refresh`          | writer | dispara uma passagem de ingestão agora ([freshness.md](freshness.md#pushing-freshness-from-ci))                                                                          |
| `code.scip_ingest`      | writer | envia um delta da camada do compilador de um produtor externo (aplicado por `code_scip_apply`)                                                                           |
| `code.reindex`          | admin  | move a marca d'água de ingestão para um commit                                                                                                                           |
| `code.calibrate`        | admin  | reajusta o piso de ruído e o limiar de abstenção da busca                                                                                                                |

Adicionar, ramificar, restringir e remover repositórios (`code.repo.add`,
`code.repo.remote_branches`, `code.repo.add_branch`, `code.repo.set_access`,
`code.repo.set_source`, `code.repo.remove`) estão em
[repositories.md](repositories.md).

<a id="related"></a>

## Relacionados

- [overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) — camadas, suporte a linguagens, como ler uma resposta
- [freshness.md](freshness.md) — `as_of`, cobertura de commits, atualização por CI, coverage watches
- [history-and-prs.md](history-and-prs.md) — o plano de PRs, bisect, backports
- [Referência do SDK Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md), [Referência do MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)
- [Referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code) — argumentos gerados
