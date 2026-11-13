<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# Menor costo de modelo para agentes de programación

Con el plugin de Loom y el índice de código, Claude Code respondió preguntas sobre el código base de ClickHouse con un costo de modelo entre un 6% y un 21% menor que sin ellos en los tres conjuntos de preguntas medidos. El ahorro fue del 20% en 24 preguntas calificadas por respuesta (calidad de respuesta 0.84 frente a 0.78), del 21% en 36 preguntas conceptuales y del 6% en 24 preguntas de múltiples saltos, donde rastrear un error hasta su corrección en una rama de lanzamiento costó lo mismo en ambos casos.

<a id="what-was-measured"></a>

## Qué se midió

Ambos brazos ejecutaron Claude Code (`claude-sonnet-5`) sobre el mismo checkout: una sesión simple y otra
con el plugin de Loom y el toolset `coding`. El costo es la factura de modelo de toda la sesión y
excluye las llamadas a modelos del lado del servidor de Loom, el hosting y la indexación. La comparación mide
el plugin como un todo; no separa el índice de código de la memoria. Los resultados son
específicos de las cargas de trabajo probadas. Con Loom una pregunta tarda más en tiempo real: la
búsqueda por prompt del plugin y los viajes de ida y vuelta de MCP añaden latencia. La forma en que se compara el costo por pregunta
con el ruido entre ejecuciones está en la página de resultados.
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) incluye cada conjunto, el
protocolo y los artefactos, y [evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) muestra cómo
medirlo en tu propio repositorio.

Para la memoria leída por un modelo en lugar de un historial de chat completo, consulta
[agent memory](agent-memory.md#measured).

<a id="how-the-plugin-is-designed-to-save"></a>

## Cómo está diseñado el plugin para ahorrar

- **Respuestas del índice en lugar de lecturas de archivos.** `code.symbol`, `code.callers` y `code.search`
  devuelven las líneas que responden una pregunta sobre la estructura. [Índice de código](code-intelligence.md)
- **Contexto guardado en lugar de redescubrimiento.** Las decisiones y los hallazgos que guarda una sesión se
  cargan al inicio de la sesión siguiente. [Memoria compartida](agent-memory.md)

Loom también registra lo que gasta en llamadas de embedding y de LLM por operación.
[Costo y uso](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
