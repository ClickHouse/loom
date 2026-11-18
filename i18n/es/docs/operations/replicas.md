<!-- i18n: source=docs/operations/replicas.md blob=f4c8729ef4e795f5b6ab89dcd363a0bf9c30aad0 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md), which is authoritative.

<a id="running-n-replicas"></a>

# Ejecutar N réplicas

Cada proceso de Loom es una réplica completa. Puedes ejecutar tantas como necesites sobre un mismo servicio de ClickHouse detrás de una sola URL, sin sesiones fijas (sticky sessions): cada una atiende todas las ops, verifica todos los tokens y escribe en el servicio compartido. El trabajo que debe ocurrir una sola vez se ejecuta en el proceso que tenga el lease de ClickHouse, y se traslada cuando ese proceso se detiene:

| Una vez por despliegue                        | Dónde se ejecuta                                                                                                          |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Migraciones de esquema                        | el primer proceso que arranca una build con migraciones pendientes, bajo el lease de migración; los demás esperan a que termine |
| Workers en segundo plano                      | el titular del lease `workers_supervisor` ([despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md))                                    |
| Publicación de la configuración del despliegue | el mismo titular del lease (más abajo)                                                                                    |

Cada proceso mantiene sus propios mirrors de git (una lectura de historial clona el que falte; `LOOM_CODE_HISTORY_CLONE_ON_READ`), sus propias cachés y sus propios buckets de límite de tasa.

<a id="one-env-for-all-of-them"></a>

## Un mismo env para todas

Arranca cada réplica desde el mismo env, con `LOOM_DEPLOYMENT=1`:

```bash
LOOM_DEPLOYMENT=1
LOOM_CH_HOST=...                 # the one ClickHouse service
LOOM_CH_PORT=8443
LOOM_CH_USER=...
LOOM_CH_PASSWORD=...
LOOM_JWT_SECRET=...              # the same root secret on every replica
LOOM_EMBEDDING_MODEL=...         # the same model and width on every replica
LOOM_EMBEDDING_DIM=...
OPENAI_API_KEY=...               # each replica makes its own provider calls
LOOM_TRUSTED_PROXIES=...         # the load balancer, so per-client limits see the client
```

`LOOM_JWT_SECRET` firma con el kid `default` y cifra cualquier otra clave de firma y toda credencial almacenada, de modo que con un único valor cada token funciona en todas las réplicas. Las claves de firma, las revocaciones, las concesiones y las cuentas de servicio son filas del servicio compartido; una rotación o una revocación llega a todas las réplicas dentro de `LOOM_JWT_KEYRING_REFRESH_SECONDS` y `LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS`.

<a id="configuration-drift"></a>

## Deriva de configuración

Cada proceso escribe una fila de heartbeat en `loom.instances` cada `LOOM_INSTANCE_HEARTBEAT_SECONDS` (10 s por defecto): su build, una huella de sus ajustes MUST_MATCH (el espacio de embeddings: `LOOM_EMBEDDING_PROVIDER`, `_MODEL`, `_DIM`, `_BASE_URL` y `_SEND_DIMENSIONS`), una huella de su secreto raíz y su hora de inicio. Una fila que no se renueva durante tres intervalos, o que su proceso marcó como detenida al apagarse, no está activa.

El titular del lease publica su configuración en `loom.deployment_config`: los ajustes MUST_MATCH y una huella HMAC de su secreto raíz. Cada proceso se compara con esa instantánea al arrancar y en cada heartbeat. Un secreto raíz distinto (sus tokens y credenciales almacenadas no se abrirían en otro lugar) o un valor MUST_MATCH distinto (sus vectores estarían en otro espacio) es deriva: el proceso no está listo, de modo que el balanceador de carga no le envía nada, y `/v1/health` informa del problema `config_drift` con lo que difiere. Que no esté listo solo detiene las conexiones nuevas, así que el proceso además responde con `503 CONFIG_DRIFT` a toda llamada de API, MCP e inicio de sesión y cierra la conexión: el reintento de un cliente cae en una réplica que sí coincide. Los valores comparados son los del env: un cambio de modelo desde el dashboard es del despliegue (cada réplica lo lee de ClickHouse), por lo que no es deriva, y una réplica iniciada después con el mismo env está lista. `deployment_config` en `/v1/health` muestra la última comprobación y los hosts activos.

El titular del lease nunca publica sobre una instantánea de la que deriva: una réplica con el secreto raíz o el modelo de embeddings equivocado que gane el lease sigue sin estar lista, y las réplicas con el env publicado siguen listas. Una instantánea con la que coincide solo se reescribe cuando ninguna otra réplica activa ejecuta la almacenada, de modo que las instantáneas de forma distinta de N-1 y N durante un despliegue gradual nunca alternan de un lado a otro.

**Cambiar un ajuste MUST_MATCH o `LOOM_JWT_SECRET`** no se puede hacer de forma gradual: las réplicas con el valor nuevo no están listas mientras las del valor antiguo atienden. Detén todas las réplicas, cambia el env, ejecuta `loom deployment publish --yes` con el env nuevo (lista lo que cambia) y luego vuelve a iniciarlas (a un cambio de embeddings le sigue una re-incrustación completa, `loom reembed`). Tras cambiar el secreto, vuelve a emitir los tokens y a almacenar las credenciales.

Los procesos sin `LOOM_DEPLOYMENT` (un servidor de rama o de A/B sobre el mismo servicio) también envían heartbeat, pero nunca publican, nunca se comprueban y no cuentan como réplicas.

<a id="rate-limits"></a>

## Límites de tasa

Los topes configurados son para todo el despliegue. La parte de cada réplica es el tope dividido entre los hosts con una fila activa en `loom.instances`, de modo que añadir o quitar una réplica redimensiona todos los buckets dentro de un heartbeat. `LOOM_REPLICA_COUNT` es el mínimo de esa cuenta, y la cuenta mientras no se pueden leer las filas. Los web workers de un mismo host comparten una tabla de buckets ([despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)).

<a id="code-repositories"></a>

## Repositorios de código

Cada proceso vuelve a leer `loom.code_repos` cada `LOOM_CODE_REPOS_REFRESH_SECONDS` (30 s por defecto), de modo que un repositorio añadido o restringido en una réplica se aplica en todas las réplicas dentro de ese tiempo.

Un host que no deba servir historial de git (un edge sin disco para mirrors) define `LOOM_GIT_HISTORY=none`: blame, las comprobaciones de commits y el historial de PR responden entonces sin él.

<a id="stopping-a-replica"></a>

## Detener una réplica

Deténla de forma ordenada (`docker stop`, SIGTERM): marca su fila de heartbeat como detenida y retira su fila de participante del proveedor. Una terminación forzada deja ambas; la fila de heartbeat deja de contar tras tres intervalos. Un proceso que se reinicia en el mismo contenedor retira por sí mismo la fila de participante del proceso muerto. Si una reconfiguración del proveedor espera desde otro lugar a la fila de un proceso muerto, retírala con el `participant_id` que informó su `/v1/health`, una vez que hayas confirmado que ya no existe (el comando rechaza uno cuyo heartbeat es reciente o cuyas consultas siguen en ejecución):

```bash
loom participant retire --participant <participant_id> --yes
```
