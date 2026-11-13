<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# Memoria de agentes autoalojada en ClickHouse

Loom se ejecuta en tu propia infraestructura, con Docker o en Kubernetes con su chart de Helm, y
almacena la memoria de los agentes, los documentos, las conversaciones y los índices de código como tablas en ClickHouse. Un
despliegue puede elegir Postgres en el primer arranque para el estado de la memoria activa y del conocimiento. Los datos se
almacenan solo en tus almacenes (los proveedores de modelos alojados y los destinos de trazas que configures
reciben lo que procesan), y lo que saben tus agentes se puede consultar con SQL.

<a id="what-you-get"></a>

## Qué obtienes

- **Docker o Kubernetes.** El instalador de una línea inicia el stack local; el Helm chart
  ejecuta N réplicas idénticas detrás de una única URL. [Despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Ejecución de N réplicas](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS o ClickHouse Cloud.** [Elegir el ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud y Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **Un único almacén para vectores, texto y auditoría.** Con la autoridad ClickHouse predeterminada,
  la búsqueda vectorial, léxica y de grafos, los filtros, la auditoría y el borrado son SQL en una sola base de datos.
  [Autoridad de almacenamiento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **Multiusuario.** Organizaciones, namespaces, roles, tokens y single sign-on.
  [Identidades y namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [Single sign-on](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **Modelos locales.** El stack publicado genera embeddings en CPU con un modelo local incluido, y
  el proveedor de embeddings acepta un endpoint compatible con OpenAI que tú mismo ejecutes.
  [Modelos y claves de API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## Mediciones

Una carga sintética de memorias con 2.4 versiones por clave y embeddings de 3072 dimensiones (dos
vectores por fila) ocupó cerca de 13.6 KB por clave en ClickHouse, casi todo en la fila del
embedding; un embedder más pequeño ocupa menos, y las partes con pocas filas ocupan algo más por fila
hasta que se fusionan.
[Memory at scale](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## Inicio

[Inicio rápido](../getting-started/quickstart.md) · [Requisitos previos](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) ·
[Actualizaciones](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [Copia de seguridad y recuperación](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
