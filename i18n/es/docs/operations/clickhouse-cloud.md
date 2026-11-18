<!-- i18n: source=docs/operations/clickhouse-cloud.md blob=3fc467ae1903f7f94d3cdaccb6831c92c883fd58 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md), which is authoritative.

<a id="clickhouse-cloud-and-managed-postgres"></a>

# ClickHouse Cloud y Managed Postgres

Esta guía ejecuta Loom en Kubernetes sobre dos servicios gestionados: ClickHouse Cloud para proyecciones, vectores, historial y analítica, y ClickHouse Managed Postgres como autoridad de la memoria. Cubre los servicios, los valores `postgres` del chart, las comprobaciones posteriores a la instalación, la rotación de la contraseña de Postgres y de la CA, y el usuario de la superficie SQL. El chart en sí está en [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) y la autoridad en [Autoridad de memoria en Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md).

El overlay `deploy/helm/loom/values-clickhouse-cloud.yaml` se aplica sobre el de EKS. Está pensado para un despliegue sobre un ClickHouse vacío: la autoridad se elige en el primer arranque de un despliegue, y sobre un ClickHouse que ya contiene memoria los pods se niegan a iniciar, porque ninguna migración traslada la memoria de la autoridad ClickHouse a Postgres.

<a id="the-services"></a>

## Los servicios

- **ClickHouse Cloud**: un servicio con una base de datos llamada `loom` y un usuario para Loom, con su lista de acceso por IP que permite las direcciones NAT del clúster ([Kubernetes, En EKS](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md#on-eks)).
- **ClickHouse Managed Postgres**: un servicio con **dos standbys de HA**, una base de datos y un rol que es su propietario, y el endpoint **directo** en el puerto 5432
  ([Postgres, ClickHouse Managed Postgres gestionado](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#clickhouse-managed-postgres)). Otorga
  al rol `pg_read_all_stats`, para que Loom pueda ver qué standbys están conectados:

  ```sql
  GRANT pg_read_all_stats TO loom;
  ```

<a id="secrets"></a>

## Secretos

El overlay lee dos Secrets junto a `loom-secrets`: `loom-postgres`, clave `password`, con
la contraseña del rol, y `loom-postgres-ca`, clave `ca.crt`, con el bundle de la CA. Créalos
del mismo modo que `loom-secrets`, cada uno con su propio ExternalSecret.

`ca.crt` contiene todos los certificados raíz que la CA del servicio publica actualmente,
concatenados en formato PEM; el driver carga el archivo como un bundle y no confía en nada
más. Toma las raíces de la lista publicada por la CA (la URL "CA Issuers" del certificado
del servidor indica la CA); `openssl s_client -starttls postgres -showcerts` normalmente
imprime la cadena sin su raíz. Convierte primero una raíz obtenida en formato DER:

```bash
openssl x509 -inform DER -in root.der -out root.pem
```

Concatena los archivos `root.pem` en `ca.crt`, el archivo que contiene el Secret. Compruébalo
contra la cadena que envía el servidor, sin confiar en nada salvo el bundle, como hace el
driver: `leaf.pem` es el primer certificado que imprime `s_client -showcerts` y `chain.pem`
el resto (`-no-CAstore` requiere la versión 3 de `openssl`):

```bash
openssl verify -no-CApath -no-CAstore -CAfile ca.crt -untrusted chain.pem \
  -verify_hostname <postgres host> leaf.pem
```

Un certificado que el bundle no cubre falla de forma cerrada: un pod que arranca con él no
inicia, y su log muestra `SSLCertVerificationError`; en un pod en ejecución, toda conexión
nueva falla. Cuando la CA anuncie un cambio en su jerarquía, actualiza el bundle bajo un
nuevo nombre de Secret (más abajo).

<a id="install"></a>

## Instalación

```bash
helm install loom deploy/helm/loom -n loom \
  -f deploy/helm/loom/values-eks.yaml -f deploy/helm/loom/values-clickhouse-cloud.yaml \
  --set publicBaseUrl=https://loom.example.com \
  --set clickhouse.host=<service>.<region>.aws.clickhouse.cloud \
  --set postgres.host=<postgres direct host>
```

Con `postgres.host` definido, el chart entrega a cada pod que ejecuta Loom (web, workers, los Jobs de hooks y la exportación de analítica):

- `LOOM_STORAGE_AUTHORITY=postgres`, `LOOM_PG_DURABILITY` y `LOOM_PG_POOL_MAX_SIZE` a partir de
  `postgres.durability` y `postgres.poolMaxSize`;
- `LOOM_PG_DSN` sin contraseña,
  `postgres://<user>@<host>:<port>/<database>?sslmode=verify-full&sslrootcert=/etc/loom/pg-ca/<key>`,
  de modo que la contraseña nunca queda en la especificación del pod;
- `PGPASSWORD` a partir de `postgres.passwordSecret`, que el driver lee cuando el DSN no incluye ninguna;
- `postgres.caSecret` montado en modo de solo lectura en `/etc/loom/pg-ca`;
- con `networkPolicy.enabled`, salida (egress) hacia `postgres.port`.

El chart se niega a renderizar un bloque `postgres` que no pueda transmitir tal como está escrito, y también cualquiera de los nombres que renderiza si se define por segunda vez en `env`, `extraEnv`, `secrets.extra` o `settingsFile`; cada rechazo indica el valor que hay que corregir, y los comentarios del bloque en `deploy/helm/loom/values.yaml` describen las reglas. Sus valores prevalecen sobre los mismos nombres en `existingSecret` y `envFrom`.

<a id="connections"></a>

### Conexiones

Cada proceso de Loom mantiene hasta `postgres.poolMaxSize` conexiones; una llamada que las encuentra todas ocupadas espera hasta `LOOM_PG_STATEMENT_TIMEOUT_MS` por una libre y después responde 503. Un despliegue progresivo (rollout) ejecuta los pods antiguos y los nuevos a la vez, así que dimensione el servicio para el doble del número en régimen estable:

```text
poolMaxSize × (webWorkers × web pods + worker processes) × 2
  + booting processes + running erasures + CLI runs  ≤  max_connections − reserved
```

Los pods web son `autoscaling.maxReplicas` con el autoescalado activado, y `replicaCount` en caso contrario; los procesos worker son `workers.replicaCount` con `workers.enabled`, y si no, uno, que ejecuta un pod web. Cada borrado en curso mantiene una conexión más para su paso en Postgres. La sobrescritura de `webWorkers` en el overlay es 2, lo que elige la imagen para el límite de 2 CPU del chart, y `poolMaxSize` es 5: con tres pods web y un pod de workers son 70 conexiones, lo que cabe en un servicio cuyo `max_connections` deja 100 para Loom. Aumentar `webWorkers` exige ajustar `resources.limits.cpu` y la memoria (1 GiB por proceso). Las réplicas añadidas por encima de la cuenta del chart por cualquier otro medio quedan fuera de la suma.

<a id="after-the-install"></a>

## Después de la instalación

- Loom se niega a arrancar cuando el `synchronous_standby_names` del servicio está vacío. Arranca con menos standbys conectados de los que nombra, y registra el WARNING `Postgres durability: fewer synchronous standbys are attached than required`. A partir de ahí, un commit espera a los standbys hasta `LOOM_PG_STATEMENT_TIMEOUT_MS` más un segundo, y responde 503 `POSTGRES_UNAVAILABLE` con `outcome_unknown`
  ([qué comprueba el arranque](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#what-the-boot-checks)).
- `GET /v1/health` informa `storage_authority: postgres` y `pg_ok`.
  `GET /v1/health?include=postgres` añade un bloque `postgres` con `standby: synchronous`;
  el bloque no aparece cuando `pg_ok` es false, cuando el `SELECT 1` de la comprobación falla por cualquier motivo o tarda más de 2 s.
- Un rol sin `pg_read_all_stats` arranca igualmente, con el WARNING de arranque
  `Postgres durability: cannot see pg_stat_replication; standbys are unverified`, y
  `standby` vale `null`. El WARNING es la señal fiable; `standby` también vale `null` cuando
  su lectura falla o tarda más de 2 s.

<a id="rotating-the-password-and-the-ca"></a>

## Rotar la contraseña y la CA

La contraseña, en este orden, una paso tras otro sin pausas:

1. Actualiza la contraseña en el almacén de secretos y confirma que `loom-postgres` se ha sincronizado.
2. Ejecuta `ALTER ROLE loom PASSWORD '…'` en el servicio.
3. Ejecuta `kubectl rollout restart` en los Deployments de web y workers a la vez.

Las conexiones que ya están abiertas siguen funcionando. Entre los pasos 2 y 3, una llamada cuyo pod deba abrir una nueva conexión (el pool cierra las conexiones inactivas durante 300 s) responde 500, y el log del servidor muestra `InvalidPasswordError` (SQLSTATE 28P01); reintentar no sirve de nada hasta que el pod se reinicie. Un pod que arranque entre los pasos 1 y 2 (un escalado, un desalojo, la exportación de analítica o un Job de hook) falla al arrancar hasta el paso 2, y arranca en su siguiente reinicio.

La CA: crea el nuevo bundle con un nombre de Secret nuevo y ejecuta `helm upgrade` con `postgres.caSecret.name` apuntando a él. La especificación del pod cambia, así que los pods se reinician de forma progresiva. Elimina el Secret anterior solo cuando el rollout haya terminado y ninguna revisión a la que pudieras volver lo mencione.

<a id="the-sql-surface-user"></a>

## El usuario de la superficie SQL

Una organización lee la memoria de sus agentes como SQL a través de un usuario de ClickHouse que
crea `loom sql-access grant` ([superficie SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)). Ejecútalo en un pod web
con una credencial de administrador de ClickHouse que pueda crear usuarios, pasando la contraseña
de administrador desde un archivo, nunca escrita en un terminal que muestre lo que se teclea:

```bash
web=$(kubectl get deploy -n loom -o name \
  -l app.kubernetes.io/instance=loom,app.kubernetes.io/component=web)
kubectl exec -i -n loom "$web" -- sh -c \
  'read -r LOOM_CH_PASSWORD; export LOOM_CH_PASSWORD LOOM_CH_USER=default;
   exec loom sql-access grant --user acme_sql --org acme --namespaces support,research' < admin.pass
```

El comando imprime la contraseña del usuario SQL una sola vez, solo en el terminal: un flujo de exec no es
un registro del contenedor. `--namespaces '*'` también cubre los espacios de nombres que se creen más adelante; nunca lo uses en
una organización que comparten los registros de nuevos usuarios ([acceso](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)). `--all` en lugar de
`--org … --namespaces …` es adecuado para un despliegue cuyos datos pertenecen a una sola organización. Revoca el usuario
antes de desinstalar: `loom sql-access revoke --user acme_sql`, ejecutado de la misma forma.

<a id="restoring-a-service"></a>

## Restaurar un servicio

Después de restaurar cualquiera de los dos servicios, se ejecuta `loom reconcile` en un Job creado a partir de la especificación del pod web mientras la release está detenida ([restaurar un solo almacén](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#restoring-one-store)).

<a id="related"></a>

## Relacionado

[Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Autoridad de memoria en Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) ·
[Despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [Superficie SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md) ·
[Consultar Loom desde Postgres](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)
