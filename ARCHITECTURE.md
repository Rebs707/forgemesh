# ForgeMesh — Architecture Reference

Technical reference for contributors. For setup and usage see [README.md](README.md).

---

## Data flow

```
MySQL (bronze)
    │
    ▼ inspector.py
TableProfile / ColumnProfile
    │
    ▼ mapper.py  ◄─── corrections_store.py (warm cache)
    │             ◄─── ontology/loader.py  (OWL graph)
MappingReport (JSON)
    │
    ├──▶ silver/pipeline.py  →  silver_{table}  (MySQL)
    │
    ├──▶ gold/pipeline.py    →  gold_{class}    (MySQL)
    │
    └──▶ rdf/pipeline.py     →  Fuseki          (RDF/SPARQL)
                                     │
                                     ▼
                               query/rdf/engine.py   (NL → SPARQL)
                               mcp_server.py          (Claude Desktop)
```

---

## Mapping engine

### Strategy execution order

```
Tier 1  HistoryMatcher      (SQLite lookup — zero compute)
        AnnotationMatcher   (rdflib graph scan — skos:altLabel / rdfs:label / rdfs:comment)
        NameMatcher         (rapidfuzz string comparison)
        TypeMatcher         (XSD type alignment + FK structural typing)

Tier 2  SemanticMatcher     (sentence-transformers cosine similarity)
        [only if best Tier 1 score < AUTO_CONFIRM_THRESHOLD]

Tier 3  LLMStrategy         (OpenAI batch call)
        [only if best Tier 2 score < AUTO_CONFIRM_THRESHOLD]
```

### Aggregation algorithm (`aggregator.py`)

```
1.  HistoryMatcher short-circuit
    → any human correction wins immediately at 1.0

2.  AnnotationMatcher exact short-circuit
    → score ≥ ANNOTATION_EXACT_CONFIRM_THRESHOLD (default 0.95)
    → suppressed if property rdfs:domain ≠ table OWL class

3.  NameMatcher exact short-circuit
    → score ≥ NAME_EXACT_CONFIRM_THRESHOLD (default 0.97)
    → suppressed if property rdfs:domain ≠ table OWL class

4.  Collect best score per (uri, strategy)

5.  Type gate
    → eliminate all URIs where TypeMatcher score == 0.0
    (DatatypeProperties on FK columns; ObjectProperties on scalar columns)

6.  Domain-preference filter
    → if any surviving candidate has rdfs:domain matching the table OWL class,
      drop all candidates with a mismatching or absent domain
    → skipped if no domain-matching candidates exist (preserves backwards compat)

7.  Weighted average with floor denominator
    floor = Σ(weight_s for non-LLM strategies that ran)
    denom(uri) = max(Σ(weight_s for strategies that scored THIS uri), floor)
    score(uri) = Σ(score_s × weight_s) / denom(uri)

8.  Return highest-scoring URI
```

### TypeMatcher score matrix

**Non-FK DatatypeProperties** (evaluated against `rdfs:range`):

| Condition | Score |
|---|---|
| Data-proven date (profiler detected date format in VARCHAR) + `xsd:date` range | 0.92 |
| Native SQL DATE/DATETIME + `xsd:date` range | 0.85 |
| Exact XSD match (non-date) | 0.80 |
| Compatible numeric family | 0.70 |
| Both `xsd:string` | 0.60 |
| No `rdfs:range` declared | 0.15 |
| Type mismatch | 0.05 |

**FK columns** (`_id` suffix or confirmed in `table_profile.foreign_keys`):

| Property type | Score |
|---|---|
| DatatypeProperty | 0.0 (eliminated by type gate) |
| ObjectProperty | 0.80 |

**ObjectProperties on non-FK scalar columns:**

| Condition | Score |
|---|---|
| Categorical column (has `top_values`) | 0.10 (NamedIndividual value-mapping compatible) |
| Non-categorical | 0.0 (eliminated by type gate) |

---

## Iterative ontology generator

### Generation loop

```
for table in fk_topological_order(tables):
    context = build_table_context(table, accumulating_graph, embedding_index)
    for attempt in range(max_retries + 1):
        manchester = gen_llm(build_gen_prompt(context))
        fragment   = parse_manchester(manchester)
        verdict    = judge_llm(build_judge_prompt(fragment, context))
        if verdict.accepted:
            break
        context = rebuild_with_revision(context, verdict.issues)
    merge(accumulating_graph, fragment)
    embedding_index.add_all(new_classes)
```

### Post-accumulation safety passes (in execution order)

| Pass | Function | What it fixes |
|---|---|---|
| URI normalisation | `normalize_datatype_property_uris` | Inconsistent camelCase on DatatypeProperty URIs |
| ObjectProperty qualification | `_qualify_obj_props_safety` | ObjectProperties with >1 domain get abstract parent + class-scoped subproperties |
| Missing column fill | `_fill_missing_column_properties` | Columns with no property get a synthetic DatatypeProperty |
| Domain/range inference | `_infer_missing_domains_and_ranges` | Properties missing domain or range get inferred values from profile |
| Abstract parent cleanup | `_strip_domain_from_abstract_parents` | Strips `rdfs:domain` from any property with ≥2 `rdfs:subPropertyOf` children |
| FK abstract removal | `_remove_superseded_fk_datatype_abstracts` | Removes DatatypeProperty abstracts superseded by ObjectProperty abstracts |
| Deduplication | `_deduplicate_properties` | Merges same-altLabel DatatypeProperties; longest-named URI wins |
| Multi-domain split | `_split_multi_domain_dt_properties` | Splits DatatypeProperties that accumulated >1 domain (from dedup) into class-scoped subproperties named `has{DomainClass}{AltLabelCamelCase}` |
| OneOf enumerations | `_build_oneof_enumerations` | Low-cardinality class values become `owl:oneOf` NamedIndividuals |
| Self-ref cleanup | `_scan_and_strip_self_ref_subprops` | Removes self-referential `rdfs:subPropertyOf` triples |

### Multi-domain split in detail

After `_deduplicate_properties` merges, a single winner property can end up with N `rdfs:domain` triples — one per table that contributed that column name. `_split_multi_domain_dt_properties` resolves this:

1. Find every DatatypeProperty with >1 domain.
2. Derive concept from `skos:altLabel` + `ONTOLOGY_COL_WORD_SPLITS` expansion → CamelCase.
3. For each domain class `C`, compute `has{C}{Concept}`.
   - If the computed URI **equals** the original (property already correctly named for this domain): re-attach domain directly, no subPropertyOf.
   - If the computed URI **already exists**: wire `rdfs:subPropertyOf` only.
   - Otherwise: create new DatatypeProperty with domain, range, altLabel, and `rdfs:subPropertyOf` link.
4. Make original abstract (remove all domain triples, keep as parent).

---

## Silver layer

### Wide-row model

Each `silver_{table}` has one row per bronze row. Columns:

- `_bronze_source_ref` — opaque identifier linking back to the bronze row
- `_silver_run_id` — run_id of the silver pipeline run that created the row
- `_mapping_run_id` — run_id of the mapping run used (for lineage)
- One column per confirmed property mapping (named by property local name)

### Transformer chain

First matching transformer wins. All three are stateless; the chain is rebuilt per row.

```
DateNormaliser  →  ValueMapper  →  NullHandler (catch-all)
```

Rows that raise an exception in any transformer go to `quarantine_{table}` with the error message. `write_quarantine` never raises.

---

## Gold layer

### Builder logic (`gold/builder.py`)

```
for table in mapping_report.tables:
    class_uri = highest_confidence_class_uri(table)
    for col in table.confirmed_columns:
        gold_col = resolve_property(col.mapped_uri, ontology_graph)
        definition[class_uri].add_column(gold_col)
```

FK columns are resolved to OWL ObjectProperty direction using `rdfs:range` on the ontology graph — a FK pointing to `Patient` becomes a column in the Patient gold table, not a literal.

### Lineage columns added to gold tables

- `_silver_refs` — JSON array of `_bronze_source_ref` values that contributed to this gold row
- `_mapping_run_id` — run_id of the mapping run

---

## RDF layer

### Triple building (`rdf/triple_builder.py`)

For each gold row:

1. **Subject URI**: `UriFactory.individual(entity_name, row["id"])` — percent-encodes the id value.
2. **Type triple**: `subject rdf:type ontology:ClassName`.
3. **Property triples**: for each non-audit column:
   - FK column (name matches an entity in the gold entity map) → `owl:ObjectProperty` triple, object URI derived from the FK value.
   - Otherwise → `owl:DatatypeProperty` triple, literal with XSD type inferred from SQL type and value.

### XSD type inference (`_xsd_type`)

| SQL type | XSD type |
|---|---|
| INT, BIGINT | `xsd:integer` |
| DECIMAL, NUMERIC | `xsd:decimal` |
| FLOAT, DOUBLE | `xsd:double` |
| BOOLEAN, BIT | `xsd:boolean` |
| DATE (value matches YYYY-MM-DD) | `xsd:date` |
| DATETIME, TIMESTAMP (ISO format) | `xsd:dateTime` |
| All others | `xsd:string` |

---

## Query interface

Two independent engines:

| Engine | Location | Query target | Protocol |
|---|---|---|---|
| `SemanticQueryEngine` | `query/engine.py` | Silver or gold MySQL tables | NL → SQL |
| `SparqlQueryEngine` | `rdf/query/engine.py` | Fuseki triplestore | NL → SPARQL |

Both:
- Never raise — all errors captured in result model
- Enforce SELECT-only via stripping comment syntax before keyword scan
- Enforce `QUERY_MAX_ROWS` (cap or append LIMIT)
- Track `api_calls`, `prompt_tokens`, `completion_tokens` on result

---

## Evaluation (`eval/`)

OMOP-aligned ground truth YAML drives precision/recall/F1 measurement:

```bash
pytest tests/eval/ -v
```

`MappingQualityReport` records per-column correct/incorrect decisions with the actual URI and status (confirmed/pending/auto). Used in `tests/mimic/test_omop_validation.py` for live regression against the MIMIC-III demo database.

---

## Database models

### Corrections store (`corrections.db`)

```sql
CREATE TABLE corrections (
    id          INTEGER PRIMARY KEY,
    column_name TEXT NOT NULL,
    mapped_uri  TEXT NOT NULL,
    status      TEXT NOT NULL,   -- CONFIRMED | REJECTED
    source      TEXT,            -- HUMAN | AUTO
    created_at  DATETIME
);
```

Pattern-based lookup: `column_name` is table-agnostic (e.g. `subject_id` matches across all tables).

### Run store (`pipeline_runs.db`)

Stores metadata and serialised mapping reports per pipeline run. Columns: `run_id`, `phase`, `started_at`, `completed_at`, `mapping_report` (JSON blob), `status`.

---

## Settings reference (key knobs)

All configurable via `.env` or environment. Full list in `semantic_mapper/config/settings.py`.

| Setting | Default | Effect |
|---|---|---|
| `AUTO_CONFIRM_THRESHOLD` | 0.70 | Score floor for automatic confirmation |
| `ANNOTATION_EXACT_CONFIRM_THRESHOLD` | 0.95 | AnnotationMatcher short-circuit floor |
| `NAME_EXACT_CONFIRM_THRESHOLD` | 0.97 | NameMatcher short-circuit floor |
| `ANNOTATION_MATCHER_WEIGHT` | 0.20 | Aggregation weight |
| `NAME_MATCHER_WEIGHT` | 0.35 | Aggregation weight |
| `TYPE_MATCHER_WEIGHT` | 0.25 | Aggregation weight |
| `SEMANTIC_MATCHER_WEIGHT` | 0.45 | Aggregation weight |
| `LLM_MATCHER_WEIGHT` | 2.00 | Aggregation weight (exceeds Tier 1+2 floor) |
| `ONTOLOGY_COL_WORD_SPLITS` | `{}` | JSON map of column abbreviation → expansion used by iterative generator and multi-domain split pass |
| `PIPELINE_WORKERS` | CPU count (max 12) | Thread count for parallel silver/gold/RDF phases |
| `SAMPLE_SIZE` | 1000 | Rows sampled per column in profiling |
| `LOW_CARDINALITY_THRESHOLD` | 20 | Max distinct values for `top_values` population |
| `NULL_RATE_WARNING_THRESHOLD` | 0.30 | Fraction of NULLs that triggers a warning |
