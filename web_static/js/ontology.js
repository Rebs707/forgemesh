// ── Ontology tab ───────────────────────────────────────────────────────────────

const _DISMISSED_KEY        = 'ont_dismissed_suggestions';
const _APPLIED_KEY          = 'ont_applied_suggestions';
const _SUGGESTIONS_ON_KEY   = 'ont_suggestions_enabled';

function _getDismissed() {
  try { return new Set(JSON.parse(localStorage.getItem(_DISMISSED_KEY) || '[]')); }
  catch(e) { return new Set(); }
}
function _dismiss(id) {
  const s = _getDismissed();
  s.add(id);
  localStorage.setItem(_DISMISSED_KEY, JSON.stringify([...s]));
}
function _clearDismissed() {
  localStorage.removeItem(_DISMISSED_KEY);
}

function _getApplied() {
  try { return new Set(JSON.parse(localStorage.getItem(_APPLIED_KEY) || '[]')); }
  catch(e) { return new Set(); }
}
function _addApplied(...ids) {
  const s = _getApplied();
  for (const id of ids) s.add(id);
  localStorage.setItem(_APPLIED_KEY, JSON.stringify([...s]));
}
function _clearApplied() {
  localStorage.removeItem(_APPLIED_KEY);
}

async function loadOntology() {
  document.getElementById('ontology-classes-list').innerHTML =
    '<div style="padding:20px 12px;text-align:center;font-size:11px;color:var(--muted)">Loading…</div>';
  try {
    const data = await fetch('/api/ontology').then(r => r.json());
    _ontologyData = data;
    _updateOntologyStatus(data.status);
    renderOntologyClasses();
  } catch(e) {
    document.getElementById('ontology-classes-list').innerHTML =
      `<div style="padding:16px;color:var(--error);font-size:11px">Failed: ${escHtml(String(e))}</div>`;
  }
  loadOntologySuggestions();
}

function _updateOntologyStatus(status) {
  if (!status) return;
  window._lastOntologyStatus = status;

  const statusLine = document.getElementById('ontology-status-line');
  if (statusLine) {
    if (!status.configured_path) {
      statusLine.innerHTML = `<span style="color:var(--error)">⚠ No ontology configured.</span> Set <code>ONTOLOGY_PATH</code> in your <code>.env</code> file to an OWL file or a Protégé catalog XML, then reload.`;
    } else if (!status.path_exists) {
      statusLine.innerHTML = `<span style="color:var(--error)">⚠ File not found:</span> <code>${escHtml(status.configured_path)}</code><br>Check your <code>ONTOLOGY_PATH</code> setting or add a file below.`;
    } else {
      const fileList = (status.files || []).map(f => `<code>${escHtml(f.filename)}</code>`).join(', ');
      statusLine.innerHTML = `<span style="color:var(--success)">✓ Loaded</span> from <code>${escHtml(status.configured_path)}</code>${fileList ? ` — ${fileList}` : ''}`;
    }
  }

  const wrkSel = document.getElementById('ontology-file-select');
  if (wrkSel && status.configured_path) {
    const active         = status.configured_path;
    const activeFilename = active.split('/').pop().split('\\').pop();
    for (const opt of wrkSel.options) {
      if (opt.value === activeFilename || opt.value === active) {
        opt.selected = true;
        break;
      }
    }
    _updateHeaderOntology(activeFilename || active);
  }

  const badge    = document.getElementById('ont-write-target-badge');
  const nameSpan = document.getElementById('ont-write-target-name');
  if (badge && nameSpan) {
    const wt = status.active_write_target;
    if (wt) {
      nameSpan.textContent = wt;
      badge.classList.add('active');
    } else {
      badge.classList.remove('active');
    }
  }
}

// ── Suggestions panel collapse/expand (#33) ───────────────────────────────────
(function() {
  const _SP_KEY = 'sp_collapsed';
  function _applySpState(collapsed) {
    const panel = document.getElementById('ontology-suggestions-panel');
    const btn   = document.getElementById('sp-toggle-btn');
    if (!panel) return;
    panel.classList.toggle('sp-collapsed', collapsed);
    if (btn) btn.textContent = collapsed ? '◀' : '▶';
    try { sessionStorage.setItem(_SP_KEY, collapsed ? '1' : '0'); } catch(_) {}
  }
  document.getElementById('sp-toggle-btn').addEventListener('click', () => {
    const collapsed = !document.getElementById('ontology-suggestions-panel').classList.contains('sp-collapsed');
    _applySpState(collapsed);
  });
  const _storedSp = (() => { try { return sessionStorage.getItem(_SP_KEY); } catch(_) { return null; } })();
  if (_storedSp === '1') {
    _applySpState(true);
  } else {
    // Panel starts expanded — ensure button shows correct direction
    const btn = document.getElementById('sp-toggle-btn');
    if (btn) btn.textContent = '▶';
  }
})();

// On page load: restore server-side state (persisted DB + ontology), then load UI data.
(async () => {
  try {
    const r = await fetch('/api/ui-reset', { method: 'POST' });
    if (r.ok) {
      const d = await r.json();
      if (d.ontology_status) _updateOntologyStatus(d.ontology_status);
    }
  } catch(_) { /* server not yet ready — silently ignore */ }
  loadAvailableOntologyFiles();
  loadDataSources();
})();

// Tracks which tree nodes are collapsed (by URI). Seeded on first render = all collapsed.
const _collapsedNodes = new Set();
let _collapsedInitialised = false;

function _makeClassItem(cls, depth, childMap, q) {
  const local      = cls.uri.includes('#') ? cls.uri.split('#').pop() : cls.uri.split('/').pop();
  const instCount  = (cls.instances || []).length;
  const propLabel  = `${cls.properties.length} prop${cls.properties.length !== 1 ? 's' : ''}`;
  const instLabel  = instCount ? ` · ${instCount} instance${instCount !== 1 ? 's' : ''}` : '';
  const children   = childMap[cls.uri] || [];
  const hasChildren = children.length > 0;
  const collapsed  = _collapsedNodes.has(cls.uri);

  const wrapper = document.createElement('div');

  const item = document.createElement('div');
  item.className = 'ontology-class-item' + (_selectedClassUri === cls.uri ? ' active' : '');
  item.dataset.uri = cls.uri;
  item.style.paddingLeft = `${12 + depth * 16}px`;

  const toggleHtml = hasChildren
    ? `<span class="ont-tree-toggle">${collapsed ? '▶' : '▼'}</span>`
    : `<span class="ont-tree-toggle" style="opacity:0">▼</span>`;

  item.innerHTML = `
    <div style="display:flex;align-items:center;gap:0">
      ${toggleHtml}
      <div style="flex:1;min-width:0">
        <div class="ontology-class-label">${escHtml(cls.label || local)}</div>
        <div class="ontology-class-meta">
          <span class="ont-prop-count" title="${escHtml(cls.source_file || '')}">${cls.properties.length + instCount}</span>
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:11px">${escHtml(propLabel)}${escHtml(instLabel)}</span>
        </div>
      </div>
    </div>`;

  // Toggle expand/collapse on the arrow; select class on the rest
  const toggleEl = item.querySelector('.ont-tree-toggle');
  if (hasChildren && toggleEl) {
    toggleEl.addEventListener('click', e => {
      e.stopPropagation();
      if (_collapsedNodes.has(cls.uri)) _collapsedNodes.delete(cls.uri);
      else _collapsedNodes.add(cls.uri);
      renderOntologyClasses();
    });
  }
  item.addEventListener('click', () => selectClass(cls.uri));
  wrapper.appendChild(item);

  // Render children recursively
  if (hasChildren && !collapsed) {
    const childContainer = document.createElement('div');
    for (const child of children) {
      const matchChild = !q || child.label.toLowerCase().includes(q) || child.uri.toLowerCase().includes(q)
        || child.properties.some(p => p.label.toLowerCase().includes(q) || p.uri.toLowerCase().includes(q));
      if (!q || matchChild) {
        childContainer.appendChild(_makeClassItem(child, depth + 1, childMap, q));
      }
    }
    wrapper.appendChild(childContainer);
  }

  return wrapper;
}

function renderOntologyClasses() {
  const list      = document.getElementById('ontology-classes-list');
  const countEl   = document.getElementById('ontology-class-count');
  const orphanBtn = document.getElementById('ontology-orphan-btn');
  if (!_ontologyData) return;

  const { classes, orphan_properties } = _ontologyData;
  const q = (document.getElementById('ontology-search').value || '').toLowerCase();

  // Split: categorical = has instances; regular = no instances
  const regular     = classes.filter(c => (c.instances || []).length === 0);
  const categorical = classes.filter(c => (c.instances || []).length > 0);

  // Build child map for regular classes only (categoricals don't have subclasses)
  const uriSet   = new Set(regular.map(c => c.uri));
  const childMap = {};
  const roots    = [];
  for (const cls of regular) {
    const parents = cls.parent_uris.filter(p => uriSet.has(p));
    if (parents.length === 0) {
      roots.push(cls);
    } else {
      for (const p of parents) {
        childMap[p] = childMap[p] || [];
        if (!childMap[p].find(x => x.uri === cls.uri)) childMap[p].push(cls);
      }
    }
  }

  // Seed collapsed set on first render so all nodes start collapsed.
  if (!_collapsedInitialised) {
    for (const uri of Object.keys(childMap)) _collapsedNodes.add(uri);
    _collapsedInitialised = true;
  }

  list.innerHTML = '';
  let shown = 0;

  const matchesFilter = cls =>
    !q ||
    cls.label.toLowerCase().includes(q) ||
    cls.uri.toLowerCase().includes(q) ||
    cls.properties.some(p => p.label.toLowerCase().includes(q) || p.uri.toLowerCase().includes(q));

  // ── Regular class hierarchy ──────────────────────────────────────────────
  if (regular.length > 0) {
    const header = document.createElement('div');
    header.className = 'ont-class-section-header';
    header.textContent = `Classes (${regular.length})`;
    list.appendChild(header);

    if (q) {
      // When filtering, flatten — show all matching regardless of hierarchy
      for (const cls of regular) {
        if (!matchesFilter(cls)) continue;
        shown++;
        list.appendChild(_makeClassItem(cls, 0, {}, q));
      }
    } else {
      // No filter — render tree from roots
      for (const cls of roots) {
        shown++;
        list.appendChild(_makeClassItem(cls, 0, childMap, q));
      }
      // Count descendants too
      shown = regular.length;
    }
  }

  // ── Categorical classes ──────────────────────────────────────────────────
  const visibleCats = categorical.filter(matchesFilter);
  if (visibleCats.length > 0) {
    const header = document.createElement('div');
    header.className = 'ont-class-section-header';
    header.textContent = `Categorical (${categorical.length})`;
    list.appendChild(header);

    for (const cls of visibleCats) {
      shown++;
      list.appendChild(_makeClassItem(cls, 0, {}, q));
    }
  }

  if (shown === 0) {
    const status = _ontologyData && _ontologyData.status;
    if (!status || !status.loaded) {
      const pathMsg = status && status.configured_path
        ? (status.path_exists
            ? `Configured path <code>${escHtml(status.configured_path)}</code> loaded no classes.`
            : `Configured path <code>${escHtml(status.configured_path)}</code> was not found.`)
        : 'No <code>ONTOLOGY_PATH</code> is configured.';
      list.innerHTML = `
        <div style="padding:28px 20px;text-align:center;line-height:1.8;font-size:11px;color:var(--muted)">
          <div style="font-size:28px;opacity:0.25;margin-bottom:10px">⬡</div>
          <div style="margin-bottom:10px">${pathMsg}</div>
          <div style="display:flex;flex-direction:column;gap:6px;align-items:center">
            <button onclick="document.getElementById('ontology-file-input').click()" style="font-size:11px;padding:4px 14px;border-radius:5px;border:1px solid var(--border);background:none;color:var(--text);cursor:pointer;">
              + Load an OWL file
            </button>
            <span style="font-size:10px;color:var(--muted)">or</span>
            <button onclick="document.querySelectorAll('.tab-btn[data-tab=pipeline]')[0].click();document.getElementById('pipeline-phase-select').value='inspect,seed'" style="font-size:11px;padding:4px 14px;border-radius:5px;border:1px solid var(--border);background:none;color:var(--text);cursor:pointer;">
              Generate seed ontology from data →
            </button>
          </div>
        </div>`;
    } else {
      list.innerHTML = '<div style="padding:16px;text-align:center;font-size:11px;color:var(--muted)">No classes match</div>';
    }
  }

  countEl.textContent = `${regular.length + categorical.length} class${regular.length + categorical.length !== 1 ? 'es' : ''}`;
  orphanBtn.style.display = orphan_properties.length > 0 ? 'inline' : 'none';
  orphanBtn.textContent   = `${orphan_properties.length} abstract`;
  _refreshClassDatalist(classes);
}

function _refreshClassDatalist(classes) {
  const dl = document.getElementById('ont-class-datalist');
  if (!dl) return;
  dl.innerHTML = (classes || []).map(cls => {
    const local = cls.uri.includes('#') ? cls.uri.split('#').pop() : cls.uri.split('/').pop();
    const label = cls.label || local;
    return `<option value="${escHtml(local)}">${escHtml(label !== local ? label + ' — ' + local : local)}</option>`;
  }).join('');
}

function selectClass(uri) {
  _selectedClassUri = uri;
  document.querySelectorAll('.ontology-class-item').forEach(el => {
    el.classList.toggle('active', el.dataset.uri === uri);
  });
  const cls = _ontologyData.classes.find(c => c.uri === uri);
  if (!cls) return;
  renderClassDetail(cls);
}

function renderClassDetail(cls) {
  const panel = document.getElementById('ontology-detail-panel');
  const q     = (document.getElementById('ontology-search').value || '').toLowerCase();
  const local = cls.uri.includes('#') ? cls.uri.split('#').pop() : cls.uri.split('/').pop();

  const parentsHtml = cls.parent_uris.length
    ? `<div class="ont-parents">Extends:
        ${cls.parent_uris.map(pUri => {
          const pLocal = pUri.includes('#') ? pUri.split('#').pop() : pUri.split('/').pop();
          const exists = _ontologyData.classes.some(c => c.uri === pUri);
          return exists
            ? `<span class="ont-parent-link" data-uri="${escHtml(pUri)}">${escHtml(pLocal)}</span>`
            : `<span style="font-family:var(--mono);font-size:11px">${escHtml(pLocal)}</span>`;
        }).join('<span style="color:var(--muted)"> · </span>')}
      </div>`
    : '';

  const propsHtml = cls.properties.length === 0
    ? '<div style="font-size:12px;color:var(--muted);padding:8px 0">No properties with this class as domain</div>'
    : `<table class="ont-prop-table">
        <thead><tr>
          <th>Name</th><th>Type</th><th>Range</th><th>Label / Comment</th><th>Source</th>
        </tr></thead>
        <tbody>
          ${cls.properties.map(p => {
            const pLocal  = p.uri.includes('#') ? p.uri.split('#').pop() : p.uri.split('/').pop();
            const hidden  = q && !p.label.toLowerCase().includes(q) && !p.uri.toLowerCase().includes(q) ? ' hidden' : '';
            const rangeHtml = p.range_uris.map(r => {
              const rLocal  = r.includes('#') ? r.split('#').pop() : r.split('/').pop();
              const isClass = _ontologyData.classes.some(c => c.uri === r);
              return isClass
                ? `<span class="ont-range-link" data-uri="${escHtml(r)}">${escHtml(rLocal)}</span>`
                : `<span style="font-family:var(--mono);font-size:10px;color:var(--muted)">${escHtml(rLocal)}</span>`;
            }).join(', ') || '<span style="color:var(--muted)">—</span>';
            const pSrcFile = p.source_file ? p.source_file.replace(/^.*[\\/]/, '') : '';
            const srcBadge = pSrcFile
              ? `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(136,192,208,0.15);color:var(--accent)" title="${escHtml(p.source_file)}">${escHtml(pSrcFile)}</span>`
              : '<span style="color:var(--muted)">—</span>';
            const editBtn = p.type === 'datatype'
              ? `<button class="btn-edit-prop" data-uri="${escHtml(p.uri)}" title="Edit property" style="font-size:10px;padding:1px 5px;margin-left:4px;background:rgba(136,192,208,0.1);color:var(--accent);border:1px solid rgba(136,192,208,0.3);border-radius:3px;cursor:pointer">✎</button>` +
                `<button class="btn-del-prop" data-uri="${escHtml(p.uri)}" data-label="${escHtml(p.label || pLocal)}" title="Delete property" style="font-size:10px;padding:1px 5px;margin-left:2px;background:rgba(248,113,113,0.08);color:var(--error);border:1px solid rgba(248,113,113,0.25);border-radius:3px;cursor:pointer">×</button>`
              : '';
            return `<tr class="${hidden}" data-prop-uri="${escHtml(p.uri)}" data-prop-type="${p.type}">
              <td><span class="ont-prop-name">${escHtml(pLocal)}</span></td>
              <td><span class="ont-prop-type-chip ${p.type}">${p.type}</span></td>
              <td>${rangeHtml}</td>
              <td class="ont-prop-comment">${escHtml(p.label || '')}${p.label && p.comment ? ' — ' : ''}${escHtml(p.comment || '')}</td>
              <td style="white-space:nowrap">${srcBadge}${editBtn}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>`;

  const instances = cls.instances || [];
  const instancesHtml = instances.length === 0 ? '' : `
    <div class="ont-section-label" style="margin-top:12px">Instances (${instances.length})</div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:4px">
      ${instances.map(ind => {
        const tooltip = ind.comment ? ` title="${escHtml(ind.comment)}"` : '';
        return `<span${tooltip} style="font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:12px;background:rgba(136,192,208,0.12);color:var(--accent);border:1px solid rgba(136,192,208,0.25)">${escHtml(ind.label)}</span>`;
      }).join('')}
    </div>`;

  const clsSrcFile = cls.source_file ? cls.source_file.replace(/^.*[\\/]/, '') : '';
  panel.innerHTML = `
    <div class="ont-class-title" style="display:flex;align-items:center;gap:8px;">${escHtml(cls.label || local)}${clsSrcFile ? `<span style="font-size:10px;padding:1px 6px;border-radius:3px;background:rgba(136,192,208,0.15);color:var(--accent)" title="${escHtml(cls.source_file)}">${escHtml(clsSrcFile)}</span>` : ''}</div>
    <div class="ont-class-uri">${escHtml(cls.uri)}</div>
    ${cls.comment ? `<div class="ont-class-comment">${escHtml(cls.comment)}</div>` : ''}
    ${parentsHtml}
    <div class="ont-section-label">Properties (${cls.properties.length})</div>
    ${propsHtml}
    ${instancesHtml}`;

  panel.querySelectorAll('.ont-parent-link').forEach(el => {
    el.addEventListener('click', () => selectClass(el.dataset.uri));
  });
  panel.querySelectorAll('.ont-range-link').forEach(el => {
    el.addEventListener('click', () => selectClass(el.dataset.uri));
  });
  panel.querySelectorAll('.btn-edit-prop').forEach(btn => {
    btn.addEventListener('click', () => {
      const tr      = btn.closest('tr');
      const propUri = btn.dataset.uri;
      const prop    = cls.properties.find(p => p.uri === propUri);
      if (!prop) return;
      enterPropEditMode(tr, prop, cls);
    });
  });
  panel.querySelectorAll('.btn-del-prop').forEach(btn => {
    btn.addEventListener('click', async () => {
      const label = btn.dataset.label || btn.dataset.uri;
      if (!confirm(`Delete property "${label}" from the ontology? This cannot be undone.`)) return;
      btn.disabled = true;
      btn.textContent = '…';
      try {
        const r = await fetch('/api/ontology/property/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ uri: btn.dataset.uri }),
        });
        const data = await r.json();
        if (!r.ok) { alert('Delete failed: ' + (data.detail || r.statusText)); btn.disabled = false; btn.textContent = '×'; return; }
        _ontologyData = null;
        await loadOntology();
        renderOntologyClasses();
        await loadOntologyHistory();
      } catch(e) {
        alert('Delete failed: ' + e);
        btn.disabled = false;
        btn.textContent = '×';
      }
    });
  });
}

function enterPropEditMode(tr, prop, cls) {
  const pLocal           = prop.uri.includes('#') ? prop.uri.split('#').pop() : prop.uri.split('/').pop();
  const currentRangeLocal = prop.range_uris.length
    ? (prop.range_uris[0].includes('#') ? prop.range_uris[0].split('#').pop() : prop.range_uris[0].split('/').pop())
    : 'string';
  const XSD_TYPES = ['string','integer','decimal','float','double','boolean','date','dateTime','anyURI'];
  const rangeOpts = XSD_TYPES.map(t =>
    `<option value="${t}" ${t === currentRangeLocal ? 'selected' : ''}>${t}</option>`
  ).join('');

  tr.innerHTML = `<td colspan="5" style="padding:6px 4px">
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
      <span style="font-size:10px;color:var(--muted)">Name</span>
      <input class="pef-name" value="${escHtml(pLocal)}" spellcheck="false"
        style="width:130px;font-family:var(--mono);font-size:11px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:2px 5px">
      <span style="font-size:10px;color:var(--muted)">Range</span>
      <select class="pef-range" style="font-size:11px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:2px 4px">${rangeOpts}</select>
      <span style="font-size:10px;color:var(--muted)">Label</span>
      <input class="pef-label" value="${escHtml(prop.label || '')}" spellcheck="false"
        style="flex:1;min-width:100px;font-size:11px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:2px 5px">
      <button class="pef-save" style="font-size:11px;padding:2px 8px;background:rgba(52,211,153,0.12);color:var(--success);border:1px solid rgba(52,211,153,0.35);border-radius:3px;cursor:pointer">Save</button>
      <button class="pef-cancel" style="font-size:11px;padding:2px 8px;background:rgba(156,163,175,0.1);color:var(--muted);border:1px solid var(--border);border-radius:3px;cursor:pointer">Cancel</button>
    </div>
  </td>`;

  tr.querySelector('.pef-cancel').addEventListener('click', () => renderClassDetail(cls));
  tr.querySelector('.pef-save').addEventListener('click', async () => {
    const newName  = tr.querySelector('.pef-name').value.trim() || pLocal;
    const newRange = tr.querySelector('.pef-range').value;
    const newLabel = tr.querySelector('.pef-label').value.trim();
    const saveBtn  = tr.querySelector('.pef-save');
    saveBtn.textContent = 'Saving…'; saveBtn.disabled = true;
    try {
      const r = await fetch('/api/ontology/property/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_uri: prop.uri,
          new_local_name: newName !== pLocal ? newName : null,
          new_label: newLabel,
          new_range_uri: 'http://www.w3.org/2001/XMLSchema#' + newRange,
        }),
      });
      if (!r.ok) {
        const d = await r.json();
        alert('Save failed: ' + (d.detail || r.statusText));
        saveBtn.textContent = 'Save'; saveBtn.disabled = false;
        return;
      }
      _ontologyData = null;
      await loadOntology();
      await loadOntologyHistory();
      document.getElementById('ontology-rerun-banner').style.display = 'flex';
      if (_ontologyData) {
        const fresh = _ontologyData.classes.find(c => c.uri === cls.uri);
        if (fresh) { _selectedClassUri = fresh.uri; renderClassDetail(fresh); }
      }
    } catch(e) {
      alert('Save failed: ' + e);
      saveBtn.textContent = 'Save'; saveBtn.disabled = false;
    }
  });
}

function _isSuggestionsEnabled() {
  try { return localStorage.getItem(_SUGGESTIONS_ON_KEY) === '1'; } catch(_) { return false; }
}
function _setSuggestionsEnabled(on) {
  try { localStorage.setItem(_SUGGESTIONS_ON_KEY, on ? '1' : '0'); } catch(_) {}
}

// Wire toggle on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.getElementById('suggestions-enabled-toggle');
  if (!toggle) return;
  toggle.checked = _isSuggestionsEnabled();
  toggle.addEventListener('change', () => {
    _setSuggestionsEnabled(toggle.checked);
    const rebuildBtn = document.getElementById('suggestions-rebuild-btn');
    if (rebuildBtn) rebuildBtn.style.display = toggle.checked ? '' : 'none';
  });
  // Sync rebuild-btn visibility on load
  const rebuildBtn = document.getElementById('suggestions-rebuild-btn');
  if (rebuildBtn) rebuildBtn.style.display = _isSuggestionsEnabled() ? '' : 'none';
});

async function loadOntologySuggestions() {
  if (!_isSuggestionsEnabled()) return;
  try {
    const data = await fetch('/api/ontology/suggestions').then(r => r.json());
    _suggestionsData = data.suggestions || [];
    renderSuggestions();
  } catch(e) { /* ok */ }
}

async function rebuildSuggestions() {
  const btn = document.getElementById('suggestions-rebuild-btn');
  if (btn.disabled || _pipelineRunning) return;
  btn.disabled = true;
  btn.textContent = 'Building…';
  btn.style.opacity = '0.5';
  setPipelineStatus('running');

  document.getElementById('pipeline-log').innerHTML = '';
  _setPdState('partial');
  appendPipelineLine('=== Rebuilding ontology gap suggestions ===');
  appendPipelineLine('Analysing inspection report and mapping data…');

  try {
    const data = await fetch('/api/ontology/suggestions?rebuild=true').then(r => r.json());
    _suggestionsData = data.suggestions || [];
    const applied     = _getApplied();
    const dismissed   = _getDismissed();
    const total       = _suggestionsData.length;
    const visible     = _suggestionsData.filter(s => !dismissed.has(s.id) && !applied.has(s.id)).length;
    const hiddenCount = total - visible;
    appendPipelineLine('LLM enrichment complete.');
    appendPipelineLine(`--- Done: ${total} suggestion${total !== 1 ? 's' : ''} generated` +
      (hiddenCount > 0 ? `, ${visible} shown (${hiddenCount} hidden: applied or dismissed)` : '') + ` ---`);
    if (data.prompt_tokens || data.completion_tokens) {
      const costStr = data.cost_usd != null ? ` ($${data.cost_usd.toFixed(4)})` : '';
      appendPipelineLine(
        `    LLM cost: gpt-4o-mini  ${(data.prompt_tokens||0).toLocaleString()} in / ` +
        `${(data.completion_tokens||0).toLocaleString()} out${costStr}`
      );
    }
    renderSuggestions();
    setPipelineStatus('done');
  } catch(e) {
    appendPipelineLine(`Error: ${e}`);
    setPipelineStatus('failed');
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ Rebuild';
    btn.style.opacity = '';
  }
}

function renderSuggestions() {
  const dismissed = _getDismissed();
  const applied   = _getApplied();
  _suggestionsData = _suggestionsData.filter(s => !applied.has(s.id));
  const visible   = _suggestionsData.filter(s => !dismissed.has(s.id));

  const badge   = document.getElementById('suggestions-tab-badge');
  const panel   = document.getElementById('ontology-suggestions-panel');
  const countEl = document.getElementById('ontology-suggestions-count');
  const list    = document.getElementById('ontology-suggestions-list');

  if (visible.length === 0 && _suggestionsData.length === 0) {
    badge.style.display = 'none';
    countEl.textContent = '';
    const offMsg = !_isSuggestionsEnabled()
      ? 'Enable the Auto-suggest toggle above, then click ↻ Rebuild to run gap analysis.'
      : 'Run the mapping pipeline to generate ontology gap analysis.';
    list.innerHTML = `<div style="padding:32px 16px;text-align:center;color:var(--muted);font-size:11px;line-height:1.8">
      <div style="font-size:30px;opacity:0.25;margin-bottom:10px">⬡</div>
      <div style="font-weight:600;margin-bottom:4px;color:var(--text);opacity:0.5">No gap suggestions yet</div>
      ${escHtml(offMsg)}
    </div>`;
    return;
  }

  badge.textContent = visible.length || '';
  badge.style.display = visible.length > 0 ? 'inline' : 'none';
  panel.classList.add('visible');
  countEl.textContent = visible.length;
  list.innerHTML = '';

  const byTable = new Map();
  for (const s of visible) {
    const key = s.table || '(unknown)';
    if (!byTable.has(key)) byTable.set(key, []);
    byTable.get(key).push(s);
  }

  for (const [tableName, suggestions] of byTable) {
    const group = document.createElement('div');
    group.className = 'suggestion-group';
    group.innerHTML = `
      <div class="suggestion-group-header">
        <span class="suggestion-group-chevron">▼</span>
        <span>Table</span>
        <span class="group-table-name">${escHtml(tableName)}</span>
        <span class="group-count">${suggestions.length} suggestion${suggestions.length !== 1 ? 's' : ''}</span>
      </div>
      <div class="suggestion-group-cards"></div>`;
    const cardsContainer = group.querySelector('.suggestion-group-cards');
    group.querySelector('.suggestion-group-header').addEventListener('click', () => {
      const collapsed = group.classList.toggle('sg-collapsed');
      group.querySelector('.suggestion-group-chevron').textContent = collapsed ? '▶' : '▼';
    });

    for (const s of suggestions) {
      const card   = document.createElement('div');
      card.className = 'suggestion-card';
      const stubId = `stub-${Math.random().toString(36).slice(2)}`;
      const candidates    = (s.candidates || []);
      const hasCandidates = s.type === 'property' && candidates.length > 0;

      const candidatesHtml = hasCandidates ? `
        <div class="suggestion-candidates">
          <div class="suggestion-candidates-label">Map to existing property:</div>
          ${candidates.map(c => {
            const localName    = c.uri.split('/').pop().split('#').pop();
            const displayLabel = c.label || localName;
            const showUri      = displayLabel !== localName;
            return `
            <div class="suggestion-candidate" data-uri="${escHtml(c.uri)}">
              <span class="candidate-label" title="${escHtml(c.uri)}">${escHtml(displayLabel)}</span>
              ${showUri ? `<span class="candidate-uri">${escHtml(localName)}</span>` : ''}
              <span class="candidate-score">${Math.round(c.score * 100)}%</span>
              <span class="candidate-strategy">${escHtml(c.strategy)}</span>
            </div>`;
          }).join('')}
        </div>` : '';

      const isProperty    = s.type === 'property';
      const nearMatch     = s.near_match_class || null;
      const domainInfo    = s.suggested_domain || {};
      const domainDefault = domainInfo.local_name || '';
      const domainInferred = !!domainInfo.local_name && domainInfo.local_name !== 'TODO_ClassName';
      const domainRowHtml = isProperty ? `
        <div class="suggestion-domain-row">
          <span class="suggestion-domain-label">Domain</span>
          <input class="suggestion-domain-input${domainInferred ? ' inferred' : ''}" type="text"
            value="${escHtml(domainDefault)}" list="ont-class-datalist"
            spellcheck="false" placeholder="Class name…"
            title="${domainInfo.uri ? 'Matched existing class: ' + escHtml(domainInfo.uri) : 'New class — will be resolved to the ontology namespace'}" />
        </div>` : '';

      const classNearMatchHtml = (!isProperty && nearMatch) ? `
        <div style="font-size:11px;color:var(--warn);background:rgba(251,191,36,0.06);border:1px solid rgba(251,191,36,0.2);border-radius:4px;padding:5px 9px;margin:5px 0;line-height:1.5">
          Similar class <b>${escHtml(nearMatch.local_name)}</b> already exists in the ontology.
          You can create <b>${escHtml(s.proposed_name)}</b> as a new separate class, or add all missing properties directly to <b>${escHtml(nearMatch.local_name)}</b>.
        </div>` : '';

      const addBtnHtml = (!isProperty && nearMatch)
        ? `<button class="btn-add-to-ont" style="background:rgba(52,211,153,0.08);color:var(--success);border-color:rgba(52,211,153,0.25)">Create new class</button>
           <button class="btn-add-props-to-class" style="background:rgba(52,211,153,0.15);color:var(--success);border-color:rgba(52,211,153,0.4);font-weight:600">Add properties to ${escHtml(nearMatch.local_name)}</button>`
        : `<button class="btn-add-to-ont" style="background:rgba(52,211,153,0.1);color:var(--success);border-color:rgba(52,211,153,0.3)">Add to ontology</button>`;

      const XSD_TYPES     = ['string','integer','decimal','float','double','boolean','date','dateTime','anyURI'];
      const suggestedRange = s.suggested_range || 'string';
      const rangeRowHtml  = isProperty ? `
        <div class="suggestion-domain-row">
          <span class="suggestion-domain-label">Range</span>
          <select class="suggestion-range-select" style="font-size:11px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:2px 4px">
            ${XSD_TYPES.map(t => `<option value="${t}" ${t === suggestedRange ? 'selected' : ''}>${t}${t === suggestedRange ? ' (suggested)' : ''}</option>`).join('')}
          </select>
        </div>` : '';

      card.innerHTML = `
        <div class="suggestion-card-header">
          <span class="suggestion-type-chip ${s.type}">${s.type === 'object_property' ? 'object' : s.type}</span>
          <input class="suggestion-name-input" type="text" value="${escHtml(s.proposed_name)}" spellcheck="false" />
        </div>
        <div class="suggestion-rationale">${escHtml(s.rationale)}</div>
        ${s.label   ? `<div style="font-size:12px;color:var(--text);margin:6px 0 2px;font-weight:600">${escHtml(s.label)}</div>` : ''}
        ${s.comment ? `<div style="font-size:11px;color:var(--muted);margin-bottom:4px;font-style:italic">${escHtml(s.comment)}</div>` : ''}
        ${s.hint    ? `<div style="font-size:11px;color:var(--accent2);background:rgba(136,192,208,0.06);border:1px solid rgba(136,192,208,0.15);border-radius:4px;padding:5px 9px;margin:5px 0;line-height:1.5"><b>Hint:</b> ${escHtml(s.hint)}</div>` : ''}
        ${s.is_fk_like && !s.hint ? `<div style="font-size:10px;color:var(--warn);background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.2);border-radius:4px;padding:3px 8px;margin:4px 0;">⚠ Looks like a foreign-key reference — consider an <b>owl:ObjectProperty</b> linking to the related class instead of a datatype property.</div>` : ''}
        ${classNearMatchHtml}
        ${domainRowHtml}
        ${rangeRowHtml}
        ${candidatesHtml}
        <pre class="suggestion-stub" id="${stubId}">${escHtml(s.owl_stub)}</pre>
        <div class="suggestion-actions">
          <div style="display:flex;gap:4px;flex:1;flex-wrap:wrap">
            ${addBtnHtml}
            <button class="btn-use-candidate" style="display:none;background:rgba(136,192,208,0.12);color:var(--accent);border-color:rgba(136,192,208,0.4)">Use this property</button>
            <button style="display:none" class="btn-copy-stub">Copy</button>
          </div>
          <div style="display:flex;gap:4px;flex-shrink:0">
            <button class="btn-stub-toggle" title="Show/hide OWL stub" style="font-weight:700;min-width:22px">?</button>
            <button class="btn-dismiss" title="Dismiss suggestion" style="min-width:22px;padding:2px 7px">✕</button>
          </div>
        </div>`;

      let selectedCandidateUri = null;
      let currentStub = s.owl_stub;
      const nameInput = card.querySelector('.suggestion-name-input');
      const stubEl    = card.querySelector('.suggestion-stub');

      nameInput.addEventListener('input', () => {
        const oldName = s.proposed_name;
        const newName = nameInput.value.trim() || oldName;
        currentStub = currentStub.replace(
          new RegExp(`(rdf:about="&ont;)[^"]*(")`),
          `$1${newName}$2`
        );
        stubEl.textContent = currentStub;
      });

      const rangeSelect = card.querySelector('.suggestion-range-select');
      if (rangeSelect) {
        rangeSelect.addEventListener('change', () => {
          const newRange = rangeSelect.value;
          rangeSelect.querySelectorAll('option').forEach(opt => {
            opt.textContent = opt.value === suggestedRange
              ? `${opt.value} (suggested)`
              : opt.value;
          });
          currentStub = currentStub.replace(
            /(<rdfs:range rdf:resource="(?:&xsd;|http:\/\/www\.w3\.org\/2001\/XMLSchema#))[^"]*(")/,
            `$1${newRange}$2`
          );
          stubEl.textContent = currentStub;
        });
      }

      const domainInput = card.querySelector('.suggestion-domain-input');
      if (domainInput) {
        domainInput.addEventListener('input', () => {
          const newDomain = domainInput.value.trim() || 'TODO_ClassName';
          currentStub = currentStub.replace(
            /(<rdfs:domain rdf:resource=")[^"]*(")/,
            `$1&ont;${newDomain}$2`
          );
          stubEl.textContent = currentStub;
        });
      }

      card.querySelector('.btn-stub-toggle').addEventListener('click', function() {
        const copyBtn = card.querySelector('.btn-copy-stub');
        const open    = stubEl.classList.toggle('open');
        this.style.background = open ? 'rgba(136,192,208,0.15)' : '';
        this.style.color      = open ? 'var(--accent)' : '';
        copyBtn.style.display = open ? 'inline-block' : 'none';
      });

      card.querySelector('.btn-copy-stub').addEventListener('click', function() {
        navigator.clipboard.writeText(stubEl.textContent).then(() => {
          this.textContent = 'Copied!';
          setTimeout(() => this.textContent = 'Copy stub', 1500);
        });
      });

      card.querySelectorAll('.suggestion-candidate').forEach(row => {
        row.addEventListener('click', () => {
          const uri = row.dataset.uri;
          if (selectedCandidateUri === uri) {
            row.classList.remove('selected');
            selectedCandidateUri = null;
            card.querySelector('.btn-add-to-ont').style.display    = 'inline-block';
            card.querySelector('.btn-use-candidate').style.display = 'none';
          } else {
            card.querySelectorAll('.suggestion-candidate').forEach(r => r.classList.remove('selected'));
            row.classList.add('selected');
            selectedCandidateUri = uri;
            card.querySelector('.btn-add-to-ont').style.display    = 'none';
            card.querySelector('.btn-use-candidate').style.display = 'inline-block';
          }
        });
      });

      card.querySelector('.btn-add-to-ont').addEventListener('click', () => {
        applyOntologySuggestion(s, card, nameInput.value.trim() || s.proposed_name, currentStub);
      });

      card.querySelector('.btn-use-candidate').addEventListener('click', () => {
        if (!selectedCandidateUri) return;
        useExistingProperty(s, card, selectedCandidateUri);
      });

      card.querySelector('.btn-dismiss').addEventListener('click', () => {
        _dismiss(s.id);
        renderSuggestions();
      });

      const addPropsBtn = card.querySelector('.btn-add-props-to-class');
      if (addPropsBtn && nearMatch) {
        addPropsBtn.addEventListener('click', () => {
          addPropertiesToExistingClass(s, card, nearMatch);
        });
      }

      cardsContainer.appendChild(card);
    } // end inner suggestions loop

    list.appendChild(group);
  } // end table group loop

  // Show/hide the pinned "Clear dismissed" footer button (#34)
  const clearFooter    = document.getElementById('suggestions-clear-dismissed-footer');
  const dismissedItems = _suggestionsData.filter(s => dismissed.has(s.id));
  if (clearFooter) clearFooter.style.display = dismissedItems.length > 0 ? 'block' : 'none';

  // ── Dismissed section ──────────────────────────────────────────────────────
  if (dismissedItems.length > 0) {
    const section = document.createElement('div');
    section.style.cssText = 'margin-top:12px;opacity:0.5;';
    section.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;padding:6px 10px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);margin-bottom:4px;">
        <span style="flex:1;font-weight:600">Dismissed (${dismissedItems.length})</span>
        <button onclick="_clearDismissed();renderSuggestions();" style="font-size:10px;padding:1px 7px;border-radius:4px;border:1px solid var(--border);background:none;color:var(--muted);cursor:pointer">Restore all</button>
      </div>`;
    for (const s of dismissedItems) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 10px;font-size:11px;border-radius:4px;';
      row.innerHTML = `
        <span class="suggestion-type-chip ${s.type}" style="flex-shrink:0">${s.type === 'object_property' ? 'object' : s.type}</span>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)" title="${escHtml(s.rationale)}">${escHtml(s.proposed_name)}<span style="color:var(--muted);margin-left:6px">${escHtml(s.table || '')}</span></span>
        <button data-id="${escHtml(s.id)}" style="font-size:10px;padding:1px 7px;border-radius:4px;border:1px solid var(--border);background:none;color:var(--muted);cursor:pointer;flex-shrink:0">Restore</button>`;
      row.querySelector('button').addEventListener('click', function() {
        const d = _getDismissed();
        d.delete(this.dataset.id);
        localStorage.setItem(_DISMISSED_KEY, JSON.stringify([...d]));
        renderSuggestions();
      });
      section.appendChild(row);
    }
    list.appendChild(section);
  }
}

async function applyOntologySuggestion(s, card, editedName, currentStub) {
  const btn  = card.querySelector('.btn-add-to-ont');
  const orig = btn.textContent;
  btn.textContent = 'Applying…';
  btn.disabled = true;
  try {
    const r = await fetch(`/api/ontology/suggestions/${encodeURIComponent(s.id)}/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owl_stub: currentStub || s.owl_stub, proposed_name: editedName || s.proposed_name }),
    });
    const data = await r.json();
    if (!r.ok) {
      btn.textContent = orig; btn.disabled = false;
      alert('Apply failed: ' + (data.detail || r.statusText));
      return;
    }
    _addApplied(s.id);
    _suggestionsData = _suggestionsData.filter(x => x.id !== s.id);
    renderSuggestions();
    _ontologyData = null;
    await loadOntology();
    await loadOntologyHistory();
    if (_ontologyData) {
      const targetName = s.type === 'class'
        ? (editedName || s.proposed_name)
        : (s.suggested_domain && s.suggested_domain.local_name);
      const match = targetName && _ontologyData.classes.find(c => {
        const local = c.uri.includes('#') ? c.uri.split('#').pop() : c.uri.split('/').pop();
        return local === targetName;
      });
      if (match) _selectedClassUri = match.uri;
      renderOntologyClasses();
      if (match) {
        renderClassDetail(match);
        const el = document.querySelector(`.ontology-class-item[data-uri="${CSS.escape(match.uri)}"]`);
        if (el) el.scrollIntoView({ block: 'nearest' });
      }
    }
  } catch(e) {
    btn.textContent = orig; btn.disabled = false;
    alert('Apply failed: ' + e);
  }
}

async function addPropertiesToExistingClass(classS, card, targetClass) {
  const btn = card.querySelector('.btn-add-props-to-class');
  btn.textContent = 'Applying…';
  btn.disabled = true;
  card.querySelector('.btn-add-to-ont').disabled = true;

  const propSuggestions = _suggestionsData.filter(
    ps => (ps.type === 'property' || ps.type === 'object_property') && ps.table === classS.table
  );

  const succeeded = [];
  for (const ps of propSuggestions) {
    const stub = (ps.owl_stub || '').replace(
      /(<rdfs:domain rdf:resource=")[^"]*(")/,
      `$1&ont;${targetClass.local_name}$2`
    );
    try {
      const r = await fetch(`/api/ontology/suggestions/${encodeURIComponent(ps.id)}/apply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ owl_stub: stub, proposed_name: ps.proposed_name }),
      });
      if (r.ok) succeeded.push(ps.id);
    } catch(_) {}
  }

  const appliedIds = [classS.id, ...succeeded];
  _addApplied(...appliedIds);
  _suggestionsData = _suggestionsData.filter(x => !new Set(appliedIds).has(x.id));
  renderSuggestions();

  _ontologyData = null;
  await loadOntology();
  await loadOntologyHistory();
  if (_ontologyData) {
    const match = targetClass.local_name && _ontologyData.classes.find(c => {
      const local = c.uri.includes('#') ? c.uri.split('#').pop() : c.uri.split('/').pop();
      return local === targetClass.local_name;
    });
    if (match) _selectedClassUri = match.uri;
    renderOntologyClasses();
    if (match) {
      renderClassDetail(match);
      const el = document.querySelector(`.ontology-class-item[data-uri="${CSS.escape(match.uri)}"]`);
      if (el) el.scrollIntoView({ block: 'nearest' });
    }
  }
}

async function useExistingProperty(s, card, uri) {
  const btn  = card.querySelector('.btn-use-candidate');
  const orig = btn.textContent;
  btn.textContent = 'Saving…';
  btn.disabled = true;
  try {
    const r = await fetch('/api/mappings/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_table: s.table, source_column: s.column, confirmed_uri: uri }),
    });
    const data = await r.json();
    if (!r.ok) {
      btn.textContent = orig; btn.disabled = false;
      alert('Confirm failed: ' + (data.detail || r.statusText));
      return;
    }
    _suggestionsData = _suggestionsData.filter(x => x.id !== s.id);
    renderSuggestions();
    document.getElementById('mappings-stale-banner').style.display = 'flex';
  } catch(e) {
    btn.textContent = orig; btn.disabled = false;
    alert('Confirm failed: ' + e);
  }
}

// ── Working Ontology selector ──────────────────────────────────────────────────

async function loadAvailableOntologyFiles() {
  try {
    const data = await fetch('/api/ontology/available-files').then(r => r.json());
    const sel  = document.getElementById('ontology-file-select');
    const active         = data.active || '';
    const activeFilename = active.split('/').pop().split('\\').pop();
    while (sel.options.length > 1) sel.remove(1);
    for (const f of (data.files || []).slice().sort((a, b) => b.localeCompare(a))) {
      const opt = document.createElement('option');
      opt.value = f;
      opt.textContent = f;
      if (f === activeFilename || f === active) opt.selected = true;
      sel.appendChild(opt);
    }
  } catch(e) { /* no-op */ }
}

async function selectOntologyFromDropdown(path) {
  if (!path) return;
  try {
    const r = await fetch('/api/ontology/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const data = await r.json();
    if (!r.ok) { alert('Could not load: ' + (data.detail || r.statusText)); return; }
    if (data.ontology_status) _updateOntologyStatus(data.ontology_status);
    _ontologyData = null; _selectedClassUri = null;
    await loadOntology();
  } catch(e) { alert('Error: ' + e); }
}

async function uploadOntologyFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r    = await fetch('/api/ontology/upload', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { alert('Upload failed: ' + (data.detail || r.statusText)); return; }
    if (data.ontology_status) _updateOntologyStatus(data.ontology_status);
    _ontologyData = null; _selectedClassUri = null;
    await loadOntology();
    await loadAvailableOntologyFiles();
  } catch(e) { alert('Upload error: ' + e); } finally {
    input.value = '';
  }
}

/** Show a generation strategy modal. Calls onConfirm(strategy) where strategy is "full" or "iterative". */
function showOntologyStrategyModal(onConfirm) {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center';

  const strategies = [
    { value: 'full',      title: 'Full Generator',   desc: 'Single multi-pass LLM call over the entire schema. Fast. Good for initial exploration.' },
    { value: 'iterative', title: 'RIGOR Generator',  desc: 'One table at a time in FK topological order. Gen-LLM + Judge-LLM per table with retry. Produces _rigor_ files. More reliable for complex schemas.' },
  ];

  let selected = 'full';

  const box = document.createElement('div');
  box.style.cssText = 'background:var(--bg,#1a1a2e);border:1px solid var(--border,#333);border-radius:10px;padding:24px;width:420px;max-width:90vw;color:var(--fg,#e2e8f0);font-family:var(--sans,sans-serif)';

  const title = document.createElement('h3');
  title.textContent = 'Ontology Generation Strategy';
  title.style.cssText = 'margin:0 0 16px;font-size:15px;font-weight:600';
  box.appendChild(title);

  strategies.forEach(s => {
    const label = document.createElement('label');
    label.style.cssText = 'display:flex;gap:10px;padding:10px 12px;border-radius:6px;cursor:pointer;border:1px solid transparent;margin-bottom:8px;transition:border-color 0.15s';

    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'ontology-strategy';
    radio.value = s.value;
    radio.checked = s.value === 'full';
    radio.style.cssText = 'margin-top:3px;flex-shrink:0;accent-color:var(--accent,#4ade80)';

    const textWrap = document.createElement('div');
    const strong = document.createElement('div');
    strong.textContent = s.title;
    strong.style.cssText = 'font-size:13px;font-weight:600;margin-bottom:3px';
    const desc = document.createElement('div');
    desc.textContent = s.desc;
    desc.style.cssText = 'font-size:11px;color:var(--muted,#94a3b8);line-height:1.5';
    textWrap.appendChild(strong);
    textWrap.appendChild(desc);

    label.appendChild(radio);
    label.appendChild(textWrap);

    radio.addEventListener('change', () => {
      selected = s.value;
      box.querySelectorAll('label').forEach(l => l.style.borderColor = 'transparent');
      label.style.borderColor = 'var(--accent,#4ade80)';
    });
    if (s.value === 'full') label.style.borderColor = 'var(--accent,#4ade80)';

    box.appendChild(label);
  });

  const btnRow = document.createElement('div');
  btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:8px;margin-top:16px';

  const cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Cancel';
  cancelBtn.style.cssText = 'padding:7px 16px;border-radius:5px;border:1px solid var(--border,#333);background:transparent;color:var(--muted,#94a3b8);cursor:pointer;font-size:13px';
  cancelBtn.onclick = () => document.body.removeChild(overlay);

  const confirmBtn = document.createElement('button');
  confirmBtn.textContent = 'Generate';
  confirmBtn.style.cssText = 'padding:7px 16px;border-radius:5px;border:none;background:var(--accent,#4ade80);color:#000;cursor:pointer;font-size:13px;font-weight:600';
  confirmBtn.onclick = () => { document.body.removeChild(overlay); onConfirm(selected); };

  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(confirmBtn);
  box.appendChild(btnRow);
  overlay.appendChild(box);
  overlay.addEventListener('click', e => { if (e.target === overlay) document.body.removeChild(overlay); });
  document.body.appendChild(overlay);
}

async function generateSeedOntology() { return _generateOntology('seed', null); }
async function generateFullOntology()  {
  showOntologyStrategyModal(strategy => _generateOntology('full', strategy));
}

async function _generateOntology(mode, ontologyGenerator) {
  const btnId   = mode === 'seed' ? 'generate-seed-btn' : 'generate-full-btn';
  const otherId = mode === 'seed' ? 'generate-full-btn' : 'generate-seed-btn';
  const label   = mode === 'seed' ? 'Generate Seed'     : 'Generate Full';
  const phase   = mode === 'seed' ? 'seed'               : 'full';

  const btn = document.getElementById(btnId);
  if (btn.disabled) return;

  let phases = phase;
  try {
    const insp = await fetch('/api/inspection').then(r => r.json());
    if (!insp.tables || insp.tables.length === 0 || insp.stale) phases = `inspect,${phase}`;
  } catch(e) { phases = `inspect,${phase}`; }

  btn.disabled = true;
  btn.textContent = 'Generating…';
  const otherBtn = document.getElementById(otherId);
  if (otherBtn) otherBtn.disabled = true;

  try {
    const body = { phases };
    if (ontologyGenerator) body.ontology_generator = ontologyGenerator;
    const r = await fetch('/api/pipeline/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) { alert('Could not start generation: ' + (data.detail || r.statusText)); return; }

    _pipelineLogCursor = 0;
    document.getElementById('pipeline-log').innerHTML = '';
    setPipelineStatus('running');
    startPipelineStream(0);
    _setPdState('partial');

    let done = false;
    let generatedOntology = null;
    while (!done) {
      await new Promise(res => setTimeout(res, 1500));
      try {
        const st = await fetch('/api/pipeline/status').then(r2 => r2.json());
        if (st.status !== 'running') {
          done = true;
          generatedOntology = st.generated_ontology || null;
        }
      } catch(e) { done = true; }
    }

    await fetch('/api/mappings/clear', { method: 'POST' });
    _clearDismissed();
    _clearApplied();
    loadMappings();

    await loadAvailableOntologyFiles();
    if (generatedOntology) {
      // Use the exact path the generator wrote — no guessing needed.
      const filename = generatedOntology.split('/').pop().split('\\').pop();
      const sel = document.getElementById('ontology-file-select');
      // Try to set the dropdown to the generated file (it may appear as filename or full path).
      for (const opt of sel.options) {
        if (opt.value === filename || opt.value === generatedOntology) {
          sel.value = opt.value;
          break;
        }
      }
      await selectOntologyFromDropdown(generatedOntology);
    }
  } catch(e) {
    alert('Generation error: ' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
    if (otherBtn) otherBtn.disabled = false;
  }
}

async function startMappingFromOntology() {
  if (_pipelineRunning) { alert('Pipeline is already running. Check the Pipeline tab.'); return; }

  _showThresholdDialog(async (threshold) => {
  const btn = document.getElementById('start-mapping-btn');
  btn.disabled = true;
  btn.textContent = 'Starting…';

  const ontologyPath = (() => {
    const sel = document.getElementById('ontology-file-select');
    if (!sel || !sel.value) return undefined;
    const statusPath = (window._lastOntologyStatus && window._lastOntologyStatus.configured_path) || '';
    const ontDir     = statusPath.includes('/')
      ? statusPath.slice(0, statusPath.lastIndexOf('/') + 1)
      : '';
    return ontDir + sel.value;
  })();

  try {
    const r = await fetch('/api/pipeline/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phases: 'inspect,map', threshold, ontology_path: ontologyPath }),
    });
    const data = await r.json();
    if (!r.ok) { alert('Could not start mapping: ' + (data.detail || r.statusText)); return; }
    _pipelineLogCursor = 0;
    document.getElementById('pipeline-log').innerHTML = '';
    setPipelineStatus('running');
    startPipelineStream(0);
    _setPdState('partial');
  } catch(e) {
    alert('Error starting mapping: ' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Start mapping';
  }
  }); // end _showThresholdDialog callback
}

document.getElementById('ontology-search').addEventListener('input', () => {
  if (!_ontologyData) return;
  renderOntologyClasses();
  if (_selectedClassUri) {
    const cls = _ontologyData.classes.find(c => c.uri === _selectedClassUri);
    if (cls) renderClassDetail(cls);
  }
});

document.getElementById('ontology-reload-btn').addEventListener('click', () => {
  _ontologyData = null;
  _selectedClassUri = null;
  loadOntology();
});

document.getElementById('ontology-orphan-btn').addEventListener('click', () => {
  if (!_ontologyData || !_ontologyData.orphan_properties.length) return;
  _selectedClassUri = '__orphan__';
  document.querySelectorAll('.ontology-class-item').forEach(el => el.classList.remove('active'));
  const panel = document.getElementById('ontology-detail-panel');
  const props = _ontologyData.orphan_properties;
  panel.innerHTML = `
    <div class="ont-class-title">Abstract Properties</div>
    <div class="ont-class-comment" style="margin-bottom:16px">Properties with no rdfs:domain — intentionally abstract, used as superproperties for SPARQL reasoning</div>
    <div class="ont-section-label">Properties (${props.length})</div>
    <table class="ont-prop-table">
      <thead><tr><th>Name</th><th>Type</th><th>Range</th><th>Label / Comment</th></tr></thead>
      <tbody>
        ${props.map(p => {
          const pLocal    = p.uri.includes('#') ? p.uri.split('#').pop() : p.uri.split('/').pop();
          const rangeHtml = p.range_uris.map(r => {
            const rLocal = r.includes('#') ? r.split('#').pop() : r.split('/').pop();
            return `<span style="font-family:var(--mono);font-size:10px;color:var(--muted)">${escHtml(rLocal)}</span>`;
          }).join(', ') || '<span style="color:var(--muted)">—</span>';
          return `<tr>
            <td><span class="ont-prop-name">${escHtml(pLocal)}</span></td>
            <td><span class="ont-prop-type-chip ${p.type}">${p.type}</span></td>
            <td>${rangeHtml}</td>
            <td class="ont-prop-comment">${escHtml(p.label || '')}${p.label && p.comment ? ' — ' : ''}${escHtml(p.comment || '')}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`;
});

// ── Ontology Catalog ───────────────────────────────────────────────────────────

function toggleCatalogDrawer() {
  const drawer = document.getElementById('ontology-catalog-drawer');
  if (!drawer) return;
  const open = drawer.style.display === 'none';
  drawer.style.display = open ? 'block' : 'none';
  const btn = document.getElementById('ontology-catalog-btn');
  if (btn) btn.textContent = open ? 'OWL Files ▴' : 'OWL Files ▾';
  if (open) loadCatalog();
}

async function loadCatalog() {
  try {
    const data = await fetch('/api/ontology/catalog').then(r => r.json());
    _catalogData = data.entries || [];
    renderCatalog();
  } catch(e) {
    const m = document.getElementById('ontology-catalog-msg');
    if (m) m.textContent = `Error: ${e}`;
  }
}

function renderCatalog() {
  const list = document.getElementById('ontology-catalog-list');
  if (!list) return;
  if (!_catalogData.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px">No catalog loaded — single OWL file mode.</div>';
    return;
  }
  list.innerHTML = _catalogData.map((entry, i) => {
    const pathHint = entry.path ? escHtml(entry.path) : escHtml(entry.logical_uri || '');
    const mtime    = entry.last_modified ? `<span style="font-size:10px;color:var(--muted);white-space:nowrap">${escHtml(entry.last_modified)}</span>` : '';
    return `<div style="display:flex;flex-direction:column;gap:2px;padding:5px 8px;border-radius:5px;background:rgba(255,255,255,0.03);">
      <div style="display:flex;align-items:center;gap:10px;">
        <input type="checkbox" ${entry.enabled ? 'checked' : ''} data-catalog-idx="${i}" onchange="toggleCatalogEntry(${i}, this.checked)" style="cursor:pointer;" />
        <span style="font-family:var(--mono);font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${pathHint}">${escHtml(entry.filename)}</span>
        ${mtime}
        <span style="font-size:10px;padding:1px 6px;border-radius:3px;${entry.enabled ? 'background:rgba(74,222,128,0.15);color:#4ade80' : 'background:rgba(156,163,175,0.1);color:var(--muted)'};">${entry.enabled ? 'on' : 'off'}</span>
      </div>
      ${pathHint ? `<div style="font-size:10px;color:var(--muted);padding-left:22px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${pathHint}">${pathHint}</div>` : ''}
    </div>`;
  }).join('');
}

async function toggleCatalogEntry(idx, enabled) {
  const entry = _catalogData[idx];
  if (!entry) return;
  const msgEl = document.getElementById('ontology-catalog-msg');
  if (msgEl) msgEl.textContent = 'Updating…';
  try {
    const r = await fetch('/api/ontology/catalog/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ filename: entry.filename, enabled }),
    });
    const data = await r.json();
    if (!r.ok) { if (msgEl) msgEl.textContent = `Error: ${data.detail || r.statusText}`; return; }
    if (msgEl) msgEl.textContent = data.message || 'Updated.';
    await loadCatalog();
    _ontologyData = null; _selectedClassUri = null;
    loadOntology();
  } catch(e) { if (msgEl) msgEl.textContent = `Error: ${e}`; }
}

async function addCatalogFile() {
  const pathInput = document.getElementById('ontology-catalog-add-path');
  const msgEl     = document.getElementById('ontology-catalog-msg');
  if (!pathInput) return;
  const path = pathInput.value.trim();
  if (!path) { if (msgEl) msgEl.textContent = 'Enter a file path.'; return; }
  if (msgEl) msgEl.textContent = 'Loading…';
  try {
    const r = await fetch('/api/ontology/catalog/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path }),
    });
    const data = await r.json();
    if (!r.ok) { if (msgEl) msgEl.textContent = `Error: ${data.detail || r.statusText}`; return; }
    if (msgEl) msgEl.textContent = data.message || 'Added.';
    pathInput.value = '';
    await loadCatalog();
    _ontologyData = null; _selectedClassUri = null;
    loadOntology();
  } catch(e) { if (msgEl) msgEl.textContent = `Error: ${e}`; }
}

// ── Ontology Version History ───────────────────────────────────────────────────

function toggleOntologyHistory() {
  const drawer = document.getElementById('ontology-history-drawer');
  if (!drawer) return;
  const open = drawer.style.display === 'none';
  drawer.style.display = open ? 'block' : 'none';
  const btn = document.getElementById('ontology-history-btn');
  if (btn) btn.textContent = open ? 'History ▴' : 'History';
  if (open) loadOntologyHistory();
}

async function loadOntologyHistory() {
  try {
    const data = await fetch('/api/ontology/history').then(r => r.json());
    _ontologyHistoryData = data.snapshots || [];
    renderOntologyHistory();
  } catch(e) {
    const hl = document.getElementById('ontology-history-list');
    if (hl) hl.innerHTML = `<div style="color:var(--muted);font-size:11px">Error: ${escHtml(String(e))}</div>`;
  }
}

function renderOntologyHistory() {
  const list = document.getElementById('ontology-history-list');
  if (!list) return;
  if (!_ontologyHistoryData.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px">No snapshots yet. Run the pipeline to capture a version.</div>';
    return;
  }
  list.innerHTML = _ontologyHistoryData.map(snap => {
    const hash8     = (snap.hash || '').slice(0, 8);
    const date      = snap.snapped_at ? _formatRunDate(snap.snapped_at) : '—';
    const fileCount = (snap.snapshot_files || []).length;
    const desc      = snap.description || 'Pipeline checkpoint';
    const isSuggestion = desc.toLowerCase().includes('via suggestion');
    const chipStyle = isSuggestion
      ? 'background:rgba(251,191,36,0.15);color:#fbbf24'
      : 'background:rgba(136,192,208,0.15);color:#81A1C1';
    const chipLabel = isSuggestion ? 'edit' : 'pipeline';
    return `<div style="display:flex;flex-direction:column;gap:3px;padding:8px 10px;border-radius:5px;background:rgba(255,255,255,0.03);margin-bottom:4px;">
      <div style="display:flex;align-items:center;gap:10px;">
        <span style="font-family:var(--mono);font-size:10px;color:var(--accent)">${escHtml(hash8)}</span>
        <span style="font-size:10px;padding:1px 6px;border-radius:3px;${chipStyle}">${chipLabel}</span>
        <span style="font-size:11px;color:var(--muted)">${escHtml(date)}</span>
        <span style="font-size:10px;color:var(--muted)">${fileCount} file${fileCount !== 1 ? 's' : ''}</span>
        <button onclick="restoreOntologySnapshot(${snap.id})" style="margin-left:auto;font-size:10px;padding:2px 8px;border-radius:4px;border:1px solid var(--border);background:none;color:var(--muted);cursor:pointer;">Restore</button>
      </div>
      <div style="font-size:11px;color:var(--text);padding-left:2px">${escHtml(desc)}</div>
    </div>`;
  }).join('');
}

async function restoreOntologySnapshot(id) {
  const list = document.getElementById('ontology-history-list');
  if (!list) return;
  const oldHtml = list.innerHTML;
  list.innerHTML = '<div style="color:var(--muted);font-size:11px;padding:4px 0">Restoring snapshot…</div>';
  try {
    const r    = await fetch(`/api/ontology/history/${id}/restore`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok) {
      list.innerHTML = `<div style="color:var(--error,#f87171);font-size:11px">${escHtml(data.detail || r.statusText)}</div>`;
      setTimeout(() => { list.innerHTML = oldHtml; }, 3000);
      return;
    }
    _ontologyData = null; _selectedClassUri = null;
    await loadOntology();
    await loadOntologyHistory();
  } catch(e) {
    list.innerHTML = `<div style="color:var(--error,#f87171);font-size:11px">Error: ${escHtml(String(e))}</div>`;
    setTimeout(() => { list.innerHTML = oldHtml; }, 3000);
  }
}
