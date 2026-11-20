<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=d7c22dbc73bf2a6dd64de2cb8471a3fafc9d76ba model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Proveedor de contexto de Microsoft Agent Framework

`LoomContextProvider` implementa la interfaz de proveedor de contexto del framework sobre
el SDK asíncrono de Python. El extra opcional admite solo la serie de versiones menores del framework que se ha probado; el SDK principal y el servidor no importan el framework.

Antes de una ejecución, el proveedor busca el último texto del usuario y añade los hechos coincidentes
a las instrucciones del framework. Después de la ejecución, envía el texto nuevo del usuario y del asistente
a `memory.set_from_messages` en lotes acotados. Ejecuta ese hook una vez por
turno, incluido un bucle de middleware del framework. El framework mantiene su propio historial;
este proveedor almacena hechos duraderos en lugar de reemplazar a un proveedor de historial.

<a id="invariants"></a>

## Invariantes

El id de usuario y el id de sesión del framework derivan una sesión de Loom, lo que evita que las sesiones con el mismo nombre de dos usuarios compartan fragmentos extraídos. Si se omite el usuario, se leen todas las memorias del espacio de nombres que el token permite y se escriben
filas compartidas. Los ids de usuario explícitos no pueden tener espacios en blanco al principio ni al final. Las concesiones del espacio de nombres autorizan el acceso; un parámetro de usuario es un filtro.
El servidor proporciona la procedencia del actor y de la fuente y aplica las reglas habituales de conflicto,
borrado y esquema progresivo. No se añade ninguna base de datos, caché ni historial local.
Los errores se propagan a través de los hooks del proveedor del framework, incluidos los errores posteriores a
una respuesta del modelo. Las escrituras concurrentes siguen las reglas normales de conflicto del servidor.

<a id="conversation-protection"></a>

## Protección de la conversación

Un proveedor atiende a una familia lógica de conversación. Conserva los handles de protección
propiedad de la aplicación presentes en la construcción y en la entrada del callback, incluidos los contribuyentes
ambientales y cada handle de cliente enlazado posteriormente. Los ids nativos de usuario y de sesión enrutan
las solicitudes; no crean ni reemplazan esta autoridad. La restauración reabre la cápsula existente
antes de construir el proveedor.

La recuperación y la extracción propias se ejecutan en el contexto de captura automática del SDK. El transporte
vuelve a comprobar cada familia conservada después de las credenciales y antes del envío, observa las respuestas
antes de la conversión y marca de forma duradera a todos los contribuyentes antes de liberar una respuesta marcada.
La primera recuperación marcada puede llegar al modelo. El trabajo automático posterior se rechaza, y cada lote
de extracción se comprueba antes de que pueda iniciarse el siguiente. Un rechazo posterior a una respuesta del modelo
se propaga a través del ciclo de vida nativo del proveedor; no puede deshacer la respuesta ni los efectos de las herramientas.
El proveedor no añade ninguna declaración de capacidad ni calificación de host restaurado.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`.
- `sdk/python/tests/test_agent_framework.py`: comportamiento del proveedor nativo y del agente.
- `tests/integration/test_sdk_agent_framework_memory.py`: extracción y recuperación en vivo.

[Configuración del framework](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
