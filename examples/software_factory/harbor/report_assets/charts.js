let data = JSON.parse(document.getElementById('initial').textContent);
const colors = {baseline: '#8b98ad', 'loom-treesitter': '#44bda5', 'loom-scip': '#eea650'};
const node = (tag, text, cls) => {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (cls) item.className = cls;
  return item;
};
const valueFor = (row, metric) => row[metric.key === 'agent_seconds'
  ? document.getElementById('timing').value : metric.key];
function format(value, kind) {
  if (value === null || value === undefined) return 'pending / unknown';
  if (kind === 'cost') return '$' + value.toFixed(6);
  if (kind === 'rate') return (100 * value).toFixed(1) + '%';
  if (kind === 'time') return value.toFixed(2) + ' s';
  return String(value);
}
function aggregate(rows, arm, metric) {
  const scheduled = rows.filter(row => row.arm === arm);
  const observed = scheduled.filter(row => row.state !== 'pending');
  const values = observed.map(row => valueFor(row, metric))
    .map(value => typeof value === 'boolean' ? Number(value) : value)
    .filter(value => typeof value === 'number');
  const total = values.reduce((sum, value) => sum + value, 0);
  const value = values.length && values.length === observed.length
    ? metric.kind === 'count' ? total : total / values.length : null;
  return {expected: scheduled.length, n: observed.length, known: values.length, total, value};
}
function charts(rows) {
  const panel = node('div', undefined, 'charts');
  for (const metric of data.metrics) {
    const card = node('article', undefined, 'card');
    const title = metric.key === 'agent_seconds'
      ? document.querySelector('#timing option:checked').textContent : metric.title;
    card.append(node('h3', title));
    const sums = Object.fromEntries(Object.keys(data.arms).map(arm => [arm, aggregate(rows, arm, metric)]));
    const maximum = metric.kind === 'rate' ? 1
      : Math.max(...Object.values(sums).map(sum => sum.value ?? 0), 0) * 1.1 || 1;
    const target = metric.key === 'resolved' ? 1
      : ['cost', 'agent_seconds'].includes(metric.key) && sums.baseline.value !== null
        ? sums.baseline.value * (1 - data.target_reduction) : null;
    for (const [arm, label] of Object.entries(data.arms)) {
      const sum = sums[arm], row = node('div', undefined, 'row');
      const heading = node('div', undefined, 'label');
      const display = metric.kind === 'rate' && sum.value !== null
        ? `${sum.total}/${sum.known} (${format(sum.value, metric.kind)})` : format(sum.value, metric.kind);
      heading.append(node('span', label), node('strong', display));
      const track = node('div', undefined, 'track');
      track.setAttribute('role', 'img');
      track.setAttribute('aria-label', `${label}: ${display}`);
      if (sum.value !== null) {
        const bar = node('div', undefined, 'fill');
        bar.style.width = Math.min(100, Math.max(0, 100 * sum.value / maximum)) + '%';
        bar.style.background = colors[arm];
        track.append(bar);
      }
      if (target !== null && target <= maximum) {
        const line = node('span', undefined, 'target');
        line.style.left = 100 * target / maximum + '%';
        line.title = metric.key === 'resolved' ? 'All issues fixed' : '20% below no-Loom';
        track.append(line);
      }
      row.append(heading, track, node('div', `${sum.n}/${sum.expected} observed · ${sum.known} measured`, 'status'));
      if (metric.key === 'cost') {
        const generation = aggregate(rows, arm, {key: 'generation_cost', kind: 'cost'}).value;
        const backend = aggregate(rows, arm, {key: 'loom_provider_cost', kind: 'cost'}).value;
        row.append(node('div', `Generation ${format(generation, 'cost')} · Loom provider ${format(backend, 'cost')}`, 'status'));
      }
      if (metric.key === 'model_total_tokens') {
        const values = ['model_input_tokens', 'model_output_tokens', 'model_cache_read_tokens', 'model_cache_write_tokens']
          .map(key => aggregate(rows, arm, {key, kind: 'count'}).value);
        row.append(node('div', `Input ${format(values[0], 'count')} · output ${format(values[1], 'count')} · cache read ${format(values[2], 'count')} · cache write ${format(values[3], 'count')}`, 'status'));
      }
      card.append(row);
    }
    const axis = node('div', undefined, 'axis');
    axis.append(node('span', format(0, metric.kind)), node('span', format(maximum, metric.kind)));
    card.append(axis, node('div', metric.kind === 'count' ? 'Total count' : 'Per observed attempt', 'status'));
    panel.append(card);
  }
  return panel;
}
function selected() {
  const issue = document.getElementById('issue').value;
  const repeat = document.getElementById('repeat').value;
  return data.attempts.filter(row => (!issue || row.task === issue) && (!repeat || String(row.repeat) === repeat));
}
function render() {
  document.getElementById('subtitle').textContent = `${data.name} · ${data.model || 'model unavailable'}`;
  document.getElementById('progress').textContent = `${data.observed}/${data.scheduled} attempts observed · ${data.complete ? data.evaluation_complete ? 'complete' : 'evaluations running' : 'running'}`;
  document.getElementById('comparison').href = data.comparison_url || '/jobs';
  const rows = selected();
  document.getElementById('overview').replaceChildren(...charts(rows).children);
  const issues = document.getElementById('issues');
  issues.replaceChildren();
  for (const task of [...new Set(rows.map(row => row.task))].sort()) {
    const section = node('section');
    section.append(node('h2', data.titles[task] || task), charts(rows.filter(row => row.task === task)));
    issues.append(section);
  }
  const heading = node('tr');
  for (const text of ['Issue / repeat', 'Arm', ...data.metrics.map(metric => metric.title), 'Trial']) heading.append(node('th', text));
  document.getElementById('attempt-heading').replaceChildren(heading);
  const body = document.getElementById('attempts');
  body.replaceChildren();
  for (const row of rows.toSorted((a, b) => a.task.localeCompare(b.task) || a.repeat - b.repeat || a.arm.localeCompare(b.arm))) {
    const item = node('tr');
    item.append(node('td', `${data.titles[row.task] || row.task} · ${Number(row.repeat) + 1}`), node('td', data.arms[row.arm]));
    for (const metric of data.metrics) {
      const value = valueFor(row, metric);
      const cell = node('td', typeof value === 'boolean' ? value ? 'yes' : 'no' : format(value, metric.kind));
      if (metric.key === 'model_total_tokens') cell.title = `Input ${format(row.model_input_tokens, 'count')}; output ${format(row.model_output_tokens, 'count')}; cache read ${format(row.model_cache_read_tokens, 'count')}; cache write ${format(row.model_cache_write_tokens, 'count')}`;
      item.append(cell);
    }
    const link = node('td');
    if (row.url) {
      const anchor = node('a', 'Open trajectory');
      anchor.href = row.url;
      link.append(anchor);
    }
    item.append(link);
    body.append(item);
  }
  document.getElementById('notes').textContent = data.cost_note + ' Judge evaluations are blinded and separate from hidden tests. Unmeasured results remain unknown. Agent time includes the coding loop and its tests; full end-to-end also includes environment setup, verification and index audits. Input tokens include cached tokens; total tokens equal input plus output. Counters show totals; cost and time show means. A/A controls are separate. Three task clusters do not establish a general performance claim.';
}
for (const task of [...new Set(data.attempts.map(row => row.task))].sort()) {
  const option = node('option', data.titles[task] || task);
  option.value = task;
  document.getElementById('issue').append(option);
}
for (const repeat of [...new Set(data.attempts.map(row => row.repeat))].sort()) {
  const option = node('option', 'Repeat ' + (Number(repeat) + 1));
  option.value = String(repeat);
  document.getElementById('repeat').append(option);
}
for (const id of ['issue', 'repeat', 'timing']) document.getElementById(id).addEventListener('change', render);
render();
if (location.protocol.startsWith('http') && !data.evaluation_complete) {
  const timer = setInterval(async () => {
    try {
      const response = await fetch(location.pathname.replace(/\.html$/, '.json'), {cache: 'no-store'});
      if (response.ok) {
        data = await response.json();
        render();
        if (data.evaluation_complete) clearInterval(timer);
      }
    } catch {}
  }, 10000);
}
