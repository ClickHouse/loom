<!-- i18n: source=docs/architecture/google-adk-adapter.md blob=6c709e61102ee7413e9b4764bfd5c80f4511830e model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md), which is authoritative.

<a id="google-adk-memory-service"></a>

# Servicio de memoria de Google ADK

El adaptador de Google ADK implementa el servicio de memoria del framework a través de los SDK de Python y TypeScript de Loom. Los paquetes del framework son dependencias opcionales. Cada aplicación usa un espacio de nombres de Loom distinto, porque sus documentos de conocimiento se comparten con todos los lectores de ese espacio de nombres.

Las escrituras de sesión extraen texto de los eventos activos del usuario y del modelo, excluyendo las cargas de herramientas y los pensamientos, y luego llaman a `memory.set_from_messages` en lotes acotados. Ambos SDK rechazan ids de usuario vacíos, centinela o con espacios en los extremos antes de realizar cualquier llamada. Cada llamada incluye al usuario final y una sesión derivada del usuario y del id de sesión nativo. Python también implementa los métodos de eventos incrementales, memoria directa y eliminación del framework instalado. Los ids de eliminación de Python incluyen la clave, la sesión, el escritor y la versión exacta. Llaman directamente a `memory.delete_version`. Una versión inexistente o reescrita no produce ningún efecto, y nunca se selecciona la fila de otro escritor. El campo de respuesta adicional `agent` en los resultados de búsqueda de memoria proviene de la identidad de la fila almacenada, incluidas las páginas aparcadas. Un resultado sin escritor o versión conocidos no tiene id de eliminación. Los clientes antiguos ignoran el campo adicional; la eliminación exacta en ADK requiere un servidor que lo proporcione. Esto no añade ninguna columna persistida ni migración. TypeScript devuelve ids informativos acotados al escritor; la interfaz del framework instalada no tiene un método de eliminación.

La búsqueda primero llama a `memory.search` para el usuario vinculado y después rellena los espacios de resultados disponibles con `knowledge.search`, acotada al espacio de nombres. El conocimiento es contexto compartido de la aplicación, no memoria privada del usuario. Los dos SDK usan la misma codificación de digest; la representación nativa de marcas de tiempo del framework se convierte en el límite. Una instancia del servicio está vinculada a una aplicación. Los permisos por espacio de nombres aíslan aplicaciones que no confían entre sí.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `sdk/python/loom/integrations/google_adk.py`: `LoomMemoryService`.
- `sdk/typescript/src/integrations/google-adk.ts`: `LoomMemoryService`.

<a id="invariants"></a>

## Invariantes

El servidor proporciona la procedencia del actor y de la fuente a través de su ruta de escritura habitual. El adaptador no añade ninguna base de datos, esquema ni proveedor. Se aplican las reglas existentes de eliminación de memoria, borrado de conocimiento y actualización progresiva. La extracción semántica puede revisar hechos; este servicio no es un almacén exacto de transcripciones. Los resultados de conocimiento no tienen id de memoria de usuario y no pueden eliminarse a través del servicio de memoria.
