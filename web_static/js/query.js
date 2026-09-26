// ── Ontology property loader (for URI autocomplete) ───────────────────────────

async function _loadOntologyProps() {
  try {
    const data = await fetch('/api/ontology/properties').then(r => r.json());
    _ontologyProps = data.properties || [];
  } catch(e) { /* ok if ontology not loaded */ }
}

// ── Query layer track ──────────────────────────────────────────────────────────

const _QUERY_LAYERS = [
  { id: 'bronze', label: 'bronze' },
  { id: 'silver', label: 'silver' },
  { id: 'gold',   label: 'gold'   },
  { id: 'graph',  label: 'graph'  },
];
var _queryLayer = 'graph';

(function _buildQueryLayerTrack() {
  const track = document.getElementById('query-layer-track');
  if (!track) return;
  _QUERY_LAYERS.forEach((layer, i) => {
    if (i > 0) {
      const rail = document.createElement('div');
      rail.className = 'qt-rail';
      track.appendChild(rail);
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'qt-node' + (layer.id === _queryLayer ? ' sel' : '');
    btn.dataset.layer = layer.id;
    btn.textContent = layer.label;
    btn.addEventListener('click', () => {
      _queryLayer = layer.id;
      track.querySelectorAll('.qt-node').forEach(n =>
        n.classList.toggle('sel', n.dataset.layer === _queryLayer)
      );
    });
    track.appendChild(btn);
  });
})();

// ── Query ──────────────────────────────────────────────────────────────────────

async function submitQuery() {
  const q = document.getElementById('question').value.trim();
  if (!q) return;

  setLoading(true);
  document.getElementById('question').value = '';

  try {
    const limit = parseInt(document.getElementById('limit-select').value);
    const res = await fetch('/api/query', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ question: q, limit, layer: _queryLayer }),
    });
    const data = await res.json();
    data._limit = limit;
    data._layer = _queryLayer;
    addTurn(q, data);
    showTurn(turns.length - 1);
  } catch(e) {
    addTurn(q, { error: String(e), rows: [], row_count: 0, sparql: '', explanation: '', classes_used: [], _layer: _queryLayer });
    showTurn(turns.length - 1);
  } finally {
    setLoading(false);
  }
}

function addTurn(question, result) {
  turns.push({ question, result });
  renderHistory();
}

// ── Render history ─────────────────────────────────────────────────────────────

function renderHistory() {
  const el = document.getElementById('history');
  el.innerHTML = '';
  turns.forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'turn' + (i === activeTurn ? ' active' : '');
    div.onclick = () => showTurn(i);

    let badgeClass = 'badge-ok';
    let badgeLabel = `${t.result.row_count} rows`;
    if (t.result.error)             { badgeClass = 'badge-err';  badgeLabel = 'Error'; }
    else if (t.result.row_count === 0) { badgeClass = 'badge-zero'; badgeLabel = '0 rows'; }

    const layerTag = t.result._layer
      ? `<span class="layer-badge layer-${t.result._layer}">${t.result._layer}</span>`
      : '';
    div.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px">
        <div class="turn-q">${escHtml(t.question)}</div>
        ${layerTag}
      </div>
      <div class="turn-meta">
        <span class="badge ${badgeClass}">${badgeLabel}</span>
        ${t.result._limit ? `<span>limit ${t.result._limit}</span>` : ''}
        ${t.result.classes_used.length ? `<span>${t.result.classes_used.join(', ')}</span>` : ''}
      </div>`;
    el.appendChild(div);
  });
  el.scrollTop = el.scrollHeight;
}

// ── Show turn results ──────────────────────────────────────────────────────────

function showTurn(idx) {
  activeTurn = idx;
  renderHistory();

  const { question, result } = turns[idx];

  // Accumulate tokens (only on first show — track via dataset)
  const tk = document.getElementById('grid-container');
  if (!tk.dataset['seen_' + idx]) {
    tk.dataset['seen_' + idx] = '1';
    totalPrompt     += result.prompt_tokens     || 0;
    totalCompletion += result.completion_tokens || 0;
    totalCalls      += 1;
    document.getElementById('tok-prompt').textContent     = totalPrompt.toLocaleString();
    document.getElementById('tok-completion').textContent = totalCompletion.toLocaleString();
    document.getElementById('tok-calls').textContent      = totalCalls;
  }

  document.getElementById('results-title').style.color = 'var(--text)';
  document.getElementById('results-title').textContent  = question;

  const badge = document.getElementById('row-count-badge');
  badge.style.display = 'inline-block';
  if (result.error) {
    badge.textContent  = 'Error';
    badge.style.color  = 'var(--error)';
    badge.style.background = 'rgba(248,113,113,0.1)';
  } else {
    badge.textContent  = `${result.row_count} row${result.row_count !== 1 ? 's' : ''}`;
    badge.style.color  = result.row_count === 0 ? 'var(--warn)' : 'var(--success)';
    badge.style.background = result.row_count === 0 ? 'rgba(251,191,36,0.1)' : 'rgba(52,211,153,0.1)';
  }

  const expBar = document.getElementById('explanation-bar');
  if (result.explanation) {
    expBar.style.display = 'block';
    expBar.textContent   = result.explanation;
  } else {
    expBar.style.display = 'none';
  }

  const sparqlSec    = document.getElementById('sparql-section');
  const sparqlToggle = document.getElementById('sparql-toggle');
  const sparqlCode   = document.getElementById('sparql-code');
  const queryLabel   = result._layer && result._layer !== 'graph' ? 'SQL' : 'SPARQL';
  const sparqlLbl    = document.getElementById('sparql-section-label');
  if (sparqlLbl) sparqlLbl.textContent = queryLabel;
  if (result.sparql) {
    sparqlSec.style.display = 'block';
    document.getElementById('sparql-pre').textContent = result.sparql;
    if (result.error) {
      sparqlToggle.classList.add('open');
      sparqlCode.style.display = 'block';
    } else {
      sparqlToggle.classList.remove('open');
      sparqlCode.style.display = 'none';
    }
  } else {
    sparqlSec.style.display = 'none';
  }

  document.getElementById('reset-ctx-btn').style.display = turns.length ? 'inline-block' : 'none';

  const proveBtn = document.getElementById('prove-btn');
  if (proveBtn) {
    proveBtn.style.display = (!result.error && result.run_id) ? 'inline-flex' : 'none';
  }

  const gridEl  = document.getElementById('grid');
  const emptyEl = document.getElementById('empty-state');
  const errorEl = document.getElementById('error-state');

  if (result.error) {
    gridEl.style.display  = 'none';
    emptyEl.style.display = 'none';
    errorEl.style.display = 'block';
    errorEl.innerHTML = `<b style="color:var(--error)">Error</b><br>${escHtml(result.error)}`;
  } else if (!result.rows || result.rows.length === 0) {
    gridEl.style.display  = 'none';
    errorEl.style.display = 'none';
    emptyEl.style.display = 'flex';
    emptyEl.querySelector('p').textContent    = 'No results returned';
    emptyEl.querySelector('.sub').textContent = 'The query ran successfully but matched no data.';
  } else {
    errorEl.style.display = 'none';
    emptyEl.style.display = 'none';
    gridEl.style.display  = 'block';
    initGrid(Object.keys(result.rows[0]), result.rows);
  }
}

// ── SPARQL toggle ──────────────────────────────────────────────────────────────

document.getElementById('sparql-toggle').addEventListener('click', () => {
  const btn  = document.getElementById('sparql-toggle');
  const code = document.getElementById('sparql-code');
  const open = btn.classList.toggle('open');
  code.style.display = open ? 'block' : 'none';
});

document.getElementById('sparql-copy-btn').addEventListener('click', async () => {
  const text = document.getElementById('sparql-pre').textContent;
  if (!text.trim()) return;
  await navigator.clipboard.writeText(text);
  const btn  = document.getElementById('sparql-copy-btn');
  const orig = btn.textContent;
  btn.textContent = 'Copied!';
  setTimeout(() => { btn.textContent = orig; }, 1500);
});

// ── Reset context only (keeps visual history, clears LLM memory) ──────────────

document.getElementById('reset-ctx-btn').addEventListener('click', async () => {
  await fetch('/api/reset', { method: 'POST' });
  const btn  = document.getElementById('reset-ctx-btn');
  const prev = btn.textContent;
  btn.textContent = 'Context cleared';
  btn.style.color = 'var(--success)';
  btn.style.borderColor = 'var(--success)';
  setTimeout(() => {
    btn.textContent = prev;
    btn.style.color = '';
    btn.style.borderColor = '';
  }, 1500);
});

// ── Input handling ─────────────────────────────────────────────────────────────

document.getElementById('ask-btn').addEventListener('click', submitQuery);

document.getElementById('question').addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault();
    submitQuery();
  }
});

// Pick a suggested question from the dropdown → ask it, then reset to placeholder.
document.getElementById('suggestion-select').addEventListener('change', e => {
  const q = e.target.value;
  if (!q) return;
  document.getElementById('question').value = q;
  e.target.selectedIndex = 0;
  submitQuery();
});

async function loadQuerySuggestions() {
  try {
    const res  = await fetch('/api/query/suggestions');
    const data = await res.json();
    const sel    = document.getElementById('suggestion-select');
    const status = document.getElementById('schema-status');
    const placeholder = sel.querySelector('option');   // keep "Try asking…"
    sel.innerHTML = '';
    sel.appendChild(placeholder);
    if (data.suggestions && data.suggestions.length > 0) {
      data.suggestions.forEach(q => {
        const o = document.createElement('option');
        o.value = q;
        o.textContent = q;
        sel.appendChild(o);
      });
      if (status) status.style.display = 'none';
    } else if (status) {
      status.textContent = 'No schema loaded — run the pipeline first.';
      status.style.display = 'block';
    }
  } catch(e) {
    console.warn('Could not load query suggestions', e);
  }
}

// Load suggestions on startup
loadQuerySuggestions();

// ── Helpers ────────────────────────────────────────────────────────────────────

function setLoading(on) {
  document.getElementById('ask-btn').disabled      = on;
  document.getElementById('spinner').style.display = on ? 'block' : 'none';
}

// ── CSV Export ─────────────────────────────────────────────────────────────────

document.getElementById('csv-btn').addEventListener('click', () => {
  if (!_lastRows || _lastRows.length === 0) return;
  const cols  = Object.keys(_lastRows[0]);
  const lines = [
    cols.map(c => JSON.stringify(c)).join(','),
    ..._lastRows.map(row =>
      cols.map(c => {
        const v = row[c];
        return v == null ? '' : JSON.stringify(String(v));
      }).join(',')
    ),
  ];
  const blob = new Blob([lines.join('\r\n')], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = (_lastQuestion.slice(0, 40).replace(/[^a-z0-9]+/gi, '_') || 'results') + '.csv';
  a.click();
  URL.revokeObjectURL(url);
});

// Patch showTurn to track last rows and show/hide the CSV button
const _origShowTurn = showTurn;
showTurn = function(idx) {
  _origShowTurn(idx);
  const { question, result } = turns[idx];
  _lastRows     = (result.rows && result.rows.length > 0) ? result.rows : null;
  _lastQuestion = question;
  document.getElementById('csv-btn').style.display      = _lastRows ? 'inline-block' : 'none';
  document.getElementById('fragment-btn').style.display = _lastRows ? 'inline-block' : 'none';
};

document.getElementById('fragment-btn').addEventListener('click', () => {
  _fragmentMode = !_fragmentMode;
  const btn = document.getElementById('fragment-btn');
  btn.classList.toggle('active', _fragmentMode);
  // Re-render current grid with updated display mode
  if (_lastRows) {
    initGrid(Object.keys(_lastRows[0]), _lastRows);
  }
});
