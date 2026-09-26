# Proof Presentation Contract

**Version:** 1.0.0  
**Schema class:** `semantic_mapper.prove.presentation.ProofPresentation`  
**Stability:** STABLE — breaking changes increment the minor or major version in `proof_presentation_version`.

---

## Purpose

`ProofPresentation` is the standardised presentation layer for proof results returned by the `prove_answer` MCP tool. It sits above the raw proof records (v1 lineage, v2 semantic) and provides an ordered, frozen, versioned structure that agentic clients and web renderers can rely on.

**Constraints:**
- The underlying proof records (v1 ProofRecord, v2 ProofRecord) are **not modified** by this layer.
- SPARQL generation and query execution are **not affected**.
- `ProofPresentation` is always computed on top of existing proof data; it never generates new claims.

---

## Section Order (contractually fixed)

Renderers **must** present sections in this order. Omitting or reordering sections is a contract violation.

| # | Key | Description |
|---|-----|-------------|
| 1 | `question_interpretation` | What the NL question was understood to mean, in one sentence derived from the v2 interpretation. |
| 2 | `query_display` | The SPARQL executed, the plain-English question, and the cache status (`fresh` / `cached` / `stale_ontology`). |
| 3 | `ontology_grounding` | The OWL classes and properties referenced by the query, with full URIs. |
| 4 | `data_layer_trace` | Gold/silver/bronze tables that contributed to the result, with column names. |
| 5 | `transformation_provenance` | Mapping decisions from the semantic mapping report (confirmation, score, strategy). |
| 6 | `result_confidence` | Row count, quarantined rows, broken lineage chains, and any pipeline caveats. |
| 7 | `limitations` | Epistemic limitations — always the last section. Must not be omitted or paraphrased. |

---

## Sub-models

### `QueryDisplay`
```
sparql: str                  — the SPARQL as executed
plain_english_summary: str   — the original NL question
cache_status: "fresh" | "cached" | "stale_ontology"
```
`stale_ontology` is emitted when the ontology subgraph hash stored in the proof record differs from the current ontology hash. Not yet implemented in v1.0.0; always `"fresh"`.

### `OntologyGrounding`
```
classes_used: List[str]       — full OWL class URIs
properties_used: List[PropertyTrace]
```
`PropertyTrace`:
```
uri: str                      — full property URI
local_name: str               — local name (after # or /)
abstract_parent: Optional[str] — reserved: rdfs:subPropertyOf (future)
```

### `LayerTrace`
```
layer: "gold" | "silver" | "bronze"
table_name: str
column_names: List[str]       — from row_values / raw_values keys in lineage
```
Ordered: gold entries come before silver, bronze last.

### `TransformationRecord`
```
property_uri: str
local_name: str
confirmation: str             — "HUMAN", "AUTO", "CONFIRMED"
score: Optional[float]
rule_reference: Optional[str] — winning_strategy from mapping report
```

### `ResultConfidence`
```
row_count: int                — from v2 framing.cardinality (preferred) or v1 subject_count
quarantined_row_count: int
broken_lineage_count: int
cache_hit: bool               — False in v1.0.0
stale_ontology_warning: bool  — False in v1.0.0
notes: List[str]              — non-QUARANTINED caveats from v1
```

---

## Agent rendering rules

When the MCP tool returns `proof_presentation`, agents **must**:

1. Render all seven sections in order.
2. Quote `question_interpretation` verbatim as the opening statement.
3. List every limitation in section 7 — do not condense or paraphrase.
4. Not reorder, merge, or omit sections.
5. Not add substantive interpretation beyond the data.

Prose framing ("Verified:") around sections is acceptable; editorial restructuring is not.

---

## Versioning policy

- **Patch** (1.0.x): bug fixes, clarification. No field changes.
- **Minor** (1.x.0): additive fields (new optional keys). Renderers that ignore unknown keys remain compatible.
- **Major** (x.0.0): field removal, rename, or section reorder. Renderers must update.

Agents should read `proof_presentation_version` and warn if the major version exceeds what they were built against.

---

## Data sources

| Section | Primary source | Fallback |
|---------|---------------|----------|
| 1 | v2 `interpretation` (fm: annotations) | v1 `interpretation.interpreted_as` |
| 2 | `MCPQueryRecord.executed_query` | — |
| 3 | v2 `framing` + `scope_assumptions` | empty lists |
| 4 | v1 `lineage` chains | empty list |
| 5 | v1 `mapping_decisions` | empty list |
| 6 | v2 `framing.cardinality` + v1 `caveats` | v1 `subject_count` |
| 7 | v2 `limitations[].text` + v1 `limitations` | — |

---

## Changelog

### 1.0.0 (2026-05-17)
- Initial release. Seven ordered sections. `cache_hit` and `stale_ontology_warning` reserved (always `False`). `abstract_parent` on `PropertyTrace` reserved (always `None`).
