<!-- i18n: source=docs/guides/performance-tuning.md blob=285ede1bf84d35f39aa2de165f838e15263c115e model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md), which is authoritative.

<a id="tune-looms-performance"></a>

# Ajuste o desempenho do Loom

Cada ajuste desta página é uma configuração ou um campo de requisição. A
[referência de configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) informa o valor padrão e o
significado de cada configuração. Quando um trade-off é medido, a página indica o comando que o mede.

<a id="measure-first"></a>

## Meça primeiro

| Pergunta                                           | Comando                                                                               | Requer                                               |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| p50 / p95 / p99 e RPS de uma op com concorrência N | `make bench OP=memory.search C=5 D=60` (`scripts/ops/benchmark.py`)                   | um servidor em execução; as escritas vão para um namespace `bench-…` |
| Uma mudança de ranking ou de embedder prejudicou o recall? | `make bench-quick` (uma fatia de um benchmark público de memória de longo prazo, com um juiz de respostas) | provedores reais, `OPENAI_API_KEY`                   |
| Recall em relação à linha de base registrada       | `make bench-quality-gate MIN_RECALL=…`                                                | provedores reais                                     |
| modo de busca × rerank × largura do pool           | `make bench-matrix` (escreve `bench/MATRIX_RESULTS.md`)                               | um servidor em execução                              |
| Regressões de extração e de extração de grafo      | `make bench-algorithm-gate`                                                           | um servidor em execução                              |
| Símbolo, chamadores, recall e custo em tokens do plano de código | `make bench-code-intel`, `make bench-code-legs`                         | um embedder real                                     |
| O que mudou após um deploy                         | `loom deploy-delta` (formatos do ledger e das instruções do ClickHouse em cada lado da troca) | `var/deployed.json`                                  |

> **Aviso:** um número de recall medido com o embedder fake não diz nada sobre a recuperação real. Por isso os quality gates ficam fora do CI. Execute-os contra um servidor com os provedores que você vai usar em produção e refaça a linha de base após qualquer mudança de embedder.

Para uma única requisição lenta, leia o cabeçalho `Server-Timing` (`app` vs `ch`) e as linhas de `system.query_log` antes de mexer em qualquer configuração ([depuração](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)). O custo por operação em todo o deployment é uma única consulta sobre `log_comment`:

```sql
SELECT splitByChar(' ', log_comment)[1] AS source,
       count() AS queries, sum(read_bytes) AS bytes, sum(query_duration_ms) AS ms
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 1 HOUR AND log_comment != ''
GROUP BY source ORDER BY ms DESC LIMIT 20;
```

O tráfego de benchmark logo após uma troca lê caches frios; espere antes de comparar a latência de um deploy com a do build anterior.

<a id="ranking-and-thresholds"></a>

## Ranking e limiares

`memory.search` ranqueia um conjunto de candidatos da primeira etapa e depois aplica termos de ranking e gates
([recuperação e ranking](../concepts/retrieval-and-ranking.md)). A maioria dos termos pode ser definida por
deployment e sobrescrita por requisição, de modo que uma mudança possa ser testada em A/B primeiro em um único chamador.

| Ajuste                             | Configuração (campo da requisição)                                                                                                                                      | Trade-off                                                                                                                      |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Gates de similaridade              | `LOOM_ADAPTIVE_THRESHOLDS` (padrão: ativado)                                                                                                                            | Ativado: os gates são derivados, na inicialização, da distribuição de cosseno do próprio embedder. Desativado: você controla os gates escalares abaixo. |
| Gates escalares (fallback)         | `LOOM_MEMORY_SEARCH_MIN_SIMILARITY`, `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`, `LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD`, `LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD` | Ajustados para `text-embedding-3-small`; usados com embedders falsos, sonda com falha ou adaptatividade desativada.            |
| Resultados abaixo do piso          | `LOOM_MEMORY_SEARCH_FLOOR_MODE` (`floor_mode`)                                                                                                                          | `readmit` preenche `top_k` e marca com `below_floor`; `ids_only` preenche com corpos vazios; `drop` retorna menos resultados. |
| Conjunto de candidatos             | `LOOM_MEMORY_SEARCH_POOL_MULTIPLIER_STANDARD`, `_RERANK`, `_DEEP`, limitado por `LOOM_MEMORY_SEARCH_MAX_POOL_TOP_K`                                                     | Um conjunto maior aumenta o recall e a entrada do rerank; custa linhas vetoriais lidas e tokens de rerank.                     |
| Pular o rerank                     | `LOOM_LLM_RERANK_SKIP_ABOVE` (fallback quando o gate adaptativo não tem sonda)                                                                                          | Pula a chamada de rerank quando o melhor resultado vetorial já é forte.                                                        |
| Cascata de fallback                | `LOOM_MEMORY_SEARCH_FALLBACK_MAX_ROUNDS`, `_MAX_WALL_MS`, `_MAX_LLM_CALLS`, `_MAX_VECTOR_QUERIES`                                                                       | Limita as novas tentativas que uma primeira passada fraca pode gastar (reescrita da consulta, HyDE).                           |
| Reforço                            | `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` (`reinforcement_weight`), `_HALF_LIFE_DAYS`                                                                                   | Quanto os acessos e os resultados positivos elevam uma linha; 0 desativa. Limitado abaixo da relevância.                       |
| Proveniência                       | `LOOM_MEMORY_SEARCH_TRUST_WEIGHT` (padrão: 0)                                                                                                                           | Um termo limitado que favorece fontes atestadas.                                                                               |
| Recência                           | `LOOM_MEMORY_SEARCH_RECENCY_POOL_WEIGHT` (`recency_pool_weight`)                                                                                                        | Reordena o conjunto em direção a linhas recentes em uma consulta com intenção de recência; 0 = desativado.                     |
| Perna lexical em consultas com formato de código | `rrf_adaptive_lexical` (requisição), `LOOM_MEMORY_RRF_CODE_SHAPE_LO` / `_HI`                                                                                | Reduz o peso da perna lexical para consultas com formato de caminho ou de código sob RRF.                                      |
| Modo padrão                        | `LOOM_MEMORY_SEARCH_DEFAULT_MODE` (`search_mode`)                                                                                                                       | Usado quando o chamador não informa nem um modo nem uma receita.                                                               |
| Duplicatas por chave               | `LOOM_MEMORY_SEARCH_PER_KEY_CAP`                                                                                                                                        | Linhas que uma chave pode contribuir para uma página.                                                                          |

> **Aviso:** um limiar ajustado em um embedder está errado no seguinte. Com
> `LOOM_ADAPTIVE_THRESHOLDS=false`, ou com um embedder falso, execute o bench novamente (`make bench-quick`,
> `make bench-quality-gate`) após cada troca de embedder. `calibration.get` mostra qual
> camada produziu os gates em vigor para um namespace.

<a id="vector-reads"></a>

## Leituras vetoriais

As colunas vetoriais são `Array(BFloat16)`; onde `enable_quantized_codec` pode estar ativado, o migrator as cria codificadas ([ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)).

| Ajuste                                  | Configuração                                                                                                | Contrapartida                                                                                                                                                                                                                                                                                                                                                                                  |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Varredura primeiro pelos códigos        | `LOOM_VECTOR_SEARCH_QUANTIZED_CODES` (padrão: ativado), `LOOM_MEMORY_SEARCH_FACT_CODES_FIRST` (`auto`)      | Lê os códigos quantizados e depois refaz a pontuação com precisão total. `auto` desativa essa opção enquanto `memory_embeddings` tiver partes de patch, caso em que a nova pontuação seria ignorada.                                                                                                                                                                                           |
| Busca excedente de códigos              | `LOOM_VECTOR_SEARCH_QUANTIZED_FETCH_MULTIPLIER`, `LOOM_MEMORY_SEARCH_FACT_CODES_FETCH_MULTIPLIER`           | Linhas mantidas pela distância dos códigos antes da nova pontuação. Um valor menor é mais barato e altera os melhores resultados com mais frequência.                                                                                                                                                                                                                                         |
| Threads por instrução de fatos          | `LOOM_MEMORY_SEARCH_FACT_LEG_MAX_THREADS` (0 = sem fixação)                                                 | Fixar limita a CPU e o pico de memória por instrução, ao custo de alguma latência.                                                                                                                                                                                                                                                                                                             |
| Leituras de pares nas gravações em lote | `LOOM_MEMORY_BULK_PEER_READ` (padrão: `batched`; `per_fact`)                                                | `per_fact` lê primeiro pelos códigos os pares de conflito e de atualização de valor de cada fato do lote, como `memory.set` faz: para namespaces grandes, em que a instrução de pares em lote falha com `MEMORY_LIMIT_EXCEEDED` (`batched conflict candidates failed` no log do servidor) e uma entrada é gravada sem verificação quando o fallback por fato também falha. Um par pode diferir quando as distâncias empatam. |

Para mover uma tabela existente para um codec ou layout, use `scripts/ops/vector_codec_rebuild.py` (tabela gêmea, verificações de consistência e de recall, `EXCHANGE`; [passos](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#recommended-clickhouse-settings)), nunca um `ALTER` em uma migração.

<a id="clickhouse-connections-and-deadlines"></a>

## Conexões e prazos do ClickHouse

| Ajuste                    | Configuração                                                                                                                                                                        | Compromisso                                                                                                                                                                                                                                                    |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tamanho do pool           | `LOOM_CH_POOL_MAXSIZE` (padrão 64 por processo)                                                                                                                                     | Dimensione conforme a concorrência de requisições esperada mais os workers, mais as conexões que os inserts em lote mantêm durante a espera; o padrão do próprio driver se esgota sob escritas em massa.                                                       |
| Inserts em lote           | `LOOM_CH_BATCHED_INSERTS` (padrão ativado), `LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS` (10), `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS` (50), `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` (200) | Inserts concorrentes compartilham uma parte, com até a latência da janela cada um; um insert isolado espera o mínimo; cada insert em espera mantém uma conexão do pool, então o pool cresce com a taxa de escrita ([inserts em lote](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#batched-inserts)). |
| Espera pelo pool          | `LOOM_CH_POOL_WAIT_MS` (padrão 500)                                                                                                                                                 | Após esse tempo, a requisição falha rapidamente com 503 `OVERLOADED` em vez de entrar na fila; 0 = sem limite.                                                                                                                                                  |
| Keep-alive                | `LOOM_CH_KEEPALIVE_TIMEOUT` (padrão 5 s)                                                                                                                                            | Deve ficar abaixo do corte por inatividade do balanceador de carga, para que o Loom feche a conexão do pool primeiro.                                                                                                                                           |
| Prazos de instrução       | `LOOM_CH_TIMEOUT_READ_S`, `LOOM_CH_TIMEOUT_WRITE_S`, `LOOM_CH_TIMEOUT_DDL_S`                                                                                                        | Aplicados como `max_execution_time` e como um limite no cliente que libera o slot do pool; 0 = sem limite para essa classe.                                                                                                                                     |
| Prioridade por classe de tráfego | `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` (JSON)                                                                                                                                   | `priority` e `max_threads` por classe (`production`, `interactive`, `bench`, `background`), para que benchmarks e workers cedam vez às leituras de produção.                                                                                                    |
| Read-your-writes          | `LOOM_CH_READ_YOUR_WRITES`, `LOOM_CH_READ_YOUR_WRITES_WINDOW_S`                                                                                                                     | Leituras sequencialmente consistentes após uma escrita; tem custo em serviços replicados e não faz nada em um único nó.                                                                                                                                         |
| Orçamentos de operação    | `LOOM_LATENCY_BUDGET_MULTIPLIER`                                                                                                                                                    | Multiplica o `budget_p99_ms` de cada op; um salto WAN até o ClickHouse precisa de cerca de 20. Os orçamentos apenas geram eventos `budget.violated`; não interrompem requisições.                                                                               |

A capacidade web é `LOOM_WEB_WORKERS` por contêiner vezes o número de contêineres; o limitador de taxa divide
seus limites pelas réplicas ativas que ele conta em `loom.instances`, nunca menos que
`LOOM_REPLICA_COUNT` ([réplicas](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md#rate-limits)).

<a id="providers"></a>

## Provedores

| Alavanca                           | Configuração                                                                                                                      | Compromisso                                                                                                                                                                                                                      |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Limite do embedding da consulta    | `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`, `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`                                                      | Limite por tentativa para o embedding da consulta; depois das novas tentativas, a busca responde 503 `EMBED_TIMEOUT`.                                                                                                            |
| Grafia da consulta                 | `LOOM_MEMORY_SEARCH_QUERY_SPELLING_MAX_WORDS`                                                                                     | Uma consulta com no máximo essa quantidade de palavras lê o namespace uma vez, junto ao embedding, para gerar o embedding de suas palavras em minúsculas na grafia do corpus: 100–300 ms para um namespace de 40k linhas, reaproveitado por um minuto; `0` = desativado. |
| Caches de embedding da consulta    | `LOOM_EMBEDDING_CACHE_SIZE`, `LOOM_EMBEDDING_CACHE_TTL_S`, `LOOM_EMBEDDING_CACHE_SHARED`, `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS` | Um LRU por processo na frente de um `loom.embedding_cache` compartilhado; consultas repetidas dispensam a chamada ao provedor.                                                                                                   |
| Cache de respostas do LLM por classe | `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` (JSON)                                                                                         | Faz cache das respostas do LLM (ativado por padrão para `bench`) e pode impedir que uma classe use a camada forte ou intermediária.                                                                                              |
| Modelo de fallback                 | `LOOM_LLM_FALLBACK_MODEL`                                                                                                         | Responde quando o primário continua indisponível após as novas tentativas, em vez de fazer a op falhar.                                                                                                                          |
| Limite de gasto                    | `LOOM_DEFAULT_DAILY_COST_CAP_USD` (por namespace `daily_cost_cap_usd`)                                                            | Acima do limite, os workers baseados em LLM ignoram o namespace e `memory.ask` se abstém com `budget_capped`; leituras e escritas simples continuam.                                                                             |

<a id="embedder-choice"></a>

### Escolha do embedder

| Embedder (`LOOM_EMBEDDING_MODEL`)                            | Compromisso                                                                                              |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `text-embedding-3-large`, `gemini-embedding-001`             | Padrão de 3072 dimensões do Loom; qualidade e custo ainda precisam ser medidos nos seus dados.           |
| `text-embedding-3-small`                                     | Opção de vetor mais estreito, de 1536; mudar a largura exige gerar os embeddings novamente.              |
| `local`, `local:bge-small`, `local:bge-base`, `local:minilm` | Embedding em CPU no próprio processo, sem chamada ao provedor nem chave; troca qualidade por latência e custo. |
| `fake-ngram-3072`                                            | N-gramas de caracteres na largura padrão, apenas para testes e integração; os números de recall não significam nada. |

Trocar o embedder muda o espaço vetorial: defina `LOOM_EMBEDDING_DIM` de acordo, execute
`loom reembed` e, com limiares adaptativos ativados, deixe a sondagem de inicialização recalibrar. Execute
`make bench-quick` antes e depois; o mesmo vale para `LOOM_LLM_MODEL`, com o qual
os prompts de extração são calibrados.

<a id="workers-and-batches"></a>

## Workers e lotes

Todo worker tem um interruptor `*_ENABLED` e uma cadência `*_INTERVAL_SECONDS`, e muitos têm
uma pausa `*_IDLE_INTERVAL_SECONDS` após uma passagem que não encontrou nada
([referência de workers](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)). Intervalos menores reduzem o atraso do
trabalho em segundo plano ao custo de mais varreduras; o intervalo ocioso mantém uma fila vazia barata.

| Worker                | Ajustes                                                                                                                                                                                                                               |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `embedding_worker`    | `LOOM_EMBEDDING_WORKER_INTERVAL_SECONDS`, `_IDLE_INTERVAL_SECONDS`; `LOOM_EMBEDDING_WORKER_ORPHAN_FULL_SCAN_SECONDS` / `_ORPHAN_RECENT_SECONDS` limitam o anti-join de órfãos.                                                          |
| `extraction_queue`    | `LOOM_EXTRACTION_QUEUE_INTERVAL_SECONDS`, `_MAX_BATCHES_PER_PASS`, `_NAMESPACE_CONCURRENCY`, `_LEASE_SECONDS`; `LOOM_EXTRACTION_DEFER_AGENTS` adia `set_from_messages` para os agentes listados, de modo que a requisição retorna antes da extração. |
| `memory_expiry_sweep` | `LOOM_MEMORY_EXPIRY_SWEEP_BATCH_SIZE`.                                                                                                                                                                                               |
| `task_lease_sweep`    | `LOOM_TASK_LEASE_SWEEP_INTERVAL_SECONDS`, `_IDLE_INTERVAL_SECONDS`.                                                                                                                                                                  |

Os workers rodam na classe de tráfego `background`, então `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` também
limita a prioridade e as threads deles no ClickHouse. Execute exatamente um processo de workers: uma segunda
cópia de um worker baseado em LLM dobra suas varreduras e seu gasto. Os workers de ingestão e da
camada do compilador do plano de código têm seus próprios ajustes ([operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)).

<a id="client-side-levers"></a>

## Ajustes no lado do cliente

Alguns dos maiores ganhos estão na própria requisição:

- Peça menos corpos: `bodies` e `body_budget_tokens` em `memory.search`, `token_budget`
  em `context.pack` e `code.search`.
- Carregue o contexto uma vez por tarefa (`context.pack`) em vez de fazer uma busca a cada passo.
- Use `retrieval_budget` para escolher um perfil de busca mais barato ou mais profundo a cada chamada.
- Repita operações idempotentes com um header `Idempotency-Key` (os SDKs geram um): uma
  repetição retorna a resposta armazenada em vez de executar a operação novamente.

<a id="related"></a>

## Relacionados

- [Falha e recuperação](../architecture/failure-and-recovery.md): capacidade e o que escalar
- [Monitoramento](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)
- [Executando benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)
- [Depuração](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
