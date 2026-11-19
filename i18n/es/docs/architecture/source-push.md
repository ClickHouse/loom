<!-- i18n: source=docs/architecture/source-push.md blob=3114dcae551415671efdfa6f880845f601eb7342 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/source-push.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/source-push.md), which is authoritative.

<a id="document-source-push-notifications"></a>

# Notificaciones push de fuentes de documentos

Requiere `LOOM_KNOWLEDGE_SOURCE_PUSH_ENABLED=true` para solicitar sincronizaciones de fuentes de documentos a partir de las notificaciones del proveedor. Las notificaciones nunca escriben contenido de documentos ni cambian la política de acceso de la fuente. El sondeo normal de 15 minutos sigue activo y es la vía de recuperación ante entregas perdidas. Desactivar push pausa la configuración, las entregas y la renovación, sin impedir el resellado de secretos ni el retiro de la configuración.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `server/knowledge/api/ops_source_push.py` (`OPERATIONS`) declara las operaciones de administración para configuración, confirmación y estado.
- `server/knowledge/app/source_push_routes.py` (`notification`) limita el tamaño de los cuerpos de callback y despacha las entregas del proveedor.
- `server/knowledge/app/source_push.py` (`receive`, `_fresh`, `_put`, `renew_drive`,
  `reseal_registry`) autentica las señales y coordina las escrituras del ciclo de vida del registro.
- `server/knowledge/domain/source_push.py` (`fingerprint`, `verify_drive`,
  `verify_notion`, `verify_linear`) vincula la configuración y verifica la prueba del proveedor;
  `server/knowledge/store/source_push.py` (`get`, `put`) persiste las versiones del registro.
- `server/knowledge/app/sources.py` (`sources_lease`, `deactivate_namespace_sources`)
  coordina los cambios en el ciclo de vida de las fuentes;
  `server/erasure/store/steps_ledgers.py` (`delete_source_push_by_namespace`) liquida el borrado del registro.

<a id="invariants"></a>

## Invariantes

- Las entregas autenticadas solicitan la sincronización a través de la vía de fuentes existente. Las cargas del proveedor no pueden escribir contenido ni ampliar el acceso a la fuente.
- Una entrega debe coincidir con el ID de fuente actual, la generación de configuración y la credencial, el elemento y la huella de configuración. El lease de la fuente y las lecturas secuenciales protegen frente al reemplazo y retiro de la fuente y a respuestas tardías del proveedor.
- Los secretos push usan sellado vinculado a un propósito. Solo un administrador puede revelar un desafío pendiente; las lecturas ordinarias, los sujetos de auditoría y la repetición por idempotencia lo omiten.
- Las escrituras del registro y el borrado de espacios de nombres comparten un lease de escritor con vallado. El borrado elimina todas las versiones almacenadas, y la publicación de la configuración requiere soporte verificado de toda la flota para ese paso de borrado.

<a id="registration-and-authentication"></a>

## Registro y autenticación

El registro push es independiente del registro de fuentes y de los cursores de sincronización. Una fila vincula una organización, un espacio de nombres, un ID de fuente, un proveedor y una generación de configuración aleatoria con una huella de la credencial y los elementos de la fuente. La configuración es una operación de administración. Se requiere una URL base pública HTTPS configurada explícitamente.

La URL de callback incluye en la ruta el espacio de nombres, el ID de fuente y la generación de configuración, y la organización exacta en el parámetro de consulta obligatorio `org`. La codificación de la consulta conserva los nombres de organización que contienen barras o caracteres reservados de URL.
Un callback vuelve a leer el registro y la fuente actual bajo el lease de fuentes del espacio de nombres antes de registrar una solicitud de sincronización. Una fuente eliminada o reemplazada, una credencial modificada, un conjunto de elementos modificado, un espacio de nombres inactivo o una generación retirada hacen que se rechace la entrega. Las cargas y los encabezados de los callbacks no se almacenan ni se registran en logs.

- Los canales de Drive usan IDs de canal aleatorios independientes y tokens de canal sellados. Una entrega debe coincidir con el ID de canal activo, el token y el ID de recurso antes de su expiración. Esto es verificación por token de canal: Drive no firma estas entregas. Las notificaciones `sync` tempranas se confirman sin solicitar trabajo. Las respuestas de watch solo se activan tras volver a comprobar la generación de configuración.
- La configuración de Notion captura un desafío de verificación sellado y acotado para una generación nueva. Solo un administrador puede revelarlo mientras la configuración está pendiente. Tras verificar la suscripción en la interfaz del proveedor, el administrador confirma esa generación exacta y la huella del desafío. Un desafío sin firma no puede activar una suscripción ni solicitar sincronización. Las entregas activas requieren el HMAC del cuerpo sin procesar y la identidad del workspace configurada.
- La configuración de Linear expone primero una dirección de notificación pendiente. El administrador crea allí su webhook y luego confirma la misma generación con su secreto de firma. Las entregas requieren el HMAC del cuerpo sin procesar, la marca de tiempo en milisegundos del cuerpo firmado dentro de un minuto y la identidad de la organización configurada.

La identidad del proveedor se lee durante la configuración con la credencial existente de la fuente. Los cuerpos de las notificaciones tienen un límite de tamaño fijo. Las notificaciones válidas duplicadas se fusionan con la solicitud de sincronización pendiente existente; una sincronización completa en espera se conserva. Los callbacks exitosos responden HTTP 200 dentro de un presupuesto de cuatro segundos del handler.

<a id="drive-coverage-and-renewal"></a>

## Cobertura y renovación de Drive

Un canal de cambios de usuario de Drive no cubre todos los cambios de las unidades compartidas. Por ello, la configuración crea canales para el corpus del usuario y para cada unidad compartida a la que pertenece la cuenta, hasta 19 unidades. Un listado de membresía completo y acotado evita confundir el ID de unidad de un archivo compartido directamente con una membresía: los elementos compartidos directamente con quien no es miembro usan el registro del usuario. La configuración rechaza un conjunto de corpus incompleto o que excede el límite, en lugar de afirmar una cobertura push completa. Mover un archivo a un corpus nuevo puede requerir configurar de nuevo; el sondeo sigue detectando el cambio.

Un worker acotado y desactivado por defecto renueva los canales antes de que expiren. Crea un canal único nuevo, valida la respuesta y la generación de la fuente, y luego lo intercambia bajo el lease de la fuente. Detiene el canal anterior en la medida de lo posible. Una renovación fallida deja disponible el sondeo y muestra el canal expirado en el estado de la configuración.

El catálogo de configuración controla los plazos del trabajo del proveedor, de la detención por canal, de la limpieza total y de la pasada del worker. Sus valores por defecto son 40, dos, cinco y 120 segundos. Una pasada deja de iniciar renovaciones cuando queda tiempo suficiente para un intento del proveedor y dos ventanas de limpieza. La configuración rechaza un trabajo del proveedor más limpieza que pudiera superar la ventana de trabajo de 48 segundos dentro del lease de renovación de 120 segundos, o un presupuesto de pasada que no deje tiempo para iniciar una renovación. El plazo de detención del canal no puede superar la ventana total de limpieza.

<a id="lifecycle-and-storage"></a>

## Ciclo de vida y almacenamiento

El registro está ordenado por organización, espacio de nombres e ID de fuente, de modo que las lecturas por espacio de nombres podan su rango de claves físico. Los recorridos de mantenimiento avanzan en ese mismo orden. El registro almacena la tupla de identidad completa, el actor estampado por el servidor y la procedencia derivada. Los secretos usan sellado vinculado a un propósito y participan en el resellado. Las lecturas ordinarias de fuentes y los sujetos de auditoría nunca incluyen tokens ni desafíos. La lectura de configuración de administración omite el almacenamiento de repetición por idempotencia, de modo que un desafío pendiente revelado no pueda repetirse desde una caché ordinaria tras la activación.

La eliminación de una fuente, la rotación de credenciales, las actualizaciones de elementos y el retiro de un espacio de nombres retiran la configuración push bajo el mismo lease de la fuente. Las respuestas externas tardías de watch no pueden revivir una configuración retirada. El borrado de un espacio de nombres elimina todas las versiones del registro mediante la maquinaria de borrado con liquidación existente. Las escrituras de configuración requieren una flota que entienda el paso de borrado del registro; una flota ilegible las rechaza. Antes de volver a una compilación sin este registro, desactiva push, borra las filas del registro con la compilación actual y verifica que los borrados de espacios de nombres se completen. Los canales del proveedor que no se pueden detener expiran; los callbacks retirados los rechazan.

<a id="alternatives"></a>

## Alternativas

Poner el estado push en la fila de la fuente haría que cada entrega compitiera con las escrituras de credenciales y arriesgaría arrastrar una credencial antigua. Ponerlo en el detalle de sincronización mezclaría secretos de configuración sellados con la salida de inspección ordinaria. El registro separado evita ambos problemas. Como las notificaciones son señales, no hacen falta una cola de eventos duradera, un archivo de cargas del proveedor ni una segunda vía de ingesta.

<a id="provider-contracts"></a>

## Contratos de los proveedores

- [Notificaciones push de Drive](https://developers.google.com/workspace/drive/api/guides/push)
- [Registros de usuario y de unidades compartidas de Drive](https://developers.google.com/workspace/drive/api/guides/about-changes)
- [Verificación de webhooks de Notion](https://developers.notion.com/reference/webhooks)
- [Entrega de eventos de Notion](https://developers.notion.com/reference/webhooks-events-delivery)
- [Verificación de webhooks de Linear](https://linear.app/developers/webhooks)
