# Exporting usage to a data warehouse

`loom export-analytics` writes one UTC day of usage and a snapshot of the namespaces to an
S3 bucket as Parquet files, one file per entity per day, for a data warehouse to load. The
Helm chart runs it once a day as a CronJob (`analyticsExport` in `values.yaml`).

```bash
loom export-analytics --bucket analytics-landing --prefix loom \
  --role-arn arn:aws:iam::123456789012:role/loom-analytics-writer \
  --expected-bucket-owner 123456789012
```

The default `--names hashed` needs `LOOM_ANALYTICS_EXPORT_NAME_KEY` (see
[what never leaves Loom](#what-never-leaves-loom)).

`--day YYYY-MM-DD` exports another day; the default is yesterday (UTC). A day that has not
ended is refused.

## Files

```text
<prefix>/<entity>/<yyyy>/<mm>/<dd>/<entity>-<yyyy>-<mm>-<dd>.parquet    entity: usage, namespaces
```

Each upload carries `If-None-Match: *`, so an object is never replaced. A second run of a
day keeps the first run's files and prints `already there, kept` for each; a failed or
repeated run can be retried as it is. To export a day again, delete its objects first.

| Entity       | One row per                                                                                                         | Columns                                                                                                                                                                                                                                        |
| ------------ | ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `usage`      | `org`, `namespace`, `agent`, `operation`, `kind`, `transport`, `traffic_class` of the day, from `loom.agent_events` | `day`, `ops`, `errors`, `server_errors`, `slow` (over its latency budget), `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`, `cost_usd`, `bytes_written`, `latency_p50_ms` / `p95` / `p99`, `users`, `sessions`, `snapshot_at` |
| `namespaces` | `org`, `namespace` registered in `loom.namespaces` or holding a live memory row                                     | `registered`, `kind`, `visibility`, `active`, `created_at`, `live_memories`, `last_memory_at`, `day`, `snapshot_at`                                                                                                                            |

`kind` in `usage` is `request` for a request and `worker` for a worker pass. `users` and
`sessions` are distinct counts within the row that leave out the `-` of a call that named
none; a sum over rows counts a user once per row. The
`namespaces` file is the state at `snapshot_at`, whatever `--day` says: a backfill of an
old day gets that day's usage and today's namespaces. An unregistered namespace has
`registered = false` and NULL registry columns.

## What never leaves Loom

The columns are an allowlist (`server/insights/store/analytics_export.py`; the integration
test pins each entity's exact column list). A file in the bucket is outside
[erasure](../features/memory/retention-and-erasure.md), so the export carries no memory
content and no person: no memory keys or values, no `args`, no `user_id`, `session_id`,
`subject`, `owner_email` or request id. Users and sessions go out as counts only.

Names can identify a person too: self-signup builds an agent's namespace and name from the
OS username and host. `--names` decides how `org`, `namespace` and `agent` leave:

| `--names`          | Written as                                                                                        | Refused when            |
| ------------------ | ------------------------------------------------------------------------------------------------- | ----------------------- |
| `hashed` (default) | the lowercase hex of `sipHash128Keyed` under `LOOM_ANALYTICS_EXPORT_NAME_KEY`, the same every day | the key is empty        |
| `raw`              | the stored name                                                                                   | `LOOM_SELF_SIGNUP=open` |

A hash is stable while the key is, so the warehouse can still group and join by it; a new
key changes every hash. A system that knows the key computes the same hash of its own ids
to join on them. Use `raw` only where every org, namespace and agent is an opaque id that
the operator's own provisioning mints (a tenant or agent UUID), and where no namespace from
an earlier self-signup remains.

## Credentials

The command uses the default AWS credentials of its process. With `--role-arn` it assumes
that role with them first and writes under the role's credentials, which fits a bucket in
another account: the bucket's account grants `s3:PutObject` to a writer role, and the
writer role trusts only the export's own identity. The export needs no read, list or
delete permission on the bucket. `--expected-bucket-owner` makes S3 refuse the write when
another account owns the bucket.

## On Kubernetes

```yaml
analyticsExport:
  enabled: true
  bucket: analytics-landing
  prefix: loom
  roleArn: arn:aws:iam::123456789012:role/loom-analytics-writer
  expectedBucketOwner: "123456789012"
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::210987654321:role/loom-analytics-export
secrets:
  extra:
    LOOM_ANALYTICS_EXPORT_NAME_KEY: "<a random string, kept>"
```

The chart creates a ServiceAccount `<fullname>-analytics-export` and a CronJob of the same
name, where `<fullname>` is the chart's full name: the release name when it contains
`loom`, else `<release>-loom` (`kubectl get sa` shows it). The job runs at 00:30 UTC
(`schedule`), one run at a time, with up to 3 retries and a one-hour deadline; each
statement may run 20 minutes. The web and workers pods keep their own ServiceAccount, so
the role that may assume the writer role is bound to the export's pod only. With IRSA, that
role's trust names `system:serviceaccount:<namespace>:<fullname>-analytics-export`, and its
only permission is `sts:AssumeRole` on `roleArn`. With EKS Pod Identity, bind the role to
the ServiceAccount and leave `annotations` empty.

The job reads ClickHouse with the release's Secret, settings, `extraEnv` and
`extraVolumes`, as the web pods do, and writes nothing to it. A failed run shows as a
failed Job; alert on the CronJob's last successful run being more than a day old.
