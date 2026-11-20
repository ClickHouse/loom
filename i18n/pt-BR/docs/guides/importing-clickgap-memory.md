<!-- i18n: source=docs/guides/importing-clickgap-memory.md blob=5fff9080da2e5f36035250de4a491ae273d3484c model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md), which is authoritative.

<a id="import-clickgap-memory"></a>

# Importar a memória do ClickGap

`loom import-clickgap` carrega um formato específico de exportação SQLite, um armazenamento `lessons` do ClickGap, em um namespace do Loom; pule esta página, a menos que você tenha uma exportação desse tipo. Uma lição é uma memória de revisão curta e deduplicada, com entidades tipadas, categorias, um tipo (kind), um TTL por linha e um `recall_count`.

Dois comandos fazem o trabalho:

- `loom import-clickgap`: o importador (`server/cli/import_clickgap.py`), executado onde a CLI `loom` alcança o ClickHouse (o contêiner do servidor ou um clone com o `.env` do servidor).
- `python -m bench.clickgap_migration_validate`: o validador (`bench/clickgap_migration_validate.py`).

<a id="what-the-importer-preserves"></a>

## O que o importador preserva

| ClickGap                    | Loom                                                                              | Observação                                                                                                                                                                                                                                                                      |
| --------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lessons.text`              | `loom.memory.value` + `content_excerpt` embutido                                  | a afirmação é embutida; o raciocínio não                                                                                                                                                                                                                                        |
| `lessons.reasoning`         | `value_json.reasoning` (com `.text` = afirmação)                                  | `get` retorna `value_json`, então a afirmação viaja junto com o raciocínio, nunca concatenada a ele                                                                                                                                                                             |
| `lessons.kind`              | `memory_type` (na maioria 1:1) **+** tag `kind:<kind>`                            | `summary → episodic` (não pesquisável): os resumos continuam acessíveis por `memory.get` / `memory.list` e pela tag `kind:summary`, mas não são ranqueados em `memory.search`. A tag `kind:<kind>` preserva a taxonomia original. Veja `_clickgap_memory_type` / `_CLICKGAP_TYPE_REMAP`. |
| `lessons.source`            | `source_trace='clickgap://<class>/<source>'` + tag `source:<class>`               | classes: pipeline / sessão / humano / revisor / avaliação                                                                                                                                                                                                                       |
| `lessons.created_at`        | `created_at`, `valid_at`, `temporal_anchor`                                       | o momento original do registro, não o momento da importação                                                                                                                                                                                                                     |
| `lessons.expires_at`        | `expires_at` **literal, por linha**                                               | nunca recalculado a partir de um TTL global                                                                                                                                                                                                                                     |
| `lessons.recall_count`      | `recall_count` **e** `memory_embeddings.reinforcement_count`                      | veja [sinal de recall](#recall-signal)                                                                                                                                                                                                                                          |
| `lessons.last_recalled_at`  | `last_recalled_at` **e** `last_reinforced_at`                                     | o eixo de recência do recall                                                                                                                                                                                                                                                    |
| `lessons.evidence_count`    | `evidence_count`                                                                  |                                                                                                                                                                                                                                                                                 |
| `lesson_entities`           | `entities` (simples) + tags `entity:<kind>:<text>` + `loom.entity_vectors`        | o array `entities` simples descarta o tipo; a tag o mantém                                                                                                                                                                                                                      |
| `lesson_categories` + items | `loom.categories` + `loom.memory_categories`                                      | nomes normalizados para minúsculas antes do `category_id` determinístico                                                                                                                                                                                                        |
| lessons `false_positive`    | tag `polarity:negative`                                                           | memórias de supressão ("do NOT re-raise", ou seja, não levantar novamente)                                                                                                                                                                                                      |
| fontes atribuídas a humanos | tag `pii:human-attributed`                                                        | candidatas a apagamento por tag                                                                                                                                                                                                                                                 |

**A importação é idempotente.** `memory_key='lesson_<id>'` e um `memory_id` UUIDv5 (a partir de
`clickgap://<org>/<namespace>/lesson/<id>`) são determinísticos, e cada execução carimba uma
`version` nova. Uma nova importação substitui os registros sob `ReplacingMergeTree(version)` e a contagem de linhas
permanece a mesma.

<a id="1-dry-run"></a>

## 1. Execução de teste

Nenhuma escrita é feita. A execução de teste verifica se todos os timestamps são interpretados corretamente, exibe a tabela cruzada de kind → type e TTL e a contagem de categorias, e testa o embedder para que nenhuma linha seja gravada sem embedding.

```bash
loom import-clickgap \
  --sqlite ~/Downloads/clickgap-memory-YYYYMMDD/memory.db \
  --org <org> --namespace clickgap --dry-run
```

Confirme que a tabela cruzada corresponde à origem e que o embedder está acessível.

<a id="2-import"></a>

## 2. Importação

Use um **namespace dedicado** (`clickgap`) para que o rollback seja um único `namespace.delete`. A exportação não traz vetores, então cada linha recebe embedding na importação com o `LOOM_EMBEDDING_MODEL` em uso.

```bash
loom import-clickgap \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap
```

`--agent` tem como padrão `clickgap`; `--batch-size` define as linhas por insert no ClickHouse (padrão 1000). O bloco de verificação no final imprime os resultados e faz a execução falhar (exit 1) em caso de divergência em:

- linhas de memória ativas em comparação com a contagem de lições da origem;
- linhas de embedding com o modelo errado (deve ser 0);
- a contagem de linhas permanentes (sem TTL);
- o maior `recall_count`, lido de volta.

`lesson_recalls` (o log de eventos por recall) **não** é importado por padrão: as colunas desnormalizadas `recall_count` já carregam o sinal de ranking. Passe `--seed-recall-events` somente se você precisar do log por evento em `loom.memory_usage_events` para análises.

> **Aviso:** `--seed-recall-events` é aditivo e não é idempotente: execute-o uma única vez, em um namespace novo. Ele **descarta `pr_number`**, porque os eventos de uso do Loom não têm coluna de PR.

<a id="recall-signal"></a>

## Sinal de recall

O importador espelha `recall_count` em `memory_embeddings.reinforcement_count` e
`last_recalled_at` em `last_reinforced_at`. Em `memory.search`, essa contagem alimenta o
termo de ranking de reforço com decaimento: `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` define o seu peso
(0 o desativa), `LOOM_MEMORY_SEARCH_REINFORCEMENT_HALF_LIFE_DAYS` define o seu decaimento, e uma
requisição pode sobrescrever ambos com `reinforcement_weight` / `reinforcement_half_life_days`
([configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)).

O termo tem um limite abaixo da relevância, então o Loom **não** reproduz a ordenação
`recall_count DESC` do ClickGap: ele ordena principalmente por similaridade, com termos de entidade, recência e
reforço ([recuperação e ranking](../concepts/retrieval-and-ranking.md)). Se a sua
carga de trabalho depende da ordenação por frequência de recall, valide se a ordem resultante é
aceitável (o validador relata o τ da ordem de recall) antes da virada.

<a id="3-validate-before-cutover"></a>

## 3. Validar antes da migração final

O validador gera seu próprio token de administrador com `server.auth.jwt.mint_token`, portanto precisa de
`LOOM_JWT_SECRET` e de um `LOOM_BASE_URL` explícito: não há destino padrão. Suas sondagens
são leituras de avaliação (`auto_touch: false`, os campos de medição de `bench/_gate.py`), então uma
execução não registra uso de recall nas lessons importadas, e execuções repetidas não alimentam o sinal de
reforço que o gate τ mede.

> **Aviso:** aponte para um endpoint confiável. As consultas das sondagens são as
> asserções das lessons; os dados da importação não devem sair da sua infraestrutura por meio de um
> embedder de terceiros. `--show-queries` imprime o texto truncado das sondagens e vem desativado por padrão pelo mesmo
> motivo.

```bash
LOOM_BASE_URL=http://localhost:7777 LOOM_JWT_SECRET=... \
python -m bench.clickgap_migration_validate \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap \
  --probes 200 --top-k 10 \
  --min-recall 0.90 --min-recallw 0.90 --min-tau 0.2
```

Cada gate faz a execução falhar com saída diferente de zero quando definido acima de 0 (0 = apenas relatório):

| Gate            | O que mede                                                                                                                                                          |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--min-recall`  | Recall@k de item conhecido: a própria lesson da sondagem está entre os k primeiros do Loom? Os detalhamentos por tipo e por tipo de entidade detectam o colapso de uma única fatia (um remapeamento de tipo errado, por exemplo). |
| `--min-recallw` | Recall@k ponderado por recall: cada sondagem pesa conforme seu `recall_count`, então deixar de encontrar uma lesson muito acessada custa mais.                      |
| `--min-tau`     | τ da ordem de recall: concordância de Kendall entre a ordem do Loom e `recall_count`. Um diagnóstico: o Loom não ordena por `recall_count`, então não espere um τ alto. |

Uma busca que falha é reportada e excluída das razões, nunca contada como erro, e qualquer
busca com falha faz a execução sair com código diferente de zero, com ou sem gates. Uma execução sem τ calculável imprime `n/a` e
falha em `--min-tau`.

Os resumos (`summary → episodic`) são excluídos das sondagens, pois não são pesquisáveis por
design. `--seed` (padrão 42) torna a amostra de sondagens reproduzível; `--concurrency`
(padrão 8) limita as buscas concorrentes.

<a id="use-suppression-memories"></a>

## Usar memórias de supressão

As lições `false_positive` ("parece um bug, NÃO levantar de novo") formam a camada de
supressão. Elas são pesquisáveis e carregam a tag `polarity:negative`. Um detector que
decide se deve levantar um achado candidato executa uma busca filtrada por tag sobre o
texto do achado:

```python
hits = await agent.memory.search(finding_text, tags=["polarity:negative"], top_k=5)
if hits and hits[0].similarity >= SUPPRESS_THRESHOLD:  # calibrate per embedder
    skip_or_downweight(finding)  # a known false positive
```

O filtro `tags` se aplica a todas as etapas de recuperação (vetorial, lexical e o
preenchimento por recência/confiança), de modo que o resultado contém apenas supressões
(`test_lexical_fallback_applies_caller_filters` em `tests/unit/test_memory_search_service.py`).
Um positivo que vazasse para o resultado esconderia um achado real.

Filtre qualquer tipo da mesma forma, pela tag `kind:<kind>`: `tags=["kind:principle"]`, ou
`tags=["kind:false_positive", "kind:maintainer_preference"]` para uma lista de bloqueio com
vários tipos (`tags` é um filtro OR). O parâmetro `memory_type` de `memory.search` aceita
apenas os tipos gerais (`semantic`, `procedural`, `preference`, `reflection`, `decision`),
então a taxonomia completa de tipos fica nas tags.

<a id="roll-back"></a>

## Reverter

- **Reversão completa:** `namespace.delete` em `clickgap` (admin; via MCP, chame-o por meio de
  `loom_op`). A importação usa um namespace dedicado, então nada mais é removido.
- **Apagamento ou limpeza de coorte:** `erasure.request` com escopo de namespace (com checkpoint e
  retomado pelo worker de recuperação), ou apague apenas as linhas `pii:human-attributed` para
  atender a um pedido de remoção de um mantenedor ([retenção e apagamento](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)).
- **Correção de mapeamento:** execute o importador novamente. Chaves determinísticas e um `version`
  maior substituem as linhas incorretas no lugar.

<a id="consolidate-optional-after-validation"></a>

## Consolidar (opcional, após a validação)

Se você compactar quase-duplicatas de conjuntos de entidades, execute `memory.consolidate` **depois** da validação.
As proteções em `server/memory/app/consolidate.py` excluem linhas **fixadas** (pinned) e nunca mesclam
entre `memory_type` diferentes nem entre **polaridades** diferentes, de modo que uma supressão `false_positive` não pode ser
absorvida por uma observação positiva, e um princípio não pode absorver uma observação.

Mantenha `consolidation_enabled=false` no namespace (`namespace.register`) para que o
worker de consolidação em segundo plano não consolide uma segunda vez as lições `summary` do ClickGap
que já foram agrupadas.

<a id="related"></a>

## Relacionados

- [Escrevendo memória](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
- [Recuperação e ranqueamento](../concepts/retrieval-and-ranking.md)
- [Namespaces e compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
