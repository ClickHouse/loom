<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# Contexto compartilhado para fluxos de trabalho com vários agentes

O Loom oferece aos agentes de um fluxo de trabalho com vários agentes um único lugar para compartilhar contexto: memória que todo agente de um namespace lê, caixas de entrada de tarefas para passar trabalho de um agente a outro e um registro de decisões. Seus agentes e o orquestrador executam o fluxo de trabalho; o Loom guarda o que eles aprendem e repassam.

<a id="what-you-get"></a>

## O que você recebe

- **Memória compartilhada com proveniência.** Cada linha registra qual agente a escreveu e, sob a política padrão `auto_reinforce`, uma contradição entre dois agentes é sinalizada para revisão em vez de ser sobrescrita silenciosamente. [Namespaces e compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [Política de conflito do namespace](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **Caixas de entrada de tarefas.** Cada agente tem uma fila durável com leases, novas tentativas e regras de revisão; uma tarefa assumida tem um único responsável por vez. [Tarefas, decisões e políticas](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **Decisões.** O que foi decidido em um caso como este, por quê e se funcionou.
  [Decisões](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **Memória de trabalho.** Texto que o agente recebe sempre, em vez de encontrá-lo por busca, separado da memória de longo prazo. [Memória de trabalho e perfis](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **Pacotes de contexto.** `context.pack` monta o pacote limitado que um agente carrega no início de uma tarefa. [Busca unificada e pacotes de contexto](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Adaptadores de framework.** Os SDKs apresentam o Loom pelas próprias interfaces de memória dos frameworks de agentes. [Frameworks de agentes](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## Primeiros passos

[Integrar o Loom a um agente](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) explica a identidade que cada agente envia e o que escrever; [tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) dá a cada agente seu próprio token.

Algumas ops desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
