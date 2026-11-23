const data = JSON.parse(document.getElementById("initial").textContent);
const colors = {
  baseline: "#8b98ad",
  "loom-treesitter": "#44bda5",
  "loom-treesitter-memory": "#71d5c7",
  "loom-scip": "#eea650",
  "loom-scip-memory": "#cf8ce8",
};
const names = {
  baseline: "No Loom",
  "loom-treesitter": "Tree-sitter",
  "loom-treesitter-memory": "Tree-sitter + memory",
  "loom-scip": "SCIP",
  "loom-scip-memory": "SCIP + memory",
};
const arms = Object.keys(data.arms);
const labelFor = (arm) => names[arm] || data.arms[arm] || arm;
const byId = (id) => document.getElementById(id);
const cacheStarts = [...new Set(data.attempts.map((row) => row.cache_start).filter(Boolean))];
function reportLink(value) {
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value, window.location.href);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}
const node = (tag, text, cls) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
};
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const tokenParts = [
  ["model_cache_read_tokens", "cache reads"],
  ["model_cache_write_tokens", "cache writes"],
  ["model_uncached_input_tokens", "uncached input"],
  ["model_output_tokens", "output"],
];
const cacheCosts = [
  { key: "equal_cold_cost", title: "Equal-cold estimate", kind: "cost" },
  { key: "equal_warm_cost", title: "Equal-warm estimate", kind: "cost" },
];
const signedPercent = new Intl.NumberFormat(undefined, {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
  signDisplay: "exceptZero",
});
function format(value, kind) {
  if (!finite(value)) return "Unknown";
  if (kind === "cost") return "$" + value.toFixed(4);
  if (kind === "rate") return (value * 100).toFixed(0) + "%";
  if (kind === "time") return value.toFixed(1) + " s";
  return Math.round(value).toLocaleString();
}
function tokenCell(label, partValue) {
  const cell = node("td", label, "numeric");
  for (const [key, label] of tokenParts) {
    const value = partValue(key);
    if (finite(value)) cell.append(node("small", `${format(value, "count")} ${label}`));
  }
  return cell;
}
function changeFromBaseline(value, baseline) {
  return finite(value) && baseline > 0 ? signedPercent.format(value / baseline - 1) : null;
}
function addFirstPrompt(cell, valueFor) {
  const total = valueFor("first_prompt_input_tokens");
  const read = format(valueFor("first_cache_read_tokens"), "count");
  const write = format(valueFor("first_cache_write_tokens"), "count");
  const uncached = format(valueFor("first_uncached_input_tokens"), "count");
  cell.append(node("small", finite(total) ? `First prompt ${format(total, "count")} input tokens` : "First prompt input"));
  cell.append(node("small", `${read} read · ${write} written · ${uncached} uncached`));
}
function addCacheCosts(cell, valueFor, baselineFor = () => null) {
  for (const metric of cacheCosts) {
    const value = valueFor(metric);
    const delta = changeFromBaseline(value, baselineFor(metric));
    const valueLabel = format(value, "cost");
    cell.append(node("small", `${metric.title} ${valueLabel}${delta === null ? "" : ` (${delta})`}`));
  }
}
function aggregate(rows, arm, metric) {
  const observed = rows.filter((row) => row.arm === arm && row.state !== "pending");
  const measured = observed.filter((row) => metric.key !== "resolved" || row.state === "complete");
  const values = measured
    .map((row) => row[metric.key])
    .map((v) => (typeof v === "boolean" ? Number(v) : v))
    .filter(finite);
  const total = values.reduce((sum, value) => sum + value, 0);
  const interruptedPerformance =
    ["cost", "time"].includes(metric.kind) && observed.some((row) => row.state === "error");
  return {
    n: measured.length,
    known: values.length,
    total,
    value:
      !interruptedPerformance && values.length && values.length === measured.length
        ? metric.kind === "count"
          ? total
          : total / values.length
        : null,
  };
}
function selected() {
  const tasks = data.cohorts.groups[byId("cohort").value].task_ids;
  return data.attempts.filter(
    (row) =>
      tasks.includes(row.task) &&
      (!byId("issue").value || row.task === byId("issue").value) &&
      (!cacheStarts.length || row.cache_start === byId("cache-start").value) &&
      (!byId("repeat").value || String(row.repeat) === byId("repeat").value),
  );
}
function costMetric(rows) {
  const observed = rows.filter((row) => row.state !== "pending");
  return observed.length && observed.every((row) => finite(row.cost))
    ? { key: "cost", title: "Provider cost / attempt", kind: "cost" }
    : { key: "generation_cost", title: "Solver cost / attempt", kind: "cost" };
}
function timeMetric() {
  return {
    key: byId("timing").value,
    title: byId("timing").selectedOptions[0].textContent + " / attempt",
    kind: "time",
  };
}
function matched(rows) {
  const keys = (arm) =>
    rows
      .filter((row) => row.arm === arm && row.state === "complete")
      .map((row) => `${row.task}:${row.repeat}`)
      .sort()
      .join("|");
  const baseline = keys("baseline");
  return Boolean(baseline) && arms.every((arm) => keys(arm) === baseline);
}
function headings(id, cells) {
  const row = node("tr");
  for (const text of cells) row.append(node("th", text));
  byId(id).replaceChildren(row);
}
function summary(rows, plannedRows) {
  const cost = costMetric(rows),
    time = timeMetric(),
    metrics = [cost, time];
  const baseline = metrics.map((metric) => aggregate(rows, "baseline", metric).value);
  const baselineTokens = aggregate(rows, "baseline", { key: "model_total_tokens", kind: "count" }).value;
  const empty = plannedRows.some((row) => row.state === "complete") ? "Awaiting pairs" : "Not run";
  headings("summary-heading", ["Arm", "Issues solved", "Attempts passed", cost.title, time.title, "Solver tokens"]);
  const body = byId("summary-body");
  body.replaceChildren();
  for (const arm of arms) {
    const observed = rows.filter((row) => row.arm === arm && row.state === "complete");
    const tried = new Set(observed.map((row) => row.task));
    const solved = new Set(observed.filter((row) => row.resolved === true).map((row) => row.task));
    const planned = new Set(plannedRows.filter((row) => row.arm === arm).map((row) => row.task));
    const row = node("tr");
    row.append(node("th", labelFor(arm)));
    const tokens = (key) => aggregate(rows, arm, { key, kind: "count" }).value;
    const issues = node("td", tried.size ? `${solved.size} / ${tried.size} tried` : empty, "numeric");
    if (tried.size < planned.size) issues.append(node("small", `${planned.size} issues scheduled`));
    row.append(
      issues,
      node(
        "td",
        observed.length ? `${observed.filter((x) => x.resolved === true).length} / ${observed.length}` : empty,
        "numeric",
      ),
    );
    for (const [index, metric] of metrics.entries()) {
      const value = aggregate(rows, arm, metric);
      const cell = node("td", value.n ? format(value.value, metric.kind) : empty, "numeric");
      const delta = arm === "baseline" ? null : changeFromBaseline(value.value, baseline[index]);
      if (delta !== null) cell.append(node("small", `${delta} vs No Loom`));
      if (value.known < value.n) cell.append(node("small", `${value.known} of ${value.n} measured`));
      if (metric === cost && observed.length) {
        addFirstPrompt(cell, tokens);
        addCacheCosts(
          cell,
          (variant) => aggregate(rows, arm, variant).value,
          (variant) => (arm === "baseline" ? null : aggregate(rows, "baseline", variant).value),
        );
      }
      row.append(cell);
    }
    const total = tokens("model_total_tokens");
    const tokenDelta = arm === "baseline" ? null : changeFromBaseline(total, baselineTokens);
    const tokenLabel = observed.length ? format(total, "count") : empty;
    row.append(tokenCell(tokenDelta === null ? tokenLabel : `${tokenLabel} (${tokenDelta})`, tokens));
    body.append(row);
  }
  const partial = !data.complete || rows.length !== plannedRows.length || !matched(rows);
  const blocks = new Set(rows.map((row) => `${row.task}:${row.repeat}`)).size;
  const plannedBlocks = new Set(plannedRows.map((row) => `${row.task}:${row.repeat}`)).size;
  const waiting = plannedRows.filter((row) => row.state === "complete" && !row.comparison_included).length;
  const graded = plannedRows.filter((row) => row.state === "complete");
  const verified = graded.filter((row) => row.cache_start_verified === true).length;
  const measuredPairs = (data.cache_pairs || []).filter(
    (pair) => typeof pair.first_prefix_token_count_match === "boolean",
  );
  const matchingPairs = measuredPairs.filter((pair) => pair.first_prefix_token_count_match).length;
  byId("comparison-note").textContent =
    (partial
      ? `${blocks} of ${plannedBlocks} planned blocks matched across all five arms. `
      : `${blocks} matched ${blocks === 1 ? "attempt" : "attempts"} per arm. `) +
    (waiting ? `${waiting} completed attempts await matching arms. ` : "") +
    (cacheStarts.length ? `Provider cache evidence matches the planned start in ${verified} of ${graded.length} graded attempts. ` : "") +
    (measuredPairs.length ? `In ${matchingPairs} of ${measuredPairs.length} measured pairs, the warm first response read the same number of tokens the cold response wrote and wrote zero new tokens. ` : "") +
    "Hidden test failures are included. " +
    (cost.key === "generation_cost"
      ? "Observed costs use solver charges. Equal-cache estimates require a known combined provider cost."
      : "Costs include solver and matched Loom provider charges. Infrastructure and judge costs are separate.");
}
function issuesTable(rows, plannedRows) {
  headings("issues-heading", ["Issue", ...arms.map(labelFor)]);
  const body = byId("issues-body");
  body.replaceChildren();
  for (const task of [...new Set(plannedRows.map((row) => row.task))].sort()) {
    const row = node("tr"),
      title = node("th", data.titles[task] || task, "issue-title");
    title.append(node("div", task, "status"));
    row.append(title);
    const empty = plannedRows.some((entry) => entry.task === task && entry.state === "complete")
      ? "Awaiting pairs"
      : "Not run";
    for (const arm of arms) {
      const completed = rows.filter((entry) => entry.task === task && entry.arm === arm && entry.state === "complete");
      const errors = plannedRows.filter(
        (entry) => entry.task === task && entry.arm === arm && entry.state === "error",
      ).length;
      const cell = node(
        "td",
        completed.length ? `${completed.filter((x) => x.resolved === true).length} / ${completed.length}` : empty,
        "numeric",
      );
      if (errors) cell.append(node("small", `${errors} not graded`));
      row.append(cell);
    }
    body.append(row);
  }
}
function chart(rows, plannedRows) {
  const key = byId("chart-metric").value;
  const metric =
    cacheCosts.find((metric) => metric.key === key) ||
    (key === "cost"
      ? costMetric(rows)
      : key === "agent_seconds"
        ? timeMetric()
        : { key: "resolved", title: "Attempts passed", kind: "rate" });
  const sums = Object.fromEntries(arms.map((arm) => [arm, aggregate(rows, arm, metric)]));
  const maximum = metric.kind === "rate" ? 1 : Math.max(...Object.values(sums).map((x) => x.value ?? 0), 0) * 1.1 || 1;
  const target =
    metric.kind === "rate"
      ? 1
      : data.complete &&
          rows.length === plannedRows.length &&
          matched(rows) &&
          (!cacheStarts.length || rows.every((row) => row.cache_start_verified === true)) &&
          data.run_status !== "interrupted" &&
          data.run_status !== "failed" &&
          sums.baseline.value !== null
        ? sums.baseline.value * (1 - data.target_reduction)
        : null;
  const panel = byId("overview");
  panel.replaceChildren(node("h3", metric.title));
  arms.forEach((arm) => {
    const value = sums[arm],
      row = node("div", undefined, "row"),
      heading = node("div", undefined, "label");
    heading.append(node("span", labelFor(arm)), node("strong", value.n ? format(value.value, metric.kind) : "Not run"));
    const track = node("div", undefined, "track");
    track.setAttribute("role", "img");
    track.setAttribute("aria-label", `${labelFor(arm)}: ${format(value.value, metric.kind)}`);
    if (value.value !== null) {
      const bar = node("div", undefined, "fill");
      bar.style.width = Math.min(100, Math.max(0, (value.value / maximum) * 100)) + "%";
      bar.style.background = colors[arm];
      track.append(bar);
    }
    if (target !== null && target <= maximum) {
      const line = node("span", undefined, "target");
      line.style.left = (target / maximum) * 100 + "%";
      line.title = metric.kind === "rate" ? "All attempts pass" : "20% below no Loom";
      track.append(line);
    }
    row.append(heading, track);
    panel.append(row);
  });
  const axis = node("div", undefined, "axis");
  axis.append(node("span", format(0, metric.kind)), node("span", format(maximum, metric.kind)));
  panel.append(axis);
}
function extraMetrics(rows) {
  headings("metrics-heading", ["Measurement", ...arms.map(labelFor)]);
  const metrics = [
    ...data.metrics.filter((m) => !["resolved", "cost", "agent_seconds"].includes(m.key)),
    ...tokenParts.map(([key, title]) => ({ key, title: `Solver ${title}`, kind: "count" })),
    { key: "model_cache_write_5m_tokens", title: "Solver five-minute cache writes", kind: "count" },
    { key: "model_cache_write_1h_tokens", title: "Solver one-hour cache writes", kind: "count" },
    { key: "loom_provider_input_tokens", title: "Loom provider input", kind: "count" },
    { key: "loom_provider_output_tokens", title: "Loom provider output", kind: "count" },
    { key: "seconds", title: "End-to-end time", kind: "time" },
    { key: "native_seconds", title: "Native Harbor time", kind: "time" },
    { key: "generation_cost", title: "Solver cost / attempt", kind: "cost" },
    { key: "loom_provider_cost", title: "Loom provider cost / attempt", kind: "cost" },
  ];
  const body = byId("metrics-body");
  body.replaceChildren();
  for (const metric of metrics) {
    const row = node("tr");
    row.append(node("th", metric.title));
    for (const arm of arms) {
      const sum = aggregate(rows, arm, metric);
      const cell = node("td", sum.n ? format(sum.value, metric.kind) : "Not run", "numeric");
      cell.title = `${sum.known} of ${sum.n} observed attempts measured`;
      row.append(cell);
    }
    body.append(row);
  }
}
function attemptsTable(rows) {
  const cost = costMetric(rows),
    time = timeMetric();
  headings("attempt-heading", [
    "Issue / repeat",
    "Arm",
    "Status",
    "Hidden tests",
    "Comparison",
    cost.title,
    time.title,
    "Loom calls",
    "Solver tokens",
    "Trial",
  ]);
  const body = byId("attempts");
  body.replaceChildren();
  for (const row of rows
    .filter((x) => byId("attempt-filter").value === "all" || x.state !== "pending")
    .toSorted((a, b) => a.order - b.order)) {
    const item = node("tr");
    for (const text of [
      `${data.titles[row.task] || row.task} · repeat ${Number(row.repeat) + 1}`,
      labelFor(row.arm),
      row.state === "complete" ? "Completed" : row.state === "error" ? "Error / not graded" : "Not run",
      row.state === "complete" ? (row.resolved ? "Pass" : "Fail") : "Not graded",
      row.comparison_included ? "Matched" : "Awaiting matching arms",
    ])
      item.append(node("td", text));
    const costCell = node("td", format(row[cost.key], "cost"), "numeric");
    if (row.state !== "pending") {
      const costSource = {
        "claude-code-reported": "Solver cost reported by Claude Code",
        litellm_estimate: "Solver cost estimated by Harbor",
      }[row.generation_cost_source];
      if (costSource) costCell.append(node("small", costSource));
      addFirstPrompt(costCell, (key) => row[key]);
      if (row.cache_start && row.cache_start_verified !== true) {
        costCell.append(node("small", "Planned cache state unverified"));
      }
      addCacheCosts(costCell, (metric) => row[metric.key]);
    }
    item.append(
      costCell,
      node("td", format(row[time.key], "time"), "numeric"),
      node("td", format(row.loom_tool_calls, "count"), "numeric"),
    );
    const solverTokens = tokenCell(format(row.model_total_tokens, "count"), (key) => row[key]);
    if (row.solver_token_source === "anthropic-reported") {
      solverTokens.append(node("small", "Usage reported by Anthropic"));
    }
    item.append(solverTokens);
    const cell = node("td");
    const target = reportLink(row.url);
    if (target) {
      const link = node("a", "Open trajectory");
      link.href = target;
      cell.append(link);
    }
    item.append(cell);
    body.append(item);
  }
}
function render() {
  const rows = selected(),
    complete = rows.filter((row) => row.state === "complete"),
    paired = rows.filter((row) => row.comparison_included);
  const status = data.run_status || (data.complete ? (data.evaluation_complete ? "complete" : "grading") : "partial");
  byId("run-status").textContent =
    {
      interrupted: "Stopped (not running)",
      failed: "Failed",
      complete: "Complete",
      grading: "Judging",
      running: "Running",
      partial: "Partial",
    }[status] || status;
  byId("completed-label").textContent = ["interrupted", "failed"].includes(status)
    ? "Completed before stop"
    : "Primary attempts completed";
  byId("subtitle").textContent = `${data.name} · ${data.model || "model unavailable"}`;
  document.querySelector("h1").textContent = "Software factory evaluation";
  byId("run-notice").hidden = !["interrupted", "failed"].includes(status);
  byId("run-notice").textContent =
    data.interruption_reason || "This run stopped before the planned comparison completed. Results are partial.";
  byId("issues-count").textContent =
    `${new Set(complete.map((row) => row.task)).size} / ${new Set(rows.map((row) => row.task)).size}`;
  byId("progress").textContent = `${complete.length} / ${rows.length}`;
  byId("error-count").textContent = rows.filter((row) => row.state === "error").length;
  const comparison = data.complete && paired.length === rows.length ? reportLink(data.comparison_url) : null;
  byId("comparison").hidden = !comparison;
  if (comparison) byId("comparison").href = comparison;
  summary(paired, rows);
  issuesTable(paired, rows);
  chart(paired, rows);
  extraMetrics(paired);
  const observed = rows.filter((row) => row.state !== "pending");
  const knownSpend = observed.every((row) => finite(row.generation_cost));
  const spend = knownSpend && observed.length ? observed.reduce((sum, row) => sum + row.generation_cost, 0) : null;
  byId("spend-progress").textContent =
    `Solver spend across all observed primary attempts: ${format(spend, "cost")}. ` +
    "This includes unmatched attempts; the comparison table uses matched attempts only. A/A controls, setup and judging are separate.";
  attemptsTable(rows);
  const group = data.cohorts.groups[byId("cohort").value];
  byId("study-disclosure").textContent = [
    data.study_role ? `Study role: ${data.study_role}.` : "",
    data.loom_schema_profile ? `All Loom arms use the ${data.loom_schema_profile} MCP catalog.` : "",
    ...Object.values(data.memory_treatment || {}),
    `${data.observed - data.attempts.filter((row) => row.state !== "pending").length} A/A control records are excluded from the primary tables. A/A controls repeat no Loom to measure variation.`,
    `${group.task_ids.length} distinct issues; repeats are not additional issues.`,
  ]
    .filter(Boolean)
    .join("\n\n");
  byId("notes").textContent =
    data.cost_note +
    " Blind judgments are separate from hidden tests and remain unknown until measured. Agent time includes the coding loop and its tests. End-to-end time also includes environment setup, verification and index audits. Input tokens include cached tokens; total tokens equal input plus output. Historical issues and authored experiments are not pooled.";
}
for (const [key, group] of Object.entries(data.cohorts.groups).filter(([, group]) => group.task_ids.length)) {
  const option = node("option", group.label);
  option.value = key;
  byId("cohort").append(option);
}
byId("cohort").value = data.cohorts.default;
byId("cache-filter").hidden = !cacheStarts.length;
byId("repeat-label").hidden = Boolean(cacheStarts.length);
for (const start of cacheStarts) {
  const option = node("option", start === "cold" ? "Cold start" : "Warm start");
  option.value = start;
  byId("cache-start").append(option);
}
byId("cohort-label").hidden = Object.values(data.cohorts.groups).filter((group) => group.task_ids.length).length === 1;
for (const task of [...new Set(data.attempts.map((row) => row.task))].sort()) {
  const option = node("option", data.titles[task] || task);
  option.value = task;
  byId("issue").append(option);
}
for (const repeat of [...new Set(data.attempts.map((row) => row.repeat))].sort()) {
  const option = node("option", "Repeat " + (Number(repeat) + 1));
  option.value = String(repeat);
  byId("repeat").append(option);
}
byId("cohort").addEventListener("change", () => {
  byId("issue").value = "";
  const tasks = data.cohorts.groups[byId("cohort").value].task_ids;
  for (const option of byId("issue").options) option.hidden = Boolean(option.value) && !tasks.includes(option.value);
  render();
});
for (const id of ["issue", "repeat", "cache-start", "timing", "chart-metric", "attempt-filter"])
  byId(id).addEventListener("change", render);
render();
