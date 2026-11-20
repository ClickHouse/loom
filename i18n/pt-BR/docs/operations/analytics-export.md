<!-- i18n: source=docs/operations/analytics-export.md blob=241a7492fe019249ba054a278a0059bcb928b7e0 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md), which is authoritative.

<a id="exporting-usage-to-a-data-warehouse"></a>

# Exportando o uso para um data warehouse

`loom export-analytics` grava um dia UTC de uso e um snapshot dos namespaces em um bucket S3 como arquivos Parquet, um arquivo por entidade por dia, para que um data warehouse os carregue. O chart do Helm o executa uma vez por dia como um CronJob (`analyticsExport` em `values.yaml`). Para consultar as memórias de um tenant, com o conteúdo delas, e o seu ledger de requisições no próprio local, use a [superfície SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md).

```bash
loom export-analytics --bucket analytics-landing --prefix loom \
  --role-arn arn:aws:iam::123456789012:role/loom-analytics-writer \
  --expected-bucket-owner 123456789012
```

Ele precisa de `LOOM_ANALYTICS_EXPORT_NAME_KEY`, o segredo sob o qual os nomes são gerados como hash (veja [nomes com hash](#hashed-names)).

`--day YYYY-MM-DD` exporta outro dia; o padrão é ontem (UTC). Um dia que ainda não terminou é recusado.

<a id="files"></a>

## Arquivos

```text
<prefix>/<entity>/<yyyy>/<mm>/<dd>/<entity>-<yyyy>-<mm>-<dd>.parquet    entity: usage, namespaces
```

Cada upload leva `If-None-Match: *`, então um objeto nunca é substituído. Uma segunda execução de um
dia mantém os arquivos da primeira execução e imprime `already there, kept` para cada um; uma execução que falhou ou
foi repetida pode ser tentada de novo como está. Para exportar um dia novamente, apague antes os objetos dele.

| Entidade     | Uma linha por                                                                                                       | Colunas                                                                                                                                                                                                                                  |
| ------------ | ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `usage`      | `org`, `namespace`, `agent`, `operation`, `kind`, `transport`, `traffic_class` do dia, a partir de `loom.agent_events` | `day`, `ops`, `errors`, `server_errors`, `slow`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`, `cost_usd`, `bytes_written`, `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`, `users`, `sessions`, `snapshot_at` |
| `namespaces` | `org`, `namespace` registrado em `loom.namespaces` ou que contém uma linha de memória ativa                         | `registered`, `kind`, `visibility`, `active`, `created_on`, `live_memories`, `last_memory_on`, `day`, `snapshot_at`                                                                                                                      |

`kind` em `usage` é `request` para uma requisição, `worker` para uma passagem do worker, `verdict` para uma
verificação de consistência adiada de `memory.ask` e `settle` para uma resolução de conflito adiada. Toda
rota `/v1/me/*` é uma única `operation`, `me`: o ledger nomeia essas linhas conforme o caminho da requisição,
que pode conter qualquer string enviada por um chamador. `users` e `sessions` são contagens distintas
dentro da linha, que deixam de fora o `-` de uma chamada que não informou nenhum; uma soma entre linhas conta um
usuário uma vez por linha. `slow` e os quantis de latência são os do rollup de SLI: contam as
requisições `ok` que esse rollup lê (uma operação com uma classe de SLO), e `slow` conta as que passaram do
orçamento de latência (um orçamento de 0 torna todas lentas); uma linha sem nenhuma tem latências de 0.
Uma rota `/v1/me/*`, um julgamento adiado ou uma passagem do worker não conta em nenhum dos dois. O
arquivo `namespaces` é o estado em `snapshot_at`, seja qual for o `--day`: um backfill de um
dia antigo recebe o uso daquele dia e os namespaces de hoje. Um namespace não registrado tem
`registered = false` e colunas de registro NULL. `created_on` e `last_memory_on` são dias,
não horários: um horário exato ao lado de um namespace pessoal com hash poderia ser associado à pessoa
que se cadastrou ou escreveu naquele momento. As linhas são ordenadas pelos nomes com hash, nunca pelos próprios
nomes.

<a id="what-never-leaves-loom"></a>

## O que nunca sai do Loom

As colunas formam uma lista de permissões (`server/insights/store/analytics_export.py`; o teste de integração fixa a lista exata de colunas de cada entidade). Um arquivo no bucket fica fora do [apagamento](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md), então a exportação não contém conteúdo de memória nem identificador armazenado de pessoas: nenhuma chave ou valor de memória, nenhum `args`, nenhum `user_id`, `session_id`, `subject`, `owner_email` ou id de requisição. Usuários e sessões saem apenas como contagens.

<a id="hashed-names"></a>

## Nomes com hash

Os nomes de org, namespace e agente podem identificar uma pessoa: um namespace pessoal recebe o nome do e-mail do seu proprietário, o autocadastro monta o namespace e o nome de um agente a partir do nome de usuário do sistema operacional e do host, e um cliente MCP dá nome ao seu agente. Por isso, `org`, `namespace` e `agent` sempre saem como o hexadecimal em minúsculas do SipHash-2-4-128 de referência do nome, sob uma chave secreta.

Cada nome recebe o hash isoladamente, então uma mesma string tem um único hash onde quer que apareça: um nome de namespace gera o mesmo hash em todas as orgs, e uma org, um namespace e um agente com o mesmo nome compartilham o hash. O data warehouse consegue relacionar essas linhas sem a chave, por exemplo o mesmo namespace pessoal em duas orgs.

- A chave é uma string aleatória de 32 ou mais caracteres em `LOOM_ANALYTICS_EXPORT_NAME_KEY`; a exportação se recusa a rodar com uma chave mais curta ou vazia, ou com espaço em branco no início ou no fim (um Secret criado com `--from-file` mantém a quebra de linha do arquivo, e isso faria todo hash depender dela).
- A chave de 16 bytes do SipHash são os primeiros 16 bytes do SHA-256 dessa string (UTF-8). O hash é o `sipHash128ReferenceKeyed` do ClickHouse, que qualquer biblioteca de SipHash-2-4-128 reproduz a partir desses 16 bytes, de modo que um sistema que possui a chave pode aplicar o hash aos seus próprios ids para fazer o join. O ClickHouse recebe a chave como duas palavras, cada metade de 8 bytes lida em little-endian. Aplique o hash ao id como a mesma string que o Loom armazena (`toString` de um UUID ou de um número): o SipHash de uma coluna tipada calcula o hash da forma binária, que nunca coincide.

  ```sql
  WITH SHA256({key:String}) AS d
  SELECT lower(hex(sipHash128ReferenceKeyed(
      (reinterpretAsUInt64(substring(d, 1, 8)), reinterpretAsUInt64(substring(d, 9, 8))),
      toString(tenant_id)))) AS hashed_tenant_id
  FROM tenants
  ```

- Um hash é um pseudônimo, não um valor anônimo: quem possui a chave pode recalculá-lo a partir de um nome que conheça. Apagar um namespace no Loom não remove as linhas dele que já estão no bucket.
- Um hash permanece estável enquanto a chave não muda. Uma nova chave altera todos os hashes, e um dia já exportado mantém seus arquivos antigos: exclua-os antes de exportá-lo novamente com a nova chave.
- Uma consulta que usa a chave a registra, seja a chave passada como parâmetro ou escrita como literal: o ClickHouse substitui os valores passados no texto da consulta em `system.query_log` e os grava nos logs de debug e de trace. No data warehouse, restrinja quem pode ler esses logs no servidor que executa o join, ou aplique o hash aos ids fora do ClickHouse com qualquer biblioteca de SipHash-2-4-128. Do lado do Loom, as palavras da chave da exportação vão parar nos mesmos logs do ClickHouse do Loom, cujos leitores já podem ler os nomes.

<a id="credentials"></a>

## Credenciais

O comando usa as credenciais padrão da AWS do seu processo e as verifica uma vez antes de
qualquer trabalho no ClickHouse: com `--role-arn`, assumindo a role; sem ele, perguntando ao STS
a quem elas pertencem (`GetCallerIdentity`). Credenciais ausentes ou expiradas, ou uma role que não pode
ser assumida, falham nesse ponto, e não depois da leitura; a falta da permissão `s3:PutObject`, um bucket incorreto
ou um proprietário do bucket diferente de `--expected-bucket-owner` falha no primeiro upload. Com
`--role-arn`, o comando assume a role novamente antes de cada upload e grava com as credenciais dela.
Assumir uma role de escrita é adequado para um bucket em outra conta: a conta do bucket concede
`s3:PutObject` à role de escrita, e a role de escrita confia apenas na identidade da própria exportação.
A exportação não precisa de nenhuma permissão de leitura, listagem ou exclusão no bucket; um bucket
criptografado com uma chave KMS gerenciada pelo cliente também precisa de `kms:GenerateDataKey` nessa chave para
a role de escrita, caso contrário todo upload é recusado. `--expected-bucket-owner` faz o S3 recusar a
gravação quando outra conta é proprietária do bucket.

Passe `--region` (a do bucket, `analyticsExport.region` no chart), a menos que o processo
já tenha uma definida por `AWS_DEFAULT_REGION` ou pelo arquivo de configuração da AWS; o boto3 não lê
`AWS_REGION` neste caso.

<a id="on-kubernetes"></a>

## No Kubernetes

```yaml
analyticsExport:
  enabled: true
  bucket: analytics-landing
  prefix: loom
  roleArn: arn:aws:iam::123456789012:role/loom-analytics-writer
  expectedBucketOwner: "123456789012"
  nameKeySecret:
    name: loom-analytics-export-key # key LOOM_ANALYTICS_EXPORT_NAME_KEY
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::210987654321:role/loom-analytics-export
```

```bash
kubectl create secret generic loom-analytics-export-key \
  --from-literal=LOOM_ANALYTICS_EXPORT_NAME_KEY="$(openssl rand -hex 32)"
```

Somente o pod da exportação lê esse Secret. O chart se recusa a renderizar sem
`nameKeySecret.name` e recusa tudo o que entregaria a chave a todos os pods web e workers:
`LOOM_ANALYTICS_EXPORT_NAME_KEY` em `env`, `extraEnv` ou `secrets.extra`,
`analytics_export_name_key` em `settingsFile` (um ConfigMap que todos os pods montam) e um
`nameKeySecret.name` que seja o Secret da release (`secrets.existingSecret` ou o do próprio
chart) ou um Secret de `envFrom`. Ele não consegue ver o conteúdo desses Secrets: mantenha a chave fora
deles.

O chart cria uma ServiceAccount `<fullname>-analytics-export` e um CronJob com o mesmo
nome, em que `<fullname>` é o nome completo do chart: `fullnameOverride` quando definido; caso contrário, o nome
da release quando ele contém o nome do chart (`nameOverride`, senão `loom`); caso contrário,
`<release>-<chart name>`, cortado nos primeiros 35 caracteres para que o nome fique dentro de 52
(`kubectl get sa` mostra o nome). O job roda às 00:30 UTC (`schedule`), uma execução por vez,
com até 3 novas tentativas e um prazo de três horas somando todas as tentativas; cada instrução pode rodar
20 minutos, e o cliente do ClickHouse pode repetir uma delas, então um dia lento pode ter menos de quatro
tentativas. Uma recusa (exit 2) faz o Job falhar imediatamente, sem novas tentativas. O pod dele não herda nada
de `podLabels`, `podAnnotations` ou `affinity` dos pods web.
Os pods web e workers mantêm a própria ServiceAccount, então o role que pode assumir o
role de escrita fica vinculado somente ao pod da exportação. Com IRSA, a relação de confiança desse role cita
`system:serviceaccount:<namespace>:<fullname>-analytics-export`. Com `roleArn` definido, a
única permissão dele é `sts:AssumeRole` em `roleArn`; com `roleArn` vazio, ele mesmo escreve e
precisa de `s3:PutObject` no bucket (e `kms:GenerateDataKey` em uma chave KMS gerenciada
pelo cliente). Com EKS Pod Identity, vincule o role à ServiceAccount e deixe `annotations`
vazio.

O job lê o ClickHouse com o Secret, as configurações, o `extraEnv` e os
`extraVolumes` da release, como fazem os pods web, e não grava nada nele. Uma execução com falha aparece como um
Job com falha; configure um alerta para quando a última execução bem-sucedida do CronJob tiver mais de um dia.

Um dia cuja execução falha em todas as tentativas, ou que perde a janela de início
(`startingDeadlineSeconds`, uma hora), não é exportado depois por conta própria: a próxima execução
exporta apenas o dia anterior a ela. Exporte-o com um Job criado a partir do CronJob, para que rode
com a ServiceAccount e os flags da exportação, adicionando `--day` ao comando dele:

```bash
kubectl create job --from=cronjob/<fullname>-analytics-export analytics-export-backfill \
  --dry-run=client -o yaml > backfill.yaml
# add `- --day=YYYY-MM-DD` to the container's command in backfill.yaml, then:
kubectl apply -f backfill.yaml
```

O arquivo de uso é o daquele dia; o arquivo de namespaces é o snapshot do momento da execução.
