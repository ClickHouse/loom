<!-- i18n: source=docs/features/entity-graph.md blob=f2344ba078b13ea0d97951af2212b5da3b9af8bb model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md), which is authoritative.

<a id="entity-graph"></a>

# Grafo de entidades

Loom mantiene un grafo de entidades junto a las filas de memoria, en la misma base de datos de ClickHouse. Las entidades son nodos. Dos entidades mencionadas en la misma memoria comparten una arista de coocurrencia; la extracción agrega tripletas tipadas `(subject, relation, object)` como un segundo conjunto de aristas. El grafo es una proyección de la memoria: cada arista se puede rastrear hasta las filas que la produjeron. Úsalo para preguntas que la búsqueda vectorial no puede responder, como "qué está conectado con el servicio de facturación".

Las comunidades de entidades están desactivadas por defecto (`LOOM_ENTITY_COMMUNITIES_ENABLED`).

<a id="what-the-graph-holds"></a>

## Qué contiene el grafo

| Tabla                                                                    | Contiene                                                                                                                                                         |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.entity_edges` (+ `entity_edge_memories`, `entity_edge_rows`)       | Una fila por observación de coocurrencia (`co_occur = 1`); los lectores suman por par. Clave de origen por par, y la fila y el valor que lo nombraron.           |
| `loom.entity_triples` (+ `entity_triple_memories`, `entity_triple_rows`) | Tripletas tipadas con `confidence` y el modelo extractor como `adapter_name`. Clave de origen por tripleta, y la fila y el valor que la enunciaron.              |
| `loom.entity_degree`                                                     | El grado total de coocurrencia de cada entidad (`SummingMergeTree`); las reglas de hubs lo leen.                                                                 |
| `loom.entity_canonical_map`                                              | Texto de la mención → entidad canónica.                                                                                                                          |
| `loom.entity_communities`                                                | Id de comunidad por entidad, por nivel.                                                                                                                          |
| `loom.entity_vectors`, `loom.entity_summaries`                           | Embeddings que leen las ramas de entidades de la búsqueda.                                                                                                       |
| `loom.derived_facts`                                                     | Hechos numéricos con unidad y fecha, para respuestas calculadas en la búsqueda.                                                                                  |
| Aristas `memory.link`                                                    | Aristas que afirma un agente o una persona (`supports`, `depends_on`, `caused`, …); las lee la búsqueda en modo grafo.                                           |

Las aristas y las tripletas llevan `valid_from` / `valid_to` (tomados de `valid_at` / `valid_to` de la memoria de
origen) y una marca `invalid_at` que las retira. Una lectura con `as_of` suma solo las observaciones válidas en ese
instante.

El grafo vive en ClickHouse para que las aristas se escriban en la misma solicitud que su fila, se borren con la misma
saga y se unan con la memoria sin un salto de red. El costo: un recorrido es una serie de consultas agregadas
acotadas, por lo que todo recorrido tiene un límite.

<a id="how-the-graph-is-built"></a>

## Cómo se construye el grafo

Primero se escribe la fila de memoria; las filas del grafo son proyecciones de ella. Una proyección fallida se registra en
`loom.memory_projection_failures` y se reintenta, solo mientras la fila conserve el valor que no se pudo proyectar.
Nunca hace fallar la escritura. Una entrada masiva de sensibilidad `high` no proyecta nada; un `memory.set` de
sensibilidad `high` proyecta únicamente las aristas de las entidades que declaró su escritor, nunca su texto.

1. **Entidades.** El escritor pasa `entities` en `memory.set`, o `memory.set_from_messages` las extrae. El worker
   `entity_extractor` (`LOOM_ENTITY_EXTRACT_ENABLED`, activado) etiqueta las filas escritas sin ellas, fuera de la ruta de
   escritura; necesita un proveedor de LLM real. El texto de las entidades se canonicaliza de la misma forma al escribir y
   al leer (se eliminan artículos y posesivos, el sustantivo principal se pasa a singular y la primera persona se asigna a
   `user`), de modo que "The Billing Service's" y "billing service" son un mismo nodo. Un nombre `commit:<sha>` o
   `error:<name>` es un identificador: se canonicaliza igual, pero nunca se pasa a singular: `error:Timeouts`, o el mismo
   nombre entre comillas invertidas o comillas, es el nodo `error:timeouts`. Un espacio en blanco o marcado justo después
   de los dos puntos (`Error: the build failed`, ``error:`Timeouts` ``) lo convierte en prosa. Se descartan los
   pronombres, las interjecciones, los verbos ligeros y los tokens de dos caracteres, y también el texto de más de 256
   caracteres, tal como está escrito o una vez normalizado (NFKC convierte `ﬁ` en `fi`), tanto al escribir como en un
   `entity_filter`; una escritura puntual rechaza un sujeto u objeto de hecho de ese tipo en lugar de descartarlo.
2. **Aristas.** Cada par de entidades de una memoria se convierte en una fila de observación, con un máximo de 100 pares
   por memoria.
3. **Tripletas.** Las relaciones de un hecho extraído se convierten en tripletas, con un máximo de 8 por memoria. Los
   verbos se normalizan a `lower_snake`. `namespace.register_ontology` (admin) registra tipos de entidad, verbos y
   atributos preferidos con una cardinalidad. Los nombres fuera del conjunto generan `ontology_warnings`; no se rechaza
   nada.
4. **La sustitución cierra tripletas.** Cuando una fila sustituye a otra, las tripletas abiertas de la fila antigua que la
   nueva no confirma reciben `invalid_at`. En una relación `one` (empleador, ubicación), el valor más reciente sigue
   vigente y el anterior se cierra cuando empieza el más reciente. Una escritura que vuelve a declarar el valor anterior
   mientras se ejecuta ese cierre lo mantiene abierto.
   - Los valores se ordenan por el día en que empiezan (un valor sin fecha empieza cuando llega) y luego por su llegada
     al microsegundo, de modo que los escritores concurrentes coinciden en un único valor vigente. Dentro de una misma
     escritura, el valor declarado después es el más reciente.
   - Dentro de una misma escritura, un valor sin fecha en tiempo pasado (`lived_in`, `worked_at`) es más antiguo que uno
     con fecha, en cualquier orden: "user lived_in london" junto a Paris desde el 1 de octubre deja a Paris como vigente.
   - Un valor se juzga por su fila más reciente: "London desde el 15 de octubre, Paris desde el 14 de octubre, London desde
     el 13 de octubre" deja a London como vigente.
   - Un valor declarado con su propio final ("London hasta diciembre") nunca se cierra antes de tiempo. Sigue vigente junto
     al valor más reciente hasta ese final.
   - Un Undo (`memory.restore`) hace vigente el valor restaurado desde la restauración, o desde el inicio posterior del
     propio hecho ("mudándose a Berlín el 1 de diciembre"). El otro valor se cierra y su fila sigue activa. Un valor
     fechado en un día posterior sigue prevaleciendo, y también un valor declarado después del Undo. Deshacer y rehacer
     repetidamente no añade peso.
   - Un cierre fallido se reproduce tal como estaba en la escritura o el Undo que lo originó, de modo que un valor
     declarado desde entonces sigue vigente.

   En la [arquitectura de la memoria](../architecture/memory-architecture.md#undo) se explica cómo un Undo vuelve a
   declarar sus hechos y aristas, los pondera y se reproduce.

Una escritura puntual (`memory.set`, `memory.update`, `memory.update_by_id`) escribe las tripletas que el llamador pasa en
`relations`, con un máximo de 8, por HTTP y en las operaciones tipadas de los SDK (no en el listado de MCP); no extrae
ninguna. La extracción automática se ejecuta en la ruta masiva: `memory.set_from_messages`, o un escritor masivo que
proporcione relaciones. Con los proveedores simulados de serie, solo las entidades que pasas forman aristas.

<a id="operations"></a>

## Operaciones

| Operación                      | Rol    | Responde                                                                                                      |
| ------------------------------ | ------ | ------------------------------------------------------------------------------------------------------------- |
| `facts.list`                   | viewer | Tripletas filtradas por `subject`, `object`, `relation` (AND) o `entity` (cualquiera de los dos extremos).   |
| `facts.related`                | viewer | Entidades que coinciden con una entidad, de mayor a menor fuerza, e indica de qué lado está la semilla.      |
| `facts.traverse`               | viewer | El vecindario a N saltos de una semilla.                                                                      |
| `facts.path`                   | viewer | La cadena más corta de aristas entre dos entidades dentro de `max_hops`.                                      |
| `facts.expire_by_adapter`      | admin  | Marca como eliminadas (tombstone) las tripletas que escribió un extractor. Las memorias permanecen.          |
| `memory.search` (modo grafo)   | viewer | `search_mode="graph"`: resultados a un salto de las entidades de la consulta ([búsqueda y ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)). |
| `memory.related`               | viewer | Memorias que comparten entidades con una memoria dada.                                                        |
| `memory.timeline`              | viewer | Las memorias de una entidad en orden cronológico (`entity=…`).                                                |
| `memory.backlinks`             | viewer | Memorias que comparten las entidades de una fila.                                                             |
| `namespace.taxonomy`           | viewer | Las entidades principales de un espacio de nombres y, cuando se calculan, sus comunidades.                    |

Listas de campos: [hechos](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/facts.md), [memoria](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/memory.md). Fuera del modo
grafo, `entity_filter` da más peso a las filas sobre las entidades indicadas, y los tramos de entidades de la búsqueda encuentran filas sobre una entidad que la
consulta nombra con otras palabras ([recuperación y ranking](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)).

<a id="factslist"></a>

### `facts.list`

Sin filtros, devuelve las tripletas principales por `co_occur`; `sort_by="recency"` ordena por `last_seen`.

| Campo                   | Efecto                                                                                               |
| ----------------------- | ---------------------------------------------------------------------------------------------------- |
| `include_sources`       | Agrega hasta unas diez claves de memorias de origen por tripleta.                                    |
| `include_source_status` | Lee cada fila de origen; marca una tripleta como `stale` cuando está abierta pero todas las filas que la respaldan han sido reemplazadas. |
| `provenance`            | `extracted` (ambos extremos en el texto de origen), `inferred` o `facet` (una arista de navegación creada por Loom). |
| `as_of`                 | Conserva las tripletas válidas en ese instante.                                                      |

<a id="factstraverse"></a>

### `facts.traverse`

El recorrido poda antes de expandir. Cada salto envía la frontera como una sola consulta, suma las observaciones por par dentro de la
ventana de validez, descarta las entidades ya visitadas y conserva los `top_k_per_hop` pares más fuertes de cada padre. Una frontera de salto h
tiene como máximo K^h entidades. Cada entidad se informa una sola vez, en el primer salto que la alcanza, a través de su padre
más fuerte, por lo que el resultado se dibuja como un árbol.

Una consulta recursiva que expande todo y poda después materializaría todo el vecindario; en un espacio de nombres grande, un recorrido de tres saltos
puede superar el límite de memoria de ClickHouse. Podar en cada salto acota el costo al tamaño de la respuesta.

| Campo                   | Valor por defecto                        | Efecto                                                                                                         |
| ----------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `max_hops`              | 2 (1–4)                                  | Profundidad del recorrido.                                                                                     |
| `top_k_per_hop`         | 20 (hasta 100)                           | Hijos conservados por padre en cada salto.                                                                     |
| `min_co_occur`          | 1                                        | Mínimo de observaciones por par.                                                                               |
| `max_nodes`             | 400 (hasta 5,000)                        | Límite estricto; `truncated=true` cuando se alcanza.                                                           |
| `as_of`                 | sin definir                              | Conserva las observaciones válidas en ese instante.                                                            |
| `labeled` / `relations` | false / sin definir                      | Recorre tripletas tipadas (todas, o solo estos verbos); los nodos incluyen `via_relation`, `via_direction`, `via_provenance`. |
| `hub_degree`            | 0 (desactivado)                          | Un nodo con grado total igual o mayor que este valor es un hub.                                                |
| `hub_share`             | `LOOM_FACTS_TRAVERSE_HUB_SHARE` (0.01)   | Un nodo es un hub cuando la arista que llegó a él representa como máximo esta proporción de su grado.          |
| `rank_by`               | `LOOM_FACTS_TRAVERSE_RANK_BY` (`cosine`) | Poda por salto: `co_occur`, `specificity` (`via_co_occur / degree`) o `cosine` (`via_co_occur / sqrt(degree)`). |

Cada nodo informa `hop`, `cumulative_weight`, `via_co_occur`, `path` (la cadena desde la semilla), `valid_from` /
`valid_to`, `degree` y `community_id` cuando existen comunidades. Los nodos se ordenan por salto y luego por peso.

**Hubs.** Las entidades genéricas ("user", "pr") coinciden con todo. Un hub se informa (`hub: true`, listado en `hubs`)
pero no se expande. La semilla siempre se expande, así que parte de un hub para ver su vecindario. `hub_share` es la regla
por defecto porque no depende de la escala de grados del espacio de nombres. `hub_degree` es específico de cada espacio de nombres; revisa la cola
de la distribución de grados antes de definirlo.

<a id="factspath"></a>

### `facts.path`

Una búsqueda en anchura bidireccional acotada sobre las mismas aristas, que se encuentra en el medio. `max_hops` (1–4, por defecto 3)
acota la longitud total. La respuesta es `found`, `path` y `edges`, con el `co_occur` y la validez de cada par.
`truncated=true` significa que la respuesta puede estar incompleta. `found=false` con `truncated=false` significa que no hay conexión dentro de
`max_hops`.

<a id="example"></a>

## Ejemplo

```python
from loom import Agent

async with Agent("dev", "local", "sre-bot", base_url="http://localhost:7777") as agent:
    await agent.memory.set(
        "billing/depends_on",
        "The billing service reads invoices from the ledger database.",
        entities=["billing service", "ledger database"],
    )
    walk = await agent.facts.traverse("billing service", max_hops=2, top_k_per_hop=10)
    for node in walk["nodes"]:
        print(node["hop"], " > ".join(node["path"]), node["via_co_occur"])

    route = await agent.facts.path("billing service", "ledger outage")
    print(route["found"], route["path"], route["truncated"])
```

Sobre HTTP, un recorrido del grafo tal como estaba en un instante pasado:

```bash
curl -s localhost:7777/v1/facts.traverse -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"org":"dev","namespace":"local","seed_entity":"billing service","max_hops":2,"as_of":"'"$AS_OF"'"}'
```

Sobre MCP, llama a `loom_op` con `op="facts.traverse"`.

<a id="maintaining-the-graph"></a>

## Mantenimiento del grafo

| Tarea                                             | Cómo                                                                                                                                       |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Revertir las tripletas de un extractor defectuoso | `facts.expire_by_adapter(adapter_name=…, max_confidence=…)`. Las memorias se conservan.                                                    |
| Exportar el grafo                                 | `loom entity-export --org O --namespace N --format graphml` (o `jsonld`; `--all` incluye las aristas invalidadas).                         |
| Agrupar entidades en comunidades                  | Define `LOOM_ENTITY_COMMUNITIES_ENABLED=true` (más abajo).                                                                                 |
| Verlo                                             | La vista Graph del dashboard ([panel](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)).                           |

El worker `entity_communities` ejecuta propagación de etiquetas por espacio de nombres sobre `loom.entity_communities`, que leen `namespace.taxonomy`, la expansión en modo grafo y `community_id`. Cubre los espacios de nombres con la consolidación activada, además de los recorridos en los últimos `LOOM_ENTITY_COMMUNITIES_WALKED_DAYS` (14) días. Descarta primero las entidades de mayor grado (`LOOM_ENTITY_COMMUNITIES_HUB_DROP_FRACTION`) y duplica esa fracción mientras una comunidad contenga más de `LOOM_ENTITY_COMMUNITIES_GIANT_SHARE` de las entidades, de modo que los tokens genéricos no puedan fusionar el grafo en una sola comunidad.

<a id="settings"></a>

## Configuración

| Configuración                                | Sección                                                                                                                      |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_FACTS_TRAVERSE_HUB_SHARE`, `…_RANK_BY` | [Salud del espacio de nombres, grafo de conocimiento, comunidades](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#namespace-health-knowledge-graph-communities) |
| `LOOM_ENTITY_COMMUNITIES_*`                  | La misma sección.                                                                                                            |
| `LOOM_ENTITY_EXTRACT_*`                      | [Consolidación](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)                                                                 |
| `LOOM_MEMORY_SEARCH_GRAPH_SEED`              | [Búsqueda de memoria](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)                                                                   |

<a id="limits"></a>

## Límites

- **La co-ocurrencia no es una relación.** Dos entidades en una misma memoria quedan vinculadas sin importar lo que diga la oración. Usa triples tipados (`labeled`, `relations`) o `memory.link` cuando la relación importe.
- **El grafo solo es tan bueno como sus entidades.** Las filas sin entidades no añaden aristas hasta que el worker `entity_extractor` las procesa.
- **Los recorridos están acotados.** Lee `truncated` antes de concluir que dos entidades no están conectadas.
- **Las aristas pueden quedar rezagadas respecto de una fila reemplazada.** `facts.list(include_source_status=true)` muestra los triples que siguen abiertos.
- **La validez es tiempo de validez.** `as_of` lee cuándo se cumplía un hecho, no cuándo Loom lo supo.

<a id="related"></a>

## Relacionado

- [Búsqueda y consulta](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md): modo grafo.
- [Escritura de memorias](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md): cómo pasar `entities`.
- [Extracción](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md): de dónde provienen las entidades y los tripletes extraídos.
- [Arquitectura de memoria](../architecture/memory-architecture.md): los escritores de proyecciones.
- [Referencia de workers](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md).
