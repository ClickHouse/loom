<!-- i18n: source=docs/operations/backup-and-recovery.md blob=02de0a5c915dc18bf4a94189075292e0ca3d2cc0 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md), which is authoritative.

<a id="backup-and-recovery"></a>

# Backup e recuperação

Um backup do Loom é um backup do ClickHouse, mais o bucket de documentos enquanto qualquer linha do catálogo
nomear um objeto nele, e, sob a autoridade do Postgres, também um backup do Postgres.
O ClickHouse guarda o catálogo dos originais (`loom.document_blobs` e, no armazenamento padrão,
seus bytes), as chaves de assinatura de token (`loom.signing_keys`) e as substituições de runtime
do administrador (`loom.runtime_overrides`). Estes itens ficam fora do ClickHouse:

- `LOOM_JWT_SECRET`, o segredo raiz. Ele protege as credenciais armazenadas e as chaves de
  assinatura, então um backup restaurado sem ele não abre nenhuma das duas. Guarde-o junto com a configuração de ambiente.
- os espelhos git do índice de código em `var/code_repos`, um cache dos remotos registrados no
  ClickHouse. Um host sem o espelho o clona novamente.
- `LOOM_DOCUMENT_S3_BUCKET`, que guarda os bytes dos originais gravados enquanto
  `LOOM_DOCUMENT_OBJECT_STORE=s3` estava ativo (voltar à configuração anterior os mantém lá); o ClickHouse guarda
  a localização de cada objeto.

<a id="choosing-a-backup"></a>

## Escolhendo um backup

| Método                                                                  | Cobre                                                                   | Snapshot consistente          | Use para                                                                                                                    |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------- | ----------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Backups do ClickHouse Cloud                                             | todas as tabelas do serviço                                             | sim (no nível do serviço)     | recuperação de desastres no Cloud                                                                                           |
| `BACKUP DATABASE loom TO Disk(...)` / `S3(...)`, ou `clickhouse-backup` | todas as tabelas `loom.*`                                               | sim                           | recuperação de desastres em OSS autogerenciado                                                                              |
| `BACKUP DATABASE loom_telemetry TO ...`                                 | o rollup de SLI (`loom_telemetry.sli_minute`)                           | sim                           | manter o histórico de SLO além dos 31 dias que um refold do ledger recupera                                                 |
| `dev/backup_loom.py`, dump portátil                                     | todas as tabelas do Loom (abaixo)                                       | por tabela, não entre tabelas | mover uma implantação, uma organização ou um namespace (originais em um bucket: [abaixo](#the-portable-dump)); cópias frias |
| `namespace.snapshot`                                                    | a memória curada de um namespace, tipos, concessões, final da auditoria | uma chamada                   | copiar ou congelar um namespace pela API                                                                                    |

<a id="the-portable-dump"></a>

## O dump portátil

`dev/backup_loom.py` grava um arquivo `JSONEachRow` por tabela e depois um `manifest.json`
com o banco de dados de origem e, por tabela, o arquivo, a contagem de linhas, o tamanho em bytes, o SHA-256 e a instrução
`CREATE` (ou o erro que interrompeu o processo). O código de saída 1 significa que uma tabela falhou
(`complete: false`); o código 2 significa argumentos inválidos, ausência de destino ClickHouse ou um diretório de saída
que já contém um backup.

Ele faz o dump de toda tabela da família MergeTree do banco de dados, lida de `system.tables`. Deixa de fora
as tabelas do executor de migrações com prefixo `_`, as tabelas `.inner`, os leases do KeeperMap
(coordenação ativa, não dados) e as views, que `loom migrate` cria. O `backup_version` do manifest é 3. `--org` filtra toda tabela com uma coluna `org` e
ignora as tabelas sem ela (`idempotency`, `signing_keys`, ...), que guardam as linhas de todas as orgs.

`dev/restore_loom.py` verifica cada arquivo contra o manifest (tamanho, contagem de linhas,
SHA-256), imprime o plano, cria o banco de dados de destino e as tabelas ausentes a partir das instruções
`CREATE` registradas e, em seguida, carrega cada arquivo em uma cópia de staging da sua tabela e
anexa as partes preparadas à tabela. Um attach não dispara nenhuma materialized view, então uma linha restaurada
não é copiada de novo para uma tabela que o backup também restaura (`memory_versions`
teria, de outro modo, todas as versões em dobro), e as views continuam rodando para as gravações de um
banco de dados ativo. Restaure em um banco de dados que o `loom migrate` preparou (as views existem) ou em
um banco de dados de teste (as tabelas são criadas a partir do DDL registrado). Um backup que contém uma tabela de vetores codificados (`Quantized(` no seu
`CREATE` registrado) é recusado antes de qualquer gravação, `--dry-run` incluído, quando as configurações de codec do destino não podem ser lidas ou
`enable_quantized_codec` não pode ser ativado; quando pode, a tabela perde um codec geral após
`Quantized` se `allow_suspicious_codecs` não puder ser ativado
([configurações restritas](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)). O insert roda com
`optimize_on_insert = 0`, de modo que toda linha do dump é gravada, inclusive duplicatas de uma chave de
`ReplacingMergeTree`, e a tabela as colapsa nos seus próprios merges.
Cada tabela imprime `… attaching <table>` antes do attach e `✓` depois dele: se uma execução
for interrompida, a última linha nomeia a tabela em que ela estava; confira a contagem dessa tabela antes de executar de novo
com `--into-nonempty`, que a anexaria outra vez.

O manifest registra a tabela de origem e a de destino de cada materialized view disparada por insert.
Como um attach não dispara nenhuma view, o restore recusa uma seleção que contenha uma ponta de uma view
sem a outra: `memory` sem `memory_versions` (e `memory_code_link_queue`) deixaria
`memory.history`, `memory.redact` e `namespace.rollback` cegos para as versões restauradas,
e `code_term_freq` sem `code_chunks` seria preenchida de novo quando o código
fosse reingerido. Nenhuma view liga `documents` e `document_blobs`, mas o restore também recusa uma
sem a outra: documentos sem seus originais respondem 410 em todo download, e
originais sem seus documentos são o que a varredura de originais apaga. As duas ferramentas processam `documents` antes de qualquer outra tabela, de modo que, com `--into-live` e workers em execução,
a varredura nunca vê uma linha de original restaurada antes do documento que a nomeia (para os objetos do bucket, veja abaixo). Selecione as duas pontas ou passe `--allow-view-split` quando o destino
já contiver as linhas. `dev/backup_loom.py --exclude t1,t2` deixa de fora tabelas que uma passagem
reconstrói, junto com toda tabela que uma view preenche a partir delas (`code_chunks` leva
`code_term_freq` e `code_doc_stats` consigo); `restore_loom.py --exclude` deixa de fora tabelas
de um backup.

Os originais de documentos (`document_blobs`) são binários. O dump os grava como bytes brutos e
define `output_format_json_validate_utf8 = 0` por conta própria, de modo que um perfil que valida UTF-8
não pode substituí-los; um original binário ocupa cerca de 1.6 vez o seu tamanho no dump.

Uma linha gravada com `LOOM_DOCUMENT_OBJECT_STORE=s3` guarda apenas a localização do objeto, e
os bytes estão no bucket. Faça o backup do bucket com o seu próprio versionamento ou replicação
e restaure-o, no bucket de mesmo nome (cada linha o nomeia), depois do backup do ClickHouse
que nomeia seus objetos, ou com `LOOM_DOCUMENT_BLOB_GC_ENABLED=false`
até que ambos estejam de volta: um objeto restaurado pode manter seu antigo horário de modificação (depende de
como é restaurado), de modo que a varredura do bucket apagaria cada um cuja linha em `document_blobs`
ainda não tenha chegado.

Um bucket versionado mantém todo objeto apagado como uma versão não corrente, e a replicação
não replica exclusões por padrão, então originais apagados e varridos permanecem no bucket
(ou na sua réplica) até que uma regra de ciclo de vida (`NoncurrentVersionExpiration`, e expiração na
réplica) os remova. A janela dessa regra é por quanto tempo um original apagado ainda pode ser
restaurado, assim como a retenção de um backup do ClickHouse vale para as linhas.

Um dump não carrega os bytes de nenhum original dessas linhas, então não pode movê-los: linhas restauradas
em outro deployment ainda nomeiam o bucket de origem, a exclusão do destino as recusa por estarem fora
do seu bucket e prefixo, a varredura da origem apaga os objetos quando as linhas da origem
sumirem, e um destino apontado para o bucket e o prefixo da origem varreria todo objeto da
origem que o catálogo do destino não nomeia. Mova-os reenviando os
documentos ao destino, ou mova o deployment inteiro com a origem parada.

A cópia de staging repete o engine da sua tabela, exceto que um engine replicado em um
banco de dados que não é `Replicated` vira a sua forma local com os mesmos parâmetros (uma
cópia reivindicaria de novo o caminho do Keeper da tabela); o attach então replica as partes.
Uma tabela replicada que o restore cria mantém o caminho do Keeper registrado apenas no
banco de dados do próprio backup, e somente quando nada mais replica nesse caminho (ele recusa
enquanto houver réplicas ativas). Em outro banco de dados ela recebe um caminho independente: o
`default_replica_path` do servidor sob um UUID novo, ou o do próprio banco de dados em um banco Replicated.
Ele recusa quando o servidor não tem as macros de que esse padrão precisa.
Cada execução nomeia suas tabelas de staging `_restore_<run id>_<table>` e imprime o run id
primeiro, e cada uma apaga apenas as suas. Restores em um mesmo banco de dados (digamos, duas orgs) rodam um
de cada vez: uma execução reivindica `default._loom_restore_lock_<db>` (esperando até cinco minutos
pela reivindicação de outra execução), examina o destino de novo e aplica as recusas abaixo ao
que a execução anterior deixou, de modo que um segundo restore do mesmo escopo recusa em vez de
anexar linhas que se fundem às do primeiro. A reivindicação é uma tabela em `default`, então em um
cluster autogerenciado ela só ordena execuções pelo mesmo servidor: aponte todo restore
para um só. Uma execução que travou deixa para trás suas tabelas de staging e sua reivindicação;
`restore_loom.py --database <db> --drop-staging <run id>` as apaga, quando nenhum processo
dessa execução restar.
Nada é gravado antes da etapa de insert; `--dry-run` para depois do
plano. Ele recusa:

- o banco de dados ativo (`loom`, ou o banco de dados que a configuração do servidor nomeia) sem
  `--into-live`;
- uma tabela que já contém linhas no escopo do backup sem `--into-nonempty`. Os inserts
  acrescentam, então as tabelas MergeTree simples (`audit_log`, `agent_events`) teriam cada linha
  em dobro.

As duas ferramentas resolvem seu destino ClickHouse como o servidor (`LOOM_CH_*` do
ambiente ou de `.env`, cada um substituível por `--host`, `--port`, `--user`, `--database`,
`--secure`), recusam-se a rodar quando nenhum host é nomeado e leem a senha somente de
`LOOM_CH_PASSWORD`.

```bash
python dev/backup_loom.py --out-dir ./backups/$(date +%F) [--org acme] [--tables memory,namespaces]
python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --dry-run
python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --skip-idempotency
python dev/restore_loom.py --in-dir ./backups/<date> --into-live \
  --tables memory,memory_versions,memory_code_link_queue   # one end of a view needs the other
./scripts/ops/backup_drill.sh [--full-restore]   # round-trip check into a scratch database
```

`scripts/ops/backup_drill.sh` faz o dump do banco de dados ativo em um diretório temporário e
confere o manifest contra um snapshot de contagem de linhas. Com `--full-restore` ele restaura em
um banco de dados de teste `<db>_drill`, compara a contagem de linhas de cada tabela e apaga o banco de dados de teste
ao sair. Uma tabela que colapsa linhas de uma mesma chave (`ReplacingMergeTree`,
`AggregatingMergeTree`, ...) pode fazer merge no banco de dados de teste logo após o attach;
menos linhas ali é um aviso depois que um merge rodou, e uma falha caso contrário. Ele apenas lê o banco de dados ativo. Execute o drill de forma agendada.

O dump é por tabela, não um snapshot entre tabelas. Use-o para mover dados entre
clusters, não para recuperação de desastres.

<a id="verifying-the-audit-log-after-a-restore"></a>

## Verificando o log de auditoria após uma restauração

`python dev/verify_audit_chain.py [--org X] [--ts-since YYYY-MM-DD] [--json]` recalcula o hash de conteúdo de cada linha de `audit_log` e encerra com código 1 em caso de divergência, 2 quando não foi possível verificar (argumentos inválidos, nenhum alvo, uma consulta com falha) e 0 nos demais casos (zero linhas é considerado aprovado). Um relatório de divergência identifica a linha por partição, timestamp e id da requisição, nunca por seu ator, assunto ou namespace. O worker `audit_verifier` executa a mesma verificação por linha em um intervalo agendado. A verificação é uma detecção de adulteração linha a linha, não uma cadeia de hashes sobre as linhas; os checkpoints por hora são encadeados separadamente ([auditoria](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)).

<a id="undoing-changes-inside-a-namespace"></a>

## Desfazendo alterações dentro de um namespace

Estas operações desfazem uma escrita incorreta, um agente com comportamento inadequado ou uma importação errada sem precisar de um restore do ClickHouse. Cada uma tem escopo de um único namespace. `namespace.snapshot`, `namespace.restore` e `namespace.rollback` não estão na listagem `all` do MCP; chame-as pelo nome ou via HTTP, ou use o [painel](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md), na aba Health → Repairs, onde um admin do namespace baixa um snapshot, restaura um e faz o rollback.

| Operação             | Papel                                                  | Faz                                                                                                                                                                                                                                                                                                                                                                       |
| -------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.restore`     | `WRITER`                                               | traz de volta uma chave com tombstone                                                                                                                                                                                                                                                                                                                                     |
| `namespace.snapshot` | `ADMIN`                                                | congela as memórias, os tipos, as concessões e o final do registro de auditoria de um namespace (`include_audit_tail_n`, padrão 1000) como um payload JSON; `include_embeddings=false` (o padrão) permite que o destino gere novamente os embeddings com seu próprio modelo                                                                                                |
| `namespace.restore`  | `VIEWER` em um destino novo, `ADMIN` em um já existente | grava um payload de snapshot em `target_namespace` (opcionalmente `rebind_agent`); idempotente por `snapshot_id`. As linhas mantêm `version`, `content_sha256` e as colunas de atestação, então `verified_by` permanece; um valor do payload que sua coluna não comporta (um id de memória que não é um UUID) é recusado com 422. Uma coleção é recusada: seu snapshot é restaurado em um namespace simples |
| `namespace.rollback` | `ADMIN`                                                | devolve cada head de memória (ou `memory_keys`) à versão que ele tinha em `as_of`: reinstaura a versão mais antiga como o novo head e aplica tombstone às chaves criadas depois dela. Dry run por padrão (`dry_run=false` aplica); no máximo `max_keys` por chamada (`truncated` indica quando chamar novamente)                                                           |

Sob a autoridade Postgres, um restore deixa de fora uma memória que uma exclusão removeu do destino enquanto sua [evidência de exclusão](../architecture/storage-authority.md) é mantida, e a conta em `counts.erased_skipped`. A evidência cobre apenas os namespaces dos quais a exclusão removeu a memória, então um snapshot anterior a uma exclusão, restaurado em outro lugar, traz de volta as memórias excluídas: execute novamente a mesma exclusão (seu escopo) após esse restore.

`namespace.rollback` e `memory.history` leem `loom.memory_versions`, o espelho somente de acréscimo de cada inserção em `loom.memory` (os merges removem versões substituídas do próprio `loom.memory`). O rollback:

- recusa um `as_of` mais antigo que o histórico do namespace ou no futuro;
- ignora chaves excluídas definitivamente (`skipped_no_head`);
- deixa o worker de embeddings gerar novamente os embeddings dos heads reinstaurados.

```bash
AS_OF=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)   # GNU date; the instant before the bad writes
curl -s -XPOST http://localhost:7777/v1/namespace.rollback -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","as_of":"'"$AS_OF"'"}' | jq '{dry_run, reinstate, remove, truncated}'
```

<a id="under-the-postgres-authority"></a>

## Sob a autoridade do Postgres

Com `LOOM_STORAGE_AUTHORITY=postgres`, a memória ativa, o seu journal de revisões, os
heads de conhecimento, os jobs de ingestão e o catálogo de datasets ficam no Postgres ([autoridade de armazenamento](../architecture/storage-authority.md)),
e uma implantação tem dois armazenamentos para fazer backup: Postgres e ClickHouse.

| Armazenamento | Guarda sozinho                                                                                                                                             | RPO                                                                                                                                                                                                                                                                                                                                                                                                  | Backup feito por                                               |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| Postgres      | todo head e revisão de memória, heads de conhecimento, jobs de ingestão, o catálogo de datasets, respostas de idempotência                                 | 0 para escritas confirmadas em um failover para o standby síncrono; uma restauração para um ponto no tempo perde o que veio depois desse ponto                                                                                                                                                                                                                                                      | os base backups e o arquivo de WAL do gerenciador de HA ou do serviço |
| ClickHouse    | documentos e vetores de busca, os chunks de documentos de conhecimento e de valores longos de memória, histórico além do que o Postgres mantém, análises, traces, linhas de auditoria | o tempo desde o último backup, para o que só ele guarda; as revisões de memória e os documentos de conhecimento que o Postgres guarda são publicados novamente, mas não os seus chunks nem as linhas dos seus datasets: `loom reconcile` indica cada fonte de conhecimento a refazer o chunking, ou, acima de 20, o seu namespace (`loom rechunk`), e cada fonte a ingerir novamente, e um valor longo de memória fica pesquisável pelo seu trecho até ser definido de novo | os métodos acima                                               |

O tempo de recuperação de uma restauração é o tempo de restauração do próprio armazenamento
mais `loom reconcile`, que executa novamente o que o armazenamento restaurado perdeu do
registro do outro.

<a id="restoring-one-store"></a>

### Restaurando um armazenamento

Antes de uma restauração do ClickHouse com `LOOM_PG_PAYLOAD_CLEAR_ENABLED` ativado, pare a
limpeza de payload e escolha o backup conforme
[o journal do Postgres e uma restauração do ClickHouse](#the-postgres-journal-and-a-clickhouse-restore)
descreve. Depois que uma passagem de limpeza tiver sido executada, execute o `loom reconcile` do passo 3 com um build
que contenha `010_revision_delivered_at.sql`, e inicie a implantação com esse build no passo 4
([regra 43](../architecture/rolling-upgrades.md#the-rules) de rolling upgrade); então:

1. Pare todo servidor e worker da implantação.
2. Restaure o armazenamento a partir do seu backup e anote o horário do backup a partir dos
   metadados do próprio backup (para o ClickHouse, o início do `BACKUP`; para o Postgres, o alvo de recuperação).
3. Execute `loom reconcile --restored clickhouse --point <time>` (ou `--restored postgres`),
   que imprime o que executaria novamente; depois o mesmo com `--apply`.
4. Inicie a implantação.
5. Após uma restauração do ClickHouse, execute cada `loom rechunk` que o reconcile imprimiu e
   faça o upload e a ingestão novamente de cada fonte que ele indica em "to ingest again": os chunks de um documento
   de conhecimento e as linhas de um dataset ficam somente no ClickHouse.

O Postgres de uma [célula](../architecture/cells.md) tem triggers de org que recusam uma linha de uma
org para a qual a célula não tem linha de tenancy, e uma proteção `loom.tenancy` que recusa uma linha
de tenancy, vinda de uma sessão que não se identifica. Uma restauração lógica em uma célula é uma
sessão assim: com todos os processos da célula parados, execute-a como um único comando com o marcador,
`PGOPTIONS='-c loom.tenancy_checked=on' pg_restore ...`, e depois inicie um pod com
`LOOM_BOOT_MIGRATE` ativado antes dos demais. Um `pg_restore --disable-triggers` somente de dados em
um esquema existente termina habilitando todos os triggers como triggers de origem, o que deixa os dois triggers de
`loom.cell_first` desligados sob `session_replication_role = replica`; o boot desse pod os
define de volta como `ALWAYS`, e o `loom doctor` os aponta até que isso aconteça.

No Helm chart ([Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)), com a release `loom`:

1. Pare-o: `kubectl scale deploy/loom --replicas=0`, e também `deploy/loom-workers` com
   `workers.enabled`; suspenda a exportação de análises, se estiver ativada:
   `kubectl patch cronjob loom-analytics-export -p '{"spec":{"suspend":true}}'`.
2. Um serviço gerenciado restaura para um novo serviço: aponte a release para ele com
   `helm upgrade ... --set replicaCount=0 --set workers.replicaCount=0 --set autoscaling.enabled=false`
   (e o novo `clickhouse.host` ou `postgres.host`), para que nenhum pod inicie. O ponto é o
   horário de início que o serviço lista para o backup, ou o alvo de recuperação do Postgres.
3. O Job abaixo executa a imagem da release. Se uma passagem de limpeza já tiver sido executada e a release
   estiver em um build sem `010_revision_delivered_at.sql`, primeiro faça
   `helm upgrade ... --set image.tag=<a build that carries it>` (ou `image.digest`, que
   prevalece quando definido) com o novo host do passo 2 e seus três overrides `--set`, para que nenhum pod
   inicie, e mantenha essa imagem no passo 4. Execute o reconcile em um Job criado a partir do spec do
   próprio pod web, para que tenha o ambiente da release, o Secret e a CA do Postgres, o seu próprio
   rótulo de componente (a NetworkPolicy o seleciona, o Service não) e nenhuma probe:

   ```bash
   kubectl get deploy/loom -o json | jq '{apiVersion: "batch/v1", kind: "Job",
     metadata: {name: "loom-reconcile"}, spec: {backoffLimit: 0, template: {
     metadata: {labels: (.spec.selector.matchLabels
       | .["app.kubernetes.io/component"] = "reconcile")},
     spec: (.spec.template.spec | .restartPolicy = "Never" | .containers |= [.[0]
       | del(.livenessProbe, .readinessProbe, .startupProbe)
       | .command = ["loom", "reconcile", "--restored", "clickhouse", "--point", "<time>"]])}}}' \
     | kubectl apply -f -
   kubectl logs -f --pod-running-timeout=10m job/loom-reconcile
   kubectl wait --for=condition=complete --timeout=10s job/loom-reconcile
   ```

   Use `"postgres"` após uma restauração do Postgres. Um `wait` que falha enquanto o Job ainda está
   ativo (`kubectl get job loom-reconcile -o jsonpath='{.status.active}'` retorna `1`; um
   stream de logs interrompido encerra o `logs -f` antes da hora) significa que ele ainda está em execução: acompanhe seus logs
   novamente. Um Job que falhou (`.status.failed` retorna `1`) terminou com código diferente de zero: leia sua
   saída, exclua o Job e execute-o novamente. Após um dry run limpo, execute-o com
   `"--apply"` acrescentado.

4. Inicie-o novamente: `helm upgrade` com o arquivo de valores da release, levando o novo
   host do passo 2 e o `image.tag` ou `image.digest` do passo 3, se o passo 3 definiu um, e sem os
   três overrides `--set` do passo 2, e retome a exportação de análises. Execute
   cada `loom rechunk` que o reconcile imprimiu com
   `kubectl exec deploy/loom -- loom rechunk ...`, e faça o upload e a ingestão novamente de cada
   fonte que ele indica em "to ingest again".

`loom reconcile --apply`:

- executa novamente toda eliminação que o outro armazenamento registra como concluída após o ponto (a partir
  do segundo inteiro do ponto, já que os ledgers guardam segundos inteiros), sem censo (o outro
  armazenamento guarda o que o usuário escreveu após a eliminação), e sem os passos que a
  cópia restaurada mostra terem sido executados antes do ponto: os passos do último checkpoint do ClickHouse
  escrito antes do ponto (um `BACKUP` copia suas tabelas uma após a outra desde o seu
  início, então só esses deletes estão em todas as tabelas copiadas; uma linha eliminada por merge deixa
  todos os passos para executar) menos aqueles que o seu censo reabriu, ou os passos do Postgres que a linha do ledger
  registra na transação em que cada um foi confirmado (um passo executado por um processo do build anterior
  não é registrado e é executado novamente), que, executados de novo, apagariam o que o usuário
  escreveu depois deles. A linha restaurada do ledger passa a completed quando todos os passos foram executados; até
  lá, após uma restauração do ClickHouse, ela fica `failed` com "replay after a restore
  pending", uma linha que nem o recovery worker nem `loom erasure-re-verify --resume`
  executam. Uma cuja linha restaurada estava completed antes do ponto, ou que uma execução anterior
  concluiu, não é executada novamente. O passo das respostas de replay descarta o cache de replay inteiro da org
  nos dois armazenamentos, como qualquer eliminação faz. Uma eliminação sem estado terminal é deixada
  para o recovery worker, com seu checkpoint escrito novamente com os passos que a restauração desfez
  reabertos; após uma restauração do ClickHouse, uma cuja linha restaurada mostra que foi concluída ou abandonada
  tem, em vez disso, sua linha do ledger do Postgres marcada assim. A saga retomada é executada como qualquer
  outra, censo incluído, que conta o que o usuário escreveu desde então no seu escopo; o mesmo vale para
  uma que foi concluída após o ponto enquanto sua linha do ledger do Postgres perdeu a conclusão, um caso extremo que a
  passagem de recuperação fecha na sua primeira passagem, dez minutos após a requisição, ou após a
  conclusão, se for mais tarde. Uma abandonada nunca é executada
  (`loom erasure-re-verify` cuida dela);
- após uma restauração do ClickHouse: primeiro retém, como entregas que ainda não estão vencidas, toda
  revisão e todo head de conhecimento do Postgres confirmados após o ponto (menos o lease de um
  escritor e o limite de um insert, a margem em que uma escrita sorteada antes do ponto ainda pode ser confirmada
  depois dele) e todo head do Postgres que o ClickHouse guarda em uma versão mais antiga; depois executa as
  eliminações, pulando seus passos do Postgres (eles são retidos, e executá-los novamente
  apagaria o que o usuário escreveu após a eliminação); publica as entregas retidas; depois
  executa novamente toda redação que o Postgres registra após o ponto, o que limpa o que o
  ClickHouse restaurou e as respostas em cache e os precedentes que a citam;
- após uma restauração do Postgres: executa os passos do Postgres das eliminações e os das respostas de replay
  na cópia restaurada, e toda redação que o ClickHouse registra após o ponto; uma
  versão redigida que volta a ser o head ativo da cópia (a escrita que a substituiu foi
  perdida) é limpa no local. O que foi escrito após o ponto é perdido, como em qualquer
  restauração para um ponto no tempo; o ClickHouse ainda guarda cópias disso, que a busca não serve
  quando o Postgres não tem head correspondente.

As linhas de um dataset ficam somente no ClickHouse, e só uma ingestão as constrói: após uma
restauração do ClickHouse, um dataset cujas linhas foram escritas após o ponto (uma publicação, ou uma
aprovação que alterou o tipo de uma coluna) é lido com linhas faltando, já que a sua linha de catálogo, no
Postgres, as nomeia. `loom reconcile` indica, também no dry run, cada fonte com uma
linha de catálogo escrita após o ponto, para fazer upload e ingerir novamente; isso inclui uma linha
que uma aprovação ou uma edição de metadados escreveu sobre linhas que sobreviveram.

Os jobs de ingestão de conhecimento não precisam de passo próprio: as leituras de um job pegam a mais recente entre a
linha do Postgres e a do ClickHouse, e um job que o Postgres guarda é projetado novamente quando uma
fila o oferece. Um job que terminou após o ponto de restauração do ClickHouse não é oferecido novamente,
então `knowledge.inspect`, que lê o ClickHouse, não o lista até a sua linha expirar.

Um replay que não terminou deixa sua linha do ClickHouse `failed` com "replay after a
restore pending"; execute `loom reconcile` novamente até que saia com 0 antes de iniciar a
implantação, e antes de voltar para um build anterior ao reconcile, cujo recovery worker
retomaria essa linha com um censo.

Ele recusa antes de escrever qualquer coisa quando o ponto é posterior a agora, quando um worker
reportou uma passagem dentro de dois termos de lease de workers (`LOOM_SUPERVISOR_LEASE_TTL_S`, um minuto
por padrão), quando o ponto é mais antigo que `LOOM_ERASURE_LEDGER_DAYS` (as leituras do ledger do ClickHouse
guardam 89 dias) e para `--restored both`: eliminações e redações feitas
após os dois pontos de restauração não estão em nenhum dos armazenamentos e devem ser emitidas novamente a partir da exportação de
auditoria. Os dois ledgers guardam uma eliminação pela data em que foi requisitada, então um ponto dentro do
maior tempo que uma eliminação leva em relação a esse limite pode perder uma requisitada antes do limite e
concluída após o ponto: mantenha backups mais novos que os dias do ledger por essa margem. Ele
lê o registro de repositórios de código antes de escrever e recusa quando essa leitura falha.
Todo passo que executa é idempotente, então um reconcile interrompido, ou um que saia com 1, é executado
novamente por inteiro antes de a implantação iniciar; as entregas que ele reteve são listadas como paradas
por `GET /v1/health?include=postgres` até que uma execução seja concluída.

<a id="what-is-and-is-not-recoverable"></a>

## O que é e o que não é recuperável

| Situação                                                   | Recuperável?   | Como                                                                                                                                                                                     |
| ---------------------------------------------------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Uma memória marcada com tombstone por `memory.delete` ou `memory.clear` | sim            | `memory.restore`, ou `namespace.rollback` para um ponto anterior à exclusão                                                                                                              |
| Uma memória sobrescrita por uma versão posterior           | sim            | `memory.history` mostra as versões; `namespace.rollback` restabelece uma delas                                                                                                           |
| Um apagamento com `purge: true`, ou um namespace de benchmark removido com purge | não            | o purge exclui definitivamente; não há janela de restauração. Somente um backup feito antes dele                                                                                         |
| Um apagamento que usou tombstone (sem `purge`)             | considere definitivo | o censo da saga verificou que o escopo desapareceu de todos os caminhos de leitura; trazê-lo de volta desfaria uma solicitação de exclusão, então restaure de um backup somente quando o próprio apagamento tiver sido o erro |
| O schema de uma build mais nova, após reverter a imagem    | desnecessário  | as migrações são aditivas; a build anterior lê o schema mais novo ([atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#rollback))                    |
| Um schema anterior                                         | de um backup   | não há migrações de reversão; restaure um backup feito antes da atualização                                                                                                              |
| O disco de um servidor ClickHouse OSS de nó único          | de um backup   | sem replicação e sem recuperação para um ponto no tempo: o nó é durável apenas até o limite do próprio disco                                                                             |
| Tabelas do índice de código                                | reconstruíveis | `loom code backfill` as deriva novamente a partir do espelho git ([operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index)) |
| Linhas removidas por um TTL de retenção opcional           | não            | as exclusões por TTL são definitivas; somente um backup feito antes delas                                                                                                                |

Em um único nó OSS autogerenciado, agende `BACKUP DATABASE` (ou `clickhouse-backup`)
para um armazenamento fora do host. O ClickHouse Cloud replica e faz backup do próprio
serviço, então o dump portátil nesse caso serve para mover dados, não para durabilidade.

<a id="the-postgres-journal-and-a-clickhouse-restore"></a>

## O journal do Postgres e a restauração do ClickHouse

Sob a autoridade do Postgres, `loom.memory_revisions` guarda todas as revisões. Após uma restauração do ClickHouse, as versões confirmadas desde o backup estão no journal e não estão no `memory_versions` restaurado; o conteúdo delas só sobrevive enquanto o payload (`row`) ainda estiver no journal. Com `LOOM_PG_PAYLOAD_CLEAR_ENABLED`, o worker `pg_payload_clear` limpa os payloads das versões que todo backup utilizável contém ([limpeza de payload](../architecture/storage-authority.md#payload-clearing)). Deixe-o desligado quando o ClickHouse não tiver backup: o journal então mantém todos os payloads.

**O horizonte.** Defina `LOOM_CH_BACKUP_INTERVAL_MINUTES` com a frequência com que um backup do ClickHouse é iniciado. A passada só limpa uma versão depois que ela foi entregue ao ClickHouse há mais tempo que o horizonte. O horizonte é `LOOM_PG_PAYLOAD_RESTORE_HORIZON_MINUTES`:

- Em 0 (o padrão), ele é o dobro do intervalo mais 60. Assim, uma restauração pode recuar um backup quando o mais recente falhou, se o anterior começou no horário previsto.
- Ele nunca fica abaixo do intervalo mais 60, nem abaixo do limite de inserção mais 60. Um valor configurado abaixo disso é elevado ao maior dos dois.
- Um valor entre o intervalo mais 60 e um pouco abaixo do dobro do intervalo mais 60 cobre apenas o backup mais recente, se ele começou no horário previsto.

Ambas as configurações são LIVE. Uma mudança tem efeito na primeira passada após o processo reler os overrides (a cada `LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS`); uma passada já em execução mantém o horizonte com o qual começou.

**Por que a margem.** Os 60 minutos cobrem o que o horário de início de um backup não mostra:

- a réplica que um backup lê estar atrasada em relação à réplica que recebeu a inserção;
- o desvio de relógio entre o Postgres e o host do backup.

Toda versão limpa foi entregue há mais que o horizonte antes de a limpeza parar; assim, um backup iniciado menos que o horizonte menos 60 minutos antes desse momento contém toda versão limpa, enquanto o atraso e o desvio somados ficarem abaixo de 60 minutos. Um backup que começou atrasado revela isso no próprio horário de início, que o passo 4 abaixo verifica: no menor horizonte, o backup mais recente passa quando começou no horário previsto, e um atrasado pode não passar. `BACKUP ... ON CLUSTER` pega todos os shards de uma vez. Com uma ferramenta por nó, conte o início de um backup como o início do seu nó mais antigo.

**Antes de uma restauração, pare a limpeza.** Faça isto antes de escolher o backup, enquanto o ClickHouse ainda pode conter versões que o backup não tem:

1. Defina `LOOM_PG_PAYLOAD_CLEAR_ENABLED=false` no env do deployment. A configuração não é LIVE, e uma restauração de `loom.runtime_overrides` não consegue reativá-la.
2. Reinicie todos os processos do Loom com esse env. Remova qualquer instância parada que possa ser iniciada de novo, ou recrie-a com o novo env.
3. Confirme pelo orquestrador que todo processo do Loom em execução ou capaz de iniciar tem a chave explicitamente `false`. Se ficar sem valor, a chave toma o valor de um arquivo dotenv (`LOOM_ENV_FILE`, por padrão `.env` no diretório de trabalho) ou de um arquivo de configurações indicado pelo ambiente ou por esse dotenv (`LOOM_SETTINGS_FILE`), e qualquer um dos dois pode ativá-la.
4. Só então escolha o backup: um que começou menos que o horizonte menos 60 minutos antes da confirmação do passo 3 acima (o momento em que a limpeza parou). Se o horizonte foi reduzido, use o menor horizonte que esteve em vigor em qualquer momento do último horizonte. Com o horizonte padrão, isso permite o backup mais recente ou o anterior, se começaram no horário previsto. Um backup mais antigo pode não ter uma versão cujo payload já foi limpo, e o conteúdo dessa versão fica perdido.
5. Reative a chave quando o deployment iniciar de novo no passo 4 de [restaurar um store](#restoring-one-store), e não antes: nesse ponto, `loom reconcile --apply` já voltou a registrar uma entrega pendente para toda revisão confirmada após o ponto de restauração, e uma revisão mantém o payload enquanto tiver uma entrega pendente. Primeiro confira `LOOM_CH_BACKUP_INTERVAL_MINUTES` e `LOOM_PG_PAYLOAD_RESTORE_HORIZON_MINUTES` em relação ao cronograma de backup: a restauração trouxe de volta os overrides do momento do backup, então reaplique os que mudaram desde então. Uma versão que o ClickHouse ainda não tem após a restauração mantém o payload, e cada passada que a alcança a conta em `missing_in_clickhouse`, que pode continuar acima de zero. Quando o passo 4 de [restaurar um store](#restoring-one-store) inicia um build que inclui `010_revision_delivered_at.sql` em um deployment que servia um sem ele, a chave permanece desligada até um horizonte de limpeza depois que esse build passar a servir sozinho ([regra 43](../architecture/rolling-upgrades.md#the-rules)).

No [par de zero downtime](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair):

1. Edite o `.env`.
2. Execute `./redeploy.sh --force` a partir de um checkout que não esteja atrás de `origin/main`.
3. Quer o redeploy tenha dado certo ou falhado, pare e execute `docker rm` em todo contêiner `loom-*-workers`, em execução ou encerrado, cujo `docker inspect -f '{{.Config.Env}}'` não mostre `LOOM_PG_PAYLOAD_CLEAR_ENABLED=false`. Os contêineres web não executam workers, e, com o contêiner de workers da cor anterior removido, `--rollback` mantém os workers atuais.
4. Se isso não deixar nenhum contêiner de workers, corrija a causa e execute o passo 2 de novo.

Uma restauração que pula esses passos pode perder o conteúdo das versões confirmadas entre o backup e a restauração; os metadados delas permanecem no journal.

<a id="the-rollback-colour"></a>

## A cor de rollback

Em um [par de zero downtime](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair), o `redeploy.sh` para os contêineres da
cor anterior (`loom-<colour>`, `loom-<colour>-workers`) em vez de
removê-los, e marca a imagem anterior como `loom:prev`. O `./redeploy.sh --rollback`
os inicia, verifica a saúde deles e troca o proxy de volta. Se o contêiner da cor anterior
não existir mais, o rollback é recusado ("the previous build is gone"); faça o redeploy de um commit
sabidamente bom.

> **Aviso.** Nunca execute `docker container prune` ou `docker system prune` em um host
> que executa o par: isso remove a cor de rollback parada. Libere disco com
> `docker builder prune` e `docker image prune`. Nunca dê a um contêiner temporário o nome
> `loom-blue` ou `loom-green`: removê-lo pelo nome remove uma cor.

Após um rollback, o contêiner web da cor que recebeu o rollforward continua em execução até que você o pare
(`docker stop -t 75 loom-<colour>`, como o rollback imprime). O contêiner de workers dela
já estava parado antes da troca, então o lease dos workers fica com a cor revertida; somente
quando essa cor não tem um contêiner de workers é que a cor do rollforward continua executando-os.

<a id="moving-a-deployment"></a>

## Mover um deployment

Um novo processo do Loom apontado para o mesmo ClickHouse (e Postgres, com
`LOOM_STORAGE_AUTHORITY=postgres`), ou para um ClickHouse movido ou restaurado, recupera todos os
recursos. Com a autoridade Postgres, pare a origem antes de copiar qualquer um dos stores;
restaurar um store a partir de um backup feito em outro momento é uma
[restauração de um store](#restoring-one-store). Leve estes itens:

- ClickHouse: o mesmo serviço, uma cópia com o estado do Keeper, ou um dump de `dev/backup_loom.py`
  restaurado em um ClickHouse preparado por `loom migrate`;
- Postgres, com `LOOM_STORAGE_AUTHORITY=postgres`: o mesmo serviço, ou uma cópia;
- a configuração de ambiente (`.env` ou o ambiente do contêiner);
- `LOOM_JWT_SECRET`. Sem ele nenhum token é verificado, e as credenciais armazenadas e
  as chaves de assinatura não abrem;
- enquanto alguma linha do catálogo nomear um objeto nele, `LOOM_DOCUMENT_S3_BUCKET` com o mesmo
  nome e prefixo: o mesmo bucket acessado a partir do novo host, ou um bucket com esse nome em
  outro endpoint (`LOOM_DOCUMENT_S3_ENDPOINT_URL` não faz parte da localização de uma linha).
  Cada linha nomeia o seu bucket, e o Loom não tem ferramenta para reescrevê-lo: uma cópia com outro
  nome deixa todo download e toda exclusão apontando para o bucket antigo.

Todo o resto volta do ClickHouse: originais de documentos armazenados nele, chaves de assinatura,
sobrescritas de runtime, o lease do supervisor, revogações, contagens de tentativas de login e
marcas de invalidação do percurso do grafo. Os espelhos Git são clonados novamente a partir dos remotes registrados
na próxima passagem de ingestão, ou na primeira leitura de histórico no novo host.

Exceções limitadas:

- O fluxo de eventos (violações de orçamento) é drenado no `SIGTERM`. Um `SIGKILL` ou OOM perde
  no máximo um intervalo de flush dessa telemetria, nunca linhas de auditoria ou de memória.
- A reconfiguração de provedor aguarda um processo morto até que um processo no host dele comprove que ele
  sumiu ou, no Kubernetes, até que o líder dos workers veja o pod dele desaparecido. Nos demais casos, um processo
  cujo host também sumiu (uma mudança para um novo host) é desativado manualmente com
  `loom participant retire --participant <id> --yes`
  ([reconfiguração de provedor](../architecture/provider-reconfiguration.md)).
- O preenchimento dos buckets de limite de taxa é por host. Ele se recompõe em segundos, então um novo host começa com
  os buckets cheios.

<a id="related"></a>

## Relacionados

- [Atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md): o procedimento de rollback e a regra de migração aditiva
- [Manutenção](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#erasure): sagas de exclusão e sua recuperação
- [Histórico e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md): o que
  `memory.history` retorna
- [Falha e recuperação](../architecture/failure-and-recovery.md): como o sistema
  se comporta quando um componente falha
