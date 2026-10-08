#!/usr/bin/env bash
#
# Create Loom's three HyperDX/ClickStack sources (logs, traces, metrics)
# and wire the correlation between them.
#
# Loom writes OTel data DIRECTLY into default.otel_* on the same ClickHouse
# service it uses for loom.* — there is no collector. So provisioning
# ClickStack is purely a matter of pointing HyperDX's sources at tables that
# already exist. This script does that via the HyperDX API.
#
# Verified against HyperDX v2.33.0, with the sources querying a ClickHouse
# Cloud service end-to-end.
#
# Usage:
#   HDX_BASE=https://<your-hyperdx-host> \
#   HDX_COOKIE='connect.sid=s%3A...' \
#   ./deploy/clickstack_sources.sh
#
# Auth: hosted ClickStack authenticates with your console session, so pass
# the `connect.sid` cookie from browser devtools (Application → Cookies).
# Self-hosted with a local login instead:
#   HDX_BASE=http://localhost:8080 HDX_EMAIL=... HDX_PASSWORD=... ./…
#
# Optional:
#   HDX_CONNECTION_ID=...  # skip auto-discovery of the ClickHouse connection
#   HDX_DB=default         # database holding the otel_* tables
#   HDX_PREFIX=loom        # source name prefix
#   DRY_RUN=1              # print payloads, write nothing
#
# Safe to re-run: a source whose name already exists is updated in place,
# never created twice. The cookie and password never reach a command line
# or the output; plain http is accepted only for a loopback host.

set -euo pipefail

HDX_BASE="${HDX_BASE:?set HDX_BASE, e.g. https://<your-hyperdx-host>}"
HDX_BASE="${HDX_BASE%/}"
HDX_DB="${HDX_DB:-default}"
HDX_PREFIX="${HDX_PREFIX:-loom}"
DRY_RUN="${DRY_RUN:-0}"

base_re='^https?://[][A-Za-z0-9.:-]+$'
loopback_re='^http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?$'
if [[ ! "$HDX_BASE" =~ $base_re ]]; then
  echo "✗ HDX_BASE must be a scheme and host with no path, e.g. https://hyperdx.example.com" >&2
  exit 2
fi
if [[ "$HDX_BASE" == http://* && ! "$HDX_BASE" =~ $loopback_re ]]; then
  echo "✗ refusing plain http to a non-loopback host: the session cookie would travel in clear" >&2
  exit 2
fi
for v in HDX_DB HDX_PREFIX; do
  if [[ ! "${!v}" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "✗ $v must be letters, digits, _ or -" >&2
    exit 2
  fi
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"
JAR="$TMP/cookies"
HDRS="$TMP/headers"
{
  echo 'Content-Type: application/json'
  [[ -n "${HDX_COOKIE:-}" ]] && printf 'Cookie: %s\n' "$HDX_COOKIE"
} >"$HDRS"

api() {
  local method="$1" path="$2"
  shift 2
  curl -sS --max-time 40 -X "$method" "${HDX_BASE}${path}" \
    -H @"$HDRS" -b "$JAR" -c "$JAR" "$@"
}

# ---- auth -------------------------------------------------------------
if [[ -z "${HDX_COOKIE:-}" ]]; then
  : "${HDX_EMAIL:?set HDX_COOKIE, or HDX_EMAIL + HDX_PASSWORD}"
  : "${HDX_PASSWORD:?set HDX_COOKIE, or HDX_EMAIL + HDX_PASSWORD}"
  echo "▸ logging in as ${HDX_EMAIL}"
  python3 -c 'import json, os; print(json.dumps({"email": os.environ["HDX_EMAIL"], "password": os.environ["HDX_PASSWORD"]}))' \
    | api POST /api/login/password -d @- >/dev/null
fi

me="$(api GET /api/me)"
if ! grep -q '"email"' <<<"$me"; then
  echo "✗ not authenticated (GET /api/me answered without an email)" >&2
  exit 1
fi
echo "✓ authenticated"

# ---- connection -------------------------------------------------------
# Hosted ClickStack pre-creates the connection to your service; find it
# rather than making a second one.
if [[ -n "${HDX_CONNECTION_ID:-}" ]]; then
  CONN="$HDX_CONNECTION_ID"
else
  CONN="$(api GET /api/connections | python3 -c '
import json, sys
conns = json.load(sys.stdin)
if not conns:
    sys.exit("no ClickHouse connection found — create one in Team Settings first")
if len(conns) > 1:
    sys.stderr.write("multiple connections; using the first. Override with HDX_CONNECTION_ID:\n")
    for c in conns:
        sys.stderr.write("  %s  %s  %s\n" % (c["id"], c["name"], c.get("host", "")))
print(conns[0]["id"])
')"
fi
echo "✓ connection ${CONN}"

# ---- payloads ---------------------------------------------------------
# Column names below match schemas/otel/clickstack_otel_tables.sql.
#
# Logs use TimestampTime for `timestampValueExpression` (the filter/prune
# column: the table is ORDER BY (ServiceName, SeverityText, TimestampTime,
# Timestamp) and PARTITION BY toDate(TimestampTime)) but Timestamp for
# `displayedTimestampValueExpression`, since TimestampTime is DateTime and
# would drop sub-second precision in the UI.
logs_payload() {
  cat <<EOF
{"kind":"log","name":"${HDX_PREFIX}-logs","connection":"${CONN}",
 "from":{"databaseName":"${HDX_DB}","tableName":"otel_logs"},
 "timestampValueExpression":"TimestampTime",
 "displayedTimestampValueExpression":"Timestamp",
 "implicitColumnExpression":"Body",
 "bodyExpression":"Body",
 "serviceNameExpression":"ServiceName",
 "severityTextExpression":"SeverityText",
 "traceIdExpression":"TraceId",
 "spanIdExpression":"SpanId",
 "resourceAttributesExpression":"ResourceAttributes",
 "eventAttributesExpression":"LogAttributes",
 "defaultTableSelectExpression":"Timestamp,ServiceName,SeverityText,Body"}
EOF
}

# durationPrecision 9: Loom writes Duration as Int64 NANOSECONDS. Set this
# wrong and every latency in the UI is off by 10^6.
traces_payload() {
  cat <<EOF
{"kind":"trace","name":"${HDX_PREFIX}-traces","connection":"${CONN}",
 "from":{"databaseName":"${HDX_DB}","tableName":"otel_traces"},
 "timestampValueExpression":"Timestamp",
 "displayedTimestampValueExpression":"Timestamp",
 "implicitColumnExpression":"SpanName",
 "durationExpression":"Duration",
 "durationPrecision":9,
 "traceIdExpression":"TraceId",
 "spanIdExpression":"SpanId",
 "parentSpanIdExpression":"ParentSpanId",
 "spanNameExpression":"SpanName",
 "spanKindExpression":"SpanKind",
 "statusCodeExpression":"StatusCode",
 "statusMessageExpression":"StatusMessage",
 "serviceNameExpression":"ServiceName",
 "resourceAttributesExpression":"ResourceAttributes",
 "eventAttributesExpression":"SpanAttributes",
 "defaultTableSelectExpression":"Timestamp,ServiceName,StatusCode,round(Duration/1e6),SpanName"}
EOF
}

# metricTables takes exactly these four keys in v2.33.0 — there is no
# `exponential_histogram` key. Loom only writes sum + histogram; gauge and
# summary are mapped because the source config expects the full set and the
# ClickStack erasure backend iterates them.
metrics_payload() {
  cat <<EOF
{"kind":"metric","name":"${HDX_PREFIX}-metrics","connection":"${CONN}",
 "from":{"databaseName":"${HDX_DB}","tableName":""},
 "timestampValueExpression":"TimeUnix",
 "resourceAttributesExpression":"ResourceAttributes",
 "metricTables":{"gauge":"otel_metrics_gauge","histogram":"otel_metrics_histogram","sum":"otel_metrics_sum","summary":"otel_metrics_summary"}}
EOF
}

if [[ "$DRY_RUN" == "1" ]]; then
  echo "--- logs ---"
  logs_payload
  echo "--- traces ---"
  traces_payload
  echo "--- metrics ---"
  metrics_payload
  exit 0
fi

# ---- create or update ------------------------------------------------
# A source is matched by name: an existing one is updated with a whole-object
# PUT (there is no PATCH route), a missing one is created.
EXISTING="$TMP/sources.json"
api GET /api/sources >"$EXISTING"

# upsert <payload-json on stdin>: prints the source id.
upsert() {
  local body id
  body="$(cat)"
  id="$(BODY="$body" python3 -c '
import json, os, sys
name = json.loads(os.environ["BODY"])["name"]
print(next((s["id"] for s in json.load(open(sys.argv[1])) if s.get("name") == name), ""))
' "$EXISTING")"
  if [[ -z "$id" ]]; then
    api POST /api/sources -d @- <<<"$body" | extract_id
    return
  fi
  ID="$id" BODY="$body" python3 -c '
import json, os, sys
s = next(x for x in json.load(open(sys.argv[1])) if x["id"] == os.environ["ID"])
s.update(json.loads(os.environ["BODY"]))
for k in ("_id", "__v", "createdAt", "updatedAt", "team"):
    s.pop(k, None)
print(json.dumps(s))
' "$EXISTING" | api PUT "/api/sources/${id}" -d @- >/dev/null
  echo "$id"
}

extract_id() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id") or sys.exit("no id in response: %s" % d))'; }

echo "▸ ${HDX_PREFIX}-logs"
LOGS_ID="$(logs_payload | upsert)"
echo "  ${LOGS_ID}"

echo "▸ ${HDX_PREFIX}-traces"
TRACES_ID="$(traces_payload | upsert)"
echo "  ${TRACES_ID}"

echo "▸ ${HDX_PREFIX}-metrics"
METRICS_ID="$(metrics_payload | upsert)"
echo "  ${METRICS_ID}"

# ---- correlate --------------------------------------------------------
# Correlation is stored per source and is NOT symmetric, so both sides must
# be set or click-through works one way only.
correlate() {
  api GET /api/sources >"$EXISTING"
  upsert
}

echo "▸ wiring correlation"
printf '{"name":"%s-logs","traceSourceId":"%s","metricSourceId":"%s"}' "$HDX_PREFIX" "$TRACES_ID" "$METRICS_ID" | correlate >/dev/null
printf '{"name":"%s-traces","logSourceId":"%s","metricSourceId":"%s"}' "$HDX_PREFIX" "$LOGS_ID" "$METRICS_ID" | correlate >/dev/null

# ---- verify -----------------------------------------------------------
echo
echo "▸ result"
api GET /api/sources | PREFIX="$HDX_PREFIX-" python3 -c '
import json, os, sys
for s in json.load(sys.stdin):
    if s["name"].startswith(os.environ["PREFIX"]):
        ids = [str(s.get(k))[-6:] for k in ("logSourceId", "traceSourceId", "metricSourceId")]
        print("  %-16s kind=%-7s log=%-7s trace=%-7s metric=%s" % (s["name"], s["kind"], *ids))
'
cat <<'EOF'

Next: open Search, set the time range to the last 15 minutes and try
  ServiceName:loom
  SpanAttributes['loom.namespace']:<a-real-namespace>
then click a `POST /v1/<op>` span — the waterfall shows its ch.insert.* /
embed.compute children, and the Logs tab resolves via the correlation set
above.

Dashboard panels: deploy/hyperdx_dashboards.md
EOF
