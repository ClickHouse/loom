"""The analysis boundary as data: the tables and columns `insights.query` may
read, and the ClickHouse access entities that hold a statement to them.

The boundary is the database user, never a parse of the statement. The user
`loom_analyst` gets column grants on the allowlist below and nothing else, a
settings profile that pins `readonly`, the caps and the overflow modes, and on
every allowlisted table a row policy that admits a row only when its `org` and
`namespace` match the request's signed custom settings. Loom signs the scope
with HMAC-SHA256 under a random key the first boot made and every replica
reads (`loom.analysis_secrets`); the policy recomputes the HMAC inside
ClickHouse, so a statement that sets other `SQL_loom_*` values reads no rows.
The profile forces `FINAL`, which ClickHouse applies before the row policy, so
a statement sees one live row per key.
docs/architecture/assistant.md has the reasoning and the isolation proof.

Pure: no I/O. `server.insights.store.analysis` runs the statements.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import NamedTuple

from server.core.sql_literal import sql_string

# Bumped whenever the allowlist, the profile or the policy shape changes. The
# profile carries it, and a build writes the entities only when its version is
# at least the stored one, so during a rolling upgrade build N-1 never puts
# back its narrower grants over N's (`needs_rewrite`).
ENTITY_VERSION = 2

ANALYST_USER = "loom_analyst"
POLICY_NAME = "loom_analyst_scope"
CAPS_PROFILE = "loom_analyst_caps"
# A row policy on a table hides it from every user no policy names, unless the
# server's `users_without_row_policies_can_read_rows` is on; this one keeps
# every other user reading the table as before, whatever the server says.
OTHERS_POLICY_NAME = "loom_analyst_others"
# Tables that left the allowlist: each keeps a policy that reads nothing, so a
# grant that outlives it (a write cut short) still reads no row.
RETIRED_TABLES: tuple[str, ...] = ()
# One row in a reserved scope, under a policy written last with the same key:
# the probe's signed read of it shows the policies accept this replica's key.
CANARY_TABLE = "analysis_canary"
CANARY_ORG = ""
CANARY_NAMESPACE = ""
# Custom settings carry the scope; ClickHouse accepts any setting with the
# `SQL_` prefix (custom_settings_prefixes defaults to it).
ORG_SETTING = "SQL_loom_org"
NS_SETTING = "SQL_loom_ns"
EXP_SETTING = "SQL_loom_exp"
SIG_SETTING = "SQL_loom_sig"
VERSION_SETTING = "SQL_loom_analyst_version"
DIGEST_SETTING = "SQL_loom_analyst_digest"
# The caps profile's own stamp: the digest of the caps it holds.
CAPS_DIGEST_SETTING = "SQL_loom_analyst_caps_digest"

# Logged statement text is cut to "(removed N characters)"; the formatted copy,
# which the cut misses, is not written. Cloud pins log_queries but not these.
LOG_TEXT_CUT = {"log_queries_cut_to_length": 1, "log_formatted_queries": 0}

_BLOCK = 64  # SHA-256 block size, for HMAC's inner and outer pads


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    comment: str


@dataclass(frozen=True)
class Table:
    name: str
    comment: str
    # How to read it correctly: one row per key or per event, a sum to take.
    notes: str
    columns: tuple[Column, ...]
    # The rows the policy admits beside the scope, after FINAL: a row a
    # deletion or an erasure tombstoned is gone, as on every read path
    # (`server.erasure.store.census` counts the same predicates).
    live: str = "1"


def _c(name: str, type_: str, comment: str) -> Column:
    return Column(name, type_, comment)


_IDENTITY = (
    _c("org", "LowCardinality(String)", "the tenant; every row read is the caller's org"),
    _c("namespace", "LowCardinality(String)", "the namespace the row belongs to"),
)
_WRITER = (
    _c("agent", "LowCardinality(String)", "the agent that wrote the row"),
    _c("user_id", "String", "the end user the row is about, '-' for none"),
    _c("session_id", "String", "the session that wrote it, '-' for none"),
)

# Metadata only: no memory value, document text, prompt, query or request
# argument is granted. A question about content goes through memory.search,
# memory.ask or knowledge.search, which apply their own scope and sensitivity
# rules. Every table here is deleted from by the erasure saga or listed in the
# census as holding no memory content (tests/unit/test_analysis_boundary.py).
TABLES: tuple[Table, ...] = (
    Table(
        "memory",
        "one row per version of every memory",
        "one row per (agent, user_id, session_id, memory_key): its current version; a "
        "deleted or erased memory is absent",
        (
            *_IDENTITY,
            *_WRITER,
            _c("run_id", "String", "the run that wrote it, '-' for none"),
            _c("memory_key", "String", "the key; cite it as [memory:<key>@v<version>]"),
            _c("memory_id", "UUID", "the version's id"),
            _c("memory_type", "LowCardinality(String)", "fact, decision, preference, ..."),
            _c("version", "UInt64", "the version number; higher is newer"),
            _c(
                "status",
                "Enum8",
                "active, conflicted, tombstoned, duplicate, superseded, rejected, proposed",
            ),
            _c("confidence", "Float32", "the writer's confidence, 0 to 1"),
            _c("pinned", "UInt8", "1 when pinned"),
            _c("created_at", "DateTime", "when this version was written"),
            _c("updated_at", "DateTime", "when the row last changed"),
            _c("expires_at", "Nullable(DateTime)", "when it expires, NULL for never"),
            _c("valid_at", "Nullable(DateTime)", "when the fact became true"),
            _c("invalid_at", "Nullable(DateTime)", "when the fact stopped being true"),
            _c("evidence_count", "UInt32", "how many writes supported it"),
            _c("recall_count", "UInt32", "how many reads returned it"),
            _c("last_recalled_at", "Nullable(DateTime)", "the last read that returned it"),
            _c("reinforcement_count", "UInt32", "how many times it was reinforced"),
            _c("conflict_with", "Array(UUID)", "memory_ids it contradicts"),
            _c("tags", "Array(String)", "the writer's tags"),
            _c("actor", "String", "the token subject that wrote it"),
            _c("source_class", "LowCardinality(String)", "first_party, derived, external, ..."),
            _c("sensitivity", "LowCardinality(String)", "the sensitivity label, '' for none"),
            _c("grounding", "LowCardinality(String)", "how the fact is grounded"),
            _c("value_len", "UInt32", "the value's length in characters (not the value)"),
        ),
        live="tombstoned_at IS NULL",
    ),
    Table(
        "memory_record_conflicts",
        "one row per field two writes of a record disagreed on",
        "one row per (memory_key, field_name) conflict, its current version; resolved_at "
        "NULL is open",
        (
            *_IDENTITY,
            _c("schema_name", "LowCardinality(String)", "the record schema"),
            _c("memory_key", "String", "the record's key"),
            _c("field_name", "String", "the field the two writes disagreed on"),
            _c("prior_memory_id", "Nullable(UUID)", "the earlier version"),
            _c("incoming_memory_id", "Nullable(UUID)", "the later version"),
            _c("detected_at", "DateTime", "when the conflict was found"),
            _c("resolved_at", "Nullable(DateTime)", "when it was resolved, NULL while open"),
            _c("version", "UInt64", "the row version"),
        ),
        live="tombstoned_at IS NULL",
    ),
    Table(
        "memory_usage_totals",
        "how often each memory was recalled and used",
        "one row per memory_id with its summed counts",
        (
            *_IDENTITY,
            _c("memory_id", "UUID", "joins memory.memory_id"),
            _c("evidence_count", "Int64", "writes that supported it"),
            _c("recall_count", "Int64", "reads that returned it"),
            _c("used_count", "Int64", "answers that used it"),
            _c("harm_count", "Int64", "answers it was reported to have hurt"),
            _c("last_recalled_at", "DateTime", "max over rows: the last recall"),
        ),
    ),
    Table(
        "documents",
        "one row per version of every ingested document",
        "one row per live document (source_uri): its current version; a replaced, "
        "deleted or erased version is absent",
        (
            *_IDENTITY,
            *_WRITER,
            _c("source_uri", "String", "the document's address; cite as [doc:<source_uri>]"),
            _c("doc_id", "UUID", "the version's id"),
            _c("title", "String", "the document's title"),
            _c("version", "UInt64", "the version; higher is newer"),
            _c("revision", "UInt64", "the source's revision number"),
            _c("ingested_at", "DateTime", "when this version was ingested"),
            _c("blob_size", "UInt64", "the original's size in bytes"),
            _c("blob_content_type", "LowCardinality(String)", "the original's media type"),
            _c("format", "LowCardinality(String)", "the parsed format"),
            _c("actor", "String", "the token subject that ingested it"),
            _c("source_class", "LowCardinality(String)", "first_party, external, imported, ..."),
        ),
        live="superseded_at IS NULL",
    ),
    Table(
        "agent_events",
        "the request ledger: one row per op call",
        "one row per call, append only: filter on ts; upstream_cost_usd is model spend in "
        "USD; an erased end user's rows read user_id '<erased>'",
        (
            _c("ts", "DateTime64(3)", "when the call ended"),
            *_IDENTITY,
            *_WRITER,
            _c("run_id", "String", "the run the call belonged to"),
            _c("operation", "LowCardinality(String)", "the op name, e.g. memory.search"),
            _c("request_id", "String", "the request id"),
            _c("latency_ms", "UInt32", "server time in ms"),
            _c("success", "Bool", "whether the call succeeded"),
            _c("error_code", "LowCardinality(String)", "the error code, '' on success"),
            _c("status_class", "LowCardinality(String)", "ok, client_error, server_error, ..."),
            _c("memory_key", "String", "the key the call addressed, '' for none"),
            _c("kind", "LowCardinality(String)", "the row kind: '' for a request, worker, ..."),
            _c("input_tokens", "UInt32", "model input tokens the call spent"),
            _c("output_tokens", "UInt32", "model output tokens the call spent"),
            _c("upstream_cost_usd", "Decimal(18, 6)", "model spend in USD"),
            _c("model", "LowCardinality(String)", "the model the call used most"),
            _c("consumer", "LowCardinality(String)", "the client the call came from"),
            _c("subject", "LowCardinality(String)", "the token subject that called"),
            _c("transport", "LowCardinality(String)", "http or mcp"),
            _c("build_commit", "LowCardinality(String)", "the server build that served it"),
        ),
    ),
)


def allowlisted_tables() -> tuple[str, ...]:
    return tuple(t.name for t in TABLES)


def scope_payload(org: str, namespaces: str, exp: int, query_id: str) -> str:
    """Each part but the last prefixed with its length in bytes, as
    ClickHouse's length() counts a String, so no two scopes share a payload.
    The statement's query id comes last: a signature read inside one statement
    (getSetting) and sent with another reads nothing, since the policy signs
    the query id Loom sends with the statement it checks (initialQueryID())."""
    exp_s = str(exp)
    return (
        f"{len(org.encode())}:{org}{len(namespaces.encode())}:{namespaces}"
        f"{len(exp_s)}:{exp_s}{query_id}"
    )


def sign_scope(key: bytes, org: str, namespaces: str, exp: int, query_id: str) -> str:
    """HMAC-SHA256 of the payload, hex in upper case as ClickHouse's hex() prints it."""
    payload = scope_payload(org, namespaces, exp, query_id)
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest().upper()


def scope_settings(
    key: bytes, org: str, namespaces: list[str], exp: int, query_id: str
) -> dict[str, str]:
    """The settings one statement carries: the four scope settings and the
    query id the signature covers."""
    ns = ",".join(sorted(set(namespaces)))
    return {
        ORG_SETTING: org,
        NS_SETTING: ns,
        EXP_SETTING: str(exp),
        SIG_SETTING: sign_scope(key, org, ns, exp, query_id),
        "query_id": query_id,
    }


def _setting(name: str) -> str:
    # toString: a statement can set the setting to a number or an array, and a
    # type error raised inside the policy would quote it, key included.
    return f"toString(getSetting('{name}'))"


def _masked_sql(key: bytes, data: bytes) -> str:
    """`data` as `decrypt(encrypt(data))`: ClickHouse prints every argument of
    `decrypt` as [HIDDEN], so an error that quotes the policy shows neither
    `data` nor `key`. A mask, not a seal: the policy text holds both."""
    k = f"unhex('{key.hex()}')"
    return f"decrypt('aes-256-ecb', encrypt('aes-256-ecb', unhex('{data.hex()}'), {k}), {k})"


def _pads(key: bytes) -> tuple[bytes, bytes]:
    """HMAC's inner and outer pads of `key`."""
    padded = key.ljust(_BLOCK, b"\0")
    return bytes(b ^ 0x36 for b in padded), bytes(b ^ 0x5C for b in padded)


def key_spellings(key: bytes) -> list[str]:
    """The hex spellings of `key` and its HMAC pads, and the printable runs
    of 0x36 ('6') and 0x5C ('\\') its zero padding XORs to."""
    return [key.hex(), *(p.hex() for p in _pads(key)), "6" * 32, "\\" * 32]


# A hex literal in a policy's text, as written or TSV-escaped: the key and
# its pads are spelled so.
KEY_LITERAL = re.compile(r"unhex\(\\?'([0-9A-Fa-f]+)\\?'\)")


def policy_spellings(select_filter: str) -> list[str]:
    """The spellings of the key a policy filter holds; the filter may be
    TSV-escaped."""
    literals = KEY_LITERAL.findall(select_filter)
    key = next((h for h in literals if len(h) == 64), None)
    if key is None:
        raise ValueError("the policy holds no 32-byte key literal")
    return key_spellings(bytes.fromhex(key))


def _hmac_sql(key: bytes, payload: str) -> str:
    """HMAC-SHA256 of `payload` under `key` as ClickHouse computes it, with the
    key's inner and outer pads masked as `decrypt` expressions."""
    inner_pad, outer_pad = _pads(key)
    ipad = _masked_sql(key, inner_pad)
    opad = _masked_sql(key, outer_pad)
    inner = f"SHA256(concat({ipad}, {payload}))"
    return f"hex(SHA256(concat({opad}, {inner})))"


def signature_filter(key: bytes) -> str:
    """The expiry is ahead and the scope is signed under `key`."""
    org, ns, exp, sig = map(_setting, (ORG_SETTING, NS_SETTING, EXP_SETTING, SIG_SETTING))
    # initialQueryID(): the id of the statement Loom sent, also on any server
    # a query fans out to.
    payload = (
        f"concat(toString(length({org})), ':', {org}, toString(length({ns})), ':', {ns}, "
        f"toString(length({exp})), ':', {exp}, initialQueryID())"
    )
    return f"toUInt64OrZero({exp}) > toUnixTimestamp(now()) AND {sig} = {_hmac_sql(key, payload)}"


def policy_filter(key: bytes) -> str:
    """The row policy's condition: the row's org and namespace match the signed scope."""
    org, ns = _setting(ORG_SETTING), _setting(NS_SETTING)
    return f"org = {org} AND has(splitByChar(',', {ns}), namespace) AND {signature_filter(key)}"


@dataclass(frozen=True)
class Caps:
    """The profile's ceilings; each is pinned as a MAX no statement can raise."""

    max_execution_s: float
    max_rows_to_read: int
    max_memory_bytes: int
    max_result_rows: int
    max_result_bytes: int
    max_concurrent: int
    max_threads: int


def profile_settings() -> list[str]:
    """The main profile's pins: what no statement may change. The version and
    digest join them in the stamp, written last."""
    return [
        "readonly = 2 CONST",
        # One live row per key: ClickHouse applies the row policy after FINAL.
        "final = 1 CONST",
        "apply_row_policy_after_final = 1 CONST",
        # A statement that runs is logged without its text; the ledger keeps its digest.
        *(f"{name} = {value} CONST" for name, value in LOG_TEXT_CUT.items()),
        "read_overflow_mode = 'throw' CONST",
        "result_overflow_mode = 'throw' CONST",
        "timeout_overflow_mode = 'throw' CONST",
        "use_query_cache = 0 CONST",
        "allow_introspection_functions = 0 CONST",
        # Server log lines sent back with a statement can quote the policies.
        "send_logs_level = 'none' CONST",
        "use_query_condition_cache = 0 CONST",
    ]


def caps_settings(caps: Caps) -> list[str]:
    """The caps profile: deployment settings, each a MAX no statement can raise."""

    def capped(name: str, value: float, floor: float = 1) -> str:
        v = int(value) if float(value).is_integer() else value
        f = int(floor) if float(floor).is_integer() else floor
        return f"{name} = {v} MIN {f} MAX {v}"

    return [
        capped("max_execution_time", caps.max_execution_s, 0.1),
        capped("max_rows_to_read", caps.max_rows_to_read),
        capped("max_memory_usage", caps.max_memory_bytes),
        # The wrapper asks for one row past the cap to tell a full page from a cut one.
        capped("max_result_rows", caps.max_result_rows + 1),
        capped("max_result_bytes", caps.max_result_bytes),
        capped("max_threads", caps.max_threads),
        f"max_concurrent_queries_for_user = {caps.max_concurrent} CONST",
    ]


def caps_sql(caps: Caps) -> list[str]:
    """The statements that write the caps profile, which inherits the main
    one and is the profile the analyst user carries. A boot writes them, so a
    cap setting never touches the version."""
    caps_profile = f"`{CAPS_PROFILE}`"
    stamp = f"{CAPS_DIGEST_SETTING} = {sql_string(caps_digest(caps))} CONST"
    settings_ = ", ".join([f"INHERIT '{ANALYST_USER}'", *caps_settings(caps), stamp])
    return [
        f"CREATE SETTINGS PROFILE IF NOT EXISTS {caps_profile} SETTINGS {settings_}",
        f"ALTER SETTINGS PROFILE {caps_profile} SETTINGS {settings_}",
    ]


def caps_digest(caps: Caps) -> str:
    """What the caps profile holds, so a boot writes it only when a cap changed."""
    return hashlib.sha256("\n".join(caps_settings(caps)).encode()).hexdigest()[:32]


class Grant(NamedTuple):
    """One row of system.grants for the analyst. `home` is whether it names
    Loom's own database; an empty database, table or column is a wildcard."""

    access: str
    database: str
    table: str
    column: str
    home: bool


def _allowed(g: Grant) -> bool:
    return g.access == "SELECT" and g.home and (g.table, g.column) in allowed_columns()


def _revoke(g: Grant, user: str) -> str:
    on = "*.*"
    if g.database:
        on = f"`{g.database}`.`{g.table}`" if g.table else f"`{g.database}`.*"
    what = f"{g.access}(`{g.column}`)" if g.column else g.access
    return f"REVOKE {what} ON {on} FROM {user}"


def revokes_for(grant_lines: list[str]) -> list[str]:
    """A REVOKE for each line of SHOW GRANTS, for SQL whose writer cannot read
    system.grants; the allowlist is granted after them."""
    out = []
    for line in grant_lines:
        bare = line.replace(" WITH GRANT OPTION", "").replace(" WITH ADMIN OPTION", "")
        if bare.startswith("GRANT ") and " TO " in bare:
            what, _, to = bare[len("GRANT ") :].rpartition(" TO ")
            out.append(f"REVOKE {what} FROM {to}")
    return out


def policy_sql(db: str, name: str, table: str, using: str, to: str) -> str:
    """One row policy, replaced in place; `db` is already quoted."""
    return (
        f"CREATE ROW POLICY OR REPLACE `{name}` ON {db}.{table} "
        f"AS permissive FOR SELECT USING {using} TO {to}"
    )


def entity_sql(
    db: str,
    password: str,
    key: bytes,
    caps: Caps,
    granted: frozenset[Grant] = frozenset(),
    roles: frozenset[str] = frozenset(),
) -> list[str]:
    """Every statement that writes the analyst user, in an order that never
    leaves a granted table without its policy (ClickHouse lets a user with no
    policy on a table read every row of it): each policy is replaced in place,
    every other user's before the analyst's so no user is ever left without
    one, then every grant and role the user holds outside the allowlist
    (`granted` and `roles` are what it holds now) is revoked, and then the
    allowlist is granted, since a table's revoke also takes the column grants
    under it. The profile's pins come after every policy, so on an upgrade
    the older profile's `log_queries = 0` holds until no policy has the pads
    in clear, and before every grant, so a new pin is in force before the
    grant it guards. A retired table keeps a policy that reads nothing. The
    stamp comes last, so a write cut short is written again by the next boot.
    `db` is already quoted."""
    user = f"`{ANALYST_USER}`"
    identified = f"IDENTIFIED WITH sha256_password BY {sql_string(password)}"
    condition = policy_filter(key)
    profile = profile_settings()

    out = [
        f"CREATE SETTINGS PROFILE IF NOT EXISTS {user} SETTINGS " + ", ".join(profile),
        *caps_sql(caps),
        f"CREATE USER IF NOT EXISTS {user} {identified} SETTINGS PROFILE '{CAPS_PROFILE}'",
        f"ALTER USER {user} {identified} SETTINGS PROFILE '{CAPS_PROFILE}'",
    ]
    for t in TABLES:
        out.append(policy_sql(db, OTHERS_POLICY_NAME, t.name, "1", f"ALL EXCEPT {user}"))
        out.append(policy_sql(db, POLICY_NAME, t.name, f"({t.live}) AND {condition}", user))
    for name in RETIRED_TABLES:
        out.append(policy_sql(db, POLICY_NAME, name, "0", user))
    out.append(f"INSERT INTO {db}.{CANARY_TABLE} (id) VALUES (0)")
    # Last of the policies: a canary that reads under the key means all of them do.
    out.append(policy_sql(db, POLICY_NAME, CANARY_TABLE, condition, user))
    out.append(f"ALTER SETTINGS PROFILE {user} SETTINGS " + ", ".join(profile))
    out += [_revoke(g, user) for g in sorted(granted) if not _allowed(g)]
    out += [f"REVOKE `{role}` FROM {user}" for role in sorted(roles)]
    out += [
        f"GRANT SELECT({', '.join(c.name for c in t.columns)}) ON {db}.{t.name} TO {user}"
        for t in TABLES
    ]
    out.append(f"GRANT SELECT(id) ON {db}.{CANARY_TABLE} TO {user}")
    stamp = [
        *profile,
        # A string, as ClickHouse stores a number setting as `UInt64_1`.
        f"{VERSION_SETTING} = '{ENTITY_VERSION}' CONST",
        f"{DIGEST_SETTING} = {sql_string(_digest(db, profile))} CONST",
    ]
    out.append(f"ALTER SETTINGS PROFILE {user} SETTINGS " + ", ".join(stamp))
    return out


def allowed_columns() -> set[tuple[str, str]]:
    """Every (table, column) the analyst may hold SELECT on."""
    return {(t.name, c.name) for t in TABLES for c in t.columns} | {(CANARY_TABLE, "id")}


@dataclass(frozen=True)
class Stored:
    """What the analyst's settings profile says it holds."""

    version: int
    digest: str


def needs_rewrite(stored: Stored | None, digest: str) -> bool:
    """Whether this build writes the entities at boot: never over a newer
    build's, so during a rolling upgrade N-1 never narrows N's grants."""
    if stored is None or stored.version < ENTITY_VERSION:
        return True
    return stored.version == ENTITY_VERSION and stored.digest != digest


def entity_digest(sql: list[str]) -> str:
    """The digest `entity_sql` stored in the profile, read back from its statements."""
    for stmt in sql:
        marker = f"{DIGEST_SETTING} = '"
        if marker in stmt:
            return stmt.split(marker, 1)[1].split("'", 1)[0]
    return ""


def structure_digest() -> str:
    """The grants and policies a version names, caps aside. A unit test pins it
    to ENTITY_VERSION, so a change to either cannot ship without a new version."""
    parts = [f"retired:{name}" for name in RETIRED_TABLES]
    parts += [f"{t.name}:{t.live}:{','.join(c.name for c in t.columns)}" for t in TABLES]
    parts += [policy_filter(b"\0" * 32), CANARY_TABLE]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def _digest(db: str, profile: list[str]) -> str:
    """What the entities hold, caps aside (the caps profile has its own), so a
    boot that would write the same leaves them. The key never changes, so it
    is not part of it."""
    parts = [db, structure_digest(), *profile]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def wrap_statement(sql: str, limit: int) -> str:
    """The caller's statement, less one trailing `;`, as a subquery, so only a
    SELECT or WITH parses; the newline ends a trailing `--` comment before
    the closing parenthesis."""
    body = sql.strip().removesuffix(";").rstrip()
    return f"SELECT * FROM (\n{body}\n) LIMIT {int(limit) + 1}"
