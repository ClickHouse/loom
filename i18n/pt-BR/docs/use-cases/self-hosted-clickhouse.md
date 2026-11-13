<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# Memória de agente self-hosted no ClickHouse

O Loom roda na sua própria infraestrutura, com Docker ou no Kubernetes com o Helm chart, e
armazena a memória de agente, documentos, conversas e índices de código como tabelas no ClickHouse. Uma
implantação pode escolher o Postgres na primeira inicialização para o estado de memória e conhecimento em tempo real. Os dados são
armazenados apenas nos seus próprios stores (os provedores de modelos hospedados e os destinos de traces que você configurar
recebem o que processam), e o que seus agentes sabem pode ser consultado com SQL.

<a id="what-you-get"></a>

## O que você recebe

- **Docker ou Kubernetes.** O instalador de uma linha inicia a stack local; o Helm chart
  executa N réplicas idênticas atrás de uma única URL. [Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Running N replicas](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS ou ClickHouse Cloud.** [Choosing the ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud and Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **Um único armazenamento para vetores, texto e auditoria.** Sob a autoridade padrão do ClickHouse,
  a busca vetorial, lexical e em grafo, os filtros, a auditoria e o apagamento são SQL em um único banco de dados.
  [Storage authority](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **Multiusuário.** Orgs, namespaces, papéis, tokens e single sign-on.
  [Identities and namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [Single sign-on](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **Modelos locais.** A stack lançada gera embeddings na CPU com um modelo local incluído, e
  o provedor de embeddings aceita um endpoint compatível com a OpenAI que você mesmo executa.
  [Models and API keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## Medições

Uma carga sintética de memórias com 2.4 versões por chave e embeddings de 3072 dimensões (dois
vetores por linha) ocupou cerca de 13.6 KB por chave no ClickHouse, quase todo ele na linha do
embedding; um embedder menor ocupa menos, e partes com poucas linhas carregam um pouco mais por linha
até serem mescladas.
[Memória em escala](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## Início

[Quickstart](../getting-started/quickstart.md) · [Pré-requisitos](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) ·
[Atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [Backup e recuperação](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
