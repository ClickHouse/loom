# Adoption telemetry collector

Self-hosted servers send one versioned JSON report per completed 12-hour UTC window
to `POST https://loom.clickhouse.com/api/v1/adoption`. The endpoint validates and
writes reports to the analytics ClickHouse service. It returns the installation UUID
and window start after a synchronous insert succeeds. The insert token and warehouse
view deduplicate retries. The same Vercel project serves the aggregate dashboard at
`/adoption`.

## Provision the warehouse

1. Apply [warehouse.sql](warehouse.sql) to the analytics ClickHouse service. It creates
   the report, enrollment, and distribution tables and the public views for the
   adoption site. Apply it as the warehouse owner. Reapplying adds any missing report
   columns and updates views while preserving rows. Reports expire 400 days after
   receipt.
2. Create a collector user with `INSERT` on
   `loom_product_analytics.adoption_reports` and
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

Vercel calls `/api/oss-snapshot` daily to save GitHub star and fork counts. The metric
row key makes repeated runs on the same UTC day replace that day's snapshot. Set a
request rate limit on `/api/v1/adoption` in the deployment firewall. Before releasing
the reporting worker, send a synthetic report to a deployment configured with a test
warehouse. Check the acknowledgement, then resend the report and confirm that
`loom_adoption_windows` contains one row for its installation and window.

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
- `public_adoption_version_trends`
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

The first report date and its Loom and ClickHouse versions come from the earliest
eligible retained window. The version charts count installations with successful
requests and use the Loom and ClickHouse versions from each installation's latest
active day in the period.
Public metrics exclude internal installations and reports with the smoke test
version in either software field.

The domain directory returns every reported contact domain with attributed successful
requests in today's completed reporting windows or the previous 30 or 90 UTC days.
Version 3 reports include active domains across the complete UTC day. The directory
ranks domains by attributed successful requests and counts overlapping half-day and
full-day reports once. The one-year site view uses the 90-day directory. Older
installations appear when they report qualifying activity in the window.

Version 3 adds daily counts for authenticated accounts and serving processes that
handled eligible successful requests. Counts sum across installations, which can
count the same account in more than one installation. A serving process is an
observed host and process pair. Workers and processes created by restarts count
separately. Weekly and monthly activity report the peak measured daily count.

Domain activity counts come from reports whose operators enable
`LOOM_ADOPTION_TELEMETRY_SHARE_DOMAIN_ACTIVITY` alongside domain sharing. The
directory shows successful requests and distinct active installations from
complete UTC days in its selected window. Active accounts and serving processes
use the latest measured day. First and last activity dates and the latest Loom
version come from qualifying activity in opted-in reports. The first activity date
can precede the selected window. The public response contains counts and version
labels. It keeps account identifiers and serving process names on the installation.
Measurements are unavailable for domains that share their name alone.

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
