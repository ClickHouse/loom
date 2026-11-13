<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# Memoria, recuperación, auditoría y borrado para agentes autónomos

Loom ofrece a los agentes autónomos una memoria que pueden recuperar más adelante, un registro de auditoría de quién escribió qué y el borrado de un ámbito en todos los lugares donde Loom lo guarda. Son operaciones del servidor, almacenadas en
ClickHouse (o en Postgres para la memoria activa y el estado del conocimiento).

<a id="what-you-get"></a>

## Qué obtienes

- **Memoria y recuperación.** Hechos versionados con procedencia, búsqueda que se abstiene cuando
  nada se aproxima lo suficiente y un aumento de ranking para las memorias en las que se apoyaron
  ejecuciones exitosas. [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Rechazo.** Un hecho fijado rechaza las escrituras que lo contradicen, y `blacklist.add` registra un
  patrón que ninguna escritura puede almacenar. [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **Auditoría.** Cada operación que cambia la memoria deja una fila de auditoría con un SHA-256 de su
  contenido, y un worker sella ventanas horarias en una cadena de hashes, de modo que una edición o un borrado
  se pueden detectar. Las escrituras de auditoría del transporte son fail-open (si falla una inserción, se pierde la fila);
  bajo la autoridad de Postgres, la fila de auditoría de una revisión de memoria se almacena junto con la revisión.
  [Audit log](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **Borrado verificable.** `erasure.request` borra un ámbito de todas las tablas que contienen
  contenido de memoria (lápidas por defecto; `purge=true` elimina las filas de un ámbito `user_id` o
  `namespace_list`) y termina con un censo de residuos y un recibo.
  [Retention and erasure](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **Reversión.** `namespace.rollback` devuelve cada memoria de un espacio de nombres a su versión
  en un momento dado.
  [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Operación sin interfaz.** Un bot obtiene su propia identidad (se registra por sí mismo donde el autorregistro
  está abierto), comprueba su conexión antes de su primera ejecución y se puede diagnosticar cuando se queda
  en silencio.
  [Headless bots](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

La auditoría y el borrado son primitivas de ingeniería, no una certificación de cumplimiento normativo.
[Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

Algunas operaciones de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
