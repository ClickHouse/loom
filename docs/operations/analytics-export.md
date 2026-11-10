# Exporting usage to a data warehouse

`loom export-analytics` writes one UTC day of usage and a snapshot of the namespaces to an
S3 bucket as Parquet files, one file per entity per day, for a data warehouse to load. The
Helm chart runs it once a day as a CronJob (`analyticsExport` in `values.yaml`). To query a
tenant's memories, with their content, and its request ledger in place, use the
[SQL surface](../reference/sql-surface.md) instead.

```bash
loom export-analytics --bucket analytics-landing --prefix loom \
  --role-arn arn:aws:iam::123456789012:role/loom-analytics-writer \
  --expected-bucket-owner 123456789012
```

It needs `LOOM_ANALYTICS_EXPORT_NAME_KEY`, the secret that names are hashed under (see
[hashed names](#hashed-names)).

`--day YYYY-MM-DD` exports another day; the default is yesterday (UTC). A day that has not
ended is refused.

## Files

```text
<prefix>/<entity>/<yyyy>/<mm>/<dd>/<entity>-<yyyy>-<mm>-<dd>.parquet    entity: usage, namespaces
```

Each upload carries `If-None-Match: *`, so an object is never replaced. A second run of a
day keeps the first run's files and prints `already there, kept` for each; a failed or
repeated run can be retried as it is. To export a day again, delete its objects first.

| Entity       | One row per                                                                                                         | Columns                                                                                                                                                                                                                                  |
| ------------ | ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `usage`      | `org`, `namespace`, `agent`, `operation`, `kind`, `transport`, `traffic_class` of the day, from `loom.agent_events` | `day`, `ops`, `errors`, `server_errors`, `slow`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`, `cost_usd`, `bytes_written`, `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`, `users`, `sessions`, `snapshot_at` |
| `namespaces` | `org`, `namespace` registered in `loom.namespaces` or holding a live memory row                                     | `registered`, `kind`, `visibility`, `active`, `created_on`, `live_memories`, `last_memory_on`, `day`, `snapshot_at`                                                                                                                      |

`kind` in `usage` is `request` for a request, `worker` for a worker pass, `verdict` for a
deferred `memory.ask` consistency check and `settle` for a deferred conflict settle. Every
`/v1/me/*` route is one `operation`, `me`: the ledger names those rows after the request
path, which can carry any string a caller sent. `users` and `sessions` are distinct counts
within the row that leave out the `-` of a call that named none; a sum over rows counts a
user once per row. `slow` and the latency quantiles are the SLI rollup's: they count the
`ok` requests that rollup reads (an operation with an SLO class), `slow` those past their
latency budget (a budget of 0 makes every one slow), and a row with none has latencies of 0.
A `/v1/me/*` route, a deferred judgement or a worker pass counts in neither. The
`namespaces` file is the state at `snapshot_at`, whatever `--day` says: a backfill of an
old day gets that day's usage and today's namespaces. An unregistered namespace has
`registered = false` and NULL registry columns. `created_on` and `last_memory_on` are days,
not times: an exact time beside a hashed personal namespace could be matched to the person
who signed up or wrote then. Rows are sorted by the hashed names, never by the names
themselves.

## What never leaves Loom

The columns are an allowlist (`server/insights/store/analytics_export.py`; the integration
test pins each entity's exact column list). A file in the bucket is outside
[erasure](../features/memory/retention-and-erasure.md), so the export carries no memory
content and no stored person identifier: no memory keys or values, no `args`, no `user_id`,
`session_id`, `subject`, `owner_email` or request id. Users and sessions go out as counts
only.

## Hashed names

Org, namespace and agent names can name a person: a personal namespace is named after its
owner's email, self-signup builds an agent's namespace and name from the OS username and
host, and an MCP client names its agent. So `org`, `namespace` and `agent` always leave as
the lowercase hex of the reference SipHash-2-4-128 of the name under a secret key.

Each name is hashed on its own, so one string has one hash wherever it appears: a
namespace name hashes the same in every org, and an org, a namespace and an agent with the
same name share a hash. The warehouse can link those rows without the key, for example the
same personal namespace in two orgs.

- The key is a random string of 32 or more characters in `LOOM_ANALYTICS_EXPORT_NAME_KEY`;
  the export refuses to run with a shorter or empty one, or one with leading or trailing
  whitespace (a Secret made `--from-file` keeps the file's newline, which would key every
  hash on it).
- The 16-byte SipHash key is the first 16 bytes of SHA-256 of that string (UTF-8). The hash
  is ClickHouse's `sipHash128ReferenceKeyed`, which any SipHash-2-4-128 library reproduces
  from those 16 bytes, so a system that holds the key hashes its own ids to join on them.
  ClickHouse takes the key as two words, each 8-byte half read little-endian. Hash the id
  as the same string Loom stores (`toString` of a UUID or a number): SipHash of a typed
  column hashes its binary form, which never matches.

  ```sql
  WITH SHA256({key:String}) AS d
  SELECT lower(hex(sipHash128ReferenceKeyed(
      (reinterpretAsUInt64(substring(d, 1, 8)), reinterpretAsUInt64(substring(d, 9, 8))),
      toString(tenant_id)))) AS hashed_tenant_id
  FROM tenants
  ```

- A hash is a pseudonym, not an anonymous value: whoever holds the key can recompute it
  from a name they know. Erasing a namespace in Loom does not remove its rows already in
  the bucket.
- A hash is stable while the key is. A new key changes every hash, and a day already
  exported keeps its old files: delete them before exporting it again under the new key.
- A query that uses the key records it, whether the key is bound as a parameter or written
  as a literal: ClickHouse substitutes bound values into the query text in
  `system.query_log` and writes them to its debug and trace logs. On the warehouse,
  restrict who reads those logs on the server that runs the join, or hash the ids outside
  ClickHouse with any SipHash-2-4-128 library. On Loom's side the export's key words land
  in the same logs of Loom's ClickHouse, whose readers can already read the names.

## Credentials

The command uses the default AWS credentials of its process, and checks them once before
any ClickHouse work: with `--role-arn` by assuming the role, without one by asking STS who
they belong to (`GetCallerIdentity`). Missing or expired credentials, or a role that cannot
be assumed, fail there, not after the read; a missing `s3:PutObject` grant, a wrong bucket
or a bucket owner other than `--expected-bucket-owner` fails at the first upload. With
`--role-arn` it assumes the role again before each upload and writes under its credentials.
Assuming a writer role fits a bucket in another account: the bucket's account grants
`s3:PutObject` to the writer role, and the writer role trusts only the export's own
identity. The export needs no read, list or delete permission on the bucket; a bucket
encrypted with a customer-managed KMS key also needs `kms:GenerateDataKey` on that key for
the writer role, or every upload is refused. `--expected-bucket-owner` makes S3 refuse the
write when another account owns the bucket.

Pass `--region` (the bucket's, `analyticsExport.region` in the chart) unless the process
already has one from `AWS_DEFAULT_REGION` or the AWS config file; boto3 does not read
`AWS_REGION` here.

## On Kubernetes

```yaml
analyticsExport:
  enabled: true
  bucket: analytics-landing
  prefix: loom
  roleArn: arn:aws:iam::123456789012:role/loom-analytics-writer
  expectedBucketOwner: "123456789012"
  nameKeySecret:
    name: loom-analytics-export-key # key LOOM_ANALYTICS_EXPORT_NAME_KEY
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::210987654321:role/loom-analytics-export
```

```bash
kubectl create secret generic loom-analytics-export-key \
  --from-literal=LOOM_ANALYTICS_EXPORT_NAME_KEY="$(openssl rand -hex 32)"
```

Only the export's pod reads that Secret. The chart refuses to render without
`nameKeySecret.name`, and refuses whatever would hand the key to every web and workers pod:
`LOOM_ANALYTICS_EXPORT_NAME_KEY` in `env`, `extraEnv` or `secrets.extra`,
`analytics_export_name_key` in `settingsFile` (a ConfigMap every pod mounts), and a
`nameKeySecret.name` that is the release's Secret (`secrets.existingSecret` or the chart's
own) or an `envFrom` Secret. It cannot see the contents of those Secrets: keep the key out of
them.

The chart creates a ServiceAccount `<fullname>-analytics-export` and a CronJob of the same
name, where `<fullname>` is the chart's full name: `fullnameOverride` when set, else the
release name when it contains the chart name (`nameOverride`, else `loom`), else
`<release>-<chart name>`, cut to its first 35 characters so the name stays within 52
(`kubectl get sa` shows it). The job runs at 00:30 UTC (`schedule`), one run at a time,
with up to 3 retries and a three-hour deadline over all attempts; each statement may run
20 minutes, and the ClickHouse client may retry one, so a slow day can get fewer than four
attempts. A refusal (exit 2) fails the Job at once instead of retrying. Its pod takes none
of the web pods' `podLabels`, `podAnnotations` or `affinity`.
The web and workers pods keep their own ServiceAccount, so the role that may assume the
writer role is bound to the export's pod only. With IRSA, that role's trust names
`system:serviceaccount:<namespace>:<fullname>-analytics-export`. With `roleArn` set, its
only permission is `sts:AssumeRole` on `roleArn`; with `roleArn` empty it writes itself and
needs `s3:PutObject` on the bucket (and `kms:GenerateDataKey` on a customer-managed KMS
key). With EKS Pod Identity, bind the role to the ServiceAccount and leave `annotations`
empty.

The job reads ClickHouse with the release's Secret, settings, `extraEnv` and
`extraVolumes`, as the web pods do, and writes nothing to it. A failed run shows as a
failed Job; alert on the CronJob's last successful run being more than a day old.

A day whose run fails every attempt, or that misses its start window
(`startingDeadlineSeconds`, one hour), is not exported later on its own: the next run
exports only the day before it. Export it with a Job made from the CronJob, so it runs
under the export's ServiceAccount and flags, with `--day` added to its command:

```bash
kubectl create job --from=cronjob/<fullname>-analytics-export analytics-export-backfill \
  --dry-run=client -o yaml > backfill.yaml
# add `- --day=YYYY-MM-DD` to the container's command in backfill.yaml, then:
kubectl apply -f backfill.yaml
```

The usage file is that day's; the namespace file is the snapshot at the time of the run.
