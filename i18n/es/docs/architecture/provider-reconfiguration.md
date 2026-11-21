<!-- i18n: source=docs/architecture/provider-reconfiguration.md blob=71fa716069549df892dd0ea521757421e0539568 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/provider-reconfiguration.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/provider-reconfiguration.md), which is authoritative.

<a id="provider-reconfiguration"></a>

# Reconfiguración de proveedores

Loom almacena la configuración de modelos del dashboard como filas cifradas y versionadas en
`loom.provider_configuration`. Una configuración tiene un único predecesor confirmado;
los guardados obsoletos se rechazan. Las sobrescrituras del dashboard tienen prioridad sobre el entorno de
inicio del servidor. Restablecer al entorno restaura esa instantánea de inicio,
no un archivo dotenv recién editado.

<a id="activation-and-admission"></a>

## Activación y admisión

La rotación de claves y los cambios que solo afectan al LLM pueden activarse sin reconstruir los vectores.
Cambiar un proveedor de embeddings, el endpoint, el modelo o las dimensiones crea una transición
en cola. No debe activarse contra vectores del espacio anterior.

El runtime cierra su compuerta local de admisión de trabajo de modelos cuando observa una
transición sin terminar, incluida una transición fallida. El trabajo ya admitido puede
terminar; las nuevas solicitudes del plano de datos reciben una respuesta de mantenimiento
reintentable, y las pasadas de los workers esperan sin consumir su plazo de ejecución. Las rutas de estado, de configuración
de modelos y de control del dashboard siguen disponibles. Una transición confirmada que coincida
permite recalibrar y reabrir la admisión. Una revisión posterior que solo cambie la clave
puede satisfacer esta comprobación mediante su cadena de predecesoras confirmadas.

Esta compuerta cuenta el trabajo dentro de un solo proceso. **No es una barrera distribuida de escritores**
y no demuestra que las escrituras diferidas o que otra réplica se hayan vaciado.

El bucle de refresco ejecuta el coordinador fuera de esa compuerta. Cada proceso del servidor y cada
comando de la CLI nativa del plano de datos se inscribe en `loom._provider_participants` antes de
cargar la configuración confirmada o admitir trabajo. Una transición requiere que cada
participante existente confirme el mismo ID de configuración después de que se hayan vaciado las solicitudes, las pasadas de los workers,
las tareas hijas finitas registradas y las escrituras en búfer. Un proceso recién iniciado
carga la compuerta cerrada pendiente o el reemplazo confirmado antes de atender solicitudes.

Cada participante escribe su fila cada 10 s desde un hilo propio, y cada
cliente de ClickHouse que abre indica su id en el User-Agent. Un participante se considera
detenido solo cuando se retiró a sí mismo (un apagado ordenado), cuando un proceso posterior en su
host demostró que ya no existe, o cuando un operador lo retiró. Un latido expirado no es una prueba: un
proceso en pausa puede mantener trabajo admitido fuera de ClickHouse y escribirlo con la configuración anterior
cuando se reanude, así que la transición espera a que vuelva a latir y
a que se vacíe. Cada fila incluye su nombre de host, el arranque del kernel, el id del contenedor (el de Docker, o el
pod de Kubernetes y el nombre del contenedor), el espacio de nombres de pid, el pid y la hora de inicio; cada 10 s cada
proceso retira a los participantes de su propio host y arranque cuyo pid ya no ejecuta ese proceso, o que se ejecutaron en su
propio contenedor antes de que este se reiniciara en un nuevo espacio de nombres de pid, una vez que ya no se ejecuta ninguna consulta suya. Esto cubre un proceso terminado cuyo
contenedor o worker se reinicia en el mismo lugar; dos contenedores que comparten un nombre de host
(red del host, dos contenedores de un mismo pod) nunca se retiran entre sí. Un operador que haya confirmado que su
proceso ya no existe lo retira con `loom participant retire --participant <id> --yes`,
que rechaza hacerlo si su latido es reciente o si sus consultas siguen en ejecución. El `/v1/health`
del líder de workers lista cada participante por el que esperaría una transición como
`provider_participants_stale` (`heartbeat_expired` o `queries_running`).
`system.processes` se lee mediante `clusterAllReplicas(LOOM_CH_CLUSTER)`; un servidor
sin ese clúster responde por sí mismo, y cualquier otro fallo de la lectura
hace que el participante se considere en ejecución.

Cada paso acotado de reconstrucción mantiene la clave compartida de configuración en Keeper, renovada cada
20 s (TTL de 60 s). Un competidor toma una clave expirada solo cuando el
participante de su titular se considera detenido; un TTL vencido no es prueba por sí solo. `loom
provider-lock clear --holder <holder>` libera una clave cuyo titular no tiene
fila de participante, y la rechaza mientras el titular renueve, emita latidos o tenga una consulta
en ejecución. Las migraciones de arranque nunca se ejecutan bajo esta clave: un arranque la mantiene solo para comprobar
que no queda ninguna reconstrucción sin terminar, después de inscribirse, de modo que una reconfiguración iniciada mientras
migra espera a que termine. Un participante congelado durante más de 60 s puede considerarse detenido;
se vuelve a inscribir con su siguiente latido y cierra su compuerta en su siguiente refresco.

El primer punto de control del coordinador fija la identidad de endpoint/modelo y los anchos de prefijo
configurados válidos. Reiniciar no adopta ajustes de prefijo modificados.
Los clientes de reconstrucción usan solicitudes directas al proveedor con credenciales explícitas, sin pasar por
las cachés del proceso ni las cachés compartidas de servicio. Las solicitudes hermanas terminan su cancelación
antes de que un lote fallido libere la propiedad. La activación borra los vectores de la caché del proceso
y el ancho sondeado anterior antes de reconstruir la calibración de servicio.

<a id="rebuild-staging"></a>

## Preparación de la reconstrucción

El inventario de reconstrucción contabiliza todas las columnas de vectores activas conocidas. Los vectores del historial se conservan; las cachés y los prefijos de código derivados tienen acciones independientes. Las superficies de vectores desconocidas hacen fallar la comprobación previa en lugar de omitirse en silencio.

Las primitivas de preparación implementadas requieren un lease exclusivo del coordinador y escritores drenados:

- La comprobación previa de la fase de snapshot reconcilia las copias parciales por recibo, esquema de origen y UUID antes de cualquier escritura. Las copias de origen sin terminar pueden contabilizarse en el inventario, pero se reconstruyen y nunca se aceptan como completas. Los destinos sin orígenes coincidentes completados y las tablas privadas inesperadas detienen la recuperación.
- Las copias de origen conservan todas las columnas escribibles y asignan ordinales de paginación estables. Un recibo de finalización vincula una copia correcta con el UUID de su origen y la huella de su esquema. Una copia sin confirmar se reconstruye; no se acepta a partir de su recuento de filas.
- Las tablas de reemplazo con clave de modelo seleccionan la revisión más alta para cada clave lógica entre las etiquetas de embedding antiguas. Con revisiones iguales se prefiere una tombstone y, después, la etiqueta del modelo antiguo sirve como desempate determinista. Una revisión activa posterior conserva una restauración explícita. La selección mantiene la fila elegida completa en lugar de mezclar campos agregados de forma independiente. La versión de la política de origen forma parte de la huella del snapshot, de modo que los recibos antiguos no pueden adoptar en silencio una receta modificada.
- La limpieza del snapshot comprueba la propiedad, el UUID y el estado local de las consultas. La inspección de consultas debe usar el mismo nodo ClickHouse fijado que la creación; no es una confirmación de todo el clúster. Los coordinadores antiguos deben quedar aislados antes de la recuperación.
- Las tablas de destino son tablas `ReplacingMergeTree` privadas con clave en el ordinal de origen. Los lotes repetidos tienen una sola fila lógica con `FINAL`. Su marcador de propiedad las vincula a un snapshot de origen y a la huella del espacio de embeddings.
- La generación de vectores usa un embedder de destino suministrado de forma explícita. Valida el lote completo antes de escribir y no modifica los proveedores de todo el proceso.
- La acción de caché invalida solo el destino de caché privado propio y verifica que esté vacío, sin llamar a un proveedor de embeddings. Esto también invalida las entradas antiguas específicas de un endpoint cuando la etiqueta del modelo no cambia. La caché en servicio no se toca hasta que se publique su reemplazo.
- Las páginas del tracker hidratan el texto del título y del cuerpo mediante la puerta de origen existente del espacio de nombres y la receta de indexación normal. La coincidencia incluye tenant, tipo, número de elemento, ID de origen del repositorio y resumen del texto almacenado. Las tombstones no necesitan lectura del origen. Un origen presente cuyo texto cambió actualiza su resumen de texto preparado con el texto autorizado exacto enviado al proveedor de destino. La propiedad y el resto de los metadatos permanecen congelados; las filas del snapshot no cambian. Los orígenes ausentes o inaccesibles detienen la página antes de cualquier llamada al proveedor o escritura de preparación. Las tombstones del snapshot nunca provocan lecturas del origen ni llamadas al proveedor.
- La derivación de prefijos usa vectores de código completos reconstruidos y verificados, conserva sus metadatos dentro de ClickHouse y aplica la regla normal de normalización de prefijos. Los ordinales estables separan las proyecciones configuradas. La verificación compara los multiconjuntos de filas completas de cada etiqueta, no solo los recuentos de filas. Una política de prefijos congelada y vacía elimina los gemelos obsoletos. El manifiesto persiste la política; las solicitudes que no coinciden con ella fallan antes de modificar la preparación. La activación en tiempo de ejecución reconstruye las etiquetas de búsqueda de código y las rutas de tablas a partir de la receta completada, incluso tras rotaciones de claves posteriores. Una huella de proveedor/endpoint que no coincide mantiene cerrada la admisión. Un prefijo habilitado por defecto conserva su ancho si ese ancho se reconstruyó; los anchos ausentes quedan deshabilitados. Las cachés de capacidades de vectores se refrescan antes de que se reabra la admisión.
- Las inserciones preparadas seleccionan los metadatos sin cambios directamente de la copia de origen dentro de ClickHouse. Python solo transporta las actualizaciones relacionadas con embeddings, lo que evita reinterpretaciones de zona horaria y pérdida de precisión de las marcas de tiempo en los viajes de ida y vuelta de metadatos.
- La verificación comprueba la cobertura de filas, la igualdad de metadatos con tratamiento seguro de nulos, la identidad del modelo, las dimensiones de los vectores, que los vectores sean finitos y no nulos, y que las tombstones tengan los vectores borrados. Un resultado de verificación ausente o mal formado no es un éxito.
- La verificación también comprueba la clave de ordenación activa final. Los ordinales de preparación distintos no deben colapsar en silencio cuando las filas del modelo antiguo se reescriben a un único modelo y se publican en un motor de reemplazo o de agregación. Las colisiones restantes hacen fallar la verificación tras la selección canónica de la entrada.

El ejecutor de pasos persiste el progreso solo después de que un efecto se complete. Los efectos deben ser idempotentes porque un proceso puede detenerse después de una escritura en la base de datos pero antes de guardar su checkpoint. Los fallos conservan el último cursor y exponen un código de error fijo; la acción de reintento del administrador vuelve a encolar esa misma transición.

El checkpoint de progreso también incluye un manifiesto de recuperación versionado, con el ID de configuración, la huella del espacio de embeddings, las dimensiones de prefijo congeladas y el inventario completo y ordenado de tablas. Una política de prefijos ausente no se interpreta como una política vacía; las versiones de manifiesto anteriores fallan de forma cerrada en lugar de adoptar los valores por defecto del tiempo de ejecución.
Cada entrada registra el esquema y el UUID originales, además de los UUID del snapshot y del destino. La verificación añade el UUID preparado y el recuento de filas verificado sin cambiar esas identidades congeladas. El ejecutor persiste este manifiesto antes de la publicación y lo pasa a la publicación y a la limpieza. Los manifiestos ausentes, las versiones desconocidas, los inventarios parciales y los UUID reutilizados fallan de forma cerrada; la recuperación no infiere la identidad de una tabla a partir de su nombre. El manifiesto contiene solo metadatos, no credenciales, vectores ni contenido de origen.

El inventario de recuperación excluye una copia privada solo cuando su nombre exacto y su UUID físico coinciden con el manifiesto. Las tablas de vectores desconocidas, las copias de otro trabajo y los reemplazos con el mismo nombre siguen haciendo fallar la comprobación previa. Las copias sin registrar en el journal necesitan reconciliación de recibos antes de esta comprobación; un prefijo de preparación por sí solo nunca omite el inventario. Esta comprobación de inventario no demuestra que todas las copias registradas en el journal sigan existiendo.

Los parches de vectores viajan como datos de archivo multipart JSONEachRow tipados, no como un único campo de parámetro HTTP. Las páginas de tamaño completo pueden superar el límite de campos de formulario de ClickHouse incluso cuando el SQL es pequeño. Solo los ordinales y los campos relacionados con embeddings cruzan esta ruta de escritura; los metadatos sin cambios se unen desde el snapshot dentro de ClickHouse, conservando la precisión de DateTime64. La subida no crea ninguna tabla de parches persistente, y las escrituras síncronas conservan los ajustes normales de reintento y deduplicación.

<a id="publication-primitives"></a>

## Primitivas de publicación

La preparación del candidato clona el esquema activo en una tabla privada y copia
solo las columnas escribibles desde el staging verificado. Comprueba dentro de ClickHouse
tanto el recuento lógico de filas como la igualdad de multiconjuntos de filas completas,
incluidos los nulos y los recuentos de duplicados, antes de registrar la finalización.
Al reanudar un candidato completado se repiten estas comprobaciones; un recuento de filas
igual por sí solo no demuestra que la copia esté intacta. Por ahora, este helper acepta
solo motores MergeTree locales: los motores replicados y compartidos requieren un plan
aparte para la ruta de replicación.

En las tablas con codificación, la preparación conserva el DDL canónico y el método
Quantized y la cadena de codecs existentes, y reemplaza solo la identidad de la tabla,
el comentario de propiedad y el ancho del vector. El candidato se crea según lo que
el servidor admite de la configuración de codecs
([configuración restringida](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)):
se rechaza antes de su `CREATE` cuando el codec no puede activarse (primero se elimina
un candidato obsoleto propio) y se crea sin la cadena cuando `allow_suspicious_codecs`
no puede activarse. Los vectores de staging borrados se convierten en marcadores de
posición de ceros de ancho fijo en estas columnas físicas; los indicadores de dimensión,
las lápidas (tombstones) y el resto de los metadatos no cambian. La verificación de filas
completas compara esta proyección física explícita, no solo un recuento de filas igual.
Los anchos de Rabitq y TurboQuant deben ser múltiplos de ocho. Los argumentos de codec
desconocidos y los índices vectoriales o restricciones creados por el operador requieren
un plan de dimensiones aparte; la preparación falla de forma segura en lugar de eliminar
codecs o publicar un diseño incompatible.

El helper de publicación recibe los UUID duraderos de la tabla original y de la tabla
preparada. Mueve la tabla activa a un nombre de respaldo propio y luego mueve la tabla
preparada al nombre activo. Un reintento reconoce por UUID los estados anterior,
intermedio y completado. No usa `EXCHANGE`: el reintento automático de red del cliente
compartido podría repetir un intercambio tras perder una respuesta y restaurar la tabla
antigua sin querer.

La receta congelada también registra en un diario las vistas materializadas dependientes
con TO explícito y los UUID de sus reemplazos. El DDL de las vistas se cifra porque las
definiciones escritas por el operador pueden contener secretos. Cuando todas las tablas
de reemplazo están activas, la publicación vuelve a crear estas vistas para que las
inserciones futuras se enlacen a la tabla de reemplazo y no al UUID de su respaldo. Si
se pierden las respuestas de DROP/CREATE, se reanuda con las identidades registradas en
el diario; las identidades desconocidas detienen toda la comprobación previa. Las vistas
con datos internos implícitos no son compatibles: eliminarlas descartaría datos. Los
destinos de las vistas con TO explícito se conservan, y el reenlace de las vistas termina
antes de la limpieza de los respaldos. El descubrimiento sigue las matrices
`dependencies_database` y `dependencies_table` de las tablas de origen, no sus
dependencias de carga asíncronas. Los metadatos de vista ausentes y las dependencias
entre bases de datos fallan de forma segura.

Estos cambios de nombre en un solo sentido dejan un hueco temporal en el nombre activo.
Todos los lectores y escritores deben permanecer drenados hasta que cada tabla esté
publicada y verificada. La tabla preparada ya debe tener el esquema, los índices y las
restricciones activos previstos; el destino de staging con clave ordinal no es por sí
mismo un reemplazo publicable. La limpieza de respaldos y la activación del proveedor
son pasos aparte del coordinador.

La limpieza valida cada tabla activa contra el UUID de reemplazo registrado en el diario
antes de eliminar cualquier copia privada. Rechaza las copias desconocidas o reemplazadas
y comprueba las consultas locales y las mutaciones sin terminar. Las instantáneas, los
destinos y los respaldos exactos del diario se eliminan con DROP seguros ante reintentos;
si faltan copias, una limpieza interrumpida puede reanudarse. Un censo final de
identidades no debe encontrar ninguna copia restante de este trabajo. El llamador sigue
siendo responsable de la barrera distribuida y mantiene cerrada la admisión durante todo
el proceso.

<a id="recovery-and-erasure"></a>

## Recuperación y borrado

La activación del runtime local espera a que terminen las solicitudes admitidas y el trabajo registrado en `TaskSet` antes de cambiar los clientes de embeddings y reabrir la admisión. La comprobación no cancela las escrituras diferidas. No es una confirmación distribuida: las tareas no registradas, las escrituras en búfer y las consultas del lado del servidor requieren comprobaciones de vaciado independientes, y la finalización por sí sola no demuestra que una escritura haya tenido éxito.

El backend de almacenamiento concreto conecta los manifiestos de snapshot, la reconstrucción por tabla, la verificación, la preparación del reemplazo, la publicación y la limpieza con el ejecutor de pasos duradero. Recibe un embedder explícito, una política congelada y un callback de barrera; no crea el lease del coordinador ni demuestra por sí mismo la quiescencia distribuida.

Las migraciones de arranque comparten la barrera de configuración del proveedor una vez que existe su tabla. Los reinicios con el mismo esquema omiten el DDL de migración mientras haya una transición pendiente; las actualizaciones con migraciones pendientes deben esperar a que esa transición termine. El DDL de retención se admite a través de la compuerta de trabajo de modelos y se omite en un reinicio de mantenimiento. Aplica un entorno de retención modificado después de la reconstrucción reiniciando de nuevo. Los barridos de ejecuciones inactivas de MCP no inician nuevo trabajo de resumen durante el mantenimiento.

<a id="docker-verification"></a>

## Verificación de Docker

`scripts/gates/docker_quickstart_smoke.py` instala el bundle de release generado
en un proyecto de Compose nuevo y desechable. Comprueba la carga del dashboard, la memoria,
las toolchains de compiladores incluidas, los resultados persistidos del compilador, los reinicios y la ruta documentada
de actualización y recreación del entorno a través del entrypoint de producción por defecto.

Su fase de proveedores usa Chromium para enviar el formulario real de Models & keys, rotar una
clave, cambiar las dimensiones de embeddings, reintentar una reconstrucción fallida y restaurar la configuración
del entorno mediante la interfaz de confirmación. Comprueba que los vectores de memoria y de código no estén
vacíos y que los metadatos se conserven, la coordinación entre dos web workers,
las respuestas de mantenimiento, la recuperación tras una caída,
la recreación de contenedores y la eliminación de las copias privadas de reconstrucción.
La fase de proveedores redirige solo el endpoint HTTP upstream del LLM en un entrypoint de Compose exclusivo para pruebas;
las solicitudes de embeddings usan un fixture de proveedor compatible solo de red. La validación del lado del servidor, la autorización, el cifrado, el coordinador y el almacenamiento de ClickHouse siguen siendo reales. Las credenciales y salidas del fixture no demuestran acceso a un proveedor de pago, calidad del modelo ni comportamiento de un clúster de producción. CI ejecuta esta verificación en amd64 y arm64.

Para una ejecución local, primero instala las dependencias del frontend y Chromium
(`cd frontend && npm ci && npx playwright install chromium`), compila la imagen
de producción y luego ejecuta la verificación con `--image <repository:tag>`. Elimina únicamente los contenedores y volúmenes
del proyecto generado.
Las pruebas unitarias ejercitan esta orquestación con efectos de almacenamiento simulados, no un flujo completo
de cambio de proveedor en Docker.

El coordinador hace avanzar los trabajos en cola a través de snapshot, reconstrucción, verificación,
publicación, limpieza y activación. Los trabajos fallidos permanecen cerrados hasta un reintento explícito; este retoma su cursor durable. El progreso del dashboard muestra la fase, la tabla
y los contadores, no los manifiestos de recuperación ni las recetas privadas.

Las eliminaciones que llegan durante una transición reciben la misma respuesta de mantenimiento reintentable que otras escrituras del plano de datos. Las sagas de eliminación desacopladas iniciadas antes
son tareas finitas registradas y deben terminar antes del snapshot. El censo de residuos también inspecciona cada origen, destino, copia preparada y respaldo de la reconstrucción. Cuenta filas físicas, incluidas versiones antiguas de reemplazo y tombstones; las copias sin columnas de ámbito utilizables se cuentan de forma conservadora en todos los ámbitos. Las copias desconocidas o los recuentos ilegibles hacen fallar la verificación en lugar de interpretarse como cero.
Las copias se eliminan en la limpieza de la reconstrucción, no se editan bajo su receta congelada.
Por tanto, una reconstrucción interrumpida no puede dejar un snapshot fuera del
registro de eliminaciones ni informar de un borrado mientras una copia pueda resucitar los datos del sujeto.

<a id="current-implementation-boundary"></a>

## Límite de la implementación actual

Por ahora, la publicación admite motores MergeTree locales en un único nodo ClickHouse fijado. La preparación de motores replicados o compartidos y la inspección de consultas residuales en todo el clúster todavía necesitan su propio plan compatible. Por eso, un guardado o un restablecimiento que cambie el espacio de embeddings se rechaza con 409 mientras alguna tabla de reconstrucción use otro motor y mientras alguna tabla de re-embed contenga filas, antes de que se ponga en cola una transición o se cierre la admisión. La respuesta de estado incluye el mismo motivo en `embedding_change_blocked`.
Si no hay vectores almacenados, no hay nada que reconstruir, sea cual sea el almacenamiento: el cambio se confirma como activo y cada proceso se vacía y luego cambia mediante su actualización de configuración. Las filas que un proceso escribe antes de cambiar conservan los vectores del modelo anterior. El operador debe detener a los escritores directos de la base de datos que no pasen por las rutas registradas del servidor o de la CLI de Loom; un censo de procesos de Loom no puede aislar a un cliente SQL externo arbitrario. Un participante remoto perdido sin prueba de muerte es un límite de recuperación explícito, no un permiso para ignorarlo. La validación E2E completa en Docker del cambio de proveedor y de la recuperación tras fallos sigue siendo un requisito previo para la fusión; la orquestación en pruebas unitarias y las pruebas a nivel de almacenamiento no equivalen a ese flujo.
