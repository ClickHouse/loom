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
function format(value, kind) {
  if (!finite(value)) return "Unknown";
  if (kind === "cost") return "$" + value.toFixed(4);
  if (kind === "rate") return (value * 100).toFixed(0) + "%";
  if (kind === "time") return value.toFixed(1) + " s";
  return Math.round(value).toLocaleString();
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
function summary(rows) {
  const cost = costMetric(rows),
    time = timeMetric();
  headings("summary-heading", ["Arm", "Issues solved", "Attempts passed", cost.title, time.title]);
  const body = byId("summary-body");
  body.replaceChildren();
  for (const arm of arms) {
    const observed = rows.filter((row) => row.arm === arm && row.state === "complete");
    const tried = new Set(observed.map((row) => row.task));
    const solved = new Set(observed.filter((row) => row.resolved === true).map((row) => row.task));
    const planned = new Set(rows.filter((row) => row.arm === arm).map((row) => row.task));
    const row = node("tr");
    row.append(node("th", labelFor(arm)));
    const issues = node("td", tried.size ? `${solved.size} / ${tried.size} tried` : "Not run", "numeric");
    if (tried.size < planned.size) issues.append(node("small", `${planned.size} issues scheduled`));
    row.append(
      issues,
      node(
        "td",
        observed.length ? `${observed.filter((x) => x.resolved === true).length} / ${observed.length}` : "Not run",
        "numeric",
      ),
    );
    for (const metric of [cost, time]) {
      const value = aggregate(rows, arm, metric);
      const cell = node("td", value.n ? format(value.value, metric.kind) : "Not run", "numeric");
      if (value.n) cell.append(node("small", `${value.known} of ${value.n} measured`));
      row.append(cell);
    }
    body.append(row);
  }
  const partial = !data.complete || !matched(rows) || data.run_status === "interrupted" || data.run_status === "failed";
  byId("comparison-note").textContent =
    (partial
      ? "Partial results. Issue coverage and repeat counts can differ between arms; these means do not establish a winner. "
      : "Matched issues and repeats. Means describe this run; A/A controls show run-to-run variation. ") +
    (cost.key === "generation_cost"
      ? "Costs below are solver-only; the combined provider-cost audit is not complete."
      : "Costs include solver and matched Loom provider charges. Infrastructure and judge costs are separate.");
}
function issuesTable(rows) {
  headings("issues-heading", ["Issue", ...arms.map(labelFor)]);
  const body = byId("issues-body");
  body.replaceChildren();
  for (const task of [...new Set(rows.map((row) => row.task))].sort()) {
    const row = node("tr"),
      title = node("th", data.titles[task] || task, "issue-title");
    title.append(node("div", task, "status"));
    row.append(title);
    for (const arm of arms) {
      const completed = rows.filter((entry) => entry.task === task && entry.arm === arm && entry.state === "complete");
      const errors = rows.filter((entry) => entry.task === task && entry.arm === arm && entry.state === "error").length;
      const cell = node(
        "td",
        completed.length ? `${completed.filter((x) => x.resolved === true).length} / ${completed.length}` : "Not run",
        "numeric",
      );
      if (errors) cell.append(node("small", `${errors} not graded`));
      row.append(cell);
    }
    body.append(row);
  }
}
function chart(rows) {
  const key = byId("chart-metric").value;
  const metric =
    key === "cost"
      ? costMetric(rows)
      : key === "agent_seconds"
        ? timeMetric()
        : { key: "resolved", title: "Attempts passed", kind: "rate" };
  const sums = Object.fromEntries(arms.map((arm) => [arm, aggregate(rows, arm, metric)]));
  const maximum = metric.kind === "rate" ? 1 : Math.max(...Object.values(sums).map((x) => x.value ?? 0), 0) * 1.1 || 1;
  const target =
    metric.kind === "rate"
      ? 1
      : data.complete &&
          matched(rows) &&
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
    ...["model_input_tokens", "model_output_tokens", "model_cache_read_tokens", "model_cache_write_tokens"].map(
      (key) => ({ key, title: key.replaceAll("_", " "), kind: "count" }),
    ),
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
      format(row[cost.key], "cost"),
      format(row[time.key], "time"),
      format(row.loom_tool_calls, "count"),
      format(row.model_total_tokens, "count"),
    ])
      item.append(node("td", text));
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
    complete = rows.filter((row) => row.state === "complete");
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
  const comparison = reportLink(data.comparison_url);
  byId("comparison").hidden = !comparison;
  if (comparison) byId("comparison").href = comparison;
  summary(rows);
  issuesTable(rows);
  chart(rows);
  extraMetrics(rows);
  attemptsTable(rows);
  const group = data.cohorts.groups[byId("cohort").value];
  byId("study-disclosure").textContent = [
    data.study_role ? `Study role: ${data.study_role}.` : "",
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
for (const id of ["issue", "repeat", "timing", "chart-metric", "attempt-filter"])
  byId(id).addEventListener("change", render);
render();
