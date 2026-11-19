<!-- i18n: source=docs/development/release-process.md blob=c00327d2be768494a50aa34f8828ea5bff991faf model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md), which is authoritative.

<a id="release-process"></a>

# Proceso de lanzamiento

Loom sigue SemVer: el servidor, ambos SDK, la aplicación web, las skills raíz (`loom-code`, `loom`), el plugin de Cursor y el paquete Agent Plugins que las distribuyen, y la extensión de Gemini CLI comparten una misma versión `X.Y.Z`. Los plugins de Claude Code y Codex comparten su propia versión, que se incrementa cuando cambia el comportamiento de cualquier archivo que el plugin distribuye (`hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, el `plugin-defaults.json` de Codex): los clientes instalados solo reciben un cambio en un hook mediante una nueva versión del plugin. `MIN_SERVER_VERSION` en el `lib/server-version.mjs` de los plugins indica el servidor más antiguo que sus hooks admiten: cuando `GET /v1/health` del servidor informa de una `version` anterior, SessionStart imprime una línea `loom:` que pide actualizar el servidor, y no dice nada cuando no se puede leer la versión. Sube con cada lanzamiento ([publicar el lanzamiento](#cutting-the-release), paso 2).

<a id="where-the-version-lives"></a>

## Dónde reside la versión

`.venv/bin/python scripts/release/prepare.py bump X.Y.Z` actualiza cada uno de estos archivos en el commit de la versión, salvo los plugins de Claude Code y Codex, y escribe las [notas de la versión](#release-notes):

| Componente                    | Archivos                                                                                                                                                                                                                                                                                |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Servidor                      | `server/__init__.py` (`__version__`), `pyproject.toml`, `uv.lock` y `server.json`                                                                                                                                                                                                       |
| SDK de Python                 | `sdk/python/pyproject.toml` y `sdk/python/loom/_version.py`                                                                                                                                                                                                                             |
| SDK de TypeScript             | `sdk/typescript/package.json`, `sdk/typescript/package-lock.json`, `sdk/typescript/src/version.ts`                                                                                                                                                                                      |
| Aplicación web                | `frontend/package.json` y `frontend/package-lock.json`                                                                                                                                                                                                                                  |
| Skills raíz                   | `.claude-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `plugin.json` y el `metadata.version` de `skills/loom-code/SKILL.md` y `skills/loom/SKILL.md`                                                                                                                               |
| Extensión de Gemini CLI       | `gemini-extension.json`                                                                                                                                                                                                                                                                 |
| Helm chart                    | `deploy/helm/loom/Chart.yaml`: `appVersion` (la etiqueta por defecto de las imágenes del servidor y del extractor, y la etiqueta `app.kubernetes.io/version` del chart) y el `version` propio del chart: una versión menor para una versión mayor o menor, un parche para una versión de parche |
| Plugins de Claude Code y Codex | `integrations/claude-code-plugin/.claude-plugin/plugin.json`, `integrations/codex-plugin/.codex-plugin/plugin.json` (su propia versión; increméntala cuando cambie el comportamiento de cualquier archivo que el plugin incluye: `hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, el `plugin-defaults.json` de Codex) |

El servidor lee su versión solo de `server/__init__.py`; la informan el documento OpenAPI, el `serverInfo` de MCP, `loom report` y `GET /v1/health` (comprobado por `tests/unit/test_health_problems.py`). Estas comprobaciones mantienen los números coherentes:

| Comprobación                                   | Verifica                                                                                                                                         |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `tests/unit/test_release_version.py`           | que cada superficie informa la versión del servidor y que `server.json` mantiene la forma del listado del registro                              |
| `npm run check:version` (SDK de TypeScript)    | que sus cuatro fuentes de versión coinciden                                                                                                      |
| `tests/unit/test_codex_plugin.py`              | que ambos manifiestos de plugin tienen una misma versión y que `MIN_SERVER_VERSION` no es más reciente que la versión del servidor              |
| `tests/unit/test_release_prepare.py`           | que cada archivo que `prepare.py` actualiza nombra la versión del servidor                                                                       |
| `prepare.py check`, `check-tag`, `check-chart` | el job `version` de la versión ([paso 1](#what-the-release-workflow-does)): una sola versión en todas partes, el orden de los candidatos, el chart actualizado |

Dos pull requests de plugins que suben al mismo número se fusionan sin conflicto, y la segunda no entrega nada a los clientes que ya se actualizaron con la primera. Comprueba la versión del plugin en `main` antes de incrementarla.

<a id="release-notes"></a>

## Notas de la versión

La ejecución que crea el GitHub Release genera sus notas a partir de los commits desde la etiqueta de la versión final anterior (`prepare.py previous`), de modo que una versión final sobre el commit de su candidata también los lista;
una nueva ejecución o el despacho posterior al registro las mantiene tal como están. Lo que los commits no pueden decir va en una
sección "Release notes" de la descripción del pull request de la versión, de modo que se revisa antes de la etiqueta:
las entradas del changelog de los pull requests, agrupadas por su categoría del changelog (los pull requests de una
categoría que no necesita entrada se omiten), luego los pasos para operadores (nuevos ajustes, backfills, reconstrucciones de tablas)
y los cambios de protocolo visibles para el cliente, incluidos todos los campos de solicitud eliminados y las opciones retiradas.
No existe un registro de campos eliminados, así que las notas de la versión son donde un cliente se entera de uno.
`prepare.py` escribe la sección en `release-notes.md` (sin commit), a partir de los squash merges
desde la etiqueta final más alta por debajo de la versión:

- la entrada del changelog de cada pull request bajo su categoría, en el orden de la plantilla, o su título cuando
  la entrada está vacía; aquel cuya descripción no nombra una única categoría se lista bajo "Unclassified"
  para que el autor lo ubique;
- cada elemento de "Operator steps", tal como está escrito, bajo el número de su pull request, de todos los pull requests,
  incluido uno de una categoría que no necesita entrada; un elemento que dice que no hay ninguno (`none`, `N/A`,
  `No operator steps.`) o que es el texto propio de la plantilla se omite, y el autor elimina el resto
  que no nombre ningún paso;
- la diferencia del protocolo ([generación de código](code-generation.md#wire-changelog)).

Una corrección que un parche de una línea anterior ya publicó se lista de nuevo, y también un pull
request revertido en el mismo rango, junto a su reversión; el autor los elimina.

Obtiene por sí mismo las etiquetas del repositorio upstream, lee los pull requests con `gh` y la diferencia del protocolo a través del registro del servidor, por lo que necesita un `gh` con sesión iniciada y el entorno del proyecto
(`uv sync`). `.venv/bin/python scripts/release/prepare.py notes X.Y.Z` imprime de nuevo la sección
sin cambiar ninguna versión: ejecútalo tras hacer rebase del pull request de la versión justo antes de que se fusione,
y de nuevo en el commit de la candidata con el número más alto antes de la etiqueta final, para que las notas nombren todos los pull requests
que incluye la etiqueta.

<a id="cutting-the-release"></a>

## Publicar la versión

1. Comprueba que una ejecución programada de `.github/workflows/docker-smoke.yml` en `main` de la última semana
   haya pasado:

   ```bash
   gh run list -R ClickHouse/loom --workflow docker-smoke.yml --event schedule --branch main -L 1 --json conclusion,createdAt,headSha
   ```

   O inicia una nueva con `gh workflow run docker-smoke.yml -R ClickHouse/loom --ref main` y léela
   con `--event workflow_dispatch` en lugar de `--event schedule`. Analiza ambas imágenes igual que
   lo hace la versión, y el fallo de una ejecución programada solo notifica a la última persona que editó su programación. Un
   análisis en rojo nombra el paquete: mueve primero en `main` la fijación que lo incorpora (un `ARG` del `Dockerfile` o el digest
   de la imagen base, o un archivo de bloqueo). Los paquetes propios de la imagen base solo cambian con un nuevo digest
   de la imagen base; un paquete que añade una capa `apt-get install` cambia con cualquier compilación sin caché.

2. En una rama a partir de `main`, ejecuta `.venv/bin/python scripts/release/prepare.py bump X.Y.Z` y abre un
   pull request con sus cambios y con `release-notes.md` como la sección de [notas de la versión](#release-notes)
   de su descripción; edita las notas donde alguna entrada lo necesite. CI debe pasar, incluidos
   `make schema-check` y el ensayo estricto de migraciones.
   Cuando la entrada del changelog de un pull request fusionado indica que necesita la próxima versión del servidor, ese mismo
   pull request sube `MIN_SERVER_VERSION` en
   `integrations/claude-code-plugin/lib/server-version.mjs` a esta versión, lo sincroniza con el
   plugin de Codex y sube la versión de ambos plugins: los clientes lo reciben solo mediante una nueva versión del plugin.
3. Después de fusionar el pull request de la versión, etiqueta su commit de fusión y sube la etiqueta. Para probar
   antes un candidato a versión, etiqueta ahí `vX.Y.Z-rc.1` en su lugar, y la versión final más tarde sobre el commit del
   candidato con el número más alto (más abajo):

   ```bash
   git tag vX.Y.Z <merge commit>
   git push origin vX.Y.Z
   ```

4. La subida de la etiqueta ejecuta `.github/workflows/release.yml` (más abajo), publica las imágenes del servidor y del
   extractor, y solo entonces crea el GitHub Release; después comprueba desde fuera el instalador publicado
   (`release-check`). Lee esa ejecución antes del paso 5.
5. Para publicar los SDK y la entrada del MCP Registry, ejecuta manualmente el mismo workflow sobre la etiqueta:

   ```bash
   gh workflow run release.yml --ref vX.Y.Z
   ```

   Cuando los jobs `pypi` y `npm` de esa ejecución pasen, comprueba los SDK tal como los instala un usuario, con la
   actualización desde la versión anterior:
   `gh workflow run release-check.yml -f version=X.Y.Z -f previous_version=<previous X.Y.Z>`.
   `previous_version` debe ser una versión publicada anterior a `version` que incluya
   `install.sh` y `loom-stack.tar.gz`; omítela cuando no haya ninguna.

6. Pega las notas de la versión generadas en el commit de la etiqueta final ([notas de la versión](#release-notes)) en
   el GitHub Release, encima de las notas generadas.

Un candidato a versión es una etiqueta `vX.Y.Z-rc.N` y crea un GitHub pre-release; la publicación rechaza cualquier
otra etiqueta de prelanzamiento. El primer candidato va sobre el commit de fusión del pull request de la versión, cuyos
archivos nombran `X.Y.Z`, de modo que el servidor del candidato ya informa `X.Y.Z`; uno posterior, tras una corrección,
va sobre un commit posterior de `main`. La etiqueta final `vX.Y.Z` va sobre el commit del candidato con el número más alto
tras la prueba. El paquete de la versión final conserva la versión de ClickHouse del candidato con el número más alto
en ese commit que tenga un paquete publicado; sus imágenes se construyen de nuevo desde el mismo
commit, sin caché, así que un paquete del sistema operativo puede ser más reciente que en la imagen probada. Se rechaza la ejecución de una
etiqueta de candidato posterior a su versión final. A una en el commit de la versión final se le indica que ejecute la final. Una
con número inferior al del candidato más alto en el commit de la versión final, como un candidato antiguo
lanzado para publicar sus SDK, no tiene nada que ejecutar, y su etiqueta permanece. Cualquier otra es una etiqueta huérfana: además
hace fallar las ejecuciones posteriores de la versión final hasta que se elimine. El `appVersion` del chart también nombra `X.Y.Z`,
así que una prueba en Kubernetes establece `image.tag` y `extractor.image.tag` en `X.Y.Z-rc.N`.

Solo la versión final más alta se convierte en el Latest release del repositorio (`releases/latest` sirve el
instalador) y mueve `:latest`: un prelanzamiento o un parche de una línea anterior deja ambos donde están.
Las versiones se leen de las etiquetas del repositorio, así que elimina una etiqueta final que no vaya a publicarse, o
Latest se queda por debajo de ella.

<a id="rehearsing-a-release"></a>

## Ensayar un release

Una ejecución de prueba (dry run) compila, verifica y ejecuta pruebas de humo de todo lo que haría un release, en cualquier rama, y no publica nada: ni push de imágenes, ni GitHub Release, ni subida a registros.

```bash
gh workflow run release.yml --ref <branch> -f dry_run=true
```

Se ejecuta con la versión que indica `server/__init__.py` y conserva sus artefactos un día (un release los conserva 30 días, mientras se pueda volver a ejecutar una corrida). Sus imágenes van a los artefactos de la ejecución en lugar de ghcr.io, sin las atestaciones de procedencia ni de SBOM, que solo conserva un registro, y `docker-smoke` las carga desde ahí. El job `preflight` informa de cada [requisito previo](#before-the-first-release) que falta como una advertencia en lugar de fallar. Lo que solo ejercita un release publicado, una ejecución de prueba no lo ejercita: `docker-tag` (unir los dos digests de cada imagen, la decisión de Latest, `:latest`), `sign`, el GitHub Release y las firmas de sus archivos, las publicaciones en registros, `release-check`, la comprobación de imágenes publicadas (`scripts/release/published_images.sh`) con la ruta de compilación omitida que selecciona, la comprobación del orden de candidatos (`prepare.py check-tag`, que una ejecución de prueba de una rama omite con un aviso) y la comprobación del chart cuando la versión ya está etiquetada en otro commit.

<a id="before-the-first-release"></a>

## Antes del primer release

Un release publica paquetes públicos, y una versión de un registro nunca puede reutilizarse, por lo que el workflow
rechaza un release desde un repositorio privado. El job `preflight` comprueba, en segundos y antes de cualquier build,
lo que solo un propietario puede conceder, e indica la solución para cada requisito que falte:

| Requisito                                                                                        | Comprobado por                                                  |
| ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------- |
| El repositorio es público                                                                        | `preflight`                                                     |
| `ghcr.io/clickhouse/loom` y `loom-extractor` dan acceso de escritura (Write) a este repositorio  | `preflight` (una sesión de subida que abre y cancela)           |
| Ambos paquetes son públicos, ya que una instalación descarga sin iniciar sesión                  | `preflight` (un token de descarga anónimo)                      |
| Un entorno `pypi` y un trusted publisher de PyPI para `release.yml`                             | el job `pypi`                                                   |
| Un entorno `npm` y un trusted publisher de npm para `release.yml`                                | el job `npm`                                                    |
| Un entorno `mcp-registry` con las mismas reglas de protección que `pypi` y `npm`                 | el job `mcp-registry`                                           |
| La retención de artefactos del repositorio es de al menos 30 días (Settings > Actions)           | nada                                                            |
| Las ramas `release/**` están protegidas como `main` (comprobaciones obligatorias, sin push directo) | nada                                                         |

Solo un push crea un paquete de ghcr.io, y lo crea como privado. Para una imagen que ningún release ha subido
todavía, `preflight` emite una advertencia en lugar de fallar, y los jobs `docker-smoke` de esa ejecución fallan al descargarla: configura
la visibilidad del paquete como pública en su configuración y luego vuelve a ejecutar los jobs fallidos.

Hasta que un release se haya firmado, haz que la siguiente etiqueta sea un release candidate (`vX.Y.Z-rc.1`). Su ejecución
firma las imágenes y las verifica desde fuera, y un candidato nunca mueve `:latest`, así que un fallo
ahí cuesta un número de candidato, no una versión final.

<a id="what-the-release-workflow-does"></a>

## Qué hace el workflow de release

`.github/workflows/release.yml` compila una vez, verifica los archivos exactos y publica esos mismos archivos.

**Al hacer push de una etiqueta (`v*`)**:

1. `version` toma la versión de la etiqueta (`vX.Y.Z` → `X.Y.Z`) y rechaza una referencia que
   no sea una etiqueta `v*` (salvo que la ejecución sea un [ensayo](#rehearsing-a-release)) o que no sea `X.Y.Z` ni
   `X.Y.Z-rc.N`. Falla cuando alguna de estas condiciones es falsa (para `vX.Y.Z-rc.N`, respecto de `X.Y.Z`):
   - `prepare.py check`: `server/__init__.py` contiene `__version__ = "X.Y.Z"`, y todos los demás
     archivos de versión, incluido `server.json`, nombran esa versión;
   - `prepare.py check-tag`: un candidato `vX.Y.Z-rc.N` antes de etiquetar `vX.Y.Z`, y un `vX.Y.Z`
     final sobre el commit de su candidato con el número más alto;
   - `prepare.py check-chart`: la versión del chart es superior a la de la release anterior;
2. `preflight` hace fallar la ejecución si falta un [requisito previo](#before-the-first-release);
3. `clickhouse` nombra la release de ClickHouse que ejecuta el bundle de Compose
   (`scripts/release/release_clickhouse.sh`), migra un servidor nuevo de esa release y ejecuta
   `make schema-check` antes de que `build` pueda fijarla, y rechaza un downgrade de ClickHouse (véase el paso 8),
   de modo que una ejecución así se detiene antes de compilar y publicar imágenes
   ([cuándo se detiene](#when-the-clickhouse-job-stops)). La release es la primera de estas:
   - la que el bundle ya publicado de esta versión ya nombra, de modo que volver a ejecutar el comando de instalación
     para una versión nunca mueve ClickHouse (una release sin bundle se resuelve de nuevo, ya que
     `github-release` nunca reemplaza uno, así que nadie puede haberla instalado);
   - el `clickhouse_image` de un dispatch, que para una versión final se usa en lugar de cualquier release que
     sus candidatos sobre el mismo commit hayan publicado;
   - para una versión final, la release con la que se probó el candidato de número más alto sobre el mismo commit
     que tenga un bundle publicado;
   - la release que `clickhouse/clickhouse-server:latest` nombra ahora (`dev/clickhouse_release.sh`);
4. `build` escribe la versión en las fuentes del SDK de Python (en su forma PEP 440, `X.Y.ZrcN`
   para un candidato) y en las fuentes del SDK de TypeScript, y compila el wheel y el
   sdist del SDK de Python, el tarball de npm (después del `check:dist` del SDK de TypeScript) y los zips de los dos
   plugins (`loom-claude-code-plugin-vX.Y.Z.zip`, `loom-codex-plugin-vX.Y.Z.zip`); también
   compila el bundle de Compose sin código fuente y su archivo SHA-256 (que nombra esa release
   de ClickHouse), el `install.sh` de la release, que instala esta versión, y
   `agent-setup.txt`, el [contrato de configuración del agente](../getting-started/agent-setup.md) con
   esta versión y este commit rellenados (`scripts/release/agent_setup.py`);
5. `verify` comprueba esos archivos exactos: sus nombres y versiones incrustadas, los manifiestos
   de los plugins, que no haya tests en el zip de Claude Code, los bits de ejecución que registra git, y
   que el bundle nombre la release de ClickHouse que comprobó el job `clickhouse`,
   que `install.sh` instale esta versión y que `agent-setup.txt` la fije, y ejecuta una prueba de humo
   del wheel en un venv limpio (`scripts/gates/sdk_wheel_smoke.py`);
6. `docker` compila `ghcr.io/clickhouse/loom` (con el commit de la etiqueta como `LOOM_BUILD_COMMIT`,
   lo que `/v1/health` informa como `build_commit`) y `ghcr.io/clickhouse/loom-extractor`
   (el servicio extractor de documentos), cada una para linux/amd64 y linux/arm64 en el
   runner propio de esa arquitectura y sin caché de compilación (es decir, a partir de los paquetes
   tal como están ese día), con procedencia y un SBOM, publica cada una por digest y la escanea;
   `docker-tag` une los dos digests de cada imagen bajo sus etiquetas, y no etiqueta ninguna imagen hasta
   que las cuatro hayan pasado (un fallo ahí puede dejar una etiquetada, y volver a ejecutarlo etiqueta ambas);
   `sign` firma después, por digest, el índice que `docker-tag` creó para cada imagen, con una firma
   sin clave (cosign, con la identidad de GitHub de la ejecución, de modo que el certificado nombra `release.yml`
   y la etiqueta); una ejecución sobre una etiqueta cuyas imágenes ya están publicadas no firma nada. Cada ejecución verifica entonces
   las firmas de ambas imágenes como lo hacen los usuarios, de modo que la GitHub Release, PyPI, npm y el MCP Registry
   esperan una firma que se verifique ([una etiqueta o firma fallida](#a-failed-tag-or-signature));
7. `docker-smoke` instala el bundle con esas imágenes, sobre la release de ClickHouse que
   nombra, en amd64 y arm64, comprueba que la imagen del servidor nombre el commit de la etiqueta y luego ejecuta
   el `install.sh` y el bundle de la propia release como el comando de una línea en contenedores limpios de Debian, Ubuntu
   y Alpine (instalación, ida y vuelta, nueva ejecución, un downgrade rechazado con el `sh` y el `awk`
   propios de cada uno, y una actualización sobre el instalador de la
   release final anterior o, en un ensayo de un commit posterior a la release de su versión, sobre el de
   esa misma release; `scripts/gates/installer_rehearsal.py`);
8. `github-release` crea la GitHub Release una vez que ambas imágenes existen y están verificadas, con notas
   generadas a partir de los commits desde la release final anterior (`prepare.py previous`, una
   salida de `version`; las notas de un candidato también empiezan ahí), o la actualiza en una nueva ejecución, conservando
   sus notas tal como están, y adjunta los archivos verificados. Un bundle que la release ya
   lleva se conserva, y también su checksum (si la release lo perdió, se escribe de nuevo a partir
   del bundle): reemplazar un recurso lo borra primero, y una subida fallida dejaría una versión instalada
   sin su bundle. Un bundle nuevo nunca nombra una release de ClickHouse más antigua que
   la de los bundles de las 30 releases más recientes (prereleases incluidas), ya que una instalación
   que actualice a él arrancaría un servidor más antiguo sobre datos que escribió uno más nuevo; en su lugar el job falla,
   salvo que el dispatch añada `-f allow_clickhouse_downgrade=true`. Este job se ejecuta
   para una etiqueta a la vez, de modo que dos etiquetas publicadas juntas no pueden pasar ambas la comprobación contra
   el mismo bundle más antiguo. Solo espera un job más de este tipo: la publicación de una tercera etiqueta cancela
   el que espera, que entonces debe volver a ejecutarse a mano (Re-run failed jobs). Un rechazo aquí
   llega después de que `docker-tag` haya publicado las etiquetas de imagen de la etiqueta (y `:latest` cuando es la versión
   final más alta). Una nueva ejecución conserva las entradas de la ejecución y vuelve a fallar, así que hay que hacer dispatch de la etiqueta, lo
   que además publica los SDK y la entrada del MCP Registry. Hágase dispatch o bien con la release más reciente
   que nombra el error, cuando esa release volcó el `schemas/schema.sql` de la etiqueta:
   `gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`;
   o bien aceptando el downgrade:
   `gh workflow run release.yml --ref vX.Y.Z -f allow_clickhouse_downgrade=true`.
   Cuando no se toma ninguna de las dos, devuelva `:latest` (si se movió) y, para una versión final, `:X.Y` a la
   release anterior en ambas imágenes, como en [una etiqueta o firma fallida](#a-failed-tag-or-signature);
9. `sign-files` descarga cada archivo que la release sirve ahora, como lo hacen los usuarios
   (`scripts/release/download_release_files.sh`), y firma cada uno que sea idéntico byte a byte a
   la compilación de esta ejecución con una firma sin clave como la de las imágenes, en un bundle de Sigstore
   `<file>.sigstore.json` (`scripts/release/sign_release_files.sh`). El bundle del stack y el
   checksum que la release conservó de una ejecución anterior mantienen sus bundles de Sigstore cuando estos se verifican;
   cualquier otro archivo que no sea la compilación de esta ejecución hace fallar el job, sin firmar nada
   ([una etiqueta o firma fallida](#a-failed-tag-or-signature)). Después verifica cada archivo como
   lo hacen los usuarios (`scripts/release/verify_release_files.sh`). Mantiene el token OIDC de la ejecución en un job
   propio, con contents de solo lectura; `attach-signatures` sube entonces los bundles de Sigstore que creó
   (uno conservado se queda como está, ya que reemplazar un recurso lo borra primero). Un dispatch compila
   los archivos de nuevo y `github-release` los reemplaza, así que hasta que terminen sus propios `sign-files` y
   `attach-signatures`, los bundles de Sigstore de la ejecución de la etiqueta no verifican los archivos reemplazados;
10. `release-check` (en un push de etiqueta) ejecuta `.github/workflows/release-check.yml` contra lo que
    se acaba de publicar, sin iniciar sesión en nada: el comando de una línea desde github.com en los mismos contenedores
    limpios, descargando ambas imágenes de forma anónima, y verifica las firmas de ambas imágenes y
    de cada archivo de la release, que deben provenir de `release.yml` en esa etiqueta, como lo hace un usuario
    ([imágenes de contenedor](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#container-images),
    [archivos de la release](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#release-files)); un archivo sin su
    `.sigstore.json` falla. Una release publicada antes de que el workflow de release firmara sus imágenes
    o sus archivos carece de esas firmas, por lo que una comprobación de ella hace fallar su job `signatures`.

Las imágenes del servidor y del extractor se publican antes que la GitHub Release, de modo que un instalador
mostrado en la página de la release nunca apunte a una imagen inexistente.

**En dispatch manual** (`workflow_dispatch` sobre una referencia de etiqueta), se ejecutan los mismos jobs excepto
`release-check`, sin compilar ninguna imagen cuando las imágenes ya están publicadas (más abajo). `build` también
ejecuta `npm run check` del SDK de TypeScript (lint, tipos, versión, ejemplos del README, tests). Después se ejecutan
tres jobs de publicación:

| Job            | Publica                                                          | Cómo                                                                                                                                                                                                                                  |
| -------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pypi`         | `clickhouse-loom` (solo el SDK de Python) en PyPI                | el wheel y el sdist verificados; trusted publishing (OIDC); los archivos que PyPI ya tiene se omiten                                                                                                                                  |
| `npm`          | `@clickhouse/loom` en npm con procedencia                        | el tarball verificado; una versión publicada se omite; `latest` solo para la versión final más alta, una prerelease a `next`, un parche a una línea anterior a `release-X.Y`; trusted publishing (OIDC)                               |
| `mcp-registry` | `server.json` en el MCP Registry como `io.github.ClickHouse/loom` | el archivo confirmado, cuya versión el job `version` comprueba contra la etiqueta; inicio de sesión con GitHub OIDC con un `mcp-publisher` fijado; una versión ya listada y una prerelease se omiten; `preflight` rechaza un repositorio privado antes de cualquier compilación |

Las imágenes ya están publicadas por la ejecución de la etiqueta: un dispatch sobre una etiqueta cuyas imágenes llevan
la versión no compila ni etiqueta ninguna (`scripts/release/published_images.sh`), hace pruebas de humo de las
publicadas y continúa con los registros. Las imágenes de la release se compilan sin caché, por lo que una recompilación
movería las etiquetas de la versión a otros bytes.

Nunca mueva una etiqueta `X.Y.Z` publicada: `published_images.sh` falla cuando una imagen publicada se compiló
desde un commit distinto al de la ejecución, y una corrección necesita una versión nueva.

Una nueva ejecución tras una publicación parcial es segura: cada job de publicación omite lo que ya existe. La única
excepción es una ejecución de etiqueta cuya firma de archivos falló, que [una etiqueta o firma
fallida](#a-failed-tag-or-signature) recupera.

**Por qué publicar los archivos verificados.** Un job que recompilara su propio artefacto podría publicar bytes sin comprobar. Publicar lo que `verify` aprobó significa que la GitHub Release y los registros llevan los mismos bytes.

**Por qué el servidor no está en PyPI.** El servidor se despliega junto a ClickHouse, no se importa como biblioteca; se distribuye como imagen. El `pyproject.toml` raíz está marcado como `Private :: Do Not Upload`, y PyPI solo contiene el SDK. Los jobs `pypi` y `npm` necesitan trusted publishers registrados para este repositorio (sus entornos de GitHub).

**Por qué la publicación de los SDK es manual.** Una publicación en un registro no se puede deshacer: PyPI nunca acepta
una versión dos veces. Publicar los SDK solo con dispatch permite comprobar primero los archivos de la release.
La imagen del servidor es automática porque es un requisito previo del instalador de la GitHub Release,
y crear la etiqueta de release firmada es su puerta de revisión.

<a id="a-failed-tag-or-signature"></a>

### Una etiqueta o firma fallida

Un `docker-tag` fallido (una imagen etiquetada, la otra no) o un `sign` fallido (Sigstore caído, por ejemplo):

- Use "Re-run failed jobs" en la ejecución que etiquetó las imágenes (el push de la etiqueta, o el dispatch que
  las compiló y etiquetó), dentro de los 30 días posteriores; los artefactos de la ejecución se conservan otro tanto, salvo que el
  límite de retención de artefactos del repositorio sea más corto. Ese intento conserva su resultado de `preflight` y
  los digests de `docker-tag`, así que etiqueta la misma compilación y firma lo que etiquetó.
- Una vez que la ejecución de etiquetado ha firmado, un dispatch fallido o un segundo push sobre la etiqueta se recupera con su
  propio "Re-run failed jobs", o con un nuevo dispatch.
- No vuelva a ejecutar nunca una ejecución que falló antes de etiquetar una vez que otra ejecución ha etiquetado: su `preflight` no vio
  imágenes, así que recompila y movería las etiquetas publicadas.
- Una vez que "Re-run all jobs" ha reemplazado el intento de la ejecución de etiquetado, o han pasado los 30 días, ninguna
  ejecución puede etiquetar ni firmar esa compilación: una ejecución sobre una versión medio etiquetada falla su `preflight`, y una ejecución
  sobre una etiqueta publicada verifica las firmas pero no puede crearlas. Publique una versión nueva.

Hasta que esa versión salga, las etiquetas móviles de la compilación siguen nombrándola. Apunte `:latest` (si se movió) y,
para una versión final, `:X.Y` de nuevo a la release anterior que llevaba cada una, en ambas imágenes. Una `X.Y.0` no tiene
una release anterior que lleve `:X.Y`, que nombra esta compilación hasta que salga `X.Y.1`. Con
`<previous>` la versión de esa release, la etiqueta se mueve solo una vez que la firma de esa release se verifica:

```bash
digest=$(docker buildx imagetools inspect ghcr.io/clickhouse/<image>:<previous> \
  --format '{{json .Manifest}}' | jq -re .digest)
cosign verify "ghcr.io/clickhouse/<image>@$digest" \
  --certificate-identity https://github.com/ClickHouse/loom/.github/workflows/release.yml@refs/tags/v<previous> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null \
  && docker buildx imagetools create -t ghcr.io/clickhouse/<image>:<tag> "ghcr.io/clickhouse/<image>@$digest"
```

Para un `<previous>` publicado antes de que el workflow de release firmara, que no lleva firma, ejecute
la línea `digest=` y luego el comando `docker buildx imagetools create` sin el
`cosign verify … &&` anterior. En ambos casos, compruebe que la etiqueta nombra el digest:

```bash
[ -n "$digest" ] && [ "$(docker buildx imagetools inspect ghcr.io/clickhouse/<image>:<tag> --format '{{json .Manifest}}' | jq -re .digest)" = "$digest" ] \
  || { echo "<tag> does not name $digest" >&2; false; }
```

Un `sign-files` o `attach-signatures` fallido (Sigstore o la API de GitHub caídos, por ejemplo) deja archivos en
la release sin un bundle de Sigstore que los verifique. Cualquier ejecución posterior los firma de nuevo mientras
el bundle del stack y el checksum de la release lleven bundles de Sigstore que se verifiquen: "Re-run failed jobs" en
la ejecución que falló. Cuando no llevan ninguno, porque falló la propia firma de la ejecución de la etiqueta, solo "Re-run
failed jobs" en la ejecución de la etiqueta los firma, mientras sus artefactos se conserven (30 días, salvo que el
límite de retención de artefactos del repositorio sea más corto) y antes de cualquier dispatch: un dispatch, "Re-run all
jobs" o un segundo push de la etiqueta recompila el bundle del stack y el checksum pero conserva los que la
release lleva, que entonces ninguna ejecución puede firmar. Pasado ese punto, publique una versión nueva.

Un `release-check` que falla en un listado o una descarga: "Re-run failed jobs". Uno que encuentra un
bundle de Sigstore que no se verifica después de que `sign-files` y
`attach-signatures` de la última ejecución de release hayan pasado significa que la release cambió después de firmarse: un incidente de seguridad
([SECURITY.md](https://github.com/ClickHouse/loom/blob/main/SECURITY.md)). Una ejecución cuya firma falló se recupera como se indicó arriba.

`sign-files` rechaza cualquier archivo que no sea de la compilación de esta ejecución, excepto un bundle de stack o una suma de verificación conservados cuyo bundle de Sigstore se verifique. Un archivo adjuntado a la release a mano: elimínelo de la release cuando el workflow no lo genere y luego pulse "Re-run failed jobs". Un bundle de stack o una suma de verificación conservados sin bundle de Sigstore nunca se firmaron (arriba). Cualquier otro rechazo, y un archivo conservado cuyo bundle de Sigstore no se verifique fuera de una caída de Sigstore, significa que el archivo se modificó fuera del workflow: es un incidente de seguridad, no un fallo de firma.

<a id="when-the-clickhouse-job-stops"></a>

### Cuando el job clickhouse se detiene

Una ejecución que se detuvo en `clickhouse` no publicó nada.

- **`latest` apunta a una release defectuosa**: lance el tag con una release exacta,
  `gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`.
  Un bundle ya publicado conserva el suyo, y la ejecución avisa de que ignoró el valor indicado.
- **La release no genera el `schemas/schema.sql` del tag**: regenérelo con esa release (los comandos del
  [volcado del esquema](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md#schema-dump-and-check) con
  `LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D`) y haga commit. Para un candidato, cree el
  siguiente tag `-rc.N` en el commit de la corrección. Para una versión final (una sin ningún candidato en su commit que
  haya publicado un bundle, ya que solo ese resuelve `latest`), elimine su tag
  (`git push origin :refs/tags/vX.Y.Z`, y `git tag -d vX.Y.Z` en cada clon que lo haya descargado, ya que
  un fetch nunca elimina un tag) y después cree un nuevo candidato en el commit de la corrección. En ambos casos lance el
  nuevo candidato con el mismo `-f clickhouse_image=...`, ya que el push de su tag vuelve a resolver `latest`;
  déjelo en observación y cree ahí el tag de la versión final, lo que conserva esa release.
- **Un downgrade durante la observación**: también se comprueba una versión final que lleva la release de su candidato, de modo que un
  parche a una línea anterior publicado durante la observación con una release más reciente la detiene. Hay dos salidas:
  - elimine el tag de la versión final como se indicó (no publicó nada), cree un nuevo candidato en ese commit,
    déjelo en observación y vuelva a crear ahí el tag de la versión final. El push del tag del candidato resuelve `latest`; cuando
    esa release es anterior a la que nombra el error, lance el candidato con esa release:
    `gh workflow run release.yml --ref vX.Y.Z-rc.N -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`;
  - o lance la versión final con `-f allow_clickhouse_downgrade=true` y acepte el downgrade para
    las instalaciones de ese parche.

Cada lanzamiento anterior también ejecuta los jobs `pypi` y `npm`, así que un candidato lanzado publica
sus SDK una vez aprobados sus entornos; rechace esas aprobaciones para publicar solo las imágenes y
la GitHub Release.

<a id="installing-from-a-github-release"></a>

## Instalación desde un GitHub Release

Cuando los registros no se han publicado, instala desde los recursos del release:

```bash
gh release download vX.Y.Z --repo ClickHouse/loom --pattern 'clickhouse_loom-*.whl'
pip install ./clickhouse_loom-*.whl

gh release download vX.Y.Z --repo ClickHouse/loom --pattern 'clickhouse-loom-*.tgz'
npm install ./clickhouse-loom-*.tgz
```

Los plugins para agentes de programación se instalan desde los marketplaces del repositorio (`claude plugin marketplace add ClickHouse/loom`, `codex plugin marketplace add ClickHouse/loom`). Los archivos zip son para máquinas sin acceso al repositorio.

<a id="listings"></a>

## Listados

Los agentes y las personas encuentran Loom a través de estos listados. Cada uno lee archivos de este repositorio:

| Listado                                        | Lee                                                                                                                                                                                  | Cómo se actualiza                                                                                                                                                                                                                            |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MCP Registry y los directorios que lo copian   | `server.json`: sus metadatos y un remoto en `https://{loom_host}/mcp` con un encabezado `Authorization` secreto opcional                                                             | el job `mcp-registry` en cada versión final despachada                                                                                                                                                                                       |
| Página de PyPI de `clickhouse-loom`            | los metadatos de `[project]` de `sdk/python/pyproject.toml` y `sdk/python/README.md` como cuerpo de la página                                                                        | el job `pypi` en cada versión despachada; la página muestra la versión final más alta, o el candidato más alto antes de la primera versión final                                                                                             |
| Página de npm de `@clickhouse/loom`            | los metadatos de `sdk/typescript/package.json` y `sdk/typescript/README.md` como cuerpo de la página                                                                                 | el job `npm` en cada versión despachada; la página muestra la versión etiquetada como `latest`, que el job asigna solo a la versión final más alta                                                                                           |
| Directorio de plugins de Claude                | `integrations/claude-code-plugin/.claude-plugin/plugin.json` (`displayName`, `description`, `keywords`, `icon`, `documentationUrl`, `supportUrl`, `privacyPolicyUrl`) y el README    | se envía una sola vez en el portal para desarrolladores; las versiones posteriores se toman de la rama seguida. El icono se lee solo en el primer envío                                                                                      |
| Sugerencias de plugins de Claude Code          | el bloque `relevance` de la entrada `loom` en `.claude-plugin/marketplace.json`: el comando `loom`, los archivos de configuración de Loom, una dependencia del SDK en un manifiesto | se lee del marketplace en la rama predeterminada; se muestra solo donde un administrador incluye el marketplace en la lista de permitidos (`pluginSuggestionMarketplaces`)                                                                    |
| Cursor Marketplace                             | `.cursor-plugin/plugin.json` (`displayName`, `description`, `keywords`, `logo`, `variables`), `.cursor-plugin/marketplace.json`                                                      | no se envía; un envío en cursor.com/marketplace/publish se revisa a mano, y también cada actualización                                                                                                                                       |
| Marketplace de Awesome Copilot                 | el `plugin.json` y el `mcp.json` de la raíz (el paquete Agent Plugins), en la etiqueta de versión y el commit que indica el envío                                                    | no se envía; un envío pasa por el formulario de incidencia de plugins externos de `github/awesome-copilot`, que comprueba el paquete y lo instala con Copilot CLI; una versión nueva es un pull request a `plugins/external.json` de ese repositorio |
| Directorio de plugins de Codex                 | `integrations/codex-plugin/.codex-plugin/plugin.json` (`interface`)                                                                                                                  | se envía en el portal de plugins; un plugin con hooks de ciclo de vida no es elegible allí, así que el listado público necesita una compilación sin ellos                                                                                    |
| Índice de documentación para agentes de código | `context7.json`                                                                                                                                                                      | se actualiza desde la rama predeterminada una vez que el repositorio se ha añadido una vez                                                                                                                                                   |
| Índice de Agent Skills                         | `skills/loom-code`, `skills/loom` y las skills del plugin de Claude Code; las skills para colaboradores están marcadas como internas en su frontmatter y no se ofrecen               | se ordena por las instalaciones de `npx skills add ClickHouse/loom`; no hay nada que enviar                                                                                                                                                  |

El job `mcp-registry` se ejecuta en el entorno `mcp-registry`. Dale las mismas reglas de protección que a `pypi` y `npm` antes del primer despacho: GitHub crea un entorno que falta sin ninguna. Su inicio de sesión no necesita ningún secreto.

Después de cambiar el manifiesto de Claude o la entrada del marketplace, y antes de hacer push, ejecuta `claude plugin validate integrations/claude-code-plugin --profile directory --strict` para el plugin y `claude plugin validate .` para el marketplace: ejecutan las comprobaciones estructurales del directorio, y las del plugin deben pasar sin ninguna advertencia.

<a id="after-the-release"></a>

## Después del release

- Despliégalo ([actualizaciones](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)). Los pasos para operadores de las notas del release son la lista de verificación del despliegue.
- Los plugins de cliente se actualizan mediante el gestor de plugins de cada cliente, no mediante un despliegue del servidor.

<a id="related"></a>

## Relacionado

- [Campos desconocidos y eliminados](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields): cómo ve un
  cliente un campo que una versión ha eliminado
- [Contribuir](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md)
