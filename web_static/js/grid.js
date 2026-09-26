// ── AG Grid setup ──────────────────────────────────────────────────────────────

let _fragmentMode = false;

function _uriFragment(v) {
  if (typeof v !== 'string') return v;
  if (!v.startsWith('http://') && !v.startsWith('https://')) return v;
  const hash = v.lastIndexOf('#');
  if (hash >= 0 && hash < v.length - 1) return v.slice(hash + 1);
  const slash = v.lastIndexOf('/');
  if (slash >= 0 && slash < v.length - 1) return v.slice(slash + 1);
  return v;
}

function _displayVal(v) {
  return _fragmentMode ? _uriFragment(v) : v;
}

const _CURRENCY_KEYWORDS = ['cost','price','amount','coverage','payment','revenue','fee','charge','expense','income','salary','wage','total','sum','balance'];
const _CURRENCY_FMT = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
const _NUMBER_FMT   = new Intl.NumberFormat('en-US');

function _isNumeric(v) {
  if (v == null || v === '') return false;
  return !isNaN(Number(v)) && !isNaN(parseFloat(v));
}

function _columnIsNumeric(col, rows) {
  const sample = rows.map(r => r[col]).filter(v => v != null && v !== '').slice(0, 20);
  return sample.length > 0 && sample.every(_isNumeric);
}

function _columnIsCurrency(col) {
  const lower = col.toLowerCase();
  return _CURRENCY_KEYWORDS.some(kw => lower.includes(kw));
}

function initGrid(columns, rows) {
  const el = document.getElementById('grid');
  if (gridApi) {
    gridApi.destroy();
    gridApi = null;
  }

  const colDefs = columns.map(c => {
    const numeric  = _columnIsNumeric(c, rows);
    const currency = numeric && _columnIsCurrency(c);
    return {
      field: c,
      headerName: formatHeader(c),
      sortable: true,
      filter: numeric ? 'agNumberColumnFilter' : 'agTextColumnFilter',
      resizable: true,
      flex: 1,
      minWidth: 120,
      valueGetter: numeric
        ? params => { const v = params.data[c]; return v != null ? Number(v) : null; }
        : params => params.data[c],
      cellRenderer: params => {
        if (params.value == null) return '<span style="color:#64748b;font-style:italic">—</span>';
        return escHtml(String(_displayVal(params.value)));
      },
    };
  });

  const gridOptions = {
    columnDefs: colDefs,
    rowData: rows,
    defaultColDef: { sortable: true, filter: true, resizable: true },
    animateRows: true,
    suppressCellFocus: true,
    rowSelection: 'single',
  };

  gridApi = agGrid.createGrid(el, gridOptions);
}
