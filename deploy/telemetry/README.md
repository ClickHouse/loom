# Adoption telemetry collector

Self-hosted servers send one versioned JSON report per completed 12-hour UTC window.
The receiver validates the shape and writes the report to the analytics ClickHouse
service. It returns the installation UUID and window start after a synchronous insert
succeeds. The insert token and warehouse view deduplicate retries.

## Provision the warehouse

1. Apply [warehouse.sql](warehouse.sql) to the analytics ClickHouse service. It creates
   the report, enrollment, and distribution tables and the aggregate views for the
   adoption site. Apply it as the warehouse owner. Reapplying updates view
   definitions and preserves the report table. Reports expire 400 days after
   receipt.
2. Create a collector user with `INSERT` on
   `loom_product_analytics.adoption_reports` and
   `loom_product_analytics.oss_metrics`.
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

Attach `telemetry.loom.clickhouse.com` to the collector project. Set `CRON_SECRET` in the
collector project's production environment. Vercel calls `/api/oss-snapshot`
daily to save GitHub star and fork counts. The metric row key makes repeated
runs on the same UTC day replace that day's snapshot. Set a request rate
limit on `/api/v1/adoption` in the deployment firewall. Send a synthetic report to
the deployed receiver, confirm that the matching acknowledgement follows a warehouse
write, then resend it and confirm a single row in `loom_adoption_windows`. Do this
before releasing the worker that sends to the collector.

Run `npm --prefix deploy/telemetry test` to exercise the receiver locally.

## Publish adoption metrics

The Loom site's Vercel project serves the Self-hosted and OSS tabs at
`/adoption`. Give its server function a separate ClickHouse user with `SELECT` on these
views in `loom_product_analytics`:

- `public_adoption_totals`
- `public_adoption_versions`
- `public_adoption_retention`
- `public_adoption_activity`
- `public_adoption_lifecycle`
- `public_oss_adoption`

The views run with the schema owner's privileges. Give the website user access
only to the aggregate views. Use `LOOM_ADOPTION_PUBLIC_CLICKHOUSE_URL`,
`LOOM_ADOPTION_PUBLIC_CLICKHOUSE_USER`, and `LOOM_ADOPTION_PUBLIC_CLICKHOUSE_PASSWORD`
as server-side environment variables. The URL must use HTTPS. Vercel caches
the aggregate page for an hour. The site fetches repository and release counts
from GitHub, SDK downloads from PyPI Stats and npm, and container pulls from
the public package page. Stored registry metrics fill gaps when the public
sources are unavailable. The page shows the latest observation date for each
metric and compares reporting periods when both periods have coverage on
every date.

The public page supports 30-, 90-, and 365-day windows. Its chart links preserve
the selected filters. CSV links export aggregate activity, lifecycle, download,
and star data for that selection.

Grant the restricted `loom_adoption_top_domains_30d` view only to authorized
analytics staff. Its rows contain opt-in domain names and attributed counts.
The site's user needs only the listed public views.

## Edit the public site

The public pages live in [docs/pages](../../docs/pages). The site's Vercel project
uses `docs` as its root directory. Add a page as an `.astro` file, register its
public path in [mount-site-pages.mjs](../../docs/scripts/mount-site-pages.mjs), and run
`npm --prefix docs run build`. The build preserves the documentation under `/docs`
and serves the site pages at the public paths configured by that script.

For local editing, run `npm --prefix docs run dev` and open the page under the
Blume base path, such as `/docs/home` or `/docs/adoption`.
