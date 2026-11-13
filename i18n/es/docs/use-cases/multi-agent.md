<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# Contexto compartido para flujos de trabajo con varios agentes

Loom ofrece a los agentes de un flujo de trabajo con varios agentes un único lugar donde compartir contexto: una memoria que leen todos los agentes de un espacio de nombres, bandejas de entrada de tareas para pasar trabajo de un agente a otro y un registro de decisiones. Tus agentes y tu orquestador ejecutan el flujo de trabajo; Loom conserva lo que aprenden y lo que se traspasan.

<a id="what-you-get"></a>

## Qué obtienes

- **Memoria compartida con procedencia.** Cada fila registra qué agente la escribió, y con la política `auto_reinforce` por defecto, una contradicción entre dos agentes se marca para revisión en lugar de sobrescribirse en silencio. [Espacios de nombres y uso compartido](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [Política de conflictos del espacio de nombres](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **Bandejas de tareas.** Cada agente tiene una cola duradera con leases, reintentos y reglas de revisión; una tarea reclamada tiene un solo propietario a la vez. [Tareas, decisiones y políticas](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **Decisiones.** Qué se decidió en un caso como este, por qué y si funcionó.
  [Decisiones](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **Memoria de trabajo.** Texto que se le entrega al agente cada vez, en lugar de encontrarlo mediante búsqueda, separado de la memoria a largo plazo. [Memoria de trabajo y perfiles](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **Paquetes de contexto.** `context.pack` construye el paquete acotado que un agente carga al inicio de una tarea. [Búsqueda unificada y paquetes de contexto](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Adaptadores de frameworks.** Los SDK presentan Loom a través de las interfaces de memoria propias de los frameworks de agentes. [Frameworks de agentes](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## Inicio

[Integrar Loom en un agente](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) explica la identidad que envía cada
agente y qué escribir; [tokens y permisos](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) asigna a
cada agente su propio token.

Algunas operaciones de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
