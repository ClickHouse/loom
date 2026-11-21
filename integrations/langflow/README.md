# Langflow

Save and search Loom memory and search knowledge documents through the Python SDK.
The component adds no data store. Its namespace and user inputs scope memory calls;
knowledge search reads documents shared within the configured namespace.

## Build an extension wheel

The extension package targets LFX 1.13.3 and Python 3.11 through 3.14. Build it from
the repository root, then install the wheel in the same environment as Langflow:

```bash
uv build integrations/langflow --out-dir dist/langflow
python -m pip install dist/langflow/lfx_loom-0.1.0-py3-none-any.whl
```

The wheel includes the canonical `loom_component.py` without a second implementation.
Its package manifest declares the `loom` bundle, and its `langflow.extensions`
entry point identifies the package containing that manifest. Restart Langflow after
installing the wheel. The component's registry ID is `ext:loom:LoomComponent@official`.

The package metadata requires `lfx==1.13.3` and `clickhouse-loom>=1.2.0,<2`.
The hosted packaging check builds both distributions, installs the extension with
the checkout's SDK wheel in an isolated environment, and checks manifest discovery,
source bytes and class loading. Those checks do not invoke the component or run a
live flow. A live Langflow flow has not been qualified.

## Publish the extension

The extension has its own package version and publication workflow. A tag named
`lfx-loom-v<version>` must match the stable version in both `pyproject.toml` and
`extension.json`. The package workflow checks the wheel, source archive and
rebuilt wheel, then installs the wheel with published dependencies and checks
LFX discovery. It tests both SDK 1.2.0 and the current dependency resolution and
records the resolved SDK version. These checks do not invoke a component or run
a live flow.

PyPI trusted publishing must authorize the `ClickHouse/loom` repository,
`langflow-publish.yml` workflow and `pypi-lfx-loom` environment for `lfx-loom`.
For a package that has not been created, configure a pending publisher in the
authorized PyPI account. The GitHub environment's publication restrictions must
allow the extension tag. Account and environment access are separate from the
core SDK publisher.

After the source review and required CI pass, publish an immutable matching tag
on the merged workflow commit through the repository's normal tag process. A
refused push stops publication. The publish job uses only the checked build
artifacts and supplies PyPI attestations. Verify the public version, anonymous
installation, file digests and provenance before describing it as published.

## Load the custom component

The manual custom-component route targets Langflow 1.13.3. Install the SDK in the
same environment as Langflow, either from this checkout or from its published package:

```bash
pip install /path/to/loom/sdk/python
```

Or install the published package with `pip install clickhouse-loom`.

Load [`loom_component.py`](loom_component.py) as a Langflow custom component:
paste the file into a new custom component, or point Langflow's custom
components path at this directory, then start Langflow:

```bash
export LANGFLOW_COMPONENTS_PATH=/path/to/loom/integrations/langflow
langflow run
```

The component needs a reachable Loom server (the
[quickstart](../../docs/getting-started/quickstart.md) listens on
`http://localhost:7777`) and a namespace-scoped writer token.

## Inputs

| Input      | Role                                                                     |
| ---------- | ------------------------------------------------------------------------ |
| Action     | `save`, `search memory`, or `search documents`                           |
| Org        | Organization; may be empty when the token fills it                       |
| Namespace  | Namespace the component reads and writes                                 |
| User       | End user for `memory.set` and `memory.search`; empty reads the namespace |
| Query      | Value to save, or the search query                                       |
| Memory key | Required when the action is `save`                                       |
| Top K      | Number of search hits (default 5)                                        |
| Loom URL   | Server URL (default `http://localhost:7777`)                             |
| API key    | Writer token; may also come from `LOOM_API_KEY` or the Loom profile      |
| Agent      | Agent name stamped on writes (default `langflow`)                        |

## A flow

1. Start a Loom quickstart at `http://localhost:7777` and create a writer token.
2. In Langflow 1.13.3, add a Chat Input, a Loom component, and a Chat Output.
   Connect Chat Input to the Loom Query input, and the Loom Result to Chat Output.
3. On the Loom component, set Action to `save`, Namespace to your namespace, User
   to the end-user id, Memory key to a stable key such as `pref.seat`, and Org,
   API key, and Loom URL from the quickstart.
4. Run the flow with a message such as "aisle seat". The Result is a JSON object
   with `action`, `memory_id`, `version`, and `content_sha256`.
5. Add a second Loom component with the same Org, Namespace, User, API key, and
   URL, Action `search memory`, and Query from Chat Input. Run with "which
   seat". The Result is a JSON object with `action`, `abstain`, and `hits`
   (`memory_key`, `value`, `similarity`). A `search documents` run returns
   `action` and `hits` (`source_uri`, `content`, `similarity`).

Unit tests under `tests/unit/test_langflow_component.py` drive each action with
the SDK pointed at an `httpx.MockTransport`, using stubbed LFX types. They do not
establish installed extension discovery or a live Langflow flow.
