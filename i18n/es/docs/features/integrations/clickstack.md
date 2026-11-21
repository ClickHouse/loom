<!-- i18n: source=docs/features/integrations/clickstack.md blob=008f7906fbb2d60f24b7f99be4e0ee44382e64f9 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md), which is authoritative.

<a id="clickstack-and-hyperdx"></a>

# ClickStack y HyperDX

Loom siempre emite OpenTelemetry; las fuentes de HyperDX, los paneles y el paso de borrado necesitan configuración.

Loom emite un span por operación, además de logs y métricas. Los spans incluyen `loom.operation`,
`loom.org`, `loom.namespace`, `loom.agent`, `loom.request_id` y `loom.user_id` /
`enduser.id` cuando están presentes. Por defecto Loom los inserta directamente en las
tablas `default.otel_*` de su propio servicio de ClickHouse, con el esquema del collector, de modo que
HyperDX los lee sin necesidad de ejecutar ningún collector. Con `LOOM_CLICKSTACK_DATABASE` definida, las
tablas están en esa base de datos en lugar de `default`, aquí y en todas las consultas siguientes. La disposición de columnas está en
`schemas/otel/clickstack_otel_tables.sql`; si ClickStack cambia su esquema, ese archivo
debe actualizarse.

Los spans de llamadas a modelos incluyen los atributos GenAI de OpenTelemetry
([telemetría](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#traces-and-logs)) bajo las claves que la vista de LLM de HyperDX
lee para el proveedor, la operación, el uso de tokens, la proporción de caché de prompts y el costo.

<a id="choose-the-export-path"></a>

## Elegir la ruta de exportación

| `LOOM_OTEL_EXPORTER` | Comportamiento                                                                                                                                         |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ch`                 | inserta en `default.otel_traces`, `otel_logs`, `otel_metrics_sum`, `otel_metrics_histogram` y `otel_metrics_gauge` en el mismo servicio                |
| `otlp`               | envía a `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` (encabezados en `LOOM_OTEL_EXPORTER_OTLP_HEADERS`), para un collector u otro backend                        |
| vacío (predeterminado) | `otlp` cuando hay un endpoint configurado; de lo contrario, `ch`                                                                                     |

En el modo `ch`, Loom comprueba primero que `default.otel_traces` exista; si no existe, el
exportador se desactiva tras una línea de registro. Para crear las tablas:

```bash
clickhouse-client --multiquery < schemas/otel/clickstack_otel_tables.sql
```

`GET /v1/health?include=otel` muestra el exportador resuelto. El resto de la configuración de OTel
(intervalo de exportación, temporalidad, latido de gauge, nombre del servicio) está en
[telemetría](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#exporters).

<a id="point-hyperdx-at-the-tables"></a>

## Apuntar HyperDX a las tablas

Las filas ya están en el servicio, así que omite los pasos del colector y de la clave de ingestión.
Crea tres **fuentes** de HyperDX: trazas sobre `otel_traces`, logs sobre `otel_logs` y
métricas sobre `otel_metrics_*`. `deploy/clickstack_sources.sh` las crea a través de la
API de HyperDX y configura su correlación (ClickStack alojado se autentica con la cookie
de sesión de tu consola, `HDX_COOKIE`).
[`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md) incluye el
mapeo de campos de cada fuente.

<a id="dashboards-and-alerts"></a>

## Paneles y alertas

[`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md) es el recetario:

- **Loom — Memory Health**: tendencia de la calidad de recuperación (`loom.search.top_similarity`),
  llamadas y errores por operación y transporte, estado del sistema, gasto y tokens, latencia por
  plano de recuperación, resultados de escritura y paneles de solicitudes (tasa, latencia, errores, solicitudes
  más lentas, tráfico por tenant, logs de una traza).
- **Loom — Code Plane**: retraso del índice, retraso del nivel y los demás gauges `loom.code.*`. La definición
  JSON es `deploy/dashboards/code_plane.json`.
- El orden de arranque y las búsquedas guardadas.

Dos trampas al escribir tus propios tiles:

- **Los contadores son acumulativos.** A menos que `LOOM_OTEL_METRICS_DELTA_TEMPORALITY=true`, la fila de un
  contador es su total acumulado; una tasa necesita la diferencia entre puntos consecutivos,
  no una suma. Cambia el ajuste y los paneles a la vez.
- **Un resultado vacío se lee como 0 en HyperDX.** Una protección de muestra mínima en una alerta debe
  devolver un valor dentro del rango saludable, no cero filas, o la alerta se dispara por el silencio.

Los nombres de las métricas y los umbrales sugeridos están en el
[catálogo de métricas](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#metrics).

<a id="erasure"></a>

## Borrado

Requiere `LOOM_CLICKSTACK_DATABASE`. ClickStack no tiene una API de administración para borrar datos de usuarios, por lo que el backend (`ClickStackBackend` en `server/erasure/app/backends.py`) ejecuta sentencias `DELETE FROM` ligeras contra las tablas de OTel de esa base de datos:

| Tabla                                                                            | Columna de atributos consultada |
| -------------------------------------------------------------------------------- | ------------------------------- |
| `otel_logs`                                                                      | `LogAttributes`                 |
| `otel_traces`                                                                    | `SpanAttributes`                |
| `otel_metrics_gauge`, `_sum`, `_histogram`, `_exponential_histogram`, `_summary` | `Attributes`                    |
| `hyperdx_sessions`                                                               | `ResourceAttributes`            |

- **Un alcance de usuario** coincide con cualquiera de las claves de `LOOM_CLICKSTACK_USER_ID_KEYS` (por defecto `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id`), combinadas con OR, ya que los SDK no se ponen de acuerdo sobre el nombre de la clave. El span de la solicitud de Loom lleva `enduser.id`; sus spans de llamadas al modelo llevan `loom.user_id` y `langfuse.user.id`, y con Langfuse configurado también lo lleva cada otro span que abre la solicitud una vez resuelto su llamador. Cada uno de ellos lleva además `loom.org`, por lo que se aplica la protección de organización descrita más abajo.
- **Un alcance de espacio de nombres** coincide con `loom.namespace` o `service.namespace`. El exportador de Loom establece `service.namespace` con la constante `loom` y lleva el tenant en `loom.namespace`; `service.namespace` cubre a otros emisores que escriben en el mismo ClickStack, y su valor `loom` nunca se compara.
- **Protección de organización.** Una fila etiquetada con el `loom.org` de _otra_ organización nunca se borra. Una fila que coincide por `loom.namespace` debe llevar el `loom.org` de la organización que realiza el borrado. Las métricas de operaciones y de dominio de Loom (contadores e histogramas) llevan `loom.namespace` y ninguna organización, por lo que un borrado de espacio de nombres no las toca: no contienen contenido de memoria, pero su etiqueta `loom.namespace` es el nombre del espacio de nombres, que para un espacio de nombres personal se deriva del correo electrónico del usuario. Permanecen hasta que el TTL propio de la tabla del collector, si lo tiene, las elimina. Una fila sin etiqueta `loom.org` que coincide por una clave de usuario o por `service.namespace` (otro emisor) se borra, ya que podría contener al sujeto.

Un borrado ligero solo escribe la máscara de existencia de filas, de modo que las filas dejan de aparecer en cualquier lectura de inmediato y los bytes se eliminan en la siguiente fusión; `ALTER TABLE … DELETE` reescribiría cada parte afectada. El predicado recorre por completo cualquier tabla sin un filtro bloom sobre los valores del mapa de atributos (las tablas de métricas no lo tienen); `loom doctor` las nombra.

El paso cuenta las filas antes de borrar e informa de ese recuento. Una tabla o base de datos inexistente cuenta como nada que borrar; cualquier otro fallo hace que el paso falle de forma cerrada, y el worker `erasure_recovery` lo reintenta.

Comprueba qué claves de ID de usuario llevan realmente tus datos antes de confiar en los valores por defecto:

```sql
SELECT DISTINCT arrayJoin(mapKeys(LogAttributes))
FROM default.otel_logs WHERE TimestampTime > now() - INTERVAL 1 DAY
```

Consulta [retención y borrado](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) para ver la saga a la que pertenece este paso.

<a id="settings"></a>

## Configuración

| Configuración                      | Valor predeterminado                                       | Efecto                                       |
| ---------------------------------- | ---------------------------------------------------------- | -------------------------------------------- |
| `LOOM_OTEL_EXPORTER`               | `""`                                                       | `ch`, `otlp` o automático                    |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` | `""`                                                       | URL base del collector                       |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS`  | `""`                                                       | encabezados `K=V` separados por comas        |
| `LOOM_OTEL_ENABLED`                | `true`                                                     | interruptor general                          |
| `LOOM_CLICKSTACK_DATABASE`         | `""`                                                       | la base de datos de las tablas de OTel; habilita el borrado |
| `LOOM_CLICKSTACK_USER_ID_KEYS`     | `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id` | claves de atributo con las que coincide un borrado por usuario |

<a id="related"></a>

## Relacionado

- [Telemetría](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md): qué se emite y el catálogo completo de métricas.
- [Monitoreo](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md).
- [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md): los mismos spans que las trazas de llamadas al modelo.
