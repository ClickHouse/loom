<!-- i18n: source=docs/guides/teaching.md blob=5ff57fa6e6aa684faaf742b9ce4b6c698eda6682 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md), which is authoritative.

<a id="teaching-with-loom"></a>

# Enseñar con Loom

Loom puede servir como entorno de laboratorio para un curso sobre agentes de IA, recuperación de información, bases de datos o ingeniería de software. Cada estudiante ejecuta su propio stack de un solo usuario, en un portátil con Docker o en un codespace de GitHub sin nada instalado en su máquina. Cada laboratorio enlaza la página que hace el trabajo.

<a id="setup"></a>

## Configuración

| Los estudiantes tienen                | Empiecen con                                                                                                      |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Una cuenta de GitHub y un navegador   | [Pruébalo en GitHub Codespaces](../getting-started/quickstart.md#try-it-in-github-codespaces)                     |
| Docker en su propia máquina           | [Inicio rápido: vía rápida](../getting-started/quickstart.md#fast-path-released-image-no-clone)                   |

Un codespace consume la cuota de Codespaces del estudiante mientras se ejecuta y mientras se conserva su disco, y
GitHub elimina, junto con sus datos, el que se deja sin usar durante su período de retención
([facturación](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces),
[eliminación automática](https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces)).
Los `hostRequirements` del dev container fijan la máquina más pequeña en la que se ejecuta
([`.devcontainer/devcontainer.json`](https://github.com/ClickHouse/loom/blob/main/.devcontainer/devcontainer.json)).

La instalación estándar no necesita ninguna clave de API: genera los embeddings con un modelo local incluido, por lo que la búsqueda funciona
sin conexión, y ningún veredicto de escritura llama a un LLM. La extracción, los resúmenes y las respuestas generadas
(`memory.ask`, `knowledge.synthesize`) necesitan un LLM real
([modelos y claves de API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)); los ejercicios siguientes usan
solo escrituras, lecturas y búsqueda.

El Lab 1, y la vía del SDK en el Lab 2, necesitan el SDK de Python en un entorno virtual, en una
terminal del portátil o del codespace. En Debian o Ubuntu, incluidos un codespace y WSL,
ejecuta `sudo apt-get update && sudo apt-get install -y python3-venv` antes de los
comandos siguientes. El SDK necesita Python 3.11 o posterior; `python3 --version` indica cuál está
instalado. Un estudiante sin Python 3.11 o posterior puede usar el codespace, donde este paso
instala un Python lo bastante reciente.

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install clickhouse-loom
```

Los comandos `docker compose` de los labs se ejecutan en esa misma terminal, actúan sobre los contenedores de la pila
y nombran su directorio de instalación como `<dir>`: `~/.loom` en un portátil y
`/workspaces/.loom` en un codespace.

<a id="hand-out-the-labs-as-an-assignment"></a>

## Entrega los laboratorios como una tarea

El [starter del curso](https://github.com/ClickHouse/loom/blob/main/examples/course-starter/README.md) es un repositorio para cada
estudiante: el código de los Labs 1 y 2 y la consulta del Lab 4, con sus huecos marcados con `TODO`, una
página de respuestas para los ejercicios escritos, un dev container que instala la última versión
en un codespace con el SDK de Python y Claude Code, y un workflow de calificación. Cópialo en un
repositorio nuevo bajo tu cuenta o la organización de tu escuela, y marca ese
repositorio como plantilla (necesita `curl`, `tar`, `git` y la
[GitHub CLI](https://cli.github.com/), con la sesión iniciada):

```bash
mkdir loom-labs && cd loom-labs
curl -fsSL https://github.com/ClickHouse/loom/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=3 loom-main/examples/course-starter
git init -b main && git add . && git commit -m "Loom labs"
gh repo create <owner>/loom-labs --public --source . --push
gh repo edit <owner>/loom-labs --template
```

El push ejecuta el workflow de calificación una vez, antes de que el repositorio sea una plantilla, y esa ejecución
falla por diseño: la línea de cada comprobación nombra la parte del starter que aún falta por escribir. La
copia es el starter de `main`, probado contra `main`, mientras que cada ejecución instala la última
versión, así que un fallo por cualquier otro motivo significa que ambos difieren: copia el starter de nuevo
después de la siguiente versión. Los push posteriores a la plantilla no se califican.

Después, cada estudiante trabaja en un repositorio creado a partir de esa plantilla, con **Use this
template** en su página o con la herramienta que uses para repartir tus tareas, y lo abre en
un codespace o lo clona en un portátil. Un estudiante necesita acceso de lectura a la plantilla para usarla:
una pública no contiene respuestas, y una privada sirve para los miembros de su
organización o los colaboradores que añadas.

`python grading/grade.py` ejecuta el código del laboratorio contra el stack en ejecución del estudiante y comprueba
lo que escribió y devolvió, no cómo llegó ahí, así que revisa también el código. El workflow `Grade`
ejecuta las mismas comprobaciones en cada push, contra un stack nuevo en un runner de GitHub Actions, y
escribe la tabla de comprobaciones en el resumen de la ejecución; la ejecución falla mientras alguna comprobación falle. Cada
comprobación falla con el starter sin modificar y no necesita clave de API. El Lab 3 y la página de respuestas se
califican a mano: sus ejercicios piden explicaciones.

Una ejecución de calificación tarda unos dos minutos, la mayoría arrancando el stack. En un repositorio
privado esos minutos cuentan contra la cuota de Actions de la cuenta u organización
propietaria ([facturación de Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions));
los codespaces cuestan lo que indica [Configuración](#setup).

<a id="lab-1-an-agent-that-remembers"></a>

## Laboratorio 1: un agente que recuerda

Construye un agente pequeño con el SDK de Python: una identidad, hechos que guarda y vuelve a leer, una
sesión y una ejecución. Sigue [Tu primer agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md).

Ejercicios:

- Busca con `memory.search` algo que ningún hecho guardado responda, y explica el
  `abstain_code` que incluye la respuesta
  ([buscar y preguntar](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)).
- Guarda un hecho y luego un valor cercano pero distinto bajo otra clave, y lee el
  veredicto de la escritura ([cómo se juzga una escritura](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#how-a-write-is-judged)).
- Revisa un hecho y lee sus versiones con `memory.history`
  ([historial y procedencia](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory)).

<a id="lab-2-document-search-with-citations"></a>

## Lab 2: búsqueda de documentos con citas

Ingiere algunos documentos con `loom demo`, que en el stack estándar (LLM falso) imprime
los primeros resultados de `knowledge.search` para una pregunta en lugar de una respuesta ([hacer una pregunta sobre un
documento](../getting-started/quickstart.md#optional-ask-a-question-about-a-document)). La
ruta de `--file` está dentro del contenedor, que guarda esta documentación en `/app/docs/`. Copia primero
un archivo tuyo (aquí `notes.md` del directorio actual), con un nombre que
lo identifique, ya que `loom demo` nombra el documento por su ruta: un segundo archivo copiado
a la misma ruta reemplaza al primero como su siguiente revisión.

```bash
docker compose --project-directory <dir> exec loom loom demo --file /app/docs/getting-started/quickstart.md --question "How do I get a token?"
docker compose --project-directory <dir> exec -T loom sh -c 'cat > /tmp/notes.md' < ./notes.md
docker compose --project-directory <dir> exec loom loom demo --file /tmp/notes.md --question "<a question the file answers>"
```

Después busca en los documentos del espacio de nombres con `knowledge.search` por HTTP (sin cabecera
`Authorization` en el stack de un solo usuario) o con el SDK
([documentos de conocimiento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)). El método `knowledge.ingest_file` del SDK
publica un archivo local sin copiarlo al contenedor; el starter del curso en
`labs/lab2.py` lo usa.

Ejercicios:

- Para diez preguntas, etiqueta el documento y el pasaje que responde a cada una, y mide
  recall@k en `knowledge.search` ([evalúa tu carga de trabajo](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md#memory-and-rag)).
- Añade preguntas que los documentos no responden. `knowledge.search` sigue devolviendo resultados
  para ellas, así que compara la `similarity` más alta entre los resultados (1 menos la distancia coseno)
  en preguntas con respuesta y sin respuesta, y elige un umbral que las separe. El
  `score` de un resultado es un ranking fusionado en el modo híbrido por defecto y no las separa.

<a id="lab-3-a-code-index"></a>

## Laboratorio 3: un índice de código

Indexa un repositorio en el stack
([indexar un repositorio](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)).
Luego pregunta dónde se define un símbolo, quién lo llama y qué afecta un cambio en él ([qué
responde el índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#what-it-answers)) por HTTP,
con `POST /v1/code.<op>`. El stack de un solo usuario no rellena las llamadas `code.*`, por lo que cada
cuerpo indica `"org": "dev"` y el `"namespace"` del repositorio (`code-myrepo` en la
entrada del quickstart).

Un agente de código puede hacer las mismas preguntas una vez conectado
([conectar un agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)). Se ejecuta con el modelo de su propio
proveedor, por lo que necesita un inicio de sesión o una clave de API allí; el stack no proporciona ninguna.

Ejercicios:

- Elige cinco funciones y compara `code.callers` con lo que encuentra `grep`; explica cada
  diferencia ([cómo leer una respuesta](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#reading-an-answer)).
- En un clon del repositorio indexado, cambia una función y envía el diff a
  `code.impact` como `diff`
  ([cómo lo usa un agente](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#how-an-agent-uses-it)). El
  comando necesita `jq` (`sudo apt-get install -y jq` en Debian o Ubuntu):

  ```bash
  git diff | jq -Rs '{org: "dev", namespace: "code-myrepo", diff: .}' \
    | curl -s -X POST http://localhost:7777/v1/code.impact -H 'Content-Type: application/json' -d @-
  ```

<a id="lab-4-memory-as-a-database"></a>

## Laboratorio 4: la memoria como base de datos

Loom guarda sus datos como filas en tablas de ClickHouse, y un usuario de ClickHouse de solo lectura lee un espacio de nombres a través de sus vistas ([superficie SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)). Crea uno para el espacio de nombres del stack, `local` (el primer comando imprime su contraseña) y luego consulta con ese usuario:

```bash
docker compose --project-directory <dir> exec loom loom sql-access grant --user student1 --org dev --namespaces local
docker compose --project-directory <dir> exec clickhouse clickhouse-client --user student1 --password <password>
```

Ejercicios:

- Escribe una consulta sobre `loom.sql_v1_memories` que cuente los hechos que cada agente creó en
  la última hora (según `created_at`, que una revisión conserva).
- Compara el resultado de un `memory.search` con una consulta SQL sobre las mismas filas y explica qué
  aporta el ranking.

<a id="which-labs-fit-which-course"></a>

## Qué laboratorios corresponden a cada curso

| Curso                      | Laboratorios | Leer a continuación                                                                                                 |
| -------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------- |
| Agentes de IA              | 1, 2, 3      | [Integrar Loom en un agente](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md), [memoria](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)                     |
| Recuperación de información | 1, 2        | [Buscar y preguntar](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md), [evaluar Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)   |
| Bases de datos             | 1, 4         | [Autoridad de almacenamiento](../architecture/storage-authority.md), [superficie SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)     |
| Ingeniería de software     | 3            | [Índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md), [historial de PR](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) |
