// ── Boot IIFE ──────────────────────────────────────────────────────────────────

(async () => {
  try {
    const status = await fetch('/api/status').then(r => r.json());
    const modelSel = document.getElementById('model-select');
    if (modelSel && status.available_models) {
      // Rebuild options from the backend so locally-installed models (Ollama)
      // appear alongside the cloud presets. Grouped by backend for clarity.
      const am = status.available_models;
      modelSel.innerHTML = '';
      const addGroup = (label, models) => {
        if (!models || !models.length) return;
        const og = document.createElement('optgroup');
        og.label = label;
        for (const m of models) {
          const o = document.createElement('option');
          o.value = m; o.textContent = m;
          og.appendChild(o);
        }
        modelSel.appendChild(og);
      };
      addGroup('Local', am.local);
      addGroup('OpenAI', am.openai);
      addGroup('Claude', am.claude);
    }
    if (modelSel && status.model) modelSel.value = status.model;
    totalPrompt     = status.prompt_tokens     || 0;
    totalCompletion = status.completion_tokens || 0;
    document.getElementById('tok-prompt').textContent     = totalPrompt.toLocaleString();
    document.getElementById('tok-completion').textContent = totalCompletion.toLocaleString();
    const sel  = document.getElementById('limit-select');
    const opts = [...sel.options].map(o => parseInt(o.value));
    const best = opts.reduce((a, b) => Math.abs(b - status.default_limit) < Math.abs(a - status.default_limit) ? b : a);
    sel.value = best;
    if (status.default_threshold != null) {
      _savedThreshold = status.default_threshold;
      const slider = document.getElementById('threshold-dialog-input');
      if (slider) {
        slider.value = _savedThreshold.toFixed(2);
        const disp = document.getElementById('threshold-dialog-value');
        if (disp) disp.textContent = _savedThreshold.toFixed(2);
      }
    }
  } catch(e) { /* server not ready */ }
  _loadOntologyProps();
  if (typeof loadProjects === 'function') loadProjects();
})();

// ── Model selector ────────────────────────────────────────────────────────────

document.getElementById('model-select').addEventListener('change', async function() {
  try {
    await fetch('/api/model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: this.value }),
    });
  } catch(e) { console.warn('Model switch failed', e); }
});

// ── Token reset ────────────────────────────────────────────────────────────────

document.getElementById('tok-reset-btn').addEventListener('click', async () => {
  await fetch('/api/tokens/reset', { method: 'POST' });
  totalPrompt = 0; totalCompletion = 0; totalCalls = 0;
  document.getElementById('tok-prompt').textContent     = '0';
  document.getElementById('tok-completion').textContent = '0';
  document.getElementById('tok-calls').textContent      = '0';
});

// ── Tab switching ──────────────────────────────────────────────────────────────

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab;
    if (tab === activeTab) return;
    if (_pdState === 'full') _setPdState('partial');
    activeTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    document.getElementById('rawschema-panel').classList.toggle('visible', tab === 'rawschema');
    document.getElementById('mappings-panel').classList.toggle('visible', tab === 'mappings');
    document.getElementById('corrections-panel').classList.toggle('visible', tab === 'corrections');
    document.getElementById('ontology-panel').classList.toggle('visible', tab === 'ontology');
    document.getElementById('quality-panel').classList.toggle('visible', tab === 'quality');
    document.getElementById('proof-panel').classList.toggle('visible', tab === 'proof');
    if (tab === 'rawschema') { loadDataSources(); loadRawSchema(); }
    if (tab === 'mappings') loadMappings();
    if (tab === 'corrections') loadCorrections();
    if (tab === 'ontology' && !_ontologyData) { loadOntology(); loadAvailableOntologyFiles(); }
    else if (tab === 'ontology') { loadOntologySuggestions(); loadAvailableOntologyFiles(); }
    if (tab === 'quality') loadQualityTab();
  });
});
