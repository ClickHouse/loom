<!-- i18n: source=docs/use-cases/agent-memory.md blob=ef7ede85eb9ef02954cc9bdd80508b69b4b006f0 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# Memoria compartida y de largo plazo para agentes de IA

Loom es un servidor MCP y una API HTTP autoalojados que dan a los agentes de IA una memoria de largo plazo que comparten: un dato que un agente guarda en una sesión puede encontrarlo la sesión siguiente, otro agente o una persona en el dashboard. Esta página muestra cómo funciona y por dónde empezar.

<a id="what-you-get"></a>

## Qué obtienes

- **Una afirmación por clave, en un espacio de nombres.** `memory.set` guarda un dato; `memory.get` lo lee de vuelta por clave; `memory.search` y `loom_search` lo encuentran por significado. Un espacio de nombres es la unidad de uso compartido: por defecto, cada miembro lee todas las filas que contiene.
  [Memoria](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Memoria que se puede corregir.** Un cambio crea una versión nueva con su historial. El mismo dato tras la canonicalización se fusiona con la fila almacenada, el mismo dato expresado con otras palabras se guarda como duplicado, una contradicción se marca para revisión y una escritura sobre un dato fijado se rechaza.
  [Conflictos y rechazo](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [Historial y procedencia](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Búsqueda que se abstiene.** Cuando nada supera el umbral de similitud, `memory.search` responde `abstain=true` con un código de motivo y etiqueta cada resultado débil como `below_threshold`; `loom_search` los descarta. [Búsqueda y consulta](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **Memoria de largo plazo para tu agente de código, compartida entre agentes.** Los plugins de Claude Code y Codex cargan el contexto relevante al inicio de la sesión y capturan la sesión mientras se ejecuta. Cursor, GitHub Copilot, Gemini CLI, Cline, Kilo Code, OpenCode, goose, Zed y otros clientes MCP se conectan al mismo servidor, de modo que un dato que guarda un agente lo encuentran los demás.
  [Conectar un agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [la configuración de cada cliente](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#other-editors-and-agents)
- **Memoria por usuario para tu propio agente.** Los SDK de Python y TypeScript escriben y leen memoria por cada usuario final. [Tu primer agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## Medición

En 100 preguntas emparejadas de LongMemEval-S con el mismo lector `gpt-5`, las respuestas basadas en las memorias recuperadas por Loom obtuvieron un 94 % frente a un 90 % con el historial completo, con un 91 % menos de tokens en el prompt del lector (sin contar el costo único de construir la memoria); la diferencia de precisión no está establecida estadísticamente.
[Con y sin Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## Empezar

[Guía de inicio rápido](../getting-started/quickstart.md) levanta el stack local y guarda una primera memoria por HTTP, Python y MCP. [Cuándo Loom no encaja](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit) cubre los casos en que una biblioteca de memoria en proceso resulta más adecuada.

Algunas ops de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
