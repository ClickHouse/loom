<!-- i18n: source=docs/reference/sql-surface.md blob=cbe1b629ce7bff1c19c5d51c3a461f058ad2fd2c model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md), which is authoritative.

<a id="sql-surface"></a>

# Superfície SQL

A superfície SQL é um conjunto de views do ClickHouse somente leitura sobre as memórias de um tenant, o histórico de memórias, as conversas e o registro de requisições. O cliente SQL, a ferramenta de BI ou o job de warehouse de uma organização lê essas views com um usuário do ClickHouse criado por `loom sql-access`. Esse usuário lê as views e nada além delas, e cada view mostra apenas as organizações e os namespaces aos quais o usuário recebeu acesso.

<a id="version-and-compatibility"></a>

## Versão e compatibilidade

O nome de cada view carrega a sua versão: `sql_v1_memories` é a versão 1. Os nomes, os tipos e os significados das colunas são um contrato público:

- Uma view `v1` pode ganhar colunas. Ela nunca perde nem renomeia uma coluna, e uma coluna nunca muda de tipo nem de significado.
- Uma mudança que quebraria essa regra é publicada como uma view `sql_v2_*` ao lado da `v1`. As duas permanecem disponíveis por pelo menos uma versão, e as notas de versão indicam qual view `v1` será removida.
- Selecione as colunas pelo nome: `SELECT *` inclui as colunas adicionadas.

<a id="views"></a>

## Visões

Toda visão fica no banco de dados do Loom (`loom`, a menos que `LOOM_CH_DATABASE` indique outro) e traz as colunas de identidade `org` e `namespace`. `sql_v1_memories`, `sql_v1_memory_history` e `sql_v1_agent_events` também trazem `agent`, `user_id` e `session_id`; `sql_v1_conversations` traz `agent` e `user_id`; `sql_v1_usage_daily` traz `agent`.
Um join pode casar qualquer parte da [tupla de identidade](../concepts/identities-and-namespaces.md)
que os dois lados tenham.

<a id="sql_v1_memories"></a>

### `sql_v1_memories`

Uma linha por memória (identidade e chave): a versão atual dela. Uma memória excluída ou apagada não aparece, e uma rejeitada também não. Os demais status aparecem com o respectivo `status`:
`memory.list` oculta as linhas `duplicate`, `superseded` e `proposed` a menos que sejam pedidas, então filtre
por `status = 'active'` para obter o que uma listagem padrão mostra. Uma memória que passou do `expires_at` aparece
com o seu valor até a varredura de expiração agir sobre ela, como `memory.list` a lista; acrescente
`(expires_at IS NULL OR expires_at > now())` para obter o que `memory.search` serve.

| Coluna                                                             | Significado                                                                                        |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`     | a quem a memória pertence e a execução que a escreveu (`-` quando não há)                          |
| `memory_key`, `memory_id`, `memory_type`                           | a chave, o id da memória (junto com `version`, identifica uma versão) e o tipo (`fact`, `decision`, ...) |
| `value`                                                            | o valor como `memory.get` o serve, ou NULL quando retido                                           |
| `value_withheld`                                                   | `''` quando `value` é servido, `sensitive` para uma linha `high`, `redacted` para uma versão redigida |
| `content_sha256`                                                   | o SHA-256 do valor, NULL sempre que `value` for NULL: o hash de um segredo curto pode ser adivinhado |
| `version`, `status`, `confidence`, `pinned`, `tags`                | como em `memory.get`                                                                               |
| `sensitivity`, `source_class`, `actor`                             | o rótulo, a classe de procedência e o subject do token que a escreveu                              |
| `created_at`, `updated_at`, `expires_at`, `valid_at`, `invalid_at` | quando foi escrita e alterada pela última vez, quando expira, quando o fato passou a ser verdadeiro e deixou de ser |

<a id="sql_v1_memory_history"></a>

### `sql_v1_memory_history`

Uma linha por versão de cada memória, incluindo a versão de uma exclusão (`status =
'tombstoned'`, com `tombstoned_at` preenchido). Uma versão rejeitada não aparece. As colunas são as de
`sql_v1_memories`, sem `expires_at`, `valid_at` e `invalid_at`, mais `tombstoned_at`.
`value` e `content_sha256` são NULL para uma versão redigida e para toda versão de uma chave
que algum dia recebeu o rótulo `high`: a versão anterior à varredura de sensibilidade que rotulou uma linha
contém o mesmo valor sem rótulo.

<a id="sql_v1_conversations"></a>

### `sql_v1_conversations`

Uma linha por turno de conversa armazenado de cada usuário: `org`, `namespace`, `agent`, `user_id`,
`thread_id`, `msg_id`, `role`, `content`, `tokens`, `run_id` e `ts` (a escrita mais recente do turno). Um turno excluído ou compactado (`tombstoned_at` preenchido) não aparece, e um apagado
some junto com a sua linha. Um turno não tem rótulo de sensibilidade, então `content` é servido como foi escrito,
como `conversation.get` serve os turnos de uma thread; a verificação das listagens entre threads, que detecta texto
que parece `high`, roda no Loom, não no SQL.

<a id="sql_v1_agent_events"></a>

### `sql_v1_agent_events`

O registro de requisições: uma linha por chamada de op ou passagem de worker, com `kind` `''` para uma requisição.
`ts`, `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`, `operation`, `kind`,
`request_id`, `trace_id`, `latency_ms`, `success`, `error_code`, `status_class`,
`memory_key`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`,
`upstream_cost_usd` (gasto com modelos em USD), `bytes_written`, `model`, `consumer`, `subject`
(o subject do token que fez a chamada) e `transport`. Os argumentos da requisição não estão nele. As linhas de um
usuário final apagado trazem `user_id` como `<erased>`.

Ele contém os 31 dias UTC completos anteriores a hoje e o dia de hoje até agora; cada instrução lê o seu próprio
`now()`. A duração da janela e o seu alinhamento aos dias UTC fazem parte do contrato `v1`.

<a id="sql_v1_usage_daily"></a>

### `sql_v1_usage_daily`

`sql_v1_agent_events` somado por `day` UTC, `org`, `namespace`, `agent`, `operation` e
`kind`, sobre a mesma janela: `ops`, `errors`, `input_tokens`, `output_tokens`,
`embedding_tokens`, `llm_calls`, `upstream_cost_usd`, `bytes_written`, `latency_sum_ms` (com
`ops`, uma média entre linhas), e `latency_p50_ms` e `latency_p95_ms`, aproximados e por
linha: não se combinam entre linhas. Todo `day`, exceto o de hoje, é um dia completo.

<a id="what-a-read-costs"></a>

### Quanto custa uma leitura

Uma visão aplica o filtro do chamador às linhas que ela retorna, então um filtro em `ts`, `day`,
`thread_id` ou `memory_key` restringe a resposta, não o que é lido. Toda leitura de
`sql_v1_memories`, `sql_v1_memory_history` e `sql_v1_conversations` lê o escopo inteiro da concessão:
o histórico ordena todas as versões dele, e a visão de conversas agrupa todos os turnos
dele, conteúdo incluído. Esses volumes crescem com o que o escopo armazena, e um escopo amplo pode ultrapassar
os limites de leitura abaixo. Cada leitura de uma visão do registro lê o escopo inteiro da concessão sobre a
janela: no máximo desde o início do mês em que a janela abre, em geral perto da própria
janela. Uma concessão mais ampla (`*` namespaces, `--all`) lê mais. Os limites de leitura do usuário SQL
valem para essa leitura (`LOOM_CH_ANALYSIS_MAX_ROWS_TO_READ`, `_MAX_EXECUTION_S`,
`_MAX_MEMORY_BYTES`, `_MAX_THREADS`), então um escopo movimentado pode ultrapassá-los: restrinja a concessão ou
aumente essas configurações, que `insights.query` compartilha, e execute `loom sql-access grant` de novo,
o que as grava no perfil dos usuários SQL. Eventos mais antigos que a janela não estão na superfície; a
[exportação de analytics](../operations/analytics-export.md) grava totais diários de uso,
com nomes em hash, no bucket do operador. `loom.agent_events` em si não tem TTL, a menos que
`LOOM_RETENTION_AGENT_EVENTS_DAYS` defina um.

<a id="what-the-views-never-show"></a>

## O que as views nunca mostram

As views de memória aplicam as regras das próprias listagens do Loom
([sensibilidade](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)); a view de conversa serve um
turno como `conversation.get`, acima. Nenhuma delas mostra:

- o valor de uma linha `high` e seu hash, na view atual e em todas as versões da sua
  chave no histórico;
- o valor de uma versão redigida e seu hash;
- uma memória rejeitada, uma memória excluída ou apagada, e um turno de conversa excluído;
- qualquer linha de um namespace classificado como `restricted` em `loom.namespaces` ou `loom.code_repos`,
  independentemente da concessão, a partir do momento em que é classificado: o Loom audita cada leitura de um
  [namespace restrito](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces), o que uma view
  não consegue fazer;
- qualquer linha de um espelho (um namespace chamado `acl-` seguido de 24 dígitos hexadecimais), independentemente da concessão:
  cada um dos seus documentos só é mostrado a um leitor que a lista de permissões do seu repositório admita
  ([fontes de documentos](../architecture/document-sources.md#the-mirror)), o que uma view
  não consegue verificar;
- tabelas fora das cinco views: tokens, concessões, segredos, o log de auditoria, documentos,
  embeddings e argumentos de requisições;
- as tabelas `system` que mostram instruções de outros usuários, como `system.query_log`,
  `system.mutations` e `system.processes`. O ClickHouse as nega a um usuário sem
  concessão somente com `access_control_improvements.select_from_system_db_requires_grant` ativado (o
  padrão), então `loom sql-access grant` faz login como cada usuário para o qual define uma senha e
  o remove quando `system.processes` responde. No banco de dados do Loom, `system.tables` lista
  apenas as cinco views.

As views de memória e de conversa leem as tabelas das quais a
[saga de apagamento](../features/memory/retention-and-erasure.md) exclui dados e que seu censo de resíduos
conta, de modo que as linhas de um usuário ou namespace apagado saem delas junto com as tabelas.
As views do ledger mantêm suas linhas, que não contêm conteúdo: o apagamento de um usuário reescreve o
`user_id` do usuário para `<erased>`, como faz em `loom.agent_events`.

<a id="freshness"></a>

## Atualidade dos dados

As views leem o ClickHouse no momento da consulta; nada é copiado. O Postgres guarda a memória ativa ([autoridade de armazenamento](../architecture/storage-authority.md)), e `loom.memory` e `loom.memory_versions` são projeções publicadas após cada commit, em um flush a cada `LOOM_PG_PUBLISH_INTERVAL_MS` (250 ms por padrão). Uma escrita de memória confirmada aparece em `sql_v1_memories` e `sql_v1_memory_history` assim que o flush é concluído, normalmente em cerca de um intervalo; uma entrega que um escritor interrompido deixou pendente é publicada pelo worker de entrega, o que pode levar mais tempo. As views não esperam por nada: diferentemente de `memory.search`, que responde `PROJECTION_BEHIND` após `LOOM_PG_PROJECTION_WAIT_MS`, uma view entrega o que o ClickHouse contém. As conversas e o registro de requisições são gravados diretamente no ClickHouse.

<a id="access"></a>

## Acesso

Um usuário SQL da superfície é um usuário do ClickHouse com `SELECT` nas cinco views e nenhuma outra
permissão, no perfil de configurações `loom_sql_surface`:

- `readonly = 1`: um comando não altera nenhuma configuração além de `final`, `join_use_nulls`,
  `group_by_use_nulls` e `transform_null_in`, que não mudam as linhas de nenhuma view e
  que os clientes enviam por conta própria (`pg_clickhouse` entre eles). Qualquer outra configuração poderia encurtar
  o filtro de uma view: um modo de overflow, `additional_table_filters`, PREWHERE sob
  `FINAL`.
- valores que o perfil mantém quaisquer que sejam os padrões do servidor: `apply_deleted_mask = 1`
  e `apply_patch_parts = 1` (uma linha removida continua oculta), `use_query_cache = 0` e
  `use_query_condition_cache = 0` (ambos usam como chave o texto do comando, que todos os usuários
  compartilham), `use_skip_indexes_if_final_exact_mode = 1`,
  `do_not_merge_across_partitions_select_final = 0` e
  `optimize_move_to_prewhere_if_final = 0` (`FINAL` lê a versão mais recente de cada chave),
  todo modo de overflow em `'throw'` (read e timeout com suas formas `_leaf`, set,
  transfer, join, sort, distinct, group by e result),
  `send_logs_level = 'none'`, nenhuma função de introspecção, e `query_plan_filter_push_down = 0`
  e `enable_optimize_predicate_expression = 0`, de modo que o filtro de quem chama roda sobre o que uma view
  retorna e nunca sobre linhas de outro tenant (um filtro que gera erro por causa de um valor
  o citaria);
- os limites de `insights.query`, exceto os limites de resultado, já que um cliente SQL lê conjuntos
  de resultados inteiros: `LOOM_CH_ANALYSIS_MAX_EXECUTION_S`, `_MAX_ROWS_TO_READ`, `_MAX_MEMORY_BYTES`
  e `_MAX_THREADS`, e `_MAX_CONCURRENT` comandos por vez.

Cada view roda com os privilégios de seu definidor (`SQL SECURITY DEFINER`, o usuário que executou
a migração) e admite apenas as linhas cujo `(org, namespace)` a tabela
`loom.sql_access` concede a `currentUser()`, o usuário que enviou o comando. Um usuário sem
nenhuma linha concedida não lê nada, e o mesmo vale para todo outro usuário do ClickHouse, inclusive
os do próprio Loom.

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
loom sql-access grant --user acme_bi --org acme --namespaces '*'   # every namespace of acme
loom sql-access grant --user ops_bi --all                          # the whole deployment
loom sql-access grant --user acme_bi --org acme --namespaces support --rotate
loom sql-access list
loom sql-access revoke --user acme_bi
```

- `grant` cria o usuário quando ele não existe e exibe a senha uma única vez; o Loom não guarda
  nenhuma cópia. `--org` e `--all` se excluem mutuamente, e `--all` não aceita `--namespaces`. Executado
  de novo, define o escopo do usuário exatamente como o que ele nomeia: namespaces que ele deixa de nomear
  param de aparecer assim que as novas linhas chegam à réplica que atende a leitura; `revoke` encerra
  o login imediatamente. `--rotate` exibe uma nova senha. Quando uma etapa posterior à senha
  falha, `grant` informa; execute-o de novo com `--rotate`.
- `--all` é para um deployment cujos dados pertencem a uma única org. Em um deployment
  compartilhado, conceda a cada tenant sua própria org.
- `grant` recusa um escopo que nomeia um namespace restrito, e um curinga que alcançaria um
  que somente a configuração do repositório de código classifica: as views não conseguem ler essa
  configuração, então execute o comando com a do servidor. Um namespace restringido ali depois de
  um grant que o alcança está nas views desse grant: `list` nomeia cada um desses grants, para
  substituí-los pelos outros namespaces por nome, e termina com código 2.
- `revoke` primeiro remove as linhas de escopo e depois apaga o usuário. Um namespace que um grant nomeia
  conta como em uso até que `revoke` remova essa linha: seu nome não é entregue a um novo dono,
  e uma primeira escrita nele o registra sem dono. Um grant `*` não nomeia nenhum namespace:
  ele lê todos os namespaces da org, inclusive os criados depois, então nunca conceda um em uma
  org compartilhada por sign-ups (`LOOM_SELF_SIGNUP_ORG`).
- O comando roda como `LOOM_CH_USER`, que precisa de gerenciamento de acesso: `CREATE USER`,
  `ALTER USER`, `DROP USER`, `CREATE SETTINGS PROFILE` e `ALTER SETTINGS PROFILE`,
  `SELECT ... WITH GRANT OPTION` nas cinco views, e `SHOW USERS` com leitura de
  `system.users` e `system.settings_profile_elements`. Ele recusa um nome já usado
  por um usuário do ClickHouse que não está no perfil `loom_sql_surface`, e os usuários do próprio Loom
  (`LOOM_CH_USER`, o usuário leitor, `loom_analyst`, `default`).
- Remova um usuário com `revoke`, não com `DROP USER`: suas linhas de escopo continuam concedidas até que
  `revoke` as reescreva, e um usuário posterior com o mesmo nome e `SELECT` nas views
  as leria.

<a id="querying"></a>

## Consultas

A partir do Postgres da própria organização, consulte [Consultar o Loom pelo Postgres](../guides/query-from-postgres.md).

```sql
SELECT memory_key, value, updated_at
FROM loom.sql_v1_memories
WHERE namespace = 'support' AND status = 'active'
ORDER BY updated_at DESC
LIMIT 20;

SELECT day, sum(upstream_cost_usd) AS spend
FROM loom.sql_v1_usage_daily
WHERE day >= toDate(now(), 'UTC') - 30
GROUP BY day ORDER BY day;
```
