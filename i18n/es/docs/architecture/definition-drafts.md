<!-- i18n: source=docs/architecture/definition-drafts.md blob=6830be6e2eecf1912353fa8f9194619554e027d5 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/definition-drafts.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/definition-drafts.md), which is authoritative.

<a id="definition-drafts"></a>

# Borradores de definiciones

Requiere `knowledge_gaps_enabled` para los borradores de brechas o `knowledge_query_history_enabled` para
el historial de consultas. Ambas rutas colocan las definiciones escritas por el modelo en la cola de revisión de administradores.
El modelo nunca publica una definición. Esta página describe la evidencia, los candidatos privados
y la revisión; [brechas de conocimiento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/knowledge-gaps.md) describe el registro y la agrupación de búsquedas.

<a id="evidence-and-access"></a>

## Evidencia y acceso

La acción **Draft definition** de la Library llama a `knowledge.gaps.draft`. Vuelve a leer las
preguntas de la brecha contra la ventana actual de fallos de siete días y exige al menos
`knowledge_gaps_min_askers` solicitantes distintos para cada pregunta. Envía al modelo solo esas
preguntas normalizadas. El consentimiento de captura se lee sin caché, y si el consentimiento no está disponible se rechaza el borrador. Un cambio de umbral o el borrado de un usuario durante el trabajo invalida
la evidencia y elimina las versiones candidatas de ese intento.

`knowledge.query_history.add`, `.list` y `.remove` requieren acceso de administrador del espacio de nombres. En la
Library, **Query history** abre la lista de fuentes y el formulario de conexión. Un registro tiene
un nombre, host, puerto, opción de TLS, cuenta de solo lectura, lista de bases de datos permitidas y un clúster opcional.
Su contraseña se sella en reposo y nunca se devuelve. La conexión reutiliza las comprobaciones de host del tracker, la dirección fijada y la revisión de permisos de solo lectura. Un clúster también necesita `REMOTE`.
Eliminar un registro detiene los turnos posteriores sin borrar un borrador o una definición existentes. La desconexión sigue disponible cuando el operador deshabilita el historial de consultas.

El worker lee consultas `SELECT` iniciales y terminadas cuya lista de bases de datos no vacía
esté íntegramente dentro de la lista permitida del registro. Un clúster con nombre usa `clusterAllReplicas`;
de lo contrario, solo se lee el nodo conectado. Los grupos usan `normalized_query_hash` y
`normalizedQueryHashKeepNames`, ordenados por cuentas `initial_user` distintas y por actualidad.
El segundo hash mantiene separados los nombres de tabla con distinta numeración sin dividir las variantes de espacios en blanco
o de literales. `normalizeQueryKeepNames` elimina los comentarios y reemplaza los literales,
conservando los identificadores. Solo este SQL normalizado llega al modelo.

Un grupo debe estar respaldado por al menos `knowledge_query_history_min_users` cuentas, y nunca por menos
de tres. Las cuentas no son personas: una cuenta compartida cuenta una vez, mientras que varias cuentas de una misma persona
cuentan por separado. Este mínimo no ofrece ninguna garantía de anonimato. Un aumento de umbral en caliente o un cambio de la ventana del historial invalida un resultado en curso. La eliminación de la fuente, un UUID de fuente modificado, la deshabilitación de la función y el borrado del espacio de nombres también lo invalidan.

Los candidatos usan `source_kind = knowledge_gap` o `query_history`. Su texto escrito por el modelo y el SQL del historial de consultas
siguen siendo privados para los administradores del espacio de nombres en `learnings.list`,
los recuentos de calidad y el linaje, tanto explícito como inferido. El filtrado precede a la paginación y a la agregación, incluidas las lecturas de calidad de toda la organización. Los tokens con lectura restringida no pueden llamar a ninguno de los dos prefijos de operación.

<a id="one-model-attempt"></a>

## Un intento del modelo

El ID del candidato se deriva de la organización, el espacio de nombres, el tipo y el recurso de evidencia.
Una brecha usa su ID de brecha; el historial de consultas usa el UUID del registro y ambos hashes de consulta.
Una concesión breve del candidato persiste una reclamación pendiente antes de liberar la concesión para
la llamada al modelo. Las solicitudes concurrentes y posteriores devuelven ese ID de candidato, incluso después de
una decisión, un error del modelo, un resultado no válido o una caída. Tales fallos conservan una reclamación pendiente
cuyo fundamento indica al administrador que la rechace y escriba la definición explícitamente. Una retención de política antes de la
generación elimina solo las versiones de la reclamación no gastada, de modo que un reintento pueda continuar cuando se permita.

El interruptor de llamadas al modelo de la organización y el límite diario del espacio de nombres se comprueban antes de reclamar el trabajo;
un límite ilegible lo rechaza. Cada intento permite una única finalización JSON acotada. El
resultado valida el término, los alias, el cuerpo y los tamaños de las listas de tablas y columnas. La procedencia de la brecha en
`rests_on` contiene el ID de la brecha y las listas de tablas y columnas del modelo. Las preguntas elegibles llegan
solo a la entrada del modelo; la aplicación no las copia en el candidato. El modelo
puede repetir una pregunta en su texto redactado. Tras la generación, el borrador y el ID de la brecha pueden
sobrevivir al borrado por parte de un usuario de las preguntas de origen. La procedencia del historial de consultas conserva su
SQL normalizado y las listas de tablas y columnas del modelo. Una concesión final del candidato vuelve a leer el estado para que
la finalización no pueda reemplazar un rechazo ni restaurar texto depurado. Las barreras de evidencia se
ejecutan antes y después del trabajo del modelo y tras la inserción.

El worker limita fuentes, grupos, intentos, bytes de consulta, filas, memoria y tiempo de ejecución. No inicia más grupos tras la mitad del presupuesto del ciclo y guarda el UUID de la última fuente antes de su turno, de modo que una fuente defectuosa no impida la rotación. El UUID de tamaño fijo
también evita que los nombres de las fuentes queden en el cursor persistido. Los fallos de conexión usan un interruptor de circuito acotado
por fuente. Los eventos de coste del worker conservan la atribución al espacio de nombres.

<a id="review-and-publication"></a>

## Revisión y publicación

`learnings.review` acepta estos candidatos solo para el destino `memory`. Antes de
escribir, comprueba la capacidad de la flota, la sensibilidad, el techo de definiciones compartidas y los conflictos de nombres. Un borrador revisa la definición compartida activa cuyo término o alias normalizado coincide con
su término; las coincidencias ambiguas se rechazan. Un término nuevo usa la clave aleatoria `definition/` persistida
con la reclamación. Las filas compartidas se vuelven a leer antes de escribir para que un cambio de nombre concurrente no pueda
redirigir la escritura a otro término.

La revisión escribe una definición derivada, válida para todo el espacio de nombres, mediante `memory.set`, borrando
los límites de expiración y de validez. Una cabecera exacta, activa y no expirada no necesita escritura, incluso si está fijada o bloqueada. Un valor modificado rechaza una cabecera fijada o bloqueada. Una revisión requiere
que el texto de origen actual coincida con la copia compartida; la edición intermedia de un escritor provoca un rechazo.
Las cabeceras expiradas retenidas usan su hash para una reescritura condicional, y una cabecera ausente usa
`if_match_sha256="absent"`. Los llamadores condicionales se serializan sobre la clave; las escrituras concurrentes ordinarias en ClickHouse
se resuelven por versión. Estas comprobaciones no hacen atómicas la escritura y la promoción.

La revisión llama a `memory.promote` con el hash del valor canónico aprobado. El servidor
aporta internamente la procedencia del candidato y estampa el actor autenticado. Un cliente
no puede aportar procedencia de confianza ni elegir el actor que decide mediante `approver`.
La promoción publica la definición completa, incluido el SQL normalizado, a los lectores del
espacio de nombres, por lo que el administrador aprueba ese texto para todos ellos.

Una escritura confirmada seguida de un fallo de publicación o promoción deja una definición ordinaria
para todo el espacio de nombres y un candidato pendiente. La recuperación ordinaria puede leer esa fila.
Un reintento promueve una cabecera exacta sin cambios sin reescribirla. Rechazar el candidato
deja intacta la fila de origen. No hay deshacer automático: podría sobrescribir la revisión de metadatos con el mismo texto de un escritor posterior. El administrador puede restaurar el texto deseado mediante el historial de memoria y las operaciones de escritura, o eliminar explícitamente una versión exacta.

<a id="erasure-audit-and-upgrades"></a>

## Borrado, auditoría y actualizaciones

El borrado del espacio de nombres elimina `loom.query_history_sources` mediante
`query_history_sources.delete`. El registro contiene configuración y una credencial sellada, por lo que se clasifica bajo ese paso sin una fila de censo de contenido de memoria.
Los candidatos siguen la cobertura de borrado de la cola de learnings. Una redacción histórica de definición
también depura los candidatos privados coincidentes por el hash del texto canónico o por las etiquetas de candidato de la versión redactada, en todas las versiones del candidato. Usa la concesión de revisión, rechaza una revisión posterior de texto en blanco y conserva una cabecera activa que aún contenga el texto redactado.

La auditoría de la revisión identifica al actor autenticado que decide y al candidato. Los `memory.set` y `memory.promote` internos emiten sus propios eventos de auditoría; el evento de promoción es también
el ID de solicitud de auditoría que sigue `memory.lineage`. Las mutaciones de fuentes auditan el nombre del registro con su alcance, nunca su contraseña.

La producción y la promoción de tipos privados requieren que todos los latidos de servidor y worker activos
anuncien la migración del registro. Un estado de flota ausente o ilegible falla de forma cerrada.
Aplique la migración y actualice todos los servidores y workers antes de habilitar cualquiera de las dos rutas de borradores. Mantenga ambas rutas deshabilitadas mientras haya alguna compilación en servicio más antigua o desconocida. Una compilación anterior no filtra los candidatos privados ni borra los registros. Las comprobaciones de flota por sí solas no protegen un rollback: una solicitud puede pasar su comprobación antes de que aparezca un latido más antiguo.

El rollback requiere una ventana de mantenimiento antes de que arranque cualquier lector anterior:

1. Deshabilite `knowledge_gaps_enabled` y `knowledge_query_history_enabled`. Detenga las nuevas solicitudes de borradores privados, de revisión y de registro de fuentes, así como los turnos del worker del historial de consultas. Deje inactivo su trabajo en cola y toda llamada al modelo, revisión, registro y eliminación admitidos. Deshabilitar un indicador no cancela una solicitud ni una revisión existentes.
2. Deje que esas operaciones terminen o finalicen, y luego resuelva sus envíos inciertos del servidor
   y las concesiones de escritor retenidas dentro de sus límites. No limpie las tablas mientras una
   operación admitida aún pueda escribir un candidato, una decisión o un registro.
3. Con solo una compilación compatible en ejecución para el mantenimiento, elimine todas las versiones de
   los candidatos `knowledge_gap` y `query_history` de `loom.learnings_queue`, y todos los registros de
   `loom.query_history_sources`. Espere a que termine la eliminación y verifique que ambos estén ausentes en cada réplica de almacenamiento que un lector pueda usar. Conserve los candidatos ordinarios y las definiciones publicadas. Solo entonces inicie un lector anterior.

Mantenga deshabilitados los productores privados durante toda la degradación de versión. Vuelva a habilitarlos solo cuando
todos los procesos en servicio admitan la migración del registro. Las filas de memoria gobernada y compartidas conservan sus reglas de actualización existentes.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `server/memory/app/definition_drafts.py`: reclamaciones duraderas y generación acotada.
- `server/memory/app/definition_review.py`: compuerta de flota, promoción y redacción de candidatos.
- `server/knowledge/app/gap_drafts.py`: elegibilidad actual de las brechas y consentimiento.
- `server/knowledge/app/query_history_sources.py`: comprobaciones de registro y credenciales.
- `server/knowledge/store/query_history_remote.py`: lecturas agrupadas del registro de consultas, de solo lectura.
- `server/knowledge/workers/query_history.py`: rotación acotada de fuentes.

<a id="invariants"></a>

## Invariantes

- Solo un administrador puede leer un candidato privado o publicarlo mediante la revisión.
- Ninguna finalización del modelo reemplaza la decisión sobre un candidato ni resucita evidencia borrada.
- Un registro de fuente no puede sobrevivir al borrado del espacio de nombres y seguir produciendo borradores.
- Una promoción rechazada o interrumpida no deshace la fila de un escritor.
