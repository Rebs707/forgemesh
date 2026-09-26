// ── Shared global state ────────────────────────────────────────────────────────
// Declared with var so they are accessible from all loaded script files.

// Query / grid
var gridApi = null;
var turns = [];
var activeTurn = -1;
var totalPrompt = 0, totalCompletion = 0, totalCalls = 0;
var _lastRows = null;
var _lastQuestion = '';

// Ontology property list (for URI autocomplete)
var _ontologyProps = [];

// Pipeline
var _pipelineHistoryData = [];
var _selectedHistoricRunId = null;
var _pipelineRunning = false;
var _pipelineEventSource = null;
var _pipelineLogCursor = 0;
var _bannerEventSource = null;
var _pdState = localStorage.getItem('pd_state') || 'collapsed';

// Tabs
var activeTab = 'query';

// Corrections
var _correctionsData = [];

// Mappings
var _mappingsData = null;

// Relational Schema
var _rawSchemaData = null;
var _rawSchemaSelectedTable = null;

// Ontology
var _ontologyData = null;
var _selectedClassUri = null;
var _suggestionsData = [];
var _catalogData = [];
var _ontologyHistoryData = [];
