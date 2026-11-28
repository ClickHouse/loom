# Adoption telemetry collector

Self-hosted servers send a snapshot of each day's domain activity every five minutes
and an installation report per completed 12-hour UTC window. Both use
`POST https://loom.clickhouse.com/api/v1/adoption`. The collector validates the payload
and acknowledges the installation UUID and snapshot timestamp or window start after
a synchronous insert. The insert token and warehouse views deduplicate retries.
The same Vercel project serves the dashboard at `/adoption`.

## Provision the warehouse

1. Apply [warehouse.sql](warehouse.sql) to the analytics ClickHouse service. It creates
   the report, enrollment, and distribution tables and the public views for the
   adoption site. Apply it as the warehouse owner. Reapplying adds any missing report
   columns and updates views while preserving rows. Reports expire 400 days after
   receipt. Domain snapshots expire after two days.
2. Create a collector user with `INSERT` on
   `loom_product_analytics.adoption_reports`,
   `loom_product_analytics.adoption_domain_snapshots`, and
   `loom_product_analytics.oss_metrics`.
3. Record internal installation classifications in `adoption_enrollments` and
   registry metrics in `oss_metrics`. Restrict access to the enrollment table.

## Configure the site

The site's Vercel project is rooted at `docs`. Use the warehouse's approved runtime
region and set these server-side environment variables in Production.

| Setting                              | Value                                             |
| ------------------------------------ | ------------------------------------------------- |
| `LOOM_TELEMETRY_CLICKHOUSE_URL`      | HTTPS address of the analytics ClickHouse service |
| `LOOM_TELEMETRY_CLICKHOUSE_USER`     | Collector's insert-only user                      |
| `LOOM_TELEMETRY_CLICKHOUSE_PASSWORD` | Credential for that user                          |
| `CRON_SECRET`                        | Secret for the daily snapshot job                 |

Vercel calls `/api/oss-snapshot` daily to save GitHub star and fork counts, release
asset downloads, and GHCR pulls. The metric row key makes repeated runs on the same
UTC day replace that day's snapshot. Distribution charts show the latest observed
total and compare snapshots from consecutive complete periods. Set a
request rate limit on `/api/v1/adoption` in the deployment firewall. Before releasing
the reporting worker, send a synthetic report to a deployment configured with a test
warehouse. Verify acknowledgements for a completed report and a domain snapshot.
Resend both and check that the warehouse uses one report per installation and
window and one snapshot per installation and day. Apply the warehouse schema and
deploy the collector before deploying servers that send snapshots.

Run `node --test docs/lib/adoption-collector.test.mjs docs/lib/oss-snapshot.test.mjs`
to exercise the two endpoints locally.

## Publish adoption metrics

The dashboard reads aggregate metrics and reported contact domain names with a
dedicated ClickHouse Cloud Query API key. Save [public-adoption.sql](public-adoption.sql)
in the SQL console and create or update its Query API endpoint. Give the endpoint's
database role `SELECT` on these views in
`loom_product_analytics`:

- `public_adoption_totals`
- `public_adoption_versions`
- `public_adoption_first_observation`
- `public_adoption_retention`
- `public_adoption_activity`
- `public_adoption_growth`
- `public_adoption_lifecycle`
- `public_adoption_domain_directories`
- `public_adoption_domain_accounts`
- `public_adoption_company_activity`
- `public_oss_adoption`

The views run with the schema owner's privileges. Set `readonly = 2` on the
endpoint role so the Query API can apply HTTP and output-format settings.
Authorize a dedicated API key for the endpoint and set
`LOOM_ADOPTION_QUERY_ENDPOINT_URL`, `LOOM_ADOPTION_QUERY_KEY_ID`, and
`LOOM_ADOPTION_QUERY_KEY_SECRET` in the site's server environment. Use the HTTPS
`/run/<id>` URL from the endpoint configuration. Redeploy the site after saving
the variables.

The site refreshes warehouse data every 30 seconds when requested. It caches
Self-hosted pages with a complete response for one minute. Failed warehouse
refreshes retry after 15 seconds, and the site leaves those pages uncached.

The site fetches repository and release counts from GitHub, SDK downloads from
PyPI Stats, npm, and crates.io, and container pulls from the public package page.
Stored registry metrics fill gaps when the public sources are unavailable. Store
Rust daily downloads in `oss_metrics` with `source = 'crates.io'`,
`artifact = 'clickhouse-loom'` and `metric = 'sdk_downloads'`. The page shows
the latest observation date for each metric and compares reporting periods when
both periods have coverage on every complete UTC day.

The public page supports 30-, 90-, and 365-day windows. Daily charts are available
for 30 days. Weekly and monthly charts are available in every window. CSV exports
use the selected reporting period.

Apply [warehouse.sql](warehouse.sql) and grant `SELECT` on the listed views before
updating the saved Query API query. Deploy the collector before releasing workers
that send version 3. Publishing the site does not run the warehouse changes.
When a version 2 sender has already delivered the final window of a UTC day,
version 3 measurements start with the next completed day.

The Self-hosted summary includes today's completed reporting windows.
Period comparisons use separate totals from complete UTC days.
Growth counts each installation once, at the first successful request in its
retained reports through the latest complete UTC day. Activity groups request,
feature, transport, model, and installation measures by day, week, and month
using the same complete-day cutoff.

The first report date comes from the earliest eligible retained window.
The software versions table counts installations with successful requests and
uses the version pair from each installation's latest active day in the past
30 complete UTC days.
Public metrics exclude internal installations and reports with the smoke test
version in either software field.

The domain directory returns every reported contact domain with attributed successful
requests in today's snapshots or completed reporting windows and the previous 30 or 90 UTC days.
Version 3 reports include active domains across the complete UTC day. The directory
ranks domains by attributed successful requests and counts overlapping snapshots and
completed reports once. The one-year site view uses the 90-day directory. Older
installations appear when they report qualifying activity in the window.

Version 3 adds daily counts for authenticated users and serving processes that
handled eligible successful requests. Counts sum across installations, which can
count the same authenticated identity in more than one installation. A serving process is an
observed host and process pair. Workers and processes created by restarts count
separately. Weekly and monthly activity report the peak measured daily count.

The directory shows successful requests and distinct active installations by
domain in its selected window, including today's snapshots. Active users and
serving processes use the latest measured day. Completed daily counts replace the
snapshot counts for the same installation and domain. If a completed report lists
a domain without counts, the directory can display its recent snapshot. The directory uses snapshots observed
within the past two hours. The first activity date can precede the selected window.
The public response contains counts and version labels. Account identifiers and
serving process names stay on the installation.

Grant the restricted `loom_adoption_top_domains_30d` view only to authorized
analytics staff. Its rows contain reported contact domains and attributed counts.
The `loom_adoption_disclosed_domains` view also contains attributed counts and
requires restricted access.

## Edit the public site

The public pages live in [docs/pages](../../docs/pages). The site's Vercel project
uses `docs` as its root directory. Add a page as an `.astro` file, register its
public path in [mount-site-pages.mjs](../../docs/scripts/mount-site-pages.mjs), and run
`npm --prefix docs run build`. The build preserves the documentation under `/docs`
and serves the site pages at the public paths configured by that script.

For local editing, run `npm --prefix docs run dev` and open the page under the
Blume base path, such as `/docs/home` or `/docs/adoption`.
