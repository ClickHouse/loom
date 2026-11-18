<!-- i18n: source=docs/getting-started/models-and-keys.md blob=52104cd19c972224d162911f3102d1fff28bd3b8 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md), which is authoritative.

<a id="configure-models-and-api-keys"></a>

# Configurar modelos y claves de API

Tu agente de programación, el LLM interno de Loom y el modelo de embeddings de Loom son elecciones independientes.
Puedes conectar Claude Code a un servidor de Loom que use modelos de OpenAI, o Codex a un servidor
que use Claude. El agente no necesita usar el mismo proveedor que Loom.

| Elección                                         | Qué hace                                                               | Dónde configurarla                                         |
| ------------------------------------------------ | ---------------------------------------------------------------------- | ---------------------------------------------------------- |
| Tu agente: Claude Code, Codex u otro cliente     | Ejecuta tu flujo de trabajo de programación o de asistente y llama a las herramientas de Loom. | En ese cliente; conéctalo con un token de Loom.            |
| El LLM de Loom                                   | Extrae hechos, resume sesiones y genera respuestas dentro de Loom.     | Los ajustes de proveedor, modelo y clave de API del servidor. |
| El modelo de embeddings de Loom                  | Hace que las memorias, los documentos y el código se puedan buscar por significado. | El modelo de embeddings, las dimensiones y la clave del proveedor del servidor. |

`LOOM_API_KEY` es un token para acceder a **Loom**. Es distinta de
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` y `GEMINI_API_KEY`, que pagan las llamadas del servidor
a los modelos. Mantén las claves de los proveedores en el servidor; conectar un agente no transfiere a Loom
sus credenciales de suscripción ni sus claves de API.

<a id="what-works-without-provider-keys"></a>

## ¿Qué funciona sin claves de proveedor?

La instalación publicada con Docker genera embeddings con `local` (bge-small, 384 dimensiones, en CPU a partir de archivos que incluye la imagen) y selecciona el LLM `fake-echo-1`, a menos que el instalador haya configurado un servidor de modelos local ([un servidor de modelos en tu máquina](#a-model-server-on-your-machine-no-api-key)). Arranca sin claves de proveedor ni red: funcionan el almacenamiento, el dashboard, la indexación estructural de código y la búsqueda semántica, aunque un embedder alojado recupera mejor en preguntas con muchas paráfrasis. El LLM falso no produce hechos extraídos, resúmenes ni respuestas generadas que tengan sentido. El `.env` de un checkout selecciona embeddings `fake-ngram-3072`, que coinciden por solapamiento de caracteres. Una instalación actualizada conserva el embedder que indica su `~/.loom/.env`; cambiarlo a `local` es un cambio de modelo como cualquier otro y vuelve a generar los embeddings de lo almacenado.

Para una recuperación más sólida sobre contenido con muchas paráfrasis, configura un proveedor de embeddings alojado. Para la extracción automática de hechos, los resúmenes y las respuestas generadas, configura también un LLM. Un LLM por sí solo no reemplaza a un modelo de embeddings; un modelo de embeddings por sí solo no genera respuestas. Si no hay ningún embedding configurado, el servidor de un checkout usa su embedder falso; no omite el embedding.

Algunos proveedores alojados también recurren a modelos falsos cuando falta su clave. Revisa `fake_embedding`, `fake_llm`, `providers_degraded` y `problems` en `/v1/health`, y luego guarda y recupera un hecho de prueba e intenta obtener una respuesta. Una clave no válida o un modelo inaccesible aún pueden hacer que las llamadas al proveedor fallen; que el servidor esté en ejecución no prueba que el acceso a los modelos funcione. Usa `LOOM_REQUIRE_REAL_PROVIDERS=true` en despliegues basados en variables de entorno para que los proveedores falsos o sin clave se consideren una condición de salud degradada.

<a id="choose-a-setup"></a>

## Elegir una configuración

| Lo que tiene su organización                          | LLM de Loom                       | Embeddings                                                          |
| ----------------------------------------------------- | --------------------------------- | ------------------------------------------------------------------- |
| Acceso a la API de OpenAI                             | Un modelo de OpenAI disponible    | OpenAI; una sola clave de API puede servir para ambos roles.        |
| Anthropic más un proveedor de embeddings aprobado     | Un modelo Claude disponible       | OpenAI, Gemini o un endpoint compatible, con su clave.              |
| Solo acceso a la API de Anthropic                     | Un modelo Claude disponible       | Añada un proveedor de embeddings y su clave de API.                 |
| Sin clave de proveedor; un servidor de modelos en la máquina | Un modelo local (`local`)  | El embedder `local` que incluye la imagen.                          |

**Una sola clave de API de OpenAI basta para ambos roles; no se requiere una clave de Anthropic.**
En Models & keys, elija un LLM principal y deje en blanco las sobrescrituras por tarea. Todas las
tareas de LLM habilitadas usan ese modelo, incluidas la verificación y la evaluación. Las
sobrescrituras avanzadas son opcionales y solo requieren la clave de otro proveedor cuando usted
selecciona uno de forma explícita.

Elija OpenAI, Google Gemini o un **endpoint de embeddings compatible con OpenAI**. Los embeddings de OpenAI usan
un modelo de embeddings como `text-embedding-3-large`, no un modelo de chat GPT.
Claude puede encargarse de las tareas de LLM de Loom, pero requiere un proveedor de embeddings y una clave por separado.
Anthropic no ofrece su propio modelo de embeddings; Voyage no es un proveedor integrado de Loom.
[Guía de embeddings de Anthropic](https://platform.claude.com/docs/en/build-with-claude/embeddings)

Las tareas internas de modelos de Loom usan claves de API de proveedores, no inicios de sesión de la CLI
de agentes de programación ni credenciales de suscripción. La elección de proveedor de su agente externo es independiente.

<a id="set-the-server-configuration"></a>

## Configurar el servidor

<a id="recommended-use-the-dashboard"></a>

### Recomendado: usar el dashboard

Como administrador de la plataforma, abre **Settings → Models & keys** (también disponible durante Setup).
Elige tu proveedor de LLM y de embeddings, introduce sus IDs de modelo y claves de API, y luego haz clic en
**Test & apply**. Deja en blanco las sobrescrituras por tarea para usar un mismo LLM en todas las tareas habilitadas.
La configuración guardada en el dashboard sobrescribe el entorno y se cifra en ClickHouse.
**No hace falta reiniciar Docker ni ejecutar ningún comando de Compose.** Los cambios de claves y de LLM se aplican en caliente;
los cambios de embeddings inician la reconstrucción que se describe más abajo. **Use environment
configuration** elimina esas sobrescrituras y restaura la configuración de arranque de este servidor;
no vuelve a cargar un archivo `.env` editado. Cambiar el espacio de embeddings requiere una reconstrucción.

Las opciones de embeddings del dashboard son proveedores alojados. Para mantener el embedder `local` de la pila publicada
y añadir un LLM real, define solo las líneas del LLM en `~/.loom/.env`
([Claude para el LLM](#claude-for-the-llm) las muestra) y vuelve a crear el contenedor: el
espacio de embeddings no cambia, así que no se vuelve a generar ningún embedding.

Para los cambios de embeddings desde el dashboard, Loom encola esa reconstrucción automáticamente y mantiene
en pausa las solicitudes del plano de datos hasta que terminen la verificación, la publicación y la limpieza.
Models & keys muestra la fase y el progreso; un trabajo fallido permanece en pausa y ofrece
reintentar desde su cursor guardado. Conserva el volumen de ClickHouse y el `LOOM_JWT_SECRET` original,
que sella las claves de proveedor almacenadas, al volver a crear los contenedores. Las reconstrucciones hacen llamadas al proveedor y pueden
tardar y generar costos proporcionales al contenido indexado. La reconstrucción se publica
solo en tablas MergeTree locales. La primera elección de embeddings en una instalación sin
vectores almacenados, como una instalación nueva que aún no ha guardado nada, cambia
directamente sin reconstrucción. Una vez que existen vectores en almacenamiento replicado o compartido,
como ClickHouse Cloud, Models & keys bloquea
los campos de embeddings y el servidor rechaza un cambio de embeddings antes de pausar
nada: cambia el modelo de embeddings en el entorno y vuelve a generar los embeddings de cada
índice como se describió antes. La recuperación de procesos remotos perdidos sigue siendo una limitación documentada en
[reconfiguración de proveedores](../architecture/provider-reconfiguration.md).

La configuración de tokens es independiente: configura el acceso de los agentes a Loom, no las credenciales del proveedor de modelos.

<a id="alternative-configure-the-environment"></a>

### Alternativa: configurar el entorno

Para despliegues automatizados, edita `~/.loom/.env` (o el directorio seleccionado con
`LOOM_INSTALL_DIR`). Para una copia del código fuente, edita el `.env` del repositorio.
Reemplaza las entradas existentes en lugar de añadir claves duplicadas. Esta configuración es la
base de arranque; las sobrescrituras guardadas en el dashboard tienen prioridad. Los cambios en el entorno requieren
volver a crear el contenedor o reiniciar el servidor nativo, como se describe más abajo.

<a id="openai-for-both-roles"></a>

### OpenAI para ambos roles

```dotenv
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=<your OpenAI API key>
LOOM_EMBEDDING_PROVIDER=openai
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_REQUIRE_REAL_PROVIDERS=true
```

Los ejemplos usan los modelos predeterminados que incluye Loom. Elige un modelo habilitado para tu cuenta
de API. Definir solo una clave no cambia la configuración estándar del instalador, que
nombra sus proveedores de forma explícita (el embedder local y el LLM falso).

<a id="claude-for-the-llm"></a>

### Claude para el LLM

```dotenv
LOOM_LLM_PROVIDER=anthropic
LOOM_LLM_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=<your Anthropic API key>
LOOM_REQUIRE_REAL_PROVIDERS=true
```

Luego elige una configuración de embeddings:

```dotenv
# OpenAI embeddings
LOOM_EMBEDDING_PROVIDER=openai
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
OPENAI_API_KEY=<your OpenAI API key>
```

```dotenv
# Or Gemini embeddings
LOOM_EMBEDDING_PROVIDER=gemini
LOOM_EMBEDDING_MODEL=gemini-embedding-001
LOOM_EMBEDDING_DIM=3072
GEMINI_API_KEY=<your Gemini API key>
```

Para otro servicio que implemente la API de embeddings de OpenAI, configura su raíz de API,
el ID del modelo y el ancho de salida nativo:

```dotenv
LOOM_EMBEDDING_PROVIDER=compatible
LOOM_EMBEDDING_BASE_URL=https://embedding-provider.example/v1
LOOM_EMBEDDING_MODEL=your-embedding-model
LOOM_EMBEDDING_DIM=768
LOOM_EMBEDDING_API_KEY=<your embedding provider API key>
# Enable only if the endpoint accepts the optional dimensions parameter:
LOOM_EMBEDDING_SEND_DIMENSIONS=false
```

La URL base es la raíz de la API; Loom añade `/embeddings`. Usa HTTPS para servicios
remotos. HTTP es compatible con endpoints privados autoalojados, pero transmite la
clave y el contenido sin cifrar. La compatibilidad requiere respuestas indexadas de arreglos de números de punto flotante
con el ancho configurado; un proveedor que use otra API necesita su propio adaptador.
Cambiar el endpoint, el modelo o el ancho requiere reconstruir los embeddings existentes.

Si tu organización solo tiene acceso a la API de Anthropic, obtén acceso a un
proveedor de embeddings antes de habilitar la búsqueda semántica.

<a id="keep-every-llm-job-on-your-chosen-provider"></a>

### Mantener todas las tareas del LLM en el proveedor elegido

Loom admite modelos distintos para tareas distintas, pero no necesitas cuentas con
varios proveedores de LLM. Añade esta configuración **después** de `LOOM_LLM_MODEL` para que las tareas
que normalmente dependen de un proveedor específico usen el modelo que elegiste:

```dotenv
LOOM_MEMORY_EXTRACT_VERIFY_MODELS=${LOOM_LLM_MODEL}
LOOM_SESSION_BRIEF_LLM_MODEL=
LOOM_CODE_BISECT_LLM_RERANK_MODEL=
LOOM_TRACKER_DUPCHECK_RERANK_MODEL=
LOOM_RETRIEVAL_JUDGE_MODEL=
```

Compose y el cargador de dotenv de Loom expanden el modelo del verificador a partir de la entrada anterior.
Los valores vacíos de arriba heredan el LLM principal. Esto mantiene habilitado el verificador de capturas;
una lista de verificadores vacía lo deshabilitaría. Elimina cualquier otra sobrescritura de modelo entre proveedores
que hayas añadido antes, incluidos los modelos de respaldo, de síntesis y de reordenamiento.

Esta configuración importa: algunos valores avanzados predeterminados incluidos nombran modelos de Claude u OpenAI
de forma explícita, y una sobrescritura sin clave de proveedor puede recurrir al cliente falso.
Usar un solo modelo para todas las tareas es una configuración de conveniencia, no la configuración detrás de
cada benchmark publicado. Ajusta tareas individuales más adelante con
[modelos por tarea](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#models-per-job).

<a id="a-model-server-on-your-machine-no-api-key"></a>

### Un servidor de modelos en tu máquina, sin clave de API

`LOOM_LLM_PROVIDER=local` ejecuta las tareas de LLM de Loom en un servidor de chat completions que tú ejecutas:
[el servidor Ollama](https://ollama.com), `llama-server` de llama.cpp, o cualquier servidor que use el
formato de chat completions de OpenAI. Loom no le envía ninguna clave. Con el embedder `local`
de la pila publicada, nada sale de la máquina.

Una instalación nueva ejecutada desde una terminal ofrece esto cuando Ollama responde en `localhost:11434`;
`LOOM_LOCAL_MODELS=ollama` antes de `sh loom-install.sh` lo establece sin preguntar, en una
instalación nueva o en una existente, y `LOOM_LOCAL_MODELS=off` nunca lo ofrece. Escribe:

```dotenv
LOOM_LLM_PROVIDER=local
LOOM_LLM_MODEL=qwen2.5:3b
LOOM_LOCAL_LLM_BASE_URL=http://host.docker.internal:11434/v1
LOOM_ASSISTANT_PROVIDER=local
LOOM_ASSISTANT_MODEL=qwen2.5:3b
LOOM_ASSISTANT_ANSWER_CEILING_USD=0
LOOM_MEMORY_EXTRACT_VERIFY_MODELS=
LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED=false
LOOM_LLM_REQUEST_TIMEOUT_S=900
LOOM_LLM_RETRY_ATTEMPTS=1
LOOM_EXTRACTION_QUEUE_MAX_BATCHES_PER_PASS=1
LOOM_EXTRACTION_QUEUE_MAX_PASS_SECONDS=3600
LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS=2
LOOM_ENTITY_EXTRACT_ENABLED=false
LOOM_SESSION_BRIEF_LLM_MODEL=
LOOM_CODE_BISECT_LLM_RERANK_MODEL=
LOOM_TRACKER_DUPCHECK_RERANK_MODEL=
LOOM_RETRIEVAL_JUDGE_MODEL=
```

El preajuste desactiva las dos tareas que eliminan hechos según el juicio de un modelo y cuyos
umbrales se definieron con modelos alojados: el verificador de capturas, que descarta un hecho que todos
los modelos verificadores consideran no duradero, y el
[resolvedor de conflictos](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#the-conflict-resolver)
en segundo plano, que retira una fila anterior que considera reemplazada. Un juicio erróneo de un modelo pequeño en estas tareas
haría perder hechos. Las escrituras siguen marcando conflictos para la cola de Review; define
`LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED=true` junto con la clave de
`LOOM_CONFLICT_RESOLVER_MODEL` (`gpt-6.1-sol`, OpenAI) para que los juzgue. En una instalación
existente, una configuración guardada en Models & keys tiene prioridad sobre `.env`: tras el reinicio,
elige allí **Use environment configuration** para que se aplique el preajuste.

Descarga primero el modelo (`ollama pull qwen2.5:3b`, unos 2 GB) y dale a Ollama una ventana de
contexto que contenga los prompts de Loom: la extracción envía unos 15,000 tokens, y Ollama recorta un
prompt más largo a su ventana sin dar error, así que ejecútalo con
`OLLAMA_CONTEXT_LENGTH=32768` (la ventana que admite `qwen2.5`). Una CPU puede tardar minutos en
leer un prompt tan largo, así que el preajuste espera `LOOM_LLM_REQUEST_TIMEOUT_S=900` segundos una
respuesta en lugar de los 300 predeterminados para modelos alojados, y lo intenta una sola vez
(`LOOM_LLM_RETRY_ATTEMPTS=1`): un servidor que agotó el tiempo no responde más rápido la segunda vez.

El worker que reproduce las capturas que envían los plugins de programación da un plazo a cada pasada, así que
el preajuste permite que una pasada de extracción diferida dure una hora en un lote
(`LOOM_EXTRACTION_QUEUE_MAX_PASS_SECONDS`, por defecto cuatro de sus intervalos, con un mínimo de 60 s
y un máximo de 900; la concesión de un lote se mantiene al menos un minuto más). Con un proveedor `local`,
una captura interrumpida (por ese plazo, o porque Loom se apaga) o cuya llamada agotó
el tiempo de espera consume uno de sus `LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS=2` intentos, así que una sesión
demasiado larga para la máquina termina en `failed` (`memory.extraction_status` muestra `cancelled` o
el tiempo de espera) en lugar de bloquear la cola: una sesión larga puede necesitar varias llamadas
seguidas, cada una de hasta el tiempo de espera. En un servidor que atiende una solicitud a la vez, el tiempo de espera
también cuenta la espera detrás de otras llamadas, y el asistente y `memory.ask` comparten el
servidor, así que su uso intenso durante una captura también puede hacerla fallar; aumenta
`LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS` si ocurre. El etiquetado de entidades
(`LOOM_ENTITY_EXTRACT_ENABLED`) está desactivado: se encolaría detrás de la extracción en un servidor que
atiende una solicitud a la vez, y su pasada, cortada en su plazo, no escribe nada, así que
la búsqueda prescinde de las coincidencias de entidades. Los demás workers que
llaman a un modelo no están ajustados para uno local: la extracción de observe (desactivada por defecto), cuya
pasada de hasta 10 sesiones se corta a los 900 s y vuelve a empezar desde la sesión más reciente,
y los títulos de conversación y los perfiles estructurados, que se ejecutan una vez configurado el registro de conversaciones o un
esquema de perfil.

Para llama.cpp, apunta `LOOM_LOCAL_LLM_BASE_URL` a su raíz `/v1`
(`http://host.docker.internal:8080/v1`) y define `LOOM_LLM_MODEL` con el nombre que sirve;
para un asistente local, define también `LIBRECHAT_LOCAL_LLM_BASE_URL` con esa raíz y
`LOOM_ASSISTANT_MODEL` con ese nombre. Un servidor ejecutado desde una copia del código fuente accede al modelo en
`localhost`, el valor predeterminado de la configuración (`http://localhost:11434/v1`).

Un modelo por tarea cuyo nombre empiece por `gpt`, `o1`, `o3`, `o4`, `chatgpt` o `claude` va a
ese proveedor alojado sea cual sea el valor de `LOOM_LLM_PROVIDER`; un modelo local con ese tipo de nombre
(`gpt-oss`) se ejecuta localmente como `LOOM_LLM_MODEL`, y en una configuración por tarea solo cuando esa
configuración nombra el mismo modelo.

- **Acceso al host.** Los contenedores acceden a la máquina como `host.docker.internal`. Docker
  Desktop reenvía eso a los servicios en loopback; con Docker Engine en Linux, Ollama debe
  escuchar más allá de loopback (`OLLAMA_HOST=0.0.0.0`, con el puerto 11434 cerrado a la red).
  Con el chart de Helm y `networkPolicy.enabled`, añade el puerto del servidor a
  `networkPolicy.extraEgress`.
- **El asistente.** `LOOM_ASSISTANT_PROVIDER=local` mueve el agente del asistente al
  endpoint `local` de LibreChat, que llama a `LIBRECHAT_LOCAL_LLM_BASE_URL` (por defecto
  `http://host.docker.internal:11434/v1`), y necesita `LOOM_ASSISTANT_MODEL`. El asistente
  responde llamando a las herramientas de Loom, así que elige un modelo que el servidor ejecute con llamadas a herramientas (Ollama
  las marca). Un modelo local no tiene precio, así que cada respuesta cuenta
  `LOOM_ASSISTANT_ANSWER_CEILING_USD` ($0.10 por defecto) contra la parte del asistente en
  el límite diario de costo de modelos del espacio de nombres, como cualquier modelo sin precio: con el límite
  y la parte predeterminados, cinco respuestas al día. El preajuste fija el techo en `0`, así que las respuestas locales
  no cuestan nada; también es lo que cuesta una respuesta cortada antes de recibir su uso, así que déjalo
  en su valor predeterminado cuando un modelo alojado atienda al asistente. Con el chart de Helm, añade
  `LOCAL_LLM_BASE_URL` al Secret del asistente; la política del chart permite que LibreChat llegue
  solo a DNS, su base de datos, Loom y TCP 443, así que el servidor de modelos necesita una NetworkPolicy
  propia que seleccione el pod de LibreChat.
- **Calidad y velocidad.** Los prompts y umbrales de Loom están ajustados con modelos alojados, y ningún
  benchmark publicado cubre uno local, así que trata un modelo pequeño como una forma de probar Loom
  sin clave o de mantener todo en la máquina, no como un equivalente: espera que la extracción,
  las respuestas de `memory.ask` y el uso de herramientas del asistente sean más débiles, en una medida que nadie
  ha medido. Un modelo pequeño en CPU es lento con prompts largos como los de la extracción, así que una
  cola de capturas ocupada se acumula; una GPU, o un modelo más grande en una máquina con la memoria necesaria,
  ayuda. Para un servidor de equipo, un modelo alojado responde mejor.

<a id="through-an-inference-gateway"></a>

### A través de un gateway de inferencia

Un gateway que habla la API de OpenAI o Anthropic delante de los proveedores
(para presupuestos, atribución de costos o una sola clave para varios servicios) recibe las
llamadas de Loom una vez que sus raíces de API apuntan allí; los nombres de modelo siguen siendo los que
sirve el gateway:

```bash
LOOM_OPENAI_BASE_URL=https://gateway.example/v1   # chat and embeddings
LOOM_ANTHROPIC_BASE_URL=https://gateway.example   # Messages: <root>/v1/messages
OPENAI_API_KEY=<the key the gateway issued for Loom>
```

Fuera de su valor predeterminado, la clave de Anthropic también se envía como `Authorization: Bearer`, el
esquema que aceptan los gateways. Con una clave de gateway, exige el gateway, para que una raíz incorrecta nunca
pueda enviar prompts ni la clave a ningún otro sitio. Un intercambio de tokens ya lo exige por sí mismo.

```bash
LOOM_INFERENCE_GATEWAY_REQUIRED=true
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example   # comma-separated; exact names, any case
```

Mientras el gateway es obligatorio, las llamadas a modelos y el contenido de memoria que Loom envía
a clasificadores y rerankers van solo a los hosts que lista `LOOM_INFERENCE_GATEWAY_HOSTS`.
Un host coincide exactamente: sin comodines, y `gateway.example` no
admite `api.gateway.example` ni `gateway.example.attacker.test`; una dirección IP
coincide solo si está en la lista.

- **Al iniciar**, Loom se niega a cargar si no hay `LOOM_INFERENCE_GATEWAY_HOSTS`,
  o mientras `LOOM_OPENAI_BASE_URL`, `LOOM_ANTHROPIC_BASE_URL`,
  `LOOM_EMBEDDING_BASE_URL`, `LOOM_INFERENCE_TOKEN_URL` (y `LOOM_LOCAL_LLM_BASE_URL`
  mientras `LOOM_LLM_PROVIDER=local`) o una entrada de
  `LOOM_RERANK_ENDPOINTS_JSON` nombre otro host, mientras el embedder sea
  el de Gemini, o mientras `LOOM_CODE_SEARCH_RERANK_DEFAULT` o
  `LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT` sea `builtin:cohere`.
  Ambas raíces de API cuentan, incluida la de un proveedor que no uses (sus
  valores predeterminados son las APIs propias de los proveedores): un modelo indicado por solicitud puede ser
  de cualquiera de los dos proveedores.
- **Al momento de la llamada**, una llamada a un modelo en cualquier otro host se rechaza antes de
  salir, también en cada salto de redirección, sea cual sea lo que lo nombró: un modelo indicado por
  solicitud, una entrada de `LOOM_CODE_SEARCH_VECTOR_MODELS`. Una llamada rechazada no se
  reintenta, y `/v1/health` informa que el proveedor está fallando.
- **Espacios de nombres**: `namespace.register` rechaza un `classify_endpoint` que no sea
  https en el puerto predeterminado de un host de la lista (422), y uno almacenado antes
  nunca se llama: memory.set conserva el tipo del llamante, con una advertencia en el registro.
  `builtin:cohere` se rechaza como opción de rerank (422), y un espacio de nombres que
  lo tenía almacenado hace rerank con nada, con una advertencia en el registro.
- **Dashboard**: guardar una configuración de modelo con un endpoint en otro host
  (o el embedder de Gemini) responde 422, y la comprobación del modelo responde
  `gateway_required`. Una configuración almacenada que la comprobación rechaza no se aplica, incluidos
  sus ajustes de vectores en uso, y `/v1/health` informa
  `model_configuration_refused`. La comprobación lee solo el embedding, por lo que una configuración
  rechazada siempre genera embeddings distintos a los de los ajustes vigentes, y las
  tablas de vectores pueden contener ese otro espacio: toda solicitud del plano de datos en ese
  servidor se rechaza (503 `GATEWAY_REQUIRED`, que ninguno de los dos SDK reintenta, sin
  `Retry-After`) y el servidor no está listo, hasta que una configuración que la comprobación
  acepte la reemplace o se restablezca a la del entorno. Un reinicio progresivo se
  detiene entonces en ese servidor: reemplace la configuración antes de reiniciar.
- **Despliegue progresivo:** una compilación anterior ignora ambos ajustes, así que actívelos
  una vez que todas las réplicas ejecuten esta. Un despliegue que ya intercambia un token
  se niega a iniciar con esta compilación hasta que `LOOM_INFERENCE_GATEWAY_HOSTS` liste su
  gateway, y mientras genere embeddings con Gemini, nombre un endpoint de rerank en otro
  host o use `builtin:cohere` como valor predeterminado de la búsqueda de código; las solicitudes que nombran
  `builtin:cohere` reciben un 422, y los espacios de nombres que lo almacenaron o que tienen un
  `classify_endpoint` fuera del gateway dejan de reordenar o clasificar hasta que se cambie. Defina
  primero los hosts y mueva el embedder detrás del gateway (`LOOM_EMBEDDING_PROVIDER=compatible`,
  `LOOM_EMBEDDING_BASE_URL` apuntando al gateway), o el despliegue se detendrá en la
  primera réplica actualizada.

Un gateway que acepta identidad de carga de trabajo (intercambio de tokens RFC 8693)
no necesita ninguna clave: Loom intercambia allí el token OIDC de su carga de trabajo,
renueva la respuesta cinco minutos antes de que caduque (a la mitad, si dura
menos de diez minutos) y la envía con cada llamada. Sin una clave de la que
inferirlos, indique el proveedor y los modelos:

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token         # https
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example                         # every root's host
LOOM_INFERENCE_WORKLOAD_TOKEN_FILE=/var/run/secrets/tokens/gateway   # rotated by the platform
LOOM_INFERENCE_TOKEN_SCOPE="chat embeddings"                         # if the gateway asks for one
LOOM_OPENAI_BASE_URL=https://gateway.example/v1                      # both roots required:
LOOM_ANTHROPIC_BASE_URL=https://gateway.example                      # the token is the gateway's
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
LOOM_EMBEDDING_MODEL=text-embedding-3-large
```

Loom se niega a iniciar mientras la raíz de cualquiera de los proveedores, o el endpoint de token,
esté en un host que `LOOM_INFERENCE_GATEWAY_HOSTS` no liste: un modelo indicado por
solicitud puede ser de cualquiera de los dos proveedores, y el token del gateway iría allí. En
el dashboard, los cambios de modelo también se comprueban a través del gateway.

En Kubernetes, el archivo de token es un token de cuenta de servicio proyectado con la
audiencia del gateway; el gateway debe confiar en el emisor OIDC del clúster y comprobar
la cuenta de servicio de Loom (`system:serviceaccount:<namespace>:<name>`), no solo el
emisor, ya que cualquier pod del clúster puede obtener un token de él.

Un gateway que verifica por sí mismo el token de la carga de trabajo lo recibe como bearer, sin
intercambio: configure el modo bearer en lugar de un endpoint de token. Loom lee el archivo como máximo
una vez por minuto, y de nuevo de inmediato cuando el gateway responde 401, de modo que un token rotado se
detecta sin reiniciar:

```bash
LOOM_INFERENCE_WORKLOAD_TOKEN_MODE=bearer
LOOM_INFERENCE_WORKLOAD_TOKEN_FILE=/var/run/secrets/tokens/gateway   # sent as it is
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example                         # required, as with the exchange
LOOM_OPENAI_BASE_URL=https://gateway.example/v1
LOOM_ANTHROPIC_BASE_URL=https://gateway.example
LOOM_EMBEDDING_PROVIDER=compatible                                   # embeddings through the gateway too
LOOM_EMBEDDING_BASE_URL=https://gateway.example/v1
```

Indique el proveedor y los modelos como en el ejemplo del intercambio. El token va solo a los
hosts del gateway: el modo bearer exige el gateway, igual que el intercambio. Un endpoint de
embeddings compatible que tenga su propia `LOOM_EMBEDDING_API_KEY` envía esa clave en su lugar.

<a id="naming-the-customer-on-every-call"></a>

#### Indicar el cliente en cada llamada

Un gateway que mide o presupuesta por cliente lee el cliente de un encabezado.
Indique los encabezados, y cada solicitud de chat, embedding, rerank y classify lleva
la org cuyos datos contiene y la funcionalidad que la originó:

```bash
LOOM_INFERENCE_CUSTOMER_HEADER=X-Customer-Id   # the org
LOOM_INFERENCE_FEATURE_HEADER=X-Feature        # optional, with the customer header: hyde, memory.search, worker.embedding
LOOM_INFERENCE_SYSTEM_CUSTOMER=loom            # calls that belong to no org
LOOM_INFERENCE_CUSTOMER_REQUIRED=true          # a cell serving several customers: refuse to start without the header
LOOM_INFERENCE_CUSTOMER_FORMAT='cloud:{org}'   # the gateway's customer id for an org; default the org id
LOOM_INFERENCE_FEATURE_MAP_JSON='{"hyde": "search", "*": "memory"}'  # Loom's feature names to the gateway's
```

- Una solicitud indica la org a la que se dirige, según lo autorizado para su token; un
  llamador no puede indicar una org a la que su token no llega.
- Un trabajo en segundo plano indica la org de las filas sobre las que trabaja, y hace una llamada
  por org: el worker de embeddings, `loom reembed` y una reconstrucción de modelo dividen sus
  lotes por org.
- Las llamadas propias del despliegue (la comprobación inicial del embedder, los pares fijos
  del worker de recalibración, la comprobación de proveedor del dashboard) indican
  `LOOM_INFERENCE_SYSTEM_CUSTOMER`.
- El juez de recuperación por muestreo (`LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE`) envía la consulta
  y las memorias de la org juzgada, por lo que cada búsqueda juzgada añade una llamada que
  indica esa org.
- Con el encabezado de cliente definido, una llamada a un modelo que no indica ninguna org se rechaza
  (`MODEL_CALL_UNATTRIBUTED`) en lugar de enviarse: es una ruta de llamada que
  olvidó su org, y el gateway no se la facturaría a nadie.
- El id de una org se envía codificado en porcentaje donde sea necesario.

Las respuestas en caché no hacen ninguna llamada: un embedding o una respuesta de modelo servidos desde la
caché de Loom no se miden de nuevo. El cliente se sigue comprobando antes de la caché, por lo que una
ruta que no indica ninguno se rechaza incluso en un acierto de caché.

<a id="naming-the-agent-on-every-call"></a>

#### Indicar el agente en cada llamada

Un gateway que factura o limita por agente lee de encabezados para quién es una llamada. Asocie cada
encabezado a una plantilla, y cada solicitud de chat, embedding, rerank y classify al gateway
lo lleva:

```bash
LOOM_INFERENCE_IDENTITY_HEADERS_JSON='{"X-Tenant": "{tenant}", "X-Agent": "{agent}", "X-Actor": "{email}"}'
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example   # required: identity headers go only to these hosts
LOOM_INFERENCE_TENANT_CLAIM=tenant_id   # the workload trust claim {tenant} names (the default)
```

- Los marcadores son `{org}`, `{namespace}`, `{subject}`, `{user_id}`, `{agent}` (el agente
  del token de una confianza de carga de trabajo, `sa:<trust>:<agent>`), `{tenant}` (el valor que esa confianza exige
  de su claim `LOOM_INFERENCE_TENANT_CLAIM`) y `{email}` (la dirección de una persona con sesión iniciada).
- Un encabezado cuya plantilla se renderiza vacía no se envía: con marcadores simples, una llamada sin
  agente detrás (la de una persona, o las propias del despliegue) no lleva encabezado de agente. Los encabezados
  que nombran `{agent}` o `{tenant}` van juntos o no van, de modo que un agente nunca va sin su
  tenant.
- Una llamada que no indica ninguna org se rechaza (`MODEL_CALL_UNATTRIBUTED`) salvo que sea propia del
  despliegue: ejecute los comandos `loom` que llaman a modelos con `--org`, y con `--namespace` mientras los
  encabezados nombren al agente (una llamada sin espacio de nombres se rechaza en lugar de facturarse al
  agente predeterminado del gateway).
- Los trabajos en segundo plano indican la identidad de las filas sobre las que trabajan: el autor de una memoria o de una
  extracción diferida; si no, el agente en cuyo propio espacio de nombres está la fila. Un trabajo que procesa filas por lotes
  hace una llamada por identidad.
- Ningún otro host recibe los encabezados de identidad: un reranker, un clasificador o un proveedor fuera de
  `LOOM_INFERENCE_GATEWAY_HOSTS` nunca sabe para quién es una llamada.
- `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON` lista las respuestas que su gateway da para una
  identidad, cada una por su estado y el `code`, `type` o `message` exactos del cuerpo de error
  (bajo `error`). Compare por `code` cuando el gateway proporcione uno. El valor predeterminado coincide con los códigos
  por agente de un proxy de inferencia y con el rechazo por presupuesto de la medición que hay detrás:

  ```json
  {
    "refused": [
      { "status": 429, "code": "budget_exceeded" },
      { "status": 429, "type": "rate_limit_error", "message": "budget exceeded" },
      { "status": 403, "code": "agent_denied" },
      { "status": 400, "code": "identity_incomplete" },
      { "status": 400, "code": "identity_malformed" }
    ],
    "wait": [{ "status": 429, "code": "agent_request_limit" }]
  }
  ```

  La segunda entrada se compara por tipo y mensaje porque la respuesta con forma de Anthropic de la medición
  en `/v1/messages` no incluye código.

- Nunca liste una respuesta que el gateway también da por un fallo propio: `token_unavailable` (no
  puede obtener una credencial, lo que también responde una caída del servicio de credenciales) o
  `identity_missing` (la solicitud no indicó ningún agente y el gateway no tiene uno predeterminado, un problema
  de configuración). Si se lista, `/v1/health` dejaría de ver la caída o la
  mala configuración. Las respuestas de nivel de despliegue también siguen siendo fallos del proveedor: `caller_*`,
  `scope_not_granted` (una ruta que los scopes del token no cubren), `route_not_found`,
  `upstream_*`, `misconfigured`, `no_identity`.
- Una respuesta bajo `refused` hace que esa solicitud responda 403 `MODEL_CALL_REFUSED`, y una bajo
  `wait` 503 `MODEL_CALL_DEFERRED` con el `Retry-After` del gateway; una espera de dos segundos o
  menos se espera primero una vez, y la llamada se repite. Nada reintenta ninguna de las dos respuestas,
  `/v1/health` no las interpreta como un fallo del proveedor, y los trabajos en segundo plano retienen las llamadas de esa
  identidad al mismo modelo durante `LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS` (10
  minutos por defecto) tras un rechazo y durante el `Retry-After` tras una espera, mientras el resto del trabajo
  continúa. Cualquier respuesta que la tabla no liste es del proveedor, como siempre.
- Cómo se encuentra cada valor: [encabezados de identidad](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md#identity-headers).

<a id="switching-an-orgs-model-calls-off"></a>

#### Desactivar las llamadas a modelos de una org

Un administrador de la org puede detener todas las llamadas a modelos para los datos de la org, con o sin
gateway, desde la página Guardrails del dashboard o por la API:

```bash
curl -s -X POST "$LOOM_BASE_URL/v1/org.set_model_calls" -H "Authorization: Bearer $ORG_ADMIN_TOKEN" \
  -d '{"org":"acme","enabled":false,"reason":"customer turned AI off"}'
```

- Mientras está desactivado, no se envía ninguna solicitud de chat, embedding, rerank o classify para la
  org, y tampoco se le sirve ningún vector ni respuesta en caché: el interruptor es un
  interruptor de emergencia. Una solicitud que necesite alguno se rechaza (403 `MODEL_CALLS_DISABLED`):
  entre ellas, `memory.set` de una memoria que se embebe al escribir, o en un espacio de nombres con un
  endpoint de classify, `memory.search`, y `tracker.similar`,
  `tracker.duplicate_check` y `code.bisect_candidates` cuando generan embeddings, juzgan
  o reordenan. Una lectura por clave (`memory.get`) y un borrado no hacen ninguna
  llamada a modelos y responden.
  El embedder local se ejecuta en el mismo proceso y no envía nada, pero también se
  desactiva. Una llamada que indica la org se comprueba incluso cuando Loom la hace por sí mismo;
  solo siguen sus propias llamadas que no indican ninguna org (la comprobación inicial, la calibración).
- `"enabled": true` lo vuelve a activar, y el trabajo en espera se ejecuta.
- **El bloqueo de un administrador de la plataforma.** Un administrador de la plataforma puede añadir `"locked": true`: los
  propios administradores de la org aún pueden desactivarlo, pero volver a activarlo o
  desbloquearlo se rechaza (403, indicando quién lo bloqueó) hasta que un administrador de la plataforma
  envíe `"locked": false`. Un cambio de un administrador de la plataforma que no envía `locked`
  mantiene el bloqueo. [`org.model_calls`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/org.md#orgmodel_calls)
  lee el ajuste, el bloqueo y quién lo estableció.
- **Dar de baja una org** es manual: un administrador de la plataforma envía
  `"enabled": false, "locked": true`. Los vectores de consulta en caché de la org caducan
  a los 30 días. Ninguna ruta de Loom desactiva una org por sí sola.
- Cada servidor aplica un cambio dentro de `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`
  (10 por defecto; el proceso que lo recibió, en su siguiente llamada). Un servidor que
  no puede leer el interruptor durante más de
  `LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS` (60 por defecto, de 15 a 300) no hace ninguna
  llamada a modelos para ninguna org hasta que pueda.
- Desactive una org solo cuando todas las réplicas ejecuten una compilación que conozca el
  interruptor: una más antigua sigue llamando a modelos para la org.

Qué queda en espera, y por cuánto tiempo, mientras una org está desactivada:

| Trabajo                                                                                                   | Mientras la org está desactivada                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Embeddings de memorias (el worker de embeddings)                                                          | No se toman: las memorias permanecen, sin vector, y se les genera el embedding cuando la org vuelve a estar activa. Nunca se descartan.                                                                                                                                                                                                                                                                                                                          |
| Solicitudes de extracción (la cola de extracción)                                                         | Permanecen pendientes, sin reclamar. Cuando una solicitud ha esperado `LOOM_MODEL_CALLS_OFF_MAX_WAIT_DAYS` (7 por defecto, de 1 a 29) desde que se puso en cola y desde que se desactivó la org, se marca como fallida con un registro `memory_drops`, motivo `model_calls_off`. El registro se escribe primero de forma síncrona: una solicitud cuyo registro no se puede escribir permanece pendiente. El límite se mantiene por debajo de 30 días porque el TTL de 30 días de la cola eliminaría antes la solicitud, sin registro. |
| Etiquetado de entidades, consolidación de perfiles, resúmenes por periodo, títulos de conversaciones, reflexiones, destilación de observaciones | No se seleccionan; se realizan cuando la org vuelve a estar activa. Se derivan de filas que permanecen, por lo que no se descarta nada.                                                                                                                                                                                                                                                                                                                         |
| Reparaciones de proyecciones                                                                              | No se examinan; se reintentan cuando la org vuelve a estar activa.                                                                                                                                                                                                                                                                                                                                                                                               |
| Juicios de conflicto pendientes                                                                           | No se vuelven a juzgar. Uno que supere la hora del barrido cuando la organización vuelve queda fuera del barrido, igual que un juicio perdido en una caída: la escritura se mantiene tal como se hizo.                                                                                                                                                                                                                                                                  |
| Ingesta de código, resúmenes de código, recuperación de embeddings de código                              | Se omiten los repositorios de la organización, de modo que todo su índice de código, búsqueda léxica incluida, queda como estaba. Un stub que el escaneo de recuperación dejó atrás lo encuentra su escaneo completo periódico.                                                                                                                                                                                                                                           |
| Juicios de enlaces de código                                                                              | Se dejan pendientes.                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Embeddings del tracker, puntuación de enlaces del tracker                                                 | El espacio de nombres se omite antes de que su cursor avance.                                                                                                                                                                                                                                                                                                                                                                                                            |
| Muestras del retrieval-judge                                                                              | Se descartan: una muestra no es trabajo pendiente.                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `loom reembed`                                                                                            | Las filas de la organización se dejan fuera, se cuentan (`skipped_switched_off`) y se informan; ejecútelo de nuevo cuando la organización vuelva a estar activada. `--delete-old` conserva los vectores antiguos de toda organización cuyas filas dejó fuera.                                                                                                                                                                                                              |
| La reconstrucción por un cambio de modelo                                                                 | Se completa; las filas de la organización quedan preparadas sin vector y se cuentan en el progreso de la reconstrucción (`rows_waiting`). Cuando la organización vuelve a estar activada, sus workers de recuperación vuelven a generar los embeddings de las memorias, los fragmentos de código y los resúmenes de código; una fila de cualquier otra tabla no tiene vector hasta que se escribe de nuevo o hasta que una reconstrucción posterior la cubre.            |

<a id="without-a-workload-token"></a>

#### Sin un token de carga de trabajo

Cuando la plataforma no entrega ningún token a Loom (una VM, un host de contenedores), Loom firma el suyo: defina una audiencia en lugar del archivo de token, y el gateway confía en Loom como emisor.

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example
LOOM_INFERENCE_TOKEN_AUDIENCE=loom-inference   # what the gateway's entry for Loom names
LOOM_PUBLIC_BASE_URL=https://loom.example      # the issuer is <this>/v1
LOOM_OPENAI_BASE_URL=https://gateway.example/v1
LOOM_ANTHROPIC_BASE_URL=https://gateway.example
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
LOOM_EMBEDDING_MODEL=text-embedding-3-large
```

Cada intercambio envía un token nuevo que dura cinco minutos: emisor `<LOOM_PUBLIC_BASE_URL>/v1`, sujeto `loom`, la audiencia indicada arriba, firmado con RS256 usando la clave RS256 más reciente del llavero de firma. Cree una primero con `loom
signing-key rotate --algorithm RS256`, a menos que `LOOM_JWT_SECRET` sea una clave RSA, que ya firma como RS256. A partir de entonces, esa clave firma también los tokens propios de Loom, que verifican todos los Loom del despliegue; nunca se usa un secreto HS256, ya que el gateway no podría verificarlo. El gasto se atribuye a ese único sujeto.

La entrada del gateway para Loom indica ese emisor, la audiencia, el sujeto `loom`, RS256 y las claves en `<LOOM_PUBLIC_BASE_URL>/v1/.well-known/jwks.json` (el descubrimiento OIDC está en `/v1/.well-known/openid-configuration`). El gateway debe poder obtener esa URL por https; si no puede alcanzar Loom, publique una copia del conjunto de claves en un lugar al que sí pueda acceder. Cada Loom publica una clave en la siguiente actualización de su llavero después de que `rotate` la escribe, y la clave firma solo `--activate-in` más tarde (300 s por defecto): ejecute rotate con un `--activate-in` mayor que el tiempo que el gateway conserva su copia de las claves, o que lo que tarda en copiarlas. Una clave retirada sigue siendo de confianza en el gateway hasta que se actualiza su copia. `loom signing-key retire` se niega a retirar la última clave RS256 activa cuando `LOOM_INFERENCE_TOKEN_AUDIENCE` está definida en su propio entorno, así que ejecútelo con el de los servidores. Loom (el servidor y todos los comandos `loom`) rechaza esta configuración sin un `LOOM_PUBLIC_BASE_URL` https, con `LOOM_JWT_AUDIENCE` como audiencia, o con un emisor en el que confíe su propio verificador, y su propio verificador nunca acepta un token con la audiencia del gateway.

<a id="apply-changes-and-check-the-result"></a>

## Aplicar los cambios y comprobar el resultado

**Cambios desde el dashboard:** espera a que Models & keys informe de la configuración activa. Si hay una reconstrucción en curso, sigue su progreso ahí; reintenta un trabajo fallido desde la misma página. No recrees los contenedores para aplicar un cambio hecho desde el dashboard.

**Solo cambios de entorno:** para el stack publicado, recrea el contenedor del servidor:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
curl -fsS http://localhost:7777/v1/health
```

Usa tu directorio de instalación y tu puerto si son distintos. Recrea el contenedor: un simple `docker compose restart` no vuelve a cargar su entorno desde `.env`. Los servidores nativos deben reiniciarse con el entorno actualizado. Para el stack del checkout, ejecuta
`docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build --force-recreate loom`.

Health informa `fake_llm`, `fake_embedding`, `providers_degraded` y `problems` accionables. Con ambos proveedores reales configurados, los indicadores fake deben ser false. Esto comprueba la configuración y el estado de salud; además, guarda y recupera un dato de prueba y prueba una respuesta para confirmar que tu cuenta puede llamar a los modelos elegidos.

Cambiar el LLM o rotar una clave de API no requiere reconstruir los embeddings. Cambiar el modelo de embeddings sí, incluso cuando sus dimensiones se mantienen. Para los cambios de entorno (en lugar del flujo del dashboard), los índices existentes de memoria, documentos, tracker y código necesitan sus procedimientos correspondientes de re-embedding o re-ingesta; `loom reembed` por sí solo no es una reconstrucción universal de todos los índices. Si cambian las dimensiones, revisa las restricciones de vectores del esquema existente antes de aplicar la nueva configuración. Conserva tus datos y sigue el documento de [mantenimiento](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#re-embed-and-re-extract) y las [operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md).
