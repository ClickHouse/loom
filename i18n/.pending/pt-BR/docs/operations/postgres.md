<!-- i18n: source=docs/operations/postgres.md blob=d087a158c4304a8400459a4a96b0f61030b0fabb model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md), which is authoritative.

<a id="postgres-memory-authority"></a>

# Autoridade de memória no Postgres

Este guia configura um deployment do Loom cujas memórias ativas e cabeças de documentos de conhecimento ficam armazenadas no Postgres, seja no ClickHouse Managed Postgres ou em um Postgres auto-hospedado, e cobre o que verificar antes de ele entrar em operação. O funcionamento interno está descrito em [autoridade de armazenamento](../architecture/storage-authority.md).

A autoridade é escolhida uma única vez, na primeira inicialização de um deployment. Um ClickHouse que já contém memória mantém a autoridade do ClickHouse; inicie um deployment com Postgres em um ClickHouse vazio.

<a id="requirements"></a>

## Requisitos

- Postgres 16 ou mais recente. Nenhuma extensão é necessária.
- Um banco de dados e um role que seja dono dele (o Loom cria o schema `loom` nele).
- O role pode executar `pg_logical_emit_message` (o público pode por padrão; caso contrário, a inicialização é recusada e informa o `GRANT` necessário).
- Para `LOOM_PG_DURABILITY=synchronous` (o padrão): pelo menos um standby síncrono em outro domínio de falha, para que uma escrita confirmada sobreviva à perda do primário.
- `wal_level = logical` se o banco de dados também alimentar uma ferramenta de replicação; o Loom em si não precisa disso.

<a id="settings"></a>

## Configurações

| Configuração                                     | Valor                                                                                                                                          |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_STORAGE_AUTHORITY`                         | `postgres`                                                                                                                                     |
| `LOOM_PG_DSN`                                    | `postgres://loom:<password>@<host>:5432/<database>?sslmode=verify-full&sslrootcert=<CA file>`, o endpoint direto, nunca um pooler de transações |
| `LOOM_PG_POOL_MIN_SIZE`, `LOOM_PG_POOL_MAX_SIZE` | conexões por processo do Loom (2 e 20); dimensione `max_connections` para todos os processos de todas as réplicas                              |
| `LOOM_PG_DURABILITY`                             | `synchronous` (recusa iniciar sem um standby síncrono) ou `local`                                                                              |

Indique a autoridade certificadora do servidor e exija `sslmode=verify-full`: ele verifica
o certificado e o nome do host, de modo que nada no caminho possa se passar pela autoridade e
ler a memória ou a senha. `require` e os modos inferiores criptografam sem verificar com quem se fala,
e servem apenas para um servidor de desenvolvimento local.

O Loom mantém seu próprio pool de conexões. Aponte `LOOM_PG_DSN` para além de qualquer pooler que entregue a um
cliente uma conexão de servidor diferente a cada transação (pooling por transação): as migrações do Loom mantêm um advisory lock de sessão e seu driver prepara statements, e esse
tipo de pooler não preserva nenhum dos dois entre transações. O
`LOOM_PG_DSN` de uma [célula](../architecture/cells.md) aponta diretamente para o Postgres, sem pooler de nenhum modo: seus processos
marcam cada sessão com o parâmetro de inicialização `loom.tenancy_checked`, que um pooler de sessão recusa ou descarta (então seus triggers de org recusam as próprias gravações do Loom para uma org da qual a
célula não tem registro, e o guard `loom.tenancy` recusa a linha de tenancy de cada org recém-alocada), e o `loom doctor` indica uma sessão que o perdeu. Scripts e carregadores em massa nunca definem
`session_replication_role = replica` em uma célula: isso ignora os triggers de org e o guard.

Cada réplica da implantação, e seu processo worker, usa os mesmos valores. A lista completa,
com as configurações de admissão e entrega, está na
[referência de configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md).

<a id="clickhouse-managed-postgres"></a>

## Postgres gerenciado do ClickHouse

1. Crie o serviço com **dois standbys de HA**. Com apenas um standby, o serviço
   replica de forma assíncrona, e uma escrita confirmada pode ser perdida junto com o primário.
2. Crie o banco de dados e a role, conceda à role `pg_read_all_stats` para que o Loom possa
   ver quais standbys estão conectados, e use a string de conexão **direta**, na porta
   5432, dos detalhes de conexão do serviço, não a do pooler: o pooler do serviço
   roda em modo de transação.
3. Defina as [configurações](#settings) e inicie o Loom. Verifique se
   `GET /v1/health?include=postgres` informa `postgres.standby` como `synchronous`: o boot
   recusa um `synchronous_standby_names` vazio e emite um aviso quando há menos standbys
   conectados do que os nomeados ([o que o boot verifica](#what-the-boot-checks)).

O failover, o posicionamento dos standbys e o fencing do primário antigo são responsabilidade do serviço. No
Kubernetes, os valores `postgres` do chart definem as [configurações](#settings) ([ClickHouse Cloud
e Postgres gerenciado](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)).

<a id="self-hosted-postgres"></a>

## Postgres auto-hospedado

Execute o Postgres sob um gerenciador de HA que mantenha um standby síncrono, isole (fence) o primary antigo na promoção e promova apenas um standby que contenha todos os commits confirmados. Configure-o no modo síncrono estrito: pelo menos um standby síncrono obrigatório, e escritas recusadas, em vez de confirmadas apenas pelo primary, quando nenhum estiver conectado. Um gerenciador que não seja estrito esvazia `synchronous_standby_names` quando o standby cai, e os commits passam a ser confirmados apenas pelo primary.

O Loom verifica a configuração do standby na inicialização, e `GET /v1/health?include=postgres` informa `postgres.standby` enquanto atende: `synchronous` quando os standbys pelos quais um commit espera (`n` de `FIRST n`, `ANY n` ou `n (...)`, senão um) estão conectados; `primary_only` quando nenhum é nomeado, de modo que apenas o primary confirma os commits; `waiting` quando há menos conectados, de modo que os commits esperam até que estejam; `null` quando o estado não pôde ser lido, porque o Postgres não respondeu ou o role não consegue ver `pg_stat_replication`.

Aponte `LOOM_PG_DSN` para o endpoint primary do gerenciador (o serviço ou endereço que acompanha o líder), nunca para um único host, de modo que uma promoção leve o Loom junto.

Um primary com um standby e sem gerenciador, como o `dev/ci_start_postgres.sh` inicia para testes, mostra as configurações:

```text
synchronous_commit = on
synchronous_standby_names = 'FIRST 1 (loom_standby)'
```

<a id="what-the-boot-checks"></a>

## O que o boot verifica

| Verificação | Recusa-se a iniciar quando                                                                                                                                                                                                                                                                      |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Conexão     | `LOOM_PG_DSN` está vazio ou o Postgres não responde                                                                                                                                                                                                                                             |
| Migrações   | um arquivo já aplicado de `schemas/pg_migrations/` foi editado, ou um que esta build não tem é mais antigo que o último dela                                                                                                                                                                    |
| Durabilidade | com `LOOM_PG_DURABILITY=synchronous`, `synchronous_standby_names` está vazio; com qualquer valor, o role não pode executar `pg_logical_emit_message`                                                                                                                                            |
| Autoridade  | o deployment registrou a autoridade do ClickHouse, este ClickHouse guarda memória sob ela, ou este Postgres pertence a outro deployment                                                                                                                                                         |
| Estado da célula | a primeira leitura das linhas de estado da célula (`loom.cell_first`, `loom.tenancy_mode`, `loom.cell_self`, `loom.tenancy`) falha; com migrações do Postgres pendentes, o processo inicia bloqueado e não pronto até que uma leitura seja concluída ([solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)) |

Uma migração espera no máximo 5 s por um bloqueio de tabela e é tentada cinco vezes; um boot
que registra `Postgres migration <file> waited too long for a lock` quatro vezes e depois falha
com `LockNotAvailableError` encontrou uma transação longa segurando a tabela: encerre-a
(`pg_stat_activity` mostra qual é) e inicie de novo. Um boot que encontra outro boot
migrando espera por ele, sem limite, e inicia quando as migrações dele terminam.

Toda conexão do Loom define `synchronous_commit = on` por conta própria, qualquer que seja o
padrão do servidor. O boot emite um aviso quando há menos standbys conectados do que um commit espera,
e quando seu role não consegue ver `pg_stat_replication`. Enquanto eles não estão conectados, um commit
espera por eles, por até `LOOM_PG_STATEMENT_TIMEOUT_MS` mais um segundo, e então responde 503
`POSTGRES_UNAVAILABLE` com `outcome_unknown`: a espera cancelada deixa o commit apenas no primário.
O worker de entrega só o publica no ClickHouse depois que os standbys o têm, de modo que um
failover nesse intervalo não deixa nada dele no ClickHouse
([autoridade de armazenamento](../architecture/storage-authority.md#only-durable-revisions-are-published)).
Um publicador que envia o commit de outra sessão o lê de novo em uma transação
que confirma uma mensagem lógica transacional com o prefixo `loom.durable`, de modo que um
consumidor de change-data-capture do banco recebe uma por cada leitura desse tipo; o boot
recusa um role que não pode escrever uma.

`GET /v1/health` informa `storage_authority` e, com Postgres, `pg_ok`; com
`?include=postgres` também um bloco `postgres` com `standby`, `parked_deliveries`,
`parked_audits`, `parked_knowledge_deliveries` e `backlog` (entregas pendentes e
estacionadas de cada fonte, contadas até `LOOM_PG_BACKLOG_COUNT_CAP`, `capped` além dele,
a idade da mais antiga e a da leitura,
[SLOs do pipeline](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#pipeline-slos)), instruções que a resposta padrão deixa
de fora de uma sonda de liveness. `pg_ok` é false quando uma consulta no pool de requisições não obteve resposta
em dois segundos, inclusive com todas as conexões do pool ocupadas. `/v1/health/ready` não
espera pelo Postgres: uma réplica que não consegue alcançá-lo ainda atende os planos que não
o usam. Ele responde 503 `POSTGRES_UNAVAILABLE` para as ops de memória, para toda op de knowledge
que lê a linha atual de um documento a partir do seu head ou grava uma, para a busca e as
leituras que verificam heads (`knowledge.search`, `knowledge.outline`,
`knowledge.synthesize`), e para as ops de dataset, que lêem ou gravam o catálogo de datasets
ali (`knowledge.dataset_get`, `knowledge.dataset_approve`, `knowledge.query`).
As ops de knowledge que apenas lêem ou escrevem no ClickHouse, como `knowledge.list` e
`knowledge.dedup_resolve`, continuam atendendo. `knowledge.ingest`, `knowledge.ingest_status`
e `knowledge.ingest_cancel` também respondem 503, pois cada uma lê ou grava seu job ali
(`loom.knowledge_jobs`); uma execução interrompida por uma indisponibilidade conta aquela tentativa e roda de novo
quando o Postgres responder.

`parked_deliveries` conta toda entrega de memória com um lease sem fim: revisões
gravadas no Postgres que o ClickHouse recusou `LOOM_PG_DELIVERY_MAX_ATTEMPTS` vezes, as
versões substituídas cujo payload apagado não deixou nada a enviar (abaixo), e as
entregas que `loom reconcile` mantém retidas (`last_error` `held by loom reconcile`) até
liberá-las. Uma cujo `targets` não contém nenhum de 1, 2 e 4 tem sua linha de memória
no ClickHouse (e seu documento de busca também, quando ganha um), sua linha de auditoria lá ou em
`loom.audit_outbox`, e deve apenas retiradas: 8, o registro de sua decisão excluída, que
`decision.chain` ainda serve até ser retirado; 16, os vetores da versão que ela
substituiu, que a busca ainda encontra até serem retirados (para uma linha `high`, sua barreira
oculta nesse meio-tempo os vetores do modelo servido). Uma que esgotou as tentativas não é repetida; seu
`last_error` em `loom.memory_deliveries` contém o tipo do erro e seu código do ClickHouse,
status HTTP ou SQLSTATE do Postgres, nunca seu texto, que pode citar a memória. Esta
instrução tenta uma de novo (nunca uma retenção do reconcile, que `loom reconcile` libera por conta própria):

```sql
UPDATE loom.memory_deliveries SET attempts = 0, held = false, lease_until = now() WHERE …
```

Depois que a passagem de [limpeza de payload](../architecture/storage-authority.md#payload-clearing)
rodou, uma build sem `010_revision_delivered_at.sql` não consegue enviar uma revisão cujo
payload foi apagado: enquanto essa build atende, uma nova tentativa deixa essas de fora, adicionando isto ao
seu `WHERE` (uma sondagem de chave primária por entrega):

```sql
AND NOT EXISTS (SELECT 1 FROM loom.memory_revisions r
                WHERE r.org = memory_deliveries.org
                  AND r.namespace = memory_deliveries.namespace
                  AND r.seq = memory_deliveries.seq
                  AND r.row IS NULL)
```

Uma publicação que encerra o processo do worker (falta de memória com um valor enorme) conta como
uma tentativa quando a passagem seguinte assume o seu claim (`last_error` `stopped mid-publish`):
no limite a revisão é estacionada ou, quando tudo o que ainda deve é seu documento de busca,
recebe um stub que o worker de embedding preenche.

Uma publicação que ultrapassa o prazo de instrução do ClickHouse conta como falha das próprias linhas,
já que o ClickHouse ainda responde: depois de uma lentidão que estacionou revisões
saudáveis, a instrução de nova tentativa acima com `WHERE attempts >= …` tenta todas de novo.

Uma estacionada com `last_error` `payload cleared, nothing to send` é uma versão cujo
payload a passagem de [limpeza de payload](../architecture/storage-authority.md#payload-clearing)
apagou e que uma versão posterior de sua chave substituiu: o ClickHouse serve o head da
chave, e o conteúdo dessa versão se foi (uma restauração que pulou o
[guia operacional](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#the-postgres-journal-and-a-clickhouse-restore)). Sua
linha de auditoria também não é enviada de novo: ela permanece no `audit` da revisão em
`loom.memory_revisions`. Tentar a entrega de novo não pode ter sucesso; depois de anotada, exclua sua linha:

```sql
DELETE FROM loom.memory_deliveries
WHERE lease_until = 'infinity' AND last_error = 'payload cleared, nothing to send';
```

O worker de entrega de knowledge publica uma entrega por vez, cada retirada à parte:
até dois inserts por entrega e `LOOM_PG_DELIVERY_BATCH_ROWS` entregas por passagem,
dentro de um único lease de `LOOM_PG_DELIVERY_LEASE_SECONDS`, que em um backlog costuma encerrar
a passagem primeiro. Uma passagem cortada pelo fim do lease libera o restante sem contá-lo, e a próxima
passagem o assume.

`parked_knowledge_deliveries` conta documentos de knowledge gravados no Postgres cuja
linha em `loom.documents` o ClickHouse recusou `LOOM_PG_DELIVERY_MAX_ATTEMPTS` vezes: o head
atende `knowledge.get` e a busca, que deixa de fora os chunks da versão mais antiga
que o ClickHouse lê como vivos até a retirada da entrega ser executada
([busca de knowledge](../architecture/storage-authority.md#knowledge-search)). A retirada de uma
exclusão estacionada roda antes de sua linha: `knowledge.get` a responde como excluída, a busca
não serve nenhum de seus chunks, e `knowledge.restore` responde 503 enquanto uma publicação da
fonte ainda pode chegar: até a entrega ser concluída, e pelo limite do insert depois disso. A
varredura de originais mantém o original que um documento vivo estacionado nomeia, e o mesmo faz a exclusão
de um usuário que não seja o seu autor. Seu `last_error` em `loom.knowledge_deliveries` contém
o tipo e o código do erro. Uma entrega de um escopo que uma build mais antiga apagou durante um
upgrade gradual não é repetida: execute essa exclusão de novo primeiro ([upgrades
graduais](../architecture/rolling-upgrades.md)). Esta instrução tenta uma de novo:

```sql
UPDATE loom.knowledge_deliveries SET attempts = 0, lease_until = now() WHERE …
```

`parked_audits` conta as linhas de `loom.audit_outbox` que o ClickHouse recusou o mesmo número de
vezes, deixadas ali com seu `last_error`: linhas de auditoria de uma exclusão, e aquelas que um claim
adiou porque o detentor que ele substituiu ainda pode inseri-las. Esta instrução tenta uma
de novo:

```sql
UPDATE loom.audit_outbox SET attempts = 0, due_at = now() WHERE …
```

<a id="rolling-upgrades-and-rollback"></a>

## Atualizações graduais e rollback

Toda réplica deve executar um build que registre a autoridade. Uma implantação da autoridade Postgres não faz rollback para um build sem esse registro: tal build não lê nenhum registro e grava em `loom.memory` diretamente ([atualizações graduais](../architecture/rolling-upgrades.md)).

<a id="backup"></a>

## Backup de dados

Faça o backup do Postgres com as ferramentas do provedor ou do gerenciador, e o do ClickHouse com as ferramentas do próprio ClickHouse. A restauração é feita em um único armazenamento, seguida de `loom reconcile`, que executa novamente nele o que o outro registrou após o ponto de restauração ([restaurando um armazenamento](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#restoring-one-store)). Um Postgres de outra implantação se recusa a iniciar.
