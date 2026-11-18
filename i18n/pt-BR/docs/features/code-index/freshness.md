<!-- i18n: source=docs/features/code-index/freshness.md blob=9dcf44236c7be8f6d338e452c10b5a0be2e98f93 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md), which is authoritative.

<a id="code-index-freshness"></a>

# Atualização do índice de código

O índice fica atrás do repositório: ele indexa commits mesclados em uma branch configurada, passada por passada, e cada camada avança sua própria marca d'água. Verifique os metadados de atualização da resposta, quando presentes, e o status da camada relevante antes de confiar no código indexado.

<a id="the-as_of-block"></a>

## O bloco `as_of`

As operações que retornam `as_of` expõem os campos de atualização a seguir. As definições
comuns de `code.symbol` não incluem esse bloco nem um commit; em vez disso, inspecione
`code.index_status` e verifique a definição retornada em relação à revisão de código-fonte relevante.

| Campo           | Significado                                                                                                                                          |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `commit`        | o commit da camada rápida para este namespace (`git_head`), não a revisão de cada camada do compilador                                               |
| `branch`        | a branch que o namespace acompanha                                                                                                                   |
| `indexed_at`    | quando esse commit foi aplicado                                                                                                                      |
| `lag_commits`   | commits de primeiro pai entre `commit` e a ponta buscada do mirror; `-1` desconhecido                                                                |
| `stale`         | `lag_commits` > `LOOM_CODE_STALE_AFTER_COMMITS` (padrão 5), ou a busca do mirror está atrasada                                                       |
| `status`        | `fresh` (na ponta), `behind` (abaixo do limite de defasagem), `stale`, `unknown` (lag `-1`)                                                          |
| `stale_reason`  | `mirror_not_fetched` quando o lag é contado em relação a um mirror que parou de buscar                                                               |
| `fetched_at`    | junto com `stale_reason`: quando o mirror fez a última busca                                                                                         |
| `failing_tiers` | presente enquanto uma camada falha na entrada: `tier`, `kind`, `failing_since`, `failed_passes`, `fix` e, para uma camada com watermark, `lag_commits` |

O bloco fica em cache por 30 segundos por namespace; uma passagem que move `git_head`
o invalida. Ele é fail-open: quando a atualização não pode ser lida, o bloco fica vazio e
a resposta ainda é retornada. Trate um `as_of` vazio ou ausente como desconhecido.

Os fatos de uma camada com falha podem ficar atrás do head mesmo quando `stale` é false:
com `failing_tiers` apontando uma camada do compilador, trate as respostas de nível de compilador (grafos de chamadas exatos,
referências) como possivelmente desatualizadas e leia o código em que você confia.

**Verifique a atualização, não apenas o estado do build.** Um namespace cujo primeiro build terminou
ainda pode estar vários commits atrás. Quando `as_of` é retornado, inspecione `stale`; sem ele,
inspecione o watermark relevante por meio de `code.index_status`. Quando a atualização estiver stale ou
desconhecida, leia do seu checkout os arquivos que você está prestes a editar.

<a id="the-mirrors-own-age"></a>

## A idade do próprio espelho

O atraso é contado em relação ao espelho local do Loom, então ele indica 0 enquanto o espelho
deixou de buscar atualizações. Por isso `stale` também passa a ser verdadeiro quando a busca está atrasada, e as
ops de status informam a idade do espelho:

- `code.index_status` (um namespace) e `code.repos` (todas as entradas legíveis) retornam
  `clone_head`, `clone_fetched_at` e `fetch_overdue` (mais antigo que
  `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`, padrão 3600). Junto com eles: `git_head`,
  `fast_tier_lag`, `scip_lag` / `scip_py_lag`, a lista `compiler_tiers`,
  `last_refresh_at` e `chunks_total` / `chunks_embedded` / `embedding_coverage`.
- Ambas retornam `tier_health`: o último resultado registrado de cada camada (fetch, ingest,
  as camadas do compilador e a execução completa semanal de C++), com o tipo de erro, `failing_since`,
  `failed_passes` e a correção enquanto ela falha. Uma camada Go que deixou de fora módulos cujas
  dependências ela não consegue buscar neste host aparece como `state: "unavailable"` (kind
  `dependency_unavailable`, `failing_since` quando foi notada pela primeira vez, sem contagem de passes) com a sua
  correção: não está falhando e não está em `as_of.failing_tiers`.
- As linhas de `code.repos` trazem `searchable`: `embedding_coverage` atingiu
  `LOOM_CODE_READY_EMBED_COVERAGE`.

`compiler_tiers` tem um cartão por camada que a entrada executa (`producer`, `tool`, `label`,
`commit`, `lag`, `workers`). `code.index_status` responde de forma compacta via MCP e sempre que
`commit` está definido; `brief: false` retorna a resposta completa.

<a id="is-one-commit-in-every-tier"></a>

## Um commit está em todas as camadas?

Quem revisa um PR já mesclado precisa que todas as camadas contenham esse merge. `code.index_status` com
`commit=<sha>` responde em `commit_coverage`:

| Campo                   | Significado                                                                                          |
| ----------------------- | ---------------------------------------------------------------------------------------------------- |
| `ready`                 | verdadeiro quando a camada rápida e toda camada do compilador com watermark alcançaram o commit      |
| `waiting_on`            | as camadas que ainda não alcançaram                                                                  |
| `in_mirror`             | se o mirror contém o commit                                                                          |
| `fast`, `tiers[]`       | por camada: `covered` (nulo quando o mirror não tem o commit) e `commits_since`                      |
| `commits_since`         | quanto a camada rápida avançou além do commit                                                        |
| `paths_changed_overlap` | com `paths=[…]`: quais desses caminhos foram alterados por um merge posterior                        |

Os caminhos em `paths_changed_overlap` diferem no índice em relação ao commit; leia-os a partir do
seu checkout.

```python
st = await agent.code.index_status(namespace="code-myrepo", commit=merge_sha, paths=pr_files)
cov = st["commit_coverage"]
if not cov["ready"]:
    print("waiting on", cov["waiting_on"])
```

<a id="waiting-for-coverage-without-polling"></a>

## Aguardando a cobertura sem polling

`code.coverage.watch` publica um evento na caixa de entrada de tarefas (task inbox) assim que um commit é coberto:

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.coverage.watch" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","commit":"'"$MERGE_SHA"'","inbox":"<your-inbox>"}'
```

- `commit` é um SHA completo ou uma abreviação que o mirror resolve. `inbox` é uma caixa de entrada de tarefas (`inbox.register`). O chamador precisa de viewer no namespace de código e de writer no namespace dono da caixa de entrada.
- O worker `code_coverage_notify` (ativado por padrão, executado a cada `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS`, 60) publica um evento `code.coverage.ready` com `patch` `{namespace, commit, tiers: [{producer, label, watermark}]}`. Ele chega por `/v1/task/watch?inbox=…`, por `task.updates` e pelo webhook assinado da caixa de entrada.
- Um commit que já está coberto responde `ready=true` e `emitted=true` e publica o evento na mesma chamada.
- Um evento por (namespace, commit, inbox): seu `task_id` é o `event_id` da resposta, então uma nova tentativa nunca publica duas vezes. As entregas de um mesmo evento são executadas uma de cada vez, sob um lease sobre esse id; assim, entre duas entregas concorrentes, apenas a que armazenou o evento responde `emitted=true`. Um `code.coverage.watch` que entrega na mesma chamada e espera mais de 10 s por outra entrega responde 409 `PRECONDITION_FAILED`, e 503 quando o armazenamento de leases (Keeper) não responde; tente novamente em ambos os casos.
- `code.coverage.unwatch` (mesmos campos) remove um watch pendente; `removed: false` significa que não havia nenhum ativo.

O SDK Python tem `agent.code.coverage_watch(commit, inbox, namespace=…)` e `agent.code.coverage_unwatch(...)`. A caixa de entrada é descrita em [tarefas e decisões](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#task-inbox).

<a id="pushing-freshness-from-ci"></a>

## Enviando atualizações a partir do CI

O poller (`LOOM_CODE_INGEST_INTERVAL_SECONDS`, 120) captura todos os merges. Para indexar um
merge imediatamente, chame `code.refresh` a partir do job de pós-merge com um token de escrita restrito ao
namespace de código:

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.refresh" \
  -H "Authorization: Bearer $LOOM_CI_TOKEN" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","expected_head":"'"$GITHUB_SHA"'"}'
```

- A execução cobre apenas o namespace indicado, que deve ser uma entrada configurada (caso contrário, retorna 404), e indexa a branch configurada até o commit mais recente obtido.
- `expected_head` é apenas registrado em log. `wait=true` bloqueia até o fim da execução (até
  `wait_timeout_seconds`, padrão 300); a resposta traz `triggered`,
  `already_running`, `head_before` e `head_after`.
- A chamada é feita em regime de melhor esforço; o poller corrige um disparo perdido.

Os operadores acompanham `loom.code.tier_lag_seconds{tier}`; o runbook para camadas atrasadas está em
[operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md).

<a id="settings"></a>

## Configurações

| Configuração                                 | Padrão  | Efeito                                                                         |
| -------------------------------------------- | ------- | ------------------------------------------------------------------------------ |
| `LOOM_CODE_STALE_AFTER_COMMITS`              | 5       | commits de atraso antes de `as_of.stale` ser true                              |
| `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`        | 3600    | idade do mirror antes de `fetch_overdue` e `stale` (0 desativa a verificação)  |
| `LOOM_CODE_INGEST_INTERVAL_SECONDS`          | 120     | com que frequência `code_ingest` consulta o mirror                             |
| `LOOM_CODE_READY_EMBED_COVERAGE`             | 0.95    | proporção de embeddings antes de `code.search` usar sua etapa vetorial         |
| `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS` | 60      | com que frequência os monitores de cobertura são verificados                   |

<a id="limits"></a>

## Limites

- A atualização é por namespace de branch. Uma correção integrada à `main` não está em um namespace de branch de release até ser integrada nele; `code.grep mode=presence namespaces=[…]` verifica vários.
- Pull requests abertos e a sua árvore de trabalho nunca estão no índice.
- Não há push para avisar que "o índice avançou"; `code.coverage.watch` notifica a cada commit.

<a id="related"></a>

## Relacionados

- [overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md): como ler uma resposta, solução de problemas
- [operations.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md): as ops de status
- [arquitetura do code index](../../architecture/code-index-architecture.md): watermarks, reconciliação, o carimbo do extrator
