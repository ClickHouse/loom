<!-- i18n: source=docs/features/runs.md blob=7aa894eee29c65b040ebfbad315df18b437034ae model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md), which is authoritative.

<a id="runs"></a>

# Execuções

Uma execução (run) é uma execução de uma tarefa, como "triage issue 1234". `run.begin` a abre e retorna um `run_id`; o
agente passa esse id nas suas escritas (`memory.set`, `memory.update`, `observe.record`); `run.complete` a encerra com
um status, as memórias nas quais o agente se baseou e, opcionalmente, uma reflexão. Um `run_id` no corpo de uma leitura (`memory.search`,
`memory.ask`) filtra apenas as linhas da própria execução, então as leituras são rotuladas de outra forma: pelo header `X-Loom-Run-Id` via HTTP
(`run_scope` no SDK Python) e pela própria execução da sessão via MCP. Uma memória em que uma execução bem-sucedida se baseou
fica mais bem ranqueada na próxima vez; uma em que uma execução com falha se baseou perde o impulso que havia ganhado.

<a id="what-a-run-records"></a>

## O que uma run registra

Uma linha de run fica em `loom.agent_runs`. A maior parte do que `run.get` mostra é derivada no momento da leitura a partir dos outros planos:

| Campo                              | Armazenado ou derivado | Fonte                                                                                                                                                                  |
| ---------------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `goal`, `user_request`, `model`, … | armazenado             | `run.begin`                                                                                                                                                            |
| `status`, `final_response`         | armazenado             | `run.complete` (`active` até lá)                                                                                                                                       |
| `memories_used`                    | armazenado             | `run.complete`: a declaração do próprio agente sobre no que ele se baseou                                                                                              |
| `grade`, `grade_source`, …         | armazenado             | `run.grade`, depois que a run foi encerrada                                                                                                                            |
| `memories_written`                 | derivado               | linhas em `loom.memory` com o `run_id` da run                                                                                                                          |
| `recalled`, `recalled_keys`        | derivado               | o log de rastreamento de busca sob o `run_id`, unido com `run.complete(recalled_keys)`                                                                                 |
| `observations`                     | derivado               | linhas episódicas de `observe.record` marcadas com o `run_id`                                                                                                          |
| `turns`, `cost_usd`                | derivado               | os turnos da sessão em `loom.agent_turn_usage` feitos enquanto a run estava aberta (gravados pelo plugin do Claude Code, ou por um agente com `cost.record_turns`)       |
| `budget_usd`, `budget_seconds`     | armazenado             | `run.begin` (0 = sem orçamento)                                                                                                                                        |
| `elapsed_seconds`, `over_budget`   | derivado               | do início da run até seu encerramento (ou até agora), no relógio do servidor, e `cost_usd`, em relação ao orçamento                                                    |

`recalled` é `-1` quando `LOOM_SEARCH_TRACE_LOG_ENABLED` está desativada e nenhum `recalled_keys` foi enviado.

<a id="budgets"></a>

## Orçamentos

`run.begin` aceita opcionalmente `budget_usd` (gasto com modelos) e `budget_seconds` (quanto tempo a execução pode levar). `run.get`
os compara com `cost_usd` e `elapsed_seconds` e lista em `over_budget` os que a execução ultrapassou
(`cost`, `time` ou ambos). O Loom não executa o agente, então um orçamento é um sinal para quem o executa, nunca uma interrupção.

O gasto de uma execução corresponde aos turnos da sua sessão feitos enquanto a execução estava aberta, segundo o horário de cada turno (`turn_at`, vindo da
transcrição), portanto `budget_usd` exige um `session_id` (caso contrário, 422). Ele é mantido com 6 casas decimais, como o custo, então o
menor valor é $0.000001. O tempo vai do início da execução até o seu fechamento, pelo relógio do servidor, independentemente do `duration_ms` que o
cliente tenha informado. Defina o orçamento no primeiro `run.begin` da execução.

O primeiro `run.get` de uma sessão que encontrar uma execução ativa acima do orçamento também traz a dica do mentor
`run_over_budget`, que os plugins repassam ao modelo como conselho. Uma execução encerrada ainda informa `over_budget`, mas não recebe
a dica. Como toda dica do mentor, ela é dita uma vez por sessão, e um chamador que não envia sessão (o SDK, HTTP simples) compartilha uma
por namespace e processo web, então decida com base em `over_budget`, não na dica.

Limites:

- **Apenas entre prompts.** O plugin do Claude Code envia os turnos de um prompt quando ele termina (um agente pode enviar os seus
  com `cost.record_turns`), então um orçamento de custo é verificado entre prompts, e um único prompt headless
  (`claude -p`) mostra o seu gasto só depois de terminar. Esses turnos ainda contam quando chegam depois do `run.complete`
  do próprio agente. O custo de uma chamada só é conhecido depois que ela retorna, então uma execução pode ultrapassar `budget_usd` pelo custo do
  último turno antes que algo perceba.
- **O relógio do cliente.** A janela compara os horários dos turnos do cliente com os horários da execução no servidor; um relógio com
  alguns minutos de diferença desloca os turnos nas bordas de uma execução. Um turno mantém o horário com que foi enviado, então um turno reenviado fica com
  a execução em que caiu da primeira vez. Um turno enviado sem horário (ou com um que não seja interpretável) assume o horário com que o seu
  `message_id` foi registrado, ou o momento atual se for novo; um reenvio feito a uma fração de segundo do primeiro ainda
  pode ser datado com o momento atual. `cost.record_turns` responde com `turns_dated_ahead`, os turnos recebidos com data mais de
  5 s à frente do relógio do servidor, de modo que um chamador que envia turnos por conta própria possa perceber um relógio adiantado; o plugin do Claude Code
  os envia sem ler a resposta.
- **Uma sessão, várias execuções.** Execuções abertas ao mesmo tempo em uma sessão veem os mesmos turnos: uma execução aninhada conta para ambas,
  e workers concorrentes que queiram cada um um orçamento de custo precisam de sessões próprias.
- **Modelos sem preço.** Um turno cujo modelo a tabela de preços não conhece custa 0; `run.get` os conta em
  `unpriced_turns`, e eles não contam para `budget_usd`.
- **MCP.** As ferramentas MCP oferecem apenas `budget_seconds`, já que o modelo não conhece o id da sessão sob o qual seus turnos
  são registrados.
- **Novas tentativas.** Um `run.begin` reenviado substitui a linha da execução como qualquer nova tentativa: ele mantém a sessão e o orçamento que
  não foram declarados de novo, um novo orçamento diferente de zero substitui o antigo, e 0 mantém o antigo.
  Um novo `budget_usd` deve ser enviado com o seu `session_id` (caso contrário, 422); um novo `budget_seconds` não precisa de mais nada.
  Um begin reenviado que move a execução para outra sessão descarta o seu orçamento de custo, que foi definido para os turnos da sessão
  antiga, mantém o orçamento de tempo e avisa isso no seu `next`. Essa é a única forma de remover um orçamento.
  Quando `run.begin` respondeu com um `run_id` diferente do enviado, reenvie esse `run_id`, ou o mesmo
  `session_id`; caso contrário, a nova tentativa abre uma nova execução.

<a id="recalled-is-not-relied-on"></a>

## Recuperado não é o mesmo que utilizado

Duas listas viajam em `run.complete`:

- **`memories_used`** — as chaves nas quais o agente de fato se apoiou. Cada uma recebe um evento de uso `used`, mais um evento `evidence` quando o resultado é positivo ou um evento `harm` quando é negativo. `outcome` tem seu valor padrão derivado de `status`: `completed` é positivo, `failed` é negativo e `cancelled` é nenhum.
- **`recalled_keys`** — as chaves que as ferramentas do Loom serviram durante a execução (os plugins do Claude Code e do Codex as guardam a partir dos resultados de `loom_search` e `memory.get` e as enviam no fim da sessão). Elas são registradas para `run.get` e nunca recebem crédito.

Uma linha que foi retornada, mas ignorada, não ganha nada; dar crédito a todo resultado de busca recompensaria o que quer que apareça primeiro no ranking.
O worker `usage_fold` incorpora os eventos ao termo de reforço pelo qual a [busca de memória](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) ordena os resultados. Um evento `harm` retira o reforço acumulado, com piso na base da linha definida no momento da escrita, de modo que uma única execução ruim nunca empurra uma linha para abaixo de sua relevância nem a exclui.

Uma chave de procedimento citada em `memories_used` também ganha uma linha no registro de procedimentos (path vazio, `ok` derivado do resultado); um agente que chama `procedure.record_run` por conta própria deve deixar essa chave fora de `memories_used`, para que a execução não seja contada duas vezes. Veja
[procedimentos e aprendizados](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md).

<a id="reflections"></a>

## Reflexões

`run.complete(reflection=...)` registra `what_worked`, `what_failed`, `assumptions`, `lessons` e `improvements` (até
20 itens cada), um `confidence` e as chaves de `evidence` em que a lição se baseia. Ela é armazenada como uma única memória do tipo
`reflection` em `reflection:<run_id>`:

- ela recebe embedding, então uma execução posterior pode encontrá-la com `memory.search(memory_type="reflection")`,
  mas fica fora da busca sem filtros e do `memory.ask`;
- `what_failed` e `lessons` exigem pelo menos uma chave de `evidence` (caso contrário, retorna 422);
- ela expira após `expires_in_days` (padrão 90, `0` = nunca), a menos que `memory.verify`
  ou um resultado positivo a renove;
- uma execução aberta com `role` (planner, executor, critic ou uma palavra sua) marca
  sua reflexão com `role:<role>`, de modo que o `memory.search(role=...)` de um agente com o mesmo papel dê preferência a ela.

<a id="what-happens-to-the-rows-a-run-wrote"></a>

## O que acontece com as linhas que uma execução escreveu

`retain` em `run.complete` decide o destino das memórias escritas durante a execução:

| `retain`         | Efeito                                                                                                                                                       |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `keep` (padrão)  | Nada muda.                                                                                                                                                   |
| `demote`         | A confiança delas é reduzida imediatamente.                                                                                                                  |
| `expire`         | Uma expiração daqui a `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS` (padrão 7) é definida com `on_expire=demote`; o worker `memory_expiry_sweep` a aplica.         |

As linhas fixadas (pinned), verificadas, promovidas, bloqueadas, episódicas e de reflexão são mantidas em qualquer modo, e nenhum modo marca nada com tombstone. O padrão vem de
`LOOM_MEMORY_RUN_COMPLETE_RETAIN`; a execução implícita de uma sessão MCP sempre usa keep, já que o transporte que a encerra não tem como avaliar o trabalho.

<a id="graded-runs"></a>

## Execuções avaliadas

`status` diz que a execução terminou; uma nota diz se o resultado dela se sustentou. Muitas vezes a nota chega dias depois, quando o CI passa ou um revisor rejeita o pull request. `run.grade` recebe `grade` (`pass` ou `fail`), um `source` (`maintainer`, `ci`, `reviewer`, … texto livre), um `reason` e um `ref` (uma URL, uma chave de achado, o id de uma execução de verificação), endereçados a um único `run_id` ou a todas as execuções encerradas de um `session_id` (por exemplo `pr_1234`). Uma reavaliação substitui o veredito; o mesmo veredito da mesma fonte não muda nada. Execuções ativas são ignoradas.

Toda memória em `memories_used` de uma execução avaliada é contabilizada na nota. Essas contagens são o que `memory.proposals` mostra ao lado de cada fato em teste (uma linha de `memory.set(proposed=true)`). O Loom decide uma proposta com base nelas somente quando o dono do namespace definiu `proposal_policy.mode` como `auto`; qualquer escritor decide com `memory.proposal_decide` em qualquer um dos dois modos. Veja [escrita de memória](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md).

`run.list(grade="ungraded")` lista as execuções encerradas que ainda aguardam um veredito.

Uma sessão MCP recebe uma execução implícita sem pedir: a primeira chamada de ferramenta com escopo de execução inicia uma, e `DELETE /mcp` ou o worker `mcp_session_run_sweep` a encerra (veja [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)). O sweep encerra uma execução ativa com `environment='mcp'` e `metadata.implicit` verdadeiro, cujo id é `mcp-` mais 40 caracteres hexadecimais (formato que só o transporte produz). Ele a encerra quando nenhuma chamada do token dela (de qualquer pessoa, no caso de uma execução que não registra token) a alcançou dentro de `LOOM_MCP_SESSION_RUN_IDLE_SECONDS`. A execução fica no namespace padrão do chamador, então o que seus leitores veem permanece ali: o objetivo dela é o rótulo `mcp session (<agent>)`, nunca o texto de uma chamada (a consulta de uma leitura pode alcançar qualquer namespace que o chamador lê), e `memories_written` lista apenas as escritas nesse namespace (as demais continuam levando o `run_id` dela). Encerrá-la não julga nada: o resultado é `none` e as memórias são mantidas, qualquer que seja o modo de retenção padrão do servidor, de modo que dois encerramentos concorrendo por ela (o sweep em dois hosts, um `DELETE`) no pior caso gravam a mesma conclusão duas vezes.

Como a execução se estende por processos web. O id dela é um hash da organização do chamador, do subject do token e do `Mcp-Session-Id`, de modo que todo processo do servidor nomeia a mesma execução para a sessão e nenhum outro token consegue nomeá-la enviando o mesmo id de sessão. Um processo registra a execução na primeira chamada admitida com escopo de execução da sessão e grava a linha de início em segundo plano (um insert aguardado seguraria essa chamada por segundos no ClickHouse Cloud); uma chamada que os gates recusam não grava linha de execução. Dois processos que não encontram linha gravam cada um um primeiro início, e vencem o início mais antigo e o seu dono (a versão de linha de um primeiro início conta em ordem decrescente a partir do começo; toda gravação posterior a substitui). Cada processo confere uma execução em cache com o ledger no máximo uma vez por minuto e a reabre quando um `DELETE` ou o sweep a encerrou nesse meio tempo. Um `DELETE` encerra a execução em qualquer processo que ele alcance, pelo id dela; o encerramento conta toda gravação sob esse id, seja quem for que a encerre. Um `DELETE` que alcança outro processo antes de a linha de início ter chegado não encontra nada para encerrar, e o início então grava uma execução aberta que o sweep encerra quando ela ficar ociosa: uma nova tentativa curta não cobriria um início que leva segundos, e atrasaria o `DELETE` de toda sessão que nunca iniciou uma execução. Uma execução reaberta mantém o `started_at` original, então uma execução encerrada pelo sweep e reaberta depois informa um `duration_ms` contado desde a primeira chamada da sessão, incluindo as lacunas. Uma verificação que não encontra linha alguma, porque uma exclusão por erasure a removeu, inicia uma nova execução começada naquele momento, de modo que nenhuma execução datada de antes da erasure volta.

<a id="operations"></a>

## Operações

| Operação       | Papel  | Faz                                                                                                                                                                                                                                                                                                   |
| -------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run.begin`    | writer | Abre uma execução: `goal` (obrigatório), `user_request`, `expected_outcome`, `run_id` (vazio ou `-`: gerado), `session_id`, `role`, `model`, `metadata`. Reenviar o mesmo `run_id` substitui a linha; um id no formato do transporte MCP (`mcp-` e 40 dígitos hexadecimais) recebe um id próprio. |
| `run.complete` | writer | Encerra a execução uma única vez: `status`, `memories_used`, `recalled_keys`, `outcome`, `reflection`, `retain`. Não é idempotente: concluir duas vezes acrescenta os eventos de uso duas vezes.                                                                                                      |
| `run.get`      | viewer | Uma execução com suas derivações ativas.                                                                                                                                                                                                                                                              |
| `run.list`     | viewer | Uma página de execuções, filtradas por `agent`, `session_id`, `status`, `grade`, `q` e `memory_activity` (`with` mantém as execuções que se apoiaram em uma memória ou escreveram uma, `without` as vazias; os filtros são aplicados antes de `limit`).                                              |
| `run.grade`    | writer | Avalia uma execução encerrada, ou as execuções de uma sessão, como `pass` / `fail`.                                                                                                                                                                                                                   |

`run.list` pagina com `order_by` (hora de início, duração ou memórias usadas, cada uma
em ordem crescente ou decrescente; `started_desc`, das mais recentes às mais antigas, por padrão) e `offset` (no máximo
100,000). `q` mantém as execuções cujo objetivo, id de execução ou agente contenha o texto, sem distinguir maiúsculas de minúsculas. `total`
conta todas as execuções que correspondem aos filtros e é nulo em uma página cheia depois da primeira, em que a página
sozinha não permite saber esse valor.

As listas completas de campos estão na [referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#run).
`memory.outcome_run` registra um resultado helped / hurt para cada memória recuperada em uma
execução, para chamadores que avaliam em lote em vez de nomear chaves.

<a id="example"></a>

## Exemplo

O `run.scope` do SDK Python abre a execução na entrada e a fecha na saída. As chaves que uma resposta de `memory.ask` citou dentro do bloco, mais as passadas para `used()`, se tornam `memories_used`; uma exceção fecha a execução como `failed` (resultado negativo) e depois é propagada. As escritas dentro do bloco carregam o id da execução.

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    async with agent.run.scope(goal="Answer ticket 4411: refund policy for annual plans") as run:
        answer = await agent.memory.ask("what is the refund policy for annual plans?")
        run.used("policy/refunds/annual")  # relied on beyond what the answer cited
        run.final_response = answer["answer"]

    replay = await agent.run.get(run.run_id)
    print(replay["run"]["status"], replay["memories_written"], replay["recalled"])

    # Later, once the customer confirmed the answer was right:
    await agent.run.grade("pass", run_id=run.run_id, source="support-lead")
```

Por HTTP, o ciclo de vida são duas chamadas:

```bash
curl -sX POST "$LOOM_BASE_URL/v1/run.begin" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","agent":"triage-bot","goal":"Answer ticket 4411"}'
# → {"run_id":"…","status":"active","started_at":"…","next":"…"}

curl -sX POST "$LOOM_BASE_URL/v1/run.complete" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","run_id":"…","status":"completed",
       "memories_used":["policy/refunds/annual"]}'
```

`Agent(auto_run=True)` (ou `LOOM_AUTO_RUN=on` no ambiente do cliente) abre uma execução de forma preguiçosa na primeira chamada ao plano de memória, encadeia todas as chamadas seguintes por ela e a conclui quando o `Agent` é fechado, com resultado `none`, de modo que nunca credita nem debita nada. Fica desativado por padrão porque uma escrita feita sob uma execução fica restrita a ela: a reafirmação de uma linha de outra execução é armazenada como `duplicate` em vez de ser mesclada.

<a id="settings"></a>

## Configurações

| Configuração                                                  | Efeito                                                                                                                         |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `LOOM_MEMORY_RUN_COMPLETE_RETAIN`                             | O modo `retain` usado quando uma chamada não especifica nenhum (`keep`).                                                       |
| `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS`                        | Quão adiante `retain=expire` define a expiração (7 dias).                                                                      |
| `LOOM_SEARCH_TRACE_LOG_ENABLED`                               | Registra cada busca sob seu `run_id`, do qual `recalled` é derivado. Desativado por padrão porque registra o texto da consulta. |
| `LOOM_USAGE_FOLD_ENABLED`                                     | O processo em segundo plano `usage_fold`, que transforma eventos de uso / evidência / dano em ranking.                         |
| `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`, `…_HALF_LIFE_DAYS` | Quanto, e por quanto tempo, a confiança de uso eleva uma linha na busca.                                                       |

Veja [configuração: deduplicação e expiração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#dedup-and-expiry),
[processos em segundo plano](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#workers) e
[busca de memória](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch).

<a id="limits"></a>

## Limites

- `memories_used` e `recalled_keys` aceitam no máximo 500 chaves cada; `goal` aceita no máximo 2,000
  caracteres.
- Uma chave em `memories_used` que não corresponde a uma linha ativa não rende nada;
  `used_applied` na resposta conta as que corresponderam.
- `run.complete` não é idempotente. Uma conclusão repetida conta os eventos de uso em duplicidade;
  conclua apenas uma vez.
- O `run.scope` do SDK grava o ledger em regime de melhor esforço: uma falha em `run.complete` nunca
  mascara a exceção do próprio bloco, então uma execução cuja conclusão falhou permanece `active`.
- `turns` e `cost_usd` valem 0 quando a execução não tem `session_id` ou o cliente não
  informa o uso de turnos. Eles contam apenas os turnos feitos enquanto a execução estava aberta.

<a id="related"></a>

## Relacionados

- [Rastrear e retomar execuções longas](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) — checkpoints na memória
  de trabalho indexados por `run_id` e como rastrear o que uma execução fez.
- [Buscar e perguntar](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) — como o reforço entra no ranqueamento.
- [Tarefas e decisões](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md) — `decision.outcome` credita as memórias
  em que uma execução de decisão se baseou.
- [Arquitetura de memória](../architecture/memory-architecture.md) — a dobra de eventos de uso
  e as tabelas por trás dela.
