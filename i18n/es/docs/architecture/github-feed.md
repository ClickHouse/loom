<!-- i18n: source=docs/architecture/github-feed.md blob=bcf50268160cb35aa8da8040dcb9b21ab7f6601a model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md), which is authoritative.

<a id="github-tracker-feed"></a>

# Feed de GitHub para el tracker

El feed de GitHub mantiene el registro de tracker de un equipo en la base de datos ClickHouse del equipo.
Los procesos de servicio de Loom leen ese registro a través de la
[fuente de tracker de solo lectura](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md) existente. El feed no
coloca cuerpos de issues, comentarios ni pull requests en las tablas de despliegue de Loom.

<a id="ownership-and-credentials"></a>

## Propiedad y credenciales

Un operador ejecuta el feed como un trabajo de CLI acotado, programado por el ejecutor de trabajos del equipo.
Antes de leer el endpoint registrado o abrir el registro, la CLI comprueba la
autoridad de almacenamiento de su despliegue real y si su celda posee la organización del feed.
La conexión externa repite esa verificación del despliegue; nunca trata los metadatos
del registro como autoridad del despliegue. El trabajo cierra ambos clientes y el pool de
Postgres del despliegue al salir.
La inicialización crea las tablas del feed y las vistas del contrato en una base de datos vacía y dedicada;
rechaza los objetos existentes. Después, el operador registra esa base de datos como fuente de tracker
con una credencial separada de solo lectura. La configuración y la sincronización requieren ese
endpoint registrado y su lista de repositorios permitidos. Una credencial de escritura suministrada por separado
puede escribir únicamente en la base de datos del feed del equipo; la credencial de lectura nunca se
eleva ni se reutiliza para escribir.

La credencial de la fuente es un token de GitHub de granularidad fina de solo lectura, o un token de instalación de App,
restringido a los repositorios configurados: Metadata, Issues y Pull requests,
todos de solo lectura. Los permisos del token son una configuración atestiguada por el operador: un
GET exitoso, el prefijo del token o una cabecera de permisos del endpoint no pueden certificar la ausencia de permisos de escritura.
Un token de host no identificado no es apto para una sonda en vivo de solo lectura. El token se
sella con el mecanismo existente de secret-root en la tabla de configuración del feed
del equipo. Ningún token aparece en argumentos, salidas ni vistas del contrato.
La CLI recibe la contraseña de escritura de la base de datos mediante una variable de entorno con nombre;
no persiste esa contraseña. Las solicitudes a GitHub usan GET sobre el origen fijo
`https://api.github.com`, rechazan las redirecciones y construyen las rutas a partir de nombres de repositorio
validados e ids numéricos de elementos. Nunca siguen las URL presentes en las respuestas.

El equipo es dueño del registro copiado, de su retención de copias de seguridad y de su borrado. Para borrar el contenido
copiado, el equipo detiene su escritor programado y elimina las filas del feed, los comentarios
y los puntos de control afectados antes de reiniciar con una exclusión o de quitar el repositorio
de origen. Eliminar un espacio de nombres de Loom retira los datos derivados y las concesiones de Loom; no
elimina el registro del equipo. Este es el mismo límite que se aplica a cualquier base de datos de tracker
registrada. La credencial del escritor del registro no puede acceder a la base de datos propia de Loom; el contenido copiado
nunca se escribe allí. Solo el lease del almacén del despliegue existente es local.

<a id="record-and-progress"></a>

## Registro y progreso

Los elementos y comentarios sin procesar se versionan en la base de datos del equipo con la tupla de identidad,
el actor del proceso y la clase de fuente importada. Las vistas del contrato exponen la última versión del elemento,
los UUID del contrato y la visibilidad efectiva. Los comentarios de issues y de pull requests se
conservan por separado y se incluyen en el cuerpo con capacidad de búsqueda del elemento correspondiente. Los campos de triaje
no admitidos usan los valores predeterminados tipados del contrato; las aristas están vacías hasta que haya evidencia.
El `updated_at` del contrato es la última marca de tiempo de publicación persistida entre el elemento
y sus comentarios, de modo que las importaciones tardías y las ediciones de comentarios hacen avanzar las lecturas incrementales del tracker.
Las versiones de publicación usan milisegundos enteros para coincidir con los cursores del tracker. Bajo el lease del
escritor del registro, cada lote de contenido recibe el mayor valor entre el reloj del registro y un milisegundo
posterior a cada versión de contenido persistida de su organización, en todos los repositorios.
Este orden también cubre los espacios de nombres que leen varios repositorios mediante un único cursor.
Las escrituras de control del repositorio también avanzan más allá de la versión persistida para su organización
y repositorio exactos, incluso cuando el reloj receptor retrocede. Por tanto, la revocación de visibilidad,
la rotación de credenciales y los puntos de control de cursores reemplazan la fila de control anterior
en lugar de perder frente a su marca de tiempo.
Las marcas de tiempo originales de origen permanecen separadas y gobiernan los cursores de la API. No se requiere ninguna llamada
a modelos ni a embeddings. Las identidades de repositorio conservan la grafía de la lista de permitidos registrada,
incluso cuando la API devuelve otra capitalización.

Las lecturas del propietario, de la revisión del repositorio y de la marca de agua de publicación solicitan
`select_sequential_consistency = 1`; el lease del escritor por sí solo no hace que una réplica
esté al día. Los escritores del feed requieren almacenamiento SharedMergeTree o un único servidor autoalojado,
conforme a las [garantías de lectura de lo escrito](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#read-your-writes).
Este ajuste por sí solo no cubre las escrituras confirmadas en tablas replicadas autoalojadas.
La validación del escritor acepta únicamente `ReplacingMergeTree` y `SharedReplacingMergeTree`
para las cinco tablas del feed en la base de datos del registro conectada. Los motores de tabla replicados, distribuidos
y no reconocidos se rechazan antes de que el trabajo pueda publicar.

Cada repositorio tiene cursores independientes para elementos, comentarios de issues y comentarios de revisión. El
[endpoint de issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues)
lista juntos los issues y las pull requests; las pull requests se hidratan mediante su endpoint
numérico. El
[endpoint de comentarios de issues](https://docs.github.com/en/rest/issues/comments#list-issue-comments-for-a-repository)
y el [endpoint de comentarios de revisión](https://docs.github.com/en/rest/pulls/comments#list-review-comments-in-a-repository)
tienen barridos separados por hora de actualización. Cada uno usa `updated_at` ascendente, con límites en páginas,
bytes de respuesta y tiempo de solicitud. El avance del cursor sigue solo a las escrituras confirmadas,
a través de un límite de marca de tiempo completo. Las solicitudes restan un segundo a la marca de tiempo guardada
porque `since` es exclusivo. Un presupuesto de páginas agotado guarda la marca de tiempo más temprana de su
última página; el siguiente turno repite ese límite. El presupuesto es de 2 a 100 páginas: una página anterior
permite avanzar mientras la última página conserva un límite de marca de tiempo incompleto.
Si una sola marca de tiempo llena todo el presupuesto de páginas configurado,
el trabajo informa un error de capacidad y conserva el punto de control en lugar de omitir filas o
afirmar que hubo progreso. El operador puede aumentar el presupuesto de páginas acotado para ese repositorio.
Las filas reproducidas reemplazan la misma identidad. La API es una colección cambiante, no una instantánea;
los barridos superpuestos reducen las condiciones de carrera de la paginación sin pretender lecturas transaccionales del origen.

Cada turno verifica la identidad y la visibilidad del repositorio antes de copiar contenido. Un
repositorio no disponible, movido o que no coincide primero oculta las filas del contrato de ese repositorio
y luego rechaza el turno. Las vistas del contrato también requieren una observación de visibilidad exitosa reciente,
que caduca tras un intervalo acotado si el programador se detiene. Cada publicación de elemento y de
comentario almacena la clasificación de privacidad de la observación en la misma fila que
su contenido. Las vistas usan la clasificación más restrictiva entre el repositorio, el elemento
y los comentarios almacenados, tanto para `is_private` como para el UUID del contrato. Por tanto, una réplica que conozca
contenido nuevo antes que los metadatos del repositorio no puede etiquetar como pública una publicación privada.
El predicado existente del tracker estándar excluye esas filas. Los espacios de nombres restringidos
conservan su verificación de concesión directa en vivo. La visibilidad es una observación por sondeo,
no un reflejo instantáneo de los permisos.
Una discrepancia de identidad validada o una observación privada puede ocultar el registro actual incluso
cuando otro turno haya cambiado su revisión; esto preserva las credenciales y los cursores actuales.
Los fallos genéricos de una credencial anterior conservan la comprobación de revisión. Las observaciones públicas
obsoletas no pueden ampliar el acceso.

Cuando un repositorio pasa a ser público, la configuración o la sincronización restablece los cursores de sus flujos existentes
antes de publicar los metadatos públicos. Los barridos acotados reproducen los elementos y comentarios
y marcan cada uno con la observación pública. Un elemento permanece restringido hasta que su propia
fila y sus comentarios almacenados se hayan reproducido. Los turnos programados repetidos continúan una
acumulación pendiente bajo los mismos límites de página y de límite de marca de tiempo. Las filas eliminadas en el origen no pueden
reproducirse; el equipo debe borrar esas copias retenidas para eliminar su clasificación
privada.

Las listas incrementales de la API no informan de issues ni comentarios eliminados. El equipo gestiona esos
borrados de forma explícita; el feed no afirma detectar eliminaciones. La configuración, las credenciales selladas
y los cursores permanecen con el registro del equipo, de modo que los reinicios de la CLI no dependen de
archivos locales. La inicialización vincula el registro al UUID de la base de datos de un único despliegue de Loom;
la configuración y la sincronización rechazan cualquier otro despliegue. Los trabajos concurrentes de ese despliegue
usan un único lease de publicación identificado por el UUID de la base de datos del registro, independiente de los alias
de fuente y de los cambios de nombre de repositorio. Las obtenciones del proveedor ocurren fuera de ese lease; la inicialización,
la configuración y la publicación lo comparten, de modo que las observaciones concurrentes de identidad por primera vez
no puedan eludir la serialización. Una escritura incierta en la base de datos conserva su reclamo durante la
ventana de envío acotada del servidor. El borrado del registro requiere que el equipo detenga antes todos los escritores.

<a id="validation-and-rollout"></a>

## Validación y despliegue

Los dobles de prueba cubren empates de paginación, respuestas parciales, límites de tasa, reintentos, cambios de visibilidad,
redirecciones y redacción de credenciales. Un ClickHouse privado verifica el contrato, las vistas actuales,
la reproducción y la publicación interrumpida de cursores. Un token de solo lectura, cuando esté disponible,
comprueba una porción acotada de un repositorio público; nunca se sustituye por un token de CLI con capacidad de escritura.
El feed no añade ninguna operación de protocolo de Loom ni ningún worker automático. Los despliegues existentes y los
trackers registrados no cambian hasta que un operador inicialice y programe un feed.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- [CLI](https://github.com/ClickHouse/loom/blob/main/server/cli/tracker_feed.py): `register_tracker_feed` y `_run` seleccionan el
  registro propio y su credencial de escritura separada; `_validate_writer` comprueba las concesiones
  y los motores de tabla admitidos.
- [Aplicación del feed](https://github.com/ClickHouse/loom/blob/main/server/tracker/app/github_feed.py): `initialize`, `configure`,
  `sync` y `_publish` hacen cumplir la propiedad, marcan la privacidad de la publicación y protegen las escrituras.
- [Almacén del registro](https://github.com/ClickHouse/loom/blob/main/server/tracker/store/github_feed.py): `statements` define las
  vistas del contrato; `repository`, `put_repository` y `put_items` leen el estado de publicación
  de forma consistente.
- [Cliente de la API](https://github.com/ClickHouse/loom/blob/main/server/clients/github_feed.py): `pages` acota los barridos del origen;
  las [reglas de dominio](https://github.com/ClickHouse/loom/blob/main/server/tracker/domain/github_feed.py) validan las identidades y
  eligen los límites de reproducción mediante `next_cursor`.

<a id="invariants"></a>

## Invariantes

- El contenido copiado permanece en el registro registrado del equipo, con borrado a cargo del equipo.
- Las comprobaciones de identidad, visibilidad y revisión del repositorio preceden a la publicación de contenido;
  las lecturas de control incluyen las escrituras confirmadas del registro en la topología de almacenamiento admitida.
- El contenido y su clasificación de privacidad comparten una misma versión de fila sin procesar. La privacidad del contrato
  y los UUID incluyen la clasificación de cada componente almacenado, incluso cuando las tablas se retrasan
  de forma independiente.
- Una transición de privado a público restablece los cursores antes de la publicación pública, de modo que los reintentos
  reproducen el contenido retenido sin perder prematuramente su clasificación privada.
- El lease de escritor de todo el registro cubre las escrituras de publicación y de puntos de control. Un envío incierto
  conserva su reclamo hasta que se resuelve; un cursor avanza solo tras escrituras confirmadas.
- Las versiones de contenido en milisegundos enteros aumentan en todos los repositorios del registro,
  de modo que los cuerpos combinados de elemento y comentario siguen siendo visibles para los cursores existentes del tracker.
