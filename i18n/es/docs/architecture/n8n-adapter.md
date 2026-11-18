<!-- i18n: source=docs/architecture/n8n-adapter.md blob=f2efbe08cacbe3e9b1bb2cb39161b71c20e2be32 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md), which is authoritative.

<a id="n8n-adapter"></a>

# Adaptador de n8n

El paquete de la comunidad de n8n almacena el historial de conversaciones y los hechos duraderos en un despliegue de Loom mediante el SDK de TypeScript. Los nodos no añaden ningún servicio de almacenamiento ni caché local.

<a id="interfaces-and-storage"></a>

## Interfaces y almacenamiento

`LoomMemory.supplyData` devuelve el `BufferWindowMemory` del framework, respaldado por una implementación `LoomChatHistory` de `BaseListChatMessageHistory`. La serialización de los mensajes usa el formato de mensajes almacenados del framework. La ventana cambia el contexto que se envía a un modelo, mientras que los mensajes más antiguos siguen disponibles para lecturas posteriores y para el borrado explícito.

Cada mensaje es una memoria `episodic`, con una marca de tiempo y un sufijo aleatorio en su clave. Las escrituras episódicas conservan los mensajes repetidos sin fusión semántica. La organización, el espacio de nombres y el nombre del agente de Loom delimitan el historial. El usuario y un resumen (digest) de los identificadores del workflow y de la sesión aportan los demás campos de identidad. Las lecturas recorren páginas con `memory.list`, eliminan duplicados de las páginas solapadas cuando se filtran las filas proyectadas, ordenan por clave y usan `memory.get` para los valores truncados. `clear` elimina solo esos elementos. Los escritores concurrentes no tienen una transacción que abarque un turno completo.

El nodo de acciones de Loom llama a `memory.set`, `memory.search` y `knowledge.search`. Sus entradas aportan la organización, el espacio de nombres, el nombre del agente y, de forma opcional, el usuario. El servidor deriva la procedencia a partir del token y de la operación, y aplica los permisos del espacio de nombres.

<a id="packaging"></a>

## Empaquetado

El paquete incluye el SDK y las bibliotecas del framework para evitar conflictos de dependencias en tiempo de ejecución con otros nodos de la comunidad. Su compilación incluye los archivos de licencia de esas bibliotecas. `n8n-workflow` es la dependencia peer que aporta el host. Las dependencias de desarrollo fijan las interfaces que usan las pruebas; no se añaden a las dependencias del servidor ni del SDK de Loom.

El paquete está dirigido a n8n autoalojado, con la configuración de la CLI oficial de nodos para ese destino. No afirma haber sido verificado en Cloud. El workflow de etiquetas de versión compila y comprueba el paquete antes de publicarlo con un publicador de confianza de npm y con provenance; no se almacena ningún token del registro en el repositorio.

<a id="invariants"></a>

## Invariantes

- El adaptador envía datos solo a la URL de Loom indicada en su credencial de n8n.
- Los permisos del token autorizan el acceso; un parámetro de usuario es un filtro, no una autorización.
- El borrado de la conversación no elimina los hechos duraderos creados por el nodo de acciones.
- Los rechazos del SDK o del servidor hacen fallar el nodo, o se convierten en elementos de error con continue-on-fail.
- El adaptador no crea tablas y usa las rutas de borrado que ya existen en Loom.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `integrations/n8n/nodes/LoomMemory/LoomChatHistory.ts`: persistencia de mensajes.
- `integrations/n8n/nodes/LoomMemory/LoomMemory.node.ts`: adaptador de memoria del framework.
- `integrations/n8n/nodes/Loom/Loom.node.ts`: acciones de memoria duradera y de documentos.
- `.github/workflows/n8n-publish.yml`: comprobaciones y publicación de confianza.

[Configuración del framework](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n)
