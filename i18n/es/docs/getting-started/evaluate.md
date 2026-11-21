<!-- i18n: source=docs/getting-started/evaluate.md blob=882e7d370aa195573ce2e4029995fa1f32beadc9 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md), which is authoritative.

<a id="evaluate-loom"></a>

# Evaluar Loom

Evalúa la decisión que necesitas tomar, no cada harness del repositorio. Esta guía
comienza con una prueba de humo del producto sin costo y luego pasa a una prueba de calidad
específica para tu carga de trabajo. La suite pública de benchmarks es el paso final cuando necesitas
auditar o reproducir un resultado publicado.

<a id="choose-an-evaluation"></a>

## Elegir una evaluación

| Decisión                                                        | Datos                                                                         | Proveedores                                                       | Resultado típico                                                                                                  |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| ¿Loom se instala y se conecta correctamente?                    | Cuatro hechos de prueba y un documento                                        | Bastan los proveedores simulados incluidos                        | Un hecho sobrevive a una sesión nueva; las filas de fuente, historial y auditoría aparecen en el dashboard        |
| ¿Es útil la memoria para nuestro agente?                        | 25–50 preguntas reales con las claves de memoria esperadas                    | Embedder real; LLM real para la extracción o `memory.ask`         | recall@k, rank@1, abstención, precisión de las respuestas, latencia y costo                                       |
| ¿Es útil la inteligencia de código para nuestro repositorio?    | Símbolos representativos, conjuntos de llamadores y preguntas con forma de issue | Embedder real; repositorio indexado                            | definiciones exactas, F1 de llamadores, recall de archivos, abstención, tokens y latencia                         |
| ¿Podemos reproducir un resultado publicado?                     | El dataset público indicado y el artefacto de resultados confirmado en el repositorio | Los modelos fijados o sustitutos documentados            | Una ejecución comparable con el mismo subconjunto, lector, juez y modo de búsqueda                                |

No uses el embedder simulado (el `.env` de un checkout) ni el LLM simulado para juzgar la calidad: existen para que el stack pueda arrancar sin credenciales y para que las pruebas de integración sean deterministas. El embedder local del stack publicado recupera de verdad, así que una primera revisión de la búsqueda con él es significativa; juzga las respuestas y la extracción con un LLM real.

<a id="before-you-run"></a>

## Antes de ejecutar

Anota primero estas cinco cosas:

1. **Carga de trabajo.** Los usuarios, repositorios y tipos de preguntas que cubre la decisión.
2. **Línea base.** Lo que hace el mismo usuario o agente sin Loom: contexto completo, grep, BM25,
   otro sistema de memoria o ninguna recuperación.
3. **Umbrales de precisión.** El recall, la exactitud de las respuestas o el éxito de las tareas mínimos que
   aceptarás. La calidad va antes que el costo.
4. **Umbrales de economía.** Latencia p95 máxima, tokens, costo del modelo, tiempo de indexación y almacenamiento.
5. **Instrumento.** Commit de Loom, despliegue de ClickHouse, modelos del proveedor, parámetros del modelo,
   hardware, versión del conjunto de datos y semilla aleatoria.

Registrar los umbrales de antemano evita que una métrica o un subconjunto convenientes se conviertan en el resultado
después de la ejecución. Mantén visibles los fallos y las abstenciones; informa de los errores de solicitud por separado
en lugar de contarlos o descartarlos en silencio como fallos de recuperación.

<a id="1-product-smoke-test"></a>

## 1. Prueba de humo del producto

Esto toma unos 15 minutos y comprueba la conexión entre componentes, la persistencia y la visibilidad, no la calidad semántica.

1. [Levanta el stack local](quickstart.md#1-boot-the-stack) y confirma que `/v1/health` informa
   `status: ok`.
2. [Escribe y busca un hecho](quickstart.md#4-first-memory-over-http).
3. [Conecta un agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md), dile una preferencia duradera, abre
   una sesión nueva y pregunta por esa preferencia.
4. Corrige el hecho y luego confirma que el dashboard muestra su fuente y su historial de versiones.
5. Ejecuta `loom demo` con un documento y confirma que los fragmentos devueltos citan ese documento
   ([prueba de humo de documentos](quickstart.md#optional-ask-a-question-about-a-document)).

La prueba se considera superada solo cuando la sesión nueva recuerda el hecho, la corrección reemplaza la respuesta actual, una pregunta no relacionada se abstiene y el dashboard muestra al autor de la escritura y el historial.
El resultado no dice nada sobre la recuperación en producción mientras el health informe `fake_embedding` o
`fake_llm`.

<a id="2-evaluate-your-workload"></a>

## 2. Evalúa tu carga de trabajo

Configura [proveedores reales](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers), restablece o vuelve a generar los embeddings de los datos
si cambió el ancho del vector, y registra los modelos resueltos desde `/v1/health`. Usa un
espacio de nombres aislado y conserva los mismos datos indexados para cada rama emparejada.

<a id="memory-and-rag"></a>

### Memoria y RAG

Construye un conjunto de al menos 25–50 preguntas a partir de tareas reales. Incluye:

- hechos directos, paráfrasis, preguntas de varios saltos y actualizaciones sensibles al tiempo;
- preguntas cuya respuesta debería haber sido reemplazada;
- preguntas verosímiles pero sin respuesta, para medir respuestas falsas y abstención;
- distractores casi duplicados que difieren en el valor que importa.

Para cada pregunta de memoria con respuesta, etiqueta el `memory_key` esperado antes de ejecutar. Para
RAG de documentos, etiqueta el documento y el fragmento esperados, consulta `knowledge.search` y puntúa
esos identificadores con las mismas métricas de recuperación. Mide la recuperación y la síntesis
por separado:

- **Recuperación:** recall@k, rank@1, MRR, tasa de falsos positivos y precisión/recall de abstención.
- **Respuesta:** corrección con un único lector y juez fijados, más la corrección de las citas.
- **Economía:** latencia p50/p95, tokens de entrada/salida, costo del modelo, costo de construcción y
  filas almacenadas.

Para un corpus de memoria existente, guarda un conjunto de datos JSON con esta forma (el arnés incluido
apunta a `memory.search`; usa un evaluador equivalente para `knowledge.search`):

```json
{
  "org": "acme",
  "namespace": "support-eval",
  "questions": [{ "query": "When can we deploy?", "gold_keys": ["deploy_window"] }]
}
```

Luego ejecuta la recuperación sin modificar el corpus:

```bash
export PYTHONPATH=$PWD LOOM_API_KEY=<token>
.venv/bin/python bench/retrieval_recall.py \
  --dataset /path/to/retrieval_qa.json --base-url http://localhost:7777 \
  --top-k 10 --out /tmp/loom-retrieval.csv
```

Define `--min-recall` y `--min-mrr` solo a partir de tus umbrales registrados de antemano. Usa
`memory.ask` o tu lector de producción con las mismas preguntas para medir la exactitud de las respuestas; el recall
y la exactitud de las respuestas son mediciones distintas.

<a id="code-intelligence"></a>

### Inteligencia de código

Indexa un repositorio que se parezca a tus repositorios de producción. No empieces solo con
el repositorio con el que se ajustó Loom. Etiqueta:

- definiciones con el archivo esperado y el símbolo calificado;
- llamadores con el conjunto completo esperado de llamadores;
- preguntas conceptuales y con forma de issue con archivos aceptables en el top 5 y el top 10;
- preguntas fuera del repositorio que deberían provocar abstención;
- las pruebas o dependientes esperados de una consulta de impacto.

Ejecuta el informe de aceptación del agente de código contra un repositorio indexado:

```bash
export PYTHONPATH=$PWD
.venv/bin/python bench/acceptance/code_agent.py \
  --base-url http://localhost:7777 --token-file /path/to/token \
  --namespace <code-namespace>
```

El fixture integrado apunta a ClickHouse y es útil para verificar un despliegue. Para una
decisión de producto, reemplázalo o amplíalo con tus preguntas etiquetadas y compara las mismas
tareas del agente usando solo herramientas de repositorio ordinarias. Informa primero el éxito de la tarea, luego los turnos,
tokens, costo y tiempo real. La sobrecarga del esquema de herramientas depende del cliente, así que registra si
difiere las definiciones de herramientas MCP.

<a id="keep-the-comparison-fair"></a>

### Mantén la comparación justa

- Usa el mismo modelo, prompt, instantánea de datos y orden de preguntas en las ramas emparejadas.
- Mantén el costo de extracción/construcción del índice separado del costo por pregunta, y luego indica el
  número de preguntas necesarias para amortizar la construcción.
- Desactiva el aprendizaje a partir de las lecturas de evaluación (`auto_touch: false`, `reinforce: false`) para que una
  consulta temprana no pueda mejorar una posterior.
- Informa el tamaño de la muestra, los resultados por categoría y la incertidumbre. No conviertas una pequeña
  prueba de humo en una afirmación general.
- Guarda las salidas por pregunta, incluidos los errores y los fallos, no solo las puntuaciones agregadas.

<a id="3-audit-or-reproduce-published-results"></a>

## 3. Auditar o reproducir los resultados publicados

El README es un cuadro de resultados, no el registro de evidencia. Para cada número publicado:

1. Abre la sección correspondiente en [Resultados de benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md).
2. Inspecciona su artefacto confirmado en [`bench/results/`](https://github.com/ClickHouse/loom/blob/main/bench/results).
3. Confirma la porción del conjunto de datos, el commit de Loom, los modelos, el lector, el juez, el modo de búsqueda y la semilla.
4. Ejecuta el comando de reproducción exacto de la sección del resultado.
5. Interpreta la diferencia con la [metodología](../benchmarks/methodology.md), incluyendo la calibración del juez, los intervalos bootstrap y los sesgos conocidos.

Este repositorio no redistribuye todos los conjuntos de datos de terceros. Su origen y licencia se indican en [conjuntos de datos](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets). Las ejecuciones públicas completas pueden tardar horas y generar cargos del proveedor; empieza con la porción de prueba rápida documentada y luego ejecuta el protocolo completo antes de publicar una comparación.

<a id="what-to-publish"></a>

## Qué publicar

Publica un resultado en el README solo cuando sea representativo, se entienda en una fila y esté respaldado por un artefacto confirmado en el repositorio y un comando de reproducción. Incluye la tabla completa de categorías, los modelos, los prompts, la incertidumbre, los costos, el análisis de fallos y las limitaciones en el
[registro de resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md). Incluye las reglas de evaluación estables en la
[metodología](../benchmarks/methodology.md), y los comandos y la obtención de los datasets en
[ejecución](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md).

Toda afirmación pública debe indicar:

- la tarea y el tamaño de la muestra;
- la métrica y si mide recuperación, calidad de la respuesta o éxito de extremo a extremo;
- la línea base y la diferencia pareada, cuando exista;
- los modelos y el juez, o que no se usó ningún juez LLM;
- la incertidumbre o la granularidad del cambio de una sola pregunta;
- el costo de construcción y el de consulta por separado;
- el artefacto confirmado en el repositorio y el comando de reproducción exacto;
- los hallazgos negativos relevantes y dónde el resultado no se generaliza.

No publiques en el README todas las cifras disponibles. Un marcador largo oculta la decisión sobre el producto, fomenta comparaciones inválidas entre benchmarks y dificulta detectar afirmaciones obsoletas. El README debe llevar al lector hacia la evidencia; la documentación debe contenerla.

<a id="next"></a>

## Siguiente

- [Cobertura de benchmarks](../benchmarks/coverage.md): qué tiene una medición pública, qué tiene solo un harness y qué aún no tiene evidencia pública.
- [Resumen de benchmarks](../benchmarks/overview.md): elige un harness según la señal que mide.
- [Ejecución de benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md): proveedores, tokens, comandos y conjuntos de datos.
- [Metodología de benchmarks](../benchmarks/methodology.md): reglas de comparación y limitaciones.
- [Resultados de benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md): mediciones y artefactos incluidos en el repositorio.
