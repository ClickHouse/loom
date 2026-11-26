<!-- i18n: source=docs/index.md blob=173314858f057f518abf314eb98af3afff03c87b model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/index.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/index.md), which is authoritative.

<a id="clickhouse-loom-documentation"></a>

# Documentação do ClickHouse Loom

Loom é um servidor de memória, documentos e índice de código para agentes de IA, armazenado no ClickHouse,
com [Postgres](architecture/storage-authority.md#why-two-stores) como opção para o estado de memória e conhecimento em tempo real. Agentes e pessoas o acessam por MCP, HTTP, pelos SDKs de Python e TypeScript e pelo painel. Esta página lista todas as páginas na ordem de leitura.

![A Biblioteca do painel: uma busca por uma regra de reembolso respondida com trechos citados](https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/document-search.webp)

<video src="https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/clip-assistant-answer.webm" controls autoplay loop muted playsinline width="100%" aria-label="Ask AI answering a question about refunds, with its numbered sources checked against Loom"><a href="https://github.com/ClickHouse/loom/blob/main/docs/assets/screenshots/clip-assistant-answer.webm">Ask AI respondendo com fontes verificadas (clipe)</a></video>

<a id="start-here"></a>

## Comece aqui

| Etapa                    | Leia                                                                                                                                                                                      |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Decidir               | [Visão geral](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md) → [Avaliar o Loom](getting-started/evaluate.md)                                                                                                    |
| 2. Instalar              | [Início rápido](getting-started/quickstart.md) → [Implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)                                                                                                      |
| 3. Conectar agentes      | [Conectar um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) → [Integrações](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)                                                                      |
| 4. SDK e API             | [Seu primeiro agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md) → [SDK Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) / [API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)                                                            |
| 5. Namespaces e acesso   | [Identidades e namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) → [Tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)                                                                     |
| 6. Índice de código      | [Índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) → [Repositórios](features/code-index/repositories.md) → [Operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)                        |
| 7. Operar                | [Monitoramento](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md) · [Solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md) · [Atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [Backup e recuperação](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md) |

<a id="use-cases"></a>

## Casos de uso

Uma página para cada necessidade, explicando o que o Loom faz por ela e por onde começar:
[memória de agente](use-cases/agent-memory.md) ·
[busca em documentos e RAG](use-cases/document-search.md) ·
[índice de código para agentes de programação](use-cases/code-intelligence.md) ·
[índice de código por linguagem](use-cases/code-intelligence-by-language.md) ·
[memória vinculada ao código](use-cases/memory-linked-to-code.md) ·
[custo de modelo de agentes de programação](use-cases/coding-agent-cost.md) ·
[contexto compartilhado entre vários agentes](use-cases/multi-agent.md) ·
[agentes autônomos: recuperação, auditoria, exclusão](use-cases/autonomous-agents.md) ·
[execuções, traces e histórico de PRs](use-cases/runs-and-pr-history.md) ·
[auto-hospedado no ClickHouse](use-cases/self-hosted-clickhouse.md) ·
[todos os casos de uso](use-cases/index.md).

<a id="1-getting-started"></a>

## 1. Primeiros passos

- [Visão geral](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md): o que é o Loom, quando ele é adequado, pré-requisitos.
- [O que está incluído](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/whats-included.md): as principais capacidades do Loom, cada uma com a sua página.
- [Início rápido](getting-started/quickstart.md): a stack local de ponta a ponta.
- [Configurar modelos e chaves de API](getting-started/models-and-keys.md): assinaturas de agentes,
  LLMs do servidor, embeddings e configurações com um único provedor.
- [Avaliar o Loom](getting-started/evaluate.md): smoke test, avaliação de carga de trabalho e
  benchmarks públicos reproduzíveis.
- [Conectar um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md): plugin ou entrada MCP.
- [Configurar o Loom com o seu agente de código](getting-started/agent-setup.md): para um agente que
  instala uma versão fixada e comprova que ela funciona, com um relatório fixo.
- [Seu primeiro agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): um tutorial de SDK.

<a id="2-install-and-run-the-server"></a>

## 2. Instalar e executar o servidor

- [Implantação](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): formatos, ambiente, provedores, modos de identidade,
  proxy, imagens, checklist de produção.
- [Login único](operations/sso.md): um provedor de identidade OIDC para o painel,
  `loom login` e clientes MCP.
- [Usar o Loom sem o painel](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md): `loom login`, `loom call`
  e a chamada do SDK para cada visão do painel.
- [Atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [Versões suportadas](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md) ·
  [Backup e recuperação](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md) ·
  [Monitoramento](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md) · [Manutenção](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md) ·
  [Configurações de runtime](operations/runtime-settings.md) ·
  [Solução de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md) ·
  [Operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md) ·
  [Executar N réplicas](operations/replicas.md): Looms idênticos atrás de uma única URL ·
  [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md): a autoridade de memória em Postgres ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md): o Helm chart, com overlays para EKS e kind ·
  [ClickHouse Cloud e Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md): o chart sobre
  os dois serviços gerenciados ·
  [Copiar um namespace para outro Loom](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md) ·
  [Exportar o uso para um data warehouse](operations/analytics-export.md)

<a id="3-connect-agents"></a>

## 3. Conectar agentes

[Integrações](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md) ·
[Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) ·
[Instruções para agentes](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md) ·
[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) ·
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) · [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md) ·
[VS Code](features/integrations/vscode.md) ·
[Estruturas de agentes](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md) ·
[Adaptador de grep](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md) ·
[Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md) ·
[ClickStack](features/integrations/clickstack.md) ·
[OKF](features/integrations/okf.md)

<a id="4-sdk-and-api"></a>

## 4. SDK e API

[Índice de referência](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md) · [API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md) ·
[MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md) · [SDK Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) ·
[SDK TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) · [Ambiente](https://github.com/ClickHouse/loom/blob/main/docs/reference/environment.md) ·
[Operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md) · [Configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) ·
[Processos de trabalho](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md) · [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md) · [Erros](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md) ·
[Estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) · [Esquema](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md) ·
[Contrato do extrator](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md). As operações e os campos mudam entre versões;
[campos desconhecidos e removidos](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields) explica como um
cliente enxerga um campo que o servidor não lê mais.

<a id="5-namespaces-access-and-the-memory-model"></a>

## 5. Namespaces, acesso e o modelo de memória

- [Identidades e namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md): a tupla, papéis,
  concessões e escopo de leitura.
- [Memória](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md): a linha, como uma escrita é avaliada, tempo, histórico,
  proveniência.
- [Recuperação e ranqueamento](concepts/retrieval-and-ranking.md): legs, fusão, abstenção,
  calibração.

<a id="guides"></a>

## Guias

[Integração com agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) ·
[Bots headless](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md) ·
[Tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) ·
[Rastrear e retomar execuções longas](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) ·
[Otimizar um namespace](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md) ·
[Memória de agentes de investigação](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md) ·
[Pipelines de extração](guides/extraction-pipeline.md) ·
[Ajuste de desempenho](guides/performance-tuning.md) · [Depuração](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md) ·
[Consultar o Loom a partir do Postgres](guides/query-from-postgres.md) ·
[Ensinando com o Loom](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md)

<a id="features"></a>

## Recursos

- Planos: [namespaces e compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [busca unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) · [conhecimento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md) ·
  [grafo de entidades](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md) ·
  [conversas e sessões](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [execuções](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md) · [tarefas e decisões](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md) ·
  [painel](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)
- Memória: [escrita](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md) ·
  [busca e perguntas](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) ·
  [conflitos e recusa](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) ·
  [histórico e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md) ·
  [retenção e exclusão](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) ·
  [procedimentos e aprendizados](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md) ·
  [memória de trabalho e perfis](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- Observabilidade: [auditoria](features/observability/audit.md) ·
  [custo e uso](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md) ·
  [telemetria](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)
- Tracker: [visão geral](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) ·
  [contrato de registro](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)

<a id="6-code-index"></a>

## 6. Índice de código

[Visão geral](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) ·
[Repositórios](features/code-index/repositories.md) ·
[Atualidade](features/code-index/freshness.md) ·
[Histórico e PRs](features/code-index/history-and-prs.md) ·
[Operações](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md) ·
[Runbook de operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)

<a id="architecture"></a>

## Arquitetura

[Visão geral do sistema](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md) ·
[Layout do servidor](architecture/server-layout.md) ·
[ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md) ·
[Arquitetura de memória](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md) ·
[Autoridade de armazenamento](architecture/storage-authority.md) ·
[Células](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md) ·
[Layout de armazenamento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-layout.md) ·
[Arquitetura do índice de código](architecture/code-index-architecture.md) ·
[Extração de documentos](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-extraction.md) ·
[Fontes de documentos](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md) ·
[LLM e embeddings](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md) ·
[Segurança](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md) ·
[Design do assistente](https://github.com/ClickHouse/loom/blob/main/docs/architecture/assistant.md) ·
[Falhas e recuperação](architecture/failure-and-recovery.md) ·
[Atualizações graduais](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)

<a id="benchmarks"></a>

## Benchmarks de desempenho

[Visão geral](benchmarks/overview.md) · [Cobertura](benchmarks/coverage.md) ·
[Metodologia](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[Execução](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md) · [Resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[Adicionando um caso](benchmarks/adding-a-case.md) · [Avaliação de skill](benchmarks/skill-eval.md)

<a id="development"></a>

## Desenvolvimento

Para quem contribui: [guia do repositório](https://github.com/ClickHouse/loom/blob/main/docs/development/repository-guide.md) ·
[desenvolvimento local](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md) ·
[testes](development/testing.md) · [geração de código](development/code-generation.md) ·
[migrações](development/migrations.md) ·
[processo de release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md) ·
[estilo da documentação](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md) ·
[design do ciclo de curadoria](https://github.com/ClickHouse/loom/blob/main/docs/development/curation-loop-design.md) ·
[estendendo o Loom](development/extending-loom.md) ·
[adicionar uma operação](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-an-operation.md) ·
[adicionar um worker](development/adding-a-worker.md) ·
[adicionar uma tabela](development/adding-a-table.md) ·
[adicionar suporte a uma linguagem](development/adding-language-support.md) ·
[design do painel](development/dashboard-design.md).
O [CONTRIBUTING.md](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) descreve o fluxo de pull requests.

<a id="also"></a>

## Veja também

[Glossário](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md): os termos do Loom, definidos uma única vez. [Perguntas frequentes](faq.md): as primeiras perguntas, um
parágrafo para cada. [`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt): um índice gerado da documentação para agentes;
[`llms-full.txt`](https://loom.clickhouse.com/docs/llms-full.txt) reúne, em um único arquivo, as
páginas que explicam o que é o Loom e como começar.
