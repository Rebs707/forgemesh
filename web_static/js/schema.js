// ── Project selector ──────────────────────────────────────────────────────────

async function loadProjects() {
  try {
    const data = await fetch('/api/projects').then(r => r.json());
    const sel = document.getElementById('project-select');
    if (!sel) return;
    // Preserve the blank "— none —" option
    sel.innerHTML = '<option value="">— none —</option>';
    for (const name of (data.projects || [])) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      if (name === data.active) opt.selected = true;
      sel.appendChild(opt);
    }
    _updateHeaderProject(data.active || '');
  } catch(e) {
    console.warn('Could not load projects:', e);
  }
}

async function selectProject(name) {
  if (name === (document.getElementById('project-select')?.dataset?.active || '')) return;
  _dismissOntologyWarningBanner();
  try {
    const r = await fetch('/api/projects/select', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project: name}),
    });
    const data = await r.json();
    if (!r.ok) {
      _showOntologyWarningBanner('Project error: ' + (data.detail || r.status), []);
      return;
    }
    _updateHeaderProject(name);
    if (typeof loadDataSources === 'function') loadDataSources();
    if (typeof loadOntologyFiles === 'function') loadOntologyFiles();
    loadRawSchema();
    if (data.warning) {
      _showOntologyWarningBanner(data.warning, data.available_ontologies || []);
    }
  } catch(e) {
    console.warn('selectProject error:', e);
  }
}

function _dismissOntologyWarningBanner() {
  const existing = document.getElementById('ontology-warning-banner');
  if (existing) existing.remove();
}

function _showOntologyWarningBanner(message, available) {
  _dismissOntologyWarningBanner();

  const banner = document.createElement('div');
  banner.id = 'ontology-warning-banner';
  banner.style.cssText = [
    'position:fixed', 'top:52px', 'left:50%', 'transform:translateX(-50%)',
    'z-index:8000', 'background:#1c1a12', 'border:1px solid #7c6a1a',
    'border-radius:6px', 'padding:10px 14px', 'display:flex', 'align-items:center',
    'gap:10px', 'max-width:680px', 'width:calc(100% - 32px)',
    'box-shadow:0 4px 16px rgba(0,0,0,0.5)', 'font-size:13px', 'color:#d4b84a',
  ].join(';');

  const icon = document.createElement('span');
  icon.textContent = '⚠';
  icon.style.flexShrink = '0';
  banner.appendChild(icon);

  const msg = document.createElement('span');
  msg.textContent = message;
  msg.style.cssText = 'flex:1;color:#c9aa3a';
  banner.appendChild(msg);

  if (available.length > 0) {
    const sel = document.createElement('select');
    sel.style.cssText = 'background:#111;color:#ccc;border:1px solid #444;border-radius:4px;padding:3px 6px;font-size:12px;max-width:220px';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '— pick ontology —';
    sel.appendChild(placeholder);
    for (const f of available) {
      const opt = document.createElement('option');
      opt.value = f;
      opt.textContent = f.replace(/^owl\//, '');
      sel.appendChild(opt);
    }

    const applyBtn = document.createElement('button');
    applyBtn.textContent = 'Use';
    applyBtn.style.cssText = 'background:#3a3010;color:#d4b84a;border:1px solid #7c6a1a;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:12px;white-space:nowrap';
    applyBtn.onclick = async () => {
      const path = sel.value;
      if (!path) return;
      if (typeof selectOntologyFromDropdown === 'function') {
        await selectOntologyFromDropdown(path);
      }
      _dismissOntologyWarningBanner();
    };

    banner.appendChild(sel);
    banner.appendChild(applyBtn);
  }

  const closeBtn = document.createElement('button');
  closeBtn.textContent = '×';
  closeBtn.title = 'Dismiss';
  closeBtn.style.cssText = 'background:none;border:none;color:#888;font-size:16px;cursor:pointer;padding:0 2px;line-height:1;flex-shrink:0';
  closeBtn.onclick = _dismissOntologyWarningBanner;
  banner.appendChild(closeBtn);

  document.body.appendChild(banner);
}

function _updateHeaderProject(name) {
  const chip = document.getElementById('header-datasource-chip');
  const nameEl = document.getElementById('header-datasource-name');
  if (chip && nameEl) {
    if (name) {
      nameEl.textContent = name;
      chip.style.display = 'flex';
    }
  }
}

// ── Data source selector ───────────────────────────────────────────────────────

async function loadDataSources() {
  try {
    const data = await fetch('/api/datasources').then(r => r.json());
    const sel  = document.getElementById('rawschema-db-select');
    sel.innerHTML = '';
    for (const db of (data.databases || [])) {
      const opt = document.createElement('option');
      opt.value = db;
      opt.textContent = db;
      if (db === data.active) opt.selected = true;
      sel.appendChild(opt);
    }
    if (!data.databases || !data.databases.length) {
      sel.innerHTML = '<option value="">No databases found</option>';
    }
    _updateHeaderDatasource(data.active || '');
    document.getElementById('rawschema-db-url').textContent = data.connection_url || '';
  } catch(e) {
    document.getElementById('rawschema-db-select').innerHTML =
      '<option value="">Could not connect</option>';
  }
}

async function selectDataSource(dbName) {
  if (!dbName) return;
  try {
    const r = await fetch('/api/datasources/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ database: dbName }),
    });
    if (!r.ok) { const d = await r.json(); alert('Could not switch database: ' + (d.detail || r.statusText)); return; }
    const resp = await r.json();
    _rawSchemaData = null;
    _rawSchemaSelectedTable = null;
    _updateHeaderDatasource(dbName);
    document.getElementById('rawschema-db-url').textContent = resp.connection_url || '';
    await loadRawSchema();
  } catch(e) { alert('Error switching database: ' + e); }
}

function _updateHeaderDatasource(dbName) {
  const chip   = document.getElementById('header-datasource-chip');
  const nameEl = document.getElementById('header-datasource-name');
  if (dbName) {
    nameEl.textContent = dbName;
    chip.style.display = 'flex';
  } else {
    chip.style.display = 'none';
  }
}

function _updateHeaderOntology(filename) {
  const chip   = document.getElementById('header-ontology-chip');
  const nameEl = document.getElementById('header-ontology-name');
  if (filename) {
    nameEl.textContent = filename;
    chip.style.display = 'flex';
  } else {
    chip.style.display = 'none';
  }
}

// ── Relational Schema tab ──────────────────────────────────────────────────────

async function loadRawSchema() {
  if (_rawSchemaData) { renderRawSchemaTable(_rawSchemaSelectedTable); return; }
  document.getElementById('rawschema-tables-list').innerHTML =
    '<div style="padding:20px 12px;text-align:center;font-size:11px;color:var(--muted)">Loading…</div>';
  try {
    const data = await fetch('/api/inspection').then(r => r.json());
    _rawSchemaData = data.tables || [];
    const staleChip = document.getElementById('rawschema-stale-chip');
    staleChip.style.display = data.stale ? 'inline' : 'none';
    if (data.active_database) _updateHeaderDatasource(data.active_database);
    renderRawSchemaList();
    if (_rawSchemaData.length > 0) renderRawSchemaTable(_rawSchemaData[0].table_name);
  } catch(e) {
    document.getElementById('rawschema-tables-list').innerHTML =
      `<div style="padding:16px;color:var(--error);font-size:11px">Failed to load: ${escHtml(String(e))}</div>`;
  }
}

function renderRawSchemaList() {
  const list    = document.getElementById('rawschema-tables-list');
  const countEl = document.getElementById('rawschema-table-count');
  countEl.textContent = `${_rawSchemaData.length} table${_rawSchemaData.length !== 1 ? 's' : ''}`;
  list.innerHTML = '';
  for (const t of _rawSchemaData) {
    const cols       = Object.values(t.columns || {});
    const warnings   = cols.flatMap(c => c.quality_warnings || []);
    const hasCritical = warnings.some(w => w.severity === 'CRITICAL');
    const hasWarning  = !hasCritical && warnings.some(w => w.severity === 'WARNING');
    const flagDot = hasCritical
      ? '<span class="rawschema-flag-dot critical" title="Has critical flags"></span>'
      : hasWarning
        ? '<span class="rawschema-flag-dot warning" title="Has warnings"></span>'
        : '';
    const item = document.createElement('div');
    item.className    = 'rawschema-table-item' + (_rawSchemaSelectedTable === t.table_name ? ' active' : '');
    item.dataset.table = t.table_name;
    item.innerHTML = `
      <div class="rawschema-table-name">${escHtml(t.table_name)}</div>
      <div class="rawschema-table-meta">
        ${flagDot}
        <span>${t.row_count.toLocaleString()} rows</span>
        <span>·</span>
        <span>${cols.length} cols</span>
      </div>`;
    item.addEventListener('click', () => renderRawSchemaTable(t.table_name));
    list.appendChild(item);
  }
}

function renderRawSchemaTable(tableName) {
  _rawSchemaSelectedTable = tableName;
  document.querySelectorAll('.rawschema-table-item').forEach(el => {
    el.classList.toggle('active', el.dataset.table === tableName);
  });

  const table = (_rawSchemaData || []).find(t => t.table_name === tableName);
  const panel = document.getElementById('rawschema-detail-panel');
  if (!table) {
    panel.innerHTML = '<div id="rawschema-detail-empty"><div style="font-size:32px;opacity:0.3">⊞</div><span>Select a table to view its columns</span></div>';
    return;
  }

  const cols  = Object.values(table.columns || {});
  const fkMap = {};      // column → ref_table
  const fkInferred = {}; // column → bool (heuristic vs declared)
  for (const fk of (table.foreign_keys || [])) {
    fkMap[fk.column] = fk.ref_table;
    fkInferred[fk.column] = !!fk.inferred;
  }

  const allWarnings = cols.flatMap(c =>
    (c.quality_warnings || []).map(w => ({ col: c.name, ...w }))
  );
  const warningsHtml = allWarnings.length ? `
    <div class="rawschema-warnings-box">
      ${allWarnings.map(w => `
        <div>
          <span class="${w.severity === 'CRITICAL' ? 'warn-critical' : ''}">${w.severity === 'CRITICAL' ? '✖' : '⚠'} ${escHtml(w.message)}</span>
          <span style="color:var(--muted)"> — <code>${escHtml(w.col)}</code></span>
          ${w.sample_anomalies && w.sample_anomalies.length ? `<span style="color:var(--muted);font-size:10px"> · ${escHtml(w.sample_anomalies.join(', '))}</span>` : ''}
        </div>`).join('')}
    </div>` : '';

  const rowsHtml = cols.map(c => {
    const warnings    = c.quality_warnings || [];
    const hasCritical = warnings.some(w => w.severity === 'CRITICAL');
    const hasWarning  = warnings.some(w => w.severity === 'WARNING');
    const rowClass    = hasCritical ? 'has-critical' : hasWarning ? 'has-warning' : '';
    const warnIcon    = hasCritical
      ? '<span class="col-warn-icon critical" title="Critical flag">✖</span>'
      : hasWarning
        ? '<span class="col-warn-icon warning" title="Warning">⚠</span>'
        : '';
    const pkBadge = c.is_primary_key ? '<span class="col-pk-badge">PK</span>' : '';
    const fkTitle = fkMap[c.name]
      ? `${fkInferred[c.name] ? 'Inferred FK' : 'FK'} → ${fkMap[c.name]}`
      : '';
    const fkBadge = fkMap[c.name]
      ? `<span class="col-fk-badge${fkInferred[c.name] ? ' inferred' : ''}" title="${escHtml(fkTitle)}">${fkInferred[c.name] ? '~FK' : 'FK'}</span>`
      : '';
    const nullPct  = Math.round((c.null_rate || 0) * 100);
    const nullBar  = nullPct > 0
      ? `<span class="col-null-bar" style="width:${Math.min(nullPct, 60)}px" title="${nullPct}% null"></span>`
      : '';
    // Cardinality ratio: what % of rows have a unique value (issue #70)
    const rowCount = table.row_count || 0;
    const uniquePct = (rowCount > 0 && c.distinct_count != null)
      ? Math.round(c.distinct_count / rowCount * 100) : null;
    const uniqueLabel = uniquePct === 100
      ? `<span style="font-size:9px;color:var(--muted)">all unique</span>`
      : uniquePct !== null && uniquePct <= 5
        ? `<span style="font-size:9px;color:var(--accent2)">${uniquePct}% unique</span>`
        : '';
    const samples = (c.top_values || []).length
      ? c.top_values.slice(0, 4).map(v => escHtml(String(v.value))).join(', ')
      : (c.sample_data || []).slice(0, 4).map(v => escHtml(String(v))).join(', ');
    const formats = (c.detected_formats || []).length
      ? `<div style="font-size:10px;color:var(--warn);margin-top:2px">formats: ${escHtml(c.detected_formats.join(', '))}</div>`
      : '';
    return `<tr class="${rowClass}">
      <td><span class="col-name-cell">${escHtml(c.name)}</span>${pkBadge}${fkBadge}${warnIcon}</td>
      <td><span class="col-type-chip">${escHtml(c.inferred_type)}</span></td>
      <td>${nullBar}${nullPct > 0 ? `<span style="font-size:10px;color:${nullPct>30?'var(--warn)':'var(--muted)'}">${nullPct}%</span>` : '<span style="color:var(--muted);font-size:10px">—</span>'}</td>
      <td style="font-size:10px;color:var(--muted)">${c.distinct_count != null ? c.distinct_count.toLocaleString() : '—'}<br>${uniqueLabel}</td>
      <td class="col-samples">${samples}${formats}</td>
    </tr>`;
  }).join('');

  panel.innerHTML = `
    <div class="rawschema-table-title">${escHtml(table.table_name)}</div>
    <div class="rawschema-table-subtitle">${table.row_count.toLocaleString()} rows · ${cols.length} columns${table.foreign_keys && table.foreign_keys.length ? ' · ' + table.foreign_keys.length + ' FK' + (table.foreign_keys.length !== 1 ? 's' : '') : ''}</div>
    ${warningsHtml}
    <table class="rawschema-col-table">
      <thead><tr>
        <th>Column</th><th>Type</th><th>Null %</th><th>Distinct</th><th>Sample values</th>
      </tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;
}

document.getElementById('rawschema-reinspect-btn').addEventListener('click', async () => {
  const btn  = document.getElementById('rawschema-reinspect-btn');
  const chip = document.getElementById('rawschema-stale-chip');
  btn.disabled = true;
  btn.textContent = 'Inspecting…';
  chip.style.display = 'inline';
  try {
    const r = await fetch('/api/pipeline/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phases: 'inspect', threshold: 0.9 }),
    });
    if (!r.ok) { const d = await r.json(); alert('Failed: ' + (d.detail || r.statusText)); }
    else {
      _pipelineLogCursor = 0;
      document.getElementById('pipeline-log').innerHTML = '';
      setPipelineStatus('running');
      startPipelineStream(0);
      _setPdState('partial');
      const poll = setInterval(async () => {
        const s = await fetch('/api/pipeline/status').then(x => x.json());
        if (s.status !== 'running') {
          clearInterval(poll);
          _rawSchemaData = null;
          btn.disabled = false;
          btn.textContent = '↻ Re-inspect';
          loadRawSchema();
        }
      }, 2000);
    }
  } catch(e) { alert('Error: ' + e); btn.disabled = false; btn.textContent = '↻ Re-inspect'; }
});
