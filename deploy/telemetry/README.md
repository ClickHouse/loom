# Adoption telemetry collector

Self-hosted servers send one versioned JSON report per completed 12-hour UTC window.
The receiver validates the shape and writes the report to the analytics ClickHouse
service. It returns the installation UUID and window start after a synchronous insert
succeeds. The insert token and warehouse view deduplicate retries.

## Provision the warehouse

1. Apply [warehouse.sql](warehouse.sql) to the analytics ClickHouse service. It creates
   the report table, the enrollment and distribution tables, and the views used by the
   dashboard queries. The report table expires rows 400 days after receipt.
2. Create a collector user with `INSERT` on
   `loom_product_analytics.adoption_reports`. Give the analytics connection read access
   to the views it queries.
3. Load verified enrollment records into `adoption_enrollments` and registry metrics
   into `oss_metrics`. The collector records self-reported installation activity.
   Keep company account verification in the enrollment workflow.

## Deploy the collector

Create a Vercel project rooted at `deploy/telemetry`. Use the warehouse's approved
runtime region and set these server-side environment variables:

| Setting                              | Value                                             |
| ------------------------------------ | ------------------------------------------------- |
| `LOOM_TELEMETRY_CLICKHOUSE_URL`      | HTTPS address of the analytics ClickHouse service |
| `LOOM_TELEMETRY_CLICKHOUSE_USER`     | Collector's insert-only user                      |
| `LOOM_TELEMETRY_CLICKHOUSE_PASSWORD` | Credential for that user                          |

Attach `telemetry.loom.clickhouse.com` to the collector project. Set a request rate
limit on `/api/v1/adoption` in the deployment firewall. Send a synthetic report to
the deployed receiver, confirm that the matching acknowledgement follows a warehouse
write, then resend it and confirm a single row in `loom_adoption_windows`. Do this
before releasing the worker that sends to the collector.

Run `npm --prefix deploy/telemetry test` to exercise the receiver locally.

## Publish the dashboard

Point the analytics workspace connection at `loom_product_analytics`. Put each
SELECT from [self-hosted adoption](../dashboards/self_hosted_adoption.sql) in a SQL
cell on the Self-hosted tab and each SELECT from [OSS adoption](../dashboards/oss_adoption.sql)
on the OSS tab. Restrict the published app to readers authorized to see company data.

## Edit the public site

The public pages live in [docs/pages](../../docs/pages). The site's Vercel project
uses `docs` as its root directory. Add a page as an `.astro` file, register its
public path in [mount-site-pages.mjs](../../docs/scripts/mount-site-pages.mjs), and run
`npm --prefix docs run build`. The build preserves the documentation under `/docs`
and serves the site pages at their public paths.

For local editing, run `npm --prefix docs run dev` and open the page under the
Blume base path, such as `/docs/home` or `/docs/telemetry`.
