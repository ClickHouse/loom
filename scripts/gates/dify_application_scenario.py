"""Exercise native tools through normal Dify console setup and workflow routes."""

import base64
import json
import secrets
import subprocess
import threading
import time
from collections import deque
from urllib.parse import quote

import httpx

FACT = "The application proof user prefers aisle seats."
DOCUMENT = "The shared application travel guide describes aisle seats."


class Console:
    def __init__(self, ctx):
        self.ctx = ctx
        self.client = httpx.Client(
            base_url="http://127.0.0.1:17881/console/api/", timeout=60, trust_env=False
        )
        ctx.cleanup_callbacks.append(self.client.close)

    def refusal(self, response, label):
        response.read()
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text}
        details = {
            name: self.ctx.redact(value)[:2000]
            for name in ("code", "message", "error")
            if isinstance(body, dict) and isinstance(value := body.get(name), str)
        }
        evidence = {"label": label, "status": response.status_code, **details}
        self.ctx.record("console-refusal-" + secrets.token_hex(4) + ".json", evidence)
        raise RuntimeError(self.ctx.redact(json.dumps(evidence)))

    def request(self, method, path, *, payload=None, allowed=(200, 201)):
        csrf = self.client.cookies.get("csrf_token")
        headers = {"X-CSRF-Token": csrf} if csrf else {}
        response = self.client.request(method, path, json=payload, headers=headers)
        for cookie in self.client.cookies.jar:
            self.ctx.secret(cookie.value)
        if response.status_code not in allowed:
            self.refusal(response, method + " " + path)
        return response

    def json(self, method, path, payload=None):
        return self.request(method, path, payload=payload).json()

    def ready(self):
        deadline = time.monotonic() + 240
        last_response = None
        last_connection_error = None
        while time.monotonic() < deadline:
            try:
                response = self.client.get("setup")
                if response.status_code == 200:
                    if response.json()["step"] != "not_started":
                        raise RuntimeError("Fresh Dify instance is unexpectedly initialized")
                    return
                if response.status_code not in (502, 503, 504):
                    self.refusal(response, "GET setup during startup")
                last_response = response
                last_connection_error = None
            except httpx.ConnectError as error:
                last_response = None
                last_connection_error = self.ctx.redact(str(error))[:2000]
            time.sleep(1)
        if last_response is not None:
            self.refusal(last_response, "GET setup startup deadline exceeded")
        evidence = {
            "label": "GET setup startup deadline exceeded",
            "status": None,
            "connection_error": last_connection_error,
        }
        self.ctx.record("startup-deadline.json", evidence)
        raise RuntimeError(self.ctx.redact(json.dumps(evidence)))


def mappings(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from mappings(item)
    elif isinstance(value, list):
        for item in value:
            yield from mappings(item)


def validate_tools(tools):
    expected = {
        "remember": {
            "user_id": ("form", "string", True),
            "memory_key": ("llm", "string", True),
            "value": ("llm", "string", True),
        },
        "recall": {
            "user_id": ("form", "string", True),
            "query": ("llm", "string", True),
            "top_k": ("form", "number", False),
        },
        "search_knowledge": {
            "user_id": ("form", "string", True),
            "query": ("llm", "string", True),
            "top_k": ("form", "number", False),
        },
    }
    if len(tools) != len(expected) or {tool["name"] for tool in tools} != set(expected):
        raise RuntimeError("Registered native tool names differ")
    for tool in tools:
        parameters = tool["parameters"]
        actual = {p["name"]: (p["form"], p["type"], p["required"]) for p in parameters}
        if len(actual) != len(parameters) or actual != expected[tool["name"]]:
            raise RuntimeError("Registered native parameter schema differs: " + tool["name"])


def has_shared_document(output):
    return any(
        item.get("source_uri") == "application-guide" and item.get("content") == DOCUMENT
        for item in mappings(output)
    )


def build_graph(provider, tool, user_id, parameters):
    start = dict(type="start", title="Start", desc="", variables=[])
    end = dict(
        type="end",
        title="End",
        desc="",
        outputs=[
            {
                "variable": "result",
                "value_selector": ["tool", "json"],
                "value_type": "array[object]",
            }
        ],
    )
    data = dict(
        type="tool",
        title=tool,
        desc="",
        provider_type="builtin",
        provider_id=provider["id"],
        provider_name=provider["name"],
        tool_name=tool,
        tool_label=tool,
        tool_node_version="2",
        tool_configurations={"user_id": {"type": "constant", "value": user_id}},
        tool_parameters={
            name: {"type": "mixed", "value": value} for name, value in parameters.items()
        },
    )
    if tool != "remember":
        data["tool_configurations"]["top_k"] = {"type": "constant", "value": 5}
    for name in ("plugin_id", "plugin_unique_identifier"):
        if provider.get(name):
            data[name] = provider[name]
    nodes = [
        {
            "id": name,
            "type": "custom",
            "data": payload,
            "position": {"x": index * 300, "y": 0},
            "sourcePosition": "right",
            "targetPosition": "left",
        }
        for index, (name, payload) in enumerate([("start", start), ("tool", data), ("end", end)])
    ]
    edges = [
        {
            "id": source + "-" + target,
            "source": source,
            "target": target,
            "sourceHandle": "source",
            "targetHandle": "target",
            "type": "custom",
            "data": {},
        }
        for source, target in [("start", "tool"), ("tool", "end")]
    ]
    return {"nodes": nodes, "edges": edges}


def workflow(console, ctx, app_id, provider, tool, user_id, parameters, label):
    path = "apps/" + app_id + "/workflows/draft"
    draft = console.request("GET", path, allowed=(200, 404))
    current = draft.json() if draft.status_code == 200 else {}
    graph = build_graph(provider, tool, user_id, parameters)
    console.json(
        "POST",
        path,
        {
            "graph": graph,
            "features": current.get("features") or {},
            "hash": current.get("hash"),
            "conversation_variables": [],
        },
    )
    events = []
    csrf = console.client.cookies.get("csrf_token")
    with console.client.stream(
        "POST", path + "/run", json={"inputs": {}, "files": []}, headers={"X-CSRF-Token": csrf}
    ) as response:
        if response.status_code != 200:
            console.refusal(response, "POST " + path + "/run")
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if event.get("event") in ("workflow_finished", "node_finished", "error"):
                    data = event.get("data") or {}
                    events.append(
                        {
                            "event": event["event"],
                            "node_type": data.get("node_type"),
                            "node_id": data.get("node_id"),
                            "status": data.get("status"),
                            "outputs": data.get("outputs"),
                            "failure": {
                                level: {
                                    key: ctx.redact(value)[:2000]
                                    for key in ("code", "message", "error")
                                    if isinstance(value := source.get(key), str)
                                }
                                for level, source in [("event", event), ("data", data)]
                            },
                        }
                    )
    ctx.record(label + "-workflow.json", events)
    terminal = [e for e in events if e["event"] == "workflow_finished"]
    tools = [e for e in events if e["event"] == "node_finished" and e["node_type"] == "tool"]
    if (
        len(terminal) != 1
        or terminal[0]["status"] != "succeeded"
        or len(tools) != 1
        or tools[0]["status"] != "succeeded"
    ):
        raise RuntimeError(
            "Native tool workflow did not succeed: "
            + label
            + "; "
            + ctx.redact(json.dumps(events[-4:]))
        )
    return tools[0]["outputs"]


def run(ctx):
    import clickhouse_connect

    console = Console(ctx)
    console.ready()
    console.json("POST", "init", {"password": ctx.env["INIT_PASSWORD"]})
    password = ctx.secret("Aa7!" + secrets.token_hex(10))
    email = "proof-" + secrets.token_hex(4) + "@example.com"
    console.json(
        "POST",
        "setup",
        {"email": email, "name": "CI plugin proof", "password": password, "language": "en-US"},
    )
    encoded = ctx.secret(base64.b64encode(password.encode()).decode())
    console.json("POST", "login", {"email": email, "password": encoded, "remember_me": False})
    if not console.client.cookies.get("csrf_token"):
        raise RuntimeError("Normal login supplied no CSRF token")
    debug = console.json("GET", "workspaces/current/plugin/debugging-key")
    key = ctx.secret(debug["key"])
    if debug["host"] != "127.0.0.1" or int(debug["port"]) != 17883:
        raise RuntimeError("Workspace advertised a different debug endpoint")
    plugin_env = dict(
        ctx.env,
        INSTALL_METHOD="remote",
        REMOTE_INSTALL_URL="127.0.0.1:17883",
        REMOTE_INSTALL_KEY=key,
    )
    logs = deque(maxlen=1000)
    child = subprocess.Popen(
        [str(ctx.plugin_python), "-m", "main"],
        cwd=ctx.plugin_dir,
        env=plugin_env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    ctx.track_plugin(child)

    def collect():
        for line in child.stdout:
            logs.append(ctx.redact(line[:4096].rstrip()))

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()

    def save_plugin_log():
        reader.join(timeout=10)
        if reader.is_alive():
            raise RuntimeError("Native plugin log reader did not finish")
        ctx.record("native-plugin-log.json", list(logs))

    ctx.cleanup_callbacks.append(save_plugin_log)
    deadline = time.monotonic() + 60
    providers = []
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Native remote plugin exited before registration")
        providers = console.json("GET", "workspaces/current/tool-providers")
        matches = [
            p
            for p in providers
            if p.get("author") == "clickhouse"
            and (p.get("name") == "loom" or p.get("name", "").endswith("/loom"))
        ]
        if len(matches) == 1:
            break
        if len(matches) > 1:
            raise RuntimeError("Ambiguous installed Loom provider")
        time.sleep(1)
    else:
        raise RuntimeError("Native plugin registration deadline exceeded")
    (provider,) = matches
    ctx.phase = "native-provider-schema"
    if provider["type"] != "builtin":
        raise RuntimeError("Unexpected native provider type")
    provider_path = quote(provider["id"], safe="/")
    ctx.phase = "native-tool-schema"
    tools = console.json(
        "GET", "workspaces/current/tool-provider/builtin/" + provider_path + "/tools"
    )
    validate_tools(tools)
    ctx.phase = "native-credentials"
    org, namespace, agent = ctx.org, ctx.namespace, ctx.agent
    token = ctx.token
    profile = ctx.env

    credentials = {
        "base_url": "http://127.0.0.1:7871",
        "api_key": token,
        "org": org,
        "namespace": namespace,
        "agent": agent,
    }
    console.json(
        "POST",
        "workspaces/current/tool-provider/builtin/" + provider_path + "/add",
        {"credentials": credentials, "name": "CI proof", "type": "api-key"},
    )
    ctx.phase = "native-application-create"
    app = console.json(
        "POST", "apps", {"name": "Loom native application proof", "mode": "workflow"}
    )
    app_id = app["id"]
    ctx.phase = "native-workflow"
    public_provider = {
        name: provider.get(name)
        for name in ("id", "name", "type", "plugin_id", "plugin_unique_identifier")
    }
    ctx.record(
        "registration.json",
        {"provider": public_provider, "tools": ["remember", "recall", "search_knowledge"]},
    )
    workflow(
        console,
        ctx,
        app_id,
        provider,
        "remember",
        "user-a",
        {"memory_key": "seat", "value": FACT},
        "remember",
    )
    own = workflow(
        console,
        ctx,
        app_id,
        provider,
        "recall",
        "user-a",
        {"query": "aisle seats"},
        "recall-own",
    )
    if FACT not in json.dumps(own):
        raise RuntimeError("Same-user native recall did not return the stored fact")
    other = workflow(
        console,
        ctx,
        app_id,
        provider,
        "recall",
        "user-b",
        {"query": "aisle seats"},
        "recall-other",
    )
    result_lists = [item["results"] for item in mappings(other) if "results" in item]
    if result_lists != [[]]:
        raise RuntimeError("Other-user native recall was not empty")
    with httpx.Client(timeout=30, trust_env=False) as loom:
        response = loom.post(
            "http://127.0.0.1:7871/v1/knowledge.upsert",
            headers={"Authorization": "Bearer " + token},
            json={
                "org": org,
                "namespace": namespace,
                "agent": agent,
                "source_uri": "application-guide",
                "content": DOCUMENT,
            },
        )
        if response.status_code != 200:
            raise RuntimeError("Owned knowledge fixture write failed")
    knowledge = workflow(
        console,
        ctx,
        app_id,
        provider,
        "search_knowledge",
        "user-b",
        {"query": "aisle seats"},
        "knowledge-shared",
    )
    if not has_shared_document(knowledge):
        raise RuntimeError("Native knowledge workflow did not return the shared document")
    ctx.phase = "native-stored-identity"
    ch = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18471,
        username=profile.get("LOOM_CH_USER", "loom"),
        password=profile.get("LOOM_CH_PASSWORD", "loomdev"),
        database=profile.get("LOOM_CH_DATABASE", "loom"),
    )
    try:
        rows = ch.query(
            "SELECT org,namespace,agent,user_id,session_id,memory_key,value FROM loom.memory FINAL "
            "WHERE org=%(org)s AND namespace=%(ns)s AND tombstoned_at IS NULL",
            parameters={"org": org, "ns": namespace},
        ).result_rows
        ctx.record("stored-identity.json", {"rows": rows})
        # Direct storage reads return JSON text, including quotes around a string value.
        if rows != [(org, namespace, agent, "user-a", "-", "seat", json.dumps(FACT))]:
            raise RuntimeError("Stored Loom identity/value differs from native workflow input")
    finally:
        ch.close()
