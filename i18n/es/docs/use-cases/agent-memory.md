<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# Memoria compartida y a largo plazo para agentes de IA

Loom es un servidor MCP y una API HTTP autoalojados que dan a los agentes de IA una memoria a largo plazo que comparten: un hecho que un agente guarda en una sesión puede encontrarlo la sesión siguiente, otro agente o una persona en el dashboard. Esta página muestra cómo funciona y por dónde empezar.

<a id="what-you-get"></a>

## Qué obtienes

- **Una afirmación por clave, en un espacio de nombres.** `memory.set` guarda un hecho; `memory.get` lo lee
  por su clave; `memory.search` y `loom_search` lo encuentran por significado. Un espacio de nombres es la
  unidad de uso compartido: por defecto, cada miembro lee todas las filas que contiene.
  [Memoria](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Memoria que se puede corregir.** Un cambio crea una versión nueva con su historial. El
  mismo hecho tras la canonicalización se fusiona con la fila almacenada, el mismo hecho dicho con otras
  palabras se almacena como duplicado, una contradicción se marca para revisión y se rechaza una escritura
  contra un hecho fijado.
  [Conflictos y rechazo](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [Historial y procedencia](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Búsqueda que se abstiene.** Cuando nada supera el umbral de similitud, `memory.search`
  responde `abstain=true` con un código de motivo y etiqueta cada resultado débil como `below_threshold`;
  `loom_search` los descarta. [Búsqueda y consulta](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **Memoria entre agentes de código.** Los plugins de Claude Code y Codex cargan el contexto
  relevante al inicio de la sesión y capturan la sesión mientras se ejecuta; Cursor y otros clientes MCP
  se conectan al mismo servidor. [Conectar un agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **Memoria por usuario para tu propio agente.** Los SDK de Python y TypeScript escriben y leen
  la memoria por cada usuario final. [Tu primer agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## Mediciones

En 100 preguntas emparejadas de LongMemEval-S con el mismo lector `gpt-5`, las respuestas a partir de las memorias recuperadas por Loom obtuvieron un 94 % frente a un 90 % con el historial completo, con un 91 % menos de tokens en el prompt del lector (sin contar el costo único de construir la memoria); la diferencia de precisión no está establecida estadísticamente.
[Con y sin Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## Empezar

La [Guía de inicio rápido](../getting-started/quickstart.md) levanta la pila local y guarda una primera
memoria por HTTP, Python y MCP. [Cuándo Loom no encaja](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)
describe los casos en que una biblioteca de memoria en proceso resulta más adecuada.

Algunas operaciones de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
