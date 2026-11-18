<!-- i18n: source=docs/operations/runtime-settings.md blob=b519ead8aee373492c371073baaa1fe49501d971 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md), which is authoritative.

<a id="runtime-settings"></a>

# Ajustes en tiempo de ejecución

Cada ajuste es una variable de entorno `LOOM_<FIELD>` que se lee al arrancar, y también puede ir en el archivo de ajustes YAML ([referencia de configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)). Un ajuste que la referencia marca como **live** admite además una sobrescritura para todo el despliegue mientras Loom está en ejecución (entre ellos la recuperación y el ranking, `memory_search_*`; la síntesis de respuestas, `memory_ask_*`; el ajuste de la extracción, del contexto y del conocimiento; y el agrupamiento de inserciones en ClickHouse, `ch_batched_insert*`). Todos los demás ajustes se leen una sola vez al arrancar y cambian con un reinicio. Los umbrales de similitud que la calibración reemplaza
(`memory_search_min_similarity`, `memory_search_abstain_threshold`,
`memory_search_hyde_fallback_threshold`, `memory_search_query_rewrite_threshold`) no son
live: con `LOOM_ADAPTIVE_THRESHOLDS` activado se aplican solo cuando no hay calibración.

<a id="see-what-is-in-force"></a>

## Ver qué está en vigor

```bash
loom settings list --changed              # off its default, or overridden
loom settings list --prefix memory_ask_   # one area
loom settings list --json
```

Cada línea muestra el valor, de dónde viene y si es live:

```text
memory_ask_recency_weight = 0.35  (override, live)
ch_host = "clickhouse"  (env)
```

| Origen          | Significado                                                                              |
| --------------- | ---------------------------------------------------------------------------------------- |
| `override`      | establecido con `loom settings set` o `/v1/admin/settings.set`                           |
| `dashboard`     | la configuración de modelos guardada en Settings → Models & keys                         |
| `env`           | el entorno del proceso                                                                   |
| `env_file`      | el archivo dotenv (`LOOM_ENV_FILE`, por defecto `.env`), también cuando se copia al entorno |
| `settings_file` | el archivo de ajustes YAML (`LOOM_SETTINGS_FILE`), también cuando `scripts/serve.sh` lo exporta |
| `inferred`      | rellenado al arrancar a partir de otros ajustes (el proveedor de LLM, la dimensión de un modelo local) |
| `default`       | el valor predeterminado incorporado                                                      |

Un secreto muestra solo su longitud.

<a id="override-a-live-setting"></a>

## Sobrescribir un ajuste live

```bash
loom settings set memory_ask_recency_weight 0.35
loom settings reset memory_ask_recency_weight
```

El valor se escribe como lo haría su variable de entorno y se valida de la misma forma: un valor fuera de rango, o un ajuste que no es live, se rechaza y no se guarda nada. La sobrescritura es una fila de `loom.runtime_overrides`. El proceso que la guarda la aplica de inmediato; todos los demás procesos, incluidos los workers, la aplican en un plazo de `LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS` (por defecto 10). Sobrevive a un reinicio y prevalece sobre el entorno y el archivo de ajustes hasta que se restablece; al restablecer, cada proceso recupera su propio valor o el predeterminado. `reset` rechaza un ajuste sin sobrescritura guardada, y elimina la sobrescritura de un ajuste que una compilación posterior ya no tiene. Cada cambio escribe una fila de auditoría `setting.changed` con el valor anterior y el posterior.

`--expect <value>` (`expected` por HTTP) no cambia nada a menos que ese sea el valor en vigor tras una lectura nueva de las sobrescrituras guardadas, y responde `409 PRECONDITION_FAILED` con el valor que encontró: un cambio hecho entretanto desde otra réplica o desde la CLI no se sobrescribe sin que se vea. Dos cambios enviados en el mismo instante aún pueden pasar ambos.

<a id="see-and-undo-past-changes"></a>

## Ver y deshacer cambios anteriores

```bash
loom settings history memory_ask_recency_weight
loom settings history --limit 20      # every setting
```

Cada línea es un cambio registrado, del más reciente al más antiguo: cuándo, quién (`cli:<user>` o el sujeto del token) y el valor anterior y el posterior. Un valor anterior que era una sobrescritura es el del despliegue: se deshace volviendo a establecerlo con `loom settings set`. Cualquier otro era el valor del proceso que hizo el cambio (su entorno o un valor predeterminado), marcado con `(undo: reset)`: se deshace con `loom settings reset`, que restaura el valor propio de cada réplica. Un cambio sin valor anterior (el restablecimiento de un ajuste que esta compilación ya no tiene) no tiene nada que deshacer.

Una sobrescritura es el valor predeterminado del despliegue, de modo que lo que establece un espacio de nombres o una solicitud sigue prevaleciendo sobre ella: los `retrieval_defaults` de un espacio de nombres (`top_k`, `min_similarity`, `reinforcement_weight` y los demás parámetros de búsqueda que acepta `namespace.register`) y los campos propios de una solicitud.

Las mismas operaciones por HTTP, con un token de administrador de la plataforma. Con la autenticación desactivada, un cambio solo se acepta desde una dirección de loopback (`localhost`): de lo contrario, una página web podría alcanzarlo mediante DNS rebinding.

```bash
curl -s "$LOOM_BASE_URL/v1/admin/settings.list" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"changed": true}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "value": 0.35}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "clear": true}'
```

`POST /v1/admin/settings.history` acepta `{"name": "...", "limit": 50}`. El SDK de Python tiene `agent.deployment.settings()`, `set_setting(name, value, expected=...)`, `reset_setting(name, expected=...)` y `setting_history(name)`.

<a id="during-a-rolling-upgrade"></a>

## Durante una actualización progresiva

Una compilación que no tiene un ajuste, que no lo lee en vivo, o cuyo campo rechaza el valor guardado, registra la sobrescritura guardada una sola vez y la ignora; los demás procesos siguen aplicándola. Una compilación anterior a la existencia de los ajustes en tiempo de ejecución ignora las sobrescrituras guardadas sin avisar, así que mientras una siga en servicio, una sobrescritura se aplica solo en las réplicas más nuevas. Los ajustes de ranking cambian lo que devuelve `memory.search`: mide un cambio con un benchmark antes de conservarlo ([ejecutar un benchmark](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).
