<!-- i18n: source=docs/use-cases/multi-agent.md blob=d147692b665800cf10e0dfda74f2fdc9f9de8293 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# Contexto compartido para flujos de trabajo con varios agentes

Loom ofrece a los agentes de un flujo de trabajo con varios agentes un único lugar donde compartir contexto: memoria que lee cada agente de un espacio de nombres, bandejas de tareas para pasar trabajo de un agente a otro y un registro de decisiones. Tus agentes y tu orquestador ejecutan el flujo de trabajo; Loom conserva lo que aprenden y traspasan.

<a id="what-you-get"></a>

## Qué obtienes

- **Memoria compartida con procedencia.** Cada fila registra qué agente la escribió, y con la política predeterminada `auto_reinforce` una contradicción entre dos agentes se marca para revisión en lugar de sobrescribirse en silencio. [Espacios de nombres y uso compartido](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [Política de conflictos del espacio de nombres](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **Bandejas de tareas.** Cada agente tiene una cola duradera con concesiones (leases), reintentos y reglas de revisión; una tarea reclamada tiene un único responsable a la vez. [Tareas, decisiones y políticas](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **Decisiones.** Qué se decidió en un caso como este, por qué y si funcionó.
  [Decisiones](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **Memoria de trabajo.** Texto que se entrega al agente siempre, en lugar de encontrarlo mediante búsqueda, separado de la memoria a largo plazo. [Memoria de trabajo y perfiles](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **Paquetes de contexto.** `context.pack` construye el paquete acotado que un agente carga al inicio de una tarea. [Búsqueda unificada y paquetes de contexto](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Resúmenes de sesión.** El estado reanudable de una línea de trabajo: objetivo, estado, decisiones, callejones sin salida y lo que está hecho. Un agente lo guarda con `session.brief_save` y el siguiente lo carga con `session.brief_get`. [Resúmenes de sesión](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md#session-briefs)
- **Adaptadores de frameworks.** Los SDK presentan Loom a través de las interfaces de memoria propias de los frameworks de agentes. [Frameworks de agentes](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="two-agents-sharing-a-research-note"></a>

## Dos agentes que comparten una nota de investigación

En el portátil de una sola persona, la [guía de inicio rápido para un solo usuario](../getting-started/quickstart.md) permite que dos agentes conectados lean el mismo espacio de nombres. Conecta ambos clientes con la [guía de conexión](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md); por ejemplo, usa Claude Code para recopilar un hallazgo y Cursor para escribir a partir de él.

1. En el primer cliente, pídele que use `loom_memory_set` para guardar `example/research/next-step` con el valor “Check the paper's evaluation dataset before adding it to the reading list.” Comprueba el `memory_id` devuelto y busca la fila en el dashboard.
2. En el otro cliente, pídele que use `loom_memory_get` para leer exactamente esa clave. Exige la respuesta de la herramienta, incluidos `found`, `value` y `agent`, antes de que redacte un siguiente paso. Debería leer el valor del primer cliente e identificar quién lo escribió.
3. Cuando termines, elimina la clave de ejemplo desde el cliente que la escribió.

Los agentes comparten un hecho guardado; Loom no los ejecuta ni programa su trabajo. Para agentes en máquinas de personas distintas, usa un [despliegue de equipo autenticado](../getting-started/quickstart.md#start-in-multi-user-mode), con el token de cada persona concediendo acceso al mismo espacio de nombres. Una ejecución local de un solo usuario no prueba esos controles de acceso.

<a id="start"></a>

## Primeros pasos

[Integrar Loom en un agente](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) explica la identidad que envía cada agente y qué escribir; [tokens y permisos](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) da a cada agente su propio token.

Algunas ops de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
