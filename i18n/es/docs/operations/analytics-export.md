<!-- i18n: source=docs/operations/analytics-export.md blob=241a7492fe019249ba054a278a0059bcb928b7e0 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md), which is authoritative.

<a id="exporting-usage-to-a-data-warehouse"></a>

# Exportar el uso a un data warehouse

`loom export-analytics` escribe un día UTC de uso y una instantánea de los espacios de nombres en un bucket de S3 como archivos Parquet, un archivo por entidad y por día, para que los cargue un data warehouse. El Helm chart lo ejecuta una vez al día como un CronJob (`analyticsExport` en `values.yaml`). Para consultar in situ las memorias de un tenant, con su contenido, y su registro de solicitudes, usa en su lugar la [superficie SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md).

```bash
loom export-analytics --bucket analytics-landing --prefix loom \
  --role-arn arn:aws:iam::123456789012:role/loom-analytics-writer \
  --expected-bucket-owner 123456789012
```

Necesita `LOOM_ANALYTICS_EXPORT_NAME_KEY`, el secreto con el que se aplica el hash a los nombres (consulta [nombres con hash](#hashed-names)).

`--day YYYY-MM-DD` exporta otro día; por defecto es ayer (UTC). Se rechaza un día que aún no ha terminado.

<a id="files"></a>

## Archivos

```text
<prefix>/<entity>/<yyyy>/<mm>/<dd>/<entity>-<yyyy>-<mm>-<dd>.parquet    entity: usage, namespaces
```

Cada subida lleva `If-None-Match: *`, por lo que un objeto nunca se reemplaza. Una segunda ejecución de un día conserva los archivos de la primera y muestra `already there, kept` por cada uno; una ejecución fallida o repetida se puede reintentar tal cual. Para exportar un día de nuevo, elimine antes sus objetos.

| Entidad      | Una fila por                                                                                                        | Columnas                                                                                                                                                                                                                                 |
| ------------ | ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `usage`      | `org`, `namespace`, `agent`, `operation`, `kind`, `transport`, `traffic_class` del día, a partir de `loom.agent_events` | `day`, `ops`, `errors`, `server_errors`, `slow`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`, `cost_usd`, `bytes_written`, `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`, `users`, `sessions`, `snapshot_at` |
| `namespaces` | `org`, `namespace` registrado en `loom.namespaces` o que contiene una fila de memoria activa                        | `registered`, `kind`, `visibility`, `active`, `created_on`, `live_memories`, `last_memory_on`, `day`, `snapshot_at`                                                                                                                      |

`kind` en `usage` es `request` para una solicitud, `worker` para una pasada del worker, `verdict` para una comprobación de consistencia diferida de `memory.ask` y `settle` para una resolución de conflictos diferida. Cada ruta `/v1/me/*` es una sola `operation`, `me`: el registro nombra esas filas según la ruta de la solicitud, que puede contener cualquier cadena enviada por quien llama. `users` y `sessions` son recuentos distintos dentro de la fila que excluyen el `-` de una llamada que no indicó ninguno; una suma sobre varias filas cuenta a un usuario una vez por fila. `slow` y los cuantiles de latencia son los del resumen del SLI: cuentan las solicitudes `ok` que ese resumen lee (una operación con una clase de SLO), y `slow` las que superan su presupuesto de latencia (un presupuesto de 0 hace que todas sean lentas); una fila sin ninguna tiene latencias de 0. Una ruta `/v1/me/*`, un juicio diferido o una pasada del worker no cuentan en ninguno de los dos. El archivo `namespaces` es el estado en `snapshot_at`, sin importar lo que indique `--day`: un backfill de un día antiguo obtiene el uso de ese día y los espacios de nombres de hoy. Un espacio de nombres no registrado tiene `registered = false` y columnas de registro NULL. `created_on` y `last_memory_on` son días, no horas: una hora exacta junto a un espacio de nombres personal con hash podría relacionarse con la persona que se registró o escribió en ese momento. Las filas se ordenan por los nombres con hash, nunca por los nombres en sí.

<a id="what-never-leaves-loom"></a>

## Lo que nunca sale de Loom

Las columnas son una lista de permitidos (`server/insights/store/analytics_export.py`; la prueba de integración fija la lista exacta de columnas de cada entidad). Un archivo en el bucket queda fuera del [borrado](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md), por lo que la exportación no incluye contenido de memoria ni identificadores almacenados de personas: no hay claves ni valores de memoria, ni `args`, ni `user_id`, `session_id`, `subject`, `owner_email` ni id de solicitud. Los usuarios y las sesiones salen solo como recuentos.

<a id="hashed-names"></a>

## Nombres con hash

Los nombres de organización, espacio de nombres y agente pueden identificar a una persona: un espacio de nombres personal lleva el nombre del correo de su propietario, el autorregistro construye el espacio de nombres y el nombre de un agente a partir del usuario y del host del sistema operativo, y un cliente MCP pone nombre a su agente. Por eso `org`, `namespace` y `agent` siempre salen como el hexadecimal en minúsculas del SipHash-2-4-128 de referencia del nombre con una clave secreta.

Cada nombre se calcula por separado, así que una misma cadena tiene un único hash dondequiera que aparezca: el nombre de un espacio de nombres produce el mismo hash en todas las organizaciones, y una organización, un espacio de nombres y un agente con el mismo nombre comparten hash. El almacén de datos puede enlazar esas filas sin la clave, por ejemplo el mismo espacio de nombres personal en dos organizaciones.

- La clave es una cadena aleatoria de 32 o más caracteres en `LOOM_ANALYTICS_EXPORT_NAME_KEY`; la exportación se niega a ejecutarse con una clave más corta o vacía, o con espacios en blanco al principio o al final (un Secret creado con `--from-file` conserva el salto de línea del archivo, lo que haría que cada hash dependiera de él).
- La clave SipHash de 16 bytes son los primeros 16 bytes del SHA-256 de esa cadena (UTF-8). El hash es `sipHash128ReferenceKeyed` de ClickHouse, que cualquier biblioteca de SipHash-2-4-128 reproduce a partir de esos 16 bytes, de modo que un sistema que tenga la clave puede calcular el hash de sus propios ids para cruzarlos. ClickHouse recibe la clave como dos palabras, cada una la mitad de 8 bytes leída en little-endian. Calcula el hash del id como la misma cadena que guarda Loom (`toString` de un UUID o de un número): el SipHash de una columna tipada procesa su forma binaria, que nunca coincide.

  ```sql
  WITH SHA256({key:String}) AS d
  SELECT lower(hex(sipHash128ReferenceKeyed(
      (reinterpretAsUInt64(substring(d, 1, 8)), reinterpretAsUInt64(substring(d, 9, 8))),
      toString(tenant_id)))) AS hashed_tenant_id
  FROM tenants
  ```

- Un hash es un seudónimo, no un valor anónimo: quien tenga la clave puede recalcularlo a partir de un nombre que conozca. Borrar un espacio de nombres en Loom no elimina sus filas que ya están en el bucket.
- Un hash es estable mientras lo sea la clave. Una clave nueva cambia todos los hashes, y un día ya exportado conserva sus archivos antiguos: bórralos antes de volver a exportarlo con la clave nueva.
- Una consulta que usa la clave la deja registrada, tanto si la clave se pasa como parámetro como si se escribe como literal: ClickHouse sustituye los valores enlazados en el texto de la consulta en `system.query_log` y los escribe en sus registros de depuración y de traza. En el almacén de datos, restringe quién puede leer esos registros en el servidor que ejecuta el join, o calcula el hash de los ids fuera de ClickHouse con cualquier biblioteca de SipHash-2-4-128. Por parte de Loom, las palabras de la clave de la exportación quedan en esos mismos registros del ClickHouse de Loom, cuyos lectores ya pueden leer los nombres.

<a id="credentials"></a>

## Credenciales

El comando usa las credenciales de AWS predeterminadas de su proceso y las comprueba una vez antes de
cualquier trabajo con ClickHouse: con `--role-arn`, asumiendo el rol; sin él, preguntando a STS a quién
pertenecen (`GetCallerIdentity`). Si faltan credenciales o han caducado, o si no se puede asumir el rol,
el comando falla en ese punto y no después de la lectura; si falta el permiso `s3:PutObject`, si el bucket es incorrecto
o si el propietario del bucket es distinto de `--expected-bucket-owner`, falla en la primera subida.
Con `--role-arn` vuelve a asumir el rol antes de cada subida y escribe con sus credenciales.
Asumir un rol de escritura es adecuado para un bucket de otra cuenta: la cuenta del bucket concede
`s3:PutObject` al rol de escritura, y el rol de escritura confía solo en la identidad propia de la exportación.
La exportación no necesita permisos de lectura, listado ni borrado sobre el bucket; un bucket
cifrado con una clave KMS administrada por el cliente necesita además `kms:GenerateDataKey` sobre esa clave
para el rol de escritura, o todas las subidas se rechazan. `--expected-bucket-owner` hace que S3 rechace la
escritura cuando el bucket pertenece a otra cuenta.

Pase `--region` (la del bucket, `analyticsExport.region` en el chart) a menos que el proceso
ya tenga una definida mediante `AWS_DEFAULT_REGION` o el archivo de configuración de AWS; boto3 no lee
`AWS_REGION` aquí.

<a id="on-kubernetes"></a>

## En Kubernetes

```yaml
analyticsExport:
  enabled: true
  bucket: analytics-landing
  prefix: loom
  roleArn: arn:aws:iam::123456789012:role/loom-analytics-writer
  expectedBucketOwner: "123456789012"
  nameKeySecret:
    name: loom-analytics-export-key # key LOOM_ANALYTICS_EXPORT_NAME_KEY
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::210987654321:role/loom-analytics-export
```

```bash
kubectl create secret generic loom-analytics-export-key \
  --from-literal=LOOM_ANALYTICS_EXPORT_NAME_KEY="$(openssl rand -hex 32)"
```

Solo el pod de la exportación lee ese Secret. El chart se niega a renderizarse sin
`nameKeySecret.name` y rechaza cualquier configuración que entregue la clave a todos los pods de web y workers:
`LOOM_ANALYTICS_EXPORT_NAME_KEY` en `env`, `extraEnv` o `secrets.extra`,
`analytics_export_name_key` en `settingsFile` (un ConfigMap que monta cada pod), y un
`nameKeySecret.name` que sea el Secret de la release (`secrets.existingSecret` o el propio
del chart) o un Secret de `envFrom`. No puede ver el contenido de esos Secrets: mantén la clave fuera de
ellos.

El chart crea una ServiceAccount `<fullname>-analytics-export` y un CronJob con el mismo
nombre, donde `<fullname>` es el nombre completo del chart: `fullnameOverride` si está definido; si no, el
nombre de la release cuando contiene el nombre del chart (`nameOverride`, o si no `loom`); si no,
`<release>-<chart name>`, recortado a sus primeros 35 caracteres para que el nombre quede dentro de 52
(`kubectl get sa` lo muestra). El job se ejecuta a las 00:30 UTC (`schedule`), una ejecución a la vez,
con hasta 3 reintentos y un plazo de tres horas para todos los intentos; cada sentencia puede ejecutarse
20 minutos y el cliente de ClickHouse puede reintentar una, de modo que un día lento puede tener menos de cuatro
intentos. Un rechazo (exit 2) hace fallar el Job de inmediato en lugar de reintentarlo. Su pod no toma nada
de `podLabels`, `podAnnotations` ni `affinity` de los pods de web.
Los pods de web y workers conservan su propia ServiceAccount, de modo que el rol que puede asumir el
rol de escritura queda vinculado solo al pod de la exportación. Con IRSA, la relación de confianza de ese rol nombra
`system:serviceaccount:<namespace>:<fullname>-analytics-export`. Con `roleArn` definido, su
único permiso es `sts:AssumeRole` sobre `roleArn`; con `roleArn` vacío escribe por sí mismo y
necesita `s3:PutObject` sobre el bucket (y `kms:GenerateDataKey` sobre una clave KMS
administrada por el cliente). Con EKS Pod Identity, vincula el rol a la ServiceAccount y deja `annotations`
vacío.

El job lee de ClickHouse con el Secret, la configuración, `extraEnv` y
`extraVolumes` de la release, igual que los pods de web, y no escribe nada en él. Una ejecución fallida se muestra como un
Job fallido; configura una alerta para cuando la última ejecución correcta del CronJob tenga más de un día.

Un día cuya ejecución falla en todos los intentos, o que pierde su ventana de inicio
(`startingDeadlineSeconds`, una hora), no se exporta después por sí solo: la siguiente ejecución
exporta únicamente el día anterior a ella. Expórtalo con un Job creado a partir del CronJob, de modo que se ejecute
con la ServiceAccount y los flags de la exportación, añadiendo `--day` a su comando:

```bash
kubectl create job --from=cronjob/<fullname>-analytics-export analytics-export-backfill \
  --dry-run=client -o yaml > backfill.yaml
# add `- --day=YYYY-MM-DD` to the container's command in backfill.yaml, then:
kubectl apply -f backfill.yaml
```

El archivo de uso corresponde a ese día; el archivo de espacios de nombres es la instantánea del momento de la ejecución.
