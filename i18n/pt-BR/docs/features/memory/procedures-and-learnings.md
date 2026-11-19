<!-- i18n: source=docs/features/memory/procedures-and-learnings.md blob=156841cc5d4c9f9c0117a2c9ee4e789a7099714c model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md), which is authoritative.

<a id="procedures-and-learnings"></a>

# Procedimentos e aprendizados

Esta página trata do que um agente aprende ao executar tarefas, em oposição aos fatos que lhe são informados: procedimentos ("como fazer X", um grafo de etapas aprendido a partir de execuções reportadas), reflexões (lições escritas no fim de uma execução) e aprendizados (uma fila de revisão de fatos candidatos que um administrador promove). Os registros tipados estão em [escrita de memória](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#typed-records).

<a id="procedures"></a>

## Procedimentos

Um procedimento é um grafo direcionado: os nós são etapas ou chamadas de ferramenta, as arestas são as transições permitidas, e cada aresta carrega um texto que o agente lê (`condition`, `guidance`, `pitfalls`). O Loom nunca executa um procedimento. O agente executa e reporta; o Loom armazena o grafo, serve a parte relevante, aprende com os resultados e promove o que funciona.

Ele é armazenado como uma memória `procedural` sob a chave `procedure_<slug of name>`, com seu registro de execuções em `loom.procedure_runs`.

<a id="operations"></a>

### Operações

| Operação               | Papel  | O que faz                                                                                                                  |
| ---------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------- |
| `procedure.add`        | writer | Registra `name`, `situation` (com embedding para correspondência), `steps` (2–64), e opcionalmente `graph`, `tags`, `metadata`. |
| `procedure.match`      | viewer | Principais procedimentos para uma `situation` (e `goal`), com grafo, estatísticas de execução e `status` de confiança.     |
| `procedure.locate`     | viewer | O nó ativo e as transições até `hops` (padrão 2, no máximo 4) à frente.                                                    |
| `procedure.record_run` | writer | Reporta uma execução: ids dos nós visitados (`path`), `ok`, `note` opcional, custo.                                        |
| `procedure.evolve`     | writer | Incorpora as execuções registradas: caminho comprovado, becos sem saída, etapas propensas a falha, promoção a template, quarentena. |
| `procedure.revise`     | writer | Propõe uma nova topologia; o registro de execuções a aceita ou a reverte.                                                  |
| `procedure.fork`       | writer | Copia a forma de um procedimento de outro namespace legível, sem suas evidências.                                          |
| `procedure.list`       | viewer | Inventário por `prefix` de chave ou `tags`.                                                                                |

Listas de campos: [referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#procedure).

<a id="the-graph"></a>

### O grafo

- Tipos de nó: `step`, `action` (uma chamada de ferramenta; `tool` a nomeia), `memory`,
  `consolidation`, `decision`, `template_ref`.
- Tipos de aresta: `dep`, `dataflow`, `branch`, `converge`.
- `procedure.add` rejeita ids de nó duplicados, arestas para nós desconhecidos, laços para o próprio nó
  e ciclos sem saída.
- Limites: 256 nós, 1,024 arestas, 512 caracteres por atributo de aresta.
- Um procedimento registrado apenas com `steps` recebe um grafo linear com ids `n1`, `n2`, ….

```json
{
  "nodes": [
    { "id": "cash", "kind": "action", "tool": "check_cash_in_bank" },
    { "id": "forecast", "kind": "action", "tool": "cash_flow_forecast" },
    { "id": "raise", "kind": "action", "tool": "fund_raising_request" },
    { "id": "close", "kind": "action", "tool": "book_closing" }
  ],
  "edges": [
    { "src": "cash", "dst": "forecast", "kind": "dataflow" },
    {
      "src": "forecast",
      "dst": "raise",
      "kind": "branch",
      "condition": "projected runway below the safety buffer",
      "pitfalls": "do not stack a second request while one is pending"
    },
    { "src": "forecast", "dst": "close", "kind": "branch", "condition": "runway healthy" },
    { "src": "raise", "dst": "close" }
  ]
}
```

Uma linha `procedural` escrita com `memory.set` que não seja um corpo de procedimento (uma `situation` não vazia e pelo menos dois `steps`) é armazenada como `semantic`, com a tag `retyped_from:procedural`.

<a id="matching"></a>

### Correspondência

`procedure.match` classifica pela similaridade entre a `situation` do chamador e cada uma das armazenadas, em todos os namespaces do próprio chamador, até o limite de fan-out (`namespace_scope="all_mine"`). Ela pode adicionar:

| Termo                  | Efeito                                                                                                  |
| ---------------------- | ------------------------------------------------------------------------------------------------------- |
| Sobreposição de ferramentas | `recent_tools` favorece procedimentos cujos nós de ação as cobrem (`tool_boost`, padrão 0.2).      |
| Objetivo               | Com `goal`, situação e objetivo são pontuados separadamente e combinados por média geométrica.          |
| Utilidade              | `utility_weight` (padrão 0 = desligado) soma a taxa de sucesso suavizada do registro de execuções.      |

Toda correspondência carrega um `status` de confiança:

| Status        | Significado                                                                                                                       |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `template`    | Promovido: pelo menos 5 execuções bem-sucedidas, taxa de sucesso de pelo menos 0.8 e uma topologia que `evolve` não altera mais.  |
| `exercised`   | Tem pelo menos uma execução registrada.                                                                                           |
| `authored`    | Registrado e nunca executado.                                                                                                     |
| `candidate`   | Minerado por máquina e nunca executado: listado por último, rotulado `(unproven)` em `context.pack`, descartado por `include_candidates=false`. |
| `quarantined` | Suas últimas `strike_limit` execuções falharam; descartado a menos que `include_quarantined=true`. O próximo sucesso o libera.    |

`templates_only=true` retorna apenas templates. `for_agent` mostra as execuções do próprio agente chamador; `run_stats_by_agent` as detalha por agente.

<a id="localised-guidance"></a>

### Orientação localizada

`procedure.locate` recebe `memory_key` e `at` (o id do nó ou a ferramenta recém-usada) ou `recent_path`. Ele retorna o nó ativo e as transições à frente, cada uma com sua condition, guidance, pitfalls e com que frequência execuções anteriores a seguiram e tiveram sucesso, além de um texto `rendered` pronto para prompt. Com `recent_path`, ele marca as transições visitadas, define `looping` com os `exits` não tomados em caso de revisita, e anexa os `failure_notes` de outras execuções. Quando `at` não corresponde a nada, `matched=false` e `entry_nodes` indicam por onde começar.

<a id="learning-from-runs"></a>

### Aprendendo com as execuções

`procedure.record_run` acrescenta uma linha a `loom.procedure_runs`: o caminho, `ok`, `note`, a `revision` do grafo, `steps_taken`, `duration_ms` e `tokens`. `run.complete` também registra uma execução (com caminho vazio) para qualquer chave de procedimento em `memories_used`; um agente que chama `record_run` por conta própria deve deixar a chave fora de `memories_used` ([execuções](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)).

`procedure.evolve` incorpora as execuções mais novas que a marca d'água `ledger_folded_through`. Quando `min_runs` (padrão 3) é atingido, ele:

- segue as arestas com mais travessias bem-sucedidas como o caminho comprovado;
- marca como becos sem saída os nós de etapa e ação que nenhuma execução usou (reversível);
- marca como `fail_prone_steps` / `fail_prone_transitions` os nós e transições cuja taxa de falha excede claramente a taxa base do procedimento: leia-os como pitfalls;
- promove um procedimento que atinge o critério de template;
- coloca em quarentena aquele cujas últimas `strike_limit` execuções falharam
  (`LOOM_PROCEDURE_STRIKE_LIMIT`, padrão 3; 0 = desligado).

<a id="revisions-are-gated"></a>

### Revisões passam por um portão

`procedure.revise` recebe um `graph` completo e um `reason`, mantém as estatísticas dos ids de nó que sobrevivem e guarda o grafo anterior como `pending_revision`. As execuções seguintes carregam a nova revisão, e `procedure.evolve` as compara com as da revisão anterior:

| Portão                       | Parâmetros (padrões)                                                                                                                  |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `gate_mode="sprt"` (padrão)  | Teste sequencial da razão de probabilidades: `gate_min_drop` 0.15, `gate_alpha` 0.05, `gate_beta` 0.20, `gate_max_runs` 50, `gate_min_runs` 3. |
| `gate_mode="margin"`         | Uma `gate_margin` fixa sobre a diferença da taxa de sucesso.                                                                          |
| Custo                        | Uma revisão cujas execuções bem-sucedidas custam mais que `gate_cost_tolerance` (0.25) acima das da anterior é revertida (`gate_cost_min_samples`). |

O teste sequencial decide assim que a evidência é forte o bastante. Uma revisão revertida vai para `rejected_revisions`, e `procedure.revise` se recusa a propô-la novamente, a menos que `force=true`.

<a id="whose-procedures-a-pack-injects"></a>

### Quais procedimentos um pack injeta

`context.pack` e `memory.stack` injetam um procedimento correspondente como etapas a seguir; por isso, com autenticação ativa, eles carregam apenas procedimentos escritos pelo principal chamador (o `actor` da linha). O procedimento de um colega em um namespace compartilhado ainda é retornado por um `procedure.match` explícito, cujas correspondências carregam `actor`. Com a autenticação desativada, toda correspondência é injetada.

<a id="measuring-whether-a-procedure-helps"></a>

### Medindo se um procedimento ajuda

`context.pack` pode reter o procedimento mais bem correspondido com probabilidade `procedure_explore_rate` (padrão 0).
O pack lista os procedimentos exibidos em `procedure_keys` e os retidos em `procedure_keys_withheld`, de modo que os resultados das sessões possam ser comparados.

<a id="example"></a>

### Exemplo

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    added = await bot.procedure.add(
        "refund request",
        "a customer asks for a refund on a paid invoice",
        ["look up the invoice", "check the refund policy window", "issue or decline"],
        tags=["billing"],
    )
    hits = await bot.procedure.match(
        "customer wants money back for last month's invoice", goal="resolve a refund"
    )
    key = added["memory_key"]  # procedure_refund_request
    await bot.procedure.record_run(key, ["n1", "n2", "n3"], ok=True, duration_ms=42_000)
    await bot.procedure.evolve(key)
```

O `evolve` do SDK envia `gate_min_runs=5` e `gate_margin=0.05`, a menos que indicado de outra forma.
O `add` do SDK recebe apenas `steps`; registre um `graph` explícito via HTTP ou MCP.

`memory.delete` em uma chave de procedimento também exclui as linhas do seu registro de execuções.

<a id="reflections"></a>

## Reflexões

Uma reflexão é uma lição sobre uma execução (`what_worked`, `what_failed`, `lessons`, com
chaves `evidence`), armazenada como uma memória `reflection`. Ela recebe embedding, mas fica de fora do
recall sem filtros, porque é uma hipótese sobre o processo, não um fato. Leia as reflexões
com `memory.search(memory_type="reflection")`.

- `run.complete(reflection=...)` grava a lição do próprio agente ([execuções](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)).
- O worker `reflection_distill` (desativado por padrão; faz chamadas ao modelo) combina
  sessões que falharam com alta confiança com as sessões bem-sucedidas mais próximas e destila a diferença.

<a id="learnings-review-queue"></a>

## Fila de revisão de learnings

Um learning é um fato candidato com seus apoiadores (`supporter_count`, `supporters`,
`confidence_avg`, `rationale`, `source_kind`), aguardando a decisão de uma pessoa. O Loom
registra a escolha do revisor e nunca a toma. Qualquer candidato que não seja uma regra de um bot
(veja abaixo) é gravado pelo seu próprio processo em `loom.learnings_queue`.

A extração registra por conta própria um tipo de candidato. Com os learnings de turnos do assistente ativados
(`LOOM_MEMORY_EXTRACT_AGENT_LEARNINGS`, desativado por padrão), uma regra que um bot declara sobre o próprio
comportamento em uma conversa se torna um candidato `assistant_turn` para aquele agente
(`agent`), nunca uma memória ([extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#rules-the-assistant-states-about-itself)). A linha do
candidato contém a regra e nenhuma fala de usuário. Cada sessão que declara a regra adiciona uma linha
a `loom.learnings_supporters` com as citações do próprio usuário. Quando o candidato é listado,
`supporter_count` é o número de usuários finais distintos, `supporter_sessions` o de
sessões distintas, `supporter_writers` o de principals distintos que gravaram essas linhas (um token
não fixado a um usuário pode enviar qualquer `user_id`, então muitos usuários vindos de um mesmo writer contam como uma única
fonte), e `rationale` são as citações do apoiador restante mais antigo. O apoio nunca
o promove; promovê-lo publica a regra no conhecimento do namespace, com a tag
`agent:<agent>`. `context.pack` com `include_knowledge`, `memory.stack`, `loom_search`
e `memory.ask` a entregam às leituras daquele agente para todos os usuários e a omitem das leituras de
outros agentes. Via HTTP, uma leitura de todo o namespace (agente `*`, o padrão de `memory.ask`)
recebe as regras de todos os bots, e uma leitura sem agente (`-`, o padrão de `loom_search`)
não recebe nenhuma regra promovida para um bot nomeado. Via MCP, um `agent` não informado é o
do cliente (`claude-code`, ...), então essas leituras entregam as regras desse agente. Uma regra para
todos os agentes é entregue a toda leitura. Um token fixado a um usuário final
(`read_user_id`) atua como o agente que ele nomeia, nunca como o `agent` do corpo; qualquer outro token,
inclusive um token `own_user` sem fixação, atua como o `agent` do corpo. Um token que o Loom gera com uma
fixação `read_user_id` não nomeia nenhum agente, então não registra regra de nenhum bot e não recebe nenhuma regra
promovida para um bot nomeado. `knowledge.search` não é filtrado por agente: qualquer agente que
possa ler o namespace a encontra ali.

A exclusão de um usuário apaga as linhas de apoiador dele, e as citações junto. Um
candidato que fica sem apoiadores é excluído se estiver pendente. Se já tiver sido decidido, mantém
sua linha de status com a regra apagada, de modo que uma regra rejeitada nunca volte como pendente.

`learnings.quality` conta por padrão o seeder de conversas, pois ele informa a qualidade
da extração; passe `source_kind="assistant_turn"` para as regras de bots.

| Op                  | Papel  | O que faz                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `learnings.list`    | viewer | Candidatos por `status` (`pending` por padrão, `promoted`, `rejected`) e `source_kind`.                                                                                                                                                                                                                                                                                                                                                     |
| `learnings.review`  | admin  | `decision` `promote` ou `reject`, com `reason`. `destination`: `memory` (a zona compartilhada para um candidato de apoiadores, uma linha de namespace sob o `user_id` do aprovador para um fato originado de conversa, a linha de `user_id` '-' do agente para a regra de um bot), `knowledge` ou `both`; se não definido, `knowledge` para a regra de um bot e `memory` nos demais casos. Um candidato `definition` é recusado para `memory` e `both`: ele é promovido com `memory.promote`. |
| `learnings.quality` | viewer | Contagens de pendentes / promovidos / rejeitados por namespace e a taxa de aprovação.                                                                                                                                                                                                                                                                                                                                                       |

`learnings.list` e `learnings.review` existem apenas via HTTP e SDK (`bot.learnings`). Uma
promoção para `memory` carrega o id do candidato, então
[`memory.lineage`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#lineage) a rastreia até o candidato.
Revisar um candidato novamente com a decisão que ele já tem não faz nada; rejeitar um
candidato promovido o marca como rejeitado e deixa o que a promoção gravou (exclua o documento
`learning://<candidate_id>` de uma promoção de conhecimento com `knowledge.delete` para
parar de entregá-lo). A regra de um bot cujo último apoiador foi apagado responde 404, como
um candidato inexistente. Listas de campos:
[referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#learnings).

<a id="settings"></a>

## Configurações

| Configuração                          | Efeito                                                                                                                                                          |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_PROCEDURE_STRIKE_LIMIT`         | Execuções com falha consecutivas antes da quarentena ([Processos em segundo plano](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#workers)).                                                                |
| `LOOM_REFLECTION_DISTILL_*`           | O processo de destilação de reflexões ([Consolidação](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)).                                                           |
| `LOOM_MEMORY_EXTRACT_AGENT_LEARNINGS` | Desativado por padrão: a extração registra as regras do assistente sobre si mesmo como candidatos `assistant_turn` ([Extração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#extraction)). |

<a id="limits"></a>

## Limites

- **Evolve é explícito.** Nada consolida o ledger com base em um temporizador; chame `procedure.evolve`
  depois que as execuções forem registradas.
- **Um caminho deve nomear ids de nós reais** do grafo armazenado.
- **O grafo mantém os últimos 50 traces**; as estatísticas agregadas das execuções sobrevivem ao truncamento.
- **Procedimentos bifurcados começam sem evidências.**

<a id="related"></a>

## Relacionados

- [Execuções](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md): `run.complete`, `memories_used` e reflexões.
- [Memória de trabalho e perfis](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md): como o `context.pack`
  exibe procedimentos.
- [Namespaces e compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md): a zona compartilhada para a qual um candidato de supporters
  é promovido.
- [Conhecimento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md): para onde a regra de um bot é promovida por padrão.

Os rascunhos de definição criados a partir de lacunas de conhecimento e do histórico de consultas ficam visíveis apenas para os administradores do namespace, incluindo as contagens da fila e a linhagem. Aprovar um rascunho publica seu valor completo, incluindo o SQL de apoio, para os leitores do namespace. Consulte [rascunhos de
definição](../../architecture/definition-drafts.md) para a configuração da fonte, os conflitos de revisão e a promoção interrompida.
