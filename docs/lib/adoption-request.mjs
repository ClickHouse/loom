import { loadPublishedAdoption } from "./adoption-data.mjs";
import {
  activityForRange,
  adoptionCsv,
  adoptionFilters,
  adoptionPaths,
  csvBody,
  lifecycleForRange,
  summarizeOss,
} from "./adoption-view.mjs";

/** @param {Request} request @param {URL} url @param {"self-hosted" | "oss"} pageTab */
export async function readAdoptionPage(request, url, pageTab) {
  const submitted = request.method === "POST";
  const params = submitted ? await request.formData().catch(() => null) : url.searchParams;
  if (!params) return new Response("Invalid chart selection", { status: 400 });

  const dataset = params.get("csv");
  const legacyOpenSource = !submitted && pageTab === "self-hosted" && params.get("tab") === "oss";
  if (legacyOpenSource && !dataset && url.searchParams.size === 1) {
    return new Response(null, { status: 308, headers: { Location: adoptionPaths.oss } });
  }
  const tab = dataset
    ? dataset === "downloads" || dataset === "stars"
      ? "oss"
      : "self-hosted"
    : legacyOpenSource
      ? "oss"
      : pageTab;
  const filters = adoptionFilters(params);
  const { today, selfHosted, openSource, warehouseAvailable } = await loadPublishedAdoption(tab);
  const events = activityForRange(selfHosted.activity, filters, today);
  const lifecycle = lifecycleForRange(selfHosted.lifecycle, filters, today);
  const oss = summarizeOss(openSource, filters.days, today);
  const cacheable = !submitted && warehouseAvailable;

  if (dataset) {
    const rows = adoptionCsv(dataset, events, lifecycle, oss);
    if (!rows) return new Response("Unknown dataset", { status: 404 });
    return new Response(csvBody(rows), {
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": `attachment; filename="loom-${dataset}.csv"`,
        "Cache-Control": cacheable ? `public, max-age=${tab === "self-hosted" ? 60 : 300}` : "no-store",
      },
    });
  }

  const browserCache = submitted ? "private, max-age=0" : "no-store";
  const headers = new Headers({
    "Cache-Control": cacheable ? "public, max-age=0, must-revalidate" : browserCache,
  });
  if (cacheable) {
    headers.set(
      "Vercel-CDN-Cache-Control",
      tab === "self-hosted"
        ? "public, s-maxage=60, stale-while-revalidate=60"
        : "public, s-maxage=3600, stale-while-revalidate=86400",
    );
  }
  return {
    headers,
    tab,
    path: adoptionPaths[tab],
    params,
    filters,
    today,
    selfHosted,
    events,
    lifecycle,
    oss,
  };
}
