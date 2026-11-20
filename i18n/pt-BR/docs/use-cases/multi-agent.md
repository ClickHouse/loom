<!-- i18n: source=docs/use-cases/multi-agent.md blob=d147692b665800cf10e0dfda74f2fdc9f9de8293 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# Contexto compartilhado para fluxos de trabalho com vários agentes

O Loom oferece aos agentes de um fluxo de trabalho com vários agentes um único lugar para compartilhar contexto: memória que todo agente de um namespace lê, caixas de entrada de tarefas para repassar trabalho de um agente a outro e um registro de decisões. Seus agentes e o orquestrador executam o fluxo de trabalho; o Loom guarda o que eles aprendem e repassam.

<a id="what-you-get"></a>

## O que você recebe

- **Memória compartilhada com proveniência.** Cada linha registra qual agente a escreveu e, na política padrão `auto_reinforce`, uma contradição entre dois agentes é sinalizada para revisão em vez de ser sobrescrita silenciosamente. [Namespaces e compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [Política de conflito do namespace](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **Caixas de entrada de tarefas.** Cada agente tem uma fila durável com leases, novas tentativas e regras de revisão; uma tarefa assumida tem um único responsável por vez. [Tarefas, decisões e políticas](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **Decisões.** O que foi decidido em um caso como este, por quê e se funcionou.
  [Decisões](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **Memória de trabalho.** Texto que o agente recebe sempre, em vez de encontrá-lo por busca, separado da memória de longo prazo. [Memória de trabalho e perfis](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **Pacotes de contexto.** `context.pack` monta o pacote limitado que um agente carrega no início de uma tarefa. [Busca unificada e pacotes de contexto](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Resumos de sessão.** O estado retomável de uma linha de trabalho: objetivo, status, decisões, becos sem saída e o que está concluído. Um agente o salva com `session.brief_save` e o próximo o carrega com `session.brief_get`. [Resumos de sessão](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md#session-briefs)
- **Adaptadores de framework.** Os SDKs apresentam o Loom pelas próprias interfaces de memória dos frameworks de agentes. [Frameworks de agentes](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="two-agents-sharing-a-research-note"></a>

## Dois agentes compartilhando uma nota de pesquisa

No laptop de uma única pessoa, o [início rápido para um único usuário](../getting-started/quickstart.md) permite que dois agentes conectados leiam o mesmo namespace. Conecte os dois clientes com o [guia de conexão](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md); por exemplo, use o Claude Code para reunir uma descoberta e o Cursor para escrever a partir dela.

1. No primeiro cliente, peça que use `loom_memory_set` para salvar
   `example/research/next-step` com o valor “Check the paper's evaluation dataset
   before adding it to the reading list.” Confira o `memory_id` retornado e encontre a
   linha no painel.
2. No outro cliente, peça que use `loom_memory_get` para ler exatamente essa chave. Exija a resposta da ferramenta, incluindo `found`, `value` e `agent`, antes que ele redija um próximo passo. Ele deve ler o valor do primeiro cliente e identificar quem o escreveu.
3. Ao terminar, exclua a chave de exemplo a partir do cliente que a escreveu.

Os agentes compartilham um fato salvo; o Loom não os executa nem agenda o trabalho deles. Para agentes em máquinas de pessoas diferentes, use uma [implantação de equipe autenticada](../getting-started/quickstart.md#start-in-multi-user-mode), com o token de cada pessoa concedendo acesso ao mesmo namespace. Uma execução local para um único usuário não testa esses controles de acesso.

<a id="start"></a>

## Comece

[Integrar o Loom a um agente](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) explica a identidade que cada agente envia e o que escrever; [tokens e concessões](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) dá a cada agente o seu próprio token.

Algumas operações desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
