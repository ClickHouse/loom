<!-- i18n: source=docs/features/integrations/clickstack.md blob=008f7906fbb2d60f24b7f99be4e0ee44382e64f9 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md), which is authoritative.

<a id="clickstack-and-hyperdx"></a>

# ClickStack e HyperDX

O Loom sempre emite OpenTelemetry; as fontes do HyperDX, os painéis e a etapa de exclusão precisam ser configurados.

O Loom emite um span por operação, além de logs e métricas. Os spans carregam `loom.operation`,
`loom.org`, `loom.namespace`, `loom.agent`, `loom.request_id` e `loom.user_id` /
`enduser.id` quando presentes. Por padrão, o Loom os insere diretamente nas
tabelas `default.otel_*` do seu próprio serviço ClickHouse, no schema do coletor, de modo que o
HyperDX os lê sem nenhum coletor para executar. Com `LOOM_CLICKSTACK_DATABASE` definido, as
tabelas ficam nesse banco de dados em vez de `default`, aqui e em todas as consultas abaixo. O layout das colunas está em
`schemas/otel/clickstack_otel_tables.sql`; se o ClickStack mudar o schema, esse arquivo
deve acompanhar a mudança.

Os spans de chamadas de modelo carregam os atributos GenAI do OpenTelemetry
([telemetria](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#traces-and-logs)) nas chaves que a visualização de LLM do HyperDX
lê para o provedor, a operação, o uso de tokens, a parcela de cache de prompt e o custo.

<a id="choose-the-export-path"></a>

## Escolha o caminho de exportação

| `LOOM_OTEL_EXPORTER` | Comportamento                                                                                                                                       |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ch`                 | insere em `default.otel_traces`, `otel_logs`, `otel_metrics_sum`, `otel_metrics_histogram` e `otel_metrics_gauge` no mesmo serviço                  |
| `otlp`               | envia para `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` (cabeçalhos em `LOOM_OTEL_EXPORTER_OTLP_HEADERS`), para um collector ou outro backend                 |
| vazio (padrão)       | `otlp` quando um endpoint está definido; caso contrário, `ch`                                                                                       |

No modo `ch`, o Loom primeiro verifica se `default.otel_traces` existe; sem ela, o
exportador se desativa após uma linha de log. Para criar as tabelas:

```bash
clickhouse-client --multiquery < schemas/otel/clickstack_otel_tables.sql
```

`GET /v1/health?include=otel` mostra o exportador resolvido. As demais configurações de OTel
(intervalo de exportação, temporalidade, heartbeat do gauge, nome do serviço) estão em
[telemetria](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#exporters).

<a id="point-hyperdx-at-the-tables"></a>

## Aponte o HyperDX para as tabelas

As linhas já estão no serviço, então pule as etapas do coletor e da chave de ingestão.
Crie três **sources** do HyperDX: traces sobre `otel_traces`, logs sobre `otel_logs`,
métricas sobre `otel_metrics_*`. O `deploy/clickstack_sources.sh` as cria pela API do
HyperDX e configura a correlação entre elas (o ClickStack hospedado autentica com o cookie
de sessão do seu console, `HDX_COOKIE`).
O arquivo [`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md) traz o
mapeamento de campos de cada source.

<a id="dashboards-and-alerts"></a>

## Painéis e alertas

[`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md) é o livro de receitas:

- **Loom — Memory Health**: tendência da qualidade de recuperação (`loom.search.top_similarity`),
  chamadas e erros por operação e transporte, estado do sistema, gasto e tokens, latência por
  plano de recuperação, resultados de escrita e painéis de requisições (taxa, latência, erros, requisições
  mais lentas, tráfego por tenant, logs de um trace).
- **Loom — Code Plane**: atraso do índice, atraso da camada e os demais gauges `loom.code.*`. A definição
  em JSON está em `deploy/dashboards/code_plane.json`.
- A ordem de bootstrap e as buscas salvas.

Duas armadilhas ao escrever seus próprios tiles:

- **Os contadores são cumulativos.** A menos que `LOOM_OTEL_METRICS_DELTA_TEMPORALITY=true`, a linha de um
  contador é o seu total acumulado; uma taxa precisa da diferença entre pontos consecutivos,
  não de uma soma. Altere a configuração e os painéis ao mesmo tempo.
- **Um resultado vazio é lido como 0 no HyperDX.** Uma proteção de amostra mínima em um alerta deve
  retornar um valor dentro da faixa saudável, e não nenhuma linha, ou o alerta dispara no silêncio.

Os nomes das métricas e os limites sugeridos estão no
[catálogo de métricas](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#metrics).

<a id="erasure"></a>

## Apagamento

Requer `LOOM_CLICKSTACK_DATABASE`. O ClickStack não tem uma API de administração para excluir dados de usuários,
então o backend (`ClickStackBackend` em `server/erasure/app/backends.py`) executa instruções
`DELETE FROM` leves nas tabelas OTel desse banco de dados:

| Tabela                                                                           | Coluna de atributos pesquisada |
| -------------------------------------------------------------------------------- | ------------------------------ |
| `otel_logs`                                                                      | `LogAttributes`                |
| `otel_traces`                                                                    | `SpanAttributes`               |
| `otel_metrics_gauge`, `_sum`, `_histogram`, `_exponential_histogram`, `_summary` | `Attributes`                   |
| `hyperdx_sessions`                                                               | `ResourceAttributes`           |

- **Um escopo de usuário** corresponde a qualquer uma das chaves em `LOOM_CLICKSTACK_USER_ID_KEYS` (padrão
  `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id`), combinadas com OR, já que os SDKs divergem
  quanto ao nome da chave. O span de requisição do Loom carrega `enduser.id`; seus spans de chamada de modelo carregam
  `loom.user_id` e `langfuse.user.id`, e, com o Langfuse configurado, o mesmo vale para todos os outros
  spans que a requisição abre depois que o chamador é resolvido. Cada um deles também carrega
  `loom.org`, portanto a proteção de org abaixo se aplica.
- **Um escopo de namespace** corresponde a `loom.namespace` ou `service.namespace`. O exportador do Loom
  define `service.namespace` como a constante `loom` e carrega o tenant em
  `loom.namespace`; `service.namespace` cobre outros emissores que gravam no mesmo
  ClickStack, e seu valor `loom` nunca é correspondido.
- **Proteção de org.** Uma linha marcada com o `loom.org` de _outra_ org nunca é apagada. Uma linha
  correspondida por `loom.namespace` deve carregar o `loom.org` da org que está apagando. As métricas de op e
  de domínio do Loom (contadores e histogramas) carregam `loom.namespace` e nenhuma org, então um
  apagamento de namespace as deixa: elas não guardam conteúdo de memória, mas o rótulo `loom.namespace`
  delas é o nome do namespace, que, no caso de um namespace pessoal, é derivado do e-mail do usuário.
  Elas permanecem até que o TTL da própria tabela do coletor, se houver, as remova. Uma
  linha sem a tag `loom.org` correspondida por uma chave de usuário ou por `service.namespace` (outro
  emissor) é apagada, pois pode conter o titular dos dados.

Um delete leve grava apenas a máscara de existência da linha, então as linhas somem de toda leitura
imediatamente e os bytes saem no próximo merge; `ALTER TABLE … DELETE` reescreveria todas as
partes afetadas. O predicado varre qualquer tabela sem um bloom filter nos valores do map de atributos
(as tabelas de métricas não têm um); `loom doctor` as nomeia.

A etapa conta as linhas antes de excluir e informa essa contagem. Uma tabela ou banco de dados ausente
conta como nada a apagar; qualquer outra falha faz a etapa falhar de forma fechada, e o
worker `erasure_recovery` tenta novamente.

Verifique quais chaves de ID de usuário seus dados realmente carregam antes de confiar nos padrões:

```sql
SELECT DISTINCT arrayJoin(mapKeys(LogAttributes))
FROM default.otel_logs WHERE TimestampTime > now() - INTERVAL 1 DAY
```

Veja [retenção e apagamento](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) para a saga da qual esta etapa
faz parte.

<a id="settings"></a>

## Configurações

| Configuração                       | Padrão                                                     | Efeito                                       |
| ---------------------------------- | ---------------------------------------------------------- | -------------------------------------------- |
| `LOOM_OTEL_EXPORTER`               | `""`                                                       | `ch`, `otlp` ou automático                   |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` | `""`                                                       | URL base do coletor                          |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS`  | `""`                                                       | cabeçalhos `K=V` separados por vírgula       |
| `LOOM_OTEL_ENABLED`                | `true`                                                     | chave geral                                  |
| `LOOM_CLICKSTACK_DATABASE`         | `""`                                                       | o banco de dados das tabelas OTel; habilita a exclusão |
| `LOOM_CLICKSTACK_USER_ID_KEYS`     | `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id` | chaves de atributo que uma exclusão por usuário correlaciona |

<a id="related"></a>

## Relacionados

- [Telemetria](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md): o que é emitido e o catálogo completo de métricas.
- [Monitoramento](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md).
- [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md): os mesmos spans como traces de chamadas de modelo.
