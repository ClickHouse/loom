<!-- i18n: source=docs/features/tracker/github-feed.md blob=81fe53135dda14b5a303c6f1f10b89c97e768376 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md), which is authoritative.

<a id="maintain-a-github-tracker-record"></a>

# Mantener un registro de tracker de GitHub

`loom tracker-feed` copia issues, pull requests, comentarios de issues y comentarios de revisión en línea de un repositorio en una base de datos ClickHouse dedicada del equipo. Loom lee las vistas resultantes a través de una [fuente de tracker de solo lectura](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md). El equipo es responsable del contenido copiado, las copias de seguridad y el borrado. El trabajo no realiza llamadas a modelos ni a embeddings.

<a id="prepare-the-record"></a>

## Preparar el registro

Usa una base de datos vacía con un UUID de base de datos estable, separada de la base de datos del despliegue de Loom. Concede a un escritor dedicado permiso para crear tablas y vistas, y para leer e insertar filas en esa base de datos. Mantén sus credenciales separadas de las del lector del tracker.

```sql
CREATE DATABASE team_feed;
CREATE USER feed_writer IDENTIFIED BY '…';
GRANT CREATE TABLE, CREATE VIEW, SELECT, INSERT ON team_feed.* TO feed_writer;
GRANT SELECT ON system.columns TO feed_writer;
GRANT SELECT ON system.databases TO feed_writer;
GRANT SELECT ON system.tables TO feed_writer;
CREATE USER feed_reader IDENTIFIED BY '…' SETTINGS readonly = 2;
GRANT SELECT ON system.columns TO feed_reader;
```

Proporciona la contraseña del escritor como `LOOM_TRACKER_FEED_WRITER_PASSWORD` mediante el sistema de secretos del planificador. Ejecuta la CLI con la misma configuración de despliegue de Loom que usa el proceso que sirve, incluida su conexión a ClickHouse y la raíz de secretos.
Establece `LOOM_TRACKER_ORG` con la organización que se va a inicializar. La CLI verifica los permisos directos del escritor antes de usarlo y rechaza roles, opciones de concesión y autoridad fuera de esta base de datos, salvo por esas tres tablas de metadatos de sistema.

```bash
loom tracker-feed init --org acme --host record.example --database team_feed \
  --writer-user feed_writer
```

La inicialización crea `github_issues`, `github_prs` y `edges` como vistas definer. Concede al lector acceso solo a esas vistas:

```sql
GRANT SELECT ON team_feed.github_issues TO feed_reader;
GRANT SELECT ON team_feed.github_prs TO feed_reader;
GRANT SELECT ON team_feed.edges TO feed_reader;
```

[Registra la fuente del tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md#adding-a-tracker-source) en modo views usando `feed_reader`. Incluye cada repositorio en la lista de espacios de nombres permitidos de la fuente y usa espacios de nombres restringidos para los registros privados. Establece el `LOOM_TRACKER_ORG` del despliegue con la misma organización. El registro de la fuente mantiene sus comprobaciones existentes de host, credenciales y contrato. Las direcciones HTTP y privadas requieren el ajuste de host privado de desarrollo; las conexiones de producción usan HTTPS.

<a id="configure-and-schedule-a-repository"></a>

## Configurar y programar un repositorio

Crea un token de grano fino de solo lectura, o un token de instalación de App, restringido a los repositorios que se van a copiar. Necesita permisos de lectura de Metadata, Issues y Pull requests. `--read-only-token` es la declaración del operador sobre los permisos; la CLI no puede certificar la ausencia de permisos de escritura a partir de una solicitud exitosa ni del prefijo del token.

Proporciona ese token como `LOOM_TRACKER_FEED_GITHUB_TOKEN` mediante el sistema de secretos y luego configura un repositorio de la lista de permitidos:

```bash
loom tracker-feed configure --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer --read-only-token --max-pages 5
loom tracker-feed sync --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer
```

La configuración sella el token en la base de datos de feed del equipo. La sincronización necesita la contraseña del escritor y la raíz de secretos del despliegue; no necesita la variable de entorno del token.
Las identidades de repositorio copiadas conservan la grafía de la lista de permitidos de la fuente registrada, de modo que los filtros de espacio de nombres coinciden aunque las mayúsculas de la API difieran.
Los tokens de instalación de App [caducan tras una hora](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).
Un proceso de secretos externo debe renovar el token de instalación y volver a ejecutar `configure` antes de que caduque. El feed no emite ni renueva tokens de instalación.
Usa `--writer-password-env` o `--token-env` para elegir otra variable de credenciales.
Durante una rotación de la raíz de secretos, las escrituras de metadatos exitosas vuelven a sellar las credenciales abiertas con `LOOM_JWT_SECRET_PREVIOUS`. Completa un turno exitoso antes de retirar la raíz anterior.

Programa `sync` cada cinco minutos o con más frecuencia. Las observaciones de visibilidad caducan a los 15 minutos, lo que oculta el repositorio si las comprobaciones dejan de tener éxito. Cada trabajo tiene un límite de 120 segundos y lee como máximo `--max-pages` páginas por flujo, con 100 elementos por página y un presupuesto de 2 a 100 páginas. Se reproduce el límite de marca de tiempo de la última página; el mínimo deja una página anterior desde la que avanzar.
Un error de capacidad deja el cursor sin cambios; aumenta el presupuesto de páginas acotado o acorta el trabajo pendiente del repositorio antes de reintentar. Los recuentos en la salida estándar informan de las filas procesadas, incluidas las reproducidas.

<a id="visibility-and-erasure"></a>

## Visibilidad y borrado

Cada flujo verifica la identidad inmutable del repositorio y su visibilidad actual. Si las comprobaciones fallan, se ocultan sus vistas. Cada elemento y comentario conserva con su contenido la clasificación de privacidad de la observación. Las vistas usan la clasificación más restrictiva entre el repositorio, el elemento y los comentarios almacenados, incluso cuando esas tablas llegan a una réplica en momentos distintos. Es una observación por sondeo con una caducidad acotada, no un espejo instantáneo de permisos. Siguen aplicándose las comprobaciones normales de privacidad del tracker y de permisos directos.

Cuando un repositorio pasa a ser público, la configuración o la sincronización restablece sus cursores y los turnos programados reproducen todos los flujos dentro del mismo presupuesto de páginas. Un elemento pasa a ser público después de que su fila y sus comentarios almacenados se hayan actualizado bajo observaciones públicas. Las filas eliminadas en origen requieren borrar las copias retenidas; la reproducción no puede actualizarlas.

Los cuerpos incluyen hasta 32,768 caracteres por elemento y los 64 comentarios actualizados más recientemente, con hasta 2,048 caracteres por comentario. Las solicitudes de revisión y la evidencia de pruebas permanecen como desconocidas cuando la fuente no proporciona esa evidencia.
El `updated_at` de las vistas refleja la publicación del elemento y del comentario en el registro, de modo que un comentario importado después de su elemento sigue siendo visible para las lecturas incrementales del tracker.

Las listas incrementales no informan de eliminaciones. Para borrar contenido copiado, detén todos los escritores programados, borra las filas sin procesar afectadas, los comentarios y la configuración o los cursores del repositorio, y exclúyelo antes de reiniciar. Eliminar un espacio de nombres de Loom elimina los datos derivados de Loom; no borra la base de datos del equipo ni las copias de seguridad. La inicialización vincula un registro a un despliegue y una organización. Usa un registro distinto para otro despliegue.

Consulta la [arquitectura del feed](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md) para conocer los límites de cursor, la concurrencia y la propiedad.
