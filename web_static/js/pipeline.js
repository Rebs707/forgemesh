// ── Pipeline drawer state ─────────────────────────────────────────────────────

function _setPdState(state) {
  _pdState = state;
  try { localStorage.setItem('pd_state', state); } catch(_) {}
  const drawer = document.getElementById('pipeline-drawer');
  drawer.classList.remove('pd-collapsed', 'pd-partial', 'pd-full');
  drawer.classList.add('pd-' + state);
  const chevron = document.getElementById('pd-chevron');
  if (chevron) chevron.style.transform = state === 'collapsed' ? 'rotate(0deg)' : 'rotate(180deg)';
  document.getElementById('pd-btn-partial').classList.toggle('pd-active', state === 'partial');
  document.getElementById('pd-btn-full').classList.toggle('pd-active', state === 'full');
}

document.getElementById('pd-header').addEventListener('click', e => {
  if (e.target.closest('.pd-ctrl-btn')) return;
  _setPdState(_pdState === 'collapsed' ? 'partial' : 'collapsed');
});
document.getElementById('pd-btn-partial').addEventListener('click', e => {
  e.stopPropagation();
  _setPdState(_pdState === 'partial' ? 'collapsed' : 'partial');
});
document.getElementById('pd-btn-full').addEventListener('click', e => {
  e.stopPropagation();
  _setPdState(_pdState === 'full' ? 'partial' : 'full');
});
document.getElementById('pd-btn-collapse').addEventListener('click', e => {
  e.stopPropagation();
  _setPdState('collapsed');
});

// ── Pipeline history ──────────────────────────────────────────────────────────

async function loadPipelineHistory() {
  try {
    const data = await fetch('/api/pipeline/history').then(r => r.json());
    _pipelineHistoryData = data.runs || [];
    renderPipelineHistory();
  } catch(e) { /* ok */ }
}

const _GOLD_MODE_LABELS = { 1: 'compat', 2: 'semantic', 3: 'additive' };

function _annotateRunLabel(run) {
  let label = run.phases || 'all';
  if (label === 'all') {
    const extras = [];
    if (run.threshold != null) extras.push(`map:${run.threshold}`);
    if (run.gold_mode != null) extras.push(`gold:${_GOLD_MODE_LABELS[run.gold_mode] || run.gold_mode}`);
    if (extras.length) label += ` (${extras.join(', ')})`;
    return label;
  }
  if (run.threshold != null) label = label.replace(/\bmap\b/, `map (${run.threshold})`);
  if (run.gold_mode != null) {
    const ml = _GOLD_MODE_LABELS[run.gold_mode] || run.gold_mode;
    label = label.replace(/\bgold\b/, `gold [${ml}]`);
  }
  return label;
}

function renderPipelineHistory() {
  const list = document.getElementById('pipeline-history-list');
  if (_pipelineHistoryData.length === 0) {
    list.innerHTML = '<div style="padding:20px 12px;text-align:center;font-size:11px;color:var(--muted)">No runs yet</div>';
    return;
  }
  list.innerHTML = '';
  for (const run of _pipelineHistoryData) {
    const item = document.createElement('div');
    item.className = 'pipeline-run-item' + (_selectedHistoricRunId === run.run_id ? ' active' : '');
    item.dataset.runId = run.run_id;
    const label = _annotateRunLabel(run);
    const labelShort = label.length > 28 ? label.slice(0, 26) + '…' : label;
    const hash8 = run.ontology_hash ? run.ontology_hash.slice(0, 8) : '';
    item.innerHTML = `
      <div class="pipeline-run-phases">${escHtml(labelShort)}</div>
      <div class="pipeline-run-meta">
        <div class="run-status-dot ${escHtml(run.status)}"></div>
        <span>${_formatRunDate(run.started_at)}</span>
        ${hash8 ? `<span style="font-family:var(--mono);font-size:9px;color:var(--muted);opacity:0.7" title="Ontology hash">${escHtml(hash8)}</span>` : ''}
      </div>`;
    item.title = `${label} · ${run.started_at || ''} · ${run.status}${hash8 ? ' · ont:' + hash8 : ''}`;
    item.addEventListener('click', () => viewHistoricRun(run.run_id));
    list.appendChild(item);
  }
}

/** Show a gold mode selection modal. Calls onConfirm(goldMode) when user confirms. */
function showGoldModeModal(onConfirm) {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center';

  const modes = [
    { value: 1, title: 'Mode 1 — Compatibility', desc: 'Keep bronze column names unchanged. Safe for existing SQL consumers and retrofitting into an existing data warehouse.' },
    { value: 2, title: 'Mode 2 — Semantic',       desc: 'Rename columns to OWL property names (e.g. has_date_of_birth). Best for greenfield systems where the ontology is the primary interface.' },
    { value: 3, title: 'Mode 3 — Additive',        desc: 'Keep bronze names + create gold_semantic_{entity} views with OWL property names. Zero extra storage — the best of both worlds.' },
  ];

  let selected = 1;

  const box = document.createElement('div');
  box.style.cssText = 'background:var(--bg,#1a1a2e);border:1px solid var(--border,#333);border-radius:10px;padding:24px;width:420px;max-width:90vw;color:var(--fg,#e2e8f0);font-family:var(--sans,sans-serif)';

  const title = document.createElement('h3');
  title.textContent = 'Gold Layer Mode';
  title.style.cssText = 'margin:0 0 16px;font-size:15px;font-weight:600';
  box.appendChild(title);

  modes.forEach(m => {
    const label = document.createElement('label');
    label.style.cssText = 'display:flex;gap:10px;padding:10px 12px;border-radius:6px;cursor:pointer;border:1px solid transparent;margin-bottom:8px;transition:border-color 0.15s';

    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'gold-mode';
    radio.value = m.value;
    radio.checked = m.value === 1;
    radio.style.cssText = 'margin-top:3px;flex-shrink:0;accent-color:var(--accent,#4ade80)';

    const textWrap = document.createElement('div');
    const strong = document.createElement('div');
    strong.textContent = m.title;
    strong.style.cssText = 'font-size:13px;font-weight:600;margin-bottom:3px';
    const desc = document.createElement('div');
    desc.textContent = m.desc;
    desc.style.cssText = 'font-size:11px;color:var(--muted,#94a3b8);line-height:1.5';
    textWrap.appendChild(strong);
    textWrap.appendChild(desc);

    label.appendChild(radio);
    label.appendChild(textWrap);

    radio.addEventListener('change', () => {
      selected = m.value;
      box.querySelectorAll('label').forEach(l => l.style.borderColor = 'transparent');
      label.style.borderColor = 'var(--accent,#4ade80)';
    });
    if (m.value === 1) label.style.borderColor = 'var(--accent,#4ade80)';

    box.appendChild(label);
  });

  const btnRow = document.createElement('div');
  btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:8px;margin-top:16px';

  const cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Cancel';
  cancelBtn.style.cssText = 'padding:7px 16px;border-radius:5px;border:1px solid var(--border,#333);background:transparent;color:var(--muted,#94a3b8);cursor:pointer;font-size:13px';
  cancelBtn.onclick = () => document.body.removeChild(overlay);

  const confirmBtn = document.createElement('button');
  confirmBtn.textContent = 'Confirm';
  confirmBtn.style.cssText = 'padding:7px 16px;border-radius:5px;border:none;background:var(--accent,#4ade80);color:#000;cursor:pointer;font-size:13px;font-weight:600';
  confirmBtn.onclick = () => { document.body.removeChild(overlay); onConfirm(selected); };

  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(confirmBtn);
  box.appendChild(btnRow);
  overlay.appendChild(box);
  overlay.addEventListener('click', e => { if (e.target === overlay) document.body.removeChild(overlay); });
  document.body.appendChild(overlay);
}

async function viewHistoricRun(runId) {
  _selectedHistoricRunId = runId;
  renderPipelineHistory();
  const logEl = document.getElementById('pipeline-log');
  logEl.innerHTML = '<div style="padding:20px;color:var(--muted);font-family:var(--mono);font-size:12px">Loading…</div>';
  try {
    const data = await fetch(`/api/pipeline/runs/${runId}/log`).then(r => r.json());
    logEl.innerHTML = '';
    for (const line of data.log_lines) {
      appendPipelineLine(line);
    }
    setPipelineStatus(data.status);
  } catch(e) {
    logEl.innerHTML = `<div style="padding:20px;color:var(--error);font-size:12px">Failed to load run: ${escHtml(String(e))}</div>`;
  }
}

function setPipelineStatus(status) {
  const chip = document.getElementById('pipeline-status-chip');
  const btn  = document.getElementById('pipeline-run-btn');
  chip.style.display = 'inline-block';
  chip.className = `chip-${status}`;
  const labels = { idle: 'Idle', running: 'Running…', done: 'Done', failed: 'Failed' };
  chip.textContent = labels[status] || status;
  if (status === 'running') {
    btn.textContent = '■ Stop';
    btn.style.background = 'none';
    btn.style.border = '1px solid var(--error)';
    btn.style.color = 'var(--error)';
  } else {
    btn.textContent = '▶ Run';
    btn.style.background = '';
    btn.style.border = '';
    btn.style.color = '';
  }
  btn.disabled = false;
  _pipelineRunning = status === 'running';
  const pdChip = document.getElementById('pd-status-chip');
  if (pdChip) {
    pdChip.style.display = status !== 'idle' ? 'inline-block' : 'none';
    pdChip.className = `chip-${status}`;
    pdChip.textContent = labels[status] || status;
  }
  if (status === 'idle') {
    const pdText = document.getElementById('pd-status-text');
    if (pdText && !pdText.dataset.hasLog) pdText.textContent = 'Ready — select phases and click ▶ Run';
  }
  const mapBtn = document.getElementById('start-mapping-btn');
  if (mapBtn) {
    mapBtn.disabled = _pipelineRunning;
    if (!_pipelineRunning && mapBtn.textContent === 'Starting…') mapBtn.textContent = '▶ Start mapping';
  }
}

function appendPipelineLine(text) {
  const logEl  = document.getElementById('pipeline-log');
  const empty  = document.getElementById('pipeline-empty');
  if (empty) empty.style.display = 'none';

  const div = document.createElement('div');
  div.className = 'log-line';
  if (text.startsWith('=') || text.startsWith('---')) div.classList.add('separator');
  else if (/error|exception|traceback|failed/i.test(text)) div.classList.add('error-line');
  else if (/done|success|written|completed/i.test(text))   div.classList.add('success-line');
  div.textContent = text;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;

  const pdText = document.getElementById('pd-status-text');
  if (pdText && text.trim()) { pdText.textContent = text.trim(); pdText.dataset.hasLog = '1'; }
}

function startPipelineStream(cursor) {
  if (_pipelineEventSource) { _pipelineEventSource.close(); _pipelineEventSource = null; }
  const es = new EventSource(`/api/pipeline/stream?cursor=${cursor}`);
  _pipelineEventSource = es;
  es.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.line !== undefined) {
      appendPipelineLine(msg.line);
      _pipelineLogCursor++;
    }
    if (msg.done) {
      setPipelineStatus(msg.status);
      es.close();
      _pipelineEventSource = null;
      loadPipelineHistory();
      loadOntologySuggestions(); // refresh gap analysis after run
      _updatePipelineStateBadge(); // refresh lifecycle state badge (issue #47)
      // Auto-select a newly generated ontology without requiring a page reload.
      if (msg.generated_ontology && typeof loadAvailableOntologyFiles === 'function') {
        loadAvailableOntologyFiles().then(() => {
          if (typeof selectOntologyFromDropdown === 'function') {
            selectOntologyFromDropdown(msg.generated_ontology);
          }
        });
      }
    }
  };
  es.onerror = () => { es.close(); _pipelineEventSource = null; };
}


// Stop logic is now handled by the run button itself (see click handler below).

// ── Threshold dialog ──────────────────────────────────────────────────────────
let _savedThreshold = parseFloat(localStorage.getItem('pipeline_threshold') || '0.90');

(function () {
  const slider  = document.getElementById('threshold-dialog-input');
  const display = document.getElementById('threshold-dialog-value');
  slider.value  = _savedThreshold.toFixed(2);
  display.textContent = _savedThreshold.toFixed(2);
  slider.addEventListener('input', () => {
    display.textContent = parseFloat(slider.value).toFixed(2);
  });
  document.getElementById('threshold-cancel-btn').addEventListener('click', () => {
    document.getElementById('threshold-overlay').style.display = 'none';
  });
  document.getElementById('threshold-overlay').addEventListener('click', e => {
    if (e.target === document.getElementById('threshold-overlay'))
      document.getElementById('threshold-overlay').style.display = 'none';
  });
})();

function _showThresholdDialog(onConfirm) {
  const overlay = document.getElementById('threshold-overlay');
  overlay.style.display = 'flex';
  const confirmBtn = document.getElementById('threshold-confirm-btn');
  const newConfirm = confirmBtn.cloneNode(true);
  confirmBtn.parentNode.replaceChild(newConfirm, confirmBtn);
  newConfirm.addEventListener('click', () => {
    const t = Math.min(1, Math.max(0, parseFloat(document.getElementById('threshold-dialog-input').value)));
    _savedThreshold = t;
    localStorage.setItem('pipeline_threshold', t.toFixed(2));
    overlay.style.display = 'none';
    onConfirm(t);
  });
}

document.getElementById('pipeline-run-btn').addEventListener('click', async () => {
  if (_pipelineRunning) {
    try { await fetch('/api/pipeline/stop', { method: 'POST' }); } catch(e) { /* ignore */ }
    return;
  }
  const phaseSet = window._phaseTrackSelected || [];
  if (!phaseSet.length) return;
  const phases        = phaseSet.join(',');
  const needsGold     = phaseSet.includes('gold');
  const needsGenerator = phaseSet.includes('full');

  const _doRun = async (threshold, goldMode, ontologyGenerator) => {
    setPipelineStatus('running');
    _selectedHistoricRunId = null;
    renderPipelineHistory();
    const logEl = document.getElementById('pipeline-log');
    logEl.innerHTML = '';
    _pipelineLogCursor = 0;
    const ontologyPath = (() => {
      const s = window._lastOntologyStatus;
      if (!s || !s.configured_path) return undefined;
      return s.configured_path;
    })();
    try {
      const body = { phases, threshold, ontology_path: ontologyPath };
      if (goldMode != null) body.gold_mode = goldMode;
      if (ontologyGenerator) body.ontology_generator = ontologyGenerator;
      const r = await fetch('/api/pipeline/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        appendPipelineLine(`ERROR: ${err.detail || r.statusText}`);
        setPipelineStatus('failed');
        return;
      }
      setPipelineStatus('running');
      startPipelineStream(0);
    } catch(e) {
      appendPipelineLine(`ERROR: ${e}`);
      setPipelineStatus('failed');
    }
  };

  const _startRun = (threshold, ontologyGenerator) => {
    if (needsGold) {
      showGoldModeModal((goldMode) => _doRun(threshold, goldMode, ontologyGenerator));
    } else {
      _doRun(threshold, null, ontologyGenerator);
    }
  };

  const _afterGenerator = (ontologyGenerator) => {
    if (phaseSet.includes('map')) {
      _showThresholdDialog((threshold) => _startRun(threshold, ontologyGenerator));
    } else {
      _startRun(_savedThreshold, ontologyGenerator);
    }
  };

  if (needsGenerator) {
    showOntologyStrategyModal(_afterGenerator);
  } else {
    _afterGenerator(null);
  }
});

function runMaterialiseFromBanner() {
  if (_pipelineRunning) return;
  showGoldModeModal(async (goldMode) => {
    const btn      = document.getElementById('banner-rerun-btn');
    const statusEl = document.getElementById('banner-run-status');
    btn.disabled = true;
    statusEl.textContent = 'Running…';
    statusEl.style.color = 'var(--warn)';
    try {
      const r = await fetch('/api/pipeline/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ phases: 'silver,gold,rdf', threshold: _savedThreshold, gold_mode: goldMode }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        statusEl.textContent = `Failed: ${err.detail || r.statusText}`;
        statusEl.style.color = 'var(--error, #f87171)';
        btn.disabled = false;
        return;
      }
      setPipelineStatus('running');
      if (_bannerEventSource) _bannerEventSource.close();
      _bannerEventSource = new EventSource('/api/pipeline/stream?cursor=0');
      _bannerEventSource.addEventListener('done', () => {
        _bannerEventSource.close(); _bannerEventSource = null;
        statusEl.textContent = 'Done ✓';
        statusEl.style.color = '#4ade80';
        btn.disabled = false;
        setTimeout(() => { statusEl.textContent = ''; }, 4000);
        loadPipelineHistory();
        loadOntologySuggestions();
        loadQuerySuggestions();
      });
      _bannerEventSource.addEventListener('error', () => {
        _bannerEventSource.close(); _bannerEventSource = null;
        statusEl.textContent = 'Failed ✗';
        statusEl.style.color = 'var(--error, #f87171)';
        btn.disabled = false;
      });
    } catch(e) {
      statusEl.textContent = `Error: ${e}`;
      statusEl.style.color = 'var(--error, #f87171)';
      btn.disabled = false;
    }
  });
}

document.getElementById('pipeline-clear-btn').addEventListener('click', () => {
  const logEl = document.getElementById('pipeline-log');
  logEl.innerHTML = `<div id="pipeline-empty" style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--muted);padding:60px 0;">
    <div class="icon">⚙</div>
    <p style="font-size:14px">Pipeline output will appear here</p>
    <span style="font-size:12px">Select phases and click Run to start</span>
  </div>`;
  document.getElementById('pipeline-status-chip').style.display = 'none';
  _pipelineLogCursor = 0;
});

document.getElementById('pipeline-copy-btn').addEventListener('click', async () => {
  const logEl = document.getElementById('pipeline-log');
  const text = Array.from(logEl.querySelectorAll('.log-line'))
    .map(el => el.textContent)
    .join('\n');
  if (!text.trim()) return;
  await navigator.clipboard.writeText(text);
  const btn = document.getElementById('pipeline-copy-btn');
  const orig = btn.textContent;
  btn.textContent = 'Copied!';
  setTimeout(() => { btn.textContent = orig; }, 1500);
});

// ── Pipeline lifecycle state badge (issue #47) ────────────────────────────────

async function _updatePipelineStateBadge() {
  try {
    const data = await fetch('/api/pipeline/state').then(r => r.json());
    const chip = document.getElementById('pd-state-chip');
    if (!chip) return;
    chip.textContent = data.label || data.state;
    chip.className = '';
    chip.classList.add('pd-state-' + (data.level || 'info'));
    chip.style.display = 'inline-block';
    // Surface a warning banner when action is needed
    const pdText = document.getElementById('pd-status-text');
    if (pdText && !pdText.dataset.hasLog) {
      if (data.level === 'warn') {
        pdText.textContent = `⚠ ${data.label} — re-run the pipeline to resolve`;
        pdText.style.color = 'var(--warn)';
      } else {
        pdText.style.color = '';
      }
    }
  } catch(e) { /* ok */ }
}

function _pipelinePollOnEnter() {
  loadPipelineHistory();
  _updatePipelineStateBadge();
  fetch('/api/pipeline/status').then(r => r.json()).then(data => {
    if (data.status !== 'idle') {
      setPipelineStatus(data.status);
      if (data.status === 'running' && !_pipelineEventSource) {
        _selectedHistoricRunId = null;
        startPipelineStream(_pipelineLogCursor);
      }
    }
  }).catch(() => {});
}

// Apply saved drawer state and start pipeline polling on page load
_setPdState(_pdState);
_pipelinePollOnEnter();

// ── Pipeline phase track selector ─────────────────────────────────────────────

(function () {
  const PHASES = [
    { id: 'inspect',  label: 'inspect' },
    { id: 'full',     label: 'ontology', llmCost: true },
    { id: 'map',      label: 'map',      llmCost: true },
    { id: 'silver',   label: 'silver' },
    { id: 'gold',     label: 'gold' },
    { id: 'rdf',      label: 'graph' },
  ];
  const RAIL_W = 54; // px between nodes

  let _anchorIdx = null;
  let _dragging  = false;
  let _selSet    = new Set();

  function _indicesInRange(a, b) {
    const lo = Math.min(a, b), hi = Math.max(a, b);
    return Array.from({ length: hi - lo + 1 }, (_, k) => lo + k);
  }

  // Update CSS classes in-place — no DOM rebuild, no spurious event re-fires.
  function _applyClasses() {
    const track = document.getElementById('phase-track');
    if (!track) return;
    track.querySelectorAll('.pt-node-wrap').forEach(wrap => {
      const i = parseInt(wrap.dataset.idx, 10);
      wrap.classList.toggle('sel',    _selSet.has(i));
      wrap.classList.toggle('anchor', i === _anchorIdx);
    });
    track.querySelectorAll('.pt-segment').forEach(seg => {
      const i = parseInt(seg.dataset.segIdx, 10);
      seg.classList.toggle('sel', _selSet.has(i) && _selSet.has(i + 1));
    });
  }

  function _commit(indices) {
    _selSet = new Set(indices);
    window._phaseTrackSelected = [..._selSet].sort((a, b) => a - b).map(i => PHASES[i].id);
    _applyClasses();
    const btn = document.getElementById('pipeline-run-btn');
    if (btn) btn.disabled = _selSet.size === 0;
    const hint = document.getElementById('phase-track-hint');
    if (hint) {
      hint.textContent = _selSet.size
        ? window._phaseTrackSelected.join(' → ')
        : 'click or drag to select';
    }
  }

  // Build DOM once; all updates via _applyClasses() thereafter.
  function _build() {
    const track = document.getElementById('phase-track');
    if (!track) return;
    track.innerHTML = '';

    PHASES.forEach((phase, i) => {
      if (i > 0) {
        const seg = document.createElement('div');
        seg.className = 'pt-segment';
        seg.dataset.segIdx = i - 1;
        const rail = document.createElement('div');
        rail.className = 'pt-rail';
        rail.style.width = RAIL_W + 'px';
        seg.appendChild(rail);
        track.appendChild(seg);
      }

      const wrap = document.createElement('div');
      wrap.className = 'pt-node-wrap';
      wrap.dataset.idx = i;

      const node = document.createElement('div');
      node.className = 'pt-node';
      const lbl = document.createElement('div');
      lbl.className = 'pt-label';
      lbl.textContent = phase.label;
      wrap.appendChild(node);
      wrap.appendChild(lbl);
      if (phase.llmCost) {
        const badge = document.createElement('span');
        badge.className = 'pt-cost-badge';
        badge.textContent = '$';
        wrap.appendChild(badge);
      }
      track.appendChild(wrap);

      wrap.addEventListener('mousedown', e => {
        e.preventDefault();
        _dragging = true;
        _anchorIdx = i;
        _commit([i]);
      });

      wrap.addEventListener('mouseenter', () => {
        if (!_dragging || _anchorIdx === null) return;
        _commit(_indicesInRange(_anchorIdx, i));
      });
    });
  }

  document.addEventListener('mouseup', () => { _dragging = false; });

  _build();
  _commit([]);
})();
