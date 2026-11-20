<!-- i18n: source=docs/architecture/failure-and-recovery.md blob=40f8e4b57e275dada1e2090b1f563fcdaafd8095 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/failure-and-recovery.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/failure-and-recovery.md), which is authoritative.

<a id="failure-and-recovery"></a>

# Falha e recuperação

A gravação primária e toda decisão de acesso falham de forma fechada (fail closed). Dados derivados e telemetria falham de forma aberta (fail open) e são reparados. Uma requisição que retorna sucesso armazenou de forma durável o que prometeu; qualquer coisa projetada ao lado dela é recuperada por um worker. A última seção, [Capacidade](#capacity), cobre o que escala e como, e onde ficam os limites. Os runbooks por sintoma estão em [solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md).

<a id="errors-retries-and-idempotency"></a>

## Erros, novas tentativas e idempotência

| Fronteira            | Comportamento                                                                                                                                                                                                                                                                                                                                                                                       |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cliente ClickHouse   | Uma falha transitória de rede em uma instrução idempotente é repetida uma vez em uma conexão nova. Um erro de servidor vinculado à réplica (códigos 10, 242, 999) é repetido em até duas conexões novas. Qualquer outro erro é propagado.                                                                                                                                                           |
| Prazos das instruções | Ao passar do prazo da sua classe (`LOOM_CH_TIMEOUT_READ_S`, `_WRITE_S`, `_DDL_S`), uma requisição falha com 504 `BACKEND_TIMEOUT`, sem nova tentativa.                                                                                                                                                                                                                                              |
| Admissão no pool     | Nenhum slot livre dentro de `LOOM_CH_POOL_WAIT_MS`: 503 `OVERLOADED` com `Retry-After: 1`.                                                                                                                                                                                                                                                                                                          |
| Inserts              | Um INSERT repetido carrega o mesmo `insert_deduplication_token`, de modo que um lote já confirmado não é gravado duas vezes. Sem o token, um insert não é repetido. Um insert em lote compartilha o flush do ClickHouse com inserts concorrentes na mesma tabela; um flush que falha por inteiro faz cada um deles falhar, e cada nova tentativa é gravada uma única vez sob o seu próprio token ([inserts em lote](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#batched-inserts)). |
| Configurações não suportadas | Uma réplica que rejeita a configuração de leitura dos códigos quantizados faz o cliente descartá-la pelo resto do processo e tentar de novo.                                                                                                                                                                                                                                                |
| Leituras pontuais    | Leituras após a própria escrita do chamador usam consistência sequencial; uma leitura sem resultado logo após uma escrita é refeita uma vez (`LOOM_CH_READ_YOUR_WRITES`); uma busca em um namespace escrito nos últimos 20 s lê de forma sequencial.                                                                                                                                                 |
| Defasagem de schema  | Uma instrução que cita uma tabela que uma migração pendente criaria é recusada com 503 `SCHEMA_PENDING` (veja abaixo).                                                                                                                                                                                                                                                                              |
| Idempotência de ops  | Ops declaradas como `idempotent` aceitam `Idempotency-Key` (HTTP) ou `_idempotency_key` (MCP). A resposta é mantida em `loom.idempotency` por 24 horas, com chave composta por org, subject, op e key. Uma duplicata enquanto a primeira ainda executa recebe um 409.                                                                                                                              |
| Escrita primária     | A linha do fato e a sua linha de vetor são inserts confirmados e aguardados. Uma falha faz a requisição falhar.                                                                                                                                                                                                                                                                                     |
| Autoridade Postgres  | Com `LOOM_STORAGE_AUTHORITY=postgres`, a escrita é o commit no Postgres; as suas linhas no ClickHouse são publicadas a partir dele e reparadas pelo worker `pg_delivery`. Com o Postgres fora do ar, escritas e leituras pontuais falham de forma fechada (503 `POSTGRES_UNAVAILABLE`) ([autoridade de armazenamento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md#failure)). |
| Projeções            | As tabelas auxiliares de grafo, chunks, evidências e supersessão falham de forma aberta e são reparadas por workers.                                                                                                                                                                                                                                                                                |
| Telemetria           | `agent_events` e o stream de eventos ficam em fila no processo; um flush de `agent_events` é um insert em lote, e o de um stream de eventos é um insert confirmado. Nenhum dos dois faz uma requisição falhar; uma queda pode perder a fila ainda não descarregada.                                                                                                                                  |
| Workers              | Uma passagem que falha é repetida com backoff. O trabalho é gravado como valores absolutos ou novas versões, de modo que uma passagem repetida converge.                                                                                                                                                                                                                                            |
| Apagamento           | `erasure.request` é uma saga com checkpoints. `erasure_recovery` a retoma, e um censo de resíduos em todas as tabelas decide o sucesso.                                                                                                                                                                                                                                                             |

**Respostas que permitem nova tentativa.** `OVERLOADED`, `AUTH_UNAVAILABLE` e `RATE_LIMITED` carregam
`Retry-After` (e `retry_after_s`); tente de novo após o intervalo indicado. `SCHEMA_PENDING` some quando um
operador aplica a migração indicada. `EMBED_TIMEOUT`: um chamador HTTP ou MCP direto
tenta de novo uma vez; um chamador via SDK já tentou de novo (ops idempotentes), então deve aguardar com backoff.
`BACKEND_TIMEOUT` permite nova tentativa apenas para uma op `idempotent`, com uma chave de idempotência para uma
escrita. A lista completa está na [referência de erros](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).

**Por que o cache de idempotência é de melhor esforço:** um cache consistente em todo o cluster exige um lock por
requisição. As ops que o usam gravam o mesmo conteúdo, em uma execução duplicada, em uma
`ReplacingMergeTree`, então uma disputa custa uma segunda execução do handler, não um segundo efeito. Ops cuja
repetição dobraria um efeito (`memory.set_from_messages`) não são idempotentes e o ignoram.

<a id="consistency-boundaries"></a>

## Limites de consistência

As escritas entre tabelas não são transacionais. O fato e o seu vetor são gravados juntos; os dados derivados se atualizam depois:

| Dados derivados                | Quando se atualizam                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Estatísticas BM25             | Views atualizáveis a cada minuto; a busca lê uma cauda ao vivo, então o ranqueamento permanece exato (`LOOM_MEMORY_SEARCH_BM25_LIVE_TAIL`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Contadores de uso, reforço    | Uma passagem de `usage_fold` (a cada 300 s).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Projeção do grafo de entidades | Logo após a resposta. Um `facts.traverse` do mesmo namespace que comece depois da resposta espera por ela (no máximo 3 s) em todos os processos da implantação: cada projeção em andamento é registrada em `loom.graph_projections` (chaves do Keeper onde o servidor tem um Keeper), e o seu término registra a escrita no grafo do namespace antes de limpá-la, no relógio do servidor. Um processo que morre no meio de uma projeção deixa o seu registro por no máximo 15 s: cada processo espera esse registro expirar uma vez (3 s) e, até o registro ficar obsoleto, as travessias desse namespace ali são lidas de forma atualizada, nunca servidas do memo nem guardadas nele. Acima de 32 projeções simultâneas de um mesmo namespace, as excedentes não são aguardadas; cada uma conta como uma escrita no grafo antes de o seu `memory.set` responder e de novo ao seu término. |

**Por que não transações:** o ClickHouse não tem transações entre tabelas, e emulá-las acrescenta uma viagem de ida e volta e um modo de falha a cada escrita. Um par primário durável mais um reparo idempotente mantém a escrita em uma única viagem de ida e volta.

<a id="backpressure"></a>

## Contrapressão

Nenhuma requisição espera indefinidamente por um recurso compartilhado:

- **Pool do ClickHouse:** uma espera limitada, depois `OVERLOADED` ([cliente do ClickHouse](#clickhouse-client)).
- **Bucket de documentos:** 8 transferências e 4 streams por processo, cada um com uma espera limitada
  (`LOOM_CH_POOL_WAIT_MS`), depois `OVERLOADED`; listagens e exclusões (apagamento, a varredura)
  esperam por um dos 4 slots próprios.
- **Limites de taxa:** 429 `RATE_LIMITED` com `Retry-After`. Via MCP, o transporte espera uma vez
  por uma dica curta (`LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S`) e verifica novamente.
- **Embedding da consulta:** `LOOM_MEMORY_SEARCH_EMBED_BOUND_S` (2.5 s) por tentativa, repetida
  `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES` vez(es) (1), depois 503 `EMBED_TIMEOUT`.
- **Filas de telemetria:** o emissor de `agent_events` (10,000 linhas) e o stream de eventos
  (20,000) descartam e contam as linhas quando estão cheios (`dropped_queue_full`, `dropped_flush_failed`),
  registrando em log o primeiro descarte e a cada 1000º. A entrega é no máximo uma vez.
- **Workers:** `usage_fold` pula uma passada enquanto o ClickHouse tem mutações pendentes demais;
  cada passada é limitada pelo tamanho do lote e por um prazo.
- **Orçamentos:** uma op acima do seu `budget_p99_ms` ainda responde e deixa uma linha
  `budget.violated` (no máximo uma por op a cada `LOOM_BUDGET_VIOLATION_AUDIT_INTERVAL_S`, 60 s).

<a id="schema_pending"></a>

## `SCHEMA_PENDING`

Um servidor construído a partir de um commit mais recente que o seu schema (um servidor de branch com
`LOOM_BOOT_MIGRATE=false`, ou uma cor que iniciou antes de suas migrations) pode referenciar uma tabela
que ainda não existe. `server/core/ch_schema_guard.py` (`guard_pending_schema`) recusa esse comando
com 503 `SCHEMA_PENDING`, informando a tabela e a migration. Uma etapa de recuperação que trata uma
leitura com falha como opcional continua sem ela, e a linha do ledger registra a tabela. DDL
nunca é recusado.

O conjunto pendente vem do ledger de migrations (`server/core/schema/status.py`),
mantido em cache por 60 s por processo. Quando o ledger informa
que nada foi aplicado ou que há mais de 25 arquivos pendentes, o guard fica inativo e emite um aviso.
`/v1/health` informa `schema.pending`, e uma troca blue/green espera que ele esteja vazio.

<a id="worker-supervision"></a>

## Supervisão de workers

`server/workers/supervisor.py` executa cada worker habilitado como sua própria task asyncio:

- **Escalonamento.** A primeira execução espera `crc32(name) % interval`, exceto que um worker cuja
  tabela de saída está vazia executa imediatamente, e um worker cuja última execução é mais antiga que seu
  intervalo começa em até 300 s após o boot.
- **Prazo limite.** Uma execução que passa de `min(4 × interval, 900 s)`, com piso de 60 s (a menos que o worker
  declare o seu próprio), é cancelada e contada como falha.
- **Backoff.** Uma execução com falha espera `min(60 s, 2^(n-1) s)`. Uma execução interrompida no prazo limite, ou uma
  execução com falha de um worker cujo intervalo é maior que seu prazo limite, espera o intervalo.
- **Degradado.** Três falhas consecutivas registram `DEGRADED` no log; a sequência é
  `loom.worker.consecutive_failures{worker}`, e uma execução com uma etapa que falhou conta em
  `loom.worker.partial_failures`.
- **Status.** Cada execução grava uma linha em `loom.worker_status` (`GET /v1/admin/workers.status`);
  um cursor de retomada permite que uma nova cor continue uma varredura longa.
- **Desligamento.** `stop_all` cancela as tasks, espera até 10 s e depois grava as linhas de status pendentes.

As exceções de um worker ficam dentro da sua task. Os workers gravam valores absolutos ou novas versões, então
uma execução que roda duas vezes converge.

<a id="repair-workers"></a>

## Workers de reparo

| Worker                    | Repara                                                                                                                                                                         | Garantia                                                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| `projection_repair`       | Reexecuta projeções com falha (links de evidência, chunks, tabelas laterais do grafo, o espelho de busca da varredura de expiração) registradas em `loom.memory_projection_failures`. | Entradas não suportadas permanecem para inspeção.                                     |
| `entity_extractor`        | Extrai entidades para linhas que não têm nenhuma; reemite linhas de `memory_embeddings` cujas `entities` divergem de `memory`.                                                 | Toda linha pesquisável acaba carregando suas entidades.                               |
| `orphan_vector_reconcile` | Marca com tombstone as linhas de embedding e de vetor de entidade cuja memória está com tombstone ou não existe mais, após um período de carência.                             | Nenhum vetor sobrevive ao seu fato.                                                   |
| `embedding_worker`        | Gera embeddings de stubs com `embedding_dim = 0`, dos mais novos para os mais antigos, e de memórias sem linha de embedding; refaz o embedding de stubs de um modelo mais antigo. | Uma linha que falha 5 vezes é marcada com `embed_failed_at` e ignorada.               |
| `erasure_recovery`        | Retoma sagas travadas em `in_progress` ou `failed` a partir de `steps_completed`.                                                                                              | Após `max_attempts` (5): `failed` terminal mais uma linha de auditoria `erasure.abandoned`. |
| `task_lease_sweep`        | Devolve leases expirados para `todo`, ou para `blocked` após esgotarem as tentativas; retoma tarefas adiadas.                                                                  | A tarefa de um agente que falhou não se perde.                                        |
| `audit_verifier`          | Recalcula os hashes de auditoria e verifica a cadeia de checkpoints.                                                                                                           | Adulteração é reportada, não reparada.                                                |

<a id="fail-closed-choices"></a>

## Escolhas de falha fechada

Estes casos recusam em vez de degradar, porque degradar ampliaria o acesso ou mentiria sobre
uma exclusão:

- **Revogação.** Até que um processo tenha carregado o conjunto de tokens revogados uma vez, todo token recebe
  503 `AUTH_UNAVAILABLE`. Depois disso, uma indisponibilidade do ClickHouse mantém o último snapshot.
- **Autorização.** Uma falha na leitura do mapa de concessões do sujeito responde 503, a menos que o
  processo tenha uma entrada em cache mais recente que `LOOM_JWT_REVOCATION_MAX_STALE_SECONDS`
  (600 s) que nenhuma mudança de concessão tenha substituído, e nesse caso ele a serve
  ([tokens de acesso](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#tokens)). A consulta única de concessão
  (`authz.grant_role_for`) trata uma leitura com falha como ausência de concessão. Um namespace restrito cuja
  classificação não pode ser lida é negado.
- **Migrações de boot.** Uma migração com falha interrompe o processo antes de qualquer worker iniciar.
- **Apagamento.** Um backend federado que não consegue confirmar a exclusão falha em sua etapa; resíduo
  em qualquer lugar do escopo faz a saga falhar.
- **Segredo de assinatura de desenvolvimento.** Uma implantação que exige autenticação se recusa a iniciar
  com o segredo versionado no repositório.

Estes casos falham abertos, com contagem ou registro em log, porque recusar derrubaria o serviço por causa de
um controle de disponibilidade: classificação de tráfego, o limitador de taxa, a leitura do cache de idempotência,
a inserção de auditoria (exceto as duas restaurações em [limites de perda de dados](#data-loss-boundaries)),
a calibração de boot, os caches de embedding e toda escrita de telemetria. Veja
[segurança](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md).

<a id="when-a-dependency-is-down"></a>

## Quando uma dependência está fora do ar

| Fora do ar            | O que acontece                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse            | `/v1/health` e `/v1/health/ready` respondem 503; `/v1/health/live` continua em 200. As requisições falham com `OVERLOADED`, `BACKEND_TIMEOUT` ou um 500 com o id da requisição. Os tokens são verificados com base no último snapshot de revogação. Os workers falham em suas execuções e aplicam backoff.                                                                                                                                                                                                                                                                                                                                                                  |
| Provedor de embeddings | `memory.set` e `memory.bulk_set` falham: nenhum fato é armazenado sem o seu vetor. Os caminhos que podem esperar (`memory.promote`, embeds de chunks) armazenam um stub para o `embedding_worker`. Um turno de conversa é armazenado com o erro de embed registrado e permanece sem embedding (nada tenta de novo); `conversation.compact` não grava resumo e não marca nenhum turno com tombstone (`embed-failed`). `memory.search` falha com 503 `EMBED_TIMEOUT`, a menos que o vetor da consulta esteja no cache de embeddings. Um provedor travado não define nenhum flag de saúde (o log mostra `query embed exceeded`); um provedor que responde com erros aparece como `provider_failing:<name>` em `/v1/health`. |
| Chave de embeddings ausente | O processo passa a usar o embedder falso, com um log de erro e uma entrada em `problems`; as buscas encontram apenas linhas gravadas com o modelo falso.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Provedor de LLM       | Cada chamada tenta de novo (quatro tentativas). Os estágios fail-open (HyDE, rerank, o verificador de extração, títulos) são ignorados; a extração e a síntese de `memory.ask` não têm substituto. `LOOM_LLM_FALLBACK_MODEL` transfere uma indisponibilidade para um segundo modelo.                                                                                                                                                                                                                                                                                                                                                                                       |
| Bucket de documentos  | Uploads e downloads de originais mantidos no bucket respondem 503 `BACKEND_UNAVAILABLE`, e o download de um original que não existe mais responde 410. Uma etapa de apagamento que alcança o bucket falha de forma fechada (fail closed) e o `erasure_recovery` tenta de novo. A varredura de originais registra a falha da execução e a sua passagem pelo bucket continua a partir do cursor na próxima execução.                                                                                                                                                                                                                                                        |
| Langfuse, ClickStack  | O tracing e a exportação são descartados; as requisições não são afetadas. Um backend de apagamento configurado que esteja fora do ar faz a etapa da saga falhar, e a recuperação tenta de novo.                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |

**Por que `memory.set` falha em vez de armazenar um stub:** um stub fica invisível para todas as vias de busca até ser embedado, então um chamador que recebesse a confirmação de "armazenado" não encontraria a própria escrita. Falhar permite que ele tente de novo com a sua chave de idempotência.

<a id="bluegreen-rollback"></a>

## Rollback blue/green

`./redeploy.sh` inicia a cor ociosa e só faz a troca depois que o `/v1/health` dela reporta
`status: ok`, ClickHouse acessível, `schema.pending` vazio, nenhum provider degradado e nenhuma incompatibilidade de largura de embedding, dentro de `LOOM_REDEPLOY_READY_TIMEOUT_S` (uma variável do script, 180 s).
A cor antiga é parada, não removida, e sua imagem recebe a tag `loom:prev`.
`./redeploy.sh --rollback` a inicia e volta a troca. Toda migração é aditiva
(`tests/unit/test_migrations_additive.py`), então o build anterior roda sobre o schema mais novo. Veja [atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md).

<a id="a-replica-that-dies-mid-migration-or-mid-reconfiguration"></a>

## Uma réplica que morre durante uma migração ou uma reconfiguração

Todo processo é um participante do provider: uma linha em `loom._provider_participants`, gravada
a cada 10 s por uma thread própria, e um User-Agent com seu id em cada consulta ao ClickHouse
que ele envia. Um participante conta como parado quando foi desativado (retired), ou quando seu último heartbeat
tem mais de 60 s (relógio do servidor) e `system.processes` não mostra nenhuma consulta dele
em nenhum nó. Nada além disso conta: um processo inacessível não é um processo parado.

- **Encerrado durante uma migração.** As migrações seguram apenas o lock de migração (`ttl_s`, renovado enquanto
  são aplicadas), nunca o lock do provider, que um boot segura somente para verificar que nenhuma reconstrução
  está inacabada. Um segundo contêiner assume o lock de migração depois que ele expira e aplica
  o que restar.
- **Encerrado segurando o lock do provider** (uma etapa de reconstrução, uma configuração salva). O TTL da chave
  é de 60 s, renovado a cada 20 s; um concorrente assume o lock depois que ele expira e o
  participante do detentor conta como parado. `loom provider-lock clear --holder <holder>`
  libera um lock cujo detentor não tem linha de participante, o que nunca conta como parado.
- **Encerrado durante uma reconfiguração.** A transição espera por um participante cujo heartbeat
  expirou, já que um processo pausado pode retomar com trabalho admitido, até que um processo no
  mesmo host comprove que ele sumiu (seu contêiner reiniciou ou seu pid terminou) e o desative.
  O líder dos workers
  exclui as linhas de participantes desativados há um dia e lista, uma vez por minuto, aqueles pelos quais uma
  transição esperaria como `provider_participants_stale` em seu `/v1/health`. Desative
  um manualmente com `loom participant retire --participant <id> --yes` depois de confirmar que seu processo
  sumiu; o comando recusa um participante cujo heartbeat é recente ou cujas
  consultas ainda estão em execução.
- **Um boot que falha após o registro** desativa sua linha; um contêiner em crash loop não deixa
  nenhuma para trás.

Um processo congelado além do seu heartbeat (um contêiner pausado, uma pausa de VM) mantém a
transição parada até que retome e conclua o que estava em andamento, ou até que um operador o desative.

<a id="data-loss-boundaries"></a>

## Limites de perda de dados

- **Escritas confirmadas.** Um `memory.set` bem-sucedido confirmou as duas linhas.
- **Linhas de telemetria e de auditoria.** Uma falha perde a fila de `agent_events` ainda não descarregada (descarregada a cada 100 ms) e a fila do fluxo de eventos (a cada `LOOM_EVENTS_STREAM_FLUSH_INTERVAL_S`, 1 s).
  O fluxo de eventos carrega apenas violações de orçamento. As linhas de auditoria, de exposição e de rastreamento de busca são inseridas diretamente; uma inserção de auditoria que falha é registrada em log e faz falhar apenas as duas operações que dependem do seu registro, um `memory.restore` que desfaz uma aposentadoria e `namespace.restore` ([limites da auditoria](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)).
  A recuperação não lê nenhuma delas.
- **Projeções pós-resposta** perdidas em uma falha são reconstruídas pelos workers de reparo.
- **ClickHouse OSS em nó único.** Sem replicação e sem recuperação para um ponto no tempo. O ClickHouse Cloud, ou um cluster OSS replicado, é o caminho para produção. Faça backup com `BACKUP` do ClickHouse ou com backups nativos; `dev/backup_loom.py` move um deployment, uma organização ou um namespace, e não é recuperação de desastres. Veja [backup e recuperação](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md).
- **Arquivos fora do ClickHouse.** Os originais dos documentos ficam no ClickHouse (`loom.document_blobs`) ou, para os gravados com `LOOM_DOCUMENT_OBJECT_STORE=s3`, em um bucket compatível com S3, cujo backup é feito com o versionamento ou a replicação do próprio bucket; uma regra de ciclo de vida limita por quanto tempo essas cópias mantêm originais apagados. Os espelhos de código ficam em um volume, e um host que não os tenha os clona novamente a partir do remote do repositório Git registrado no ClickHouse. A reconfiguração de provedor não mantém nada no host ([reconfiguração de provedor](https://github.com/ClickHouse/loom/blob/main/docs/architecture/provider-reconfiguration.md)).
- **Uma migração para outro host ou ClickHouse.** Leve o ClickHouse, a configuração de env, o `LOOM_JWT_SECRET` e, enquanto houver linhas que nomeiam objetos nele, o bucket de documentos; todo o resto é restaurado ([migração de um deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#moving-a-deployment)).

<a id="capacity"></a>

## Capacidade

Um processo do servidor não mantém estado durável, então a capacidade web escala executando mais processos.
O ClickHouse faz o trabalho de recuperação (varreduras de vetores, índices de texto, percursos em grafos), então a capacidade
além da camada Python é a capacidade do ClickHouse. Os workers não escalam por replicação: um
supervisor os executa. Os procedimentos estão em [implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) e
[ajuste de desempenho](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md).

| Camada                     | Escala por                                                 | Limitada por                                                              |
| -------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------- |
| Web (HTTP, MCP, painel)    | Mais contêineres atrás de um balanceador de carga; `LOOM_WEB_WORKERS` | CPU Python por processo; o pool por processo; limites de taxa por réplica |
| Workers em segundo plano   | Um supervisor por implantação; um contêiner de workers maior | Duração da passada em relação ao intervalo; cota do provedor para trabalho de LLM e de embedding |
| Recuperação e armazenamento | Computação, memória e armazenamento do ClickHouse         | Linhas varridas por leitura de tenant; pressão de merge durante a ingestão |
| Provedores                 | Cota do provedor e tamanhos de lote                        | Limites de taxa do provedor; `loom.embed.pending` mostra o backlog de embedding |

<a id="web-tier"></a>

### Camada web

| Ajuste                           | Efeito                                                                                                                                                                                                                                       |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Mais réplicas web                | N réplicas idênticas atrás de um balanceador de carga, todas com `LOOM_BACKGROUND_WORKERS=auto`.                                                                                                                                             |
| `LOOM_WEB_WORKERS`               | Processos uvicorn por contêiner; se não definido, a imagem inicia um por CPU que o contêiner pode usar, no máximo um por GiB de memória e no máximo 8. Cada processo executa seu Python em um núcleo; o embedder local dá a cada um uma parcela igual das CPUs. |
| `LOOM_REPLICA_COUNT`             | Piso da contagem de réplicas que divide cada bucket de limite de taxa (as linhas `loom.instances` em serviço ativas, abaixo).                                                                                                                |
| `LOOM_LATENCY_BUDGET_MULTIPLIER` | Multiplica o `budget_p99_ms` de cada op antes de uma violação de orçamento ser disparada (padrão 1.0).                                                                                                                                       |

Uma nova réplica passa a atender quando `/v1/health/ready` responde 200: o ClickHouse responde, nenhuma
migração está pendente, o snapshot de revogação e o keyring de assinatura estão carregados e atualizados,
o registro de trackers e as sobrescritas de runtime foram carregados uma vez (o boot espera até
`LOOM_BOOT_WARMUP_TIMEOUT_S` por ambos), a calibração de similaridade foi adotada, sua
configuração corresponde à da implantação, e o sandbox executa onde é exigido. Ao receber
SIGTERM, ela responde 503 ali e continua atendendo por `LOOM_SHUTDOWN_READY_DELAY_S` antes que
sua porta feche. A leitura das próprias escritas entre processos é tratada pelo ClickHouse (inserts
com espera, confirmados ou em lote, releituras sequenciais), não por afinidade de processo.

<a id="clickhouse-client"></a>

### Cliente ClickHouse

`server/core/ch.py` dá a cada processo um pool e um semáforo de admissão do mesmo tamanho.

| Configuração                                    | Padrão  | Efeito                                                                                                                 |
| ----------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CH_POOL_MAXSIZE`                          | 64      | Conexões e instruções concorrentes por processo; um insert em lote mantém uma durante sua espera e o flush.            |
| `LOOM_CH_POOL_WAIT_MS`                          | 500     | Espera por um slot; além disso, 503 `OVERLOADED` com `Retry-After: 1`. 0 desativa o gate.                              |
| `LOOM_CH_TIMEOUT_READ_S`                        | 30      | Prazo de leitura.                                                                                                      |
| `LOOM_CH_TIMEOUT_WRITE_S`                       | 60      | Prazo de `INSERT` / `UPDATE`.                                                                                          |
| `LOOM_CH_TIMEOUT_DDL_S`                         | 300     | Prazo de DDL, `OPTIMIZE`, `SYSTEM`.                                                                                    |
| `LOOM_CH_BATCHED_INSERTS`                       | true    | Agrupa em lote os inserts escritos a cada requisição, cada um mantendo uma conexão durante sua espera; false os confirma, um part cada. |
| `LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS`            | 10      | Menor espera de um insert assíncrono em lote ou com espera, o que um insert isolado espera; a conexão é mantida durante a espera. |
| `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS`            | 50      | Maior espera de um insert assíncrono em lote ou com espera que uma requisição aguarda; a conexão é mantida durante ela. |
| `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` | 200     | O mesmo para os ticks e o drain do flusher de eventos de uso e para o flush das marcas de escrita.                     |

**Por que uma espera limitada:** quando o ClickHouse fica lento, uma fila sem limite transforma cada requisição em
um timeout do cliente depois que o trabalho já foi enfileirado. Uma espera curta falha rápido com um 503 que permite nova tentativa. Os gauges `loom.ch.pool.in_use`, `waiters`, `overloaded_total` e `timeouts_total`
mostram qual lado está saturado.

<a id="traffic-classes"></a>

### Classes de tráfego

Toda chamada tem uma classe: `production`, `interactive` (padrão), `bench` ou `background`
(definida pelo supervisor, nunca por uma requisição). Ela vem da claim do token, ou então do
registro de namespaces, ou então da dica da requisição. Ela altera o agendamento, nunca os resultados:

- Configurações de leitura do ClickHouse por classe (`LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS`): `production`
  prioridade 1, `interactive` 2, `background` 5 com 3 threads, `bench` 10 com 4 threads.
- Buckets de limite de taxa (`LOOM_RATELIMIT_CLASS_FRACTIONS`; `bench` recebe metade).
- Política de LLM por classe (`LOOM_LLM_POLICY_BY_TRAFFIC_CLASS`).

Um namespace `bench-…` novo é classificado como `bench`, de modo que um teste de carga não prive a produção de recursos.

<a id="rate-limits"></a>

### Limites de taxa

`server/core/ratelimit.py` mantém token buckets por (namespace, classe de op) e por (org, classe
de op); a camada de org é três vezes a camada de namespace. Uma camada opcional por ator pega uma
fração do bucket do namespace (`LOOM_RATELIMIT_ACTOR_FRACTION`, 1.0 = desligada). Uma chamada deve
passar por todos os buckets, e uma recusa não decrementa nenhum.

| Classe de op       | Abrange                                                                                                       | Capacidade | Reposição / s |
| ------------------ | ------------------------------------------------------------------------------------------------------------- | ---------- | ------------- |
| `memory`           | `memory.*`, `conversation.*` (exceto as leituras por chave exata e `memory.import`)                           | 100        | 17            |
| `memory.read`      | Leituras por chave exata: `memory.get`, `bulk_get`, `history`, `working_memory.get`, …                        | 300        | 50            |
| `memory.import`    | `memory.import`                                                                                               | 10         | 2             |
| `knowledge.search` | `knowledge.search`                                                                                            | 20         | 3             |
| `knowledge.query`  | `knowledge.query`                                                                                             | 20         | 3             |
| `knowledge.read`   | As demais ops `knowledge.*` `read_only`, exceto `knowledge.inspect`: `knowledge.get`, `knowledge.list`, …     | 50         | 10            |
| `knowledge.add`    | Toda outra op `knowledge.*`, incluindo `knowledge.inspect`, `knowledge.synthesize` e `knowledge.dedup_list`   | 10         | 2             |
| `code`             | `code.*` (exceto `code.outcome`, que consome de `memory`)                                                     | 60         | 10            |
| `insights.query`   | `insights.query`                                                                                              | 20         | 3             |
| `assistant.ask`    | `assistant.ask` e a rota de chat do assistente                                                                | 10         | 1             |
| `default`          | Todo o resto                                                                                                  | 50         | 10            |

Estes são os padrões da camada de namespace (`DEFAULT_LIMITS`, `op_class_for`):

- **Por réplica.** Os buckets vivem no processo ou no host. Cada réplica mantém o limite
  dividido pelos hosts com uma linha `loom.instances` em serviço ativa, de modo que a soma das réplicas
  aproxima o limite. `LOOM_REPLICA_COUNT` é o piso dessa contagem, e a contagem
  enquanto as linhas não podem ser lidas.
- **Dentro de um host.** Com `LOOM_WEB_WORKERS > 1`, os processos compartilham uma tabela de buckets em um
  arquivo mapeado em memória (`LOOM_RATELIMIT_SHARED=auto`, `LOOM_RATELIMIT_SHARED_PATH`,
  `LOOM_RATELIMIT_SHARED_SLOTS`). `/v1/health` informa `ratelimit.backend`.
- **Fail open.** Um erro do limitador admite a chamada e é contabilizado.

**Por que buckets locais:** uma ida e volta ao ClickHouse por admissão colocaria o banco de dados no
caminho de toda requisição para proteger o banco de dados.

<a id="background-workers"></a>

### Workers em segundo plano

Um supervisor executa por implantação. No modo `auto`, todo processo da implantação
(`LOOM_DEPLOYMENT=1`) disputa um lease no ClickHouse: chave `workers_supervisor` na
tabela de locks do Keeper `loom._migration_lock` (KeeperMap estrito). Fora da implantação
(um servidor iso ou de branch no mesmo ClickHouse), `auto` não executa workers e o log de boot
informa isso; `on` os executa ali, mas apenas o detentor da implantação faz o trabalho do líder
(ALTERs de retenção, linhas de namespace do tracker, a configuração publicada, a varredura de
participantes do provedor).
A expiração é julgada pelo relógio do servidor ClickHouse. Com T = `LOOM_SUPERVISOR_LEASE_TTL_S`
(padrão 30 s, no mínimo 24):

| Etapa     | Quando                                                                       | Por quê                                                                                     |
| --------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Renovação | a cada T/6; uma renovação com falha é tentada de novo a cada T/12            | Várias falhas cabem antes de o detentor desistir                                            |
| Perda     | T/2 depois de a última renovação que chegou ter sido enviada                 | Medido a partir do envio, de modo que o carimbo do servidor nunca seja anterior             |
| Parada    | nesse prazo, mesmo enquanto uma chamada de renovação ainda está bloqueada    | Uma chamada travada não pode manter os workers em execução; cada chamada é enviada uma vez, timeout max(1, T/24) s |
| Drain     | até T/6, de modo que os workers estejam parados em 2T/3                      | Um processo filho de workers ainda em execução nesse momento é encerrado à força            |
| Expiração | T depois de o servidor carimbar a última renovação; então outro processo assume | T/3 de margem após o drain                                                               |

Um detentor que perdeu suas renovações não inicia nenhuma nova passada até renovar (um processo
pausado que retoma não executa uma ao lado do novo detentor), e aquele cuja chave do Keeper ainda
o nomeia retoma o lease imediatamente em vez de esperar o TTL expirar. Um detentor que encontra o lease
tomado para seus workers; quando o detentor morre, outro processo assume após T. Um
processo que se torna o detentor após o boot faz também o trabalho de boot do líder (linhas de
namespace do tracker, configurações publicadas, `LOOM_RETENTION_*_DAYS`).

Parar os workers cancela cada passada no seu próximo `await`. O trabalho que o cancelamento não alcança
(uma thread como o cálculo de PageRank ou de comunidades, uma passada que retoma após uma
pausa antes de seu loop de lease executar) para no próximo `pass_context.check_stop()` da passada,
que os loops longos chamam entre lotes: as etapas de ingestão, as rodadas de delta do SCIP,
`code_pr_history`, `code_graph_stats` e `entity_communities`. Ele dispara uma exceção assim que a passada
foi parada ou o lease não está mais atualizado, de modo que a passada não envia nenhuma escrita adicional. Não
há fencing tokens: uma instrução já enviada ainda é aplicada.

Um processo se afasta (o detentor para seus workers e abre mão da chave; um concorrente
não a disputa) enquanto:

- ele faz o drain após o SIGTERM, de modo que outro processo assume durante seu ready delay;
- sua configuração difere da publicada pela implantação (ele não está pronto, e
  poderia escrever vetores no espaço errado ou assinar com a raiz errada);
- uma linha `loom.instances` ativa de um build mais novo disputa o mesmo lease.

O último caso é a passagem de bastão em uma atualização progressiva. Cada linha carrega a versão de schema
do seu build (o número da última migração) e o lease que ela disputa agora: nenhum enquanto aquele
processo faz drain, divergiu ou abriu mão do lease, de modo que tal linha nunca faz um build mais antigo
ceder. A passagem só ocorre quando as versões de schema diferem, então uma release apenas de código
nunca passa o bastão: o antigo detentor mantém os workers até parar. Quando nenhum
build mais novo está ativo (um rollback), o mais antigo volta a disputar. Um processo cujo processo filho
de workers continua falhando ao iniciar abre mão do lease por cinco minutos (abaixo).

Portanto:

- Vários hosts podem executar em `auto`; um deles executa os workers. `on` pula a eleição.
- Uma segunda cópia dos workers de manutenção desperdiça trabalho do ClickHouse; uma segunda cópia dos workers
  de LLM dobra suas varreduras e seus gastos.
- O detentor executa os workers em um processo filho (`LOOM_WORKERS_PROCESS=child`), de modo que uma
  passada longa não compartilhe o event loop que atende suas requisições (passadas no
  loop de atendimento praticamente dobraram o p90 das requisições: `memory.get` 148 ms contra 74–79 ms durante
  uma passada de `code_ingest`). O filho executa o
  supervisor e o que seus workers usam (o cliente ClickHouse, a configuração de provedores,
  o emissor de eventos, os registros de tracker e de repositório), não o lifespan de atendimento.
  Ele lê o prazo do lease de seu pai a cada segundo; o prazo deixa de avançar
  quando o lease não está atualizado. O filho desaparece até esse prazo mais T/6: ele sai
  por conta própria então, seu pai o encerra então, e o kernel o encerra quando o pai morre
  (`PR_SET_PDEATHSIG`). O event loop do filho envia um heartbeat ao pai a cada segundo, e
  `/v1/health` mostra um filho sem heartbeat por 15 s como não vivo. Um processo em drain
  (SIGTERM) para seus workers, em linha ou em um filho, e abre mão do lease enquanto
  ainda atende. Um filho que sai em até 60 s após iniciar cinco vezes seguidas faz seu
  processo abrir mão do lease e informar `workers_lease_given_up`; outro processo assume
  os workers, e este pede de novo cinco minutos depois.

O trabalho dos workers não é particionado entre processos. Um worker acompanha o ritmo por meio de passadas limitadas:
um orçamento de lote ou de tempo por passada (por exemplo, `embedding_worker` gera embeddings de até 100 stubs;
`usage_fold` consolida até 1000 memórias em 120 s e recua com 20 mutações pendentes)
e o prazo acima. Uma passada que atinge rotineiramente seu prazo significa que o contêiner de workers
precisa de mais CPU ou o provedor de mais cota.

<a id="batch-sizes-and-queues"></a>

### Tamanhos de lote e filas

| Caminho                           | Limite                                                                                                     |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Corpo da requisição               | `LOOM_MAX_REQUEST_BYTES` (32 MiB); 413 acima disso                                                         |
| `memory.bulk_set`                 | Até 1000 entradas em um `embed_many`; verificações de conflito até `LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES` (200) |
| `memory.bulk_get`                 | Até 1000 chaves                                                                                            |
| `memory.set_from_messages`        | Até 400 mensagens                                                                                          |
| Requisições de embedding          | Até 100 entradas por requisição ao provedor (OpenAI, Gemini)                                               |
| Emissor de `agent_events`         | Fila de 10,000 linhas, com flush a cada 0.1 s ou 500 linhas                                                |
| Stream de eventos (violações de orçamento) | Fila de 20,000, com flush a cada `LOOM_EVENTS_STREAM_FLUSH_INTERVAL_S` ou `_FLUSH_BATCH` linhas   |
| Linhas de status dos workers      | Flush a cada 15 s ou 200 linhas                                                                            |

<a id="vector-cost"></a>

### Custo dos vetores

Uma memória armazena até dois vetores (valor e chave) de `Array(BFloat16)`: 6 KiB com 1536
dimensões, 12 KiB com 3072. Os bytes de armazenamento e de varredura crescem linearmente com `LOOM_EMBEDDING_DIM`.
Não há índice HNSW, então uma leitura varre todos os vetores do namespace do tenant e a latência
cresce com o tamanho do namespace. Códigos quantizados reduzem a varredura onde o codec pode ser ativado
([configurações protegidas](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)).
Os detalhes estão em [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#vector-storage).

<a id="measuring"></a>

### Medição

Esta página não informa números de latência ou de vazão; eles dependem da implantação do ClickHouse,
da distância de rede e do tamanho do namespace. Meça os seus, contra um servidor
isolado:

| Comando                                                     | Mede                                                                                                   | Saída               |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------- |
| `make bench OP=memory.set C=10 D=30`                        | p50 / p95 / p99 e RPS de uma op com concorrência C por D segundos (`scripts/ops/benchmark.py`)         | stdout, ou `--json` |
| `python bench/search_latency.py --namespaces … --queries …` | p50 / p95 de `memory.search` por faixa de tamanho de namespace, com tempos por etapa de `retrieval_work.stage_ms` | CSV de `--out` |
| `make bench-quick`                                          | Qualidade de recuperação em uma amostra de 30 perguntas (requer provedores reais)                      | CSV em `/tmp`       |

`scripts/ops/benchmark.py` escreve em um namespace `bench-…` novo, então executa na
classe `bench`; uma execução de `memory.search` precisa de um namespace preenchido por uma execução de `memory.set`. Veja
[executando benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md).

<a id="what-to-scale"></a>

### O que escalar

| Sinal                                                           | Escalar                                                   |
| --------------------------------------------------------------- | --------------------------------------------------------- |
| CPU web alta, p99 das requisições subindo, ClickHouse respondendo rápido | Réplicas web ou `LOOM_WEB_WORKERS`               |
| `loom.ch.pool.waiters` e `OVERLOADED` subindo, ClickHouse rápido | `LOOM_CH_POOL_MAXSIZE`, ou mais réplicas                 |
| `BACKEND_TIMEOUT` e entradas lentas em `system.query_log`       | Computação do ClickHouse, ou namespaces menores por leitura |
| `loom.embed.pending` subindo                                    | Vazão do embedder (cota do provedor)                      |
| `loom.worker.consecutive_failures` ou durações de passada crescendo | CPU e memória do contêiner de workers                 |
| Pressão de merge, erros de memória durante a ingestão           | Computação e memória do ClickHouse, ou menor concorrência de escrita |

As métricas são descritas em [monitoramento](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md).

<a id="known-limits"></a>

### Limites conhecidos

- **Um líder de workers por implantação.** As passadas dos workers não são distribuídas entre hosts.
- **Limites de taxa por réplica** são aproximados: o preenchimento do bucket é por host. Os limites de tentativas
  de login e de cadastro são contados entre todos os processos em `loom.auth_attempts`.
- **Verificar e depois inserir entre réplicas** (nome e e-mail de um cadastro, um namespace
  pessoal, um código OAuth ou refresh token, uma chave de idempotência em andamento) executa sob uma
  claim de toda a implantação, uma chave KeeperMap estrita em `loom._migration_lock`, com a
  verificação lida sequencialmente. Um segundo
  concorrente recebe um 409; um armazenamento de claims que não consegue responder resulta em 503 (uma chave de idempotência
  então executa como um miss simples).
- **A telemetria do stream de eventos** (violações de orçamento) é drenada no `SIGTERM`; um `SIGKILL` ou
  OOM perde no máximo um intervalo de flush dela, nunca linhas de auditoria ou de memória.
- **O tamanho do namespace determina a latência de leitura**, porque as leituras de vetores varrem as linhas do namespace
  sem índice vetorial.
- **ClickHouse OSS de nó único** não tem replicação nem failover.

<a id="knowledge-restore"></a>

## Restauração de conhecimento

`knowledge.restore` ([restaurar um documento excluído](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#restoring-a-deleted-document))
nunca deve trazer de volta o que uma exclusão definitiva alcançou. Uma exclusão definitiva deixa as próprias marcas: ela carimba
cada chunk do seu escopo, inclusive os de um documento excluído, e substitui uma linha ativa no próprio lugar.
A restauração também lê o registro de exclusões em busca de uma exclusão que possa alcançar o documento e que
não havia terminado quando o documento foi escrito: do namespace dele, de uma lista de namespaces que o inclua,
ou do usuário que escreveu a revisão restaurada (de qualquer usuário, no caso dos resíduos ativos de uma
restauração interrompida, que nenhuma assinatura liga a um autor).

Ela lê o registro antes de gravar a linha e de novo depois: quando a segunda leitura encontra uma
exclusão, ela desfaz o que gravou, e faz o mesmo quando qualquer coisa falha depois que começou a gravar.
Sob uma purga, ela exclui a própria linha, de modo que nenhuma cópia do conteúdo permaneça.
Quando uma mesclagem incorporou a linha de exclusão a ela, isso deixa a origem sem nenhuma linha: uma
purga que cobre o documento também não deixaria nenhuma, e uma que apenas pode cobri-lo
(uma lista ilegível, ou a purga de qualquer usuário no caso dos resíduos de uma restauração interrompida) custa
ao documento a sua linha desativada, que é a escolha de falha segura.
Caso contrário, e quando o registro não pode ser lido, ela grava a linha de volta como desativada, mantendo qualquer
marca que uma exclusão tenha colocado nela nesse meio tempo: excluí-la poderia deixar a origem sem nenhuma linha, já que
uma mesclagem pode incorporar a linha de exclusão a ela a qualquer momento. Quando não resta nenhuma linha da origem
(uma purga levou todas), ela não grava nada de volta.

Sob a autoridade Postgres, a restauração grava pelos cabeçalhos de documento ([autoridade de
armazenamento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md#knowledge-documents)). Ela recusa, com possibilidade de nova tentativa, enquanto uma
publicação da origem ainda puder chegar ao ClickHouse (uma pendente, ou uma que o worker de entrega assumiu e liquidou dentro do limite de inserção do seu lease), já que a
desativação dos chunks por uma exclusão poderia, de outro modo, chegar depois de a restauração
tê-los trazido de volta. A gravação de retorno é confirmada sobre a versão que ela leu, de modo que uma escrita de texto que tenha assumido a origem nesse meio tempo
a recusa (409). Uma que é confirmada e não consegue publicar é desfeita e responde 503. O desfazer de uma purga
também retira o cabeçalho restaurado e a sua entrega, e espera o lease dessa entrega expirar
antes de excluir a linha do ClickHouse.

Uma restauração cujo chamador desaparece (o cliente se desconecta, a requisição é cancelada)
antes de responder ainda é executada até o fim sob o claim e se desfaz, já que nenhuma
linha de auditoria a registraria; o desfazer termina antes de o claim ser liberado. Um chamador
que desaparece depois de a restauração responder, enquanto o transporte grava a sua linha de auditoria, pode deixar
a restauração em vigor sem linha de auditoria, como qualquer escrita auditada pode.

O que ela não consegue desfazer:

- Uma restauração interrompida antes do seu desfazer deixa os chunks pesquisáveis sob uma linha desativada;
  chamá-la de novo a conclui. Uma exclusão que rode antes desativa esses chunks carimbados na
  exclusão do próprio documento, de modo que uma restauração ainda os encontra.
- Uma exclusão definitiva que se registra e termina entre as suas duas leituras do registro, seguida de uma falha do desfazer
  (ou da segunda leitura e do desfazer, ou do término do processo entre eles): a linha continua ativa, e uma chamada posterior responde `live`.
- Uma purga para a qual o desfazer não consegue ler o registro deixa a linha desativada: o censo de resíduos da purga,
  que conta todas as linhas, só a informa se rodar depois do desfazer.
- A varredura de originais exclui um original que nenhuma linha ativa nomeia quando ele é antigo o bastante, e
  verifica apenas a idade no momento da exclusão. Uma passada da varredura que leu o namespace enquanto o
  documento estava excluído pode excluir o original depois que a restauração o encontrou, de modo que a linha restaurada
  nomeia um original que não existe mais (e responde `kept`); então o download falha. Qualquer
  escrita que nomeie de novo um original existente compartilha essa janela.

Escritas concorrentes: uma escrita de texto (`knowledge.add`, `knowledge.upsert`) e a exclusão de um
documento ativo não fazem claim. Sob a autoridade ClickHouse, uma escrita de texto que chegue à
mesma origem durante uma restauração pode deixar os chunks das duas versões pesquisáveis, como pode
durante uma exclusão, até a próxima escrita dessa origem; uma que começou antes da
restauração grava a sua linha com uma versão menor, de modo que a linha restaurada a oculta, assim como a linha de uma
exclusão oculta uma que começou antes da exclusão: escreva-a de novo. Sob a autoridade
Postgres, a que for confirmada em segundo lugar responde 409 e desativa os próprios chunks.
Quando uma restauração se desfaz depois de a linha de uma escrita posterior ter prevalecido, ela deixa essa
linha como está e desativa apenas os próprios chunks. Uma exclusão de uma origem já excluída
desativa os chunks que sobraram sob o claim da origem, lendo o documento de novo sob
ele com consistência sequencial: um que uma restauração trouxe de volta nesse meio tempo está ativo então
e mantém os seus chunks. Ela desativa e conta apenas os chunks até a marca de exclusão (tombstone); os chunks de uma
escrita mais nova, ainda por confirmar, permanecem, e uma exclusão que encontra só esses responde que
nada foi excluído.

<a id="knowledge-move"></a>

## Movimentação de conhecimento

`knowledge.move` ([movimentação de documentos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#moving-documents)) grava cada documento no namespace de destino e só depois o apaga de onde estava, de modo que um move interrompido deixa o documento nos dois lugares e nunca em nenhum. Por documento, ele lê o lugar de destino e responde `exists` se houver outro documento ativo ali, antes de gravar qualquer coisa; sob os claims, lê os dois de novo. Em seguida armazena o original e o artefato de extração no novo namespace, fora de qualquer claim (um original que nenhuma linha nomeia ainda pertence à varredura, como qualquer upload cuja linha não chegou a ser gravada). Um original mantido como linha do ClickHouse é copiado dentro do ClickHouse; um que está no bucket, ou que foi movido para lá nesse meio tempo, é lido e gravado de novo sob o lugar do novo namespace ali. Um original que uma exclusão (erasure) removeu (`blob_erased_at` no documento) não é copiado, mesmo quando os bytes do seu sha ainda são mantidos para outro upload.

Depois, sob o claim da origem nos dois namespaces, obtido em ordem de classificação e sem espera (uma origem já retida responde `busy` imediatamente), o move recusa (`busy`) quando qualquer um dos lugares mudou desde que foram lidos, quando uma publicação de qualquer um dos lugares ainda pode ser concluída, ou enquanto houver um job de ingestão aberto em qualquer um deles (sua publicação gravaria o documento de volta onde ele estava, ou por cima da cópia). Ele copia os chunks da revisão do documento, com seus vetores, dentro do ClickHouse (`INSERT … SELECT`, todas as versões que a linha serve: uma edição de metadados deixa os chunks na própria versão), depois as tabelas, depois a linha, e apaga a origem sobre a versão que leu. Uma cópia deixada por um move anterior que parou antes do seu delete (o mesmo texto, título, descrição, tags, metadados, autor e hora de ingestão no destino) não é gravada de novo quando serve tantos chunks ativos quanto a origem e as mesmas tabelas: apenas a origem é apagada. Uma cópia com menos chunks, ou sem tabelas onde a origem tem algumas (um insert de linha cancelado pode chegar depois do seu take-back, o que deixa a linha intacta), é sobrescrita, como um documento apagado, antes de a origem ser apagada. Uma com mais chunks está completa: um delete interrompido entre a retirada dos chunks da origem e a gravação do seu tombstone (ele os retira primeiro) deixa a linha da origem ativa e sem chunks, e resta apenas o delete; uma cópia nunca é sobrescrita quando serve mais chunks que a origem: se ela também não tiver suas tabelas, responde `exists`. Tabelas no destino que diferem de outra forma foram alteradas em um dos lados desde então (uma aprovação, um esclarecimento), o que não se distingue de uma alteração no outro lado: essa cópia, assim como uma cujo texto ou metadados foram editados, é outro documento e responde `exists`.

Qualquer coisa que não seja gravada por inteiro, ou uma escrita que tomou qualquer um dos lugares antes do delete, desfaz o que o move gravou no destino, e essa origem responde `busy`. O take-back vai até o fim antes de os claims serem liberados, por mais vezes que a requisição seja cancelada nesse meio tempo (o cancelamento é levantado depois que ele termina). Seu primeiro statement retira os chunks da cópia sob todos os modelos de embedding de uma só vez; um take-back começa pelo corte dos seus claims, ou logo após um delete iniciado antes dele que a autoridade Postgres recusa, cujo commit responde dentro de `pg_commit_lock_timeout_ms` e `pg_statement_timeout_ms` (2 s e 5 s por padrão). Com o limite próprio do statement (15% do TTL, 9 s), essa retirada termina em cerca de 40 s, dentro do TTL de 60 s do claim. O restante pode ultrapassá-lo, mas um move posterior que então obtenha os claims lê a cópia incompleta: sob os claims ele verifica de novo se a cópia de um move anterior está inteira, responde `busy` quando não está e, em uma chamada posterior, grava a cópia de novo; as outras origens da chamada ainda são movidas. Após a retirada, o take-back descarta seu commit (sob a autoridade Postgres), apaga sua linha, depois seus chunks e suas tabelas: um take-back que para no meio não deixa nenhum chunk servindo sem uma linha, e um `INSERT … SELECT` cancelado que chega depois do delete dos chunks também é retirado. O trabalho sob os claims é cortado em 40% do TTL de 60 s do claim (24 s); um documento cuja cópia ultrapassa esse limite é desfeito e responde `busy`. O delete vai até o fim independentemente de como a requisição ou os claims terminem nesse meio tempo, e o documento é contado como movido no instante em que seu tombstone é gravado, de modo que um corte ou um cancelamento enquanto o tombstone está em andamento ainda responde como movido e o registra na auditoria, e um tombstone que a autoridade Postgres recusa então ainda desfaz a cópia. Uma falha do próprio delete (uma resposta perdida, que faz a chamada falhar) pode suceder o tombstone, de modo que a cópia permanece: o documento fica nos dois lugares, ou apenas no destino, e uma nova tentativa encontra a cópia e apenas apaga a origem, ou responde `not_found`. Uma vez gravado o tombstone, o move está feito: as tabelas da origem são apagadas depois que os claims são liberados, pois o delete de um documento ativo não toma nenhum, então nenhum corte as alcança; uma falha ali é registrada em log, e um delete posterior da origem as resolve. Um documento conta como movido no instante em que seu tombstone é gravado, sob os claims: sua linha de auditoria e a limpeza das tabelas seguem, seja o que for que encerre a chamada depois disso (um cancelamento ao liberar os claims, ou durante a limpeza). Uma falha que não é de uma origem específica (um store que não responde) é levantada depois que todas as origens da chamada responderam, após a linha de auditoria das que foram movidas.

O ledger de exclusões é lido duas vezes para cada documento, como `knowledge.restore` o lê (`unfinished_reaching` para o namespace e o writer do documento, filtrado com `may_cover`): antes de o original ser armazenado, para uma exclusão que possa alcançá-lo onde está (não concluída quando seu texto foi gravado ali: o `created_at` do seu primeiro chunk, que um move marca com o momento em que copiou os chunks, enquanto a linha mantém sua hora de ingestão) ou o lugar de destino (não concluída quando o move começou), e de novo sob os claims, logo antes do seu delete, para uma registrada desde então: uma pode ter passado pela cópia, a deste move ou a de um move anterior. Essa desfaz uma cópia que este move gravou, deixa a cópia de um move anterior para a exclusão e responde `erased` sem apagar a origem, de modo que um documento `erased` sempre permanece onde estava. Sob a autoridade Postgres, um delete sobre um documento que uma exclusão removeu nesse meio tempo também responde `erased`. A primeira leitura vem logo antes de o original ser armazenado no novo lugar, de modo que uma exclusão cuja etapa de originais possa perdê-lo deve se registrar no intervalo entre essa leitura e o armazenamento; a nova leitura antes do delete então responde `erased`, e esse original, nomeado por nenhuma linha, pertence à varredura após `document_blob_min_age_seconds`, como qualquer original cuja linha não chegou a ser gravada. O move não apaga nenhum original por conta própria: um original é identificado por seus bytes, e uma escrita concorrente dos mesmos bytes cuja linha ainda não foi gravada pode precisar dele.

Escritas concorrentes: uma escrita de texto não toma nenhum claim. Sob a autoridade Postgres, a linha da cópia e o delete são confirmados cada um sobre a versão lida, de modo que uma escrita que tomou a origem ou o lugar primeiro os recusa (409), e a cópia é desfeita. Sob a autoridade ClickHouse, o move lê o head do destino sob os claims antes de copiar e de novo após sua linha: uma escrita com versão posterior vence e a cópia é desfeita, enquanto uma que foi gravada entre essa primeira leitura e a linha (toda a cópia de chunks e tabelas), com versão anterior, é substituída, como acontece com duas escritas de texto na mesma origem.

<a id="invariants"></a>

## Invariantes

- Uma gravação bem-sucedida teve suas linhas primárias confirmadas (commit); a falha de qualquer uma das duas faz a requisição falhar.
- Um INSERT repetido carrega seu token de deduplicação, ou não é repetido.
- `OVERLOADED` e `AUTH_UNAVAILABLE` carregam uma dica de nova tentativa; `SCHEMA_PENDING` aguarda um operador.
- Uma passada de worker é idempotente: executá-la duas vezes converge.
- As verificações de acesso e a exclusão de dados falham de forma fechada; os controles de disponibilidade e a telemetria falham de forma aberta, e isso é contabilizado.
- Nenhuma requisição espera indefinidamente pelo pool, por um provedor ou por uma fila. Cada instrução espera no máximo `LOOM_CH_POOL_WAIT_MS` por um slot e é executada por no máximo o prazo da sua classe.
- Uma classe de tráfego altera o agendamento e os buckets, nunca as linhas retornadas.
- Toda migração é aditiva, de modo que a build anterior funciona com o schema atual.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

| O quê                                | Onde                                                                                                                                                                                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Retentativas e controles do cliente  | `server/core/ch.py` (`_with_retry`, `_acquire_slot`, `_bounded`, `statement_timeout`), `server/core/ch_schema_guard.py`                                                                                                                     |
| Códigos de erro                      | `server/core/errors.py`                                                                                                                                                                                                                     |
| Idempotência                         | `server/core/idempotency.py`                                                                                                                                                                                                                |
| Supervisor e lease                   | `server/workers/supervisor.py`, `server/workers/leader.py`, `server/workers/registry.py` (`default_max_pass_seconds`)                                                                                                                       |
| Workers de reparo                    | `server/memory/workers/projection_repair.py`, `orphan_vector_reconcile.py`, `embedding.py`, `entity_extractor.py`; `server/erasure/workers/recovery.py`; `server/tasks/workers/lease_sweep.py`; `server/insights/workers/audit_verifier.py` |
| Snapshot de revogação                | `server/auth/revocation.py` (`load_at_boot`)                                                                                                                                                                                                |
| Limite de embedding de consulta      | `server/memory/app/search/embed.py`                                                                                                                                                                                                         |
| Projeções após a escrita             | `server/memory/app/after_write.py`, `server/memory/app/projection_failures.py`                                                                                                                                                              |
| Filas de telemetria                  | `server/observability/events.py`, `server/observability/events_stream.py`, `server/workers/status_store.py`                                                                                                                                 |
| Classes de tráfego                   | `server/observability/traffic_class.py`, `server/identity/app/traffic_class_registry.py`                                                                                                                                                    |
| Limitador de taxa                    | `server/core/ratelimit.py` (`DEFAULT_LIMITS`, `op_class_for`), `server/core/ratelimit_shared.py`, `server/auth/attempts.py`                                                                                                                 |
| Status do schema                     | `server/core/schema/status.py`                                                                                                                                                                                                              |
| Implantação azul/verde               | `redeploy.sh`, `dev/docker-compose.colour.yml`                                                                                                                                                                                              |
| Teste de carga                       | `scripts/ops/benchmark.py`, `bench/search_latency.py`                                                                                                                                                                                       |

<a id="related"></a>

## Relacionados

- [Solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md): runbooks organizados por sintoma.
- [Monitoramento](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md): as métricas citadas aqui.
- [Segurança](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md): as verificações de acesso com falha fechada (fail-closed).
- [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md): tabelas, colunas vetoriais, atribuição de consultas.
