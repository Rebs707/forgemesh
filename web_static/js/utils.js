// ── Shared utilities ──────────────────────────────────────────────────────────

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function formatHeader(key) {
  return key
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

function _uriDisplay(uri) {
  if (!uri) return { ns: '', local: uri };
  const hashIdx  = uri.lastIndexOf('#');
  const slashIdx = uri.lastIndexOf('/');
  const split    = hashIdx > slashIdx ? hashIdx : slashIdx;
  return { ns: uri.slice(0, split + 1), local: uri.slice(split + 1) };
}

function _formatRunDate(isoStr) {
  try {
    const d    = new Date(isoStr);
    const diff = Date.now() - d.getTime();
    if (diff < 60000)    return 'just now';
    if (diff < 3600000)  return Math.floor(diff / 60000)   + 'm ago';
    if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
    return d.toLocaleDateString();
  } catch(e) { return isoStr ? isoStr.slice(0, 10) : '—'; }
}
