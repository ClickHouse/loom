<!-- i18n: source=docs/operations/replicas.md blob=f4c8729ef4e795f5b6ab89dcd363a0bf9c30aad0 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md), which is authoritative.

<a id="running-n-replicas"></a>

# Executando N réplicas

Todo processo do Loom é uma réplica completa. Execute quantas precisar em um único
serviço ClickHouse, atrás de uma única URL, sem sessões fixas: cada uma atende a
todas as ops, verifica todos os tokens e grava no serviço compartilhado. O trabalho
que deve acontecer uma única vez roda no processo que detém o lease do ClickHouse e
muda de processo quando aquele para:

| Uma vez por deployment                    | Onde roda                                                                                                  |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Migrações de schema                       | o primeiro processo a iniciar um build com migrações pendentes, sob o lease de migração; os demais esperam por ele |
| Workers em segundo plano                  | o detentor do lease `workers_supervisor` ([implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md))                                 |
| Publicação da configuração do deployment  | o mesmo detentor do lease (abaixo)                                                                         |

Cada processo mantém seus próprios espelhos git (uma leitura de histórico clona um
espelho ausente; `LOOM_CODE_HISTORY_CLONE_ON_READ`), seus próprios caches e seus
próprios buckets de limite de taxa.

<a id="one-env-for-all-of-them"></a>

## Um mesmo env para todas

Inicie todas as réplicas a partir do mesmo env, com `LOOM_DEPLOYMENT=1`:

```bash
LOOM_DEPLOYMENT=1
LOOM_CH_HOST=...                 # the one ClickHouse service
LOOM_CH_PORT=8443
LOOM_CH_USER=...
LOOM_CH_PASSWORD=...
LOOM_JWT_SECRET=...              # the same root secret on every replica
LOOM_EMBEDDING_MODEL=...         # the same model and width on every replica
LOOM_EMBEDDING_DIM=...
OPENAI_API_KEY=...               # each replica makes its own provider calls
LOOM_TRUSTED_PROXIES=...         # the load balancer, so per-client limits see the client
```

`LOOM_JWT_SECRET` assina com o kid `default` e sela todas as outras chaves de
assinatura e todas as credenciais armazenadas; com um único valor, todo token
funciona em todas as réplicas. Chaves de assinatura, revogações, grants e contas de
serviço são linhas no serviço compartilhado; uma rotação ou revogação chega a todas
as réplicas dentro de `LOOM_JWT_KEYRING_REFRESH_SECONDS` e
`LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS`.

<a id="configuration-drift"></a>

## Divergência de configuração

Cada processo grava uma linha de heartbeat em `loom.instances` a cada
`LOOM_INSTANCE_HEARTBEAT_SECONDS` (10 s por padrão): seu build, uma impressão digital
de suas configurações MUST_MATCH (o espaço de embedding: `LOOM_EMBEDDING_PROVIDER`,
`_MODEL`, `_DIM`, `_BASE_URL` e `_SEND_DIMENSIONS`), uma impressão digital de seu
segredo raiz e seu horário de início. Uma linha não renovada por três intervalos, ou
marcada como parada pelo próprio processo no desligamento, não está ativa.

O detentor do lease publica sua configuração em `loom.deployment_config`: as
configurações MUST_MATCH e uma impressão digital HMAC de seu segredo raiz. Todo
processo se compara com esse snapshot na inicialização e a cada heartbeat. Um
segredo raiz diferente (seus tokens e credenciais armazenadas não abririam em outro
lugar) ou um valor MUST_MATCH diferente (seus vetores ficariam em outro espaço) é
divergência: o processo não fica pronto, de modo que o balanceador de carga não envia
nada a ele, e `/v1/health` reporta o problema `config_drift` com o que difere. Como
não estar pronto só impede novas conexões, o processo também responde a toda chamada
de API, MCP e login com `503 CONFIG_DRIFT` e fecha a conexão: a nova tentativa do
cliente cai em uma réplica que concorda. Os valores comparados são os do env: uma
troca de modelo feita pelo painel é do deployment (toda réplica a lê do ClickHouse),
portanto não é divergência, e uma réplica iniciada com o mesmo env depois dela fica
pronta.
`deployment_config` em `/v1/health` mostra a última verificação e os hosts ativos.

O detentor do lease nunca publica por cima de um snapshot do qual diverge: uma
réplica com o segredo raiz ou o modelo de embedding errado que ganhe o lease continua
não pronta, e as réplicas com o env publicado continuam prontas. Um snapshot com o
qual ele concorda só é reescrito quando nenhuma outra réplica ativa executa o
armazenado, de modo que os snapshots de formatos diferentes de N-1 e N durante um
rollout nunca alternem de um para o outro.

**Alterar uma configuração MUST_MATCH ou `LOOM_JWT_SECRET`** não pode ser feito de
forma gradual: as réplicas com o novo valor não ficam prontas enquanto as réplicas
com o antigo atendem. Pare todas as réplicas, altere o env, execute
`loom deployment publish --yes` com o novo env (ele lista o que muda) e inicie-as
novamente (uma alteração de embedding é seguida de um re-embed completo,
`loom reembed`). Após uma troca de segredo, emita os tokens novamente e armazene as
credenciais de novo.

Processos sem `LOOM_DEPLOYMENT` (um servidor de branch ou de teste A/B no mesmo
serviço) também enviam heartbeat, mas nunca publicam, nunca são verificados e não
contam como réplicas.

<a id="rate-limits"></a>

## Limites de taxa

Os limites configurados valem para o deployment inteiro. A parcela de cada réplica é
o limite dividido pelos hosts com uma linha ativa em `loom.instances`, portanto
adicionar ou remover uma réplica redimensiona todos os buckets em até um heartbeat.
`LOOM_REPLICA_COUNT` é o piso dessa contagem e a contagem usada enquanto as linhas
não podem ser lidas. Os web workers de um mesmo host compartilham uma tabela de
buckets ([implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)).

<a id="code-repositories"></a>

## Repositórios de código

Todo processo relê `loom.code_repos` a cada `LOOM_CODE_REPOS_REFRESH_SECONDS`
(30 s por padrão), portanto um repositório adicionado ou restringido em uma réplica
vale em todas as réplicas dentro desse tempo.

Um host que não deve servir histórico git (uma borda sem disco para espelhos) define
`LOOM_GIT_HISTORY=none`: blame, verificações de commit e histórico de PR passam a
responder sem ele.

<a id="stopping-a-replica"></a>

## Parando uma réplica

Pare-a de forma graciosa (`docker stop`, SIGTERM): ela marca sua linha de heartbeat
como parada e retira sua linha de participante do provider. Uma interrupção forçada
deixa ambas; a linha de heartbeat deixa de contar após três intervalos. Um processo
que reinicia no mesmo container retira por conta própria a linha de participante do
processo morto. Se uma reconfiguração do provider estiver esperando pela linha de um
processo morto vinda de outro lugar, retire-a com o `participant_id` que o
`/v1/health` dele reportou, depois de confirmar que ele não existe mais (o comando
recusa uma linha cujo heartbeat está recente ou cujas consultas ainda estão em
execução):

```bash
loom participant retire --participant <participant_id> --yes
```
