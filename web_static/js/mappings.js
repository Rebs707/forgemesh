// ── Rationale rendering ────────────────────────────────────────────────────────

function _rationaleHtml(rationale) {
  if (!rationale) return '';
  // Name-exact short-circuit: render a distinct badge line
  if (rationale.startsWith('[name-exact]')) {
    const rest = rationale.replace(/^\[name-exact\]\s*/, '');
    return '<span style="color:var(--accent);font-weight:700">⚡ Exact name match</span> — ' + escHtml(rest);
  }
  // Normalise both separators: old " | Abstract property" and new "\n⟶ Abstract property"
  const normalised = rationale
    .replace(/\s*\|\s*(Abstract property [^|]+)/g, '\n$1')
    .replace(/\n⟶\s*/g, '\n');
  const parts = normalised.split('\n');
  return parts.map((part, i) => {
    if (i > 0 && part.startsWith('Abstract property')) {
      return '<div style="margin-top:6px;color:var(--accent);font-weight:600">⟶ ' + escHtml(part) + '</div>';
    }
    return escHtml(part);
  }).join('');
}

// ── URI autocomplete helper ────────────────────────────────────────────────────

function _attachUriAutocomplete(inputEl) {
  if (!inputEl || inputEl.dataset.acAttached) return;
  inputEl.dataset.acAttached = '1';

  const wrapper = document.createElement('div');
  wrapper.className = 'uri-input-wrapper';
  inputEl.parentNode.insertBefore(wrapper, inputEl);
  wrapper.appendChild(inputEl);

  const dropdown = document.createElement('div');
  dropdown.className = 'uri-autocomplete-dropdown';
  wrapper.appendChild(dropdown);

  function updateDropdown() {
    const q = inputEl.value.toLowerCase().trim();
    if (!q || _ontologyProps.length === 0) { dropdown.style.display = 'none'; return; }
    const matches = _ontologyProps.filter(p =>
      p.uri.toLowerCase().includes(q) || (p.label && p.label.toLowerCase().includes(q))
    ).slice(0, 12);
    if (matches.length === 0) { dropdown.style.display = 'none'; return; }
    dropdown.innerHTML = '';
    for (const p of matches) {
      const local = p.uri.includes('#') ? p.uri.split('#').pop() : p.uri.split('/').pop();
      const item  = document.createElement('div');
      item.className = 'uri-autocomplete-item';
      item.innerHTML = `<span class="uri-ac-local">${escHtml(local)}</span>${p.label ? `<span class="uri-ac-label">${escHtml(p.label)}</span>` : ''}`;
      item.title = p.uri;
      item.addEventListener('mousedown', (e) => {
        e.preventDefault();
        inputEl.value = p.uri;
        dropdown.style.display = 'none';
        const mappingRow = inputEl.closest('.mapping-row');
        if (mappingRow) {
          mappingRow.querySelectorAll('.candidate-chip').forEach(c => {
            c.classList.toggle('selected', c.dataset.uri === p.uri);
          });
        }
      });
      dropdown.appendChild(item);
    }
    dropdown.style.display = 'block';
  }

  inputEl.addEventListener('input', updateDropdown);
  inputEl.addEventListener('focus', updateDropdown);
  inputEl.addEventListener('blur', () => setTimeout(() => { dropdown.style.display = 'none'; }, 150));
}

// ── Mappings ───────────────────────────────────────────────────────────────────

// Mirror of the server-side weighted_sum aggregator (aggregator.py).
// Must be kept in sync with strategy weights and thresholds in settings.py.
const _STRATEGY_WEIGHTS = {
  AnnotationMatcher: 0.20,
  NameMatcher:       0.35,
  TypeMatcher:       0.25,
  SemanticMatcher:   0.45,
  LLMStrategy:       2.00,
};
const _DEFAULT_WEIGHT = 0.30;
const _NAME_EXACT_THRESHOLD = 0.97;  // mirrors NAME_EXACT_CONFIRM_THRESHOLD in settings.py

function _computeWeightedScores(evidenceTrail) {
  // Step 1.5: NameMatcher exact-match short-circuit (mirrors aggregator.py Step 1.5)
  if (_NAME_EXACT_THRESHOLD > 0) {
    let bestNameEv = null;
    for (const ev of evidenceTrail) {
      if (ev.strategy === 'NameMatcher' && ev.mapped_uri) {
        if (!bestNameEv || ev.score > bestNameEv.score) bestNameEv = ev;
      }
    }
    if (bestNameEv && bestNameEv.score >= _NAME_EXACT_THRESHOLD) {
      return [{ uri: bestNameEv.mapped_uri, score: bestNameEv.score, strategy: 'NameMatcher' }];
    }
  }

  // Step 1: best score per (uri, strategy), excluding HistoryMatcher
  const uriStratScores = {};
  const strategiesSeen = new Set();
  for (const ev of evidenceTrail) {
    if (!ev.mapped_uri || ev.strategy === 'HistoryMatcher') continue;
    strategiesSeen.add(ev.strategy);
    if (!uriStratScores[ev.mapped_uri]) uriStratScores[ev.mapped_uri] = {};
    const existing = uriStratScores[ev.mapped_uri][ev.strategy] ?? -1;
    if (ev.score > existing) uriStratScores[ev.mapped_uri][ev.strategy] = ev.score;
  }
  if (Object.keys(uriStratScores).length === 0) return [];

  // Step 2: type gate — eliminate URIs where TypeMatcher scored exactly 0.0
  let candidates = uriStratScores;
  if (strategiesSeen.has('TypeMatcher')) {
    const eliminated = new Set(
      Object.entries(uriStratScores)
        .filter(([, ss]) => ss['TypeMatcher'] != null && ss['TypeMatcher'] === 0.0)
        .map(([uri]) => uri)
    );
    const filtered = Object.fromEntries(
      Object.entries(uriStratScores).filter(([uri]) => !eliminated.has(uri))
    );
    candidates = Object.keys(filtered).length > 0 ? filtered : uriStratScores;
  }

  // Step 3: floor denominator (sum of non-LLM strategy weights that ran)
  let floorWeight = 0;
  for (const s of strategiesSeen) {
    if (s !== 'LLMStrategy') floorWeight += (_STRATEGY_WEIGHTS[s] ?? _DEFAULT_WEIGHT);
  }

  // Step 4: weighted average per URI
  const results = [];
  for (const [uri, stratScores] of Object.entries(candidates)) {
    const uriWeight = Math.max(
      Object.keys(stratScores).reduce((sum, s) => sum + (_STRATEGY_WEIGHTS[s] ?? _DEFAULT_WEIGHT), 0),
      floorWeight
    );
    if (uriWeight === 0) continue;
    const weightedSum = Object.entries(stratScores)
      .reduce((sum, [s, score]) => sum + score * (_STRATEGY_WEIGHTS[s] ?? _DEFAULT_WEIGHT), 0);
    const avg = Math.min(1.0, weightedSum / uriWeight);
    const topStrat = Object.entries(stratScores)
      .reduce((best, [s, sc]) => sc > best[1] ? [s, sc] : best, ['', -1]);
    results.push({ uri, score: avg, strategy: topStrat[0] });
  }
  return results.sort((a, b) => b.score - a.score);
}

function _topCandidates(evidenceTrail, n = 3) {
  return _computeWeightedScores(evidenceTrail).slice(0, n);
}

function _scoreClass(s) {
  if (s >= 0.85) return 'high';
  if (s >= 0.65) return 'mid';
  return 'low';
}

function _evidenceTrailHtml(evidenceTrail, mappedUri) {
  if (!evidenceTrail || evidenceTrail.length === 0) return '';

  let decidingStrategy = null;
  let decidingScore    = -1;
  for (const ev of evidenceTrail) {
    if (ev.mapped_uri === mappedUri && ev.score > decidingScore) {
      decidingScore    = ev.score;
      decidingStrategy = ev.strategy;
    }
  }

  const byStrategy = {};
  for (const ev of evidenceTrail) {
    (byStrategy[ev.strategy] = byStrategy[ev.strategy] || []).push(ev);
  }
  for (const s of Object.keys(byStrategy)) {
    byStrategy[s].sort((a, b) => b.score - a.score);
    byStrategy[s] = byStrategy[s].slice(0, 3);
  }

  const strategyOrder = Object.keys(byStrategy).sort((a, b) => {
    if (a === decidingStrategy) return -1;
    if (b === decidingStrategy) return 1;
    return byStrategy[b][0].score - byStrategy[a][0].score;
  });

  const sections = strategyOrder.map(strategy => {
    const isDecidingGroup = strategy === decidingStrategy;
    const entries = byStrategy[strategy];
    return entries.map((ev, i) => {
      const isDeciding = isDecidingGroup && ev.mapped_uri === mappedUri && i === 0;
      const sc      = _scoreClass(ev.score);
      const pct     = (ev.score * 100).toFixed(0);
      const uriLocal = ev.mapped_uri ? _uriDisplay(ev.mapped_uri).local : '—';
      return `<div class="evidence-row${isDeciding ? ' deciding' : ''}">
        <div class="evidence-strategy">${isDeciding ? '<span class="deciding-star">★</span>' : '<span style="display:inline-block;width:12px"></span>'}${i === 0 ? `<span style="font-weight:700">${escHtml(strategy)}</span>` : `<span style="color:var(--muted);font-weight:400;font-size:10px">#${i + 1}</span>`}</div>
        <div class="evidence-score ${sc}">${pct}%</div>
        <div class="evidence-rationale">${escHtml(ev.rationale || '')}${ev.mapped_uri ? ` <span style="font-family:var(--mono);color:var(--accent2)">#${escHtml(uriLocal)}</span>` : ''}</div>
      </div>`;
    }).join('');
  }).join('');

  const id = `ev-${Math.random().toString(36).slice(2)}`;
  return `<div class="evidence-toggle" onclick="document.getElementById('${id}').classList.toggle('open');this.querySelector('.ev-arrow').textContent=document.getElementById('${id}').classList.contains('open')?'▲':'▼'">
    <span class="ev-arrow">▼</span> Why this mapping?
  </div>
  <div class="evidence-drawer" id="${id}">${sections}</div>`;
}

function _gapStubHtml(col) {
  const bestScore = col.evidence_trail.length
    ? Math.max(...col.evidence_trail.map(e => e.score))
    : 0;
  if (col.status !== 'unmapped' && bestScore >= 0.5) return '';

  const localName = 'has' + col.source_column
    .split(/[_\s]+/)
    .map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join('');

  const t = (col.source_data_type || '').toUpperCase();
  let xsdRange = 'xsd:string';
  if      (t.includes('INT') || t.includes('BIGINT'))         xsdRange = 'xsd:integer';
  else if (t.includes('DECIMAL') || t.includes('NUMERIC'))    xsdRange = 'xsd:decimal';
  else if (t.includes('FLOAT')   || t.includes('DOUBLE'))     xsdRange = 'xsd:double';
  else if (t.includes('BOOLEAN') || t.includes('BIT'))        xsdRange = 'xsd:boolean';
  else if (t.includes('DATETIME'))                             xsdRange = 'xsd:dateTime';
  else if (t.includes('DATE'))                                 xsdRange = 'xsd:date';

  const nearMiss = col.evidence_trail.length && bestScore > 0
    ? `\n  <!-- Best near-miss: ${Math.round(bestScore * 100)}% — below threshold -->`
    : '';

  const stub = `<owl:DatatypeProperty rdf:about="&ont;${localName}">${nearMiss}
  <rdfs:label xml:lang="en">${localName.replace(/([A-Z])/g, ' $1').trim().toLowerCase()}</rdfs:label>
  <rdfs:comment xml:lang="en">TODO: mapped from ${col.source_column} (${col.source_data_type})</rdfs:comment>
  <rdfs:domain rdf:resource="&ont;TODO_ClassName"/>
  <rdfs:range rdf:resource="${xsdRange.replace('xsd:', '&xsd;')}"/>
</owl:DatatypeProperty>`;

  const stubId = `stub-${Math.random().toString(36).slice(2)}`;
  return `<div class="gap-stub">
    <div class="gap-stub-label">⚠ No ontology property found — suggested stub</div>
    <pre class="gap-stub-code" id="${stubId}">${escHtml(stub)}</pre>
    <button class="gap-stub-copy" onclick="navigator.clipboard.writeText(document.getElementById('${stubId}').textContent).then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)})">Copy</button>
  </div>`;
}

const _STATUS_STYLE = {
  confirmed: { label: 'Confirmed', color: 'var(--success)', bg: 'rgba(52,211,153,0.12)' },
  pending:   { label: 'Pending',   color: 'var(--warn)',    bg: 'rgba(251,191,36,0.12)' },
  unmapped:  { label: 'Unmapped',  color: 'var(--muted)',   bg: 'rgba(100,116,139,0.1)' },
  rejected:  { label: 'Rejected',  color: 'var(--error)',   bg: 'rgba(248,113,113,0.1)' },
};

// Tracks which table groups the user has manually collapsed (persists across re-renders)
const _collapsedTables = new Set();

async function loadMappings() {
  const content = document.getElementById('mappings-content');
  document.getElementById('mappings-count-label').textContent = 'Loading…';
  document.getElementById('mappings-stale-banner').style.display = 'none';
  try {
    const data = await fetch('/api/mappings').then(r => r.json());
    _mappingsData = data;
    renderMappings(data);
  } catch(e) {
    content.innerHTML = `<div style="color:var(--error);padding:20px">Failed to load mappings: ${escHtml(String(e))}</div>`;
  }
}

function renderMappings(data) {
  const content   = document.getElementById('mappings-content');
  const label     = document.getElementById('mappings-count-label');
  const badge     = document.getElementById('pending-badge');
  const filterVal = document.getElementById('mappings-filter').value;
  const searchQ   = (document.getElementById('mappings-search')?.value || '').toLowerCase().trim();
  const searching = searchQ.length > 0;

  const pending = data.pending_total ?? 0;
  label.textContent = `${data.total} mapping${data.total !== 1 ? 's' : ''} · ${pending} pending review`;

  badge.style.display = pending > 0 ? 'inline' : 'none';
  if (pending > 0) badge.textContent = pending;

  content.innerHTML = '';

  const visibleTables = data.tables.map(t => {
    let cols = filterVal === 'all' ? t.columns : t.columns.filter(c => c.status === filterVal);
    if (searching) {
      const tableMatch = t.source_table.toLowerCase().includes(searchQ);
      cols = tableMatch ? cols : cols.filter(c =>
        c.source_column.toLowerCase().includes(searchQ) ||
        (c.mapped_property_uri || '').toLowerCase().includes(searchQ) ||
        (c.rationale || '').toLowerCase().includes(searchQ)
      );
    }
    return { ...t, columns: cols };
  }).filter(t => t.columns.length > 0);

  if (visibleTables.length === 0) {
    content.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:60px 0;color:var(--muted);">
      <div style="font-size:36px;opacity:0.4">✓</div>
      <p style="font-size:14px">${searching ? 'No matches' : filterVal === 'all' ? 'No mappings found' : `No ${filterVal} mappings`}</p>
    </div>`;
    return;
  }

  visibleTables.forEach(table => {
    const group      = document.createElement('div');
    const isCollapsed = !searching && _collapsedTables.has(table.source_table);
    group.className  = 'mapping-table-group' + (isCollapsed ? ' collapsed' : '');

    const h3 = document.createElement('h3');
    h3.innerHTML = `<span class="mapping-group-chevron">▼</span>${escHtml(table.source_table)}<span class="mapping-group-count">${table.columns.length}</span>`;
    h3.addEventListener('click', () => {
      const tbl = table.source_table;
      if (_collapsedTables.has(tbl)) { _collapsedTables.delete(tbl); }
      else { _collapsedTables.add(tbl); }
      group.classList.toggle('collapsed');
    });
    group.appendChild(h3);

    const rowsContainer = document.createElement('div');
    rowsContainer.className = 'mapping-group-rows';
    group.appendChild(rowsContainer);

    table.columns.forEach(col => {
      const row = document.createElement('div');
      row.className    = 'mapping-row';
      row.dataset.table  = table.source_table;
      row.dataset.column = col.source_column;

      const confidence = (col.confidence_score * 100).toFixed(0);
      const uri        = col.mapped_property_uri || '';
      const st         = _STATUS_STYLE[col.status] || _STATUS_STYLE.unmapped;
      const isPending  = col.status === 'pending';
      const uriShort   = uri ? (uri.includes('#') ? uri.split('#').pop() : uri.split('/').pop()) : '';
      const candidates = _topCandidates(col.evidence_trail);

      row.innerHTML = `
        <div class="mapping-col-info">
          <div class="mapping-col-name">${escHtml(col.source_column)}</div>
          <div class="mapping-col-type">${escHtml(col.source_data_type)}</div>
          <div style="margin-top:6px">
            <span style="font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px;background:${st.bg};color:${st.color};">${st.label}</span>
            <span style="font-size:11px;color:var(--muted);margin-left:6px">${confidence}%</span>
          </div>
        </div>
        <div class="mapping-details">
          <div class="mapping-uri" title="${escHtml(uri)}">${uri
            ? `<span style="color:var(--muted);font-size:10px;margin-right:3px">${escHtml(_uriDisplay(uri).ns)}</span><b>${escHtml(uriShort)}</b>`
            : '<span style="color:var(--muted);font-style:italic">No URI mapped</span>'}</div>
          ${isPending ? `
          <div class="candidates">
            <div class="candidates-label">Candidates</div>
            ${candidates.map((c, i) => {
              const parts = _uriDisplay(c.uri);
              const pct   = (c.score * 100).toFixed(0);
              const sc    = _scoreClass(c.score);
              return `<div class="candidate-chip" data-uri="${escHtml(c.uri)}" title="${escHtml(c.uri)}">
                <span class="candidate-rank">#${i + 1}</span>
                <span class="candidate-uri">${escHtml(parts.local)}</span>
                <span class="candidate-score ${sc}">${pct}%</span>
              </div>`;
            }).join('')}
            ${candidates.length === 0 ? '<span style="font-size:11px;color:var(--muted);font-style:italic">No candidates in evidence trail</span>' : ''}
          </div>
          <div class="mapping-uri-edit">
            <input type="text" class="uri-input" value="${escHtml(uri)}" placeholder="Select a candidate above or enter URI…" />
          </div>` : ''}
          <div class="mapping-rationale">${_rationaleHtml(col.rationale)}</div>
          ${_evidenceTrailHtml(col.evidence_trail, col.mapped_property_uri)}
          ${_gapStubHtml(col)}
        </div>
        <div class="mapping-actions">
          ${isPending
            ? `<button class="btn-confirm">✓ Confirm</button><button class="btn-reject">✕ Reject</button>`
            : `<button class="btn-override" style="font-size:11px;font-weight:600;padding:4px 10px;border-radius:5px;border:1px solid var(--border);background:none;color:var(--muted);cursor:pointer;" title="Override this mapping">Edit</button>`
          }
          <button class="btn-view-corrections" data-column="${escHtml(col.source_column)}"
            style="font-size:10px;padding:3px 8px;border-radius:5px;border:1px solid var(--border);background:none;color:var(--muted);cursor:pointer;margin-top:2px;"
            title="View corrections store entries for this column">Corrections</button>
        </div>`;

      if (isPending) {
        const uriInput = row.querySelector('.uri-input');
        _attachUriAutocomplete(uriInput);

        row.querySelectorAll('.candidate-chip').forEach(chip => {
          chip.addEventListener('click', () => {
            uriInput.value = chip.dataset.uri;
            row.querySelectorAll('.candidate-chip').forEach(c => c.classList.remove('selected'));
            chip.classList.add('selected');
          });
          if (chip.dataset.uri === uri) chip.classList.add('selected');
        });

        row.querySelector('.btn-confirm').addEventListener('click', async () => {
          const uriVal = uriInput.value.trim();
          if (!uriVal) { alert('Select a candidate or enter a property URI to confirm.'); return; }
          await doMappingAction('confirm', table.source_table, col.source_column, uriVal, row);
        });
        row.querySelector('.btn-reject').addEventListener('click', async () => {
          await doMappingAction('reject', table.source_table, col.source_column, null, row);
        });
      } else {
        row.querySelector('.btn-override').addEventListener('click', () => {
          const actionsEl = row.querySelector('.mapping-actions');
          const detailsEl = row.querySelector('.mapping-details');
          if (!detailsEl.querySelector('.uri-input')) {
            const editDiv = document.createElement('div');
            editDiv.className = 'mapping-uri-edit';
            editDiv.innerHTML = `<input type="text" class="uri-input" value="${escHtml(uri)}" placeholder="Enter property URI…" />`;
            detailsEl.appendChild(editDiv);
            _attachUriAutocomplete(editDiv.querySelector('.uri-input'));
          }
          actionsEl.innerHTML = `<button class="btn-confirm">✓ Confirm</button><button class="btn-cancel" style="font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid var(--border);background:none;color:var(--muted);cursor:pointer;">Cancel</button>`;
          actionsEl.querySelector('.btn-confirm').addEventListener('click', async () => {
            const uriVal = row.querySelector('.uri-input').value.trim();
            if (!uriVal) { alert('Enter a property URI to confirm.'); return; }
            await doMappingAction('confirm', table.source_table, col.source_column, uriVal, row);
          });
          actionsEl.querySelector('.btn-cancel').addEventListener('click', () => loadMappings());
        });
      }

      row.querySelector('.btn-view-corrections').addEventListener('click', () => {
        document.querySelector('.tab-btn[data-tab="corrections"]').click();
        document.getElementById('corrections-search').value = col.source_column;
        renderCorrections();
      });

      rowsContainer.appendChild(row);
    });
    content.appendChild(group);
  });
}

async function doMappingAction(action, sourceTable, sourceColumn, confirmedUri, rowEl) {
  const body = { source_table: sourceTable, source_column: sourceColumn };
  if (confirmedUri) body.confirmed_uri = confirmedUri;

  try {
    const r = await fetch(`/api/mappings/${action}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert(`Error: ${err.detail || r.statusText}`);
      return;
    }
    document.getElementById('mappings-stale-banner').style.display = 'flex';
    rowEl.classList.add(action === 'confirm' ? 'confirmed' : 'rejected');
    rowEl.querySelector('.btn-confirm').disabled = true;
    if (rowEl.querySelector('.btn-reject')) rowEl.querySelector('.btn-reject').disabled = true;
    rowEl.style.opacity = '0.5';

    if (_mappingsData) {
      for (const t of _mappingsData.tables) {
        if (t.source_table === sourceTable) {
          for (const c of t.columns) {
            if (c.source_column === sourceColumn) {
              c.status = action === 'confirm' ? 'confirmed' : 'unmapped';
              if (confirmedUri) c.mapped_property_uri = confirmedUri;
            }
          }
        }
      }
      _mappingsData.pending_total = _mappingsData.tables.reduce(
        (n, t) => n + t.columns.filter(c => c.status === 'pending').length, 0
      );
      const badge = document.getElementById('pending-badge');
      const p = _mappingsData.pending_total;
      document.getElementById('mappings-count-label').textContent =
        `${_mappingsData.total} mapping${_mappingsData.total !== 1 ? 's' : ''} · ${p} pending review`;
      badge.style.display = p > 0 ? 'inline' : 'none';
      if (p > 0) badge.textContent = p;
    }
  } catch(e) {
    alert(`Request failed: ${e}`);
  }
}

document.getElementById('mappings-reload-btn').addEventListener('click', loadMappings);
document.getElementById('mappings-filter').addEventListener('change', () => {
  if (_mappingsData) renderMappings(_mappingsData);
});
