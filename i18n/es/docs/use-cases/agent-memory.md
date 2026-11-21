<!-- i18n: source=docs/use-cases/agent-memory.md blob=e9322c4d7b4b3395943f3caead94d6436cd0954b model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# Memoria a largo plazo y compartida para agentes de IA

Loom es un servidor MCP y una API HTTP autoalojados que dan a los agentes de IA una memoria a largo plazo compartida: un hecho que un agente guarda en una sesión puede encontrarlo la sesión siguiente, otro agente o una persona desde el dashboard. Esta página muestra cómo funciona y por dónde empezar.

<a id="what-you-get"></a>

## Qué obtienes

- **Una afirmación por clave, en un espacio de nombres.** `memory.set` guarda un hecho; `memory.get` lo lee de vuelta por clave; `memory.search` y `loom_search` lo encuentran por significado. Un espacio de nombres es la unidad de uso compartido: por defecto, cada miembro lee todas las filas que contiene.
  [Memoria](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Una memoria que se puede corregir.** Un cambio crea una versión nueva con su historial. El mismo hecho tras la canonicalización se fusiona con la fila almacenada, el mismo hecho dicho con otras palabras se almacena como duplicado, una contradicción se marca para revisión y una escritura contra un hecho fijado se rechaza.
  [Conflictos y rechazo](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [Historial y procedencia](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Una búsqueda que se abstiene.** Cuando nada supera el umbral de similitud, `memory.search` responde `abstain=true` con un código de motivo y etiqueta cada resultado débil como `below_threshold`; `loom_search` los descarta. [Búsqueda y consulta](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **Memoria a largo plazo para tu agente de código, compartida entre agentes.** Los plugins de Claude Code y Codex cargan el contexto relevante al iniciar la sesión y capturan la sesión mientras se ejecuta. Cursor, GitHub Copilot, Gemini CLI, Cline, Kilo Code, OpenCode, goose, Zed y otros clientes MCP se conectan al mismo servidor, y un agente en un espacio de nombres encuentra los hechos que otros agentes guardaron allí.
  [Conectar un agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- **Memorias vinculadas al código del que tratan.** Las lecciones de un agente de código siguen a las definiciones que nombran a través de renombrados y ediciones, y aparecen cuando un agente lee ese código. [Memoria vinculada al código](memory-linked-to-code.md)
- **Memoria por usuario para tu propio agente.** Los SDK de Python y TypeScript escriben y leen la memoria por usuario final. [Tu primer agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## Medición

En 100 preguntas emparejadas de LongMemEval-S con el mismo lector `gpt-5`, las respuestas a partir de las memorias recuperadas por Loom obtuvieron un 94 % frente a un 90 % con el historial completo, con un 91 % menos de tokens en el prompt del lector (sin contar el coste único de construir la memoria); la diferencia de precisión no está establecida estadísticamente.
[Con y sin Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="a-research-assistant-that-remembers-your-reading-preferences"></a>

## Un asistente de investigación que recuerda tus preferencias de lectura

Después del [inicio rápido](../getting-started/quickstart.md), conecta Claude Desktop o Cursor mediante la [configuración de clientes](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#how-do-i-give-cursor-or-another-mcp-client-shared-memory) del dashboard.
Las herramientas funcionan con el LLM estándar: tu aplicación de chat aporta el modelo que las llama.

1. Dile al asistente: “Usa `loom_memory_set` para guardar mi preferencia de investigación en `example/research/source-policy`: incluir artículos revisados por pares en mi lista de lectura.”
2. Comprueba que la respuesta de la herramienta contiene un `memory_id`. Abre **Memories** en el dashboard y confirma el valor y el escritor. Si la herramienta rechaza la escritura, resuelve el problema que indique antes de continuar.
3. Inicia un chat nuevo. Dile al asistente: “Usa `loom_memory_get` para leer `example/research/source-policy` y dime qué fuentes pertenecen a mi lista de lectura.”
   Comprueba que la herramienta devuelve `found: true` y la preferencia que guardaste.
4. Pide al asistente que elimine esa clave de ejemplo al terminar y confirma que el dashboard ya no la muestra como memoria activa.

El segundo chat lee una preferencia almacenada sin la transcripción del primero. Esto comprueba la persistencia y la conexión del cliente; no prueba si el asistente sabe valorar artículos o responder preguntas de investigación. Para buscar en los propios artículos, usa la [búsqueda de documentos](document-search.md).

<a id="start"></a>

## Empezar

El [inicio rápido](../getting-started/quickstart.md) levanta la pila local y guarda una primera memoria por HTTP, Python y MCP. [Cuándo Loom no encaja](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit) cubre los casos que una biblioteca de memoria en proceso atiende mejor.

Algunas ops de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).

Usa un [proveedor de contexto de Microsoft Agent Framework](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework) para recuperar hechos del usuario antes de una ejecución y registrar la conversación después.

Guarda en Loom el árbol de archivos de la [herramienta de memoria de la API de Claude](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#claude-api-memory-tool) para que los agentes de un espacio de nombres compartan su contenido.

Conserva en Loom el [historial de sesiones de OpenAI Agents](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#openai-agents-sdk) entre ejecuciones de agentes y traspasos.

Para un flujo de trabajo visual, los [nodos de n8n](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n) conectan un agente de IA con el historial de conversaciones, las memorias duraderas y la búsqueda de conocimiento de Loom.
