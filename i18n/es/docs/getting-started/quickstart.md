<!-- i18n: source=docs/getting-started/quickstart.md blob=2dcc795de559021866a2affe24bef8e4e8f41f81 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# Inicio rápido

Esta página arranca Loom en un portátil y guarda una primera memoria mediante HTTP, Python y MCP. No
requiere clave de API ni cuenta. Una instalación nueva del stack publicado genera embeddings con un pequeño
modelo local que incluye la imagen, de modo que la búsqueda devuelve resultados útiles sin red (una
instalación actualizada conserva el embedder que indica su `.env`); su LLM es simulado, así que la extracción y
las respuestas de `memory.ask` no producen nada. El `.env` de un checkout usa proveedores simulados para ambos.

**La instalación estándar con Docker es de un solo usuario:** sin inicio de sesión, con acceso solo desde localhost y
un espacio de nombres `dev/local` predeterminado. Una persona puede conectar varios agentes sin activar el
modo multiusuario. **Los despliegues de equipo también están soportados en OSS**, con cuentas autenticadas
y controles de acceso por espacio de nombres; [empieza en modo multiusuario](#start-in-multi-user-mode)
o consulta [Deployment → Identity modes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes).
Ambos modos admiten las mismas funciones de Loom; los LLM y embeddings reales requieren configurar proveedores
en cualquiera de los dos modos.

<a id="prerequisites"></a>

## Requisitos previos

| Necesidad                          | Para                                                                                     |
| ---------------------------------- | ---------------------------------------------------------------------------------------- |
| Docker con el plugin compose       | El stack: ClickHouse y el servidor de Loom.                                              |
| Python 3.12 o superior             | CLI `loom`, solo para contribuidores. El SDK es compatible con 3.11+.                    |
| Node.js 20 o superior              | El SDK de TypeScript (opcional).                                                         |
| Puerto libre `7777`                | Loom. El desarrollo desde el checkout también expone ClickHouse en `8123` y `9000`.      |
| `jq`                               | Leer la salida de `curl` en el paso 4 (opcional).                                        |

<a id="1-boot-the-stack"></a>

## 1. Iniciar el stack

<a id="fast-path-released-image-no-clone"></a>

### Ruta rápida: imagen publicada, sin clonar

Requisito previo: Docker Desktop o Docker Engine con Compose 2.20 o posterior.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

El comando guarda el instalador en el directorio actual y luego ejecuta ese archivo, así que ejecútalo desde tu directorio personal, u otro directorio en el que solo tú puedas escribir: en uno compartido, otra persona puede reemplazar `loom-install.sh`, o apuntarlo a otro archivo, antes de que se ejecute. Una descarga fallida se detiene con el error de curl y un código de salida distinto de cero; después de una instalación fallida, corrige lo que informa y vuelve a ejecutar el comando completo. El instalador descarga `loom-stack.tar.gz` de la última GitHub Release, verifica su SHA-256, instala la configuración de Compose y ClickHouse en `~/.loom`, descarga las imágenes correspondientes `ghcr.io/clickhouse/loom:X.Y.Z` y `ghcr.io/clickhouse/loom-extractor:X.Y.Z` (el servicio extractor de documentos) y espera a `http://localhost:7777/v1/health`. La imagen del servidor contiene la API, los workers, las migraciones y el dashboard de React ya compilado en la raíz del servidor; no hace falta instalar Python ni Node. Los indexadores de Python, TypeScript y Go están incluidos y habilitados. Un repositorio indexado con el indexador de TypeScript recibe sus dependencias instaladas automáticamente. Ambas imágenes son compatibles con Linux `amd64` y `arm64`, incluido Docker Desktop en Apple Silicon.

El stack ejecuta su propio contenedor de ClickHouse. Para usar un ClickHouse que ya tengas, un servidor autogestionado o un servicio de ClickHouse Cloud (26.4 o posterior, con un Keeper), pasa su URL; el instalador pide la contraseña cuando la URL no la incluye, crea la base de datos `loom` y un usuario lector de solo lectura, o imprime el SQL que debe ejecutar un administrador ([elegir el ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

El `install.sh` de cada release instala esa release, así que la copia que sirve `latest` instala la última release, y la URL propia de una release, más abajo, instala siempre esa release. Sobre una instalación más nueva, la copia de una release anterior se niega a instalar, en lugar de iniciar un Loom y un ClickHouse más antiguos sobre datos más nuevos; para volver a una versión anterior a propósito, sigue [downgrading an installer install](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install). Para fijar una release, usa su propia URL:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Para inspeccionar todo antes de iniciar, descarga los archivos y compruébalos:

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

Luego, cuando lo que imprimen te parezca correcto, instala desde el paquete verificado:

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

`LOOM_PORT`, `LOOM_BIND_HOST`, `LOOM_IMAGE_REPOSITORY`, `LOOM_EXTRACTOR_IMAGE_REPOSITORY` y `LOOM_CLICKHOUSE_IMAGE` que se pasen al instalador se escriben en el `.env` del directorio de instalación, de modo que los comandos `docker compose` posteriores en ese directorio y las ejecuciones posteriores del instalador usen el mismo puerto y las mismas imágenes. Un `LOOM_BIND_HOST` distinto de loopback publica Loom fuera de este host, y el servidor se niega a iniciarse así sin `LOOM_PUBLIC_BASE_URL` y sin inicio de sesión: instala con `LOOM_SKIP_START=1`, defínelos en `.env` (más abajo) y luego vuelve a ejecutar el comando de instalación sin `LOOM_SKIP_START=1`.

Cada release ejecuta la versión de ClickHouse que era la más reciente cuando se compiló, así que volver a ejecutar el comando de instalación para la misma versión de Loom conserva su ClickHouse; instalar una versión más nueva de Loom lo actualiza, a menos que `.env` fije `LOOM_CLICKHOUSE_IMAGE` (elimina esa línea para volver a seguir las releases). Volver a ejecutar el comando de instalación actualiza los archivos de Compose y de configuración gestionados y reconcilia los contenedores. Conserva `~/.loom/.env`, añadiendo la URL del extractor y un token a uno que no los tenga, y los volúmenes de Docker con nombre. Para detener sin borrar datos:

```bash
docker compose --project-directory "$HOME/.loom" down
```

Añade `--volumes` solo para un reinicio intencionado e irreversible.

<a id="try-it-in-github-codespaces"></a>

### Pruébalo en GitHub Codespaces

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

El dev container del repositorio ejecuta la última release en un codespace, sin nada en tu máquina: instala Loom con el instalador anterior, lo inicia y conecta Claude Code a él. El primer inicio descarga unos 3 GB de imágenes y tarda unos minutos. Abre el dashboard en la dirección que imprimen las últimas líneas del registro de creación (`Loom is running in the
codespace. Dashboard: https://<codespace name>-7777.app.github.dev`, la dirección reenviada de la pestaña **Ports** para el puerto 7777); el `http://127.0.0.1:7777/` del instalador que aparece encima es la dirección propia del codespace. En el terminal del codespace, ejecuta `cd ~ && claude` (fuera del checkout, cuyas instrucciones son para trabajar en el propio Loom), inicia sesión y di "Remember that I deploy only on Tuesdays"; en una sesión nueva de `claude`, pregunta "When do I deploy?".

La prueba se ejecuta en modo de un solo usuario, sin inicio de sesión. En el navegador, el puerto 7777 se reenvía de forma privada: su dirección solo responde a tu cuenta de GitHub. Mantenlo privado: hacerlo público, o privado para una organización, abre un servidor sin inicio de sesión a todos con quienes se comparta. Los agentes dentro del codespace, como su `claude`, usan `http://localhost:7777`; las líneas de conexión del dashboard nombran en cambio la dirección reenviada, que requiere un inicio de sesión de GitHub que los agentes del codespace no tienen. Al abrirlo en VS Code de escritorio, el codespace también reenvía el puerto al `localhost:7777` de tu portátil, donde está tan abierto como una instalación local para todos los programas del portátil, y los agentes del portátil pueden alcanzarlo allí; el reenvío necesita que ese puerto local esté libre, así que detén primero un Loom local. Usa el dashboard desde la dirección del navegador: desde el `localhost:7777` del portátil la página se abre, pero el servidor rechaza sus peticiones, por lo que no muestra datos; solo la dirección reenviada es la página propia de este servidor.

La instalación vive en `/workspaces/.loom`, así que un codespace reconstruido conserva su `.env` y su proyecto de Compose, aunque sus memorias viven en volúmenes de Docker que una reconstrucción puede no conservar; una reconstrucción también ejecuta sobre ella el instalador de la última release, lo que la actualiza a esa release. Dondequiera que estos documentos nombren `~/.loom` o `"$HOME/.loom"`, un codespace usa `/workspaces/.loom`. Codespaces ofrece abrir el puerto 7777 en cuanto escucha; el dashboard responde cuando el registro de creación imprime "Loom is running in the codespace". Si en cambio el registro termina con un error, ejecuta `sh .devcontainer/start-loom.sh` en el terminal del codespace: conserva lo que configuró la primera ejecución.

<a id="start-in-multi-user-mode"></a>

### Iniciar en modo multiusuario

Para una instalación nueva con imagen publicada, con cuentas e inicio de sesión desde el primer arranque, prepara los archivos del stack sin iniciar contenedores:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Edita `~/.loom/.env` antes de iniciar Docker. Reemplaza las entradas existentes de identidad y autenticación, y añade el ajuste de registro:

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Conserva los valores generados de `COMPOSE_PROJECT_NAME` y `LOOM_JWT_SECRET`. Luego descarga e inicia el stack (con tu propio ClickHouse, ejecuta primero `ch-setup`, como indicó el instalador):

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

Cuando pasen las [comprobaciones de estado](#check-the-stack), abre `http://localhost:7777/` y regístrate. Los clientes SDK y MCP necesitan tokens; usa la organización y los espacios de nombres concedidos a tu cuenta, no los valores predeterminados de un solo usuario `dev/local` de los ejemplos siguientes.

Esto habilita la autenticación multiusuario, no el acceso público de red: el stack sigue enlazado a localhost. Antes de compartirlo con un equipo, configura el [acceso de operador](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) y [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls). Configura también los [modelos reales y las claves de API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md); elegir el modo multiusuario no sustituye al LLM falso.

Para un stack de un solo usuario que ya está en ejecución, sigue en cambio las [instrucciones para cambiar](#2-open-the-dashboard); la recreación de contenedores conserva los volúmenes de datos existentes.

<a id="contributor-path-build-a-checkout"></a>

### Ruta de colaborador: compilar un checkout

Requisito previo: Docker Compose 2.34 o posterior, que el archivo compose del checkout necesita para descargar ClickHouse a diario.

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` hace tres cosas:

1. Crea los dos archivos que git no rastrea: `.env` a partir de `.env.example`, y `deploy/clickhouse-config/named_collections.xml` a partir de su plantilla. Nunca sobrescribe ninguno; a un `.env` existente sin token del extractor le añade uno.
2. Ejecuta el stack de compose de desarrollo con `--build`.
3. Consulta `http://localhost:7777/v1/health` hasta que el servidor esté listo, imprimiendo el número de migraciones pendientes. Se rinde tras `--timeout` (300 s) con el último cuerpo de salud y las últimas líneas del registro.

El primer arranque de colaborador compila la imagen y aplica todas las migraciones de esquema, así que cuenta con unos minutos. Luego `loom up` imprime la URL del dashboard (`http://localhost:7777/`), una llamada `curl` y un fragmento de Python que guardan una primera memoria, y una línea que conecta Claude Code. Cuando la autenticación está activada, también genera un token de escritor y lo incluye en esos fragmentos.

| Flag         | Qué hace                                  |
| ------------ | ----------------------------------------- |
| `--rebuild`  | Recompila tras un cambio de código.       |
| `--no-build` | Reutiliza la imagen en caché.             |
| `--open`     | Abre el dashboard.                        |
| `--port`     | Usa otro puerto de Loom.                  |
| `--env-file` | Apunta a otro archivo env.                |

Volver a ejecutar `loom up` sobre un stack en ejecución solo vuelve a comprobar el estado. `make dev` hace lo mismo sin la CLI y ejecuta:

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

Con `compose.yaml` primero, Compose lee el `.env` de la raíz del repositorio para las sustituciones `${VAR:-default}`; `--env-file .env` solo lo indica de forma explícita. `loom down` detiene el stack y conserva sus volúmenes. `loom down --delete-data` elimina de forma permanente solo los volúmenes con nombre de esta instalación, y únicamente después de que escribas el nombre del proyecto de Compose generado; `--yes` es la confirmación no interactiva. Los targets del Makefile hacen lo mismo: `make down` conserva los datos y `make down-all` los elimina.

<a id="what-the-stock-env-boots"></a>

### Qué inicia el `.env` por defecto

Las plantillas `.env` del checkout y del paquete de release seleccionan ambas el modo de un solo usuario. El paquete de release define `LOOM_IDENTITY_PROVIDER=single-user` de forma explícita; el checkout lo deja vacío e infiere el mismo modo a partir de `LOOM_AUTH_REQUIRED=false`.

| Ajuste                                                                       | Valor         | Efecto                                                                                          |
| ---------------------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (o vacío)   | `single-user` | Sin inicio de sesión. Espacio de nombres predeterminado `dev/local`. Las llamadas SDK y MCP no necesitan token. |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | LLM falso     | La extracción, el etiquetado de entidades y la síntesis de `memory.ask` no producen nada.       |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (paquete de release)  | bge-small     | Búsqueda semántica en CPU, a partir de los archivos del modelo que incluye la imagen.           |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (checkout) | embedder falso | La búsqueda ordena por n-gramas de caracteres. Basta para comprobar la conexión, no la recuperación. |

Ambos instaladores generan un `COMPOSE_PROJECT_NAME` único, un secreto aleatorio de firma JWT, un token aleatorio del extractor y `LOOM_BIND_HOST=127.0.0.1`. Así, clones distintos no comparten volúmenes de ClickHouse sin que lo notes, y Loom y ClickHouse solo son accesibles desde este equipo.

Para usar proveedores reales, abre **Settings → Models & keys**, elige tus proveedores y modelos, introduce sus claves de API y haz clic en **Test & apply**. Una sola clave de API de OpenAI puede servir tanto para el LLM como para los embeddings. No hace falta reiniciar Docker; los cambios de embeddings activan una reconstrucción verificada, con el progreso y el reintento visibles en el dashboard.

Para un despliegue basado en variables de entorno, reemplaza las líneas de proveedor en `~/.loom/.env` (stack publicado) o `.env` (checkout). Definir solo `OPENAI_API_KEY` no cambia nada: las líneas de proveedor por defecto nombran sus proveedores de forma explícita. Dejar de usar el embedder local o falso vuelve a calcular los embeddings de lo almacenado.

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Para Claude, OpenAI, embeddings alojados, clientes solo de suscripción y despliegues con un único proveedor de LLM, sigue [Configure models and API keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md). Solo después de editar el `.env` del stack publicado, aplícalo con `docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom`; un reinicio simple no vuelve a cargar el entorno del contenedor. Los ajustes avanzados están en [Deployment → Providers](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers).

<a id="check-the-stack"></a>

### Comprobar el stack

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health` devuelve `status: ok` cuando ClickHouse es accesible y ningún problema distinto de `code_tier_failing` (el nivel de índice de código de un repositorio que falla) es un error. `fake_embedding`, `fake_llm` y `providers_degraded` indican en qué modo estás; `problems` lista cada incidencia con su solución; `schema.pending` lista las migraciones sin aplicar.
- `loom doctor` imprime una línea por cada comprobación del lado del servidor. `--json` da JSON. Desde un stack de checkout de código fuente, ejecuta `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`.

<a id="2-open-the-dashboard"></a>

## 2. Abre el dashboard

`http://localhost:7777/` abre directamente el dashboard en modo `single-user`;
en modo multiusuario, regístrate o inicia sesión primero. En modo `single-user`
el dashboard y los valores predeterminados del agente usan `dev/local` (`LOOM_DEFAULT_NAMESPACE` cambia el nombre del
espacio de nombres). Es un modo de conveniencia, no un límite de aislamiento entre usuarios: cualquiera que pueda
acceder al servidor puede acceder a sus datos y a la administración. Mantenlo accesible solo desde localhost.

Para probar el registro de usuarios (`trust-network`: correo electrónico y contraseña, y la primera persona que
reclame el nombre de un espacio de nombres se convierte en su administrador), reemplaza estas entradas en `~/.loom/.env`
(stack publicado) o en `.env` (checkout):

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Establecer solo `LOOM_AUTH_REQUIRED=true` no es suficiente cuando `single-user` se selecciona explícitamente:
ese modo mantiene la autenticación desactivada. Conserva el `LOOM_JWT_SECRET` generado por el instalador y los volúmenes existentes, y luego recrea el servidor para cargar el entorno:

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

A partir de entonces, el dashboard abre en su página de inicio de sesión, que tiene una pestaña **Sign up**, y cada llamada del SDK
y de MCP necesita un token. Los modos de identidad se describen en [Identities and
namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes). Indicar
`trust-network` es también lo que permite que un `LOOM_BIND_HOST` que no sea loopback arranque con el registro abierto,
de modo que cualquiera que llegue al puerto podrá registrarse. Antes de hacer que el servidor sea accesible para un
equipo, sigue la [guía de despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) para
el acceso de operadores y [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls).

<a id="3-get-a-token"></a>

## 3. Obtener un token

En modo de un solo usuario el token es opcional. Aun así, generar uno mantiene tus scripts funcionando
cuando se active la autenticación, siempre que indiquen su `org` (o la envíen en blanco), `namespace`
y `agent`: una llamada que envía una credencial, como hace el SDK cuando encuentra alguna ([Python
SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)), no recibe ninguno de ellos completado automáticamente,
y un `org` en blanco es el propio del token.

- **Dashboard:** **Settings → Tokens → New token**. De forma predeterminada, el token lee todos los
  espacios de nombres y repositorios a los que perteneces (un repositorio restringido solo cuando lo añades),
  pero ninguna colección, y guarda en el espacio de nombres en el que estás. _Only what you choose_ lo limita
  a los espacios de nombres, colecciones y repositorios que marques (ninguno al principio); los marcados
  se buscan en el orden indicado, y lo cambias con las flechas de una fila o, en un
  escritorio, arrastrando el asa de la fila. El resultado muestra el token una sola vez, con la configuración para
  tu cliente MCP o un script.
- **CLI, como operador:** ejecútalo dentro del contenedor del servidor, que guarda el secreto
  de firma:

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  Añade `--ttl-days 30` para cualquier token que salga de tu máquina. `loom mint-token --help`
  lista las opciones de alcance (`--namespace-role`, `--read-scope`, `--traffic-class`).

- **Un bot que se registra solo** (solo en redes de confianza):
  `loom init --agent nightly-triage --server-url http://localhost:7777` crea el
  espacio de nombres `agent-nightly-triage`, guarda un token de escritor (writer) en `~/.loom/token` y la URL
  en `~/.loom/config.json`, e imprime un código de reclamación. Una persona lo introduce una vez en
  `/oauth/agent-claim` con una contraseña propia para tomar posesión.

Guarda el token donde lo buscan el SDK, la CLI y los plugins:

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

Los claims, roles y permisos están en [Tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

<a id="4-first-memory-over-http"></a>

## 4. Primera memoria por HTTP

Cada operación es un `POST /v1/<op>` con un cuerpo JSON. Una instalación nueva con el `.env` por defecto, ya sea desde el instalador o desde un checkout, es de un solo usuario: sin token, y una llamada cotidiana de memoria o búsqueda (`memory.set`, `memory.get`, `memory.search` y alguna más) que no indica `org` ni `namespace` se ejecuta en los propios de la instalación, org `dev` y espacio de nombres `local`, los mismos que usan MCP y el dashboard; un `memory.set` que no indica `agent` se registra con el agente `http`, y una búsqueda lee todos los agentes. Por eso las llamadas siguientes solo necesitan sus propios campos ([qué ops](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)). Si una llamada sin token responde 401
`AUTH_INVALID`, la autenticación está activada: añade `-H "Authorization: Bearer $LOOM_API_KEY"` e indica el `org`, el `namespace` y el `agent` de tu cuenta; un encabezado con un token vacío se rechaza.

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.get -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window"}' | jq '{found, value}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"query":"when can we deploy?"}' | jq '.results[0].content_excerpt'
```

La respuesta de `memory.set` incluye `memory_id`, `content_sha256` y `status`.
`memory.get` vuelve a leer una clave: `found` indica si la clave contiene un valor vigente, y `value` es el valor tal como se escribió. La respuesta de búsqueda incluye `results` y `abstain`, que indica si el mejor resultado es lo bastante sólido como para responder a partir de él. La búsqueda lee las memorias de todos los agentes del espacio de nombres, incluidas las añadidas en el dashboard; indica un `agent` para leer solo las de ese agente.
`GET /v1/operations` lista todas las operaciones, y <http://localhost:7777/docs> permite probar cada una desde el navegador. Los formatos están en la [referencia de la API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md).

<a id="5-first-memory-from-python"></a>

## 5. Primera memoria desde Python

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

Una versión que todavía no está en PyPI se instala desde sus assets
([instalar desde un GitHub Release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)).

El ejemplo indica `org` y `namespace`, así que funciona con o sin el token del paso
3, que el SDK envía cuando encuentra uno. En un servidor de un solo usuario, una llamada sin token
puede omitirlos: `Agent(agent="quickstart", base_url=...)` también se ejecuta en `dev`/`local`.

```python
import asyncio

from loom import Agent


async def main():
    async with Agent(
        org="dev",
        namespace="local",
        agent="quickstart",  # names the writer in the audit log
        base_url="http://localhost:7777",
    ) as a:  # sends a credential it finds; none needed in single-user mode
        await a.memory.set("standup_time", "Daily standup is at 9:30.")
        row = await a.memory.get("standup_time")  # None when the key is unset
        print(row["value"] if row else "unset")
        for hit in await a.memory.search("when is standup?"):
            print(hit.content_excerpt)


asyncio.run(main())
```

Dónde encuentra el SDK su servidor y su credencial se explica en
[Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials).
El SDK de TypeScript se instala con `npm install @clickhouse/loom`
([desde un clon](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)).

Las superficies del cliente están en las referencias del [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) y
del [TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md).

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. Primera memoria desde un agente de código

Para Claude Code, instala el plugin de Loom. Añade hooks (contexto al inicio de la sesión,
captura al final) y skills sobre las herramientas MCP:

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

En un servidor de un solo usuario en esta máquina, mantén la URL del servidor por defecto y deja el token
vacío cuando Claude Code pida la configuración del plugin.

Sin el plugin, añade la entrada MCP directamente:

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

Ambas opciones llegan a un servidor de un solo usuario en esta máquina sin un token tuyo.

Con la autenticación activada, crea un token en **Settings → Tokens** y elige **Claude Code** en
**Connect**: el diálogo muestra la configuración para el plugin y para una entrada de `claude mcp add`,
con el token incluido. Después:

1. Sobrescribe `~/.loom/config.json` con el bloque `config.json` del diálogo más el
   espacio de nombres en el que guarda el token (`"namespace": "<namespace>"`): un `org` o `namespace` que
   una sesión de un solo usuario haya dejado en el archivo tiene prioridad sobre el token, y sin uno el plugin
   escribe en el primer espacio de nombres del token distinto de `*`, o en `coding`. Vuelve a añadir cualquier
   `code_repo` o `agent` que hubieras definido.
2. Deja vacíos `LOOM_API_KEY` (el paso 3 puede haber exportado uno: `unset LOOM_API_KEY`) y la
   opción `token` del plugin, o asígnales el nuevo token: el plugin lee ambos
   antes que ese archivo
   ([configuración](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)).
3. Elimina una entrada de `claude mcp add` creada sin token (`claude mcp remove loom -s user`)
   antes de ejecutar el comando del diálogo, que rechaza un nombre que ya existe.

`loom install --client <name>` escribe la entrada MCP para Claude Desktop, Claude Code,
Codex, Cursor, VS Code, Cline, Windsurf y Zed; sin ningún flag configura todos los clientes que
encuentra. [Conectar un agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) cubre Codex y Cursor.

Inicia una sesión nueva y di _"Recuerda que nunca despliego los viernes."_ El agente llama a
`loom_memory_set`. En una sesión nueva, pregunta _"¿Cuándo puedo desplegar?"_ El agente llama a
`loom_search` y responde a partir del dato almacenado. El modelo decide cuándo guardar y
buscar a partir de las descripciones de las herramientas; no hay palabras clave que lo activen.

<a id="7-see-it-in-the-dashboard"></a>

## 7. Ver en el dashboard

| Página            | Muestra                                                              |
| ----------------- | -------------------------------------------------------------------- |
| `/memories`       | Las memorias del espacio de nombres, con autor y hora.               |
| `/admin/activity` | El registro de auditoría: qué agente escribió qué clave y cuándo.    |
| `/admin/usage`    | Costo por operación y agente.                                        |

El resto está en [Dashboard](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md).

<a id="optional-index-a-repository-on-the-released-stack"></a>

## Opcional: indexar un repositorio en el stack publicado

En este stack, agregar un repositorio desde el dashboard se rechaza con `SANDBOX_UNAVAILABLE`:
requiere el [sandbox de trabajos de repositorio](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox),
que este stack deja desactivado porque el confinamiento predeterminado de Docker lo bloquea. Incluye solo
repositorios de confianza: sus pasos de compilación (instalaciones de TypeScript, descargas de módulos de Go,
CMake) se ejecutan sin el sandbox, en el contenedor `loom`, cuyo entorno contiene el secreto JWT
y las claves de los proveedores.

`LOOM_CODE_REPOS_JSON` es una sola línea que contiene todos los repositorios como un arreglo JSON
([Configurar la entrada](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)).
En `~/.loom/.env`, descomenta esa línea (agrégala si falta) y edítala.
`dev` es la organización con la que se ejecuta un stack de un solo usuario, y la organización en la que se registran
las cuentas multiusuario (`LOOM_SELF_SIGNUP_ORG`); si lo cambiaste, usa aquí la organización de tu cuenta y en
`--org` más abajo:

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom elige los indexadores del nivel de compilador a partir de los archivos de compilación del repositorio (`go.mod`,
  `package.json`, `pyproject.toml`, un `CMakeLists.txt` en la raíz, …); `scip_producer` indica
  uno explícitamente ([Nivel de compilador](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)).
  C/C++ además necesita un árbol de compilación configurado, de modo que sin él un repositorio C/C++ solo obtiene
  el nivel de parser.
- Para un repositorio privado, agrega un token de GitHub (`LOOM_CODE_GITHUB_TOKEN` en `.env`, o
  `github_token` en la entrada) y quita `"public":true`: esa opción es para repositorios públicos en
  su host, y permite que cualquier miembro de la organización se una de inmediato al índice como lector desde
  **Repositories**. Decídelo antes del primer `loom code init`: este establece la política de ingreso del espacio de nombres
  a partir de `public`, y editar `public` después no la cambia (un maintainer
  la cambia con `code.repo.set_access`).

Luego recrea el contenedor `loom` y ejecuta el primer índice:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` se ejecuta con el embedder local del stack publicado. Con un embedder falso (el
`.env` de un checkout, o una instalación actualizada que aún nombra `fake-ngram-*`) se niega y
termina; para probar la configuración de todos modos, agrega `--allow-fake-embedder`. Elegir un modelo después en
**Settings → Models & keys** vuelve a generar los embeddings del índice de código; tras un cambio en `.env` necesita su
propia regeneración de embeddings, como describe [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).

En modo multiusuario, un repositorio listado no tiene maintainer, así que sin `"public":true` la
solicitud de ingreso de un lector queda esperando sin nadie que la apruebe. Después de `loom code init`, da
acceso a alguien como operador con un token generado en el contenedor: `admin` lo convierte
en maintainer, quien aprueba a los lectores a partir de entonces, y `viewer` le permite leer. El
sujeto es el espacio de nombres con el que se registró la cuenta (su nombre de usuario, el sujeto de
sus tokens); el correo de un autorregistro nunca se verifica, por lo que una concesión a él no admite a nadie:

```bash
ADMIN_TOKEN=$(docker compose --project-directory "$HOME/.loom" exec -T loom loom mint-token --subject operator --org dev --namespaces code-myrepo --roles admin --ttl-days 1)
curl -s http://localhost:7777/v1/grants.create -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"dev","namespace":"code-myrepo","subject":"your-namespace","role":"admin"}'
```

<a id="optional-ask-a-question-about-a-document"></a>

## Opcional: hacer una pregunta sobre un documento

```bash
loom demo
```

`loom demo` sube el `AGENTS.md` de este repositorio, lo ingiere con `knowledge.ingest` (el extractor de referencia del stack lo procesa) y hace una pregunta sobre él con `knowledge.synthesize`, e imprime
la respuesta y los fragmentos que citó. Con el LLM simulado imprime en su lugar los primeros resultados de
`knowledge.search`. `--file` y `--question` lo dirigen a tu propio documento.

<a id="common-problems"></a>

## Problemas comunes

| Síntoma                                           | Causa y solución                                                                                                                                                                               |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` no escribe hechos             | El LLM es el falso. Sigue [Modelos y claves](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md) para configurar uno.                                        |
| Los resultados de búsqueda parecen arbitrarios    | El embedder es falso (`fake-ngram-*` en `.env`). Cámbialo como indica [Modelos y claves](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md), lo que vuelve a generar los embeddings de lo almacenado. No borres tus volúmenes para cambiar de modelo. |
| 401 `AUTH_INVALID` después de activar la autenticación | No hay token, o está firmado con otro `LOOM_JWT_SECRET`. Genera uno nuevo con el comando de Compose de [Obtener un token](#3-get-a-token).                                                |
| El dashboard devuelve 503                         | El servidor se ejecuta desde el código fuente (`make server`) sin un frontend compilado. Ejecuta `make frontend-build`.                                                                        |
| La stack publicada no llega a estar saludable     | Ejecuta `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`. Consulta [Solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md). |
| Al añadir un repositorio se responde `SANDBOX_UNAVAILABLE` | La stack se ejecuta sin el sandbox de trabajos de repositorio. Indexa el repositorio mediante `LOOM_CODE_REPOS_JSON` ([Indexar un repositorio](#optional-index-a-repository-on-the-released-stack)). |
| El instalador dice que el puerto `7777` está en uso | Otro programa, u otro Loom, escucha ahí. Deténlo, o ejecuta de nuevo el comando de instalación con `LOOM_PORT=7778` delante de `sh loom-install.sh`; `.env` conserva el puerto.           |
| El instalador dice que el puerto `7777` no responde | Un programa lo ocupa sin responder, o `LOOM_BIND_HOST`, `LOOM_PORT` o un firewall impiden que este host lo alcance.                                                                            |
| El instalador no pudo comprobar el puerto `7777`  | Curl falló por el motivo que indica su código de salida; revisa `LOOM_BIND_HOST` y `LOOM_PORT`.                                                                                                |
| El puerto `8123` o `9000` de un clon está ocupado | Reasigna los puertos del host de ClickHouse ([stack local](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)).                              |
| El instalador no pudo descargar las imágenes      | Docker accede al registro por sí mismo: si estás detrás de un proxy, indica al daemon de Docker la [configuración del proxy](https://docs.docker.com/engine/daemon/proxy/).                     |

<a id="next"></a>

## Siguientes pasos

- [Evaluar Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md): convierte esta prueba de humo en una evaluación de calidad,
  latencia y costo específica para tu carga de trabajo.
- [Conectar un agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md): Claude Code, Codex o Cursor
  sobre este stack.
- [Tu primer agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): identidad, manejo de abstain, sesiones, context packs
  y ejecuciones desde el SDK.
- [Despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): cómo ejecutar Loom para un equipo.
