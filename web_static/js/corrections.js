// ── Corrections tab ────────────────────────────────────────────────────────────

function _populateDatasetFilter() {
  const sel = document.getElementById('corrections-dataset-filter');
  const current = sel.value;
  // Collect unique (ontology_source, db_source) pairs
  const seen = new Map(); // ontology_source → db_source (use first db_source seen per ontology)
  for (const r of _correctionsData) {
    if (r.ontology_source && !seen.has(r.ontology_source)) {
      seen.set(r.ontology_source, r.db_source || '');
    }
  }
  // Rebuild options, preserving the blank "All datasets" option
  while (sel.options.length > 1) sel.remove(1);
  for (const [ontology, db] of seen) {
    const opt = document.createElement('option');
    // Value encodes both parts as JSON so we can split on retrieval
    opt.value = JSON.stringify({ ontology_source: ontology, db_source: db });
    // Display: basename of the OWL path, or full string if no slash
    const label = ontology.split('/').pop() || ontology;
    opt.textContent = label + (db ? ` (${db})` : '');
    sel.appendChild(opt);
  }
  // Restore previous selection if still valid
  if (current && [...sel.options].some(o => o.value === current)) {
    sel.value = current;
  }
}

async function loadCorrections() {
  try {
    const res  = await fetch('/api/corrections');
    const data = await res.json();
    _correctionsData = data.corrections || [];
    _populateDatasetFilter();
    renderCorrections();
  } catch(e) {
    document.getElementById('corrections-content').innerHTML =
      `<div style="color:var(--error);padding:20px">Failed to load corrections: ${escHtml(String(e))}</div>`;
  }
}

function _correctionRationale(entry) {
  if (entry.confirmation_type === 'HUMAN') return 'Confirmed via web UI';
  const by = (entry.confirmed_by || '').trim();
  if (!by || by === 'AUTO') return 'Auto-confirmed by pipeline';
  const strategies = by.split(',').map(s => s.trim()).filter(Boolean);
  const readable = strategies.map(s => {
    if (s === 'LLMStrategy')       return 'LLM';
    if (s === 'AnnotationMatcher') return 'annotation match';
    if (s === 'NameMatcher')       return 'name match';
    if (s === 'SemanticMatcher')   return 'semantic similarity';
    if (s === 'TypeMatcher')       return 'type match';
    if (s === 'web_ui')            return 'web UI';
    return s;
  });
  if (readable.length === 1) return `Auto-confirmed by ${readable[0]}`;
  const last = readable.pop();
  return `Auto-confirmed by ${readable.join(', ')} and ${last}`;
}

function _strategyLabel(strategy) {
  const map = {
    'LLMStrategy':       'LLM',
    'AnnotationMatcher': 'Annotation',
    'NameMatcher':       'Name Match',
    'SemanticMatcher':   'Semantic',
    'TypeMatcher':       'Type Match',
  };
  return map[strategy] || strategy;
}

function _scoreClass(score) {
  if (score >= 0.8) return 'high';
  if (score >= 0.5) return 'mid';
  return 'low';
}

function _buildEvidencePanel(entry) {
  let evidence = [];
  try {
    if (entry.evidence_json) evidence = JSON.parse(entry.evidence_json);
  } catch(_) {}

  const panel = document.createElement('div');
  panel.className = 'correction-evidence';

  if (!evidence.length) {
    panel.innerHTML = `<div style="padding:8px 12px;font-size:11px;color:var(--muted)">
      ${entry.confirmation_type === 'HUMAN'
        ? 'Manually confirmed via web UI — no strategy evidence recorded.'
        : 'No evidence trail recorded for this entry.'}
    </div>`;
    return panel;
  }

  // Deduplicate by strategy (keep highest score per strategy), then top 3
  const bestByStrategy = {};
  for (const ev of evidence) {
    if (!bestByStrategy[ev.strategy] || ev.score > bestByStrategy[ev.strategy].score)
      bestByStrategy[ev.strategy] = ev;
  }
  const top3 = Object.values(bestByStrategy)
    .sort((a, b) => b.score - a.score)
    .slice(0, 3);

  const maxScore = top3[0]?.score ?? 0;

  panel.innerHTML = `<div class="correction-evidence-header">
    <span>Strategy</span><span>Score</span><span>Rationale</span><span>URI</span>
  </div>`;

  for (const ev of top3) {
    const row = document.createElement('div');
    row.className = 'correction-evidence-row' + (ev.score === maxScore ? ' top-score' : '');
    const uriLocal = ev.mapped_uri ? _uriDisplay(ev.mapped_uri).local : '—';
    row.innerHTML = `
      <div style="font-size:11px;font-weight:600;color:var(--text)">${escHtml(_strategyLabel(ev.strategy))}</div>
      <div class="correction-evidence-score ${_scoreClass(ev.score)}">${(ev.score * 100).toFixed(0)}%</div>
      <div class="correction-evidence-rationale">${escHtml(ev.rationale || '—')}</div>
      <div class="correction-evidence-uri" title="${escHtml(ev.mapped_uri || '')}">${escHtml(uriLocal)}</div>`;
    panel.appendChild(row);
  }

  return panel;
}

function renderCorrections() {
  const content       = document.getElementById('corrections-content');
  const typeFilter    = document.getElementById('corrections-type-filter').value;
  const datasetRaw    = document.getElementById('corrections-dataset-filter').value;
  const search        = document.getElementById('corrections-search').value.toLowerCase();
  const clearDataset  = document.getElementById('corrections-clear-dataset-btn');

  // Show/hide "Clear dataset" button depending on whether a dataset is selected
  clearDataset.style.display = datasetRaw ? 'inline-block' : 'none';

  let rows = _correctionsData;
  if (typeFilter) rows = rows.filter(r => r.confirmation_type === typeFilter);
  if (datasetRaw) {
    try {
      const { ontology_source, db_source } = JSON.parse(datasetRaw);
      rows = rows.filter(r => r.ontology_source === ontology_source && (r.db_source || '') === db_source);
    } catch(_) {}
  }
  if (search) rows = rows.filter(r =>
    r.source_column.toLowerCase().includes(search) ||
    (r.confirmed_uri || '').toLowerCase().includes(search) ||
    r.source_table.toLowerCase().includes(search)
  );

  document.getElementById('corrections-count').textContent =
    `${rows.length} entr${rows.length !== 1 ? 'ies' : 'y'}`;

  if (rows.length === 0) {
    content.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:60px 0;color:var(--muted);">
      <div style="font-size:36px;opacity:0.4">⟳</div>
      <p style="font-size:14px">No corrections found</p>
    </div>`;
    return;
  }

  // Group by source_table.source_column
  const groups = {};
  for (const r of rows) {
    const key = `${r.source_table}.${r.source_column}`;
    (groups[key] = groups[key] || []).push(r);
  }

  content.innerHTML = '';
  for (const [key, entries] of Object.entries(groups)) {
    const group = document.createElement('div');
    group.className = 'correction-group';

    const humanCount = entries.filter(e => e.confirmation_type === 'HUMAN').length;
    const autoCount  = entries.filter(e => e.confirmation_type === 'AUTO').length;
    group.innerHTML = `<div class="correction-group-header">
      ${escHtml(key)}
      <span style="font-weight:400;color:var(--muted)">${humanCount ? `${humanCount} human` : ''}${humanCount && autoCount ? ' · ' : ''}${autoCount ? `${autoCount} auto` : ''}</span>
    </div>`;

    const header = document.createElement('div');
    header.style.cssText = 'display:grid;grid-template-columns:22px 2fr 48px 72px 90px 28px;gap:10px;padding:3px 10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.07em;color:var(--muted);';
    header.innerHTML = '<span></span><span>URI</span><span>Score</span><span>Type</span><span>Date</span><span></span>';
    group.appendChild(header);

    for (const entry of entries) {
      // Determine if there is evidence to show
      let hasEvidence = false;
      try {
        const ev = entry.evidence_json ? JSON.parse(entry.evidence_json) : [];
        hasEvidence = ev.length > 0 || entry.confirmation_type === 'HUMAN';
      } catch(_) {}

      const row = document.createElement('div');
      row.className = `correction-row${entry.winning ? ' winning' : ''}${!hasEvidence ? ' no-evidence' : ''}`;

      const uriLocal  = entry.confirmed_uri ? _uriDisplay(entry.confirmed_uri).local : '—';
      const score     = entry.confidence_score != null ? `${(entry.confidence_score * 100).toFixed(0)}%` : '—';
      const date      = entry.created_at ? entry.created_at.slice(0, 10) : '—';
      const typeClass = entry.confirmation_type === 'HUMAN' ? 'human' : 'auto';

      row.innerHTML = `
        <button class="correction-expand-btn" title="Show evidence">▶</button>
        <div class="correction-uri" title="${escHtml(entry.confirmed_uri || '')}">
          ${escHtml(uriLocal)}${entry.winning ? ' <span style="color:var(--success);font-size:9px;font-weight:700;">★ winning</span>' : ''}
          <div style="font-size:10px;color:var(--muted);font-weight:400;margin-top:2px;font-family:inherit">${escHtml(_correctionRationale(entry))}</div>
        </div>
        <div style="font-size:11px;font-weight:700;color:var(--muted)">${score}</div>
        <div><span class="correction-type-chip ${typeClass}">${entry.confirmation_type}</span></div>
        <div style="font-size:10px;color:var(--muted)">${date}</div>
        <button class="correction-delete-btn" data-id="${entry.id}" title="Delete this entry">✕</button>`;

      const evidencePanel = _buildEvidencePanel(entry);

      const expandBtn = row.querySelector('.correction-expand-btn');
      if (!hasEvidence) {
        expandBtn.style.visibility = 'hidden';
      } else {
        expandBtn.addEventListener('click', () => {
          const open = evidencePanel.classList.toggle('open');
          expandBtn.classList.toggle('open', open);
        });
      }

      row.querySelector('.correction-delete-btn').addEventListener('click', async (e) => {
        const id = parseInt(e.currentTarget.dataset.id);
        if (!confirm(`Delete correction #${id} for ${key}?`)) return;
        const r = await fetch(`/api/corrections/${id}`, { method: 'DELETE' });
        if (r.ok) { await loadCorrections(); }
        else { alert('Delete failed: ' + (await r.text())); }
      });

      group.appendChild(row);
      group.appendChild(evidencePanel);
    }
    content.appendChild(group);
  }
}

document.getElementById('corrections-type-filter').addEventListener('change', renderCorrections);
document.getElementById('corrections-dataset-filter').addEventListener('change', renderCorrections);
document.getElementById('corrections-search').addEventListener('input', renderCorrections);

document.getElementById('corrections-prune-btn').addEventListener('click', async () => {
  const total   = _correctionsData.length;
  const winning = _correctionsData.filter(r => r.winning).length;
  const toDelete = total - winning;
  if (toDelete === 0) { alert('Nothing to prune — all entries are already winning.'); return; }
  if (!confirm(`Remove ${toDelete} non-winning entr${toDelete !== 1 ? 'ies' : 'y'}, keeping ${winning} winner${winning !== 1 ? 's' : ''}?\n\nThis tidies up redundant AUTO votes from previous pipeline runs. The pipeline will still use the same winning mappings on the next run.`)) return;
  const res = await fetch('/api/corrections/prune', { method: 'POST' });
  if (res.ok) { await loadCorrections(); }
  else { alert('Prune failed: ' + (await res.text())); }
});

document.getElementById('corrections-clear-dataset-btn').addEventListener('click', async () => {
  const datasetRaw = document.getElementById('corrections-dataset-filter').value;
  if (!datasetRaw) return;
  let parsed;
  try { parsed = JSON.parse(datasetRaw); } catch(_) { return; }
  const { ontology_source, db_source } = parsed;
  const label = ontology_source.split('/').pop() || ontology_source;
  const datasetRows = _correctionsData.filter(r => r.ontology_source === ontology_source && (r.db_source || '') === db_source);
  if (!confirm(`Delete all ${datasetRows.length} corrections for dataset:\n${label}\n\n⚠ This will remove all learned mappings for this ontology. MIMIC and other datasets are unaffected.\n\nThis cannot be undone.`)) return;
  const params = new URLSearchParams({ ontology_source, db_source });
  const res = await fetch(`/api/corrections/by-dataset?${params}`, { method: 'DELETE' });
  if (res.ok) {
    document.getElementById('corrections-dataset-filter').value = '';
    await loadCorrections();
  } else {
    alert('Delete failed: ' + (await res.text()));
  }
});

document.getElementById('corrections-clear-btn').addEventListener('click', async () => {
  const total = _correctionsData.length;
  if (!confirm(`Delete all ${total} corrections?\n\n⚠ Warning: this will remove all learned mappings.\n\n• The next pipeline run will re-learn all mappings from scratch using the LLM and matching strategies.\n• This will take longer and incur additional API costs.\n• Any HUMAN corrections made via the web UI will also be lost and must be re-confirmed.\n\nThis cannot be undone.`)) return;
  const res = await fetch('/api/corrections', { method: 'DELETE' });
  if (res.ok) { await loadCorrections(); }
  else { alert('Delete failed: ' + (await res.text())); }
});
