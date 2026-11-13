<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# Contexto compartido para flujos de trabajo con múltiples agentes

Loom ofrece a los agentes de un flujo de trabajo con múltiples agentes un lugar único para compartir contexto: memoria que lee cada agente de un espacio de nombres, bandejas de tareas para pasar trabajo de un agente a otro y un registro de decisiones. Tus agentes y tu orquestador ejecutan el flujo de trabajo; Loom conserva lo que aprenden y lo que se traspasan.

<a id="what-you-get"></a>

## Qué obtienes

- **Memoria compartida con procedencia.** Cada fila registra qué agente la escribió y, con la política `auto_reinforce` predeterminada, una contradicción entre dos agentes se marca para revisión en lugar de sobrescribirse en silencio. [Namespaces and sharing](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [Namespace conflict policy](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **Bandejas de tareas.** Cada agente tiene una cola duradera con concesiones (leases), reintentos y reglas de revisión; una tarea reclamada tiene un único responsable a la vez. [Tasks, decisions and policies](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **Decisiones.** Qué se decidió en un caso como este, por qué y si funcionó.
  [Decisions](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **Memoria de trabajo.** Texto que se entrega al agente cada vez, en lugar de encontrarlo mediante búsqueda, separado de la memoria a largo plazo. [Working memory and profiles](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **Paquetes de contexto.** `context.pack` construye el paquete acotado que un agente carga al inicio de una tarea. [Unified search and context packs](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Adaptadores de frameworks.** Los SDK presentan Loom a través de las interfaces de memoria propias de los frameworks de agentes. [Agent frameworks](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## Primeros pasos

[Integrate Loom into an agent](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) explica la identidad que envía cada agente y qué escribir; [tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) da a cada agente su propio token.

Algunas operaciones de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
