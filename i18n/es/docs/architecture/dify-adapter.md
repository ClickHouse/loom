<!-- i18n: source=docs/architecture/dify-adapter.md blob=34e577e662d3664872b0d8163d92fc018b884cd1 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md), which is authoritative.

<a id="dify-tools-adapter"></a>

# Adaptador de herramientas de Dify

El plugin de Dify llama a Loom a través del SDK de Python y no almacena contenido de forma local.
Sus tres herramientas nativas se corresponden con `memory.set`, `memory.search` y `knowledge.search`.
El paquete independiente opcional requiere la serie menor 0.11 del SDK de plugins y el
SDK de Loom 1.x; el servidor y el SDK principal no dependen de Dify.

<a id="invariants"></a>

## Invariantes

Las credenciales del proveedor fijan el endpoint, la organización, el espacio de nombres, el token y el agente.
Un parámetro de formulario obligatorio, User ID, vincula al usuario final fuera de los argumentos del modelo.
La recuperación puede leer las memorias de ese usuario entre distintos agentes de Loom; los documentos se comparten por
espacio de nombres. Ambas operaciones de búsqueda fijan `namespace_scope=this`. Los espacios de nombres separados
y las concesiones de token limitan las aplicaciones que no confían entre sí. Loom aplica a estas llamadas sus reglas habituales de
actor, procedencia, gestión de conflictos, esquema progresivo y borrado.

El administrador controla el destino HTTP. El plugin comprueba la forma de su URL
y rechaza credenciales incrustadas, consultas y fragmentos; la política de red del despliegue
limita a qué hosts puede llegar el ejecutor del plugin. Las solicitudes tienen tiempos de espera finitos
y un solo intento. Los errores de las herramientas no exponen credenciales ni detalles de la respuesta remota.
El plugin no crea ninguna ruta adicional de base de datos, caché, proveedor de modelos ni telemetría.

Dify parchea el threading de Python con greenlets de gevent. Cada solicitud construye, invoca
y cierra su cliente del SDK dentro del pool de hilos nativos del hub actual, de modo que los clientes
superpuestos no ejecutan bucles de asyncio en el mismo hilo nativo. Las credenciales permanecen locales a
esa solicitud. La llamada al SDK espera como máximo 30 segundos; la admisión en el pool, la configuración del cliente y
el cierre tienen esperas separadas en lugar de un plazo para toda la invocación.

Dify instala el paquete desde un `.difypkg` local. La CLI oficial empaqueta el código fuente,
los metadatos, los requisitos de ejecución, los iconos, la licencia, el README y la declaración de privacidad. Su
archivo de exclusión deja fuera las pruebas, los entornos, las credenciales y el estado de compilación. La CI invoca
las herramientas nativas contra un transporte HTTP falso del SDK, comprueba las llamadas nativas superpuestas
mediante HTTP de loopback, valida los esquemas de las herramientas nativas y compila el paquete.
Las pruebas en vivo solo se ejecutan con una URL privada de Loom y un token
indicados de forma explícita; la ejecución en la plataforma se documenta aparte de esas pruebas del SDK.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `integrations/dify/client.py`: llamadas al SDK con alcance y fallos de solicitud.
- `integrations/dify/provider/loom.py`: validación de credenciales.
- `integrations/dify/tools/`: implementaciones y esquemas de las herramientas nativas.
- `.github/workflows/dify-plugin.yml`: pruebas y empaquetado.
