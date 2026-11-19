<!-- i18n: source=docs/getting-started/overview.md blob=6fbe75840c341cccfe02d72afa8d5bbb4dfb7809 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md), which is authoritative.

<a id="overview"></a>

# Descripción general

Loom es un servidor que almacena memoria, documentos, conversaciones, un grafo de entidades y un índice de código para agentes de IA, como filas en ClickHouse, con
[Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) como opción para el estado de memoria y conocimiento en vivo.
Los agentes y las personas acceden a él mediante MCP, HTTP, los SDK de Python y TypeScript, y el
panel. Tiene licencia Apache-2.0. Esta página te ayuda a decidir si encaja con tus necesidades.

<a id="what-it-does"></a>

## Qué hace

- **Memoria que perdura.** Los hechos que un agente guarda siguen disponibles en sesiones posteriores y en otras
  herramientas, una afirmación por clave, con historial, procedencia y validez. Puede ser la memoria de una sola persona
  o estar compartida por muchos agentes y personas.
- **Comprobaciones al escribir.** Un duplicado casi idéntico se fusiona, una contradicción se marca y una escritura
  contra un hecho fijado se rechaza.
- **Búsqueda que se abstiene.** `memory.search` devuelve `abstain=true` con un código de motivo
  cuando ningún resultado supera un umbral calibrado para tu embedder.
- **Auditoría y borrado.** Cada escritura que cambia la memoria deja una fila de auditoría con hash del contenido.
  `erasure.request` termina con un censo de residuos sobre todas las tablas que usan las rutas de lectura.
- **Un índice de código con su historial.** Búsqueda, símbolos, llamadores, impacto, historial de PR con
  comentarios de revisión y candidatos de bisect para tus repositorios, y cada respuesta indica el commit y el nivel del índice
  en que se basa. Con una fuente de seguimiento conectada, también el gestor de incidencias:
  comprobación de duplicados, vínculos incidencia↔PR y tests inestables.

La memoria y el índice de código son planos separados en un mismo servidor. Usa uno solo o ambos.
La memoria necesita un servidor y un token de escritor. El índice de código necesita además que se haya añadido un repositorio
(`LOOM_CODE_REPOS_JSON`, o **Repositories → Add repository** en el panel donde se ejecuta el
[sandbox de trabajos de repositorio](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox)) y
`LOOM_CODE_INGEST_ENABLED=true`. Los [plugins de Claude Code y Codex](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
usan ambos.

<a id="where-namespaces-fit"></a>

## Dónde encajan los espacios de nombres

- Una **org** es el inquilino (tenant).
- Un **espacio de nombres** es la unidad de uso compartido y de acceso dentro de ella. Un despliegue suele tener
  uno por equipo o proyecto, uno por cada bot que deba permanecer aislado, uno `personal-<slug>`
  por persona (se crea en el primer uso y es el valor predeterminado para los clientes MCP de esa persona), y uno `code-<repo>[-<branch>]` por cada rama indexada.
- Todos los miembros de un espacio de nombres leen todas las filas que contiene.
- `agent` registra quién escribió una fila; no es un límite.
- `user_id` / `read_scope=own_user` separa a los usuarios finales dentro del espacio de nombres de un bot.
- El acceso es un rol (viewer/writer/admin) sobre un espacio de nombres, que lleva un token o se añade mediante una
  concesión (grant).

[Identidades y espacios de nombres](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) contiene el modelo completo.

<a id="when-loom-fits"></a>

## Cuándo encaja Loom

- Usas varias herramientas de IA y quieres que compartan lo que le dijiste a cualquiera de ellas, en un
  espacio de nombres personal.
- Varias herramientas y personas (Claude Code, Codex, Cursor, un bot de soporte, una persona en el
  panel) necesitan la misma memoria.
- Quieres SQL sobre lo que saben tus agentes: los hechos, las trazas, los costos y las filas de auditoría son
  tablas de ClickHouse.
- Ya ejecutas ClickHouse, o puedes hacerlo. Es el único almacén requerido de Loom;
  [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) es una opción para la memoria activa y el estado
  del conocimiento.
- Es mejor una abstención o un conflicto señalado que una respuesta incorrecta pero segura.
- Necesitas historial, procedencia, auditoría o borrado verificable.
- Los agentes de programación necesitan un índice de tus repositorios.

<a id="when-it-does-not-fit"></a>

## Cuándo no encaja

| Situación                                                              | Por qué no                                                              | Alternativa                                        |
| ---------------------------------------------------------------------- | ----------------------------------------------------------------------- | -------------------------------------------------- |
| Un agente, un proceso, memoria de corta duración                       | Un servidor y una base de datos son sobrecarga cuando no se comparte nada | Una biblioteca de memoria dentro del proceso       |
| Búsquedas por clave de menos de un milisegundo en una ruta crítica     | Cada lectura es una consulta a la base de datos                         | Una caché delante de tu propio almacén             |
| Analítica sobre el grafo completo o rutas largas                       | El recorrido es una consulta acotada por salto, con un máximo de cuatro saltos | Un almacén de grafos, alimentado con `loom entity-export` |
| Necesitas un framework de agentes                                      | Loom no ejecuta el bucle del agente, los prompts ni el modelo           | Un framework de agentes, con Loom como su memoria  |
| Una garantía de cumplimiento normativo lista para usar                 | El borrado y la auditoría son primitivas de ingeniería                  | Tu proceso de cumplimiento, usando esas primitivas |
| Producción en ClickHouse OSS de un solo nodo sin plan de copias de seguridad | No hay replicación ni recuperación en un punto en el tiempo       | ClickHouse Cloud                                   |
| APIs congeladas                                                        | Las operaciones y los campos cambian entre versiones                    | Fija una versión; consulta `ops.capabilities`      |

La validez es solo tiempo de validez (`valid_at`, `valid_to`); lo que Loom registró y cuándo
se guarda en el historial de versiones, no en un segundo eje temporal. Algunas partes requieren que un operador las
active: el índice de código, las fuentes de trackers, OAuth, la exportación de telemetría y el borrado en Langfuse,
ClickStack.

<a id="what-each-property-costs"></a>

## Lo que cuesta cada propiedad

| Propiedad                                  | Mecanismo                                                                                                         | Costo                                                                                                                                              |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Un solo motor (ClickHouse como autoridad)  | Vectores, búsqueda léxica, grafo, filtros, auditoría y borrado son SQL sobre las mismas filas                     | Cada lectura es una consulta. Los lectores pagan `FINAL` en las filas versionadas. Las filas derivadas (entidades, resúmenes) llegan después, a través de workers. |
| Abstenerse en lugar de dar una coincidencia débil | Umbrales derivados al arrancar a partir de la distribución de los valores de similitud coseno del propio embedder (`LOOM_ADAPTIVE_THRESHOLDS`) | Se rechazan algunas coincidencias débiles pero correctas; define `min_similarity` o `floor_mode` por solicitud.                                    |
| Hechos que cambian                         | Veredictos de fusión, duplicado, conflicto y fijado en cada escritura; `loom.memory_versions` de solo anexado     | Cada escritura se compara con sus vecinos. Una paráfrasis puede esperar en la cola de revisión hasta que alguien la resuelva.                      |
| Ranking explicable                         | `memory.search` con `explain=true` detalla la puntuación de cada resultado; los resultados incluyen `source_class` y `verified` | Por encima de `top_k` 15, donde `explain` está desactivado por defecto, `explain=true` hace que cada resultado sea más grande.                     |
| Compartir con control de acceso            | La tupla de identidad en cada fila; los tokens y las concesiones llevan roles de espacio de nombres               | Tú emites y rotas los tokens y concedes los roles. Una suite de fugas entre principals protege cada op de lectura en CI.                           |
| Borrado verificable                        | Una saga reanudable que termina en un censo de residuos; los backends externos fallan en modo cerrado             | Un borrado es un trabajo de varios pasos. Un backend que no puede confirmar la eliminación deja la saga en `failed` para escalado manual.         |
| Respuestas de código con frescura          | Niveles de parser y compilador (SCIP); las respuestas incluyen `as_of.commit` y un indicador de obsolescencia     | Un repositorio grande tarda horas en indexarse. El nivel del compilador necesita la toolchain.                                                     |

Las afirmaciones de calidad te corresponde medirlas a ti. El repositorio incluye los arneses de benchmark y los resultados en bruto detrás de las cifras publicadas ([resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)). Ejecuta los gates en tu despliegue con proveedores reales antes de confiar en un valor por defecto ([ejecución](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="decision-checklist"></a>

## Lista de verificación para decidir

Dos o más respuestas en la columna de la derecha significan que Loom probablemente no es la herramienta adecuada.

| Pregunta                                                                                   | Apunta a Loom | Apunta a otra opción          |
| ------------------------------------------------------------------------------------------ | ------------- | ----------------------------- |
| ¿Dos o más herramientas o agentes necesitan compartir memoria?                             | Sí            | No, un solo proceso           |
| ¿Puede ejecutar ClickHouse y operar un servicio?                                           | Sí            | No, prefiere un servicio gestionado |
| ¿Los datos deben permanecer en su propia infraestructura?                                  | Sí            | Sin preferencia               |
| ¿Es mejor una abstención o un conflicto señalado que una respuesta incorrecta pero segura? | Sí            | Siempre devolver algo         |
| ¿Necesita historial, procedencia, auditoría o borrado verificable?                         | Sí            | No                            |
| ¿Su ruta crítica es una búsqueda por clave en menos de un milisegundo?                     | No            | Sí                            |
| ¿Necesita análisis de todo el grafo en lugar de unos pocos saltos?                         | No            | Sí                            |
| ¿Puede asumir cambios de API entre versiones, o fijar una versión?                         | Sí            | No                            |
| ¿Pagará por un embedder y un LLM reales, y hará benchmarks con sus datos?                  | Sí            | No                            |

<a id="prerequisites"></a>

## Requisitos previos

| Necesitas                                         | Para                                                                                                                                                                                                                                                           |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse (OSS para evaluar, Cloud en producción) | Todo. Consulta los [requisitos de ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#clickhouse-requirements).                                                                                                                                                                |
| Postgres 16+ (opcional)                           | Estado en vivo de la memoria y del conocimiento bajo la [autoridad de memoria en Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md), que se elige en el primer arranque de un despliegue; necesita un standby síncrono, salvo que `LOOM_PG_DURABILITY=local` ([requisitos](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#requirements)). |
| Docker con Compose 2.20 o posterior               | El stack local publicado (instalador de una línea; en Windows, [desde WSL 2](quickstart.md#fast-path-released-image-no-clone)) y la imagen del servidor.                                                                                                                   |
| Python 3.12+                                      | `loom up` solo para contribuidores, desarrollo del servidor y trabajo con la CLI de operador.                                                                                                                                                                  |
| Un proveedor de embeddings                        | Recuperación real: OpenAI, Gemini o el embedder local en CPU que usa el stack publicado.                                                                                                                                                                       |
| Un proveedor de LLM                               | Extracción, síntesis de `memory.ask`, `code.ask`: OpenAI o Anthropic.                                                                                                                                                                                          |
| Tokens y espacios de nombres                      | Acceso. Consulta [tokens y permisos](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).                                                                                                                                                                                               |
| Para el índice de código                          | Acceso Git a los repositorios, `LOOM_CODE_GITHUB_TOKEN` para el historial de PR, y tiempo para el primer índice.                                                                                                                                               |

<a id="next"></a>

## Siguiente

[Evaluar Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) distingue una prueba rápida del producto de una evaluación real de calidad. La [guía de inicio rápido](quickstart.md) ejecuta el stack local.
[Despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) explica cómo ejecutarlo para un equipo. El
[mapa de la documentación](https://github.com/ClickHouse/loom/blob/main/docs/index.md) enumera todas las páginas en orden de lectura.
