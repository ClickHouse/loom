const repositoryUrl = "https://api.github.com/repos/ClickHouse/loom";
const containerUrl = "https://github.com/ClickHouse/loom/pkgs/container/loom";
const releasePageSize = 100;
const starHistoryPageSize = 30;
const starHistoryMaxPages = 100;

export async function readSource(
  url,
  { fetcher = fetch, headers, signal = AbortSignal.timeout(5_000), decode = (response) => response.json() } = {},
) {
  try {
    const response = await fetcher(url, { headers, redirect: "error", signal });
    if (response.ok) return await decode(response);
  } catch {
    // A network or response error leaves the source unavailable for this refresh.
  }
  return null;
}

export function readGitHubRepository(options) {
  return readSource(repositoryUrl, options);
}

async function decodeStarHistoryPage(response) {
  if (response.status !== 200) return null;
  const weeks = await response.json();
  const hasNext = response.headers.get("link")?.includes('rel="next"');
  if (!Array.isArray(weeks) || weeks.length > starHistoryPageSize || (hasNext && !weeks.length)) return null;

  const rows = [];
  for (const entry of weeks) {
    if (!Number.isSafeInteger(entry?.week) || !Number.isSafeInteger(entry.total)) return null;
    const week = new Date(entry.week * 1_000);
    const { days, total } = entry;
    if (
      week.getUTCDay() !== 0 ||
      !Array.isArray(days) ||
      days.length !== 7 ||
      !days.every((count) => Number.isSafeInteger(count) && count >= 0) ||
      days.reduce((sum, count) => sum + count, 0) !== total
    ) {
      return null;
    }
    rows.push({ day: week.toISOString().slice(0, 10), days, total });
  }
  return { rows, hasNext };
}

export async function readStarHistory({ signal = AbortSignal.timeout(5_000), ...options } = {}) {
  const request = {
    ...options,
    signal,
    headers: { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10" },
    decode: decodeStarHistoryPage,
  };
  const history = [];
  for (let page = 1; page <= starHistoryMaxPages && !signal.aborted; page++) {
    const result = await readSource(
      `${repositoryUrl}/stargazers/history?per_page=${starHistoryPageSize}&page=${page}`,
      request,
    );
    if (!result || signal.aborted) return null;
    history.push(...result.rows);
    if (!result.hasNext) return history;
  }
  return null;
}

async function decodeReleasePage(response) {
  if (response.status !== 200) return null;
  const releases = await response.json();
  const hasNext = response.headers.get("link")?.includes('rel="next"');
  if (!Array.isArray(releases) || releases.length > releasePageSize || (hasNext && releases.length === 0)) return null;

  let downloads = 0;
  for (const release of releases) {
    if (!Array.isArray(release?.assets)) return null;
    for (const asset of release.assets) {
      const count = asset?.download_count;
      if (!Number.isSafeInteger(count) || count < 0) return null;
      downloads += count;
    }
  }
  return { downloads, hasNext };
}

export async function readReleaseDownloads({ signal = AbortSignal.timeout(5_000), ...options } = {}) {
  const request = { ...options, signal, decode: decodeReleasePage };
  let total = 0;
  for (let page = 1; !signal.aborted; page++) {
    const result = await readSource(`${repositoryUrl}/releases?per_page=${releasePageSize}&page=${page}`, request);
    if (!result || signal.aborted) return null;
    total += result.downloads;
    if (!Number.isSafeInteger(total)) return null;
    if (!result.hasNext) return total;
  }
  return null;
}

export function readContainerPulls(options) {
  return readSource(containerUrl, {
    ...options,
    async decode(response) {
      if (response.status !== 200) return null;
      // GitHub displays a compact count with the exact total in the heading title.
      const html = await response.text();
      const total = html.match(/<span[^>]*>\s*Total downloads\s*<\/span>\s*<h3\s+title="([0-9]+)"/)?.[1];
      const count = Number(total);
      return Number.isSafeInteger(count) ? count : null;
    },
  });
}
