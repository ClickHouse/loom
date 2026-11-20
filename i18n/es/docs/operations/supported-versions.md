<!-- i18n: source=docs/operations/supported-versions.md blob=32631f7ef1ce0afc4871e89ec501bcad37732e46 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md), which is authoritative.

<a id="supported-versions"></a>

# Versiones compatibles

Las versiones N y N−1 reciben correcciones, y un SDK en `X.Y` funciona con un servidor en `X.Y` o `X.(Y−1)`. Loom
publica una sola versión para el servidor, las dos imágenes, la aplicación web, ambos SDK, las skills raíz y el `appVersion` del
chart de Helm ([proceso de publicación](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md)). Esta página también cubre
los plugins de agentes de programación y cómo se anuncia un cambio que elimina algo.

<a id="release-lines"></a>

## Líneas de versiones

| Línea | Qué recibe                                                                                        |
| ---- | ------------------------------------------------------------------------------------------------- |
| N    | la versión menor más reciente (`X.Y`): todas las correcciones, como versión de parche (`X.Y.(Z+1)`) cuando no pueden esperar |
| N−1  | la versión menor anterior: correcciones de seguridad y correcciones de pérdida de datos o de respuestas erróneas, como parches |
| N−2  | ninguna: actualice a N o N−1                                                                      |

Una versión menor sale de `main` aproximadamente cada dos semanas; un parche sale de la rama
`release/X.Y` de la línea, con la corrección fusionada antes en `main`. Una actualización sigue
[las actualizaciones](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md); dos versiones adyacentes funcionan en paralelo mientras una reemplaza a la otra
([actualizaciones progresivas](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)).

<a id="sdks-and-servers"></a>

## SDK y servidores

Los SDK (`clickhouse-loom` en PyPI, `@clickhouse/loom` en npm) se publican con cada versión, con
la versión del servidor; el `latest` de npm apunta a la versión final más alta, y un parche de una línea anterior usa su
dist-tag `release-X.Y`. Frente al servidor más antiguo:

- un campo que el SDK envía y que el servidor más antiguo no conoce se atiende y se nombra en
  `ignored_args` de la respuesta, excepto en una op que cambia el estado, donde una clave que el servidor interpreta como un error ortográfico
  de uno de sus campos (un nombre cercano, un sinónimo, una palabra del nombre de un campo) se rechaza con
  `422 UNKNOWN_ARGUMENT` y no se escribe nada
  ([campos desconocidos y eliminados](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields));
- una operación que el servidor más antiguo no atiende responde `404`, que los SDK lanzan como `NotFound`
  (Python `loom.errors.NotFound`, TypeScript `NotFound`): una llamada nunca tiene éxito en silencio frente a un
  servidor que no puede realizarla. El mismo error significa que falta una fila, así que un cliente que deba distinguir ambos casos
  lee `ops.capabilities` al arrancar
  ([descubrir qué atiende un despliegue](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)).

Una versión mayor (`X.0`) puede romper la regla frente al último servidor `(X−1).Y`; sus notas de la versión
indican qué necesita un SDK en `X.0`.

Un SDK más antiguo sigue funcionando con un servidor más reciente mientras el servidor atienda lo que llama;
una eliminación sigue la regla de abajo.

<a id="coding-agent-plugins"></a>

## Plugins de agentes de programación

Los plugins de Claude Code y Codex tienen su propia versión e indican el servidor más antiguo que sus
hooks admiten (`MIN_SERVER_VERSION` en `lib/server-version.mjs`). Una sesión contra un servidor más
antiguo empieza con una línea `loom:` que lo indica, y el plugin sigue funcionando donde puede
([Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)). El plugin de Claude Code requiere Claude Code
2.1.139 o posterior, la primera versión que ejecuta los `args` de un hook sin shell.

<a id="removing-something"></a>

## Eliminar algo

Una eliminación se anuncia, a mano en el pull request de la versión, en las notas de la versión menor
anterior a la que la elimina, bajo "What changed for clients". Hasta la eliminación, el
servidor atiende el campo o la op antigua; después, un campo de solicitud eliminado se sigue aceptando y se nombra
en `ignored_args`, de modo que un cliente que lo envía se entera del cambio sin fallar, y una op eliminada
responde `404` (`NotFound`), lo que `ops.capabilities` distingue de una fila ausente.

<a id="release-artifacts"></a>

## Artefactos de la versión

Cada imagen de la versión se construye en la arquitectura en la que se ejecuta, se analiza en busca de vulnerabilidades críticas
con corrección disponible, y se publica con su procedencia y un SBOM. El índice multiarquitectura de cada imagen lleva una
firma sin clave del flujo de trabajo de publicación; [imágenes de contenedor](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#container-images)
da el comando `cosign verify`. Cada archivo de la GitHub Release se firma del mismo modo, en un
`<file>.sigstore.json` junto a él; [archivos de la versión](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#release-files) da el
comando `cosign verify-blob`.
