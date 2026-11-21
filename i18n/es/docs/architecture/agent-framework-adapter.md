<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=6a5670ddd5050886dcda1b0bf590a83063c1e4aa model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Proveedor de contexto de Microsoft Agent Framework

`LoomContextProvider` implementa la interfaz de proveedor de contexto del framework sobre
el SDK asíncrono de Python. El extra opcional admite solo la serie menor del framework que se ha probado; el SDK principal y el servidor no importan el framework.

Antes de una ejecución, el proveedor busca el último texto del usuario y añade los hechos coincidentes
a las instrucciones del framework. Después de la ejecución, envía el texto nuevo del usuario y del agente a
`memory.set_from_messages` en lotes acotados. Ejecuta ese hook una vez por
turno, incluido un bucle de middleware del framework. El framework conserva su propio historial;
este proveedor almacena hechos duraderos en lugar de reemplazar a un proveedor de historial.

<a id="invariants"></a>

## Invariantes

El id de usuario y el id de sesión del framework derivan una sesión de Loom, lo que evita que dos sesiones de usuarios distintos
con el mismo nombre compartan fragmentos extraídos. Si se omite el usuario, se leen todas las memorias del espacio de nombres que el token permite y se escriben
filas compartidas. Los ids de usuario explícitos no pueden tener espacios en blanco al principio ni al final. Las concesiones del espacio de nombres autorizan el acceso; un parámetro de usuario es un filtro.
El servidor aporta la procedencia del actor y de la fuente y aplica las reglas habituales de conflicto,
borrado y esquema progresivo. No se añade ninguna base de datos, caché ni historial local.
Los errores se propagan a través de los hooks de proveedor del framework, incluidos los errores posteriores a
una respuesta del modelo. Las escrituras concurrentes siguen las reglas normales de conflicto del servidor.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`.
- `sdk/python/tests/test_agent_framework.py`: comportamiento del proveedor nativo y del agente.
- `tests/integration/test_sdk_agent_framework_memory.py`: extracción y recuperación en vivo.

[Configuración del framework](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
