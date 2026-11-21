<!-- i18n: source=docs/operations/upgrades.md blob=8caf8c789d2fc39ab89d9830d45a7e1229be2051 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md), which is authoritative.

<a id="upgrades"></a>

# Atualizações

Uma atualização substitui a imagem (e a do extrator de documentos, que não guarda estado) e
aplica as migrations que o novo build traz. Nenhum outro estado é movido: tudo o que é durável
está no ClickHouse (e no bucket de documentos, para os originais gravados com
`LOOM_DOCUMENT_OBJECT_STORE=s3`), e os espelhos de código são montados sem alterações. Esta página
trata do lado do operador. Como um cliente enxerga campos desconhecidos e removidos está na [referência da
API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields). Quais versões recebem correções e qual
SDK funciona com qual servidor: [versões suportadas](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md).

<a id="how-migrations-run"></a>

## Como as migrations são executadas

O journal, a numeração, as diretivas do runner e as regras para escrever uma migration estão em
[migrações](../development/migrations.md). O que um operador precisa saber:

- **Na inicialização.** O servidor aplica os arquivos pendentes antes de informar que está pronto ou de iniciar
  os workers (`LOOM_BOOT_MIGRATE=true`, o padrão). Todo processo, em qualquer container, obtém
  o lock `KeeperMap` em `loom._migration_lock`, de modo que cada arquivo seja aplicado uma única vez. Um servidor
  sem Keeper é recusado.
- **Reconstruções de provider.** Uma inicialização com arquivos pendentes primeiro se registra como participante
  do provider e, em seguida, verifica sob o lock de configuração do provider (uma chave do Keeper, mantida apenas
  durante essa verificação) que nenhuma reconstrução de provider está inacabada, e se recusa a migrar caso haja
  uma: `Finish the provider rebuild before applying schema migrations`. Depois disso, os arquivos
  são aplicados apenas sob o lock de migração. Uma reconfiguração iniciada nesse meio-tempo espera até que o
  processo em inicialização a reconheça, de modo que uma reconstrução nunca é executada junto com uma migração.
- **Um container encerrado à força durante uma migração ou reconfiguração** bloqueia os outros apenas por um
  tempo. O lock de migração é assumido quando seu detentor para de renová-lo (`ttl_s`).
  O lock do provider (TTL de 60 s, renovado a cada 20 s) é assumido quando expirou e a linha de participante
  de seu detentor também expirou: nenhum heartbeat por 60 s (um a cada 10 s) e nenhuma
  consulta marcada com seu id em `system.processes` em nenhum nó. Containers que iniciam juntos
  esperam até 120 s pelo lock do provider em vez de falhar. Um lock cujo detentor não
  tem linha de participante é liberado com `loom provider-lock clear --holder <holder>` depois que
  o processo é confirmado como parado; o comando se recusa enquanto o detentor renova,
  envia heartbeats ou tem uma consulta em execução.
- **Mutations.** Um arquivo só é registrado depois que suas mutations terminam. Uma mutation que falhou,
  ou que passou de `LOOM_MIGRATE_MUTATION_WAIT_S` (padrão 600), faz o arquivo falhar e
  interrompe a inicialização, com o arquivo indicado no log.
- **Mudanças de chave.** Uma migration que altera uma chave de ordenação ou de partição reconstrói por conta própria
  uma tabela vazia e faz a inicialização falhar se a tabela tiver linhas. Execute `scripts/ops/table_rebuild.py`
  para essa tabela antes do deploy; o dry run estrito detecta o problema.
- **Instalação nova.** Uma instalação nova e uma atualização seguem o mesmo caminho: o servidor reaplica
  o que o ledger não tiver. Em um banco de dados vazio, isso é o journal inteiro e leva
  alguns minutos.
- **Ledger interrompido.** Um banco de dados cujo ledger parou no meio dos arquivos que um
  baseline substitui é recusado (`stranded`): migre-o primeiro com um build anterior a esse
  baseline.
- **Arquivos renumerados.** Uma linha do ledger cujo arquivo o repositório não tem mais é
  `ledger-only`, e `--strict` termina com código 1 nesse caso, exceto quando seu SHA-256 registrado é o de
  um arquivo atual: trata-se do mesmo arquivo com um novo número. O runner o informa como renomeado,
  registra o novo nome sem executá-lo de novo se ele estiver pendente, e
  `--strict` passa. Um arquivo editado depois de executado sob o nome antigo tem outro SHA-256,
  então continua ledger-only: seu novo nome é executado como arquivo pendente, e a linha antiga é
  removida manualmente ([solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)).
  `/v1/health` lista a linha antiga em `schema.ledger_only` até lá.

A CLI executa o mesmo runner manualmente:

```bash
loom migrate --status              # applied and pending; changes nothing
loom migrate --dry-run --strict    # pending files; exit 1 on ledger-only rows (not renames) or checksum drift
loom migrate                       # apply pending migrations
loom migrate --target N            # apply through file number N only
```

`make migrate`, `make migrate-status` e `make migrate-dry` executam o mesmo módulo a partir de um
checkout.

<a id="upgrade-procedure"></a>

## Procedimento de upgrade

1. Leia as notas da [versão no GitHub](https://github.com/ClickHouse/loom/releases) para ver os passos do operador (novas configurações, backfills que um deployment deve executar) e as
   mudanças de protocolo visíveis ao cliente.
2. A partir do checkout do novo build, contra o ClickHouse de produção, execute
   `loom migrate --dry-run --strict`. Ele lista o que o boot vai aplicar e para diante de
   problemas no ledger ([solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)).
   Se a versão declarar uma reconstrução de tabela, execute `scripts/ops/table_rebuild.py` para essa
   tabela agora. Um deployment da autoridade Postgres que migra para um build que mantém
   os heads de documentos de conhecimento no Postgres remove `LOOM_RETENTION_DOCUMENTS_DAYS` agora, à
   mão: o build mais novo se recusa a subir com essa variável definida, e sob essa autoridade nenhum
   TTL de retenção alcança `loom.documents`, e o `retention_days` de um namespace também não
   alcança `loom.chunks`
   ([upgrades graduais](../architecture/rolling-upgrades.md), regra 38). Uma
   [célula](../architecture/cells.md) que migra para um build que marca suas sessões e
   carrega os triggers de org (regra 47) primeiro aponta seu `LOOM_PG_DSN` direto para o
   Postgres, sem passar por nenhum pooler ([Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)), e uma célula posterior preparada
   e depois revertida com a remoção de `LOOM_CELL` é limpa antes
   ([solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)):
   o build mais novo restringe seus processos conforme esse estado. Em uma célula posterior já preparada,
   o primeiro boot com migração do build mais novo cria os triggers de org, então um pod mais antigo
   ainda em execução ali tem todo insert de uma linha de org de tenant recusado (um
   erro sem mapeamento para seus chamadores) até ser substituído: faça o rollout da célula em uma única etapa.
3. Faça o deploy da nova imagem. O primeiro contêiner a subir aplica as migrations; os demais
   esperam por ele e depois não encontram nada pendente.
4. Espere `/v1/health` informar `status: ok` e um `schema.pending` vazio antes de
   direcionar tráfego para ele. O [par de zero downtime](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)
   faz os passos 2 a 4 por você, exceto remover a configuração que o passo 2 menciona: o `redeploy.sh`
   executa o dry run estrito no preflight e só troca depois que a nova cor está
   pronta.
5. Execute os backfills da versão, se houver ([manutenção](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#backfills-and-repairs)).
6. Atualize os plugins do cliente quando a versão mencionar uma mudança de plugin: os hooks rodam
   na máquina do cliente e mudam apenas pelo gerenciador de plugins do cliente
   (`claude plugin update loom@loom` para o Claude Code), nunca por um deploy do servidor.
7. Quando a última réplica antiga tiver parado, reverifique as erasures concluídas durante o
   rollout: `loom erasure-re-verify --completed-since <when step 3 began> --resume`. A saga de uma
   réplica antiga não conhece as tabelas que o novo build adicionou
   ([upgrades graduais](../architecture/rolling-upgrades.md), regra 14). O comando
   exclui apenas links de linhas órfãos: linhas de `entity_triple_rows` e `entity_edge_rows`
   no escopo de cada erasure cuja memória não tem linha viva e nenhuma linha escrita desde o
   censo da erasure. Sem `--resume`, ele as conta e sai com 1 se restar alguma.
   Os dados escritos depois da erasure permanecem.
8. Ative um recurso que o build anterior não consegue honrar somente quando todas as réplicas executarem
   o novo build: `memory_extract_agent_learnings`, já que o build anterior não
   apaga `loom.learnings_supporters`
   ([upgrades graduais](../architecture/rolling-upgrades.md), regra 23);
   `LOOM_DOCUMENT_OBJECT_STORE=s3`, já que o build anterior lê o `data` vazio de uma linha de bucket
   e responde ao download com `410 BLOB_GONE` (regra 9); e
   `LOOM_PG_PAYLOAD_CLEAR_ENABLED`, não antes de um horizonte de limpeza depois que o novo
   build passar a servir sozinho, já que o build anterior não carimba uma entrega tardia (regra 43).
9. Um deployment com `LOOM_CODE_ASK_MEMORY_NAMESPACE` definido, agora ou em qualquer momento antes,
   que migra de um build cujas respostas de `code.ask` em cache não nomeiam a memória que
   citaram (um anterior a `086_code_answer_memory_scope.sql`) para um que nomeia, incluindo um roll
   forward após um rollback: quando o build mais novo servir sozinho, sem nenhuma
   reconstrução de provider em execução, limpe o cache de respostas com
   `TRUNCATE TABLE <LOOM_CH_DATABASE>.code_answers` (`loom.code_answers` por padrão). As
   respostas e tombstones do build antigo não nomeiam nenhuma memória, e uma erasure ou redact que ele executou
   durante o rollout não conhecia a tabela
   ([upgrades graduais](../architecture/rolling-upgrades.md), regra 36), então nenhuma saga consegue encontrar
   o que elas guardam. Uma reconstrução publica sua cópia da tabela por cima dela, então uma em execução
   durante o truncate traz as linhas de volta. Um truncate descarta as partes de imediato, em toda
   réplica de uma tabela replicada, sem nenhuma mutation a esperar, e o usuário loom tem
   o grant. Cada pergunta limpa custa uma síntese na próxima vez que for feita.
10. Ao migrar para um build cuja erasure de usuário alcança o que um resumo consolidado das
    memórias do usuário alimentou, incluindo um roll forward após um rollback: quando o build mais novo
    servir sozinho, solicite novamente cada erasure de usuário sem `purge` solicitada antes disso
    (as linhas `user_id` de `loom.erasure_requests`, mantidas por 90 dias), incluindo uma que o build mais novo
    concluiu: a saga de um build mais antigo deixou o histórico, os chunks, os vetores,
    as linhas de grafo e as cópias compartilhadas do resumo ([upgrades graduais](../architecture/rolling-upgrades.md),
    regra 37).
    A nova saga também apaga o que o usuário escreveu desde então: solicite-a para um usuário sem
    nenhuma linha de memória viva, ou apenas onde isso for desejado. Um purge de usuário que um build mais antigo concluiu
    deixou as mesmas linhas, e nenhuma erasure de usuário posterior as encontra: as chaves do usuário sumiram.
    Um que ele executou além de `shared.tombstone` também deixou as linhas de busca das cópias compartilhadas
    das memórias do próprio usuário, qualquer que seja o build que o concluiu. Uma cópia compartilhada que um build mais antigo
    promoveu com `agent="*"` nomeia `*` como seu agente de origem, e nenhuma erasure do
    usuário que ela copiou pode encontrá-la. Liste-as e revise cada uma, com `memory.demote` para uma
    que contenha o texto de um usuário apagado:

    ```sql
    SELECT org, namespace, memory_key FROM loom.shared FINAL
    WHERE tombstoned_at IS NULL AND arrayExists(x -> splitByChar('/', x)[3] = '*', promoted_from)
    ```

11. Um deployment da autoridade Postgres que migra para um build que mantém os heads de documentos de
    conhecimento no Postgres (`002_knowledge_documents.sql`), incluindo um roll forward após um
    rollback: quando o build mais novo servir sozinho, execute novamente toda erasure que
    foi concluída enquanto uma réplica de um build mais antigo servia, desde que o build mais novo serviu pela primeira
    vez. A erasure de um build mais antigo não remove nenhum head nem entrega: o texto de um documento apagado
    permanece em `loom.knowledge_heads` até a erasure ser executada de novo, e uma entrega que ela
    deixou é publicada novamente pelo worker de entrega
    ([upgrades graduais](../architecture/rolling-upgrades.md), regra 38). Faça isso antes de
    tentar de novo uma entrega estacionada.
12. Um deployment da autoridade Postgres que migra para um build que mantém jobs de ingestão
    de conhecimento (`005_knowledge_jobs.sql`) ou o catálogo de datasets
    (`007_knowledge_datasets.sql`) no Postgres, incluindo um roll forward após um rollback:
    quando o build mais novo servir sozinho, execute novamente toda erasure que foi concluída
    enquanto uma réplica de um build mais antigo servia, desde que o build mais novo serviu pela primeira vez. A
    erasure de um build mais antigo não remove nenhum job de `loom.knowledge_jobs` nem nenhuma linha de catálogo
    de `loom.knowledge_datasets`: o worker do build mais novo projeta esse job de volta em
    `loom.knowledge_ingestions` e o executa, e as linhas do catálogo permanecem até que a
    erasure seja executada de novo ([upgrades graduais](../architecture/rolling-upgrades.md), regras
    40 e 42). As execuções do passo 11 cobrem igualmente os heads, os jobs e as linhas do catálogo; uma
    execução por erasure basta. Peça de novo, também, cada `knowledge.delete` e
    `knowledge.bulk_delete` que um build mais antigo respondeu nessa janela: ele removeu os
    datasets das fontes apenas de `loom.datasets`.
13. Ao migrar para um build que mantém fontes de documentos em qualquer namespace, incluindo um roll forward
    após um rollback: quando o build mais novo servir sozinho, retire as fontes que um
    `namespace.delete`, `collection.delete` ou erasure de namespace de um build mais antigo deixou
    ativas ([upgrades graduais](../architecture/rolling-upgrades.md), regra 44). As
    fontes em um namespace excluído:

    ```sql
    SELECT s.org, s.namespace, s.name
    FROM (SELECT org, namespace, name FROM loom.knowledge_sources FINAL WHERE active) AS s
    INNER JOIN (SELECT org, namespace FROM loom.namespaces FINAL WHERE NOT active) AS n
        ON s.org = n.org AND s.namespace = n.namespace
    ```

    e as fontes registradas antes da conclusão de uma erasure de namespace que ela não
    retirou (uma que um build mais antigo executou, ou uma que um build mais antigo adicionou enquanto uma erasure do build mais novo rodava):

    ```sql
    SELECT s.org, s.namespace, s.name, e.id
    FROM (
        SELECT org, namespace, name, active, created_at, updated_at
        FROM loom.knowledge_sources FINAL
    ) AS s
    INNER JOIN (
        SELECT id, org, completed_at,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    ) AS e ON s.org = e.org AND s.namespace = e.namespace
    WHERE s.created_at <= e.completed_at AND (s.active OR s.updated_at > e.completed_at)
    ```

    Para a primeira consulta, remova cada fonte listada (`knowledge.source.remove`), ou
    exclua seu namespace novamente com o build mais novo, que a retira. Para a segunda,
    remova cada fonte listada com `knowledge.source.remove` e
    `delete_documents=true`, que retira os documentos que ela gravou de volta (ela aceita uma fonte
    já retirada, por uma exclusão posterior do namespace de uma delas), e exclua com
    `memory.delete` as linhas de decisão extraídas deles. Uma erasure de namespace com o
    build mais novo faz ambos, mas também apaga tudo o que foi gravado no namespace desde
    a primeira erasure; solicite-a de novo apenas para um namespace em que ninguém gravou
    desde então. Uma fonte registrada depois da erasure não é listada: a erasure nunca
    a cobriu. A segunda lista é uma lista de trabalho para agir uma única vez: uma fonte removida
    como acima continua listada, já que sua linha de remoção é mais nova que a erasure.

    Uma erasure de namespace que um build mais antigo iniciou e o build mais novo concluiu não precisa
    de nada aqui: seu censo final encontra os documentos e decisões que sua fonte
    gravou de volta e reabre os respectivos passos, de modo que a saga só é concluída quando eles já não existem.

14. Ao migrar para um build cuja erasure de namespace alcança `loom.shared`, incluindo um roll forward após um
    rollback: quando o build mais novo servir sozinho, limpe as linhas promovidas que a erasure de namespace
    de um build mais antigo deixou ([upgrades graduais](../architecture/rolling-upgrades.md),
    regra 45). A consulta abaixo lista cada namespace que uma erasure `namespace` ou `namespace_list`
    no ledger (mantido por 90 dias) alcançou e que contém uma linha promovida, com o
    horário em que a última sem purge (`erased_at`) e a última com purge (`purged_at`) executou seu censo.
    As instruções abaixo pegam as linhas promovidas antes desse segundo, então uma linha promovida
    depois do censo permanece, e o mesmo pode ocorrer com uma promovida antes, no mesmo segundo do censo,
    o que os segundos inteiros de `promoted_at` não conseguem distinguir; o passo do build mais novo já
    pegou essa linha em uma erasure que ele executou.
    Apenas uma `namespace_list` faz purge, e uma linha `namespace` de um build mais antigo pode conter a flag
    com que foi solicitada.

    ```sql
    SELECT org, namespace,
           maxIf(counted_at, NOT purged) AS erased_at,
           maxIf(counted_at, purged) AS purged_at
    FROM (
        -- The census's time: a row promoted after it was never in the erasure's reach.
        SELECT org, ifNull(verified_at, completed_at) AS counted_at,
               scope_type = 'namespace_list' AND purge AS purged,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    )
    -- Only a namespace that holds a promoted row has anything to clear.
    WHERE (org, namespace) IN (SELECT org, namespace FROM loom.shared)
    GROUP BY org, namespace
    ```

    Para cada um, execute o que o passo do build mais novo executa, sobre toda versão promovida antes do
    censo da erasure, incluindo uma versão que uma promoção posterior da mesma chave oculta de `FINAL`.
    Onde `purged_at` estiver definido, exclua-as:

    ```sql
    ALTER TABLE loom.shared DELETE
    WHERE org = '<org>' AND namespace = '<namespace>' AND promoted_at < '<purged_at>'
    SETTINGS mutations_sync = 2
    ```

    e onde `erased_at` estiver definido, marque com tombstone as vivas que restaram:

    ```sql
    ALTER TABLE loom.shared UPDATE tombstoned_at = toDateTime('<the time you run it>')
    WHERE org = '<org>' AND namespace = '<namespace>' AND tombstoned_at IS NULL
      AND promoted_at < '<erased_at>'
    SETTINGS mutations_sync = 2
    ```

    A configuração espera cada instrução em todas as réplicas. O horário é escrito como
    literal, como o passo o vincula, porque uma tabela replicada recusa uma mutation que chama
    uma função não determinística como `now()`. Uma erasure de namespace solicitada
    antes dos 90 dias do ledger não é listada, e as linhas promovidas que ela deixou não podem ser distinguidas
    das de um namespace vivo; elas permanecem como o build mais antigo as deixou.

15. Ao migrar para um build em que todo comando da CLI que declara uma org (um comando de code-index, uma
    importação) verifica seu deployment: sob qualquer das autoridades, seu usuário do ClickHouse precisa de
    `SELECT` em `loom.storage_authority`, e o comando se recusa a rodar contra um deployment que
    registra outra autoridade. Sob a autoridade Postgres, ele também precisa de `LOOM_PG_DSN`
    e lê o estado da célula (com `LOOM_CELL` não definido, em uma sessão própria). Com
    `LOOM_CELL` definido, um comando que não grava para nenhuma org mas altera os stores da célula
    (`loom settings set`, `loom secrets reseal`, `loom code compact` e os demais) ou
    revoga acesso verifica seu deployment da mesma forma e lê a linha `first` da célula em uma
    sessão própria. Conceda ao role do Postgres com que ele se conecta:
    - com `LOOM_CELL` não definido, `USAGE` no schema `loom`, `SELECT` em `loom.cell_first`,
      `loom.tenancy_mode` e `loom.authority`, `SELECT (updated_at)` em `loom.cell_self`
      e `SELECT (since)` em `loom.tenancy` (os grants por coluna mantêm os fingerprints e
      as orgs fora de seu alcance), e um `CONNECTION LIMIT` de pelo menos `LOOM_PG_POOL_MAX_SIZE`
      vezes o número desses comandos que rodam ao mesmo tempo, já que cada um abre seu próprio pool para
      suas gravações (a leitura de estado tenta de novo quando o limite está cheio, brevemente, e depois se recusa);
    - com `LOOM_CELL` definido, o mesmo com `SELECT` em `loom.cell_self` e `loom.tenancy`
      por inteiro, e, para registrar uma nova org enquanto a primeira célula está `recording`, `INSERT` em
      `loom.tenancy` e `UPDATE (since)` em `loom.tenancy_mode`; revogue ambos com
      `LOOM_CELL_ROUTER_TOKEN` quando a célula for anexada. As configurações de bucket de documentos
      (`LOOM_DOCUMENT_S3_BUCKET`, `LOOM_DOCUMENT_S3_PREFIX`) são iguais às dos servidores.

    Uma gravação que um role com apenas os grants por coluna faz em uma célula além de `recording` é
    recusada como erro de permissão (SQLSTATE 42501) em vez de `LT001`.

16. Antes do attach de uma primeira célula, toda imagem de CLI e de CI executa um build com esse estado lido, e todo CLI que escreve para uma org tem `LOOM_CELL` definido. Nenhum CLI envia heartbeat, então o attach também não consegue verificar isso, e um CLI de um build mais antigo não é recusado em uma célula `enforced` ([células](../architecture/cells.md#failure-modes)).

17. Ao migrar para um build em que os processos da primeira célula leem o estado da própria célula a partir do router em um temporizador: toda réplica do router (`loom router serve`) executa este build antes de qualquer processo de qualquer célula. Cada processo de uma primeira célula preparada consulta a rota de estado do router desde a sua primeira leitura do estado da célula até ler `serving`, e um router de um build anterior lê o diretório a cada chamada desse tipo: cerca de uma leitura por processo de servidor a cada `min(LOOM_INSTANCE_HEARTBEAT_SECONDS, LOOM_TENANCY_MODE_CACHE_S)`, ou seja, 1,600 leituras por segundo para mil pods de oito workers nos valores padrão; um router deste build as responde com uma leitura por réplica a cada `LOOM_ROUTER_DIRECTORY_CACHE_S` ([células](../architecture/cells.md#the-heartbeats-keys)).

Enquanto uma migração é aplicada, o build antigo continua atendendo: toda migração é aditiva. As regras que permitem que os dois builds atendam lado a lado estão em [atualizações graduais](../architecture/rolling-upgrades.md).

<a id="rollback"></a>

## Reversão

Faça o redeploy da imagem anterior; com o par, `./redeploy.sh --rollback`. Antes de reverter
para um build sem os learnings de turnos do assistente, desative
`memory_extract_agent_learnings` e apague as linhas de `loom.learnings_supporters` e as
linhas `assistant_turn` de `loom.learnings_queue`
([extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#rules-the-assistant-states-about-itself)).
O build anterior serve uma regra de bot promovida a todo agente do namespace, não apenas
ao seu bot. Para evitar isso, apague com `knowledge.delete` os documentos marcados com
`source-kind:assistant_turn` antes da reversão; caso contrário, aceite a distribuição mais ampla.
Uma exclusão de namespace que o build anterior executa depois que existem guias de namespace
deixa o guia desse namespace; execute a exclusão novamente quando o build mais novo voltar a
servir ([atualizações graduais](../architecture/rolling-upgrades.md), regra 24). Uma exclusão de
namespace que o build anterior executa deixa ativas as linhas promovidas do namespace; o
passo 14 do procedimento de upgrade as lista quando o build mais novo voltar a servir (regra 45).
Após uma reversão para um build sem máximos de token de bot, os tokens de todo bot duram uma
hora, e um bot que esse build salva mantém 3600 como máximo; reaplique um máximo menor ou
maior quando o build mais novo voltar a servir
([atualizações graduais](../architecture/rolling-upgrades.md), regra 26).
Uma exclusão de usuário que o build anterior executa deixa o que um resumo das memórias do
usuário alimentou; solicite novamente cada uma executada sem `purge` quando o build mais novo
voltar a servir, como no passo 10; uma purga que ele concluiu não é encontrada de novo (regra 37).
Não reverta para antes do build que adiciona o bucket de documentos enquanto alguma
linha do catálogo nomear um objeto nele: um build mais antigo responde a esses downloads com
`410 BLOB_GONE`, sua exclusão apaga as linhas e não alcança seus objetos, e sua varredura nunca
percorre o bucket ([onde os originais são armazenados](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#where-originals-are-stored)).
Após uma reversão para um build anterior a `086_code_answer_memory_scope.sql` com um namespace
de memória vinculado a `code.ask`, limpe o cache de respostas como no passo 9 duas vezes: uma
quando o build mais antigo serve sozinho e outra quando o build mais novo voltar a servir. A
migração já está no ledger, então o roll forward não aplica nada que motive a segunda limpeza.
Desative `LOOM_PG_PAYLOAD_CLEAR_ENABLED` antes da reversão e reative-o somente depois de um
horizonte de limpeza após o build mais novo voltar a servir sozinho; depois que uma passagem
de limpeza tiver sido executada, execute `loom reconcile` apenas com um build que inclua
`010_revision_delivered_at.sql`, inicie o deployment nesse build em seguida e reverta
somente quando o build mais novo não tiver mais nenhuma entrega reivindicável para uma revisão
limpa, o que esta consulta retorna como 0 (regra 43):

```sql
SELECT count(*) FROM loom.memory_deliveries d
JOIN loom.memory_revisions r USING (org, namespace, seq)
WHERE r.row IS NULL AND NOT d.settled AND d.lease_until <> 'infinity';
```

Até que retorne 0, deixe o build mais novo entregá-las. O build anterior nunca reivindica uma
entrega estacionada, então uma estacionada pode permanecer. Enquanto o build anterior serve,
uma nova tentativa deixa de fora as revisões limpas
([operações do Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#what-the-boot-checks)), e nenhum `loom reconcile` é executado:
ele liberaria os holds do reconcile para o build anterior.
Antes de reverter uma célula preparada (uma com uma linha `loom.cell_first`) para um
build sem os triggers de org de [célula](../architecture/cells.md), pare todos os pods do build
mais novo e execute apenas a remoção dos triggers de
[Um processo com LOOM_CELL não definido é bloqueado](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated),
não a transação de estado dela (a célula continua preparada), e execute-a de novo se um pod
mais novo iniciar com migrações ativadas antes de a reversão terminar: as sessões do build mais
antigo não enviam marcador de tenancy, então, quando o build mais novo tiver criado os triggers
e o guard `loom.tenancy`, cada uma de suas escritas para uma org para a qual a célula não tem
linha de tenancy é recusada (em uma célula posterior, antes do attach, toda org tenant), assim
como sua linha de tenancy para cada org recém-alocada, e suas outras escritas pagam a leitura de
estado dos triggers.
Não há migração a desfazer: o build anterior lê e escreve o schema mais novo. Suas gravações de
versão em `loom.memory` e nas outras tabelas de memória `ReplacingMergeTree` preservam as colunas
que ele não conhece
([atualizações graduais](../architecture/rolling-upgrades.md)).

Não há down-migrations. Para retornar a um schema anterior, restaure um backup feito antes do
upgrade ([backup e recuperação](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)).

<a id="downgrading-an-installer-install"></a>

### Downgrade de uma instalação feita pelo instalador

Cada bundle do instalador indica a versão do ClickHouse com a qual foi construído, então
instalar uma versão anterior do Loom também executa um ClickHouse anterior em um volume que o
mais novo escreveu, o qual pode não conseguir ler. Para reverter apenas o Loom, mantenha
o ClickHouse mais novo: antes de executar o instalador anterior (ele inicia a stack
imediatamente), fixe no `.env` do diretório de instalação a imagem que o ClickHouse executa
agora. Este comando imprime a linha a colocar lá, substituindo qualquer linha
`LOOM_CLICKHOUSE_IMAGE` anterior:

```bash
dir=${LOOM_INSTALL_DIR:-$HOME/.loom}
echo "LOOM_CLICKHOUSE_IMAGE=$(docker inspect -f '{{.Config.Image}}' \
  "$(docker compose --project-directory "$dir" ps -q clickhouse)")"
```

Em seguida, execute o instalador da versão anterior informando a versão dela, pois um instalador
mais antigo sobre uma instalação mais nova se recusa a rodar sem isso:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/vX.Y.Z/install.sh -o loom-install.sh && LOOM_VERSION=X.Y.Z sh loom-install.sh && rm loom-install.sh
```

Execute-o a partir do seu diretório home ou de outro
[diretório em que só você possa escrever](../getting-started/quickstart.md#fast-path-released-image-no-clone).

O instalador mantém o `.env`, então a fixação mantém o ClickHouse também em upgrades posteriores:
apague a linha `LOOM_CLICKHOUSE_IMAGE` antes de instalar uma versão mais nova do Loom e não
deixe `LOOM_CLICKHOUSE_IMAGE` exportada no shell, pois o instalador grava de volta no `.env` um
valor que recebe. Para voltar em ambos, restaure um backup feito antes do upgrade.

<a id="what-stays-the-same-across-an-upgrade"></a>

## O que permanece igual em uma atualização

| Em uma atualização                                   | O que se mantém                                                                                                              | Garantido por                             |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| A build anterior com o novo schema                   | continua lendo e gravando todas as tabelas que já conhecia, então um rollback não exige nenhum passo de schema               | `tests/unit/test_migrations_additive.py`  |
| Um arquivo de migração já aplicado                   | nunca é editado nem excluído; uma correção é um novo arquivo                                                                 | o checksum do ledger; `--dry-run --strict` |
| Um cliente que envia um campo que o servidor removeu | é atendido; o campo é indicado em `ignored_args`                                                                             | `server/transport/arg_check.py`           |
| A build anterior e a nova atendendo juntas           | cada uma lê o que a outra gravou, inclusive as novas colunas, exceto um recurso ativado somente após o rollout (passo 8)    | `make test-mixed-version`                 |

Uma atualização pode alterar ou remover operações e campos. Verifique o que um deployment
atende com o catálogo de capacidades
([API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)) e liste o que
mudou entre duas builds com `scripts/codegen/wire_changelog.py <previous> <new>`
([geração de código](../development/code-generation.md#wire-changelog)).

<a id="related"></a>

## Relacionados

- [Implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair): o par que transforma uma atualização em uma troca
- [Migrações](../development/migrations.md): as regras que uma nova migração segue
- [Atualizações graduais](../architecture/rolling-upgrades.md): as regras para as builds N-1 e N
- [Solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)
