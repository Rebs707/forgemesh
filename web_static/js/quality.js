// ── Quality tab — issues #48, #49, #50, #51, #52 ──────────────────────────────

// ── 2.2 Coverage Metrics ──────────────────────────────────────────────────────

async function loadCoverage() {
  const root = document.getElementById('quality-coverage-body');
  if (!root) return;
  root.innerHTML = '<div class="quality-loading">Loading…</div>';
  try {
    const data = await fetch('/api/coverage').then(r => r.json());
    renderCoverage(data);
  } catch(e) {
    root.innerHTML = `<div class="quality-error">Failed to load coverage: ${escHtml(String(e))}</div>`;
  }
}

function renderCoverage(data) {
  const root = document.getElementById('quality-coverage-body');
  if (!root) return;

  const pct = data.coverage_pct ?? 0;
  const barHtml = _qualityBar([
    { label: 'Confirmed', pct: pct, cls: 'bar-confirmed' },
    { label: 'Pending',   pct: data.pending_pct ?? 0,  cls: 'bar-pending' },
    { label: 'Unmapped',  pct: data.unmapped_pct ?? 0, cls: 'bar-unmapped' },
  ]);

  const tableRows = (data.per_table || []).map(t => `
    <tr>
      <td class="qt-table-name">${escHtml(t.source_table)}</td>
      <td>${t.total_columns}</td>
      <td class="cov-confirmed">${t.confirmed}</td>
      <td class="cov-pending">${t.pending_review}</td>
      <td class="cov-unmapped">${t.unmapped}</td>
      <td>
        <div class="mini-bar-wrap">
          <div class="mini-bar bar-confirmed" style="width:${t.coverage_pct}%"></div>
        </div>
        <span class="mini-bar-label">${t.coverage_pct}%</span>
      </td>
    </tr>`).join('');

  root.innerHTML = `
    <div class="quality-summary-row">
      <div class="quality-stat"><span class="qs-value">${data.total_columns}</span><span class="qs-label">Total columns</span></div>
      <div class="quality-stat confirmed"><span class="qs-value">${data.confirmed}</span><span class="qs-label">Confirmed</span></div>
      <div class="quality-stat pending"><span class="qs-value">${data.pending_review}</span><span class="qs-label">Pending</span></div>
      <div class="quality-stat unmapped"><span class="qs-value">${data.unmapped}</span><span class="qs-label">Unmapped</span></div>
    </div>
    <div class="quality-bar-wrap" title="${pct}% coverage">${barHtml}</div>
    <table class="quality-table">
      <thead><tr><th>Table</th><th>Cols</th><th class="cov-confirmed">✓ Confirmed</th><th class="cov-pending">⧖ Pending</th><th class="cov-unmapped">✗ Unmapped</th><th>Coverage</th></tr></thead>
      <tbody>${tableRows}</tbody>
    </table>`;
}

// ── 2.3 Triple Completeness ───────────────────────────────────────────────────

async function loadCompleteness() {
  const root = document.getElementById('quality-completeness-body');
  if (!root) return;
  root.innerHTML = '<div class="quality-loading">Loading…</div>';
  try {
    const data = await fetch('/api/pipeline/completeness').then(r => r.json());
    renderCompleteness(data);
  } catch(e) {
    root.innerHTML = `<div class="quality-error">Failed: ${escHtml(String(e))}</div>`;
  }
}

function renderCompleteness(data) {
  const root = document.getElementById('quality-completeness-body');
  if (!root) return;

  if (!data.available) {
    root.innerHTML = '<div class="quality-empty">No pipeline stats yet. Run the pipeline (silver phase) first.</div>';
    return;
  }

  const s = data.silver || {};
  const g = data.gold   || {};
  const r = data.rdf    || {};

  const completePct = s.completeness_pct != null ? s.completeness_pct.toFixed(1) + '%' : '—';
  const quarPct     = s.quarantine_pct   != null ? s.quarantine_pct.toFixed(1)   + '%' : '—';

  root.innerHTML = `
    <div class="quality-summary-row">
      <div class="quality-stat confirmed"><span class="qs-value">${(s.bronze_rows_read || 0).toLocaleString()}</span><span class="qs-label">Bronze rows read</span></div>
      <div class="quality-stat confirmed"><span class="qs-value">${(s.silver_rows_written || 0).toLocaleString()}</span><span class="qs-label">Silver rows written</span></div>
      <div class="quality-stat ${(s.quarantine_rows_written || 0) > 0 ? 'unmapped' : ''}">
        <span class="qs-value">${(s.quarantine_rows_written || 0).toLocaleString()}</span><span class="qs-label">Quarantined</span>
      </div>
      <div class="quality-stat"><span class="qs-value">${(r.triples_written || 0).toLocaleString()}</span><span class="qs-label">RDF triples</span></div>
    </div>
    <div class="completeness-metrics">
      <span class="cm-item"><strong>Completeness:</strong> ${completePct}</span>
      <span class="cm-item ${(s.quarantine_rows_written || 0) > 0 ? 'warn' : ''}"><strong>Quarantine rate:</strong> ${quarPct}</span>
      ${g.tables_written && g.tables_written.length ? `<span class="cm-item"><strong>Gold tables:</strong> ${g.tables_written.length}</span>` : ''}
      ${r.named_graph ? `<span class="cm-item"><strong>Named graph:</strong> <code>${escHtml(r.named_graph)}</code></span>` : ''}
    </div>
    <div style="margin-top:12px">
      <button class="quality-action-btn" onclick="loadQuarantineList()">Browse quarantine rows →</button>
    </div>
    <div id="quality-quarantine-browser" style="margin-top:12px;display:none"></div>`;
}

async function loadQuarantineList() {
  const browser = document.getElementById('quality-quarantine-browser');
  if (!browser) return;
  browser.style.display = 'block';
  browser.innerHTML = '<div class="quality-loading">Loading quarantine tables…</div>';
  try {
    const data = await fetch('/api/quarantine').then(r => r.json());
    if (!data.tables || !data.tables.length) {
      browser.innerHTML = '<div class="quality-empty">No quarantine tables found. Silver pipeline may not have written any quarantine rows.</div>';
      return;
    }
    browser.innerHTML = `
      <div class="quarantine-table-list">
        ${data.tables.map(t => `
          <div class="quarantine-table-item" onclick="loadQuarantineRows('${escHtml(t.table)}', this)">
            <span>${escHtml(t.table)}</span>
            <span class="quarantine-count">${t.row_count.toLocaleString()} rows</span>
          </div>`).join('')}
      </div>
      <div id="quarantine-rows-panel"></div>`;
  } catch(e) {
    browser.innerHTML = `<div class="quality-error">${escHtml(String(e))}</div>`;
  }
}

async function loadQuarantineRows(tableName, el) {
  document.querySelectorAll('.quarantine-table-item').forEach(i => i.classList.remove('active'));
  if (el) el.classList.add('active');
  const panel = document.getElementById('quarantine-rows-panel');
  if (!panel) return;
  panel.innerHTML = '<div class="quality-loading">Loading…</div>';
  try {
    const data = await fetch(`/api/quarantine/${encodeURIComponent(tableName)}?limit=50`).then(r => r.json());
    if (!data.rows || !data.rows.length) {
      panel.innerHTML = '<div class="quality-empty">No rows.</div>';
      return;
    }
    const cols = Object.keys(data.rows[0]);
    const thead = `<tr>${cols.map(c => `<th>${escHtml(c)}</th>`).join('')}</tr>`;
    const tbody = data.rows.map(row =>
      `<tr>${cols.map(c => `<td>${escHtml(String(row[c] ?? ''))}</td>`).join('')}</tr>`
    ).join('');
    panel.innerHTML = `
      <div style="font-size:11px;color:var(--muted);margin:8px 0 4px">${data.total.toLocaleString()} total rows · showing first ${data.rows.length}</div>
      <div style="overflow-x:auto"><table class="quality-table quarantine-rows-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table></div>`;
  } catch(e) {
    panel.innerHTML = `<div class="quality-error">${escHtml(String(e))}</div>`;
  }
}

// ── 2.1 Mapping Quality Evaluation ───────────────────────────────────────────

async function loadEvalFiles() {
  const sel = document.getElementById('eval-gt-select');
  if (!sel) return;
  try {
    const data = await fetch('/api/eval/ground-truth').then(r => r.json());
    sel.innerHTML = '<option value="">— select a ground truth file —</option>';
    for (const f of (data.files || [])) {
      const opt = document.createElement('option');
      opt.value = f; opt.textContent = f;
      sel.appendChild(opt);
    }
  } catch(e) { /* no ground truth dir yet */ }
}

async function runMappingEval() {
  const sel = document.getElementById('eval-gt-select');
  const path = sel ? sel.value : '';
  if (!path) { alert('Select a ground truth file first.'); return; }
  const btn = document.getElementById('eval-run-btn');
  const root = document.getElementById('quality-eval-body');
  btn.disabled = true; btn.textContent = 'Evaluating…';
  root.innerHTML = '<div class="quality-loading">Running evaluation…</div>';
  try {
    const r = await fetch('/api/eval/mapping-quality', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    if (!r.ok) { const d = await r.json(); throw new Error(d.detail || r.statusText); }
    renderEvalResult(await r.json());
  } catch(e) {
    root.innerHTML = `<div class="quality-error">${escHtml(String(e))}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = '▶ Run Evaluation';
  }
}

function renderEvalResult(data) {
  const root = document.getElementById('quality-eval-body');
  if (!root) return;

  const pct = v => (v * 100).toFixed(1) + '%';
  const colRows = (data.per_column || []).map(c => `
    <tr class="${c.correct ? '' : 'eval-wrong'}"${c.reason ? ` title="${escHtml(c.reason)}"` : ''}>
      <td>${escHtml(c.table)}</td>
      <td>${escHtml(c.column)}</td>
      <td class="${c.correct ? 'cov-confirmed' : 'cov-unmapped'}">${c.correct ? '✓' : '✗'}</td>
      <td style="font-size:10px;font-family:var(--mono)">${escHtml(c.actual_status)}</td>
      <td style="font-size:10px;font-family:var(--mono);color:var(--muted)">${escHtml(c.expected_uri.split('#').pop() || c.expected_uri)}</td>
      <td style="font-size:10px;font-family:var(--mono)">${c.actual_uri ? escHtml(c.actual_uri.split('#').pop() || c.actual_uri) : '—'}</td>
    </tr>`).join('');

  root.innerHTML = `
    <div style="font-size:11px;color:var(--muted);margin-bottom:8px">
      Dataset: <strong>${escHtml(data.dataset)}</strong> · ${data.total_ground_truth_entries} ground truth entries
    </div>
    <div class="quality-summary-row">
      <div class="quality-stat confirmed"><span class="qs-value">${pct(data.precision)}</span><span class="qs-label">Precision</span></div>
      <div class="quality-stat confirmed"><span class="qs-value">${pct(data.recall)}</span><span class="qs-label">Recall</span></div>
      <div class="quality-stat"><span class="qs-value">${pct(data.f1)}</span><span class="qs-label">F1</span></div>
      <div class="quality-stat pending"><span class="qs-value">${pct(data.pending_rate)}</span><span class="qs-label">Pending rate</span></div>
      <div class="quality-stat unmapped"><span class="qs-value">${pct(data.unmapped_rate)}</span><span class="qs-label">Unmapped rate</span></div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin:8px 0 4px">
      TP: ${data.true_positives} · FP: ${data.false_positives} · FN: ${data.false_negatives}
      ${data.llm_cost_usd != null ? `· Est. LLM cost: $${data.llm_cost_usd.toFixed(4)}` : ''}
      ${data.llm_cost_per_mapping != null ? `· Cost/mapping: $${data.llm_cost_per_mapping.toFixed(6)}` : ''}
    </div>
    <table class="quality-table">
      <thead><tr><th>Table</th><th>Column</th><th>✓</th><th>Status</th><th>Expected</th><th>Actual</th></tr></thead>
      <tbody>${colRows}</tbody>
    </table>`;
}

// ── 2.4 Benchmark Query Set ───────────────────────────────────────────────────

async function loadBenchmarkFiles() {
  const sel = document.getElementById('bench-suite-select');
  if (!sel) return;
  try {
    const data = await fetch('/api/eval/benchmarks').then(r => r.json());
    sel.innerHTML = '<option value="">— select a benchmark suite —</option>';
    for (const f of (data.files || [])) {
      const opt = document.createElement('option');
      opt.value = f; opt.textContent = f;
      sel.appendChild(opt);
    }
  } catch(e) { /* no benchmarks dir yet */ }
}

async function runBenchmark() {
  const sel = document.getElementById('bench-suite-select');
  const path = sel ? sel.value : '';
  if (!path) { alert('Select a benchmark suite file first.'); return; }
  const btn = document.getElementById('bench-run-btn');
  const root = document.getElementById('quality-bench-body');
  btn.disabled = true; btn.textContent = 'Running…';
  root.innerHTML = '<div class="quality-loading">Executing benchmark queries against Fuseki…</div>';
  try {
    const r = await fetch('/api/eval/benchmark/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    if (!r.ok) { const d = await r.json(); throw new Error(d.detail || r.statusText); }
    renderBenchmarkResult(await r.json());
  } catch(e) {
    root.innerHTML = `<div class="quality-error">${escHtml(String(e))}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = '▶ Run Benchmarks';
  }
}

function renderBenchmarkResult(data) {
  const root = document.getElementById('quality-bench-body');
  if (!root) return;

  const rows = (data.results || []).map(r => `
    <tr class="${r.passed ? '' : r.error ? 'bench-error' : 'bench-fail'}">
      <td class="${r.passed ? 'cov-confirmed' : 'cov-unmapped'}">${r.passed ? '✓' : r.error ? 'ERR' : '✗'}</td>
      <td style="font-family:var(--mono);font-size:10px">${escHtml(r.query_id)}</td>
      <td>${escHtml(r.description)}</td>
      <td style="text-align:right">${r.expected_count.toLocaleString()}</td>
      <td style="text-align:right">${r.actual_count != null ? r.actual_count.toLocaleString() : '—'}</td>
      <td style="font-size:10px;color:var(--muted)">${r.error ? escHtml(r.error) : ''}</td>
    </tr>`).join('');

  root.innerHTML = `
    <div style="font-size:11px;color:var(--muted);margin-bottom:8px">
      Run at: ${escHtml(data.run_at)} · ${data.total} queries
    </div>
    <div class="quality-summary-row" style="margin-bottom:12px">
      <div class="quality-stat confirmed"><span class="qs-value">${data.passed}</span><span class="qs-label">Passed</span></div>
      <div class="quality-stat unmapped"><span class="qs-value">${data.failed}</span><span class="qs-label">Failed</span></div>
      <div class="quality-stat pending"><span class="qs-value">${data.errored}</span><span class="qs-label">Errored</span></div>
    </div>
    <table class="quality-table">
      <thead><tr><th>Pass</th><th>ID</th><th>Description</th><th style="text-align:right">Expected</th><th style="text-align:right">Actual</th><th>Error</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── 2.5 Ontology Coherence (invoked from ontology tab) ───────────────────────

function _closeCoherencePanel() {
  const panel = document.getElementById('coherence-panel');
  if (panel) panel.style.display = 'none';
}

async function runCoherenceCheck() {
  const panel = document.getElementById('coherence-panel');
  const btn   = document.getElementById('coherence-run-btn');
  const icon  = document.getElementById('coherence-btn-icon');
  if (!panel || !btn) return;
  // Toggle off if already open
  if (panel.style.display !== 'none') { _closeCoherencePanel(); return; }
  btn.disabled = true;
  if (icon) icon.textContent = '⬡';
  panel.innerHTML = '<div class="quality-loading">Running coherence checks…</div>';
  panel.style.display = 'block';
  try {
    const data = await fetch('/api/ontology/coherence').then(r => r.json());
    renderCoherence(data, panel);
    const hasWarnings = (data.warning_count || 0) > 0;
    if (icon) icon.textContent = data.is_coherent ? (hasWarnings ? '!' : '✓') : '✗';
    if (icon) icon.style.color = data.is_coherent
      ? (hasWarnings ? 'var(--warning, #fbbf24)' : 'var(--success, #34d399)')
      : 'var(--error, #f87171)';
  } catch(e) {
    panel.innerHTML = `<div class="quality-error">${escHtml(String(e))}</div>`;
    if (icon) icon.textContent = '⬡';
  } finally {
    btn.disabled = false;
  }
}

function renderCoherence(data, panel) {
  if (!data.available) {
    panel.innerHTML = `<div class="quality-empty">${escHtml(data.message || 'No ontology loaded.')}</div>`;
    return;
  }

  const statusCls  = data.is_coherent ? 'coherence-ok' : 'coherence-warn';
  const statusText = data.is_coherent ? '✓ Coherent' : `✗ ${data.error_count} error${data.error_count !== 1 ? 's' : ''}`;

  const issueRows = (data.issues || []).map(i => `
    <div class="coherence-issue ${i.severity === 'error' ? 'issue-error' : 'issue-warning'}">
      <span class="issue-severity">${i.severity === 'error' ? '✖' : '⚠'}</span>
      <span class="issue-category">${escHtml(i.category)}</span>
      <span class="issue-message">${escHtml(i.message)}</span>
      ${i.subject_uri ? `<span class="issue-uri" title="${escHtml(i.subject_uri)}">${escHtml(_shortUri(i.subject_uri))}</span>` : ''}
    </div>`).join('');

  panel.innerHTML = `
    <div class="coherence-header">
      <span class="coherence-status ${statusCls}">${statusText}</span>
      <span style="font-size:11px;color:var(--muted)">${data.classes_checked} classes · ${data.properties_checked} properties · ${data.warning_count} warning${data.warning_count !== 1 ? 's' : ''}</span>
      <button id="coherence-close-btn" style="margin-left:auto;background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;line-height:1;padding:2px 4px;" title="Close">✕</button>
    </div>
    <div class="coherence-issues">${issueRows || '<div class="quality-empty">No issues found.</div>'}</div>`;
  document.getElementById('coherence-close-btn').addEventListener('click', _closeCoherencePanel);
}

function _shortUri(uri) {
  if (!uri) return '';
  if (uri.includes('#')) return '#' + uri.split('#').pop();
  const parts = uri.split('/');
  return parts[parts.length - 1] || uri;
}

// ── Shared helpers ────────────────────────────────────────────────────────────

function _qualityBar(segments) {
  return segments.map(s =>
    `<div class="quality-bar-seg ${s.cls}" style="width:${Math.max(s.pct, 0)}%" title="${s.label}: ${s.pct.toFixed(1)}%"></div>`
  ).join('');
}

// ── Tab entry point ───────────────────────────────────────────────────────────

function loadQualityTab() {
  loadCoverage();
  loadCompleteness();
  loadEvalFiles();
  loadBenchmarkFiles();
}
