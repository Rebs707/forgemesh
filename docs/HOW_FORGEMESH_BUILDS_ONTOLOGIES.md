# How ForgeMesh Builds Ontologies and Materialises Triples

ForgeMesh turns a raw relational database into a queryable, provable RDF knowledge graph. It works in stacked layers — **BRONZE** (the live relational source), **SILVER** (typed/profiled/transformed), **GOLD** (ontology-mapped relational views), **RDF** (materialised triples in Fuseki), and a **QUERY/PROOF** layer on top. The OWL ontology that licenses those triples is built table-by-table by a RIGOR-style generator: a generation LLM proposes one table's OWL fragment, an adversarial judge LLM critiques it, and a long chain of deterministic post-passes repairs, deduplicates, and completes the result.

The design wager throughout is that *correctness comes from deterministic scaffolding, not LLM discretion.* Class names and URIs, FK ranges, class-vs-literal decisions, provenance literals, topological order, and coherence repairs are all computed by code; the LLMs only fill in labels, descriptions, and property shapes around that scaffolding.

This chapter follows a single relational schema all the way through and back. Each subsystem can be read on its own, but the spine of the book is five cross-cutting threads — what happens to a table, a column, a value, a primary key, and a foreign key at every stage. Those threads are traced below and re-examined in depth, with file and function citations, across thirteen sections. Where the code is heuristic, ambiguous, or a known false positive, the chapter says so.

## Quick glossary

Read these one-liners first; the rest of the chapter assumes them.

| Term | One-line meaning |
| --- | --- |
| **Ontology** | A formal model of the things in a domain — their types and how they relate. |
| **OWL** | Web Ontology Language; the language ForgeMesh writes the ontology in. |
| **RDF** | The data model that represents facts as graphs of subject–predicate–object statements. |
| **Triple** | One RDF fact: a (subject, predicate, object) statement. |
| **IRI / URI** | A globally unique identifier (like a URL) naming a thing, type, or property. |
| **Class** | A type/category of thing (e.g. `Country`, `Patient`). |
| **Individual** | A specific instance of a class (e.g. the country "France"). |
| **ObjectProperty** | A predicate whose value is another individual (a link between two things). |
| **DatatypeProperty** | A predicate whose value is a literal (a string, number, date). |
| **rdfs:domain** | Declares which class a property's subject must belong to. |
| **rdfs:range** | Declares the type of a property's value (a class for objects, a datatype for literals). |
| **rdfs:subPropertyOf** | Declares one property to be a specialisation of a more general parent property. |
| **owl:oneOf** | Defines a class by an exhaustive, fixed list of named individuals (a closed vocabulary). |
| **owl:unionOf** | Defines a class as the union ("any of these classes") of several classes. |
| **skos:altLabel** | An alternate/synonym label on a term — here, the original column name, used by the mapper. |
| **SPARQL** | The query language for RDF graphs (the graph-world analogue of SQL). |
| **Fuseki** | The triple store (RDF database) that holds and serves the materialised triples. |
| **Manchester Syntax** | A compact, human-readable text format for writing OWL; what the gen-LLM emits. |
| **Primary key (PK)** | The column(s) that uniquely identify a row in a relational table. |
| **Foreign key (FK)** | A column referencing another table's primary key — a relational link between rows. |

## Reading guide

1. **Ingestion & Inspection (Bronze → Profiles)** — reading the live database into `TableProfile`/`ColumnProfile` records.
2. **From Profiles to Table Context & Naming** — deterministic class naming, URIs, and the per-table `TableContext`.
3. **Dependency Ordering & Retrieval (Topology + RAG)** — FK topological sort and the accumulating class index.
4. **The Generation Prompt — Modelling Rules** — how `build_gen_prompt` encodes the modelling contract.
5. **The Discriminator — Judge-LLM & Retry Loop** — `JudgeVerdict`, the 17 criteria, and the bounded retry loop.
6. **Parsing Manchester Syntax into RDF** — regex-based `parse_manchester` and the prefix-leak fix.
7. **Merge & Structural Repair** — folding fragments into the accumulating graph; the MIXED_RANGE resolver.
8. **Generator Orchestration & the Post-Loop Passes** — `IterativeOntologyGenerator.generate` and the ordered repair chain.
9. **Deterministic Coherence, Enrichment & Range Checks** — reasoner-free guardrails over the finished ontology.
10. **Semantic Mapping — Silver Columns to Ontology Properties** — the tiered strategy engine producing the `SemanticMappingReport`.
11. **The Gold Layer — Ontology-Mapped Relational Views** — `GoldTableDefinition`s and the `gold_*` / `gold_semantic_*` tables.
12. **RDF Materialisation — Rows to Triples** — `TripleBuilder`, deterministic URIs, and the Fuseki load.
13. **Querying & Proving (Consumption Layer)** — NL→SQL/SPARQL, the `ProofRecord`, and lineage walking.

## The layered architecture

The journey is a single forward pipeline from a live database to provable triples, with the ontology-construction loop bolted onto the middle. The structures that flow between stages matter as much as the stages themselves — each arrow below carries a named one.

```
  BRONZE (live relational DB)
     │   MySQLInspector.profile_all()                                   §1
     ▼
  List[TableProfile]  ── per table: row_count, columns{ColumnProfile}, foreign_keys
     │                    (declared+heuristic PKs, explicit+inferred FKs, top_values,
     │                     detected_formats, null_rate, quality_warnings)
     ├──────────────────────────────────────────────┐
     │  build_table_context()                §2      │  fk_topological_sort()        §3
     ▼                                               │  + AccumulatedClassIndex (RAG)
  TableContext  ── ColumnContext[] with              │  → ordered List[TableProfile]
     │  cardinality_hint, is_categorical,            │    (dimensions first, facts last)
     │  is_pk/is_fk/fk_ref_table/fk_ref_skipped      │
     │  + deterministic derive_class_uri()           │
     ▼                                               ▼
  ┌─────────────────  RIGOR ONTOLOGY GENERATION LOOP, per table  ─────────────────┐
  │  build_gen_prompt() ─► Gen-LLM ─► Manchester text                §4           │
  │      ▼                                                                        │
  │  parse_manchester() ─► OntologyFragment (rdflib.Graph + declared lists)   §6  │
  │      ▼                                                                        │
  │  build_judge_prompt() ─► Judge-LLM ─► JudgeVerdict ─┐ (retry w/ revisions) §5 │
  │      ▼ (approved / confidence floor / max retries)  └──────────────┘          │
  │  merge() ─► accumulating OWL Graph  (+ pre/post fixers, MixedRangeResolution) §7│
  │      ▼  MergeResult.new_classes ─► AccumulatedClassIndex (feeds next table)    │
  └───────────────────────────────────────────────────────────────────────────────┘
     │  ordered post-loop passes (normalise, dedup, split, oneOf, mixed-range…)   §8
     ▼
  OWL graph  ── classes (fm:sourceTable), datatype props (fm:sourceColumn/sqlType/
     │             nullRate), object props (fm:sourceForeignKey), oneOf vocabularies
     │  OntologyLoader.load() + annotation sweep + coherence/range checks          §9
     ▼
  SemanticMappingReport  ── per column: mapped_property_uri, confidence, status,   §10
     │  value_mappings; per table: class_candidate_set, primary_key_columns
     │  (tiered strategy engine: Name/Type/Annotation/Semantic/LLM)
     ▼
  List[GoldTableDefinition]  ── gold_{class}: column_specs (property_uri,           §11
     │  value_map), id_column (PK→subject key), FK→object-property resolution
     │  GoldPipeline ─► gold_* tables + gold_semantic_* views
     ▼
  RDF triples in Fuseki named graph                                               §12
     │   TripleBuilder: PK→hashed subject URI, columns→typed literals,
     │   value_map→NamedIndividual links, FK→object props (direction-flipped),
     │   fm:bronzeSourceRef / fm:goldRunId provenance
     ▼
  QUERY / PROOF layer  ── NL→SQL/SPARQL (QueryResult) + ProofRecord               §13
         (ontology subgraph, lineage gold→silver→bronze, scope/limitations)
```

The named hand-offs: `TableProfile` (§1) is the sole output of inspection; `TableContext` (§2) is the sole structured input to the gen prompt; `OntologyFragment` (§5/§6) is what the judge reviews and the merger folds in; the accumulating `rdflib.Graph` is the single growing artifact threaded through the whole generation loop and post-passes; `GoldTableDefinition` (§11) is the contract the RDF stage reads; and the `ProofRecord` (§13) is the auditable record that closes the loop back to the ontology and the bronze rows.

## Five cross-cutting threads

These five threads are the spine of the chapter. Each follows one relational concept — a table, a column, a value, a primary key, a foreign key — through the whole pipeline, grouped into four phases so it reads as a journey rather than a checklist. Section pointers (§1–§13) say where each step is documented in full.

### TABLES

**In plain terms:** each source table becomes one entity *type* (a class), and its rows become individuals of that class.

**Discovery & naming.** The inspector profiles *every* table — no allow/deny list — emitting one `TableProfile` each (§1). Before any LLM is involved, `derive_class_uri` gives the table a deterministic PascalCase class name and IRI: it strips `raw_`/`d_` prefixes and singularises, with an optional PK-denotes-entity override (a `medals` table keyed by `country_code` becomes class `Country`) (§2). Each table is then one node in the foreign-key dependency graph; `fk_topological_sort` orders dimension/reference tables before fact tables, and any table below `ONTOLOGY_MIN_ROWS_FOR_GENERATION` rows is dropped into `skip_tables` (§3).

**Generation & checking.** The table is the unit of a single prompt. Its identity is fixed by Rule 1's deterministic URI, and an entity-class guard stops the model enumerating individuals (`owl:oneOf`) when `row_count > max_rows_for_enumeration` (§4). The judge re-checks that same guard for entity tables (§5).

**Repair & completion.** At merge, the table becomes an `owl:Class` stamped with `fm:sourceTable`; identical redeclarations are deduped, and a high-row table modelled as an enumeration raises a CRITICAL warning (§6–§7). The post-loop passes never re-mint the class, but they attach gap-filled columns to it and freeze genuinely low-cardinality classes into `owl:oneOf` (high-row entity classes are excluded) (§8). The coherence checker then flags an *orphan class* — a table-class never used as any property's domain (§9).

**Mapping & materialisation.** In mapping, `evaluate_table()` fuzzy-matches the table name to a class to choose `table_class_uri` — the linchpin for domain-aware disambiguation — and records a `class_candidate_set` (§10). The gold builder groups mappings by winning class into one `gold_{class}` table (which may merge several silver tables) (§11), and RDF materialisation reads each `GoldTableDefinition` as a stream of instances of `primary_class_uri` (§12). At query time a silver table is multi-class (split by `_ontology_class_uri`), while a gold table is single-class and name-encoded (`gold_patient` ⇒ `Patient`) (§13).

### FIELDS / COLUMNS

**In plain terms:** each column becomes a *property* — either an attribute holding a literal, or a link to another entity — and is then bound to the ontology and turned into values.

**Discovery.** The inspector profiles every column into a `ColumnProfile` carrying `inferred_type`, `null_rate`, `distinct_count`, `top_values`, `detected_formats`, and quality warnings; none are skipped (§1). Context-building flattens each into a `ColumnContext` with a `cardinality_hint` (`CATEGORICAL_CLOSED` / `CATEGORICAL_OPEN` / `FLAG` / `FREE_TEXT` / `IDENTIFIER` / `""`) that decides how it will be modelled; an FK-shadow demotion clears the hint on a name column that merely shadows a coded FK (§2). Columns enter topology only through their FK columns, which seed per-FK retrievals (§3).

**Generation & checking.** Every non-PK column is listed in the prompt with its type and flags. Rule 10 makes the full non-PK column list mandatory coverage; scalars become DatatypeProperties (with required Domain + Range), generic names are class-scoped (Rules 6/11/13), and each property must carry `skos:altLabel` of the column name (Rule 7 — the hook the mapper later keys on) plus per-column `fm:sourceColumn`/`fm:sqlType`/`fm:nullRate` provenance (P.2) (§4). The judge polices full non-PK coverage and per-hint typing (§5), and the parser emits the DatatypeProperty triples with those provenance literals intact (§6).

**Repair & completion.** Merge and post-loop reconcile the same column appearing across many tables: `_deduplicate_properties` (grouping by `skos:altLabel`) collapses duplicates, `_split_multi_domain_dt_properties` re-splits them per class under an abstract parent, `_fill_missing_column_properties` guarantees every non-PK/non-FK column has a property, and `normalize_datatype_property_uris` canonicalises the URIs from the column name (§7–§8). Coherence then flags missing domain/range and name collisions (§9).

**Mapping & materialisation.** Each column runs the tiered mapping engine (Name/Type/Annotation/Semantic/LLM) to bind a `mapped_property_uri`, keyed heavily on `skos:altLabel` and a SQL-type gate (§10).

Every CONFIRMED mapping becomes a `GoldColumnSpec` populating the `gold_*` table, with `gold_semantic_*` views aliasing columns to their property names (§11). RDF then emits typed literals via `_xsd_type` (sniffing values for string columns), skipping collision-loser columns whose `property_uri` was cleared (§12). At query time the column resurfaces with its bound ontology-property fragment, and `_validate_gold_columns` hard-rejects any column the LLM invents (§13).

### VALUES

**In plain terms:** a cell value starts as sampled data; the pipeline decides whether a column's values form a *closed vocabulary* (→ a class with named individuals) or are plain literals, and materialises them accordingly.

**Discovery & the class-vs-literal decision.** `_profile_column` stores up to five sample values, computes `distinct_count` via `COUNT(DISTINCT)`, builds `top_values` for low-cardinality columns, and runs date-format detection (mixed formats raise a CRITICAL `FORMAT_INCONSISTENCY` warning) (§1).

Context-building then decides class-vs-literal from the actual samples: `_values_look_descriptive` keeps URLs, JSON cells, and near-unique multi-word text as literals, while `is_closed_vocabulary` (gated primarily on null-rate) admits genuine controlled vocabularies as `CATEGORICAL_CLOSED` and copies them into `categorical_values` (§2). Topology reads no cell values at all — only `row_count` cardinality (§3).

**Generation & checking.** In the prompt, values surface either as the full closed set (up to 8) for `CATEGORICAL_CLOSED`/`CATEGORICAL`, or as a top-N sample (up to 5) otherwise. A closed set becomes a Class + NamedIndividuals + an `owl:oneOf`-eligible ObjectProperty (Rule 4), and `oneOf`/NamedIndividual are forbidden outside `CATEGORICAL_CLOSED` (Rule 14) (§4). The judge enforces that categorical promotion and the individual/oneOf restriction (§5), and the parser records column-level statistics as `fm:nullRate`/`fm:sqlType` and instance values via `Individual` blocks (§6).

**Repair & enrichment.** Merge and post-passes treat values as controlled vocabularies: `consolidate_enumeration_classes` conservatively merges duplicate vocab classes (on stem + label overlap), `_build_oneof_enumerations` freezes members into `owl:oneOf` and canonicalises individual URIs to `{Value}{ClassName}`, and the MIXED_RANGE R3 tier decides vocab-class vs literal from the value statistics (§7–§8). The enricher then declares enumerated values as typed `owl:NamedIndividual`s inside `owl:oneOf`, refusing to create untyped ones (§9).

**Mapping & materialisation.** `generate_value_mappings()` maps each distinct value to a class or per-value URI (validating against hallucination), and `canonicalise_value_mappings()` enforces first-declaration-wins URIs across tables (§10).

The gold builder stores these as a `GoldColumnSpec.value_map`, drives a `CASE` translation in the semantic view, and detects discriminated subtypes when one column maps distinct values to distinct classes (§11). RDF emits `value_map` hits as object links to NamedIndividuals, with guards raising `fm:invalidMapping`/`fm:incompleteReference` when a value resolves to a class (§12). At query time, live-sampled distinct values become categorical hints and `LIKE` fragments, and non-firing SILVER transforms are caught and flagged (§13).

### PRIMARY KEYS

**In plain terms:** a primary key is treated as the row's *identity*, not as data — it never becomes a property; instead it becomes the key used to mint each instance's IRI.

**Discovery.** PKs are detected in two tiers: declared (from the PK constraint) or heuristic (`_infer_pk`: a name like `id`/`uuid`/`guid`, zero nulls, perfect uniqueness) (§1). Context-building surfaces this onto `ColumnContext.is_pk` and deliberately *excludes* PKs from the cardinality-hint machinery (a PK never becomes a vocabulary or flag); only the PK's *name* can drive a class-name override (`compute_class_name_overrides`) (§2). PKs do not participate in the FK graph or ordering (§3).

**Generation & checking.** The PK column is flagged `PK`, excluded from Rule 10's coverage list and from P.2 provenance, and suppressed as the entity's identity rather than turned into a property (§4); the judge likewise excludes PKs from coverage (§5). The parser has no PK-specific predicate — a PK rides through only as an ordinary column's datatype property if emitted at all (§6).

**Repair & completion.** Merge has no PK branch; a PK's high uniqueness only indirectly keeps it out of controlled vocabularies (§7). In post-loop, PKs name the class (via the override) and gate enumeration (high-row entity classes are excluded from `oneOf`), and `_fill_missing_column_properties` deliberately skips PK columns (§8).

**Mapping & materialisation.** Mapping skips PKs as properties (`if col_profile.is_primary_key: continue`) but preserves them into `TableMapping.primary_key_columns` as the name-agnostic, composite-aware row key (§10).

The gold builder turns that into the `id_column` (the subject-IRI key), preferring the inspector PK over an `*_id` heuristic, then verifies/repairs it in `refine_id_columns()` (uniqueness checks, name/code fallback, composite keys) (§11). RDF mints the subject IRI from `id_column` — single, `|`-composite, `fm:entityIdentifierColumn`, or the PK value parsed from `_bronze_source_ref` — by SHA-256 hashing, and always emits the primary class plus transitive ancestors as `rdf:type` (§12). In proofs, `_silver_id`/`_gold_id` autoincrements key the lineage hops and the bronze PK is recovered from `_bronze_source_ref` (§13).

### FOREIGN KEYS

**In plain terms:** a foreign key should become a *link* between two entities (an ObjectProperty). It is the most heavily repaired concept — and every repair keys on the inspector's real FK info, never the LLM's annotation.

**Discovery.** The inspector captures explicit FKs from the schema and adds inferred ones via a four-strategy cascade (natural-key, table-name, PK-name, role-prefixed) plus value-overlap disambiguation, pruning redundant descriptive FKs and flagging a known false positive (`raw_transfers.los`/`outtime`) (§1). Context-building reads FKs from `TableProfile.foreign_keys` into `ColumnContext.is_fk`/`fk_ref_table`, propagates `fk_ref_skipped` when the target table won't exist, summarises them into `TableContext.explicit_fks`, and demotes columns that merely shadow a coded FK (§2).

FKs are the central driver of topology: they define every dependency edge, set processing order, are the edge dropped to break a cycle (affecting order only, never the materialised FK), and seed per-FK retrievals so shared relations like `hasPatient` surface (§3).

**Generation & checking.** Each non-skipped FK is given an *exact* derived range-class URI and must be an ObjectProperty with that range (Rule 3 — a HARD REJECTION otherwise), `SubPropertyOf:` an abstract FK parent when one matches (Rule 16), and carry `fm:sourceForeignKey` provenance (P.3); the lone exception is a `[SKIPPED]` FK modelled as `DatatypeProperty xsd:integer` (§4). The judge enforces FK→ObjectProperty, correct ranges, the SKIPPED carve-out, and FK provenance (§5), and the parser emits the ObjectProperty with `rdfs:domain`/`rdfs:range` and `fm:sourceForeignKey` — its most load-bearing output (§6).

**Repair & completion.** Merge actively corrects FKs: `_fix_datatype_fk_properties` converts wrongly-scalar FKs to ObjectProperties, `_strip_dual_annotated_object_properties` removes fabricated FK relationships, and the MIXED_RANGE R1 tier forces FK columns to class ranges — all keyed on the inspector's `fk_info`, never the LLM's annotation (§7). Post-loop abstracts FKs into shared `has{Target}` parents, removes stale FK-scalar abstracts, and strips fabricated FK classes (§8). Coherence raises a hard `SOURCE_COLUMN_COLLISION` when a column is claimed as both literal and FK, resolvable via an override sidecar (§9).

**Mapping & materialisation.** In mapping, FK recognition (an `_id` suffix or membership in `foreign_keys`) drives the Datatype→Object inversion in `TypeMatcher`, the `For<Class>` strip + domain boost in `NameMatcher`, and the cross-type upgrade in `_resolve_to_specific_property` (§10).

The gold builder rewrites FK columns to the correct object property via three strategies (entity-name match, `fm:sourceForeignKey`, `fm:sourceColumn` routing) (§11). RDF emits FK columns as object-property links — minting the target IRI by hashing the referenced PK value so joins resolve — with a *direction flip* when the property's range equals the current class, and `fm:incompleteReference` for null/broken references (§12).

At query time, gold FKs are reconstructed by name into an explicit `JOINS` block, cross-layer FKs (`_silver_refs`, `_bronze_source_ref`) drive the lineage walker, and object-property FKs surface as `infer_match_links`, OPEN_WORLD limitations, and union-expanded scope assumptions (§13).


---

## 1. Ingestion & Inspection (Bronze → Profiles)

**In plain terms:** This first stage reads your raw SQL database and writes down a detailed description of every table and column — types, null rates, sample values, primary keys, foreign keys, and data-quality flags — without changing the database itself. Everything downstream is built from that description.

**Inputs → Outputs:** a live database connection (the BRONZE layer — the untouched source tables) in, a list of in-memory `TableProfile` records out.

### 1.0 What this stage does and where the code lives

This stage *observes* the live database and produces the data structures that every later stage consumes:

- SILVER (typing)
- GOLD (mapping)
- RDF materialisation (turning data into triples — single subject–predicate–object facts)

Nothing is rewritten or moved. All of the logic lives in two files:

- `semantic_mapper/db/inspector.py` — the `MySQLInspector` class plus a set of module-level heuristic functions.
- `semantic_mapper/db/models.py` — the Pydantic models `TableProfile`, `ColumnProfile`, and `DataQualityWarning`.

### 1.1 Inputs and outputs

**Input:** a live SQLAlchemy connection.

`MySQLInspector.__init__` (inspector.py:341) builds an engine from `settings.DATABASE_URL` with `execution_options={"isolation_level": "READ UNCOMMITTED"}`. That isolation level means profiling never blocks on, or is blocked by, writers.

Despite the class name, the SQL is dialect-portable enough that the code branches on `self.engine.dialect.name == "sqlite"` for identifier quoting in just one place (inspector.py:410).

**Output:** `profile_all()` (inspector.py:348) returns a `List[TableProfile]`. That list is the sole hand-off to the rest of the pipeline.

Each `TableProfile` is a small Pydantic record (models.py:23):

| Field | Type | Meaning |
|---|---|---|
| `table_name` | `str` | The raw table name as reported by the DB |
| `row_count` | `int` | Total rows (see §1.5) |
| `columns` | `Dict[str, ColumnProfile]` | Keyed by column name |
| `foreign_keys` | `List[Dict[str, Any]]` | Both explicit and inferred FKs (see §1.6) |

The orchestration in `profile_all()` is two steps:

1. Profile every table via `_profile_table`.
2. Run `_apply_inferred_fks(profiles)` once across the whole set.

FK (foreign-key) inference is necessarily cross-table, so it cannot happen until all tables are profiled — hence the second pass.

### 1.2 TABLES — discovery and what gets profiled

`profile_all()` enumerates tables with SQLAlchemy's `inspect(self.engine).get_table_names()` and profiles **every** table returned. There is no allow/deny list, no schema filter, and no skipping by name or size.

A table with zero rows is still profiled:

- Its `row_count` is `0`.
- The per-column statistics degrade gracefully — null-rate and uniqueness ratios guard against division by zero.

Note one asymmetry: natural-key and uniqueness logic later *ignores* zero-row tables (e.g. `_build_natural_key_map` skips `p.row_count == 0`, inspector.py:72), but the `TableProfile` itself is always produced.

### 1.3 FIELDS / COLUMNS — the ColumnProfile

`_profile_table` (inspector.py:478) gathers three pieces of schema metadata:

- column metadata via `ins.get_columns`,
- the declared primary key via `ins.get_pk_constraint(...).get("constrained_columns", [])`,
- explicit foreign keys via `ins.get_foreign_keys`.

Every column is then profiled by `_profile_column` (inspector.py:494) into a `ColumnProfile` (models.py:11). All columns are profiled — none are skipped — but several fields are only *conditionally* populated:

| `ColumnProfile` field | How it is computed |
|---|---|
| `inferred_type` | `str(col_meta['type'])` — the SQLAlchemy-reported column type, verbatim (e.g. `VARCHAR(64)`, `INTEGER`). This is the BRONZE type; SILVER refines it. |
| `is_nullable` | `col_meta['nullable']` — the declared schema nullability, not an observation. |
| `null_rate` | Observed: `COUNT(*) WHERE col IS NULL` divided by `total_rows` (`0` when the table is empty). Distinct from `is_nullable` — a column can be declared nullable yet have `null_rate == 0`. |
| `distinct_count` | `SELECT COUNT(DISTINCT col)` — true cardinality from the DB, deliberately **not** `len(set(sample))`, because a sample misses values absent from the first N rows (comment at inspector.py:501). |
| `sample_data` | First 5 non-null values: a `LIMIT settings.SAMPLE_SIZE` query is run, then sliced to `sample[:5]` when stored (inspector.py:496, 527). The larger sample is used internally for format detection; only 5 are persisted. |
| `top_values` | Full value distribution `[{"value", "count"}, ...]` ordered by descending count — populated **only** for low-cardinality columns (see below). Empty otherwise; its presence is later used as a signal that a column is categorical. |
| `detected_formats` | List of date/datetime format labels found in the sample (see §1.4). |
| `is_primary_key` | Declared-or-heuristic PK (see §1.5). |
| `quality_warnings` | `List[DataQualityWarning]` (see §1.7). |

#### The low-cardinality gate (which columns get `top_values`)

**In plain terms:** `top_values` is only filled in for columns that look like a small fixed set of categories (an enum), not for ID-like or high-variety columns.

`_is_low_cardinality` (inspector.py:298) decides whether to materialise `top_values`. It requires *all* of:

- the column is **not** a (declared) primary key;
- the name does **not** match `_ID_PATTERN = (_id|_key|_ref|_token|_hash|_guid|_uuid)$` (inspector.py:12) — identifier-like columns are excluded even if numerically low-cardinality;
- `distinct_count <= settings.LOW_CARDINALITY_THRESHOLD` (an absolute ceiling);
- and, **only on tables with `row_count >= 500`**, `distinct_count / row_count <= settings.LOW_CARDINALITY_RATIO`.

Why the row-count condition on the ratio test? Below 500 rows the ratio is always high even for genuine categoricals, so the ratio test is skipped (comment at inspector.py:319-321).

The effect: `top_values` becomes the inventory of enum-like / categorical fields (e.g. a `medal` column of GOLD/SILVER/BRONZE), which downstream stages use for value-mapping rather than treating as identifiers.

### 1.4 VALUES — sampling, statistics, and format detection

Per-value work in `_profile_column` issues a handful of targeted SQL queries against the live table:

1. **Sample:** `SELECT col WHERE col IS NOT NULL LIMIT settings.SAMPLE_SIZE`.
2. **Null count → `null_rate`.**
3. **`COUNT(DISTINCT col)` → `distinct_count`.**
4. **For low-cardinality columns only:** a `GROUP BY col ORDER BY cnt DESC` to build `top_values`.

#### Format detection

**In plain terms:** This scans the string samples in a column to figure out which date/datetime formats appear — and flags a column that mixes more than one.

`_detect_formats` (inspector.py:538) regex-matches each *string* sample value against `MySQLInspector.DATE_PATTERNS` (inspector.py:327-339) and returns the **set** of distinct format labels seen. The pattern table covers:

- date-only forms (`YYYY-MM-DD`, `DD/MM/YYYY`, `YYYY/MM/DD`),
- ISO-8601 datetimes (UTC, UTC-with-milliseconds, with timezone offset, and bare),
- the MySQL space-separated `YYYY-MM-DD HH:MM:SS` canonical form.

Non-string values are ignored. Because the return is a set of *labels*, a column whose values mix two date formats yields `len(detected_formats) > 1` — the trigger for a CRITICAL warning (§1.7).

`detected_formats` is also load-bearing for FK inference: a column with any detected date/time format is categorically excluded from being a FK or a natural-key target (§1.6).

### 1.5 PRIMARY KEYS — declared first, then heuristic

**In plain terms:** ForgeMesh uses the database's declared primary key if there is one; if not, it guesses one from the column's name and uniqueness.

PK (primary-key) detection is two-tier, resolved in `_profile_column` (inspector.py:517-520):

1. **Declared:** `declared_pk = name in pk_cols`, where `pk_cols` came from SQLAlchemy's PK constraint.
2. **Heuristic fallback** (only when not declared): `_infer_pk(col_name, distinct_count, row_count, null_rate)` (inspector.py:18). It returns `True` only when **all** hold:
   - the name matches `_PK_NAME_PATTERN = ^(id|uuid|guid)$` (case-insensitive) — i.e. exactly `id`/`uuid`/`guid`, **not** merely ending in `_id`;
   - `null_rate == 0` and `row_count > 0`;
   - `distinct_count == row_count` (perfectly unique).

The final value is `is_primary_key = declared_pk or heuristic_pk`. This recovers PKs in raw dumps that carry no formal constraint (issue #70).

One subtlety worth noting: the `top_values` low-cardinality check uses the *declared* PK status (`name in pk_cols`, inspector.py:509), because heuristic PK resolution happens a few lines later.

### 1.6 FOREIGN KEYS — explicit constraints plus a four-strategy inference cascade

**In plain terms:** A foreign key is a column whose values point at another table's rows. ForgeMesh first reads the FKs the schema declares, then *guesses* additional ones with four name-based strategies and a value-overlap tiebreaker — and finally prunes guesses that look redundant.

#### Explicit FKs

Explicit FKs are read straight from the schema in `_profile_table` (inspector.py:491). Each constraint becomes `{"column": <first constrained col>, "ref_table": <referred_table>}`.

- Only the first constrained column is captured — composite FKs are reduced to their first column.
- Explicit FKs carry **no** `inferred` flag, which protects them from later pruning.

#### Inferred FKs — the entry point

Inferred FKs are added by `_apply_inferred_fks` (inspector.py:354), which runs `_infer_fks` (schema-only, strategies 1–4) and then resolves ambiguous cases by sampling the live DB.

`_infer_fks` (inspector.py:145) iterates every non-PK, non-explicit-FK, **non-date** column. Date-formatted columns are skipped at inspector.py:188 — "a date value cannot be a surrogate key". For each candidate column it tries each strategy in order until one resolves.

#### The four inference strategies

- **Strategy 1 — natural-key match.** `_build_natural_key_map` (inspector.py:59) collects non-PK columns that are near-unique in their home table: `distinct/row_count >= 0.95` (`_NATURAL_KEY_UNIQUENESS`) and `null_rate < 0.05` (`_NATURAL_KEY_NULL_RATE`). On a name collision, the table with the highest uniqueness wins.

  Two guards keep attributes out of this map:
  - date-formatted columns (`if col.detected_formats: continue`, inspector.py:81);
  - **free-text** columns — `_looks_free_text` (inspector.py:42) flags a column when ≥ 50% (`_FREE_TEXT_FRACTION`) of its string samples contain internal whitespace, i.e. multi-word names/titles/places, which are near-unique but are descriptions, not identifiers.

  A column elsewhere with the same name is then inferred as a FK to that table **only if it is *not* near-unique locally** (`local_uniqueness < 0.95`, inspector.py:200). This catches MIMIC-style naming (`subject_id`, `hadm_id`, `icustay_id`) where column names don't echo table names.

- **Strategy 2 — table-name match.** A base-name map is built by stripping a leading `raw_` and registering both the plural and (trailing-`s`-stripped) singular form (inspector.py:170-176). A column's name, with a trailing `_id` removed, that matches another table's base name becomes a FK (the `patient_id → patients` convention).

  This strategy is **skipped for columns that have `top_values`** (inspector.py:209) — a populated `top_values` marks the column as a low-cardinality categorical (e.g. a `medal` value column vs a `medals` table), which should be value-mapped, not linked.

- **Strategy 3 — PK-name match.** `_build_pk_name_map` (inspector.py:97) maps every *declared/heuristic PK* column name to the list of tables owning it. If a column's name matches exactly **one** other table's PK, that table is the target (inspector.py:216-222) — this catches bare identifiers like `cgid`/`itemid` that have no `_id` suffix and whose target PK is excluded from the natural-key map.

  Names matching PKs in **multiple** tables are *not* resolved here; they are deferred (see ambiguous resolution below).

- **Strategy 4 — role-prefixed FK pattern** (inspector.py:225-242). For a locally non-unique column with ≥ 3 underscore-separated parts, it progressively strips leading role prefixes and tests each suffix against the natural-key map: `dest_airport_id → airport_id` resolves to `raw_airports`. Suffixes of ≤ 2 chars (e.g. bare `id`) are rejected as too generic.

#### Ambiguous PK resolution by value overlap

**In plain terms:** When a column's name matches the primary key of more than one table, ForgeMesh actually looks at the data — it samples the column's values and picks the target table whose key those values best overlap with.

`_ambiguous_pk_fks` (inspector.py:116) returns columns whose name matches the PK of *more than one* table (e.g. `itemid` in both `d_items` and `d_labitems`). For each, `_apply_inferred_fks` calls `_resolve_by_value_overlap` (inspector.py:396) — unless Strategy 1/2 already resolved that column (inspector.py:369).

This is the only step that queries data to decide a FK:

- It samples up to `_FK_OVERLAP_SAMPLE = 200` distinct non-null source values.
- For each candidate, it computes the fraction that appear in the candidate's first PK column.
- The candidate with the highest score wins, but only if `best_score >= _FK_OVERLAP_THRESHOLD = 0.80` (inspector.py:470); otherwise it returns `None` and no FK is added.

Per-candidate failures are caught and logged as warnings, not raised.

#### Merging and pruning

Inferred FKs are merged into each profile's `foreign_keys`, skipping any column that already has a FK (inspector.py:383-384); merged inferred entries carry `"inferred": True`.

Finally `_prune_redundant_descriptive_fks` (inspector.py:253) drops a redundant inferred FK in this situation: a table references the same target through two columns, and one of them name-matches the target's PK (the genuine key, e.g. `country_code → medals`) while the other is only descriptive (e.g. `country → medals`). The descriptive one is pruned.

Two safeguards bound this behaviour:

- It fires **only** when a key-aligned sibling exists, so a legitimate role-prefixed pair like `source_airport_id`/`dest_airport_id` (neither matching the PK name) survives intact.
- **Explicit DB-declared FKs are never pruned** — the predicate requires `fk.get("inferred")`.

#### Known false positive

The FK cascade is heuristic, and the code comments acknowledge it can over-fire.

In the MIMIC-style data set, `raw_transfers.los` and `raw_transfers.outtime` are inferred as FKs to `raw_icustays`. This is spurious: `los` (length-of-stay) and `outtime` are *measurements*, not references.

Why each slips through:

- `outtime` is a datetime and would normally be caught by the date guard — but only if `_detect_formats` recognises its serialized form.
- `los` is numeric and near-unique, so it can slip through the natural-key or value-overlap path into a `raw_icustays` PK.

Takeaway for downstream stages: treat low-confidence inferred FKs (especially numeric/measurement columns) with suspicion rather than as ground truth.

### 1.7 Quality warnings

`_profile_column` attaches `DataQualityWarning` records (models.py:5) at two points (inspector.py:531-534):

| Warning type | Severity | Fires when | Message |
|---|---|---|---|
| `NULL_RATE` | `WARNING` | `null_rate > settings.NULL_RATE_WARNING_THRESHOLD` | `"High Nulls"` |
| `FORMAT_INCONSISTENCY` | `CRITICAL` | `len(detected_formats) > 1` (a single column carrying values in more than one date format) | `"Mixed Dates"` |

`DataQualityWarning` also has a `sample_anomalies` list, but at this stage it is left empty (defaulted).

These warnings ride along on each `ColumnProfile` into SILVER, where typing/transformation decisions can act on them.

### 1.8 Hand-off to the next stage

The output of this stage is a flat `List[TableProfile]` in memory, carrying:

- declared types, observed null rates and cardinalities,
- small samples and categorical value distributions,
- detected formats,
- declared + heuristic primary keys,
- explicit + inferred foreign keys,
- per-column quality warnings.

No data is copied or transformed.

This profile set is exactly the structure the SILVER layer reads to perform type refinement, profiling-driven transformations, and the relational graph from which GOLD ontology-mapped views are derived.

---

## 2. From Profiles to Table Context & Naming

**In plain terms:** Before any AI touches a table, this stage does the boring-but-critical prep: it reshapes each table's profile into a tidy packet the prompt builder can use, and it locks in the table's class name and IRI (IRI/URI — a globally-unique name for a thing) using plain code, not a guess from a language model.

**Inputs → Outputs:** one `TableProfile` per relational table → one `TableContext` packet (plus a deterministic class name/URI computed on the side) that feeds the per-table prompt for the generation LLM (gen-LLM).

This stage sits between profiling (which produced one `TableProfile` per relational table) and the per-table LLM generation loop. Its job is purely **deterministic preparation**.

It does two things:

- Converts a raw `TableProfile` into a `TableContext` — the structured packet the prompt assembler hands to the gen-LLM.
- Fixes the **class name and URI** for every table *before any LLM sees the table*.

As the module docstring of `semantic_mapper/ontology/iterative/context_builder.py` puts it, the deterministic URI "is the single most important architectural decision — it eliminates the entire family of 'wrong class name' bugs that plague the single-pass generator."

No LLM is invoked anywhere in this stage. Every decision here is a pure function of the profile and a handful of numeric knobs.

### 2.1 Inputs and Outputs

| | Type | Source / Sink |
|---|---|---|
| **Input** | `TableProfile` (`semantic_mapper/db/models.py`) | one per table, from the SILVER profiling stage |
| | `processing_index`, `total_tables` | the table's 1-based position in topological order, and the count |
| | `skipped_tables: Set[str]` | tables the generator has decided not to map (see §2.6) |
| | numeric knobs | `max_distinct_for_enumeration`, `max_distinct_for_open_class`, and the three `ONTOLOGY_CATEGORICAL_*` settings |
| **Output** | `TableContext` (`semantic_mapper/ontology/iterative/models.py`) | consumed by the prompt assembler / gen-LLM |

#### What's inside a `TableProfile`

A `TableProfile` carries:

- `table_name`
- `row_count`
- `columns: Dict[str, ColumnProfile]`
- `foreign_keys: List[Dict[str, Any]]`

Each `ColumnProfile` carries:

- `name`, `inferred_type`, `is_nullable`, `is_primary_key`
- `null_rate`, `distinct_count`
- `top_values` (a list of dicts)
- `detected_formats`

#### The conversion function

The conversion is performed by `build_table_context(profile, processing_index, total_tables, …)` (context_builder.py:319). It returns a `TableContext` whose `columns` is a list of `ColumnContext` objects — one per column — plus an `explicit_fks` list and the topological position fields.

The two output models are defined in `models.py`:

- **`ColumnContext`** (models.py:9) flattens each column into: `table_name`, `col_name`, `inferred_type`, `is_pk`, `is_fk`, `fk_ref_table`, `fk_ref_skipped`, `is_nullable`, `null_rate`, `distinct_count`, `top_values` (now plain strings, max 10), `detected_formats`, `is_categorical`, `categorical_values`, and `cardinality_hint`.
- **`TableContext`** (models.py:27) holds `table_name`, `row_count`, the `columns` list, `explicit_fks` (`[{column, ref_table}]`), `processing_index` (1-based topo position), and `total_tables`.

### 2.2 TABLES — Naming and class-URI derivation

**In plain terms:** the table's name alone decides its OWL class name (a class is a *type of thing*, like `Patient`) and its full URI. Same input always gives the same name — no AI in the loop.

Class identity is set deterministically from the **table name** by:

- `derive_class_local_name` (context_builder.py:110) — the short name (e.g. `Patient`).
- `derive_class_uri` (context_builder.py:129) — just `f"{base_iri}{derive_class_local_name(table_name)}"`, i.e. the base IRI plus the short name.

#### The core transform

The core transform is `_class_local_from_table` (context_builder.py:64):

1. Strip a leading `raw_` prefix (case-insensitive regex `^raw_`).
2. Strip a leading `d_` prefix (the dimension-table marker, `^d_`).
3. Split on underscores, drop empty parts.
4. Singularize each part via `_singularize`, capitalise it, and concatenate into PascalCase.

Worked examples from the docstring:

| Table name | Class local name |
|---|---|
| `raw_labevents` | `Labevent` |
| `raw_d_icd_diagnoses` | `IcdDiagnosis` |
| `raw_inputevents_mv` | `InputeventMv` |
| `raw_d_labitems` | `Labitem` |
| `raw_countries` | `Country` |
| `patients` | `Patient` |

#### Singularization rules

`_singularize` (context_builder.py:16) prefers the `inflect` library. If `inflect` is not importable it falls back to a rule table:

- `ies → y`
- `ses`/`xes`/`ches`/`shes` drop `es`
- `oes → o` (except `shoes`)
- `ves → f`
- trailing `s` dropped (except `ss`)

Crucially, **words of length ≤ 3 are never singularized** (lines 22–23). Short tokens like `mv` or `ias` are likely abbreviations that `inflect` would mangle — this is why `mv` survives in `InputeventMv`.

#### PK-driven name overrides

**In plain terms:** sometimes a table is named after a *measurement* but is really *about* an entity. This override catches that case and renames the class to the entity.

Before the main loop, a run may install per-table overrides via `set_class_name_overrides` (context_builder.py:58), populated by `compute_class_name_overrides(profiles)` (context_builder.py:77).

The motivating case: a dimension table named after a *measure* (e.g. a per-country medal-tally table literally named `medals`) whose single primary key actually denotes a different entity (`country_code`).

The override fires only when **all** of these hold:

- the table has exactly one PK column;
- that PK matches `^(.+?)_(?:code|id)$`, yielding stem `X`;
- `X` is not a surrogate stem — `_SURROGATE_ENTITY_STEMS = {"row","record","entry","line","seq","rownum",""}` (line 55);
- the table also carries a **human-readable sibling column literally named `X`** (line 102);
- the resulting PascalCased entity class differs from the table-derived name.

When satisfied, `overrides[table_name] = entity_class` (so `medals` → class `Country`).

The docstring spells out the deliberate non-firing guards:

- `raw_patients` — PK `row_id` → stem `row`, which is a surrogate, so no override.
- `raw_caregivers` — PK `cgid` → no `_id`/`_code` split, and no `caregiver` sibling, so no override.

Both keep their table-derived names. The override map defaults to empty, so absent this setup all behaviour is the plain table-derived path. `derive_class_local_name` consults the override map first (`_CLASS_NAME_OVERRIDES.get(table_name) or _class_local_from_table(...)`).

#### Important boundary

This stage **derives** class names/URIs but `build_table_context` itself does **not** stamp them onto the `TableContext` — it carries only `table_name`. The deterministic URI functions are exposed for the generator/prompt-assembler to call alongside the context.

### 2.3 FIELDS / COLUMNS — the cardinality-hint system

**In plain terms:** each column gets a one-word "hint" telling the generator how to model it — is it a closed list of categories, free text, a boolean flag, an identifier, and so on. The hint is computed purely from the column's SQL type and how many distinct values it has.

Each column gets a `cardinality_hint` from `_compute_cardinality_hint` (context_builder.py:239), which classifies a column into one of five hint strings or `""` (no hint).

The decision keys off:

- the *base* SQL type — `inferred_type` upper-cased, truncated at the first `(` to drop length/precision; and
- the column's `distinct_count` (`d`).

#### The classification rules, in order

1. **PK or FK → `""`** immediately (line 259): identifier columns get no vocabulary hint here.
2. **Date/time types** (`DATETIME`, `DATE`, `TIMESTAMP`, `TIME`) → `""`.
3. **`FLAG`** — integer/boolean/bit types (`_FLAG_TYPES`) with exactly `d == 2` distinct values (line 268). A two-valued numeric/boolean column is a boolean flag.
4. **Text types** (`_TEXT_TYPES`: `VARCHAR/CHAR/TEXT/…/STRING`):
   - **`CATEGORICAL_CLOSED`** when `d ≤ max_distinct_for_enumeration` AND `top_vals` is non-empty AND the values do **not** look descriptive (`_values_look_descriptive`, §2.4) AND `is_closed_vocabulary(...)` passes (§2.5). This is the only hint that becomes `is_categorical = True`.
   - else **`FREE_TEXT`** when `d > max_distinct_for_open_class`.
   - else **`CATEGORICAL_OPEN`** when `d > max_distinct_for_enumeration` (i.e. between the enumeration and open-class thresholds).
   - else `""`.
5. **Numeric types** (`_NUMERIC_TYPES`) with `d > max_distinct_for_open_class` → **`IDENTIFIER`** (a high-cardinality numeric column behaving like a surrogate key).
6. Anything else → `""`.

#### Thresholds and value sampling

The default thresholds are:

- `max_distinct_for_enumeration = 20`
- `max_distinct_for_open_class = 100`

(both from the `build_table_context` signature, lines 323–324).

`top_vals` come from `_top_values_as_strings` (context_builder.py:299), which reads up to 10 entries from the profile's `top_values` list-of-dicts (`item["value"]`).

#### What each hint steers toward

The hint is the only place the generator learns *how to model* a column:

- `CATEGORICAL_CLOSED` → a candidate `owl:Class` / enumeration (a class is a type; an enumeration is a closed list of allowed values).
- `FLAG`, `FREE_TEXT`, `CATEGORICAL_OPEN`, `IDENTIFIER` → steer toward `DatatypeProperty` shaping (a DatatypeProperty is an attribute whose value is a literal, e.g. a number or string).

### 2.4 VALUES — the class-vs-literal decision

**In plain terms:** the big modelling fork — should a low-variety column become its own *enumeration class* (a reusable category type) or stay a *plain literal value* on a property? Two predicates decide, both looking at the column's actual sampled values.

The pivotal modelling choice — does a low-cardinality column become an **enumeration class** or a **literal-valued datatype property**? — is split across two predicates, both driven by the column's actual sampled `top_values`.

#### Predicate 1 — does the value *look descriptive*?

**`_values_look_descriptive(values, distinct_count, non_null_count)`** (context_builder.py:207) returns `True` (→ *not* a vocabulary, keep as literal) when the low-distinct values are descriptive attributes rather than recurring labels.

It flags three shapes:

- **URL-valued**: ≥ 50% of values start with `http://` / `https://` (a venue URL, not a category).
- **JSON/list-shaped cells**: any value starting with `[` and ending with `]` — would otherwise mint nonsensical compound members like `"Bobsleigh, Luge, Skeleton"`.
- **Near-unique multi-word free text**: `distinct/non_null ≥ 0.9` AND ≥ 50% of values contain a space (e.g. a venue *name*).

It deliberately *keeps* short recurring labels — including multi-word ones like `"Head Coach"` — because they recur and so are not near-unique.

#### Predicate 2 — is it a *genuine controlled vocabulary*?

**In plain terms:** a real category set is a handful of values that show up again and again across many rows. This gate rejects columns that are too sparse or too unique to anchor a class.

**`is_closed_vocabulary(distinct_count, null_rate, non_null_count, *, max_distinct, …)`** (context_builder.py:178) is the proportional gate. It returns `True` only when a column is a *genuine controlled vocabulary*: a few distinct values that **recur across many non-null rows**.

It fails (→ literal) on any of:

- `distinct_count <= 0` or `distinct_count > max_distinct`;
- `null_rate > max_null_rate` (the **primary, safe signal**);
- `non_null_count < min_support`;
- `(distinct_count / non_null_count) > max_uniqueness`.

#### The three configurable thresholds

The thresholds are configured by three module defaults, surfaced as `ONTOLOGY_CATEGORICAL_*` settings and threaded through `build_table_context` as `max_null_rate_for_class` / `min_support_for_class` / `max_uniqueness_for_class`:

| Default constant | Value | Meaning |
|---|---|---|
| `DEFAULT_MAX_NULL_RATE_FOR_CLASS` | `0.90` | `> 90%` null → literal |
| `DEFAULT_MIN_SUPPORT_FOR_CLASS` | `0` | absolute non-null floor; `0` = **off** |
| `DEFAULT_MAX_UNIQUENESS_FOR_CLASS` | `1.0` | distinct/non-null cap; `1.0` = **off** |

#### Why null-rate is primary and the other two ship disabled

The design comment (lines 165–175) is explicit about *why* null-rate is the primary knob and the other two ship off:

- A mostly-null column carries too little signal to anchor a class. Their example `originalsite`: `distinct=1` but 100% null, so it must stay a literal.
- A support floor or uniqueness cap would wrongly demote a small but fully-populated dimension — e.g. a 10-row lookup with 10 distinct categories.

`non_null_count` is computed in the caller as `row_count * (1.0 - null_rate)` (context_builder.py:273).

#### When the categorical values get carried through

When (and only when) the hint comes out `CATEGORICAL_CLOSED`, `build_table_context` sets `is_categorical = True` and copies `top_vals` into `categorical_values` (line 382). For every other hint, `categorical_values` is empty.

### 2.5 PRIMARY KEYS

**In plain terms:** primary keys are passed through as-is and deliberately kept out of the category/flag machinery — a PK is an identifier, never a vocabulary. Its only naming influence is the override in §2.2.

- Primary keys are recognised from `ColumnProfile.is_primary_key` and surfaced verbatim onto each `ColumnContext.is_pk` (context_builder.py:373).
- PK columns are explicitly excluded from the cardinality-hint machinery — `_compute_cardinality_hint` returns `""` for any `is_primary_key` column (line 259) — so a PK never becomes a vocabulary class or flag.
- The PK also drives **class naming** indirectly, via `compute_class_name_overrides` (§2.2), which is the only place a PK's *name* changes the emitted class.

### 2.6 FOREIGN KEYS and skipped-table propagation

**In plain terms:** foreign keys become candidate links between classes (an ObjectProperty is a link from one entity to another, like `hasPatient`). This step also flags FKs that point at tables the generator decided to skip, and demotes "name" columns that merely shadow a coded FK.

#### Where FK data comes from

FK information is read **entirely** from `TableProfile.foreign_keys`, which by this stage already contains every FK the `MySQLInspector` could find — including the three inference strategies. `build_table_context` accepts **no** dataset-specific override map (docstring lines 336–340).

The list is a list of dicts, and both key styles are tolerated:

- SQLAlchemy-style keys: `constrained_column`, `referred_table`
- shorthand keys: `column`, `ref_table`

#### Per-column FK detection

Per column, `_is_fk(col_profile, table_profile)` (context_builder.py:134) scans `foreign_keys` and returns `(is_fk, ref_table)` when a constrained column matches the column's name and a referred table is present.

These populate `ColumnContext.is_fk` and `fk_ref_table`. A column-level FK set `_fk_cols` (lowercased constrained columns) is also assembled for the shadowing check below.

#### Table-level FK summary

The whole table's FKs are additionally summarised into `TableContext.explicit_fks` (context_builder.py:386) — a normalised `[{column, ref_table}]` list with `None`/empty entries dropped. This is what the prompt assembler uses to ask the LLM for object properties (links between classes) between classes.

#### Skipped-table propagation

**In plain terms:** if a FK points at a table that won't become a class, the link can't be an ObjectProperty — there'd be no class on the other end. This flag warns downstream so it degrades the reference instead.

`build_table_context` receives `skipped_tables` and, for each FK column, sets:

```
fk_ref_skipped = bool(is_fk and fk_ref_table and fk_ref_table in _skip)
```

(context_builder.py:352).

This flag rides on the `ColumnContext` so downstream generation knows a FK points at an entity that *will not exist* as a class. The reference must be degraded (e.g. to a literal / dangling property) rather than emitted as an object property into a non-existent range (rdfs:range — the type of value a property points at). The set is copied defensively at line 341.

#### FK-shadow demotion

**In plain terms:** if you have both `venue` (a name) and `venue_code` (a FK), the name column shouldn't spawn its own competing category set — it's just a readable label for the entity the code already links to.

Even a column that passed every vocabulary test can be demoted if it is merely the human-readable form of a coded FK.

`_shadows_coded_fk(col_name, fk_columns)` (context_builder.py:310) returns `True` when a sibling `{col}_code` or `{col}_id` column is itself an FK — e.g. a `venue` name column alongside a `venue_code` FK to a `Venue` table.

In that case `build_table_context` (lines 365–367) clears the hint and sets `is_categorical = False`. The column becomes a descriptive label (`DatatypeProperty`) on the referenced entity instead of minting its own competing vocabulary class.

### 2.7 Output filename — `ontology_filename`

**In plain terms:** this just builds the `.owl` output filename from the project, generation strategy, model name, and date — and avoids overwriting an earlier same-day run.

Filename construction lives separately in `semantic_mapper/ontology/naming.py`. `ontology_filename(out_dir, project, strategy, model, when=None)` (naming.py:36) builds the convention `"<project>_<strategy>_<model-short>_<DDMONYY>.owl"`, e.g. `mimic_rigor_gpt-4o_06JUN26.owl`.

Concretely:

- **`project`** is sanitised (non-alphanumerics → `-`, trimmed; fallback `"ontology"`).
- **`strategy`** is the generator marker, `rigor` or `full`.
- **`model`** is condensed by `model_short_name` (naming.py:20):
  - `:` / `/` → `-`
  - drop `-instruct` / `-chat` / `-it`
  - strip quantisation tags (`-q4_K_M`, `-q8_0`) and `-fp16`
  - collapse the rest

  So `qwen2.5:72b-instruct-q4_K_M` → `qwen2.5-72b`.
- **`when`** defaults to `datetime.now()` and is formatted as `%d%b%y` upper-cased (`06JUN26`).
- A numeric suffix `_2`, `_3`, … is appended **only** when an identically-named file already exists, to avoid clobbering an earlier same-day run with the same project/strategy/model (lines 52–55).

### 2.8 Where this connects

The `TableContext` produced here is the sole structured input to the per-table prompt the gen-LLM receives. The `derive_class_uri` result fixes the subject class so the judge-LLM and deterministic post-passes can validate generated fragments against a known class identity.

Everything in this stage is reproducible from the profile alone. The LLM never gets to choose a class name, a class-vs-literal call, or an FK target — it only fills in labels, descriptions, and property shapes around the scaffolding decided here.

---

## 3. Dependency Ordering & Retrieval (Topology + RAG)

**In plain terms:** Before the gen-LLM ever looks at a table, ForgeMesh decides (a) *what order* to process tables in, and (b) *what previously-declared vocabulary* to show the LLM as context. Both halves exist so that when a table with a foreign key is processed, the thing it points at has already been turned into a class and is available to reuse.

**Inputs → Outputs:** Takes the list of `TableProfile` objects from the SILVER profiling stage; produces (1) a re-ordered table list (referenced tables first) and (2) a growing semantic index that feeds context into each gen-LLM prompt.

### 3.0 Where this lives

Two small, dependency-light modules implement this stage:

- `semantic_mapper/ontology/iterative/topology.py` — `fk_topological_sort` and its helper `_break_cycles`. These order tables so that referenced (dimension/parent) tables are emitted *before* the fact tables that point at them.
- `semantic_mapper/ontology/iterative/embedding_index.py` — `AccumulatedClassIndex`, an incrementally-built semantic index over OWL classes (an OWL class is a *type* of entity, like `Patient`) and abstract properties. It supplies retrieval-augmented (RAG — fetching relevant already-known context to put in the prompt) context to the gen-LLM.

Both are orchestrated from `IterativeOntologyGenerator` in `semantic_mapper/ontology/iterative/generator.py`:

- the topological sort runs at line 149;
- the index is constructed at line 172 and queried/updated inside the per-table loop.

The design intent, stated in the topology docstring (lines 5–8), is that *"when the Gen-LLM processes a table it can reference already-declared classes for every FK column."* Ordering and retrieval are two halves of one mechanism:

- **Ordering** guarantees the parent class exists by the time a child is processed.
- **Retrieval** surfaces that parent class into the prompt.

### 3.1 Inputs and outputs

#### Input to the whole stage

The input is the list of `TableProfile` objects produced by the SILVER profiling stage (`semantic_mapper/db/models.py`, `TableProfile`). Each profile carries:

| Field | Type | Role here |
|---|---|---|
| `table_name` | `str` | Node identity in the FK graph |
| `row_count` | `int` | Skip threshold + cycle-break tie-breaker |
| `columns` | `Dict[str, ColumnProfile]` | Source of FK columns for retrieval |
| `foreign_keys` | `List[Dict[str, Any]]` | Raw FK metadata that drives ordering |

#### Output of `fk_topological_sort`

A re-ordered `List[TableProfile]` (referenced tables first), which the generator iterates over (`for i, profile in enumerate(ordered, ...)`, line 177).

#### Output of `AccumulatedClassIndex`

The index has no single "output" — it is **mutable state** that grows as each table is merged. Its retrieval methods return lists of `AccumulatedClassSummary` / `AccumulatedAbstractPropertySummary` (defined in `semantic_mapper/ontology/iterative/models.py`) that are injected into the gen prompt.

### 3.2 FK topological ordering — `fk_topological_sort`

#### How FK metadata drives the ordering

For every profile, the sort iterates `profile.foreign_keys` and reads the referenced table out of each FK dict (lines 51–60):

```python
ref_table = fk.get("referred_table") or fk.get("ref_table")
```

The dual-key lookup matters because the two keys come from different sources:

- `referred_table` — the key used for FKs reflected from the database via SQLAlchemy.
- `ref_table` — the key used for *inferred* FKs (the heuristic FK discovery in `semantic_mapper/db/inspector.py`, issue #70).

`fk_topological_sort` treats both identically, so inferred and declared FKs both influence ordering. The same `referred_table or ref_table` fallback is mirrored in `context_builder._is_fk`, keeping the two stages consistent.

#### Edges that are deliberately NOT added to the graph

Three kinds of FK edge are skipped:

- **Self-references** (`ref_table == profile.table_name`) — skipped (line 54), so a table that FKs to itself is not blocked behind itself.
- **FKs pointing outside the profile set** — if `ref_table not in table_names`, the edge is dropped (lines 56–58). This makes the sort robust to partial schemas.
- **FKs to skipped tables** — `skip_tables` are removed from `profiles` up front (line 41), so any edge into a skipped table also falls into the "outside the set" case and is ignored.

For that last case, the generator passes the set of tables whose `row_count <= ONTOLOGY_MIN_ROWS_FOR_GENERATION` as `skip_tables` (generator.py lines 135–149), with the explicit comment that *"FK edges pointing to a skipped table are ignored, so tables that depend on a skipped table are not blocked."*

#### Edge direction and graph construction

The graph is directed, with the semantics **A → B means "A has an FK to B", i.e. A depends on B** (docstring lines 25–26). Two adjacency maps are built (lines 47–60):

- `deps[A]` = the set of tables A depends on (the tables A has FKs to).
- `reverse_deps[B]` = the set of tables that depend on B (B's dependents).

The **in-degree** of a table is the number of tables it depends on: `in_degree[name] = len(deps[name])` (line 63). A table with in-degree 0 has no FK dependencies — these are the root dimension/reference tables (`patients`, `d_*` dictionaries, per the module docstring), and they are emitted first.

#### Kahn's BFS

The sort is **Kahn's algorithm** (a breadth-first topological sort), lines 65–85:

1. Seed a `deque` with every table whose in-degree is 0, sorted by table name for deterministic output (lines 66–71).
2. Pop a table, mark it visited, append its `TableProfile` to `result`.
3. For each dependent in `reverse_deps[name]` (iterated in sorted order, line 82), decrement its in-degree; when it reaches 0, enqueue it.

Because a table is only enqueued once **all** its dependencies have been emitted, every FK target precedes every FK source. By the time the generator processes a fact table, the classes for all of its parent tables already exist in the accumulating graph and in the `AccumulatedClassIndex` — exactly the precondition RAG relies on.

The deterministic tie-breaking (sorting both the seed queue and `reverse_deps` by name) means the same schema always yields the same processing order. That keeps generated URIs (IRI/URI — a globally-unique name for a thing) and prompts reproducible.

#### Cycle-breaking — `_break_cycles`

**In plain terms:** Circular FK chains (A → B → A) can't be put in a strict order, so the algorithm finds them and snips one edge to make progress — affecting *order only*, never the actual ontology.

Circular FK chains cannot be linearised by Kahn's algorithm: any table still unvisited after the main loop is part of a cycle. The sort detects this as `remaining = [name for name in table_names if name not in visited]` (line 88) and, if non-empty, hands those tables to `_break_cycles` (lines 90–96), logging a warning naming every table involved.

`_break_cycles` (lines 101–153) drains the cyclic set by repeatedly choosing an **entry** table and severing its back-edges:

1. Pick the entry as `min(remaining, key=lambda n: table_map[n].row_count)` — **the table with the lowest row count** (lines 117–121). The heuristic is that the smaller table is more likely the dimension/lookup side, so breaking its outgoing dependency loses the least structure.
2. Remove every dependency of that entry that is still in the remaining set: discard it from both `deps[entry]` and `reverse_deps[dep]`, logging `"Breaking FK cycle: removing back-edge %s → %s"` for each (lines 122–129).
3. Recompute local in-degrees over the remaining set and run Kahn's BFS again (lines 132–151).

The outer `while` repeats until the remaining set is empty, so multiple independent cycles (or nested cycles) are each broken in turn. The broken-cycle tables are appended after the acyclic prefix (line 91), so the final order is:

1. all cleanly-sortable tables first, then
2. formerly-cyclic tables in their post-break BFS order.

**Which edge is dropped, summarised:** for each cycle, the algorithm drops the outgoing FK edge(s) of the *lowest-row-count* table in that cycle. Dropping an edge here affects only **processing order**, not the ontology — the FK is still present in `profile.foreign_keys` and is still materialised downstream. The cycle break merely decides which table's class is declared first.

> **Honestly-flagged trade-off:** when a child is processed before its parent due to a broken edge, retrieval simply won't yet have that parent class to offer — a deliberate, logged trade-off rather than a correctness failure.

### 3.3 Retrieval-augmented context — `AccumulatedClassIndex`

#### The embedding model

The index uses **`all-MiniLM-L6-v2`** (`_MODEL_NAME`, embedding_index.py line 21), loaded lazily via `sentence_transformers.SentenceTransformer` in `_load_model` (lines 45–49) — i.e. the model is only instantiated on first `add`/`retrieve`, not at construction.

The docstring notes this model is *"already required by `SemanticMatcher` — no new deps."* In practice the generator constructs the index with whatever `SEMANTIC_MATCHER_MODEL` is configured, defaulting to `all-MiniLM-L6-v2` (generator.py lines 172–174), so the ontology generator and the semantic matcher share one embedding model.

#### What is encoded, and how

The index maintains two parallel stores, each a Python list plus a NumPy embeddings matrix:

- **Classes** — `self._classes: List[AccumulatedClassSummary]` and `self._embeddings` (shape `(N, D)`).
- **Abstract (domain-less) properties** — `self._abstract_props: List[AccumulatedAbstractPropertySummary]`, `self._abstract_embeddings` (shape `(M, D)`), plus a `_indexed_prop_uris` set to track membership. (A property here is a relationship or attribute; "abstract / domain-less" means it has no fixed owning class yet — `rdfs:domain` is the class a property is allowed to be used on.)

**Class encoding.** An `AccumulatedClassSummary` carries `local_name`, `label`, `uri`, and `source_tables`. It is encoded into a single string by `_encode_text` (lines 51–54):

```python
f"{cls.label} — tables: {tables}"   # tables = comma-joined source_tables, or "unknown"
```

So a class is embedded by its human label plus the relational tables it was derived from — meaning retrieval is sensitive both to naming and to provenance. Classes are added either:

- one at a time (`add`, lines 60–70), or
- in batches (`add_all`, lines 72–84, which encodes all texts in one `model.encode` call and is the path the generator uses).

New embeddings are appended to the matrix with `np.vstack`.

**Abstract-property encoding.** An `AccumulatedAbstractPropertySummary` carries `local_name`, `label`, `uri`, `range_uri`, and `prop_type` — where `prop_type` is either:

- `"DatatypeProperty"` (an attribute whose value is a literal, e.g. a number or string), or
- `"ObjectProperty"` (a link from one entity to another).

The `range_uri` records the property's `rdfs:range` (the type of value a property points at — a class, or a literal type like `xsd:string`). These are registered via `add_all_abstract_properties` (lines 116–131) and encoded as:

```python
f"{p.label} {p.prop_type} range={p.range_uri}"
```

#### How retrieval works

`retrieve(table_name, table_label="", top_k=10)` (lines 86–114) finds the classes most similar to the *current* table:

1. Build a query string `f"{label} derived from {table_name}"`, where `label` falls back to a title-cased version of the table name (`table_name.replace("_", " ").title()`) when no explicit label is given (lines 97–98).
2. Encode the query, L2-normalise both the stored matrix and the query vector (guarding against zero-norm with a `1e-9` floor, lines 102–109), and compute **cosine similarity** as the dot product of the normalised vectors (line 111).
3. Return the `top_k` classes by descending similarity via `np.argsort(...)[::-1][:k]`, where `k = min(top_k, len(self._classes))` (lines 112–114). An empty index returns `[]` immediately (lines 93–94).

**Abstract-property retrieval** is handled by `_retrieve_from_abstract` (lines 133–160) with the same cosine-similarity machinery, plus an optional `prop_type_filter`. When filtering (e.g. to `ObjectProperty`), it slices the embeddings matrix down to the matching indices before scoring (lines 146–148). Two public wrappers expose this:

| Wrapper | Default `top_k` | Returns |
|---|---|---|
| `retrieve_abstract_properties(query, top_k=8)` | 8 | all abstract property types |
| `retrieve_abstract_object_properties(query, top_k=3)` | 3 | `ObjectProperty` only |

#### How this feeds the gen-LLM (RAG)

Inside the per-table loop (generator.py lines 177–226), for each table in topological order:

1. **Retrieve candidate classes.** `retrieved = index.retrieve(profile.table_name, top_k=top_k)` is called, where `top_k` comes from `ONTOLOGY_GEN_TOP_K_CLASSES` (default 10, line 114). These are the candidate parent/related **classes**.
2. **Retrieve abstract properties by table name.** `retrieved_abstract = index.retrieve_abstract_properties(query=profile.table_name.replace("_"," "), top_k=8)`.
3. **Supplement per FK column** (lines 208–216). For every column where `col.is_fk and col.fk_ref_table and not col.fk_ref_skipped`, it calls `retrieve_abstract_object_properties(query=col.fk_ref_table.replace("_"," "), top_k=3)` and merges in any new URIs (de-duplicated via `_seen_abstract_uris`).

   *Why query by the referenced table name?* The inline comment explains: it surfaces `hasAdmission`, `hasPatient`, `hasIcustay`, etc. even when the current table's name doesn't semantically match those abstract parents.

   This is where ordering and retrieval interlock most tightly: because the FK target table was processed earlier, its abstract object-property already exists and can be retrieved by that referenced table's name.
4. **Assemble the prompt.** All of this flows into `build_gen_prompt(..., retrieved_classes=retrieved, ..., abstract_properties=retrieved_abstract, ...)` (lines 218–226), giving the gen-LLM the already-declared vocabulary it should reuse for subclassing and FK ranges.

#### How the index grows

After a table's fragment is merged (generator.py lines 326–340), the index is updated so the next table can retrieve the just-declared vocabulary:

- `index.add_all(merge_result.new_classes)` registers the new classes.
- `index.add_all_abstract_properties(new_abstract)` registers new abstract properties.

Thus the "accumulated" context monotonically grows in lockstep with the topological walk: dimension classes enter the index first and are available as RAG context for every fact table that follows. The full class list is also read directly as `index._classes` and passed to the judge-LLM for duplicate detection (line 266).

### 3.4 How TABLES / FIELDS / VALUES / PRIMARY KEYS / FOREIGN KEYS are handled at this stage

**In plain terms:** This stage is about *sequencing and context selection*, not about emitting ontology axioms — so most relational concepts pass through only as signals, and FOREIGN KEYS do all the real work.

#### TABLES

The unit of both ordering and retrieval. Each `TableProfile` is one node in the FK graph and (after merge) one or more entries in the class index.

- Tables below the `ONTOLOGY_MIN_ROWS_FOR_GENERATION` row-count threshold become `skip_tables` and are removed from the sort entirely (generator.py lines 135–149).
- Processing order is fully determined here: root/dimension tables first, fact tables last, formerly-cyclic tables appended after cycle-breaking.

#### FIELDS / COLUMNS

Not embedded or sorted on directly. Columns enter this stage only through the *FK columns* of `table_ctx.columns`: each FK column triggers an extra abstract-object-property retrieval keyed on its referenced table (generator.py lines 208–216). Column-level semantics (datatype vs class, enumeration) are decided in the context-building and generation stages, not here.

#### VALUES

Not used by topology or by the embedding index at all. No cell values are read or embedded in this stage. (The class-encoding text uses labels and `source_tables`, never data values.) The only value-adjacent quantity consulted is `row_count`, used both as the skip threshold and as the cycle-breaking tie-breaker — and that is a *cardinality*, not a value.

#### PRIMARY KEYS

PKs do **not** participate in the FK graph or in ordering. (PK-driven class naming happens earlier, via `compute_class_name_overrides` at generator.py lines 109–112, before the sort.) Topology cares only about FK edges; a table's PK influences which entity it becomes, not when it is processed.

#### FOREIGN KEYS

The central driver of this stage. FKs:

- define every edge of the dependency graph (`deps`/`reverse_deps`);
- determine in-degree and therefore processing order;
- are the thing dropped to break cycles;
- and — at retrieval time — seed the per-FK abstract object-property queries that surface the correct parent relations into the gen prompt.

Both reflected (`referred_table`) and inferred (`ref_table`) FKs are honoured; self-references, FKs to tables outside the profile set, and FKs into skipped tables are ignored. Crucially, breaking an FK edge for cycle-resolution affects only *ordering*; the FK itself remains in `profile.foreign_keys` and is materialised by downstream stages.

---

## 4. The Generation Prompt — Modelling Rules

**In plain terms:** the LLM that writes the ontology never touches the database. Instead, it gets one carefully built text prompt describing a single table, plus a long list of rules it must obey. Those rules — not the model's free choice — are where most of the system's correctness lives.

**Inputs → Outputs:** a structured, retrieval-augmented bundle describing one table → one plain-text prompt string that constrains the Gen-LLM (the LLM that generates the ontology) to emit valid OWL Manchester Syntax (a human-readable text format for writing OWL ontologies) for exactly that one table.

The Gen-LLM never sees the raw database. Everything it knows about a table arrives through a single, deterministically-assembled prompt string built by `build_gen_prompt` in `semantic_mapper/ontology/iterative/gen_prompt.py`.

This section walks that function section by section and through every numbered rule, because the prompt *is* the modelling contract. Most of the system's correctness comes not from the LLM's discretion but from rules phrased as **HARD REJECTION** conditions — conditions the downstream Judge-LLM (a second LLM that reviews the generated output) and deterministic post-passes enforce.

### 4.1 Inputs and output

`build_gen_prompt` (lines 21–30) takes a structured, retrieval-augmented bundle and returns one plain string. Its arguments:

| Argument | Type / source | Role |
|---|---|---|
| `table_ctx` | `TableContext` | The one table being modelled this iteration: name, `row_count`, `processing_index`/`total_tables`, and its `columns` (each a column-profile object with `is_pk`, `is_fk`, `fk_ref_table`, `fk_ref_skipped`, `is_nullable`, `null_rate`, `cardinality_hint`, `categorical_values`, `top_values`, `is_categorical`, `detected_formats`, `inferred_type`, `col_name`). |
| `retrieved_classes` | `List[AccumulatedClassSummary]` | Top-k already-declared classes from the vector/index over the accumulating ontology (`uri`, `label`, `source_tables`). |
| `deterministic_uri` | `str` | The EXACT URI (IRI/URI — a globally-unique name for a thing) the LLM must use for this table's primary class — computed outside the LLM, not chosen by it. |
| `base_iri` | `str` | The ontology namespace, e.g. `http://example.org/mimic#`. |
| `revision_instructions` | `str` | If non-empty, appended as a revision section (used on retry). |
| `existing_property_locals` | `List[str]` | Property local-names already minted, to force class-scoping on collision. |
| `abstract_properties` | `List[AccumulatedAbstractPropertySummary]` | Existing domain-less "abstract parent" properties available as `SubPropertyOf:` targets. |
| `max_rows_for_enumeration` | `int` (default `1000`) | Threshold above which a table is treated as an entity class, never a controlled vocabulary. |

#### How the output is assembled

The output is a string built from a `sections` list joined by blank lines (`return "\n\n".join(sections)`, line 394).

The module docstring (lines 1–8) describes it as a "four-section prompt (task context, accumulated classes, table profile, rules)" that constrains the LLM to emit **valid OWL Manchester Syntax for exactly one table at a time**.

A companion helper, `rebuild_with_revision` (lines 397–410), re-issues the same prompt with the rejected output and the Judge's instructions appended for the next attempt.

### 4.2 Section 1 — Task context and the entity-class enumeration guard

The opening section (lines 46–61) names the role ("expert ontology engineer building an OWL ontology table by table"), states `Table {processing_index} of {total_tables}`, the table name and row count, the namespace, and the hard output discipline: "Output ONLY valid OWL Manchester Syntax. No prose, no markdown fences, no explanations."

#### The entity-class enumeration guard

**In plain terms:** for big tables, this stops the model from trying to list every row as a named thing.

The **entity-class enumeration guard** (`entity_guard`, lines 47–53) is the large-table protection. When `table_ctx.row_count > max_rows_for_enumeration` (default 1000), the prompt prepends:

> `ENTITY TABLE — {row_count} rows > {threshold} threshold: this is an ENTITY class, NOT a controlled vocabulary. Do NOT generate owl:NamedIndividual blocks, owl:equivalentClass, or owl:oneOf for the primary class <{deterministic_uri}>.`

Here `owl:NamedIndividual` is a specific named instance, `owl:equivalentClass` declares two classes are the same set, and `owl:oneOf` (a closed, enumerated list of allowed values) would spell out every member. This stops the model from trying to enumerate, say, a million patients as named individuals or as a `oneOf` set. Below the threshold the guard string is empty and omitted.

#### Latent bug worth flagging

A bug worth flagging to anyone navigating this file: the f-string at lines 55–57 contains literal `{{...}}` braces around `table_ctx.table_name` and `table_ctx.row_count`.

As a result, the table name and row count are emitted as the literal text `{table_ctx.table_name}` / `{table_ctx.row_count:,}` rather than the interpolated values. The `entity_guard` and `base_iri` substitutions on adjacent lines *are* correctly interpolated.

### 4.3 Section 2 — Accumulated classes, existing properties, and abstract parents

**In plain terms:** this section is the prompt's memory of what the ontology already contains, so the LLM reuses existing classes and properties instead of redeclaring them.

#### Already-declared classes

Lines 64–75: if `retrieved_classes` is non-empty the prompt lists each as `Class: <uri>   Annotations: rdfs:label "..."@en   (source: <tables>)` under the banner "CLASSES ALREADY IN THE ONTOLOGY — do NOT redeclare; you MAY reference them as ranges/superclasses". (`rdfs:label` is a human-readable name for a thing.) If empty, it states "none yet — this is the first table."

#### Already-declared properties

Lines 77–83: when `existing_property_locals` is present, each is printed as `{base_iri}{local}` under "PROPERTIES ALREADY IN THE ONTOLOGY — do NOT reuse these local names without class-scoping." This list is what Rule 11 keys off.

#### Abstract parents (split by type)

Abstract parents are domain-less "parent" properties that more specific properties can inherit from. They are split by type (lines 85–112):

- **`dtype_abstracts`** (non-`ObjectProperty` — i.e. DatatypeProperty parents; a DatatypeProperty is an attribute whose value is a literal like a number or string) are listed under "ABSTRACT DATATYPEPROPERTIES (no rdfs:domain — shared parent pattern)" with their `local` and `range`. (`rdfs:domain` is the class a property is allowed to be used on; `rdfs:range` is the type of value a property points at.) The instruction: if a column's natural name matches one, the model should create a class-scoped `SubPropertyOf:` of it rather than a fresh standalone property.

- **`obj_abstracts`** (`ObjectProperty` — a link from one entity to another) are listed under "ABSTRACT OBJECTPROPERTIES FOR FK RANGES" with `local`, full `range` URI, and a parenthetical range-class local name (derived at line 107 by splitting on `#` then `/`). For any FK ObjectProperty whose `Range:` matches, the model "MUST declare SubPropertyOf: <abstract_uri>." The presence of `obj_abstracts` also conditionally adds Rule 16 (see §4.7).

### 4.4 Section 3 — The table profile: how TABLES, COLUMNS, and VALUES are shown

The **COLUMNS** block (lines 114–148) is the per-column profile the model reasons over. The loop builds a `flags` list per column, then prints one fixed-width line `  {col_name:<30} {inferred_type:<12}{flag_str}`. The flags encode every column KIND and, crucially, the VALUES.

#### Column-kind flags

- **PRIMARY KEY**: `is_pk` adds the `PK` flag (lines 119–120). PK columns are also excluded from `non_pk_cols` (lines 146–147), so they are *not* listed in Rule 10's "must each have a property" set — see §4.5 on how PKs are suppressed.
- **FOREIGN KEY**: `is_fk and fk_ref_table` adds either `FK→{table}` or, if `fk_ref_skipped`, `FK→{table} [SKIPPED — model as DatatypeProperty xsd:integer]` (lines 121–127). The SKIPPED marker is the single signal that licenses integer modelling of an FK.
- **Nullability**: `nullable` flag if `is_nullable`; an explicit `null_rate={x:.0%}` flag when `null_rate > 0.5`.

#### VALUES — categorical sets and samples

What value evidence the model sees depends on `cardinality_hint` (lines 132–141):

| Condition | Flag emitted | What it shows |
|---|---|---|
| `CATEGORICAL_CLOSED` | `CATEGORICAL_CLOSED values=[...]` | up to **8** `categorical_values` — i.e. the *complete* enumerated set |
| `CATEGORICAL_OPEN` (with `top_values`) | `CATEGORICAL_OPEN sample=[...]` | up to **5** `top_values` — explicitly a partial sample |
| `FLAG`, `FREE_TEXT`, `IDENTIFIER` | the bare hint name as a flag | — |
| else if `is_categorical` | `CATEGORICAL values=[...]` | up to 8 `categorical_values` |
| else if `top_values` | `top_values=[...]` | up to 5 |

- **Detected formats**: `detected_formats[0]` is shown as `format=...` when present.

So VALUES enter the prompt only as (a) the full closed set for `CATEGORICAL_CLOSED`/`CATEGORICAL`, or (b) a truncated top-N sample otherwise. The model is never handed full raw data.

### 4.5 Section 3.5 — FK column → exact range-class URI mapping

**In plain terms:** foreign keys get their target class URI computed by code, so the model is told exactly what to write rather than guessing.

Foreign keys get a dedicated, deterministic section (lines 150–166). `fk_active` selects columns where `is_fk and fk_ref_table and not fk_ref_skipped`. For each, the target class local name is computed by `derive_class_local_name(col.fk_ref_table)` (imported from `context_builder`) and the prompt prints:

```
FK COLUMNS — each MUST be owl:ObjectProperty. Modelling any of these as DatatypeProperty is a HARD REJECTION.
  Exact Range: URIs to use (derived deterministically from referred table):
    {col_name}  →  Range: <{base_iri}{class_local}>
```

This removes range-class naming from the LLM's discretion entirely: the FK target URI is computed by code and the model is told the *exact* URI to write. SKIPPED FKs are deliberately excluded here because they are not relationships to materialise (their target table was empty/absent).

### 4.6 Section 4 — The numbered modelling rules (1–15), the heart of the contract

Rules are accumulated into a `rules` list (lines 169–260) prefixed by "RULES (MUST follow exactly):". Each rule below is documented as written.

#### Rules 1–3 — class identity and scalar vs. FK columns

- **Rule 1 — one primary class.** "Declare exactly ONE primary class URI: `<{deterministic_uri}>`." The class identity is fixed by the deterministic URI, not the model.

- **Rule 2 — non-PK non-FK scalar → DatatypeProperty.** Such a column becomes an `owl:DatatypeProperty` and "BOTH Domain: AND Range: are REQUIRED — a property missing either is invalid." The range must be drawn from an enumerated XSD palette (the standard set of literal data types) with usage guidance:

  | XSD type | Use for |
  |---|---|
  | `xsd:string` | text |
  | `xsd:integer` | IDs / counts / integers |
  | `xsd:decimal` | amounts / rates / weights |
  | `xsd:double` | floating-point |
  | `xsd:dateTime` | timestamps with time |
  | `xsd:date` | date-only |
  | `xsd:boolean` | flags / bits |

  "Every DatatypeProperty MUST have an explicit Range:."

- **Rule 3 — FK → ObjectProperty ONLY (hard rejection otherwise).** Domain must be the primary class; Range must be "the exact class URI listed in the FK COLUMNS section above." It restates the HARD REJECTION for modelling a non-SKIPPED FK as a DatatypeProperty, with rationale: "The integer ID is a relational artefact — the ontology must express the relationship directly as ObjectProperty." If the referenced class is not yet declared, the model must "declare a placeholder Class for it." The lone EXCEPTION is the `[SKIPPED ...]` marker, which is "the only case where integer modelling is permitted for a FK."

#### Rule 4 — closed categorical → Class + Individuals

**In plain terms:** a column with a fixed set of allowed values (like admission types) becomes a class, one named individual per allowed value, and a single link from the table's class to it.

**Rule 4 — CATEGORICAL_CLOSED → ObjectProperty + Class + Individuals.** The required pattern:

- Declare exactly ONE `owl:Class` for the categorical concept; using `xsd:string` for a CATEGORICAL_CLOSED column is a HARD REJECTION.
- "NEVER create a second class with an 'Enum' suffix — one class only, never Foo + FooEnum."
- For each distinct value, declare an `Individual:` with `Types:` pointing at the class, using `{Value}{ClassName}` PascalCase URIs.
- PascalCasing is spelled out precisely: capitalise the first letter of each space-separated word and remove spaces; "Do NOT stem, singularize, or shorten word endings — 'Not Stopped' → 'NotStopped', not 'NotStopd'"; strip special characters `/ ' -` by removal then capitalise.
- Examples: `:EmergencyAdmissionType` not `:Emergency`; `:FemaleGender` not `:Female`.
- The primary class links via ONE ObjectProperty (domain = primary class, range = categorical class).

A full worked Manchester-syntax example for an `admission_type` column (Class `:AdmissionType`, two Individuals, ObjectProperty `:hasAdmissionType`) is embedded at lines 194–202.

#### Rules 5–5c — reuse and the single-range constraint

- **Rule 5 — no redeclaration.** "Do NOT redeclare any class listed in Section 2. Reference their URIs directly."

- **Rule 5b — class reuse / anti-duplication.** If a column's concept matches an already-declared class, "use that exact URI as the ObjectProperty range. Do NOT mint a table-prefixed variant" — the concrete illustration: "If AmountUom is listed, do NOT declare InputeventMvAmountUom — use AmountUom." Declaring a new class whose label or concept duplicates a Section-2 class is a HARD REJECTION.

- **Rule 5c — SINGLE RANGE.** Each property declares exactly ONE `rdfs:range`: an ObjectProperty exactly one class URI, a DatatypeProperty exactly one XSD type. "NEVER declare both a class range and an XSD range on the same property URI. NEVER declare more than one range of any kind." HARD REJECTION example: "any property with both `rdfs:range <SomeClass>` and `rdfs:range xsd:anyType`."

#### Rules 6–8 — naming, the mapper hook, and labels

- **Rule 6 — generic names must be class-scoped.** Generic property names must always be scoped with the table concept (`hasLabeventFlag` not `hasFlag`; `hasLabeventValue` not `hasValue`). It enumerates the bare names that always require scoping: `label, description, value, name, code, type, date, time, id, note, comment, text, status, flag, count, rate, seq`.

- **Rule 7 — `skos:altLabel` requirement (the mapper hook).** "Every DatatypeProperty MUST include: `Annotations: skos:altLabel "<column_name>"@en`", with the rationale "(This drives automatic column-to-property matching in the downstream mapper.)" Here `skos:altLabel` is an alternative label — here, the original column name. This is the load-bearing link that lets the GOLD-layer mapper match a relational column to its property by its original column name.

- **Rule 8 — labels and comments.** Every declared Class and Property must carry English `rdfs:label` AND `rdfs:comment` (a human-readable description annotation); the comment "must be a concise one-sentence description," with an example.

#### Rules 9–10 — prefixes and coverage

- **Rule 9 — prefixes.** The output must begin with `Ontology: <{base_iri}>` plus required prefixes: `fm:` (`http://forgemesh.ai/ontology#`), `skos:`, `rdfs:`, `xsd:`. (The `{base_iri}` placeholder here is substituted via a `.replace` at line 227.)

- **Rule 10 — coverage list.** "Columns that MUST each have a property (non-PK columns): {non_pk_cols}" — the explicit, comma-joined list of every non-PK column, guaranteeing total column coverage while leaving PKs out.

#### Rules 11–13 — collisions and the abstract-parent pattern

**In plain terms:** when a property name would clash with an existing one, or uses a too-generic word, the model makes a table-specific child property that inherits from a shared, domain-less "abstract parent."

- **Rule 11 — collision → class-scoped subproperty.** If a property local-name would collide with one in the PROPERTIES section, the model must use a class-scoped name `has{PrimaryClassLocal}{Concept}` (e.g. `hasLabeventValue` when `hasValue` exists), and when doing so add `SubPropertyOf: <abstract_parent_uri>`; the abstract parent "must have NO rdfs:domain and rdfs:range = the shared type."

- **Rule 12 — exactly one Domain, pointing only at the primary class.** Each property has EXACTLY ONE `Domain:`, equal to `<{deterministic_uri}>`. Any other class as Domain is a HARD REJECTION; multiple `Domain:` lines are forbidden ("split into class-scoped subproperties instead").

- **Rule 13 — the abstract-parent pattern for generic names.** For a column with one of the bare names (`label, description, value, name, code, type, date, time, id, note, comment, text, status, subject, amount, rate, uom`), the model must:
  1. First declare an abstract parent property with NO `Domain:` and an appropriate XSD range.
  2. Then declare a class-scoped subproperty `has{PrimaryClass}{Concept}` with `SubPropertyOf:` pointing at that parent — "even if an abstract parent does not yet exist in the ontology."

#### Rule 14 — cardinality-hint → required OWL shape

This is the master table mapping each `cardinality_hint` to its OWL shape, mirroring how the hints appear in the COLUMNS block (§4.4):

| `cardinality_hint` | Required modelling |
|---|---|
| `CATEGORICAL_CLOSED` | ObjectProperty + `owl:Class` + `owl:NamedIndividual`; "the listed values ARE the complete set," and `owl:oneOf`/`Individual:` blocks are "ONLY permitted for CATEGORICAL_CLOSED columns." |
| `CATEGORICAL_OPEN` | `DatatypeProperty xsd:string` ONLY; "Do NOT create an owl:Class or owl:ObjectProperty"; model as a plain text literal; declaring an ObjectProperty or Class for it is a HARD REJECTION. |
| `FLAG` | DatatypeProperty `xsd:boolean` or `xsd:integer`; never a class/individual. |
| `FREE_TEXT` | `DatatypeProperty xsd:string`; never a class/individual. |
| `IDENTIFIER` | `DatatypeProperty xsd:integer` (or `xsd:string` if non-numeric); never a class. |

- **HARD RULE:** `owl:oneOf` / `owl:NamedIndividual` are FORBIDDEN for any column not marked CATEGORICAL_CLOSED.

#### Rule 15 — abstract parents must not carry a Domain

**In plain terms:** giving a shared parent property a domain would trick the reasoner into believing every individual using it belongs to that one class — so abstract parents stay domain-less.

**Rule 15 — abstract-parent domain rule.** A property that other properties point at via `SubPropertyOf:` MUST NOT carry a `Domain:`; only the class-scoped subproperties carry `Domain:`. The rationale is explicit and is the reason the rule exists: a domain on an abstract parent "causes OWL inference to incorrectly infer class membership for all individuals with that property, regardless of their actual type — a semantically catastrophic error."

### 4.7 Rule 16 — FK abstract-parent rule (conditional)

Rule 16 (lines 261–273) is appended *only when* `obj_abstracts` is non-empty.

For every FK ObjectProperty, if an abstract ObjectProperty exists whose range matches the new property's intended range, the model "MUST add: `SubPropertyOf: <abstract_uri>`," and the FK property's range "MUST be the named class — never an XSD type." It gives concrete pairings (range `:Admission` → `SubPropertyOf: :hasAdmission`, etc.). Omitting `SubPropertyOf:` when a matching abstract parent exists is a HARD REJECTION.

### 4.8 Provenance rules P.1–P.5 (the `fm:` namespace)

**In plain terms:** these rules attach machine-readable "where did this come from" tags so any triple (one subject–predicate–object fact) can be traced back to the exact table, column, or foreign key it came from.

The final rule block (lines 275–384) attaches machine-readable provenance in the `fm:` (`http://forgemesh.ai/ontology#`) namespace. These are what later pipeline stages read to trace a triple back to its relational source.

#### P.1 / P.2 — class and datatype-property provenance

- **P.1 — Class provenance.** Every `Class:` block MUST carry exactly one `fm:sourceTable "<table_name>"` (with a worked example using `table_ctx.table_name`). Missing it is a HARD REJECTION.

- **P.2 — DatatypeProperty provenance.** Every DatatypeProperty MUST include `fm:sourceColumn "table.col"`, `fm:sqlType "<SQL_TYPE>"`, and `fm:nullRate "0.00"` (decimal string). The prompt then *enumerates the expected values for every data column*: looping over `table_ctx.columns`, skipping PKs and non-skipped FKs, it prints per column `fm:sourceColumn "{table}.{col}"`, `fm:sqlType "{inferred_type}"`, `fm:nullRate "{null_rate:.2f}"` (lines 305–315). So the model is handed the exact provenance literals to emit. Any of the three missing is a HARD REJECTION.

#### P.3 / P.3b — object-property provenance (FK and categorical)

- **P.3 — ObjectProperty / FK provenance.** Present only when `fk_prov_cols` (non-skipped FKs) exist. Each concrete FK ObjectProperty MUST include `fm:sourceForeignKey "source_table.fk_col -> target_table"`, and the prompt lists the exact string per FK column (`{table}.{col} -> {fk_ref_table}`). Abstract ObjectProperty parents are exempt from P.3 but still need `rdfs:comment`. Missing it on a concrete FK property is a HARD REJECTION.

- **P.3b — ObjectProperty / categorical provenance.** Present only when `cat_prov_cols` exist (selected as `is_categorical and not is_pk and not is_fk`). Every ObjectProperty modelling a categorical column MUST include `fm:sourceForeignKey "source_table.col -> ClassName"`, where `ClassName` is the OWL class the model chose; the prompt lists each categorical column with a `-> <YourChosenClassName>` placeholder. Missing it is a HARD REJECTION. This is how a categorical relationship (which has no relational FK) still records its source column.

#### P.4 / P.5 — comment hygiene

- **P.4 — comment purity.** `rdfs:comment` must contain ONLY a human-readable description, "NOT ... provenance strings like 'Derived from...'. Provenance belongs exclusively in fm: annotations."

- **P.5 — single comment.** No element may carry more than one `rdfs:comment` value.

### 4.9 Revision path

If `revision_instructions` is non-empty, a final section is appended (lines 388–392): "REVISION REQUIRED — address ALL of the following issues before responding:" followed by the instructions.

The separate `rebuild_with_revision` function (lines 397–410) constructs the retry prompt by taking the original prompt and appending:

- the rejected `previous_output` under "PREVIOUS OUTPUT (REJECTED):"
- the Judge's instructions under "REVISION REQUIRED — fix ALL issues listed below before responding:"

This is the bridge into the gen→judge→revise loop documented elsewhere in the chapter.

### 4.10 Summary — how each key is handled at this stage

| Element | Treatment in the generation prompt |
|---|---|
| **TABLES** | One per prompt; identity fixed by `deterministic_uri` (Rule 1); `fm:sourceTable` required (P.1); entity-class guard suppresses individual/oneOf enumeration when `row_count > max_rows_for_enumeration`. |
| **FIELDS / COLUMNS** | Listed in the COLUMNS block with type + flags; Rule 10 lists every non-PK column as mandatory; scalars → DatatypeProperty with required Domain + Range (Rule 2); generic names class-scoped (Rules 6, 11, 13); `skos:altLabel` of the column name required (Rule 7); per-column `fm:sourceColumn`/`fm:sqlType`/`fm:nullRate` (P.2). |
| **VALUES** | Surfaced as full closed sets for CATEGORICAL_CLOSED/CATEGORICAL (up to 8) or as top-N samples (up to 5); CATEGORICAL_CLOSED becomes Class + NamedIndividuals + oneOf-eligible ObjectProperty (Rule 4); CATEGORICAL_OPEN/FREE_TEXT/FLAG/IDENTIFIER become XSD literals (Rule 14); oneOf/NamedIndividual forbidden outside CATEGORICAL_CLOSED. |
| **PRIMARY KEYS** | Flagged `PK`, excluded from the Rule 10 coverage list and from P.2 provenance enumeration — i.e. *suppressed* as the identity of the entity rather than turned into a property. |
| **FOREIGN KEYS** | Given an exact derived range-class URI in Section 3.5; must be ObjectProperty-only with that range (Rule 3, HARD REJECTION otherwise); `SubPropertyOf:` an abstract FK parent when one matches (Rule 16); `fm:sourceForeignKey` required (P.3); the single exception is a `[SKIPPED]` FK, modelled as `DatatypeProperty xsd:integer`. |

---

## 5. The Discriminator — Judge-LLM & Retry Loop

**In plain terms:** before any AI-proposed ontology fragment is added to the graph, a *second* AI ("the Judge") reviews it against a fixed checklist. If the Judge rejects it, the first AI is asked to try again with the Judge's complaints in hand — up to a fixed number of times.

**Inputs → Outputs:** takes a per-table context + the first LLM's proposed fragment + a memory of classes already committed → emits a structured `JudgeVerdict` that either admits the fragment to the merge step or re-prompts the generator with targeted fixes.

### 5.0 What the discriminator is and isn't

After the Gen-LLM proposes a Manchester-syntax fragment for one table (Section 4), that fragment does **not** go straight into the accumulating graph. (Manchester syntax = a compact, human-readable text format for writing OWL ontology axioms.)

It first passes through a **discriminator**: a second, independent LLM call ("the Judge") that adversarially reviews the proposal against a fixed checklist and returns a structured `JudgeVerdict`. If the Judge rejects the proposal, it drives a bounded retry loop in which the Gen-LLM is re-prompted with the Judge's complaints.

This is the "judge-LLM + retry" half of the RIGOR-style generate/critique architecture. Note the clear split of responsibilities:

- **The Judge is stateless and structurally deterministic.** It never writes triples (a triple = one subject–predicate–object fact) and never mutates state. Its only job is to emit a yes/no verdict plus prose instructions.
- **The generator owns the control flow.** The actual loop control, the approval arithmetic, and the failure fallbacks all live in the generator, not the Judge.

This section documents two files:

- `semantic_mapper/ontology/iterative/judge_prompt.py` — `build_judge_prompt(...)`, which assembles the Judge's system and user prompts and encodes every evaluation criterion.
- `semantic_mapper/ontology/iterative/generator.py` — the retry loop (lines ~234–301), the approval logic (line 284), and the Judge call helper `_call_judge_llm` (lines 516–538).

### 5.1 Inputs and outputs

#### Inputs to `build_judge_prompt`

`build_judge_prompt` (`judge_prompt.py:34`) takes:

| Argument | Type | What it carries |
|---|---|---|
| `table_ctx` | `TableContext` | The same per-table context the Gen-LLM saw: table name, `row_count`, and a list of `ColumnContext`. Built upstream by the context builder. |
| `fragment` | `OntologyFragment` | The Gen-LLM's proposed output: `manchester_syntax` (the raw text under review), `classes_declared`, `properties_declared`, and `retry_count`. |
| `accumulated_classes` | `List[AccumulatedClassSummary]` | Every class already committed to the graph from prior tables, each with `uri` (its IRI — a globally-unique name for a thing) and `label`. This is the duplicate/reuse memory. |
| `base_iri` | `str` | The required namespace all new IRIs must sit under. |
| `max_rows_for_enumeration` | `int = 1000` | The entity-class guard threshold (see 5.3). |

#### Output: the `JudgeVerdict`

`build_judge_prompt` returns a `tuple[str, str]` of `(system_prompt, user_prompt)`. These are handed to `_call_judge_llm`, which calls the LLM in JSON mode and parses the reply into a **`JudgeVerdict`** (`models.py:61`):

| Field | Type | Meaning |
|---|---|---|
| `approved` | `bool` | Judge's overall pass/fail |
| `coverage_issues` | `List[str]` | per-column complaints, format `"col_name: reason"` |
| `coherence_issues` | `List[str]` | namespace / typing / structural complaints |
| `revision_instructions` | `str` | concise fix list fed back to the Gen-LLM, or `""` if approved |
| `confidence` | `float`, `0.0–1.0` (validated by Pydantic `ge=0.0, le=1.0`) | Judge's self-reported certainty |

The verdict is the discriminator's entire externally-visible output; everything else is internal to the prompt text.

### 5.2 The system prompt and the JSON contract

The system prompt (`_JUDGE_SYSTEM_PROMPT`, line 20) is deliberately terse and strict:

> "You are a strict OWL ontology reviewer. Respond with a single JSON object only — no prose, no markdown fences."

The user prompt ends by repeating an exact target schema, `_VERDICT_SCHEMA` (line 25), which mirrors the `JudgeVerdict` field names one-for-one. Combined with `json_mode=True` on the call, this is what makes the reply mechanically parseable into the Pydantic model.

### 5.3 What the user prompt shows the Judge

`build_judge_prompt` constructs the user prompt as a sequence of clearly-labelled blocks (lines 80–149):

1. **`TABLE BEING REVIEWED`** — the table name.
2. **Entity-class guard banner** (conditional, lines 71–78) — see below.
3. **`COLUMNS THAT MUST EACH HAVE A PROPERTY`** — the coverage checklist (see 5.4).
4. **`GEN-LLM OUTPUT`** — `fragment.manchester_syntax` verbatim, the text actually under review.
5. **`EXISTING CLASSES (must NOT be redeclared)`** — one line per `AccumulatedClassSummary` rendered as `<uri> rdfs:label "label"@en`, or `(none — first table)` for the first table.
6. **`REQUIRED NAMESPACE`** — the `base_iri`.
7. **`EVALUATE:`** — the 17 numbered criteria (5.5).
8. The exact `_VERDICT_SCHEMA` to fill in.

#### The entity-class guard banner (block 2)

This banner is injected only when `table_ctx.row_count > max_rows_for_enumeration`. When triggered, it states the row count and threshold and declares the table an **ENTITY TABLE**.

For an entity table, modelling its rows with `owl:equivalentClass` / `owl:oneOf` enumeration (`owl:oneOf` = a closed, enumerated list of allowed values) is declared "a HARD REJECTION." This stops the Gen-LLM from turning a large fact/entity table into a closed enumeration of individuals.

### 5.4 How FIELDS/COLUMNS, PRIMARY KEYS, FOREIGN KEYS, and CATEGORICALS are surfaced for coverage

**In plain terms:** this block lists exactly which columns the Gen-LLM was obligated to model, and tags each one so the Judge knows what kind of column it is.

The coverage checklist is built in the loop at lines 48–65, and this is where keys and column kinds are policed:

- **PRIMARY KEYS are deliberately excluded.** `if col.is_pk: continue` (line 51). PK columns are realised as the row's IRI/identity downstream, not as data properties, so the Judge is told *not* to expect a property for them. Coverage is defined as "every **non-PK** column."

- **FIELDS/COLUMNS** each become one checklist row: `col_name` (padded), `inferred_type`, and bracketed tags.

- **FOREIGN KEYS** are tagged from the column metadata. A column with `is_fk` and `fk_ref_table` gets a `FK→<ref_table>` tag. If `col.fk_ref_skipped` is set (the referenced table was empty/absent), the tag becomes `FK→<ref_table> [SKIPPED]` (lines 53–57). The `[SKIPPED]` marker is honoured by criterion #1, which explicitly *accepts* a skipped FK modelled as a plain `DatatypeProperty xsd:integer` (a DatatypeProperty is an attribute whose value is a literal, e.g. a number or string) rather than an ObjectProperty (a link from one entity to another) pointing at a non-existent class.

- **CATEGORICALS / cardinality.** If `col.cardinality_hint` is present it is appended as a tag; otherwise, if `col.is_categorical`, the literal tag `CATEGORICAL` is appended (lines 58–61).

  The `cardinality_hint` vocabulary — `CATEGORICAL_CLOSED`, `CATEGORICAL_OPEN`, `FLAG`, `FREE_TEXT`, `IDENTIFIER` — is computed *deterministically upstream* in `context_builder.py` (`_compute_cardinality_hint`), **not** by the Judge. The Judge's role is only to verify the Gen-LLM honoured those hints (criterion #12).

- **Nullability of VALUES.** If `col.null_rate > 0.5` a `nullable` tag is added (line 62), signalling columns whose values are sparsely populated.

A column with no tags renders bare; otherwise tags are joined into a `[...]` suffix. If every column is a PK, the block degrades to `(none — all columns are PK)`.

### 5.5 The seventeen evaluation criteria

The `EVALUATE:` block (lines 95–145) is the heart of the discriminator. Several criteria are flagged as **HARD REJECTION**, meaning the Judge is instructed to set `approved=false`.

The criteria are grouped below by concern.

#### Coverage and namespace coherence

- **#1 COVERAGE** — every non-PK column listed must have a corresponding OWL property; missing or partial coverage is flagged. Carve-out: a `[SKIPPED]` FK as `DatatypeProperty xsd:integer` is accepted.
- **#2 COHERENCE** — all URIs (globally-unique names) in the correct namespace; generic property names qualified; FK ranges point at the correct *existing* class URIs. (rdfs:range = the type of value a property points at — a class, or a literal type like xsd:string.)
- **#9 GENERIC NAMES** — generic column names (`value`, `name`, `code`, `type`, …) must be class-scoped rather than left bare.

#### Duplicate and reuse policing (against the EXISTING CLASSES list)

- **#3 DUPLICATES** — the fragment must not redeclare any class in the EXISTING CLASSES block.
- **#17 CLASS_REUSE** — *(hard rejection)* the fragment must not mint a **table-prefixed variant** of an existing class. The prompt gives a concrete example: if `AmountUom` already exists, declaring `InputeventMvAmountUom` is rejected, and the generator is told to reuse the exact existing URI as the object-property range. This is the discriminator's defence against the ontology fragmenting into near-duplicate classes as tables accumulate.

#### Property typing and structure

- **#5 DOMAIN (MULTI-DOMAIN)** — *(hard rejection)* every property needs exactly **one** `rdfs:domain` (the class a property is allowed to be used on); missing or multiple domains fail. A property spanning >1 class must be split into class-scoped subproperties.
- **#6 RANGE** — *(hard rejection)* every property needs an `rdfs:range`.
- **#7 DUAL-TYPE** — *(hard rejection)* no URI may be typed as both `ObjectProperty` and `DatatypeProperty`. (This is also pre-checked deterministically; see 5.6.)
- **#11 ABSTRACT PARENTS** — *(hard rejection)* abstract parent properties (targets of `SubPropertyOf`) must have **no** `rdfs:domain`; an abstract parent carrying a `Domain:` fails. The same criterion adds a **SELF-REF** rule: a `SubPropertyOf:` where subject == object is a hard rejection.
- **#16 MIXED_RANGE** — *(hard rejection)* no property may declare both a class-URI range and an XSD datatype range. A property is either an `ObjectProperty` (exactly one class URI as range) or a `DatatypeProperty` (exactly one XSD type) — never both `rdfs:range <SomeClass>` and `rdfs:range xsd:anyType` on the same URI. This is the Judge-side enforcement of the `MixedRangeResolution` concern.

#### Individuals, categoricals, and cardinality

- **#8 INDIVIDUALS** — *(hard rejection)* `owl:NamedIndividual` / `owl:oneOf` patterns (a named individual is a specific instance value; `owl:oneOf` is a closed enumerated list) may be used **only** for `CATEGORICAL_CLOSED` columns; using them for any non-categorical column fails.
- **#12 CARDINALITY** — the cardinality-hint contract, checked per hint:

  | Hint | Required modelling | Rejected if… |
  |---|---|---|
  | `CATEGORICAL_CLOSED` | `ObjectProperty` + `Class` + `owl:NamedIndividual` (or `owl:oneOf`) — **Accept** | — |
  | `CATEGORICAL_OPEN` | `DatatypeProperty xsd:string` | modelled as `ObjectProperty`/`Class` |
  | `FLAG` | `DatatypeProperty xsd:boolean` or `xsd:integer` | class/individual |
  | `FREE_TEXT` | `DatatypeProperty xsd:string` | class/individual |
  | `IDENTIFIER` | `DatatypeProperty xsd:integer` or `xsd:string` | class |

  - **CATEGORICAL-PROMOTION** *(hard rejection)*: a `CATEGORICAL` column modelled as a plain `DatatypeProperty xsd:string` fails — categorical columns must be promoted to `ObjectProperty` + `Class` + `owl:NamedIndividual`.

#### Provenance (`fm:` annotations) and labelling

- **#4 SKOS** — every `DatatypeProperty` must include `skos:altLabel` (an alternative label — here, the original column name) carrying the column name.
- **#10 LABELS/COMMENTS** — every Class and Property must have both `rdfs:label` and `rdfs:comment`.
- **#13 PROVENANCE** — the `fm:` annotation contract (`fm:` = `http://forgemesh.ai/ontology#`), with several hard rejections:
  - Every `Class:` block must carry `fm:sourceTable "<table_name>"` — *missing is a hard rejection*.
  - Every `DatatypeProperty:` block must carry `fm:sourceColumn`, `fm:sqlType`, **and** `fm:nullRate` — *missing any is a hard rejection*.
  - Every concrete FK `ObjectProperty` must carry `fm:sourceForeignKey`; every `CATEGORICAL` `ObjectProperty` must too, in the exact format `"source_table.col_name -> ClassName"` where `col_name` is the *exact* source column — *missing is a hard rejection*.
  - Carve-out: abstract `ObjectProperty` parents (those with `SubPropertyOf` pointing to them and no domain) are exempt from the FK/categorical provenance checks but must still have `rdfs:comment`.
- **#14 COMMENT PURITY** — *(hard rejection)* no `rdfs:comment` may contain `"Derived from"`; provenance belongs in `fm:` annotations, comments are human-readable description only.
- **#15 SINGLE COMMENT** — *(hard rejection)* no element may have more than one `rdfs:comment` value.

#### Division of labour

Note that provenance, comment purity, single-comment, and dual-type are *also* enforced deterministically by post-passes and merge-time checks elsewhere in the pipeline. The Judge provides an LLM-level first line of defence so the Gen-LLM is corrected *before* the fragment is merged.

### 5.6 The dual-type pre-check (deterministic, before the Judge)

Before the Judge is even called, the loop runs a deterministic guard. After parsing the fragment graph, `check_duplicate_property_types(fragment_graph)` (generator.py:257) detects any URI typed as both object and datatype property.

If any are found it logs a warning listing the offending local names (lines 258–263). This complements Judge criterion #7: the deterministic check guarantees detection regardless of whether the Judge happens to catch it.

### 5.7 Calling the Judge and the permissive timeout fallback

#### The call itself

`_call_judge_llm` (generator.py:516–538) issues the call via `call_llm(...)` with `json_mode=True`, the configured `judge_model`, and `judge_temp` (default `0.0`, from `ONTOLOGY_GEN_JUDGE_TEMPERATURE`).

It accumulates `prompt_tokens` / `completion_tokens` for cost accounting, then parses the JSON and constructs `JudgeVerdict(**raw)`.

#### The permissive fallback (fail-open)

**In plain terms:** if the Judge call breaks for any reason, the fragment is waved through at low confidence rather than stalling the pipeline.

The crucial robustness behaviour is the **permissive fallback** (lines 536–538). *Any* exception — network/timeout failures, malformed JSON, a reply that doesn't satisfy the schema — is caught, logged ("Judge-LLM call failed: … — treating as approved at low confidence"), and converted into:

```python
return JudgeVerdict(approved=True, confidence=0.3, revision_instructions="")
```

So a Judge that fails to respond does **not** block the pipeline: the fragment is treated as approved, but at a low confidence (`0.3`) that is recorded for observability.

The design choice is **fail-open** — a broken discriminator degrades to "let the Gen-LLM output through" rather than stalling table processing. Because `revision_instructions` is empty, a fallback verdict also yields no useful retry guidance, so this only ever results in acceptance (or, on the final attempt, an unconditional merge — see 5.8).

### 5.8 The retry loop and approval arithmetic

The loop runs in `generate(...)` at lines 234–301, bounded by `max_retries = ONTOLOGY_GEN_MAX_RETRIES` (default `2`, line 113). The loop iterates `for attempt in range(max_retries + 1)` — i.e. **one initial attempt plus up to `max_retries` retries**, three total by default.

#### What each iteration does

1. **Gen** — `_call_gen_llm(gen_prompt, ...)` produces Manchester text; saved as `last_manchester`.
2. **Parse** — `parse_manchester(...)` yields `(fragment_graph, classes_declared, props_declared)`, wrapped into an `OntologyFragment` with `retry_count=attempt`.
3. **Dual-type pre-check** (5.6).
4. **Judge** — `build_judge_prompt(...)` is built from `index._classes` (the *full* accumulated class list, passed deliberately for duplicate/reuse detection), then `_call_judge_llm(...)` returns the verdict, which is logged at debug level with `approved`, `confidence`, and the two issue counts.

#### The approval rule

The approval rule is a single line (line 284):

```python
approved = verdict.approved or verdict.confidence >= judge_confidence_floor
```

where `judge_confidence_floor = ONTOLOGY_JUDGE_CONFIDENCE_FLOOR` (default `0.6`, line 120). So a fragment is accepted if **either** the Judge explicitly approved it **or** its self-reported confidence meets the floor — even on a nominal rejection, sufficiently confident output is let through.

Note that the timeout fallback's `confidence=0.3` is *below* the `0.6` floor, so a failed-Judge verdict relies on its `approved=True` flag, not the floor, to pass.

#### How the loop exits

The break condition (line 285) is `if approved or attempt == max_retries`. There are two ways to exit:

- **Approved** — break and proceed to merge.
- **Retries exhausted** — on the last attempt the loop breaks regardless of approval. If still not approved it logs a warning ("max retries reached — merging partial fragment anyway") echoing the unresolved `coverage_issues` and `coherence_issues` (lines 286–293), and the fragment is merged anyway.

The discriminator never *blocks* a table outright. At worst it merges a partial/imperfect fragment after exhausting its budget, leaving the deterministic post-passes and merge-time guards (Section 6+) as the final safety net.

If neither condition holds (rejected, with attempts remaining), the loop falls through to the rebuild step.

### 5.9 How rejection drives `rebuild_with_revision`

When the fragment is rejected and retries remain, the prompt for the next iteration is regenerated by `rebuild_with_revision(...)` (gen_prompt.py:397) at lines 297–301:

```python
gen_prompt = rebuild_with_revision(
    original_prompt=gen_prompt,
    revision_instructions=verdict.revision_instructions,
    previous_output=manchester,
)
```

The helper appends two new blocks to the *prior* prompt:

- the rejected output under `PREVIOUS OUTPUT (REJECTED):`, and
- the Judge's `revision_instructions` under `REVISION REQUIRED — fix ALL issues listed below before responding:`.

The Gen-LLM thus sees its own previous attempt *and* the specific complaints, and is asked to fix all of them.

Because the loop reassigns `gen_prompt`, instructions **accumulate** across retries — the second retry's prompt still contains the first retry's appended revision block. The Judge's free-text `revision_instructions` field is therefore the sole channel by which the discriminator's verdict is translated into corrective action; `coverage_issues` / `coherence_issues` are used for logging only.

### 5.10 Summary of the discriminator's place in the pipeline

The discriminator sits strictly *between* the Gen-LLM (Section 4) and the deterministic merge (Section 6). It consumes a `TableContext` + `OntologyFragment` + the accumulated-class memory, and emits a `JudgeVerdict` that either:

- (a) admits the fragment to merge, or
- (b) re-prompts the Gen-LLM with targeted fixes, up to `ONTOLOGY_GEN_MAX_RETRIES` times.

Its policing is concentrated on the things an LLM most easily gets wrong and that are expensive to repair after merge:

- full non-PK column **coverage**,
- correct property **typing** (dual-type, mixed-range, object-vs-datatype per cardinality hint),
- **categorical promotion**,
- **FK range** correctness (including the `[SKIPPED]` carve-out),
- cross-table **class reuse**, and
- complete **`fm:` provenance**.

It is engineered to **fail open** (timeout → approve at 0.3) and to **never deadlock a table** (max-retries → merge anyway), deferring final correctness to the deterministic post-passes downstream.

---

## 6. Parsing Manchester Syntax into RDF

**In plain terms:** The LLM that builds the ontology writes each table's description in a compact text format called Manchester Syntax. This stage reads that text with a few regexes and turns it into RDF triples (subject–predicate–object facts), without using any real OWL parser. It is deliberately forgiving so that one stray token in messy LLM output never throws away a whole table's worth of work.

**Inputs → Outputs:** raw Manchester text for one table (plus a namespace) → an `rdflib.Graph` of triples, plus two lists naming the classes and properties that were declared.

### Why this stage exists

The gen-LLM in the RIGOR-style iterative generator does not emit RDF directly. For each table it produces an **OWL fragment written in OWL Manchester Syntax** — a compact, line-oriented surface form built from `Class:`, `ObjectProperty:`, `DatatypeProperty:`, and `Individual:` blocks. (OWL is the Web Ontology Language; a *fragment* here is just the chunk of OWL describing one table.)

Before that fragment can be merged into the growing ontology, judged for coverage, or materialised, it has to become triples. That conversion is the job of:

- `parse_manchester` in `semantic_mapper/ontology/iterative/manchester_parser.py`
- the name-hygiene helpers in `semantic_mapper/ontology/iterative/prefix_leak_fix.py`

### The defining design choice: no external parser

The decision that shapes this whole stage: **there is no external Manchester/OWL parser.** No OWLAPI, no `rdflib` Manchester plugin. Parsing is done by a handful of compiled regexes that slice the text into entity blocks and then scrape known keywords out of each block. The module docstring says so directly ("Uses regex extraction for Class, DatatypeProperty, and ObjectProperty blocks").

The trade-off is deliberate. LLM output is noisy and frequently not strictly valid Manchester. A forgiving line scraper that ignores what it cannot recognise is more robust than a strict grammar that would reject the entire fragment over a single stray token.

### 6.1 Input and output (how this connects to its neighbours)

#### Input

`parse_manchester(manchester_syntax, base_iri, table_name="")` takes:

| Argument | Type | What it is |
|---|---|---|
| `manchester_syntax` | `str` | The raw text the gen-LLM returned for one table (possibly wrapped in a Markdown code fence). |
| `base_iri` | `str` | The namespace into which all locally-named entities resolve, e.g. `http://mesh.local/ontology/mimic_bronze#`. (An IRI/URI is a globally-unique name for a thing; a namespace is the shared prefix those names hang off.) |
| `table_name` | `str` | Used only for logging/diagnostics. |

#### Output

A 3-tuple `Tuple[Graph, List[str], List[str]]`:

- an `rdflib.Graph` of the triples extracted from this fragment;
- `classes_declared` — the list of class URI strings found;
- `properties_declared` — the list of object- and datatype-property URI strings found.

#### Why the two URI lists matter

The two URI-string lists are the parser's contribution to the **coverage check**. Downstream, the judge-LLM and deterministic post-passes compare what the fragment *declared* against what the table actually *requires*.

This is exactly why the failure mode matters (see 6.5): a parse failure produces an *empty* graph and *empty* lists. That surfaces to the judge as "this fragment covered nothing," rather than as a crash.

### 6.2 Pre-processing: fence stripping

`_parse` first calls `_strip_fences` (lines 211–215), which removes leading code-fence openers (` ```manchester `, ` ```omn `, or bare ` ``` `) and a trailing ` ``` `.

LLMs routinely wrap their answer in a fenced block. Without this step the first and last lines would be polluted, and the leading `Class:` could fail to match the block sentinel.

The graph is then created and pre-bound with these prefixes (lines 120–126):

- `owl`, `rdfs`, `skos`, `xsd`
- `fm` → `http://forgemesh.ai/ontology#`

### 6.3 Block extraction and the sentinel

#### Carving the text into blocks

Four block-level regexes carve the body into entity blocks, one family per entity kind (lines 27–42). Each regex has the shape:

```
KEYWORD\s*:\s*(<IRI>|prefixed:name)\s*(.*?)<sentinel>
```

With `re.DOTALL`, group 1 is the entity name and group 2 is the (possibly multi-line) body of that block.

#### The shared sentinel

**In plain terms:** the sentinel is what tells one block where to stop, so each block grabs exactly one entity's worth of text and no more.

The shared `_BLOCK_SENTINEL` (line 25) is the key to non-greedy block boundaries:

```python
(?=\nClass\s*:|\nObjectProperty\s*:|\nDatatypeProperty\s*:|\nIndividual\s*:|$)
```

It is a lookahead that stops a block at the next entity-keyword line or end-of-string, so `(.*?)` consumes exactly one entity's worth of text.

A practical consequence: the sentinel only recognises a new block when the keyword starts a **new line** (note the `\n` prefix). A keyword that is not line-initial would be swallowed into the preceding block's body. This is fine in practice because the generator emits one entity per block on its own line.

#### Scraping facts inside a block

Within each block, keyword-level regexes scrape facts (lines 43–57):

- `rdfs:label`, `skos:altLabel`, `rdfs:comment` (each capturing an optional `@lang` tag) — `rdfs:label` is a human-readable display name; `skos:altLabel` is an alternative label, here the original column name.
- `Domain:`, `Range:`, `SubClassOf:`, `SubPropertyOf:`, `Types:`
- the five `fm:` provenance annotations.

### 6.4 What each entity kind produces (triples emitted)

#### Classes

`_CLASS_BLOCK_RE`, lines 134–147. (A class is a *type of thing*, e.g. an Admission.) Emits:

- `(uri, rdf:type, owl:Class)` — `rdf:type` says "X is a kind of Y"; `owl:Class` marks it as a class.
- `rdfs:label` (lang-tagged)
- `rdfs:comment`
- `rdfs:subClassOf` (each tagged with `_resolve_uri`) — "this class is a sub-kind of that one."
- the provenance triple `(uri, fm:sourceTable, "<table>")`

Every matched class URI string is appended to `classes_declared`.

#### Object properties

`_OBJ_PROP_BLOCK_RE`, lines 149–166. (An ObjectProperty is a link from one entity to another, e.g. `hasPatient`.) Emits:

- `(uri, rdf:type, owl:ObjectProperty)`
- `rdfs:label`, `rdfs:comment`
- `rdfs:domain` (the class a property is allowed to be used on)
- `rdfs:range` (the type of value a property points at — here, another class)
- `rdfs:subPropertyOf`
- the provenance triple `(uri, fm:sourceForeignKey, "...")`

URI string appended to `properties_declared`.

#### Datatype properties

`_DATA_PROP_BLOCK_RE`, lines 168–191. (A DatatypeProperty is an attribute whose value is a literal — a number or string — e.g. `ethnicity`.) Emits:

- `(uri, rdf:type, owl:DatatypeProperty)`
- `rdfs:label`, `rdfs:comment`
- `rdfs:domain`, `rdfs:range` (here the range is a literal type like `xsd:string`)
- `skos:altLabel`
- `rdfs:subPropertyOf`
- three provenance triples: `fm:sourceColumn`, `fm:sqlType`, `fm:nullRate`

URI string appended to `properties_declared`.

#### Individuals

`_INDIVIDUAL_BLOCK_RE`, lines 193–202. (An individual is one specific instance, not a type.) Emits:

- `(uri, rdf:type, owl:NamedIndividual)`
- `rdfs:label`
- for each `Types:` entry, an additional `(uri, rdf:type, <class>)`

Individuals are **not** added to either declared-list — they are instance data, not schema — and they carry no `fm:` provenance.

#### Predicate vocabulary at a glance

The following table summarises the predicate vocabulary per entity kind. Note the asymmetry: only the typed/profiled facts that matter for that entity ride along.

| Entity | rdf:type | label / comment | altLabel | subClassOf | domain / range | subPropertyOf | fm: provenance |
|---|---|---|---|---|---|---|---|
| Class | owl:Class | ✓ / ✓ | — | ✓ | — | — | `fm:sourceTable` |
| ObjectProperty | owl:ObjectProperty | ✓ / ✓ | — | — | ✓ / ✓ | ✓ | `fm:sourceForeignKey` |
| DatatypeProperty | owl:DatatypeProperty | ✓ / ✓ | ✓ | — | ✓ / ✓ | ✓ | `fm:sourceColumn`, `fm:sqlType`, `fm:nullRate` |
| Individual | owl:NamedIndividual (+ each `Types:`) | ✓ / — | — | — | — | — | — |

#### How TABLES, FIELDS, VALUES, PRIMARY KEYS, and FOREIGN KEYS ride through this stage

**In plain terms:** this parser does not *decide* how the database maps to the ontology — the generator already decided that, and baked it into the Manchester text. What the parser does is **preserve the relational provenance as `fm:` annotation triples**, so the lineage survives into RDF.

The mapping of relational concepts to what the parser emits:

- **TABLES** → one `owl:Class` per table, with the originating table name pinned by `fm:sourceTable`. The class URI itself is derived deterministically from the table name (see 6.6).
- **FIELDS / COLUMNS** → `owl:DatatypeProperty` (for literal-valued columns), carrying `fm:sourceColumn` (which column it came from). Range is the column's resolved XSD type.
- **VALUES** → represented at two levels:
  - *Column-level statistics* ride as `fm:nullRate` (the profiled null fraction) and `fm:sqlType` (the original SQL declared type) on the datatype property.
  - *Literal instance values* appear via `Individual` blocks (`rdf:type` + `rdfs:label` + `Types:`), though in the table-by-table schema-generation phase individuals are uncommon.
- **PRIMARY KEYS** → there is **no dedicated PK predicate** in this parser. A PK is a column like any other and surfaces as a datatype property with its `fm:sourceColumn`. PK identity is reflected in how the materialisation layer mints subject IRIs, not as a triple emitted here.
- **FOREIGN KEYS** → an `owl:ObjectProperty` linking two table-classes, carrying `fm:sourceForeignKey` (the FK constraint it was derived from), plus `rdfs:domain`/`rdfs:range` pointing at the referencing and referenced classes. This is the parser's most semantically load-bearing output: FK structure becomes the navigable object-property graph.

All five `fm:` predicates live in the `http://forgemesh.ai/ontology#` namespace (the `FM_NS`, line 125). All five are emitted as plain `Literal` strings — not lang-tagged, not typed — because they are machine-readable provenance, not human display text.

### 6.5 Failure handling: never raise, return empty

**In plain terms:** if anything goes wrong parsing a table, the parser swallows the error and returns nothing, so one bad table can't crash the whole run.

`parse_manchester` (lines 60–80) wraps the real work `_parse` in a try/except that catches **any** exception, logs an error naming the table, and returns `(Graph(), [], [])`. The module docstring states the contract twice: "Never raises — returns an empty graph on total failure."

This is intentional fault isolation in the iterative loop. A malformed fragment for one table must not abort the whole run.

Because the empty return also yields empty `classes_declared` / `properties_declared`, the failure is *legible downstream*. The judge/coverage pass sees a fragment that declared nothing for a table that requires entities, flags the missing coverage, and the table can be retried — rather than the pipeline crashing or, worse, silently merging partial garbage.

The cost: a *partial* parse (e.g. a fragment whose third block is broken) returns whatever the earlier `finditer` loops already added before the exception fired. Total failure returns nothing.

### 6.6 URI resolution and the prefix-leak fix

#### How a name token becomes a URI

`_resolve_uri(token, base_iri)` (lines 88–110) turns each name token into a URI string, trying these rules in order:

1. **Angle-bracket IRI** `<...>` → used verbatim (brackets stripped).
2. Otherwise the token is passed through **`normalise_entity_name`** (from `prefix_leak_fix.py`) to strip invented prefixes (covered below).
3. **Bare XSD type names** in `_XSD_BARE` (`string`, `integer`, `int`, `decimal`, `double`, `float`, `boolean`, `dateTime`, `date`, `time`, `anyURI`, `long`) → resolved to `http://www.w3.org/2001/XMLSchema#<name>`. This catches the common LLM habit of writing `Range: integer` with no `xsd:` prefix.
4. **Known prefixed names** (`owl:`, `rdfs:`, `rdf:`, `xsd:`, `skos:`, `fm:`) → expanded via the hard-coded `prefix_map`.
5. **Everything else** → concatenated onto `base_iri` (with any leading `:` stripped).

Because step 5 is a catch-all that *concatenates onto the base namespace*, any prefix the parser does not recognise becomes part of the local name instead of being rejected. That is exactly the bug `prefix_leak_fix.py` was written to defuse.

#### The "Ontology:" leak

**In plain terms:** one model run kept inventing a fake `Ontology:` prefix and gluing it into entity names. Step 5 above would have happily folded that fake prefix into the URL, producing garbage names. The fix cleans the name *before* the URI is built.

The docstring records the observed failure (run 2, qwen3-32b, MIMIC): 377 URIs across 95 names carried an invented `Ontology:` prefix baked into the local name, e.g. `…mimic_bronze#Ontology:Admission`. There were even double-mangled forms like `…#hasOntology:Caregiver`, where the generator's verb prefix (`has`) collided with the invented prefix.

The root cause is explicitly framed as a *harness bug, not a model-quality result*. The iterative design derives every class URI deterministically from the table name, so every entity *should* land in the single base namespace regardless of how the model decorates the name. The fix normalises the name before the URI is built.

#### `normalise_entity_name(raw)` — stripping one invented prefix

`normalise_entity_name(raw)` (lines 51–96) strips a single invented leading prefix, with these cases:

- **No colon** → returned unchanged.
- **Leading segment is a known-legitimate prefix** (`owl`, `rdf`, `rdfs`, `xsd`, `skos`, `fm` in `KNOWN_PREFIXES`) → returned unchanged, so `skos:altLabel` survives.
- **Double-mangled case, checked first:** if the head starts with a verb prefix (`has`, `is`, `of` in `_VERB_PREFIXES`) and the buried remainder looks like an invented prefix, it rejoins verb + final segment: `hasOntology:Caregiver` → `hasCaregiver`.
- **Simple case:** `Prefix:Local` where `Prefix` is an invented identifier → returns `Local`: `Ontology:Admission` → `Admission`.

A token only counts as a prefix if `_looks_like_prefix` matches `[A-Za-z_][A-Za-z0-9_]*` (lines 99–103).

The docstring (lines 27–32) explains *why normalise rather than just register the prefix*: adding `Ontology → base` to the prefix map would fix the simple case but not the double-mangled one. Normalisation, by contrast, is robust to whatever prefix the next model run invents (`Ont:`, `mimic:`, …).

#### The `http://` scheme false-positive note

A subtlety the comment flags: a token like `skos:altLabel` is left alone because `skos` is a known prefix — but the same colon-splitting logic could, in principle, misread a full `http://…` IRI as `http` + `//…`.

In practice this is avoided because full IRIs arrive in angle brackets and are short-circuited at step 1 of `_resolve_uri`, before `normalise_entity_name` ever sees them; bare `http`-prefixed tokens are not generated by the harness. The guard rails are therefore the `<…>` fast-path plus the `KNOWN_PREFIXES` whitelist — not a scheme-aware URL parser.

#### `detect_prefix_leak(local_names)` — the regression gate

`detect_prefix_leak(local_names)` (lines 106–124) is the matching diagnostic. Given the parser's declared-name lists, it returns `{invented_prefix: count}` (an empty dict means clean).

The docstring positions it as "a post-parse assertion in the e2e gate so the leak can never silently return." After parsing, the e2e test asserts this returns empty — turning the once-observed 377-URI regression into a permanent guard.

### 6.7 Summary of guarantees

- **Robust over correct-by-construction:** regex scraping tolerates imperfect LLM Manchester; unrecognised lines are silently ignored rather than fatal.
- **Total failures are legible, not silent:** an unparseable fragment yields an empty graph and empty declared-lists, which the coverage/judge stage detects as missing coverage.
- **Provenance is first-class:** the relational origin of every entity (table, column, FK, SQL type, null rate) is carried into RDF as `fm:` annotation triples, even though PKs get no dedicated predicate.
- **Names are normalised before URIs are minted:** `normalise_entity_name` collapses invented prefixes onto the deterministic base namespace, and `detect_prefix_leak` is the regression gate that keeps the qwen `Ontology:` leak from returning.

---

## 7. Merge & Structural Repair

**In plain terms:** ForgeMesh builds the ontology (the formal schema of classes, properties, and rules) one table at a time. This stage takes the finished OWL (Web Ontology Language — the formal vocabulary for describing classes and properties) for a single table and folds it into one big shared graph that covers every table seen so far — fixing OWL mistakes before, during, and after the fold so the combined result stays logically valid.

**Inputs → Outputs:** one validated single-table `OntologyFragment` (its `rdflib.Graph`) plus the database inspector's real foreign-key and column-profile facts → the mutated cross-table accumulating graph, plus a `MergeResult` report describing what changed.

This stage takes a single validated `OntologyFragment` — the per-table OWL produced by the RIGOR-style `IterativeOntologyGenerator` for one table — and folds its `rdflib.Graph` (an in-memory set of RDF triples) into the *accumulating* ontology graph that spans every table seen so far. It is implemented entirely in `semantic_mapper/ontology/iterative/graph_merger.py`.

### The three phases (plus extras)

The work splits into three concerns that run in a fixed order:

1. **Pre-merge fixers** — repair a fragment *in isolation*, before it touches the shared graph.
2. **The merge itself** — deduplicates URIs (IRI/URI: a globally-unique name for a thing), detects structural type conflicts, and merges annotations and blank-node subgraphs.
3. **Post-merge safety-net fixers** — operate on the whole accumulating graph (autowiring, abstract-parent domain repair, self-reference scrubbing).

On top of those three phases, a separate **MIXED_RANGE resolver** (`resolve_mixed_ranges` / `resolve_mixed_ranges_in_graph`) runs later in the loop as a deterministic OWL-coherence repair. Two standalone guards round it out: `check_duplicate_property_types` and `synthesise_missing_provenance`.

### 7.1 Inputs and Outputs

The entry point is `merge(...)` at lines 51–222:

```python
merge(accumulating, fragment_graph, table_name, base_iri,
      table_row_count=0, max_rows_for_enumeration=1000,
      fk_info=None, skipped_tables=None) -> MergeResult
```

#### Inputs

| Argument | What it is |
|---|---|
| `accumulating` | The running cross-table `Graph` that grows with every table. |
| `fragment_graph` | The validated single-table graph being folded in. |
| `base_iri` | The ontology namespace — the shared URI prefix for classes/properties (e.g. `http://forgemesh.ai/ontology#`-style). |
| `table_row_count` | Row count of the current table; drives the enumeration sanity check. |
| `max_rows_for_enumeration` | Default `1000`; the row threshold above which a table should never be modelled as an enumerated value set. |
| `fk_info` | The *inspector's* real foreign-key list for the current table — a list of dicts with keys like `constrained_column`/`column` and `referred_table`/`ref_table`. |
| `skipped_tables` | The set of table names intentionally excluded from the ontology; their FKs are deliberately left as scalar `xsd:integer` (the XSD integer literal type). |

#### Output

A `MergeResult` dataclass (lines 40–48). The accumulating graph is mutated in place; `MergeResult` is the report the orchestrator consumes:

- `new_classes` — `List[AccumulatedClassSummary]`, fed to the embedding/index.
- `new_props` — URIs of newly added datatype/object properties (DatatypeProperty: an attribute whose value is a literal, e.g. a number or string; ObjectProperty: a link from one entity to another).
- `skipped_duplicates`
- `conflict_warnings`
- `domain_range_warnings`
- `enumeration_warnings`
- `auto_wired` — `"child → parent"` strings.

### 7.2 The merge() control flow

**In plain terms:** clean the incoming fragment, copy its brand-new things wholesale, and for anything that already exists, keep the existing definition while merging in extra labels.

#### Step 1 — Pre-merge fixers on the fragment

The first four lines of `merge` (lines 74–78) run the pre-merge fixers on the fragment, in order:

1. `_strip_self_referential_subprops`
2. `_split_multi_domain_properties`
3. `_strip_dual_annotated_object_properties`
4. `_fix_datatype_fk_properties` — only when `fk_info` is supplied.

#### Step 2 — Index the types

The merge proper indexes every `rdf:type` triple (a triple is one subject–predicate–object fact; `rdf:type` states what kind of thing a subject is) in both graphs into `existing_types` and `fragment_types` (subject-URI → set of type URIs, lines 84–91). It then defines the structural-type universe (lines 93–98) — the four OWL kinds a subject's identity hinges on:

```python
OWL_STRUCTURAL_TYPES = {OWL.Class, OWL.DatatypeProperty, OWL.ObjectProperty, OWL.AnnotationProperty}
```

#### Step 3 — Decide per subject

For every subject the fragment declares (lines 100–131), one of three branches fires:

- **New subject** (not in `existing_types`): all triples for that subject are copied via `_add_triple`. If it is an `owl:Class` (a category of things) it is recorded in `new_class_uris` for index building.
- **Existing subject, conflicting structural type**: if both sides assert structural types and `existing_structural != fragment_structural` (line 112) — e.g. the URI is already an `owl:DatatypeProperty` but the fragment now claims `owl:ObjectProperty` — then a conflict warning is appended to `result.conflict_warnings`, a `logger.warning` is emitted, and **the redeclaration is skipped (the existing type wins)**. This is the core "structural type-conflict detection."
- **Existing subject, same structural type**: the subject is recorded in `skipped_duplicates` (no `rdf:type` re-add).

#### Step 4 — Merge annotations (always)

Regardless of which existing-subject branch fires, **annotations are merged** (lines 124–131) over `(rdfs:label, skos:altLabel, rdfs:comment)`:

- `rdfs:label` (a human-readable display name) and `rdfs:comment` (a free-text description) use **keep-first**: if the accumulating graph already has that predicate on the subject, the fragment's value is dropped. So the first table to declare a class/property owns its label and comment.
- `skos:altLabel` (an alternative label — here, the original column name; the hard-coded `_SKOS_ALT_LABEL` URIRef at line 30) is **always merged**, making it multi-valued. Every table that touches the same entity can contribute another alternate label.

#### Step 5 — Blank-node subgraphs and remaining structural triples

- **Blank-node subgraph merge** (lines 137–141): every fragment subject that is an `rdflib.BNode` (an anonymous node with no global name, used as connective tissue) has all its triples copied wholesale. This carries OWL restrictions, `owl:oneOf` enumeration lists (a closed, enumerated list of allowed values), and `owl:unionOf` collections (an OR over classes) across intact.
- **Re-copy structural triples** (lines 143–148): *all* triples (domain, range, `subClassOf`, `subPropertyOf`, `inverseOf`, …) for the new entities are re-copied, ensuring non-`rdf:type` structural triples land for newly added subjects.

#### Step 6 — Reporting and post-merge fixers

After the structural merge, `merge` performs reporting and the post-merge fixers:

- **Domain/range warnings** (lines 150–171): every newly added datatype/object property is checked for missing `rdfs:domain` (the class a property is allowed to be used on) and `rdfs:range` (the type of value a property points at — a class, or a literal type like `xsd:string`); each absence is appended to `domain_range_warnings`.
- **AccumulatedClassSummary** (lines 173–184): for each new class, the label is taken from the accumulating graph's `rdfs:label` (falling back to the URI local name), `local_name` is the URI fragment, and `source_tables=[table_name]`.
- **Enumeration sanity check** (lines 186–197): if `table_row_count > max_rows_for_enumeration` and the table's primary class has an `owl:equivalentClass` (i.e. it was modelled as an enumerated/controlled vocabulary), a CRITICAL warning is logged — a high-row entity table should never be an enumerated type.
- **Post-merge fixers** (lines 213–220), in order: `_fix_xsd_range_on_subproperties`, `_autowire_subproperty_by_range`, `_strip_domain_from_abstract_parents`, `_scan_and_strip_self_ref_subprops`.

#### The single add site

`_add_triple` (lines 349–365) is the one place triples are added. It is a belt-and-suspenders guard that silently drops any `rdfs:subPropertyOf` (a triple saying one property is a more-specific version of another) where subject == object — even if the pre-merge strip missed it.

### 7.3 How each element is handled

This subsection walks through how the merge treats each kind of database element.

#### TABLES

The merge is keyed entirely on URIs, not tables, but `table_name` threads through everywhere:

- It tags `AccumulatedClassSummary.source_tables`.
- It scopes the enumeration check to the table's primary class (`base_iri + derive_class_local_name(table_name)`, line 188).
- It is the source-table label used in provenance synthesis.

**Skipped tables** matter structurally: `_fix_datatype_fk_properties` excludes FK columns whose `referred_table` is in `skipped_tables` (lines 645–646), deliberately leaving those as `DatatypeProperty xsd:integer`.

#### FIELDS / COLUMNS

Columns appear as datatype/object **properties**, never as graph subjects of their own type beyond property declarations.

A column's identity is tracked through three `fm:` annotations (ForgeMesh's own provenance vocabulary):

- `fm:sourceColumn` (`FM_SOURCE_COLUMN`) — the `"table.col"` string on a `DatatypeProperty`.
- `fm:sourceForeignKey` (`FM_SOURCE_FK`) — the `"table.col -> target"` string on an `ObjectProperty`.
- `fm:sqlType` / `fm:nullRate` — the column's SQL type and null fraction.

These annotations are the keys for the dual-annotation strip (§7.4), provenance synthesis, and the MIXED_RANGE source resolver.

**The cardinal rule** stated in the file (lines 837–842): column source must be keyed on the **inspector's** real `fk_info`/column types, *never* on the LLM-fabricated `fm:sourceForeignKey`.

#### VALUES

Values surface only as **controlled vocabularies** (fixed sets of allowed values). A class is a "well-formed vocab" per `_is_wellformed_vocab` (lines 886–894) if either:

- it has `owl:oneOf` / `owl:equivalentClass`, **or**
- it carries `NamedIndividuals` (`rdf:type cls` — concrete instances declared as members) **and** is *not* used as any property's `rdfs:domain` (i.e. it is a value type, not an entity).

The enumeration sanity check (line 187) and the R3 vocab tier of the MIXED_RANGE resolver both turn on this distinction between enumerated value sets and entity classes.

#### PRIMARY KEYS

Primary keys are **not** explicitly modelled in this stage — there is no PK-specific branch. PKs influence the merge only indirectly:

- A table's primary class derives from the table name.
- A PK column profiled as high-uniqueness feeds the `is_closed_vocabulary` decision (via `max_uniqueness_for_class`) that keeps such a column from being demoted to a controlled vocabulary in R3.

#### FOREIGN KEYS

FKs are the most actively repaired element:

- **`_fix_datatype_fk_properties`** (lines 613–690) converts FK columns the gen-LLM wrongly modelled as `DatatypeProperty xsd:integer` into `ObjectProperty` with the correct class range, keyed on the inspector's `fk_info` (see §7.4).
- **`_strip_dual_annotated_object_properties`** (§7.4) removes fabricated FK ObjectProperties keyed on the `fm:sourceColumn` vs `fm:sourceForeignKey` signature.
- **R1 of the MIXED_RANGE resolver** (§7.6) uses the inspector FK lookup to force a mixed-range property to `ObjectProperty` + class range.
- **`synthesise_missing_provenance`** stamps a degraded `fm:sourceForeignKey "not recoverable"` on ObjectProperties that lack one.

### 7.4 Pre-merge fixers (fragment-local)

**In plain terms:** four repairs that run on the single-table fragment in isolation, before it ever touches the shared graph — catching the OWL mistakes LLMs tend to make.

#### `_strip_self_referential_subprops` (lines 330–346)

Removes every `rdfs:subPropertyOf` triple where subject == object — a property declared as a subproperty of itself — logging a warning per removal.

#### `_split_multi_domain_properties(fragment_graph, base_iri)` (lines 778–827)

**Why it's hard:** a property with more than one `rdfs:domain` is illegal-by-conjunction in OWL. Multiple domains AND together, yielding the empty class over disjoint tables (OWL reads "two domains" as "must belong to *both* classes at once," which is impossible across separate tables).

For each such datatype/object property it:

- strips all `rdfs:domain` triples (promoting the original to an abstract parent), then
- mints one **class-scoped subproperty per domain**, each carrying that single domain, the original range, an `rdfs:subPropertyOf` link back to the abstract parent, and the original labels.

The scoped local name is built from the domain class and the concept: a `has…`-prefixed property `hasX` over domain `ClassA` becomes `hasClassAX`; otherwise `has{Class}{Prop.capitalize()}`.

#### `_strip_dual_annotated_object_properties` (lines 693–775)

Detects and removes the **fabricated-class hallucination signature**. The LLM sometimes emits *both* of these for the *same* source column — even though the inspector never flagged that column as an FK:

- a scalar `DatatypeProperty` (carrying `fm:sourceColumn "table.col"`), **and**
- an `ObjectProperty` (carrying `fm:sourceForeignKey "table.col -> InventedClass"`).

The function builds two maps keyed on `(table, col)`:

- `dt_col_keys` — from DatatypeProperty `fm:sourceColumn` values (split on `.`).
- `obj_col_keys` — from ObjectProperty `fm:sourceForeignKey` values (taking only the **left/source** side before `->`, split on `.`).

For each `(table, col)` in the intersection, the **scalar side is authoritative** and the ObjectProperty is stripped — all triples with it as subject *and* as object are removed. If the stripped property's range class was itself declared in this fragment as an `owl:Class` and is no longer referenced by any other `rdfs:range`, that **fabricated class is stripped too** (lines 755–773).

#### `_fix_datatype_fk_properties(fragment_graph, base_iri, fk_info, skipped_tables)` (lines 613–690)

The FK-type safety net. It builds `fk_col_to_range` = `lower(col_name) → URIRef(base_iri + derive_class_local_name(ref_table))` from the inspector's `fk_info`, **excluding FKs whose `referred_table` is in `skipped_tables`** (those stay `xsd:integer` by design).

Then, for every `DatatypeProperty` in the fragment whose lower-cased `skos:altLabel` matches an FK column name, it:

- removes the `DatatypeProperty` type and adds `ObjectProperty`;
- removes any XSD range and, if no range remains, adds the referenced class URI as `rdfs:range`;
- if a non-XSD range is already present but differs, overwrites it with the FK target class.

The fix is logged at `logger.error` level. The docstring notes that `gold/builder.py._resolve_fk_properties()` and `rdf/triple_builder.TripleBuilder` detect FKs by column name (entity_map), so they work regardless — but this fix makes the *ontology* itself correct (FK columns are always ObjectProperty).

### 7.5 Post-merge safety-net fixers (graph-wide)

**In plain terms:** four repairs that run on the whole *accumulating* graph after the structural merge — fixing problems that only appear once fragments from different tables sit side by side.

#### `_fix_xsd_range_on_subproperties(accumulating, new_obj_prop_uris)` (lines 573–610)

When a new ObjectProperty has an `rdfs:subPropertyOf` parent but its own `rdfs:range` is an XSD datatype (the gen-LLM wired the hierarchy correctly but declared `Range: xsd:integer`), the XSD range is removed and replaced with the abstract parent's named-class range — unless the parent's range is *also* XSD (then it is left alone).

#### `_autowire_subproperty_by_range(accumulating, new_obj_prop_uris, result)` (lines 518–571)

Builds `abstract_by_range` = `range_uri → [abstract ObjectProperty…]`, where "abstract" means an ObjectProperty **with no `rdfs:domain`**. For each new ObjectProperty that has a domain but no `subPropertyOf` and has a range, it looks up abstract parents sharing that range:

- **exactly one match** → add `rdfs:subPropertyOf` and append a `"child → parent"` string to `result.auto_wired` (logged INFO);
- **multiple matches** → log a WARNING and do *not* wire (ambiguous).

#### `_strip_domain_from_abstract_parents(accumulating)` — the union-domain rewrite (lines 407–489)

**Why it's hard:** an abstract parent property shared across several tables (e.g. `hasPatient`) has children with different domains. Leaving the parent's own `rdfs:domain` would AND across disjoint table classes (empty intersection → unsatisfiable property). The fix gives the parent a *union* domain instead of dropping the domain entirely (which would lose the source anchor).

It groups children by `rdfs:subPropertyOf` target (`children[parent] = {kids}`, ignoring self-links). For a parent with **≥2 distinct children**:

- `union_classes` is the sorted set of `URIRef` domains across all kids.
- If `union_classes` is non-empty, the prior domain is cleared via **`_clear_property_domain`** and rebuilt via **`_set_union_domain`**. `_set_union_domain` (lines 390–404) emits either a single plain `rdfs:domain ClassURI` (one class) or one `rdfs:domain [ owl:unionOf ( ClassA ClassB … ) ]` restriction (several classes — a BNode of `rdf:type owl:Class` with an `owl:unionOf` `Collection`). A disjunctive union is the only domain form that stays satisfiable across disjoint classes.

Three edge cases:

- **Idempotency** (lines 467–477): a no-op happens *only* when the parent's current domain already equals `union_classes` **and** is in canonical single-object form (`len(existing_domain_objs) == 1`). Multiple plain `rdfs:domain` triples expanding to the same class set are still collapsed into a union. Adding a child rebuilds the union and cleans up the prior restriction BNode.
- **Fallback** (lines 478–487): when there are ≥2 children but *no* determinable named-class child domains, it falls back to the older behaviour — `_clear_property_domain` strips the parent's own domain so the conjunction trap is still avoided.
- **Single-child case** (lines 443–450): a parent with exactly one subproperty keeps its domain unchanged (it may be a legitimate single-domain parent not yet promoted to abstract); a warning is logged if it has a domain.

Supporting helper: `_clear_property_domain` (lines 378–388) removes every `rdfs:domain`, and for any BNode domain also unwinds its `owl:unionOf` `rdf:first`/`rdf:rest` list via **`_remove_rdf_list`** (lines 368–376) and removes the restriction node — so re-running leaves no orphan triples (the basis of idempotency).

#### `_scan_and_strip_self_ref_subprops(accumulating)` (lines 492–512)

The final pass; removes any `rdfs:subPropertyOf` where subject == object from the *whole* accumulating graph, catching triples that entered via a previous run, a loaded OWL file, or any path that bypassed the fragment-level strip.

### 7.6 The MIXED_RANGE resolver

**In plain terms:** a property that claims to point at *both* a class *and* a plain literal type at once is broken OWL. This resolver figures out, deterministically, which one the property really meant — preferring hard facts (real FKs, SQL types, vocab structure) and routing to a human only when it genuinely can't tell.

A property carrying **both** an ontology-namespace class range **and** an XSD literal range is unsatisfiable OWL — `range_coherence_check` flags it as `MIXED_RANGE`. `resolve_mixed_ranges` (lines 928–1019) is the deterministic repair; its trigger is exactly that definition (line 961), so it is a provable no-op on any graph with zero mixed ranges (e.g. the gold ontology).

Results accumulate in a `MixedRangeResolution` dataclass (lines 868–883) with buckets `resolved_fk`, `resolved_scalar`, `resolved_vocab_class`, `resolved_vocab_literal`, `routed_vocab`, and `unkeyable`, plus a `summary()` string.

#### The resolution tiers

For each property with both a class range and an XSD range, it derives `(table, col)` via the injected `get_source` callable, then applies tiers in order:

- **R1 — FK → class** (lines 968–975): if `(table, col)` is in `fk_lookup` (the **inspector's** real FK map), `_force_object_property` removes all ranges, swaps the type to `ObjectProperty`, and sets `rdfs:range` to `namespace + ref_class`; recorded in `resolved_fk`.
- **R2 — scalar → literal** (lines 982–990): if **not** an FK *and* either the column's SQL type is temporal (`DATE`/`DATETIME`/`TIME`/`TIMESTAMP`) or non-integer-numeric (`DOUBLE`/`FLOAT`/`DECIMAL`/`NUMERIC`/`REAL`), **or** the class range is **not** a well-formed vocab (`_is_wellformed_vocab` is false → a hallucinated relationship onto a plain attribute), then `_force_datatype_property` swaps to `DatatypeProperty` with the XSD range mapped from `_SQL_TO_XSD`; recorded in `resolved_scalar`.
- **R3 — vocab via `vocab_decider`** (lines 994–1013): if the class range *is* a well-formed `owl:oneOf` vocabulary, the choice is genuinely between a vocab class and an `xsd` literal. A `vocab_decider(table, col)` returns:
  - **`"CLASS"`** → `_force_object_property` (keep vocab, `resolved_vocab_class`);
  - **`"LITERAL"`** → `_force_datatype_property` (demote to xsd, `resolved_vocab_literal`);
  - **neither/`None`** → routed to the human coherence queue (`routed_vocab`).
- **unkeyable** (lines 1015–1017): if `(table, col)` could not be determined, the property is routed, never guessed.

Both `_force_object_property` and `_force_datatype_property` (lines 910–925) also call **`_drop_crosskind_subproperty`** (lines 897–907), which removes any `rdfs:subPropertyOf` link to a parent of the *opposite* kind (e.g. an ObjectProperty still pointing at a DatatypeProperty parent after the swap).

#### The post-loop wrapper and the source keyer

**`resolve_mixed_ranges_in_graph`** (lines 1022–1121) is the post-loop wrapper that builds the lookups from inspector `profiles`. It populates:

- `fk_lookup` — `(table, col) → ref_class_local`
- `type_lookup` — `(table, col) → SQL type`
- `stats_lookup` — `(table, col) → (null_rate, distinct, non_null)`
- `class_to_table`

Its `vocab_decider` defers to `is_closed_vocabulary(distinct, null_rate, non_null, …)` on the column stats, returning `"CLASS"` or `"LITERAL"` (or `None` when no stats exist → route).

Its **`get_source` keyer** (lines 1079–1117) embodies the cardinal rule — *prefer the most trustworthy provenance signal first* — in three ordered steps:

1. **`fm:sourceColumn` `"table.col"`** — authoritative, ordered *first* so it pre-empts the one known case where the FK annotation's source table is wrong (e.g. `los`/`outtime` citing `raw_transfers` but really from `raw_icustays`).
2. **`fm:sourceForeignKey` `"table.col -> X"`** — trust **only the left/source side**, and only when that column exists in `type_lookup` **and** is consistent with the property's `rdfs:domain` (`dom_tables`). The right/target side (the LLM-fabricated part) is never used. This recovers the column for categorical/vocab properties carrying no `fm:sourceColumn`.
3. **`rdfs:domain` (union-expanded) + range/FK match** — for each domain table, find an FK column whose target class matches one of the property's class ranges; failing that, return `(table, None)` (table known, column unknown — enough to route a vocab).

The docstring stresses ordering: this resolver must run **after** union domains are set (so shared abstracts like `hasPatient` are keyable via their `rdfs:domain`) and **after** `owl:oneOf` vocabularies are built (so R3 vocab detection is accurate).

### 7.7 Standalone guards

Two extra guards run outside the main merge flow.

#### `check_duplicate_property_types(fragment_graph)` (lines 309–323)

Returns sorted URIs declared as **both** `owl:ObjectProperty` and `owl:DatatypeProperty` within the same fragment (structurally invalid OWL), logging a warning each — intended to be caught before merge.

#### `synthesise_missing_provenance(accumulating, new_class_uris, new_prop_uris, table_name)` (lines 225–288)

A degraded-fallback sweep over newly merged classes/properties that still lack required `fm:` provenance:

| Element | Missing annotation | Synthesised value |
|---|---|---|
| Class | `fm:sourceTable` | the local name |
| ObjectProperty | `fm:sourceForeignKey` | `"not recoverable"` |
| DatatypeProperty | `fm:sourceColumn` | `"table.local"` |
| DatatypeProperty | `fm:sqlType` | `"unknown"` |
| DatatypeProperty | `fm:nullRate` | `"unknown"` |

It returns the list of violation URIs and warns for each synthesis.

#### `remove_legacy_provenance_comments(graph)` (lines 291–306)

Strips any `rdfs:comment` whose value contains the string `"Derived from"` (legacy provenance now carried structurally via `fm:` annotations), returning the count removed.

### 7.8 Connection to neighbours

**Upstream**, this stage consumes one validated `OntologyFragment.graph` per table from the iterative generator (gen-LLM + judge-LLM), plus the inspector's `fk_info`/`profiles`.

**Downstream**, the repaired accumulating graph and the `MergeResult.new_classes` (as `AccumulatedClassSummary`) feed the embedding/context index used for subsequent tables, and ultimately:

- the GOLD mapping layer (`gold/builder.py`), and
- RDF materialisation (`rdf/triple_builder.TripleBuilder`).

The merger's invariants — FK columns are ObjectProperties, abstract parents carry union domains, no self-referential subproperties, no mixed ranges — are precisely what those downstream layers rely on to produce a coherent, satisfiable ontology and correct triples.

---

## 8. Generator Orchestration & the Post-Loop Passes

**In plain terms:** one class, `IterativeOntologyGenerator`, is the "main loop" that turns table profiles into an OWL file. It walks tables one at a time through an LLM-generate / LLM-judge cycle, accumulates the results into a single in-memory graph, and then runs a long, strictly-ordered chain of deterministic repair passes over that graph before writing it to disk.

**Inputs → Outputs:** a list of `TableProfile` objects (database schema + statistics) goes in; a `.owl` file (RDF/XML) comes out. The shared, mutating `rdflib.Graph` (an in-memory store of RDF triples — subject–predicate–object facts) is the data structure threaded through every step.

### What this section covers

`IterativeOntologyGenerator` lives in `semantic_mapper/ontology/iterative/generator.py` and is the ontology generator of record. Its single public entry point is `generate(profiles: List[TableProfile]) -> Path` (line 99).

That one method drives the whole RIGOR-style pipeline:

- reads settings,
- builds an accumulating `rdflib.Graph`,
- walks the tables one at a time through a gen-LLM / judge-LLM loop,
- then runs a long, **ordered** chain of deterministic post-loop passes that repair, deduplicate, and complete the merged graph before serialising it to OWL/XML.

This section documents the orchestration first, then **every post-loop pass in execution order**, paying explicit attention to how each one treats tables, fields/columns, values, primary keys (PKs), and foreign keys (FKs).

#### Inputs and outputs in detail

- **Input:** `List[TableProfile]` (from `semantic_mapper.db.models`). Each profile carries:
  - `table_name`,
  - `row_count`,
  - `columns: dict[str, ColumnProfile]` — where each `ColumnProfile` has `inferred_type`, `null_rate`, `distinct_count`, `is_primary_key`,
  - `foreign_keys: list[dict]`.
- **Output:** a `Path` to a written `.owl` file (RDF/XML), named by `ontology_filename(out_dir, project, "rigor", gen_model)`. The accumulating `rdflib.Graph` is the in-memory structure threaded through every pass.

### 8.1 Settings read and run setup

#### Deriving the base IRI

**In plain terms:** every class and property needs a globally-unique name prefix (an IRI/URI — a globally-unique name for a thing). This step decides that prefix once, up front, so the same concept gets the same URI across related datasets.

`generate` first resolves the **base IRI** via `_derive_base_iri` (line 483):

- `ONTOLOGY_NAMESPACE` wins when set:
  - a bare token becomes `http://mesh.local/ontology/{token}#`,
  - a full IRI is used verbatim.
- otherwise it falls back to the bronze DB name, with the `bronze` storage-layer token stripped.

This is how a dataset family (e.g. `winterolympics` 2026 + 2030) shares one source/year-agnostic namespace, so classes, properties, and value-mapped vocabularies get the *same URIs* across years.

#### PK-denotes-entity class renames

Before any URI is derived, `compute_class_name_overrides(profiles)` + `set_class_name_overrides(...)` (lines 109–110) install **PK-denotes-entity** renames.

Example: a per-country medal-tally table `medals`, keyed by `country_code`, becomes the class `Country`.

Setting overrides up front means prompts, FK ranges, and merges all use the corrected name from the start.

#### Settings table

All settings are read via `getattr(self.settings, ...)` with defaults:

| Setting | Default | Role |
|---|---|---|
| `ONTOLOGY_GEN_MAX_RETRIES` | 2 | gen/judge retry budget per table |
| `ONTOLOGY_GEN_TOP_K_CLASSES` | 10 | top-k declared classes retrieved for context |
| `ONTOLOGY_GEN_MODEL` / `ONTOLOGY_JUDGE_MODEL` | `LLM_MODEL`/`OPENAI_MODEL` | gen vs judge model |
| `ONTOLOGY_GEN_TEMPERATURE` | 0.1 | gen temperature |
| `ONTOLOGY_GEN_JUDGE_TEMPERATURE` | 0.0 | judge temperature |
| `ONTOLOGY_JUDGE_CONFIDENCE_FLOOR` | 0.6 | confidence at which a non-approved verdict is accepted |
| `ONTOLOGY_MAX_ROWS_FOR_ENUMERATION` | 1000 | row cap above which a table's class must NOT be enumerated |
| `ONTOLOGY_MAX_DISTINCT_FOR_ENUMERATION` | 20 | distinct cap for a column to become a controlled vocabulary |
| `ONTOLOGY_MAX_DISTINCT_FOR_OPEN_CLASS` | 100 | open-class distinct cap |
| `ONTOLOGY_MIN_ROWS_FOR_GENERATION` | 0 | tables at/below this row count are skipped |
| `ONTOLOGY_CATEGORICAL_MAX_NULL_RATE` / `_MIN_SUPPORT` / `_MAX_UNIQUENESS` | 0.90 / 0 / 1.0 | proportional class-vs-literal thresholds (`DEFAULT_*_FOR_CLASS` in `context_builder.py`) |
| `ONTOLOGY_COL_WORD_SPLITS` | `{}` | domain word-split map (e.g. `dischtime → discharge_time`) used by `canonical_local` |

#### Table skipping and ordering

- **`skip_tables`** = every profile whose `row_count <= ONTOLOGY_MIN_ROWS_FOR_GENERATION` (each logged as a warning).
- The remaining tables are sorted by `fk_topological_sort(profiles, skip_tables=skip_tables)` so dimension/reference tables come first and fact tables last.
- Skipped tables are excluded from the sort so downstream FK dependencies on them don't block it.

#### Graph initialisation

An empty `Graph` is created and bound with prefixes:

- `""` → base,
- `owl`, `rdfs`, `skos`, `xsd`,
- `fm` → `http://forgemesh.ai/ontology#`.

It is then stamped with the triple `(<base>, rdf:type, owl:Ontology)` (declaring the graph itself an ontology).

If a sibling `fm_annotations.owl` exists next to `SEED_OUTPUT_PATH`, all of its triples are merged in (lines 164–170) so the `fm:` annotation vocabulary is present.

Finally, an `AccumulatedClassIndex` is created as the retrieval/embedding index. It uses a sentence-transformer model from `SEMANTIC_MATCHER_MODEL` (default `all-MiniLM-L6-v2`).

### 8.2 The per-table loop

**In plain terms:** for each table (in dependency order), the generator gathers context, computes the class name deterministically, asks the LLM to write the ontology fragment, has a second LLM judge it, retries if needed, and merges the approved fragment into the accumulating graph — stamping provenance as it goes.

The loop runs `for i, profile in enumerate(ordered, ...)` (line 177). For each profile in topo order:

#### Step 1 — Build context

`build_table_context(profile, ...)` produces a `TableContext`. Its `columns` list of `ColumnContext` carries `inferred_type`, `null_rate`, `is_fk`, `fk_ref_table`, and `fk_ref_skipped`. The categorical thresholds and distinct caps are passed in here.

#### Step 2 — Deterministic class URI

`derive_class_uri(profile.table_name, base_iri)` (line 187) computes the class URI deterministically *before* the LLM is called. The LLM is then told to use it.

#### Step 3 — Retrieve context for the prompt

`index.retrieve(table_name, top_k)` returns the most semantically similar already-declared classes.

Abstract properties (properties with no domain attached, meant to be shared across tables) are retrieved twice:

- `retrieve_abstract_properties(query=table_name…, top_k=8)`, then
- **per-FK** `retrieve_abstract_object_properties(query=fk_ref_table…, top_k=3)` for each non-skipped FK column (lines 208–216).

Querying by the *referenced table name* surfaces shared abstract object-properties (an ObjectProperty is a link from one entity to another) like `hasAdmission`, `hasPatient`, `hasIcustay` — even when the current table name doesn't match. This is the mechanism that lets FK columns reuse one abstract relation across many tables.

#### Step 4 — Build the gen prompt

`build_gen_prompt(table_ctx, retrieved_classes, deterministic_uri, base_iri, existing_property_locals, abstract_properties, max_rows_for_enumeration)`.

`existing_property_locals` is the live list of already-declared datatype/object property local names, so the LLM can reuse rather than re-mint properties. (A DatatypeProperty is an attribute whose value is a literal, e.g. a number or string.)

#### Step 5 — Gen/judge retry loop

Runs `for attempt in range(max_retries + 1)`:

- `_call_gen_llm` (gpt-style, `json_mode=False`, `disable_thinking=True`) returns Manchester Syntax (a human-readable OWL notation); tokens accumulate into `prompt_tokens` / `completion_tokens`.
- `parse_manchester(manchester, base_iri, table_name)` → `(fragment_graph, classes_declared, props_declared)`, wrapped in an `OntologyFragment` (which records `retry_count`).
- **Dual-type pre-check:** `check_duplicate_property_types(fragment_graph)` (in `graph_merger`) returns any URI declared as *both* `owl:ObjectProperty` and `owl:DatatypeProperty` — structurally invalid OWL — and logs it (does not block).
- `build_judge_prompt(...)` is passed the full accumulated class list (`index._classes`) for duplicate detection. `_call_judge_llm` returns a `JudgeVerdict` (`approved`, `confidence`, `coverage_issues`, `coherence_issues`, `revision_instructions`). A judge failure is caught and treated as `approved=True, confidence=0.3`.
- **Accept rule:** `approved = verdict.approved or verdict.confidence >= judge_confidence_floor`.
  - If approved, or the retry budget is exhausted (`attempt == max_retries`), the loop breaks. On exhaustion the partial fragment is merged anyway, with a warning.
  - Otherwise `rebuild_with_revision(original_prompt, verdict.revision_instructions, previous_output)` builds the next attempt's prompt.

#### Step 6 — Per-table postconditions (log-only)

If the fragment is non-empty, two checks run and any violations are logged (see §8.5):

- **G1** — `check_no_scalar_as_object_property(fragment_graph, table_ctx.columns)`.
- **G3** — `check_no_cross_table_property_reuse(fragment_graph, accumulating_graph, det_uri)`.

#### Step 7 — Merge into the accumulating graph

`merge(accumulating, fragment_graph, table_name, base_iri, table_row_count, max_rows_for_enumeration, fk_info=profile.foreign_keys, skipped_tables)` returns a `MergeResult` (`new_classes`, `new_props`, `skipped_duplicates`, plus conflict / domain-range / enumeration warnings).

`merge` first runs pre-merge fragment cleanups:

- `_strip_self_referential_subprops`,
- `_split_multi_domain_properties`,
- `_strip_dual_annotated_object_properties`,
- and (with `fk_info`) `_fix_datatype_fk_properties`.

`merge` is URI/type-aware:

- a same-URI / same-type subject is skipped — annotations are merged (`rdfs:label` / `rdfs:comment` keep-first, `skos:altLabel` (an alternative label — here, the original column name) always merged);
- a same-URI / **different**-type subject is a conflict and is *not* redeclared.

After adding, `merge` auto-wires object sub-properties by range, fixes XSD range on sub-properties, calls `_strip_domain_from_abstract_parents`, and `_scan_and_strip_self_ref_subprops`.

#### Step 8 — Index + provenance update

- New classes go into the embedding index (`index.add_all`).
- New abstract (domain-free) properties are extracted (`_extract_new_abstract_properties`) and indexed.
- `_add_provenance_annotations(accumulating, merge_result, table_ctx)` stamps `fm:` provenance (detailed below).
- `synthesise_missing_provenance(...)` is a safety sweep that fills degraded fallbacks: `fm:sourceTable`, `fm:sourceColumn` / `fm:sqlType` / `fm:nullRate`, or `fm:sourceForeignKey "not recoverable"`.

#### Provenance stamping (`_add_provenance_annotations`, line 563)

This handles each entity kind explicitly:

- **CLASSES** get `fm:sourceTable "{table_name}"`.
- **FIELDS (DatatypeProperties)** get `fm:sourceColumn "{table}.{col}"`, `fm:sqlType`, and `fm:nullRate "{rate:.2f}"`, keyed via the property's first `skos:altLabel` (the source column name).
- **FOREIGN KEYS (ObjectProperties)** get `fm:sourceForeignKey "{table}.{col} -> {target}"`.
  - If the LLM already stamped one, it's trusted.
  - Otherwise four fallback strategies recover the source column, in order:
    1. `skos:altLabel` is a known FK column;
    2. `rdfs:range` (the type of value a property points at — a class, or a literal type like `xsd:string`) class local name maps back to a FK source column;
    3. `skos:altLabel` matches any known column (categorical promotion);
    4. `_camel_to_snake(range_local)` matches a column.
  - On total failure it writes `"not recoverable"`.

### 8.3 Post-loop passes (in execution order)

**In plain terms:** once all tables are merged, the LLM's work is "good but messy." These 18 deterministic passes clean it up — renaming, deduplicating, filling gaps, abstracting FK relations, building value enumerations, and fixing structurally-invalid OWL.

Once every table is merged, `generate` runs the following deterministic passes **strictly in this order** (lines 364–469). Many depend on the state left by the previous one, so **order is load-bearing**.

#### Pass 1 — `normalize_datatype_property_uris(graph, base_iri, word_splits)` (`property_namer.py`)

Renames every `owl:DatatypeProperty` whose URI is not yet canonical to `has{CamelCase}`.

- **Source priority:** `skos:altLabel` (the original **column** name) first; else the URI local part, if not already canonical.
- **Naming rules** (`canonical_local`): strip leading `has_` / `has`, lowercase, apply `word_splits` (e.g. `dischtime → discharge_time`), split on separators, CamelCase, prepend `has`. Examples: `admission_location → hasAdmissionLocation`, `hadm_id → hasHadmId`.
- **ObjectProperties are deliberately left untouched** — they are named after their target entity, not the source column.
- Type-collision handling:
  - if the canonical URI is already an `owl:ObjectProperty`, the DatatypeProperty is a wrong-type duplicate and is **removed** (its `rdfs:domain` is first transferred to the ObjectProperty);
  - if it's an existing DatatypeProperty, the rename is skipped, deferring to the later dedup pass.

(`rdfs:domain` = the class a property is allowed to be used on.)

#### Pass 2 — `_qualify_obj_props_safety(graph, base_iri)` (generator.py, line 676)

**FK-relationship abstraction.** For each `owl:ObjectProperty` with **≥2 distinct `rdfs:domain`**, this:

- creates one abstract parent `has{RangeLocal}` (no domain), and
- makes each domain-specific property `rdfs:subPropertyOf` it.

Details and guards:

- Skips the case where all share one domain (those are pure duplicates, left for dedup).
- Abstract parents are forcibly stripped of any `rdfs:domain`.
- A self-referential `subPropertyOf` guard prevents a property becoming its own parent.

This is the graph-level mirror of the legacy `FullOntologyGenerator._qualify_obj_props`.

#### Pass 3 — `_collapse_domain_subproperty_duplicates(graph)` (line 747)

Collapses redundant LLM patterns where a domain-scoped DatatypeProperty is a specialisation of an abstract parent that shares its `skos:altLabel`.

- Example: `hasAirportAirportId [domain=Airport] subPropertyOf hasAirportId [no domain]` (both have `altLabel="airport_id"`) → the domain is **hoisted** onto `hasAirportId` and the redundant child is deleted.
- **Conditions:** child is a DatatypeProperty with a domain; parent is a domain-free DatatypeProperty; both share a case-insensitive `altLabel`.
- *Must run before gap-fill* so the cleaned state is visible to the next pass.

#### Pass 4 — `_fill_missing_column_properties(graph, ordered, base_iri, word_splits)` (line 807)

**Completeness guarantee on FIELDS.** For every table class in the graph and every column that is **not a PK and not a FK**, if no property already covers `(altLabel, domain)`, it creates a DatatypeProperty from the schema profile: `rdf:type`, `rdfs:domain`, `rdfs:range` (from `_SQL_TO_XSD`), `rdfs:label`, derived `rdfs:comment`, `skos:altLabel`, plus `fm:sourceColumn` / `fm:sqlType` / `fm:nullRate`.

Explicit handling of edge cases:

- **PRIMARY KEYS** (`col_profile.is_primary_key`) are skipped — not minted as properties.
- **FOREIGN KEYS** are skipped two ways:
  - via `fk_cols` (built from `constrained_column` / `column`), **and**
  - via an `obj_covered` index derived from existing `fm:sourceForeignKey` annotations — so a column already modelled as an ObjectProperty (even under a mismatched URI) is not re-minted as a scalar.
- If the canonical URI is already an ObjectProperty, the column is a relationship: only the missing `rdfs:domain` is filled.
- If the canonical name collides with a *different* class's property, a **class-qualified** URI `has{ClassLocal}{Concept}` is created instead (the qualified fallback).

#### Pass 5 — `_strip_dual_annotated_object_properties(graph)` (graph_merger, line 693; called at generator line 377)

Re-run on the *full* accumulated graph after gap-fill.

- Detects the hallucination signature where one column appears as both `fm:sourceColumn` (DatatypeProperty) and `fm:sourceForeignKey` (ObjectProperty).
- **DatatypeProperty wins:** the fabricated ObjectProperty is removed, and its fabricated range **class** is removed too if no longer referenced.
- Effect on VALUES/FK fabrications: a column the inspector never flagged as a FK does not get a phantom relationship.

#### Pass 6 — `_infer_missing_domains_and_ranges(graph, ordered, base_iri)` (line 1048)

Profile-backed repair of three failure modes:

- **(a) Missing `rdfs:domain`** — matched from `skos:altLabel` (= column name) when *unambiguous* (the column appears in exactly one table); else by stripping `has` / `is` / `of` and trying `_domain_candidates` (handles `*Type` suffixes). Abstract parents (those that have sub-properties) are deliberately left domain-free.
- **(b) Missing/wrong DatatypeProperty range** — re-inferred from the column's SQL `inferred_type` via `_SQL_TO_XSD`.
- **(c) Missing ObjectProperty range** — recovered by matching the stripped local name (minus any `For<Class>` suffix) against declared **class** local names.

#### Pass 7 — `_strip_domain_from_abstract_parents(graph)` (graph_merger, line 407 — first of two calls)

Authoritative sweep over shared abstract parents.

- Any property that is the `rdfs:subPropertyOf` target of **≥2** subjects is treated as a shared abstract parent.
- Multiple `rdfs:domain` triples are an OWL *conjunction* — across disjoint table classes this is empty (unsatisfiable). So the parent's domain is replaced by the **union** of its children's named-class domains, expressed as a single `rdfs:domain [ owl:unionOf (...) ]` (`owl:unionOf` = an OR over classes), or one plain class if there's only one.
- When no child domain is determinable, it falls back to removing the domain.
- Idempotent. A single-child parent keeps its domain (logged).

#### Pass 8 — `_remove_superseded_fk_datatype_abstracts(graph, ordered, base_iri)` (line 1497)

**FK-driven cleanup.**

- Builds `lower(fk_col) → ref_class_local` from each profile's `foreign_keys` (`constrained_column` / `column` + `referred_table` / `ref_table`).
- An abstract (domain-free) DatatypeProperty whose `skos:altLabel` matches a FK column name (e.g. `subject_id`) is removed **iff** the corresponding abstract ObjectProperty (`has{RefClass}`, e.g. `hasPatient`) already exists — along with all its class-scoped sub-properties.
- Rationale: once FK columns are correctly object-modelled, `hasSubjectId`-style scalar abstracts are stale artefacts.
- Safe to re-run.

#### Pass 9 — `_deduplicate_properties(graph)` (line 1285)

Groups DatatypeProperties by `skos:altLabel` (= source **column**). For each multi-member group it:

1. elects one winner by score:
   - `+3` if the local name starts with `has`,
   - `+2` for a longer / more-qualified local name,
   - `+1` for no underscore,
   - alphabetical tie-break;
2. merges all losers' `rdfs:domain` (and range, if the winner had none) into the winner;
3. deletes the losers and their triples.

Note: merging multiple domains into one winner is what *creates* the multi-domain properties that the next pass splits.

#### Pass 10 — `_split_multi_domain_dt_properties(graph, base_iri, word_splits)` (line 1355)

Inverse of dedup, at class granularity.

- Every DatatypeProperty with **>1 `rdfs:domain`** is split into one class-scoped sub-property `has{DomainClass}{Concept}` per (named-class) domain.
- All scoped sub-properties hang off a truly abstract parent `has{Concept}`, derived from the `skos:altLabel` (**not** the dedup winner's local name) so the root is table-neutral — e.g. `hasItemid`.
- BNode (union/restriction) domains are skipped — you can't name a clean sub-property from them.
- Capitalisation tolerance reuses the original property when the scoped name matches case-insensitively (`hasInputeventsMvItemId ↔ hasInputeventsMvItemid`).
- *Must run after dedup.*
- Then **G2** `check_split_column_coverage` is run against `table → class` URIs and violations are logged.

#### Pass 11 — `consolidate_enumeration_classes(graph, min_overlap=0.6)` (`enum_consolidation.py`)

**VALUE-vocabulary merge.** Per-table generation can mint `Gender` from one table and `GenderType` from another with overlapping members; this pass merges such duplicates.

- Considers only `owl:Class` nodes with **≥2 member NamedIndividuals** (entity classes are untouched).
- Groups them by normalised stem (`_vocab_stem` strips wrapper suffixes `type` / `enum` / `class` / `category` / `kind`, but **not** `code`).
- Merges duplicates into a canonical class **only when both** a stem match **and** label overlap hold (subset, or Jaccard ≥ 0.6).
- It retypes members by label, repoints property ranges/domains, and drops the duplicate class.
- Conservative by design (a `DisciplineCode`-vs-`Discipline` pair never merges) and dataset-agnostic.
- Runs *before* oneOf construction so vocabularies are built on the merged class.

#### Pass 12 — `_build_oneof_enumerations(graph, excluded_class_uris, base_iri)` (line 1224)

For each class with NamedIndividual members it:

- **(a)** canonicalises individual URIs to `{Value}{ClassName}` (e.g. `Female → FemaleGender`, rewriting all references including existing oneOf lists), and
- **(b)** adds `owl:equivalentClass [ owl:oneOf ( ... ) ]` (`owl:oneOf` = a closed, enumerated list of allowed values) if not already present.

`excluded_class_uris` = every table class whose `row_count > max_rows_for_enum`, so a high-cardinality **entity** class (PK-keyed) is never frozen into a closed value enumeration. (The `merge` pass independently logs `CRITICAL` if such a class was enumerated.)

#### Pass 13 — `_strip_domain_from_abstract_parents(graph)` (second call, line 435)

Re-run immediately before MIXED_RANGE resolution to refresh union domains, because dedup/split mutated abstract parents' children. Shared abstracts like `hasPatient` must be **keyable from their `rdfs:domain`** by the resolver's source recovery.

#### Pass 14 — MIXED_RANGE resolution — `resolve_mixed_ranges_in_graph(graph, base_iri, ordered, …thresholds)` (graph_merger, line 1022; core `resolve_mixed_ranges` line 928)

**In plain terms:** a property that claims both a class range and a literal range is invalid OWL. This pass decides, per property, whether it should be a relationship (class range) or an attribute (literal range) — keying on the inspector's real schema, never the LLM's guesses.

A property carrying **both** an ontology-class range and an XSD range is unsatisfiable OWL (this is `range_coherence_check`'s `MIXED_RANGE`).

The wrapper builds, *from the inspector profiles*:

- `fk_lookup {(table,col): ref_class_local}` (real FKs only),
- `type_lookup {(table,col): SQL_TYPE}`,
- `stats_lookup` (null-rate / distinct / non-null support).

The **cardinal rule:** key on the inspector's real FK info and column types, **never** on the LLM's (fabricated) `fm:sourceForeignKey`.

Source recovery (`get_source`) is ordered:

1. `fm:sourceColumn` "table.col" — authoritative; pre-empts the case where the FK annotation cites the wrong table (e.g. `los` / `outtime`);
2. the **left/source side only** of `fm:sourceForeignKey`, accepted only if it names a real column consistent with the domain;
3. `rdfs:domain` (union-expanded) + range/FK match.

Resolution tiers (`MixedRangeResolution` dataclass):

- **R1** — source column **is a real FK** → `_force_object_property`: becomes ObjectProperty + class range, XSD dropped.
- **R2** — not a FK **and** (temporal / non-integer-numeric SQL type, **or** range-class is not a well-formed vocab) → `_force_datatype_property`: DatatypeProperty + XSD range, class dropped. This is the deterministic fix for hallucinated relationships onto plain scalar **VALUES**.
- **R3** — range-class **is** a well-formed `owl:oneOf` vocabulary (`_is_wellformed_vocab`): decided by `vocab_decider`, which runs `is_closed_vocabulary(distinct, null_rate, non_null, …)` on the column's stats → `CLASS` (keep vocab) or `LITERAL` (demote to xsd). With no inspector stats it is genuinely ambiguous and is **routed to the coherence queue** (left unchanged, logged for human review — class-vs-`xsd:string` is a modelling decision).
- **unkeyable** — source (table,col) not determinable → routed, never guessed.

After forcing, `_drop_crosskind_subproperty` removes any now-wrong `subPropertyOf` link to a parent of the opposite kind. Results are summarised via `mixed.summary()`.

#### Pass 15 — `_scan_and_strip_self_ref_subprops(graph)` (graph_merger, line 492)

Final sweep removing any `rdfs:subPropertyOf` where subject == object — catching triples introduced by the post-loop passes that bypassed the per-merge guard.

#### Pass 16 — `remove_legacy_provenance_comments(graph)` (graph_merger, line 291)

Removes every `rdfs:comment` whose value contains the string `"Derived from"` — legacy/stale provenance comments from OWL files loaded before the current annotation regime, or left by safety-pass residue.

#### Pass 17 — `_annotate_missing_comments(graph)` (line 973)

Re-annotates any property left without an `rdfs:comment` after the legacy strip, deriving a rule-based comment from `skos:altLabel` (or the local name) and the domain class local name: `"The {column} of the {class}."` (or `"The {column}."` when there's no domain).

#### Pass 18 — `_write_owl(graph, profiles)` (line 540)

Serialises the graph to RDF/XML at `ontology_filename(out_dir, project, "rigor", gen_model)` under `SEED_OUTPUT_PATH`'s parent.

- `project` comes from the `FORGEMESH_PROJECT` env var, or the bronze-stripped DB short name.
- `fm` is re-bound before serialise.
- Returns the `Path`.

### 8.4 PropertyNamer (`property_namer.py`) and enum consolidation — summary

- **`canonical_local(source, word_splits)`** is the *pure* function defining the `has{CamelCase}` convention (6 ordered rules); `_is_canonical` tests whether a local name already conforms. It is also imported into `_fill_missing_column_properties` to name gap-filled FIELDS.
- **`normalize_datatype_property_uris`** is the graph pass (see post-loop Pass 1). The deliberate asymmetry — DatatypeProperties renamed from source columns, ObjectProperties left as target-entity names — is the structural distinction between FIELD-bearing and FK/relationship-bearing predicates.
- **`consolidate_enumeration_classes`** (Pass 11) is the conservative same-concept VALUE-vocabulary merge.

### 8.5 Postconditions (`postconditions.py`): G1, G2, G3

**In plain terms:** three structural checks that encode column/PK/FK rules. Each returns a list of human-readable violation strings (empty = pass). Callers **log** them and the generator continues — though CI may treat non-empty results as a hard failure.

#### G1 — `check_no_scalar_as_object_property(graph, column_contexts)`

Run per-table after the retry loop.

- A column whose `inferred_type` is scalar must **never** be an `owl:ObjectProperty`. The scalar set (`_SCALAR_TYPES`): datetime/date/time/timestamp, the int family, float/decimal/numeric/real, boolean/bit.
- **FK columns are exempt** (`col.is_fk`) — a FK that stores as INT/BIGINT is a legitimate surrogate-key reference, so ObjectProperty is correct there.
- Matches by `skos:altLabel` / `rdfs:label` on `table.col`, then bare `col`.
- *Caught the MIMIC `Transfer.outtime` / `Transfer.los` misclassification.*

#### G2 — `check_split_column_coverage(graph, table_to_class_uri)`

Run after `_split_multi_domain_dt_properties`.

- Every `(table, column)` referenced by a `skos:altLabel` must be reachable via a property whose `rdfs:domain` includes that table's class — i.e. the split pass must not leave a table without a scoped property for a shared column.
- *Caught 11 cross-table predicates on `InputeventsCv` subjects.*

#### G3 — `check_no_cross_table_property_reuse(new_fragment, accumulating, new_class_uri)`

Run per-table before merge.

- A property the new fragment introduces must be either brand new or already scoped to `new_class_uri`. It may **not** reuse a property that already has a *different* class in its `rdfs:domain` in the accumulating graph.
- *Caught `IcdDiagnose` reusing `hasIcdProcedure…` title predicates from `IcdProcedure`, because the two structurally-identical tables collided.*

### 8.6 How each entity kind is handled at this stage

**In plain terms:** a quick cross-reference of what happens to each kind of schema element across the whole pipeline.

#### TABLES

- Skipped below `ONTOLOGY_MIN_ROWS_FOR_GENERATION`.
- FK-topo-ordered.
- Each becomes a class with a deterministic URI (PK-denotes-entity overrides applied first), stamped `fm:sourceTable`.
- High-row-count tables are excluded from `owl:oneOf` enumeration.

#### FIELDS / COLUMNS

- Non-PK / non-FK columns become DatatypeProperties.
- `_fill_missing_column_properties` guarantees completeness.
- `normalize_datatype_property_uris` canonicalises their URIs from the column name.
- `_deduplicate_properties` / `_split_multi_domain_dt_properties` reconcile cross-table same-column properties into one abstract parent plus per-class scoped sub-properties.
- Ranges are inferred from SQL `inferred_type`.

#### VALUES

- Low-cardinality columns are promoted to controlled-vocabulary classes whose distinct values become NamedIndividuals.
- `consolidate_enumeration_classes` merges duplicate vocabularies.
- `_build_oneof_enumerations` freezes them as `owl:oneOf`.
- MIXED_RANGE R3 decides vocab-class vs literal from value statistics.

#### PRIMARY KEYS

- Used to *name* the class (the override mechanism).
- Used to gate enumeration (high-row entity classes excluded).
- PK columns are **not** minted as properties by gap-fill.

#### FOREIGN KEYS

- Modelled as ObjectProperties named after the target entity.
- Abstracted via `_qualify_obj_props_safety` into shared `has{Target}` parents (surfaced for reuse via per-FK abstract retrieval).
- Annotated `fm:sourceForeignKey`.
- Stale FK-scalar abstracts removed by `_remove_superseded_fk_datatype_abstracts`.
- Fabricated FK relationships stripped by `_strip_dual_annotated_object_properties`.
- MIXED_RANGE R1 keys strictly on the inspector's real FK info, never the LLM's annotation.

---

## 9. Deterministic Coherence, Enrichment & Range Checks

**In plain terms:** After the AI has written the ontology (a formal, machine-readable description of the data's tables, columns, and relationships), this stage runs a set of plain-code checks and fixes on it — no AI, no reasoning engine, no network — to catch mistakes and fill in missing bookkeeping before anything downstream uses the file.

**Inputs → Outputs:** an OWL file produced by the iterative generator → the same ontology graph, loaded into memory, provenance-backfilled, enriched with named values, and checked for structural problems (with issues reported, and some auto-resolvable via an override file).

### What this stage does

After the RIGOR-style `IterativeOntologyGenerator` (and its judge/post-passes) has produced an OWL file — and before that ontology is consumed by the GOLD and RDF layers — a family of **deterministic, reasoner-free guardrails** operates directly on the `rdflib.Graph` (the in-memory representation of the ontology as a set of triples — see below).

They do four distinct jobs:

1. **Load** the ontology and backfill missing provenance (the record of which table/column each part came from).
2. **Enrich** it with named individuals derived from value mappings.
3. **Structurally check** it for incoherence.
4. **Classify range defects** on properties.

None of these stages calls an LLM, a Java DL reasoner (an external logic engine that infers facts from OWL axioms), or the network. They are pure graph traversals over `rdflib`, intended as the last line of defence against generation slips and against ontologies that predate the iterative generator. All of them live in `semantic_mapper/ontology/`.

#### Quick glossary for this section

- **triple** — one subject–predicate–object fact (the atomic unit of an RDF graph).
- **IRI/URI** — a globally-unique name for a thing.
- **`owl:Class`** — a category of entity (here, typically backed by a relational table).
- **DatatypeProperty** — an attribute whose value is a literal, e.g. a number or string (here, a scalar column).
- **ObjectProperty** — a link from one entity to another (here, typically a foreign key).
- **`rdfs:domain`** — the class a property is allowed to be used on.
- **`rdfs:range`** — the type of value a property points at — a class, or a literal type like `xsd:string`.
- **`owl:NamedIndividual`** — a concrete instance (here, an enumerated value).
- **`owl:oneOf`** — a closed, enumerated list of allowed values.
- **`owl:unionOf`** — an OR over classes.
- **`skos:altLabel`** — an alternative label (here, the original column name).

### The `fm:` provenance namespace

A running theme is the `fm:` provenance namespace, `http://forgemesh.ai/ontology#` (declared as `FM` in `coherence.py:36` and `annotation_sweep.py:47`). It carries annotations that record where each piece of the ontology came from in the source database:

| Annotation | Subject type | Meaning |
|---|---|---|
| `fm:sourceTable` | `owl:Class` | originating relational table |
| `fm:sourceColumn` | `owl:DatatypeProperty` | `table.column` of the scalar source |
| `fm:sqlType` | `owl:DatatypeProperty` | the column's SQL type |
| `fm:nullRate` | `owl:DatatypeProperty` | `0.00`–`1.00` null fraction |
| `fm:sourceForeignKey` | `owl:ObjectProperty` | `table.col -> target` FK provenance |

### 9.1 Loading and the provenance annotation sweep (`loader.py`, `annotation_sweep.py`)

**In plain terms:** Load the OWL file into memory, then walk over every class and property to make sure each one carries `fm:` provenance — synthesising it from a mapping report (or, failing that, from the URI's name) when it's missing.

#### Input/output

`OntologyLoader.load(path, mapping_report=None)` (`loader.py:18`):

- Parses an OWL/XML file into an `rdflib.Graph`.
- Runs the annotation sweep over it (mutating in place).
- Strips legacy comments.
- Returns the graph.

It raises `OntologyLoadError` if the file is missing or unparseable. This is the **single entry point used everywhere downstream**: `main.py`, `query.py`, `mcp_server.py`, `web_app.py`, and `semantic_mapper/rdf/pipeline.py` and `rdf/query/engine.py` all load ontologies through it.

#### The sweep: synthesising missing provenance

**`sweep_provenance_annotations(graph, mapping_report)`** (`annotation_sweep.py:67`) is a "safety net for ontologies that were not generated by the Phase 0b iterative generator" (module docstring).

For every `owl:Class`, `owl:DatatypeProperty`, and `owl:ObjectProperty` that lacks `fm:` provenance, it synthesises annotations from the optional `SemanticMappingReport` and falls back to URI local names. The synthesis priority differs per element type, all handled at this stage:

- **Classes (→ TABLES):** if the class URI is present in the report's `class_candidate_set` (mapped via `cc.uri → tm.source_table`), set `fm:sourceTable` to the source table; otherwise fall back to the URI's local name, else `"unknown"` (`annotation_sweep.py:106-119`). Only added when `fm:sourceTable` is absent.
- **DatatypeProperties (→ FIELDS/COLUMNS):** if the property is in the report's `column_mappings` (`mapped_property_uri → (table, col, dtype)`), set `fm:sourceColumn = "{table}.{col}"`, `fm:sqlType = dtype or "unknown"`, `fm:nullRate = "0.00"`. Otherwise it uses a `skos:altLabel` (an alternative label — here, the original column name) hint (`fm:sourceColumn = "unknown.{altLabel}"`) or the local name, with type/rate set to `"unknown"` (`annotation_sweep.py:121-151`). Each of the three annotations is added only if individually missing.
- **ObjectProperties (→ FOREIGN KEYS):** if recoverable from the report, set `fm:sourceForeignKey = "{table}.{col} -> {target_table}"`, resolving the target by matching the property's `rdfs:range` against the report's `class_table_map`.
  - If the property is in the report but the target table can't be resolved → value is `"{table}.{col} -> (target unknown)"`.
  - If nothing is recoverable → it writes the literal string `"not recoverable"`. **Only this degraded case is logged at WARNING** (`annotation_sweep.py:178-183`); the recoverable cases log at DEBUG.
  - Added only if `fm:sourceForeignKey` is absent.

#### Sweep result and reporting

The result is an `AnnotationSweepResult` (`annotation_sweep.py:52`) with per-type synthesis counts and an `unsynthesisable` field (which is **never incremented in current code** — it remains `0`).

`OntologyLoader.load` inspects these counts and, if any synthesis occurred, logs a WARNING that "ontology may predate Phase 0b" (`loader.py:44-52`).

#### Legacy comment removal

`remove_legacy_provenance_comments(graph)` (`annotation_sweep.py:195`) deletes every `rdfs:comment` whose value contains the substring `"Derived from"` — the marker of pre-Phase-0b provenance prose that has been superseded by `fm:` annotations.

`OntologyLoader` calls it after the sweep. The iterative generator also calls it (`iterative/generator.py:461`), and notably the generator's LLM-generated comments are added *after* this strip, so they are preserved.

### 9.2 Value-mapping enrichment (`enricher.py`)

**In plain terms:** When an enumerated column value (like an ethnicity code) became a URI but was never declared as an actual instance, this step declares it as a typed individual and slots it into its class's list of allowed values.

`enrich_ontology_from_value_mappings(sidecar, report, graph, ontology_path)` (`enricher.py:86`) closes the loop between the SILVER value-mapping stage and the ontology: it adds **`owl:NamedIndividual` declarations (concrete instances — here, the actual VALUES)** for enumerated values that became URIs but were never declared as instances.

Its inputs are:

- The value-mappings *sidecar* (`{"table.col": [{"mapped_uri": ...}, ...]}`, built in `main.py:520-529` from each `column_mappings[].value_mappings`).
- The `SemanticMappingReport`.

It is called from `main.py:534` after the sidecar is written.

#### Lookups and per-value handling

The function builds two lookups: `(table, col) → mapped_property_uri` and `property_uri → rdfs:range class URI` (object properties only, `enricher.py:108-120`). For each sidecar value URI:

- **Find the parent class.** The parent class is the `rdfs:range` (the type a property points at) of the column's mapped property. If a candidate value URI has no resolvable parent, it is **skipped with an ERROR log** rather than creating an untyped individual (`enricher.py:152-158`) — an explicit guard against dangling individuals.
- **Declare the individual.** A new individual gets `rdf:type owl:NamedIndividual`, `rdf:type <parent>`, and an `rdfs:label` derived from the URI fragment via `_to_label` (CamelCase → "Camel Case", `enricher.py:19-21`), and is queued to extend the parent's enumeration.

#### `owl:oneOf` maintenance

`_update_one_of` (`enricher.py:24`) keeps the parent class's closed list of allowed values (`owl:oneOf` — a closed, enumerated list of allowed values) up to date:

- It **appends** individuals to an existing `owl:equivalentClass [ owl:oneOf ( ... ) ]` RDF list (walking `rdf:rest` to the tail, re-pointing the old `rdf:nil`).
- Or it **creates** the `equivalentClass`/`oneOf` collection from scratch if none exists.
- On a **warm run** (URI already in the graph), the individual is not re-declared but is *ensured present* in the parent's enumeration via `_individual_in_oneof` (`enricher.py:72`) + append.

#### Output

The graph is re-serialised to `ontology_path` (XML for `.owl`/`.rdf`, Turtle otherwise) whenever individuals were added *or* a warm-run `oneOf` append occurred (`enricher.py:176-179`). It returns `(num_added, list_of_new_uris)`.

### 9.3 `domain_class_uris` and `owl:unionOf` expansion (`owl_utils.py`)

**In plain terms:** A shared property like `hasPatient` can apply to several different table-classes at once. OWL stores that as a hidden "OR over classes" node; this helper unpacks it back into a plain set of class names so the rest of the system reads the domain correctly.

`domain_class_uris(graph, prop)` (`owl_utils.py:11`) is a tiny but load-bearing helper that returns the **named class URIs in a property's `rdfs:domain` (the classes a property may be used on), expanding any `owl:unionOf` (an OR over classes) restriction**.

#### Why the union matters

The iterative generator gives a shared abstract property (e.g. `hasPatient`) a single `rdfs:domain [ owl:unionOf ( ClassA ClassB ... ) ]`. This is deliberate: it stops OWL from *intersecting* (AND-ing) the member classes, which would make the property unsatisfiable across disjoint table classes.

The catch: a naïve `next(graph.objects(prop, RDFS.domain))` would just return the blank node wrapping the union, not the classes. So consumers **must** use this helper.

#### How it expands

- For a `URIRef` domain → it adds the URI directly.
- For a `BNode` (blank node) → it reads `owl:unionOf` and walks the RDF list via `rdflib.collection.Collection` (`owl_utils.py:28-35`).
- An **empty set means no declared domain** — by convention "unrestricted, applies to every class."

#### Who relies on it

That "empty = applies to all" convention is honoured by its consumers in the proof layer:

- `rdf/proof/scope.py:73` (`_properties_on_class`) — collects properties whose domain (union-expanded) intersects a queried class or its direct superclasses, treating empty-domain as "applies to all" (`scope.py:74`). Drives `build_scope_assumptions`.
- `rdf/proof/limitations.py:49` (`_object_properties_on_class`) — same union-aware membership test for cardinality/limitation reporting.
- It is also used inside the generator's own `graph_merger.py` (`:459`, `:1089`) during abstract-parent domain accumulation.

### 9.4 Structural coherence checks (`coherence.py`)

**In plain terms:** Run seven cheap structural sanity checks over the ontology — without a heavyweight reasoner — and report any problems, from a property with no domain to a column that's claimed as both a plain field and a foreign key.

`OntologyCoherenceChecker(graph).check()` (`coherence.py:86`) runs seven lightweight structural checks "without requiring a Java DL reasoner" (docstring) and returns a `CoherenceReport` (`coherence.py:49`) of `CoherenceIssue`s.

Each `CoherenceIssue` has:

- a `severity` (`"error"`/`"warning"`),
- a `category`,
- a message, and
- a `subject_uri`.

The report exposes `error_count`, `warning_count`, and `is_coherent` (true iff zero errors), plus `to_dict()` for serialisation. Classes are gathered excluding `owl:Thing`/`owl:Nothing` (`coherence.py:114-119`); properties are the union of datatype and object properties.

#### The seven checks

| Category | Severity | What it catches |
|---|---|---|
| `MISSING_DOMAIN` | warning | property (data or object) with no `rdfs:domain` |
| `MISSING_RANGE` | warning | property with no `rdfs:range` |
| `NAMESPACE_COLLISION` | warning | one local name shared by ≥2 class/property URIs |
| `ORPHAN_CLASS` | warning | named class never used as any property's `rdfs:domain` (→ a table-class with no attached fields) |
| `INVERSE_ASYMMETRY` | warning | `P owl:inverseOf Q` but `Q` declares no `owl:inverseOf` back |
| `SOURCE_COLUMN_COLLISION` | **error** | a `(table, col)` annotated as **both** a scalar source and an FK source |
| range checks (§9.5) | error/warning | `MIXED_RANGE` / `WRONG_RANGE_TYPE` / `MULTI_RANGE` / `DANGLING_RANGE` |

#### The standout: `SOURCE_COLUMN_COLLISION`

`SOURCE_COLUMN_COLLISION` (`coherence.py:218`) directly addresses the PRIMARY/FOREIGN-KEY-vs-VALUE ambiguity — i.e. is a given column a literal value, or a link to another table?

It works by collecting two sets of `(table, col)` keys:

- `fm:sourceColumn` annotations on DatatypeProperties → `(table, col)` keys (the literal-field claim).
- `fm:sourceForeignKey` annotations on ObjectProperties → parsing the left side of `"table.col -> target"` into the same keyspace (the foreign-key claim).

The set intersection yields columns claimed simultaneously as a **literal field** and a **foreign key**. Because "the pipeline cannot determine whether the column is a literal or a foreign key," this is surfaced as an `error` (`coherence.py:250-262`). It is the column-level sibling of the property-level `MIXED_RANGE`.

#### Resolution overrides (human-in-the-loop)

A JSON sidecar lets a human resolve collisions:

- `load_coherence_overrides(path)` (`coherence.py:359`) returns `[]` on a missing or malformed file.
- `apply_coherence_overrides(graph, overrides)` (`coherence.py:369`) takes entries `{"table", "column", "keep": "fk"|"literal"}` and strips the *losing* annotation:
  - `keep="fk"` removes `fm:sourceColumn` from the matching DatatypeProperty.
  - `keep="literal"` removes `fm:sourceForeignKey` from the ObjectProperty whose value starts with `"table.column ->"`.
  - It returns the count of triples removed.

#### Where it runs

`main.py:707-730` (the GOLD-build entry):

1. Loads overrides.
2. Applies them.
3. Runs `check()`.
4. For each `SOURCE_COLUMN_COLLISION`, logs a warning and calls `_patch_coherence_violations_in_report(...)` to flag the affected mapping `PENDING_REVIEW` (so it is **not** silently resolved by strategy ordering) before reloading the report.

`web_app.py` exposes the same checker, override loading, and an override-writing endpoint (`web_app.py:3806-3928`).

### 9.5 Range coherence: the four-way classification

**In plain terms:** A property's *range* (the kind of value it points at) can be wrong in four distinct ways. This step sorts each property's declared ranges into three buckets — class references, literal types, and "names that don't resolve" — and emits a labelled defect for each kind of problem.

#### Two implementations

The same four range rules exist in two implementations:

- **Embedded** — `OntologyCoherenceChecker._check_range_coherence` (`coherence.py:264`), run as part of `check()` over **ObjectProperties only**.
- **Standalone** — `check_range_coherence(graph, ontology_namespace)` (`range_coherence_check.py:57`), which returns a list of frozen `RangeDefect(rule, property_uri, detail)` records (`range_coherence_check.py:43`). It is used by `summarise()` and the diagnostic CLI/tests, and examines **all properties** (object *and* datatype).

The two differ in how they decide class-vs-literal membership:

- **Standalone** decides by *namespace*: ranges starting with `ontology_namespace` are class references; ranges in the XSD namespace are literals (`range_coherence_check.py:90-91`).
- **Embedded** instead tests membership against the actually-declared `owl:Class` set (`coherence.py:303`). As a result, a class-namespace URI that was never declared lands in `dangling`.

Both treat `owl:Thing` as **always a valid range** and exclude it from all defect logic (`range_coherence_check.py:85-88`, `coherence.py:301-302`).

#### The four rules

After bucketing each property's ranges into `class_ranges`, `xsd_ranges`, and `dangling`, the rules are (definitions from the `range_coherence_check.py` module docstring):

- **`MIXED_RANGE`** (error): the property declares **both** a class range and an XSD literal range. Multiple `rdfs:range` is *conjunction* (AND) in OWL, so the value would have to be simultaneously a class instance and a literal — unsatisfiable. This is the ObjectProperty-level sibling of `SOURCE_COLUMN_COLLISION`.
- **`WRONG_RANGE_TYPE`** (error): a property typed `owl:ObjectProperty` whose **only** range(s) are XSD literals, with no class side. Nothing is "mixed," but an ObjectProperty must range over a class — this is "the XSD-only case that would otherwise fall through every other bucket silently."
- **`MULTI_RANGE`** (warning): more than one range of the *same kind*.
  - Standalone version triggers on `len(class_ranges) > 1` *or* `len(xsd_ranges) > 1` (`range_coherence_check.py:121`).
  - Embedded version restricts it to `len(class_ranges) > 1 and not xsd_ranges and not dangling` (`coherence.py:332`).
  - "Usually a generation slip, not intent."
- **`DANGLING_RANGE`** (error): a class-namespace range pointing at a class that is **never declared** in the ontology — the original genuine dangling case (a name-derivation mismatch).

#### Priority logic

- `MIXED_RANGE`, `WRONG_RANGE_TYPE`, and `MULTI_RANGE` are **mutually exclusive** via an `if/elif` chain (`range_coherence_check.py:102-128`; `coherence.py:312-341`) — **at most one of the three fires** per property, in that priority order.
- `DANGLING_RANGE` is **additive**: it is emitted in a separate loop, once per undeclared class target, and can co-occur with any of the other three. (E.g. a property that is both `MIXED_RANGE` and points at an undeclared class produces two issues.)

The standalone CLI (`range_coherence_check.py:152-169`) prints a `summarise()` count keyed by rule and one line per defect.

### 9.6 Summary of the deterministic guardrails

These passes form the bridge between the generated ontology and its consumers. Everything here is deterministic and reasoner-free, by design.

- **`OntologyLoader` + the annotation sweep** guarantee that **every class/property carries `fm:` provenance** (synthesising from the report or local names, never crashing) — so the GOLD builder and RDF materialiser can always recover table/column/FK origins.
- **The enricher** guarantees that **enumerated VALUES become typed `owl:NamedIndividual`s inside `owl:oneOf`**, refusing to create untyped ones.
- **`domain_class_uris`** guarantees that **union-domain abstract properties are read as a set of class members**, not a blank node — keeping the proof/scope/limitation layers correct.
- **The coherence checker** catches **missing domains/ranges, name collisions, orphan/table classes, asymmetric inverses, FK-vs-literal column collisions**, and the four range defects — surfacing FK/literal ambiguity as a hard error that flags mappings `PENDING_REVIEW` and can be resolved by an override sidecar.

---

## 10. Semantic Mapping — Silver Columns to Ontology Properties

**In plain terms:** This stage decides, for every column in the relational database, *which* ontology property it corresponds to — and for columns holding categorical codes (like a religion or gender code), which OWL class or named value each distinct cell value means. It outputs a frozen report that the Silver layer then turns into typed rows carrying ontology URIs.

**Inputs → Outputs:** typed/profiled relational schema (`TableProfile`s) + the generated OWL ontology (an `rdflib.Graph`) → a `SemanticMappingReport` describing the column-to-property mapping, with confidence, rationale, value transforms, and per-value class mappings.

### Where this stage sits

This stage sits between the SILVER profiling layer and the GOLD ontology-mapped views.

It takes two things and combines them:

- the typed/profiled relational schema — a list of `TableProfile` objects, each holding `ColumnProfile`s
- the generated OWL ontology (the formal, machine-readable model of classes and properties) as an `rdflib.Graph`

It produces a `SemanticMappingReport` (`semantic_mapper/schema/mapping_output.py`) that says, for every column:

- *which* ontology property it maps to
- with what confidence
- why (the rationale)
- how its values transform
- and — for categorical columns — how each distinct value maps to an OWL class or NamedIndividual URI (a NamedIndividual is a specific named value, e.g. "Catholic"; a URI/IRI is a globally-unique name for a thing)

The same report is then consumed verbatim by the Silver materialisation pipeline (`semantic_mapper/silver/pipeline.py`), which emits typed `SilverRow`s carrying the resolved class/property URIs forward.

#### Orchestrator and one important non-feature

The orchestrator of record is `MapperOrchestrator.run()` in `semantic_mapper/engine/mapper.py`.

Note one thing that is **not** implemented at this stage: `semantic_mapper/engine/reasoner.py`'s `SemanticReasoner.infer_derived_classes()` is an explicit TODO stub — it just `pass`es. Derived-class inference such as `ActiveCustomer`/`LapsedCustomer` does not happen here.

### 10.1 Inputs and outputs

#### Inputs

- **`profiles: List[TableProfile]`** — from the inspector/profiler. `TableProfile` (`semantic_mapper/db/models.py`) carries:
  - `table_name`
  - `row_count`
  - `columns: Dict[str, ColumnProfile]`
  - `foreign_keys: List[Dict]` (each FK dict carries a `"column"` key)

  Each `ColumnProfile` carries:
  - `name`
  - `inferred_type` (raw SQL type)
  - `is_nullable`
  - `is_primary_key`
  - `null_rate`
  - `distinct_count`
  - `top_values` (low-cardinality value/count dicts)
  - `detected_formats` (e.g. `["YYYY-MM-DD"]`)
  - `sample_data`

- **`graph: rdflib.Graph`** — the OWL ontology produced upstream.

- **`settings`**, plus `ontology_source`/`db_source` strings used to key the corrections store.

#### Output

A `SemanticMappingReport` with `tables: List[TableMapping]`.

Each `TableMapping` holds:

- `class_candidate_set` — the candidate OWL classes for the table, score > 0.5
- `column_mappings: List[PropertyMapping]`
- `primary_key_columns` — carried forward from the inspector

Each `PropertyMapping` records:

| Field | Meaning |
|-------|---------|
| `source_column` | the column being mapped |
| `source_data_type` | its SQL type |
| `is_nullable` | whether NULLs are allowed |
| `mapped_property_uri` | the chosen ontology property |
| `confidence_score` | aggregated confidence |
| `status` | `CONFIRMED` or `PENDING_REVIEW` |
| `rationale` | human-readable reason |
| `evidence_trail` | the per-strategy `MappingEvidence` list |
| `transformation_spec` | optional value-transform metadata |
| `value_mappings: List[ValueMappingRule]` | per-distinct-value class mappings |

The report also computes a `CoverageReport` via `compute_coverage()` and a `total_pending_review` count.

### 10.2 TABLES — class selection

**In plain terms:** Before mapping any column, the engine decides which OWL class a whole table represents (e.g. the `patients` table → the `Patient` class). That choice anchors all the column decisions that follow.

Before any column is mapped, `run()` computes a **table class URI** (`mapper.py` ~lines 280–289):

- It calls `evaluate_table()` on every Tier 1 and Tier 2 strategy.
- It keeps the highest-scoring `MappingEvidence` whose `score > 0.5` as `table_class_uri`.

In practice only `NameMatcher.evaluate_table()` produces real evidence — it fuzzy-matches the (raw-prefix-stripped) table name against every `owl:Class` (a category/type in the ontology) local name. `SemanticMatcher.evaluate_table()` and the others return `[]` (TODOs / not applicable).

This `table_class_uri` is the linchpin for domain-aware disambiguation downstream.

All candidates with score > 0.5 are also exported into `TableMapping.class_candidate_set` as `ClassCandidate`s; the Silver pipeline later picks the max-confidence one as the table's primary class.

### 10.3 PRIMARY KEYS — skipped as properties, preserved as the row key

**In plain terms:** A surrogate primary key (like an auto-increment `id`) is just a row identifier, not a real-world fact, so it gets no ontology property. But it is kept aside as the key used to identify and trace each row.

Primary keys are deliberately **not** mapped to any ontology property. Every column loop in `mapper.py` filters `if col_profile.is_primary_key: continue`:

- lines 293–297 for the candidate set
- lines 352–353 when building `PropertyMapping`s

The rationale in the code: *"Surrogate PKs skipped — no ontology property for row identity."*

Instead the PK columns are carried forward untouched into `TableMapping.primary_key_columns` (lines 464–467). That list is the canonical, name-agnostic, composite-aware row key the Silver pipeline uses to build `bronze_source_ref` provenance and (downstream) the RDF subject URI (the subject of each generated triple).

See `SilverPipeline._build_bronze_source_ref`: it uses the declared/inferred PK (`{table}.{key}={val}`) in preference to any column literally named `id`, precisely to avoid collapsing rows on a non-unique business code.

### 10.4 FIELDS/COLUMNS — the tiered candidate-selection engine

**In plain terms:** For each non-PK column, the engine gathers "evidence" from several matching strategies, cheapest first, and stops early once it is confident. The cheap string/graph matchers run on every column; only the still-uncertain columns reach the embedding model, and only the columns that *still* lack a confident answer reach the LLM.

For each non-PK column, `MapperOrchestrator` builds an `evidence_trail` (a list of `MappingEvidence`, one or many per strategy) and then aggregates it into a single winning URI + score.

The engine is a **three-tier cascade with early exit** (class docstring, `mapper.py` lines 57–81), so expensive strategies only run on columns the cheap ones couldn't confirm. A column is "confirmed" when its aggregated score reaches `AUTO_CONFIRM_THRESHOLD` (default **0.70**).

| Tier | Strategies | Cost | Runs on |
|------|-----------|------|---------|
| 1 | `HistoryMatcher`, `AnnotationMatcher`, `NameMatcher`, `TypeMatcher` | cheap, local (string/graph lookups) | every non-PK column |
| 2 | `SemanticMatcher` | local embedding model (`all-MiniLM-L6-v2`) | only columns still PENDING after Tier 1 |
| 3 | `LLMStrategy` | remote OpenAI call (batched per table) | only columns still PENDING after Tiers 1+2 |

#### How the tiers chain together

- After Tier 1, `run()` calls `aggregate(...)` on each column's trail and forms `pending_after_t1` = columns scoring `< AUTO_CONFIRM_THRESHOLD` (lines 306–311).
- Tier 2's `SemanticMatcher` evidence is appended only for those pending columns (317–323).
- The survivors after re-aggregation feed Tier 3: pending columns are bundled with their current best URI/score and sent to `LLMStrategy.evaluate_pending_batch()` in a *single* API call per table (326–347).

#### The strategies, concretely

##### `HistoryMatcher` (`history_matcher.py`)

Consults the corrections store via `PatternLookup.match(name, type, human_only=True)`.

- Emits evidence **only for human-confirmed** mappings.
- AUTO-confirmed pipeline output is intentionally ignored, to prevent circular confidence inflation.
- Any hit becomes a hard override.

##### `AnnotationMatcher` (`annotation_matcher.py`)

This is the **`skos:altLabel` exact-matcher** (`skos:altLabel` is an alternative label — here, the original source column name stored on the property).

At init it caches, per property, two buckets:

- **precise** texts — `skos:altLabel` values + the source-column name extracted from `fm:sourceForeignKey` annotations of shape `"table.col -> ClassName"`
- **fuzzy** texts — `rdfs:label` (the human-readable name) and `rdfs:comment` (a free-text description)

Scoring tiers:

| Match | Score |
|-------|-------|
| explicit pattern such as `"maps to X in source"` / `"source column: X"` | **1.0** |
| verbatim word-boundary occurrence of the column name in the precise bucket | **1.0** |
| case-insensitive word-boundary hit | **`ANNOTATION_NORMALISED_MATCH_SCORE`** (default **0.95**) |
| fuzzy token-set match (rapidfuzz, min ratio 0.60) on any text | **≤0.75** |

Word-boundary matching is restricted to the precise bucket, so free-form comment prose (e.g. "DST type") cannot produce a false 1.0 for a short column like `type`.

##### `NameMatcher` (`name_matcher.py`)

Base score is `fuzz.token_set_ratio(clean_column, property_local_name)/100`, with the property's `has`/`is`/`of` verb prefix stripped first.

It is **FK-aware** (FK = foreign key, a column pointing at another table's row). For FK-like columns (name ends `_id`, or listed in `table_profile.foreign_keys`) it applies a *domain boost*:

- If the FK base name (`icustay_id` → `icustay`) appears in the property local name **and** the property's `rdfs:domain` (the class a property is allowed to be used on) overlaps the table class (prefix match, plural/singular tolerant)...
- ...it strips any `For<TableClass>` qualifier suffix (`hasICUStayForChartEvent` → `hasICUStay`), re-scores, and adds `DOMAIN_MATCH_BOOST` (default **0.15**).

This pushes the table-specific subproperty above the generic base.

##### `TypeMatcher` (`type_matcher.py`)

The **structural type gate**. It maps the column's SQL type to XSD (the XML Schema datatypes used as literal value types, e.g. `xsd:string`, `xsd:date`) via `SQL_TO_XSD`, upgrading a VARCHAR with profiler-detected date formats to `xsd:date` (a "data-proven date").

It then scores DatatypeProperties (attributes whose value is a literal, e.g. a number or string) against `rdfs:range` (the type of value a property points at):

| Situation | Score |
|-----------|-------|
| data-proven date + `xsd:date` | 0.92 |
| native SQL date + `xsd:date` | 0.85 |
| exact XSD match | 0.80 |
| compatible numeric family | 0.70 |
| string↔string | 0.60 (weak) |
| mismatch | 0.05 |
| no `rdfs:range` | 0.15 |

**The FK/scalar inversion is the structural heart of the engine.** An ObjectProperty is a link from one entity to another (vs a DatatypeProperty, which holds a literal value):

- For a **non-FK scalar** column, every ObjectProperty scores **0.0** (categorically incompatible) — unless the column is categorical, in which case 0.10 via NamedIndividual.
- For an **FK** column the scoring inverts — DatatypeProperties score **0.0** and ObjectProperties score **0.80**.

These 0.0 scores are not "low confidence"; they are eliminated outright by the aggregator's type gate.

##### `SemanticMatcher` (`semantic_matcher.py`)

Tier 2 **embedding similarity**. Loads `all-MiniLM-L6-v2` locally and pre-encodes every property's text (`rdfs:label` if present, else camelCase-expanded local name with `has`/`is` stripped).

For a pending column it encodes the column name (underscores → spaces, `raw_` prefix stripped) and emits cosine similarity (0–1) against every property.

##### `LLMStrategy` (`llm_strategy.py`)

Tier 3, the only **LLM reasoning step** for property mapping. Default model `gpt-4o-mini` (`OPENAI_MODEL`).

It batches all pending columns of a table into one prompt containing:

- the full ontology property catalogue (URI, type, label, range, domain)
- each column's stats + up to 10 samples + sibling column names
- crucially, the **current best Tier 1+2 candidate, marked "STRONG PRIOR, only override at 0.85+"**

The system prompt instructs it to prefer ObjectProperties (with matching domain) for FK columns and DatatypeProperties for scalars, and the prompt pre-computes and surfaces domain-matched ObjectProperty candidates for each FK.

Guardrails on the response:

- Every returned URI is checked against the loaded graph.
- **Hallucinated URIs (not in the ontology) are rejected** and counted in `hallucinated_uris`.
- Telemetry (`api_calls`, `prompt_tokens`, `completion_tokens`) is accumulated on the instance and copied onto the orchestrator after `run()`.

#### Aggregation — `aggregate()` in `aggregator.py`

**In plain terms:** Each column has collected evidence from several strategies. Aggregation collapses that pile of evidence into one winning URI plus a single score, applying short-circuits (some signals win outright), a type gate (drop structurally-impossible matches), and a weighted average for everything else.

`aggregate(evidence_trail, settings, graph, table_class_uri)` collapses the trail to `(best_uri, combined_score, rationale)`. With the default `AGGREGATION_STRATEGY = "weighted_sum"` the order is:

1. **HistoryMatcher short-circuit** — any HistoryMatcher evidence (human override) wins immediately at score **1.0**.

2. **AnnotationMatcher exact short-circuit** — the highest-scoring AnnotationMatcher candidate at/above `ANNOTATION_EXACT_CONFIRM_THRESHOLD` (default **0.95**, via `getattr`) wins — *unless* its `rdfs:domain` mismatches `table_class_uri`, in which case it is skipped and the next candidate tried. This prevents e.g. a shared `hadm_id` altLabel on a property scoped to a different class from claiming the FK in this table.

3. **NameMatcher exact short-circuit** — top NameMatcher score ≥ `NAME_EXACT_CONFIRM_THRESHOLD` (default **0.97**) wins, with the same domain-mismatch suppression.

4. **Type gate** — drop every URI whose `TypeMatcher` score is exactly `0.0` (this is where the FK↔scalar inversion is enforced).

5. **Domain-preference filter** — when `table_class_uri` is known and at least one surviving candidate has matching `rdfs:domain`, drop the rest (no-domain abstract props and wrong-domain cross-table props).

6. **Weighted average with a floor denominator.** Per-strategy weights:

   | Strategy | Weight |
   |----------|--------|
   | `NameMatcher` | 0.35 |
   | `TypeMatcher` | 0.25 |
   | `SemanticMatcher` | 0.45 |
   | `AnnotationMatcher` | 0.20 |
   | `LLMStrategy` | **2.0** |

   - `floor = Σ weights of all non-LLM strategies that ran`
   - `denominator(uri) = max(Σ weights of strategies that scored this uri, floor)`

   The floor stops a lone TypeMatcher ~0.80 from clearing 0.70 on type alone (so Tiers 2/3 still get to run), while the deliberately large LLM weight (2.0 > the ~0.80 Tier-1 floor) lets a confident LLM-only answer reach its natural score.

#### Abstract-property resolution

**In plain terms:** Once a winner is chosen, the engine checks whether it is actually scoped to *this* table's class. If the property is too generic, or belongs to a different class, it re-targets to the most class-appropriate property — this is where an FK gets upgraded from a scalar attribute to a real entity-to-entity link.

After aggregation picks `best_uri`, `MapperOrchestrator._resolve_to_specific_property(uri, table_class_uri, col_name)` (lines 141–266) re-targets the winner to the most domain-appropriate class-scoped property. Three modes:

- **Domain match** — the property is already scoped to this class → keep it.

- **Domain mismatch** — the property belongs to another class → search for an ObjectProperty whose `range` is the mismatched class and whose concept overlaps the column name. This is exactly the FK upgrade `hasSubjectId` [domain=Patient, Datatype] → `hasAdmissionSubject` [domain=Admission, range=Patient, Object].

- **No domain (abstract)** — walk `rdfs:subPropertyOf` (the "is a more specific kind of" link between properties) for a subproperty whose domain matches, with a compound-name containment fallback.

When a substitution occurs the rationale is annotated. Status is then `CONFIRMED` iff `combined_score >= AUTO_CONFIRM_THRESHOLD`, else `PENDING_REVIEW`.

### 10.5 FOREIGN KEYS — recognised, and mapped to ObjectProperties

**In plain terms:** A foreign-key column should become a *link* between two entity classes, not a plain attribute. Several strategies cooperate to make that happen reliably.

FK recognition is consistent across strategies: a column is FK-like if its name ends `_id` **or** it appears in `table_profile.foreign_keys` (matched on the `"column"` key).

FK handling is layered:

- `TypeMatcher` zeroes DatatypeProperties and lifts ObjectProperties to 0.80 for FK columns.
- `NameMatcher` applies the `For<Class>` strip + domain boost.
- `AnnotationMatcher` credits the FK column against the right ObjectProperty via the `fm:sourceForeignKey` annotation.
- The `LLMStrategy` prompt explicitly flags FK columns and surfaces domain-matched ObjectProperty candidates.
- `_resolve_to_specific_property` performs the final Datatype→Object cross-type upgrade.

The net effect is that an FK column reliably resolves to the table-scoped `ObjectProperty` linking the two entity classes, rather than a scalar `DatatypeProperty`.

### 10.6 VALUES — categorical value-mapping to NamedIndividuals / classes

**In plain terms:** For confirmed low-cardinality columns (e.g. a religion or ethnicity code), the engine maps each distinct cell value to an ontology class or named value — reusing existing classes where possible and minting consistent new URIs otherwise. Results are cached and made consistent across tables.

Per-column value mapping runs only when `LLMStrategy` is enabled (lines 401–449).

The candidate set is columns that are **CONFIRMED**, non-PK, and have `top_values` (low cardinality). For each:

- The orchestrator first checks the corrections store (`load_value_mappings`) for cached rules.
- Only uncached columns are sent to `LLMStrategy.generate_value_mappings()`.
- Freshly generated rules are persisted (`write_value_mappings`) so future runs skip the LLM.

#### Inside `generate_value_mappings()`

Pre-processing (`_preprocess_top_values`):

- drops null-like tokens (`""`, `null`, `n/a`, `unknown`, `0`, …)
- title-case-dedupes
- **numeric-only columns are skipped** (`_is_numeric_only_column`) and flagged for human review rather than auto-mapped

Mapping:

- The LLM is given the `ONTOLOGY CLASSES` block and asked to map each distinct value to a class URI.
- It prefers an existing class; otherwise it **derives a per-value URI** by combining a PascalCase form of the value with the parent class name in the ontology namespace — e.g. `CATHOLIC` under `Religion` → `…/CatholicReligion`.
- Each rule must get a **distinct** URI.

Validation:

- Returned URIs are validated against `_valid_class_uris` or the ontology namespace (out-of-namespace → rejected as hallucination).
- If all of a column's rules collapse to a single URI, the rules are dropped (no discrimination possible — values pass through as string literals).

Results become `ValueMappingRule`s (`source_value`, `mapped_uri`, `rationale`, `confidence_score`) attached to the `PropertyMapping.value_mappings`.

#### Cross-table canonicalisation

Finally, `canonicalise_value_mappings()` (lines 25–54) reconciles each rule against a **cross-table canonical registry** keyed by `(normalised source column, normalised value)` with **first-declaration-wins** semantics:

- The first table to map a value fixes its URI.
- Every later table reusing that value is rewritten to the same URI.

This is the categorical analogue of deterministic class URIs — it eliminates per-table fragmentation (`MaleGender` vs `MedallistGenderMale`) and same-code/different-URI splits.

### 10.7 Silver transforms applied to fields/values, and the hand-off to GOLD

**In plain terms:** Two kinds of transform metadata are attached here (date-parsing rules and "this column is object-valued"), and the Silver pipeline then materialises the actual rows — normalising values, quarantining bad ones, and tagging each row with its ontology class and property URIs ready for GOLD/RDF.

#### Transform metadata produced at this stage

1. When a column has `detected_formats`, the `PropertyMapping` gets a `TransformationSpec`:
   - `target_format="xsd:date"`
   - `parse_strategy="dateutil.parser"`
   - validation rule "must be parseable as a date"
2. `value_mappings` mark a column as object-valued (categorical).

#### The Silver materialisation pipeline (`silver/pipeline.py`)

It consumes the *frozen* report. For each table it:

- keeps only `CONFIRMED` mappings
- picks the max-confidence class candidate as `primary_class`
- streams bronze rows in batches (`SILVER_BATCH_SIZE`=500)

For each (row, mapping) it:

- sets `property_kind = OBJECT` when `value_mappings` exist, else `DATATYPE` (the DV routing signal: link table vs satellite column)
- runs the value through the transformer chain `[DateNormaliser, NullHandler]`:
  - `DateNormaliser` parses dates to ISO-8601 (ISO fast-path → dateutil → strptime over `detected_formats`; unparseable → `QUARANTINED` into a `QuarantineRecord`)
  - `NullHandler` preserves NULLs (`explicit_null` vs `unexpected_null`) or passes through unchanged
- emits a `SilverRow` carrying:
  - `ontology_class_uri`
  - `property_uri`
  - snake_cased `property_local_name`
  - `property_kind`
  - the transformed `value`
  - a `RowAudit` (with `bronze_source_ref` built from the PK columns, `run_id`, timestamp, and the mapping's confidence score)

#### Discriminated-subtype expansion

This is the one place value-mappings change a row's *class* rather than just its value.

`_is_subtype_discriminator()` treats a value-mapped column as a subtype discriminator **only when every target is an `owl:Class` that is `rdfs:subClassOf` the table's primary class** (`rdfs:subClassOf` = the "is a kind of" relationship between classes; the closure is computed in `_index_ontology`).

Consequences:

- A flat ontology yields no discriminators.
- Value-mappings whose targets are vocabulary NamedIndividuals (e.g. `gender → MaleGender`) are *not* subtypes — the row keeps its table class and the column stays a plain (object) property.
- When a true discriminator is found, `_resolve_expansion_targets()` emits one `SilverRow` set per matched subclass (tracked by `expansion_index`/`discriminator_value`).

#### Hand-off to GOLD

These `SilverRow`s, each already tagged with the resolved ontology class and property URIs, are what the GOLD layer turns into ontology-mapped relational views and ultimately into materialised RDF triples (one subject–predicate–object fact each).

---

## 11. The Gold Layer — Ontology-Mapped Relational Views

**In plain terms:** the Gold layer is a set of clean relational tables that record every mapping decision the pipeline made — which database table became which ontology class, which column became which property, which raw value became which named thing — so that the next stage can turn each row into RDF (the subject–predicate–object data model of the Semantic Web). Gold itself stays in SQL; it does not emit any RDF yet.

**Inputs → Outputs:** a `SemanticMappingReport` (the JSON record of mapping decisions) + an optional OWL ontology file (the machine-readable class/property definitions) → a list of `GoldTableDefinition` objects → physical `gold_<class>` tables and `gold_semantic_<class>` views in a database.

### What the Gold layer is for

The Gold layer is the hinge between the relational world and RDF (the graph data model where every fact is a triple — one subject–predicate–object statement). It takes the *decisions* made earlier in the pipeline and reifies them as concrete, queryable relational tables (`gold_*`) plus aliased views (`gold_semantic_*`).

Those decisions are:

- which ontology class (a category of thing, e.g. "Patient") each source table maps to,
- which ontology property each column maps to,
- which raw values map to which URIs (a URI/IRI is a globally-unique name for a thing).

Crucially, Gold does **not** itself emit triples. It produces a stable, deduplicated, identity-resolved relational projection that the downstream `TripleBuilder` (Section 12) reads row-by-row to materialise RDF.

This section documents the files that define the layer:

- `semantic_mapper/gold/builder.py` — the definition builder.
- `semantic_mapper/gold/pipeline.py` — the row-materialisation pipeline.
- `semantic_mapper/gold/models.py` — the data models.
- `semantic_mapper/gold/writer/mysql.py` — the MySQL writer that performs the DDL/DML (table creation and row inserts).

### 11.1 Inputs and outputs

#### The main input: the SemanticMappingReport

The Gold layer consumes a `SemanticMappingReport` (`semantic_mapper/schema/mapping_output.py`), the JSON artifact produced by the ontology-mapping phase. The relevant fields are:

- `report.tables: List[TableMapping]`. Each entry carries:
  - `source_table`,
  - a `class_candidate_set: List[ClassCandidate]` (each candidate has a `uri` + a `confidence_score`),
  - `column_mappings: List[PropertyMapping]`,
  - `primary_key_columns: List[str]` — the inspector-detected primary key ("declared PK preferred, else inferred", per the field doc on `TableMapping`).
- Each `PropertyMapping` carries `source_column`, `source_data_type`, `mapped_property_uri`, `status: MappingStatus`, `confidence_score`, and `value_mappings: List[ValueMappingRule]` (each a `source_value → mapped_uri` pair).

Note on naming: the report's class is named `PropertyMapping`, but the builder refers to these entries via the `column_mappings` field.

#### Two optional inputs

- The **OWL ontology graph** (`rdflib.Graph`), loaded from `settings.ONTOLOGY_PATH`. Used only for foreign-key property resolution (Section 11.5).
- `value_map_overrides` — a sidecar JSON of human-curated value mappings, keyed by `"{source_table}.{source_column}"`.

#### The output: GoldTableDefinitions and physical artifacts

`GoldDefinitionBuilder.build()` returns `List[GoldTableDefinition]`. These definitions are the contract handed to:

- the `GoldPipeline`, which fills the relational tables from Silver, and
- the RDF stage — after `refine_id_columns()` has run.

The relational artifacts written to the Gold database are the `gold_<class>` tables and `gold_semantic_<class>` views.

#### How the stage is orchestrated

Orchestration lives in `main.py` (~lines 740–770 and 812–845):

1. A single `GoldDefinitionBuilder().build(report, ontology_graph=…, gold_mode=settings.GOLD_MODE, value_map_overrides=…)` call produces the definitions.
2. Collisions are patched back into the report (Section 11.6).
3. A `GoldPipeline` runs the materialisation.

The RDF path (`run_rdf`) re-builds the definitions and additionally calls `refine_id_columns(definitions, gold_engine, pk_source_tables)` against the *already-populated* Gold DB before constructing the `TripleBuilder`.

**On `gold_mode`:** `gold_mode` / `settings.GOLD_MODE` is threaded through `build()`'s signature and through the writer's `initialise()` (`getattr(self._settings, "GOLD_MODE", 1)`). However, `build()` does not currently branch on it — it is a reserved parameter, not active behaviour.

### 11.2 The data models (`gold/models.py`)

Two frozen Pydantic models define a gold table.

#### `GoldColumnSpec` — one per materialised column

| field | meaning |
|---|---|
| `silver_table` | source silver table, e.g. `silver_raw_party` |
| `source_column` | bronze column name *before* safe-name transform (retains `_id` suffixes) |
| `gold_column_name` | physical gold column name (`_safe_col_name(source_column)`) |
| `source_data_type` | original SQL type, for DDL type mapping |
| `property_uri` | full ontology property URI; **empty string = collision loser, skip triple** |
| `value_map` | `raw_value → ontology URI`; non-empty for enumerable/discriminated columns |
| `confidence_score` | used for C2 collision tiebreaking |

#### `GoldTableDefinition` — one per ontology class

| field | meaning |
|---|---|
| `gold_table_name` | `gold_{class_label}` |
| `primary_class_uri` | the ontology class URI |
| `class_label` | snake_case URI local name |
| `source_silver_tables` | `["silver_raw_party", …]` — may be several merged tables |
| `column_specs` | CONFIRMED columns only, deduped |
| `discriminated_class_uris` | subtype URIs when a column maps distinct values to distinct classes (e.g. `admission_type: ELECTIVE→ElectiveAdmission`) |
| `id_column` | best column to use as the subject-URI key; `None` ⇒ fall back to `_bronze_source_ref` |

#### Bookkeeping models

`GoldWriteResult` and `GoldPipelineStats` are bookkeeping models for the writer.

### 11.3 TABLES — one gold table per ontology class (with merge)

**In plain terms:** every source table is filed under its single best-guess class, and tables that land on the same class are fused into one gold table.

`GoldDefinitionBuilder.build()` (builder.py:353) groups `TableMapping`s by their **winning** class URI:

- For each table it takes `max(tm.class_candidate_set, key=…confidence_score).uri` (builder.py:371) — the highest-confidence candidate class.
- Tables that share that winning class URI are *merged into a single gold table*. `source_silver_tables` becomes the list of all contributing `silver_<source_table>` names (builder.py:378).
- Tables with an empty `class_candidate_set` are skipped with a warning.
- The gold table name is `gold_{class_label}`, where `class_label = _uri_fragment(class_uri).lower()`.

This "one table per class, merge contributors" rule is why a single `gold_patient` can be fed from several silver tables.

It also makes `_entity_class_map()` (builder.py:69) the canonical entity index used by FK resolution: that function strips the `gold_` prefix to recover the entity short name → `primary_class_uri` mapping.

### 11.4 FIELDS/COLUMNS — collecting CONFIRMED specs, with three dedup rules

#### Which columns are eligible

For each merged table the builder iterates only the `CONFIRMED` `column_mappings` (builder.py:400–405), **sorted by `confidence_score` DESC** so the highest-confidence column is processed first.

Columns with any other `MappingStatus` value — `PENDING_REVIEW`, `REJECTED`, `UNMAPPED` — never reach Gold.

#### The three dedup rules

Three deduplication rules apply (builder.py:389–432):

1. **Same `source_column` already seen** → skip (a true duplicate across merged tables).
2. **`gold_column_name` collision** after `_safe_col_name` → skip (a rare edge case).
3. **Same `property_uri` claimed by two source columns** → *both* columns are kept in Gold (for data integrity), but a `C2 pre-OWL collision` warning is logged. The actual suppression happens later, in `_resolve_property_uri_collisions()` (Section 11.6).

#### Building each surviving spec

Each surviving column becomes a `GoldColumnSpec`:

- Its `property_uri` is `mapping.mapped_property_uri or ""`.
- Its `value_map` is built from `mapping.value_mappings` as `{str(source_value): mapped_uri}` — **unless** an override exists for `"{table}.{column}"` in `value_map_overrides`, in which case the sidecar wins (builder.py:433–445).

#### The physical schema (audit skeleton + data columns)

The physical schema is created by `MySQLGoldWriter._create_gold_table()` (mysql.py:269). Every gold table starts from a fixed audit skeleton:

- `_gold_id` (auto-increment surrogate PK)
- `_ontology_class_uri`
- `_bronze_source_ref`
- `_silver_run_id`
- `_gold_run_id`
- `_mapping_confidence_score`
- `_silver_refs`
- `_mapping_run_id`

Then one data column is added per spec via `_add_column_if_missing()`, typed by `_map_sql_type(source_data_type)` against `_TYPE_MAP` (e.g. `VARCHAR→TEXT`, `INT→BIGINT`, `DECIMAL→DECIMAL(20,6)`; SQLite uses `_TYPE_MAP_SQLITE`).

Tables are dropped-and-recreated on `initialise()`. `_drop_stale_tables()` removes any `gold_*` table from a prior run that is not in the current `expected` set.

### 11.5 PRIMARY KEYS — how identity is assigned (`id_column`)

**In plain terms:** the `id_column` decides which column's value becomes the unique name (subject URI) of each row's RDF individual. Picking the wrong column would silently merge unrelated rows into one entity, so this is resolved carefully in two stages.

The `id_column` is the column whose value becomes the subject-URI fragment in RDF.

#### Stage 1 — in `build()` (builder.py:489–529)

The preferred source is the inspector-detected PK carried in `tm.primary_key_columns`. `_pk_id_column()` does the following:

- Collects the PK source columns across the merged silver tables.
- Maps each to its `gold_column_name` where materialised (else keeps the PK source-column name as a marker).
- Joins multiple PK columns with `|` to form a composite key.

This is name-agnostic — it is *not* fooled by a non-unique column literally named `id`.

If no PK column reached Gold, it falls back to `_pick_id_column()` (builder.py:33), a `*_id`-name heuristic that tries, in order:

1. Among non-`row_id` source columns ending in `_id`, the one appearing in the **fewest** gold tables (`col_table_counts == 1`, "most exclusive = most likely the entity's own key").
2. The `row_id` spec.
3. `None`.

The docstring explains the failure this avoids: using a cross-table `_id` (a foreign key) as the subject key would collapse all rows sharing that FK onto one RDF individual.

#### Stage 2 — `refine_id_columns()` (builder.py:541)

This runs against the *populated* Gold DB, so it can issue real uniqueness queries. It is name-agnostic about declared PKs:

- Tables whose source appears in `pk_source_tables` **trust** the PK-derived `id_column` and skip all heuristic passes — a declared PK is unique by definition and may not even be a materialised gold column.
- For tables *without* a detected PK, it runs four passes:

| Pass | What it does |
|---|---|
| **Pass 1** | Verify the existing `id_column` is actually unique (no NULLs, distinct ratio ≥ 1.0 via `_is_unique`); reset to `None` if not (e.g. `gold_route.source_airport_id` is exclusive but non-unique). |
| **Pass 2** | Retry `*_id` candidates ordered by least cross-table frequency; the first unique one wins. |
| **Pass 3** | Name/code fallback for keyless dimension tables, preferring `name`, `*_name`, `code`, `*_code` (via `_name_sort_key`), at a relaxed **0.95** threshold — so near-duplicate names (e.g. India×2) merge onto one subject, which is semantically correct and consistent with FK join values. |
| **Pass 4** | Composite key: try 2- then 3-column combinations of `*_id` columns and pick the smallest whose `CONCAT_WS('|', …)` is unique; stored pipe-separated. |

#### How the subject URI is minted downstream

Downstream (`triple_builder.py:268–313`) the subject URI is minted from `id_column`, using whichever applies:

- a single column value,
- a `|`-joined composite, or
- when `id_column` is `None`: the `fm:entityIdentifierColumn` OWL annotation (an OWL annotation is extra metadata attached to an entity), then the PK value parsed out of `_bronze_source_ref` (format `"{table}.{key}={value}"`).

It never falls back to a blind `id`.

### 11.6 FOREIGN KEYS — object-property resolution (`_resolve_fk_properties`)

**In plain terms:** a foreign-key column should become an ObjectProperty (a link from one entity to another, e.g. `hasPatient`), not a plain attribute. The report often mislabels it, so this step rewrites it to the right link, in the right direction.

An FK column initially carries whatever `mapped_property_uri` the report gave it — often a DatatypeProperty (an attribute whose value is a literal, like a number or string), or a wrong one due to multi-domain bleed.

`_resolve_fk_properties()` (builder.py:220) rewrites these to the correct **ObjectProperty** using three strategies, tried in order per column.

#### Strategy 1 — name match via `entity_map`

If the `source_column` is itself an entity short name (`ecmap.get(spec.source_column)`) and differs from the table's own class, call `_find_object_property(ref_class, current_class, graph)`.

#### Strategy 2 — `fm:sourceForeignKey` table.col match

`_build_fk_annotation_map()` (builder.py:103) parses annotations of the form `"raw_admissions.subject_id -> raw_patients"`:

- It keeps only entries whose left-hand side is `table.column`. Pure-class references like `"admission_type -> AdmissionType"` are skipped — those are handled by `value_map`.
- The map is keyed `(source_table, source_column) → property_uri`.

This handles FK columns whose name differs from the entity name (e.g. `subject_id` resolving to `hasAdmissionPatient`).

#### Strategy 3 — `fm:sourceColumn` routing for multi-domain bleed

`_build_source_column_map()` (builder.py:124) parses `"raw_diagnoses_icd.seq_num"`-style annotations, but **only keeps properties naming a single `table.column`** (`counts[key] == 1`).

Shared properties (e.g. `hasInputeventsMvAmount` annotated on two tables) would collide and are dropped.

When the `FullOntologyGenerator` emits one property with two `rdfs:domain` entries (rdfs:domain = the class a property is allowed to be used on), and the OWL patch adds a table-scoped sibling, Strategy 3 routes each table's column to its own property — fixing a wrong datatype-property URI in the report.

#### How a candidate object property is found

`_find_object_property()` (builder.py:80) scans `OWL.ObjectProperty` subjects and reads `rdfs:domain` / `rdfs:range` as **single** values via `next(graph.objects(prop, RDFS.domain), None)` (rdfs:range = the type of value a property points at).

- It prefers a property whose `domain == ref_class` and `range == current_class`. This **direction flip** is what makes `TripleBuilder` emit `(obj_uri, prop, subject)` instead of `(subject, prop, obj_uri)` — see triple_builder.py:438–441, 467–468.
- Failing that, it falls back to the reverse orientation.

Because `next(...)` reads only the first domain triple, the resolver sees a **single class today**. This is precisely why multi-domain properties (modelled with `owl:unionOf`, an OR over classes) are problematic — they are instead disambiguated via the table-scoped-sibling + `fm:sourceColumn` mechanism (Strategy 3) rather than by reading the union.

Strategy 1 also has a fallthrough: if it finds the entity class but `_find_object_property` returns `None` (e.g. a namespace mismatch between the value-map-derived entity classes and the RIGOR OWL), it falls through to Strategies 2 and 3 (builder.py:259–296).

#### C2 collision resolution

After the FK overrides, `_resolve_property_uri_collisions()` (builder.py:147) groups specs by `property_uri` per table. If two columns still share one property URI:

- The highest-`confidence_score` column keeps the URI.
- The losers have their `property_uri` cleared to `""`.

The loser's data stays in Gold (for auditability), but `TripleBuilder` skips triple emission for empty-URI specs (triple_builder.py:372–376).

Collision records are accumulated in `builder.collisions`, and `main.py._patch_collisions_in_report()` rewrites the on-disk report to mark losers `PENDING_REVIEW`.

### 11.7 VALUES — categorical handling and discriminated subtypes

#### Two consumers of `value_map`

Categorical/enumerable columns carry a `value_map` (`raw_value → ontology URI`). Two consumers use it:

1. **The semantic view** (`_create_semantic_view`, mysql.py:311) emits a `CASE` expression (`_case_expr`) that translates raw cell values to URIs at query time. So `gold_semantic_*` shows the resolved URI while the base `gold_*` table preserves the raw value.
2. **`TripleBuilder`** uses `value_map` to emit object-property triples (or `fm:incompleteReference`) instead of literals.

#### Discriminated subtypes

**In plain terms:** when one column's values split a table into finer classes (e.g. an admission type that distinguishes elective from emergency admissions), those finer classes become the `rdf:type` written to the triplestore.

`build()` (builder.py:461–470) scans CONFIRMED mappings:

- When a column's `value_mappings` produce **more than one** distinct `mapped_uri`, those URIs become the table's `discriminated_class_uris` (sorted).
- These are the actual `rdf:type` values written to the triplestore (`rdf:type` states what class an individual belongs to) — distinct from `primary_class_uri`.
- The SPARQL context builder (`rdf/query/context_builder.py:248`) uses them to generate `UNION` patterns (SPARQL is the query language for RDF).

The `_discriminator_value` silver provenance column is read but stripped before write (Section 11.8).

### 11.8 Materialisation — `GoldPipeline` and the gold/semantic tables

#### The three-step run

`GoldPipeline.run()` (pipeline.py:34) executes in three steps:

1. `writer.initialise(definitions)` does DDL + drop/recreate, serially.
2. Each `GoldTableDefinition` is processed — serially under SQLite, otherwise in a `ThreadPoolExecutor(max_workers=PIPELINE_WORKERS)`, since each definition writes a distinct table.
3. `writer.finalise()` returns `GoldPipelineStats`.

#### How each definition is processed

`_process_definition()` (pipeline.py:80) reads each source silver table in `GOLD_BATCH_SIZE` `LIMIT/OFFSET` batches.

- It first calls `_get_silver_columns()` and filters `column_specs` to those whose `gold_column_name` actually exists in that silver table — so merged tables contributing different column subsets are handled.
- For each row it pulls audit columns (`_silver_id`, `_ontology_class_uri`, `_bronze_source_ref`, `_run_id AS _silver_run_id`, `_mapping_confidence_score`) plus any present provenance columns.
- It pops `_silver_id` into a `_silver_refs` JSON (`{silver_table: [silver_id]}`) and stamps `_mapping_run_id` / `_gold_run_id`.

Then `MySQLGoldWriter.write_batch()` (mysql.py:160) strips the four `_SILVER_PROVENANCE_COLS`, groups rows by column-set, and issues `INSERT IGNORE` / `INSERT OR IGNORE`.

#### The two artifacts per class

The result is two artifacts per class:

- The wide base table `gold_<class>` — raw values + audit columns.
- A `gold_semantic_<class>` view (`_create_semantic_view`) that:
  - aliases each data column to its OWL property local name (`_uri_fragment(property_uri)`, e.g. `hasSubjectId`), and
  - applies the `value_map` `CASE` translation.
  - Alias collisions fall back to the raw gold column name.

These tables are what the relational query layer (`semantic_mapper/query/gold_context_builder.py`, `engine.py`) and the `execute_sql_gold` MCP tool query — and what `TripleBuilder` reads to mint triples.

---

## 12. RDF Materialisation — Rows to Triples

**In plain terms:** This is the stage that turns finished relational rows into RDF triples (a *triple* is one subject–predicate–object fact, like `<patient42> hasName "Alice"`) and loads them into a graph database (Fuseki). Everything the earlier stages decided about classes, properties and identity gets *executed* here, one row at a time.

**Inputs → Outputs:** in = the generated ontology (the formal class/property model) + the GOLD relational views (the cleaned, query-ready tables); out = typed RDF triples streamed into a named graph in Fuseki, plus a stats object describing the run.

This layer consumes the ontology produced by the generator and the `GoldTableDefinition` objects produced by the gold layer, reads gold rows in batches, and emits typed triples into a *named graph* (a labelled sub-collection of triples within the triple store). The work is split across five modules under `semantic_mapper/rdf/`.

| Module | Responsibility |
| --- | --- |
| `pipeline.py` (`RdfPipeline`) | Orchestration: load ontology, enrich it with nested-column schema, batch-read gold, fan out across threads, write, finalise. |
| `triple_builder.py` (`TripleBuilder`) | The core row→triples transform. |
| `uri_factory.py` (`UriFactory`) | Mints deterministic instance URIs; the locus of entity *identity*. |
| `nested_expansion.py` | Explodes packed multi-valued cells into first-class entities and edges. |
| `models.py` (`RdfPipelineStats`) | The frozen pydantic stats object returned by a run. |

### 12.1 Inputs and Outputs

#### What goes in

`RdfPipeline.__init__` (`pipeline.py:28`) takes four things:

- a `List[GoldTableDefinition]`;
- a SQLAlchemy `Engine` pointed at the gold database;
- an `RdfWriter` (either Fuseki, or an rdflib in-memory writer);
- `Settings`.

Each `GoldTableDefinition` (`semantic_mapper/gold/models.py:24`) carries:

- `gold_table_name` (`"gold_{class_label}"`), `primary_class_uri`, `class_label`;
- `column_specs: List[GoldColumnSpec]` — confirmed columns, each with `source_column`, `gold_column_name`, `source_data_type`, `property_uri`, and a `value_map: Dict[str,str]` (raw value → URI, i.e. an *IRI/URI* — a globally-unique name for a thing);
- `discriminated_class_uris` and `id_column` (the chosen subject-URI key, possibly `None`).

#### The ontology, loaded separately

The ontology itself is loaded as an `rdflib.Graph` (`pipeline.py:51-63`) via `OntologyLoader.load(settings.ONTOLOGY_PATH)`. It drives object-property direction and typing.

A failure to load is logged as a warning, not fatal. The builder then runs with empty `_prop_range`/`_superclass_map`/`_class_uris` and degrades to literal-only / name-based behaviour.

#### What comes out

Triples are streamed into a Fuseki named graph. The run returns an `RdfPipelineStats` (`models.py:10`, frozen) with: `run_id`, `triples_written`, `tables_processed`, `named_graph`, timestamps and `duration_seconds`.

This stats object is the boundary handed to the QUERY/PROOF layer above.

### 12.2 The TripleBuilder build path (one gold row → triples)

**In plain terms:** `TripleBuilder` takes one gold row and returns the list of triples that row implies — its identity, its type(s), its attributes, and its links to other entities.

`TripleBuilder.build(row, definition)` (`triple_builder.py:260`) returns a `List[Tuple[URIRef, URIRef, Any]]` for a single row. The builder is constructed once and is **stateless after construction**, so it is shared across worker threads (`pipeline.py:77-84`).

#### Lookup tables precomputed at construction

Construction (`__init__`, lines 174-258) precomputes several lookup tables from the definitions and ontology:

- `_entity_map` — short name → gold table, e.g. `"patient" → "gold_patient"` (line 195), built by stripping the `gold_` prefix. Used for column-name FK detection.
- `_prop_range` — object-property URI → `rdfs:range` class URI (`_build_prop_range`, line 99). (`rdfs:range` is the type of value a property points at — here, a class.) Used for direction resolution and FK detection by range.
- `_superclass_map` — class URI → **all transitive ancestors** (`_build_superclass_map`, line 124), excluding `owl:Thing` (the universal root class), with a cycle guard. Drives multi-`rdf:type` emission.
- `_class_to_entity` — `primary_class_uri` → entity short name (line 212), the reverse of `_entity_map` keyed by class.
- `_class_uris` — the set of all `owl:Class` URIs (`_build_class_uri_set`, line 109), plus a cross-namespace extension (lines 232-241) that also treats a `value_map` URI as a class when its `#local` fragment matches a known class fragment (handles base-domain ontologies like `OrgHealth.owl#Payer` vs `synthea#Payer`).
- `_entity_id_col` — entity short name → its `id_column` (line 247), used to guard FK detection so only PK-carrying columns mint URIs.
- `_class_id_source_col` — class URI → source column, harvested from the OWL annotation `fm:entityIdentifierColumn` (lines 252-258), used as a fallback when `id_column` is `None` (e.g. MIMIC's `cgid`, `itemid`).

#### TABLES → typed instances

The gold table identity is fixed by `definition`. Each table becomes a stream of subjects of class `primary_class_uri`.

There is no per-table triple beyond the per-row instances — the table's *schema* role was already discharged by the ontology generator and the gold builder.

#### PRIMARY KEYS → subject URI (identity)

**In plain terms:** every row needs a stable, unique name (its subject URI). This step picks which column value to base that name on, then hashes it so the same key always produces the same URI.

Identity is minted in `build` lines 268-313. The subject key is chosen in this priority order:

1. **Composite key** — if `id_column` contains `|` (e.g. `"source_airport_id|dest_airport_id|airline_id"`), the non-null parts are joined with `_` and that string keys the URI (lines 291-299). If any part is null, it falls back.
2. **Single `id_column`** — `row[id_column]` if non-null (lines 309-311).
3. **`fm:entityIdentifierColumn` fallback** — when `id_column` is `None`, the annotated source column is resolved to its gold column (lines 301-308).
4. **`_fallback_key()`** (lines 280-289) — prefers `_bronze_source_ref`, taking the *value* after `=` in the `"{table}.{key}={value}"` format (a stable, unique PK-derived fragment), then `_gold_id`, then literal `"unknown"`. The code explicitly refuses to blindly default to an `id` column, because a non-unique business code would collapse rows onto one subject.

##### How the URI is minted

The actual minting is delegated to `UriFactory.individual(class_label, id_value)`. **Identity is a hash of the PK value.**

`hash_identifier` (`uri_factory.py:10`) is `sha256(str(value).strip())[:16]` — a deterministic 64-bit hex digest. It is applied identically to subject keys *and* FK-target keys, so that a foreign key reconstructs the *exact same* URI from the referenced PK value and joins hold.

URI shape (`individual`, line 53):

- without source scoping: `{base_uri}/{entity_name}/{hash}`
- with `RDF_SOURCE_NAME` set: `{base_uri}/{source_name}/{entity_name}/{hash}`

Source scoping (percent-encoded) prevents cross-source collisions of equal `id_value`s.

Note the deliberate namespace separation: `RDF_BASE_URI` is the **data** namespace (default `http://mesh.local/data/`), distinct from the ontology namespace used for classes/properties.

##### Edge case: rows that merge

A consequence the pipeline watches for: if two gold rows hash to the same subject they **merge** into one RDF individual.

`_process_definition` (`pipeline.py:296`, 318-325) tracks distinct subjects per table and logs a warning quantifying any merge (relevant to the C1 row-count-vs-subject-count proof check). Merged rows still emit triples — they just collapse onto one subject.

#### Class assertions (`rdf:type`)

**In plain terms:** this step declares what kind of thing the subject is. (`rdf:type` is the predicate that says "this individual is an instance of this class".) It asserts the specific subtype, every ancestor of that subtype, and the primary class — so broad queries always match.

After the subject is minted (lines 324-343):

- the row's `_ontology_class_uri` (the discriminated subtype, if any) is emitted;
- then **all its transitive ancestors** from `_superclass_map`;
- then the `primary_class_uri` is *always* emitted too (with its ancestors);
- all de-duplicated via `emitted_types`.

The primary-class guarantee ensures `?x a :Admission` matches every row of `gold_admission`, even when the discriminated type is a bare `NamedIndividual` (an individual whose class membership isn't reached via a `subClassOf` path to the primary class).

The first triple is always a type assertion, which `pipeline.py:309` relies on to recover the subject URI for merge tracking.

#### FIELDS/COLUMNS → datatype-property literals

**In plain terms:** plain attribute columns become *DatatypeProperty* values — an attribute whose value is a literal, e.g. a number, string, or date — with the right XSD type attached.

For each `GoldColumnSpec` with a non-empty `property_uri` (lines 354-377), the value is read from `row[gold_column_name]`.

A spec whose `property_uri` is empty is a **C2 collision loser** — its property was cleared by the gold builder to suppress duplicate-predicate emission — and is silently skipped (lines 372-376).

When the value is not an FK/object link (see below), it becomes a typed literal:

```python
datatype = _xsd_type(spec.source_data_type, value)
triples.append((subject, prop, Literal(_normalise_literal(value, datatype), datatype=datatype)))
```

##### How SQL types map to XSD datatypes

`_xsd_type` (line 60) maps the declared SQL type to an XSD datatype (XSD = the standard set of literal value types like `xsd:integer`, `xsd:string`):

| Declared SQL type | XSD datatype | Condition |
| --- | --- | --- |
| `INT` / `BIGINT` / `SMALLINT` / `TINYINT` | `xsd:integer` | — |
| `DECIMAL` / `NUMERIC` | `xsd:decimal` | — |
| `FLOAT` / `DOUBLE` | `xsd:double` | — |
| `BOOLEAN` / `BIT` | `xsd:boolean` | — |
| `DATE` | `xsd:date` | only if value matches `YYYY-MM-DD`, else string |
| `DATETIME` / `TIMESTAMP` | `xsd:dateTime` | only if it matches the datetime regex |

##### Value-sniffing for string columns

Crucially, for **string** SQL types (`TEXT`, `VARCHAR`, etc., listed in `_STRING_SQL_TYPES`) it *sniffs the value*:

- a purely-integer string becomes `xsd:integer`;
- a decimal string becomes `xsd:decimal`.

This means numerics stored as TEXT (common in Synthea/CSV imports) remain usable in SPARQL aggregates.

##### Date normalisation

`_normalise_literal` (line 45) rewrites MySQL `"YYYY-MM-DD HH:MM:SS"` to ISO-8601 `T`-separated form when the datatype is `xsd:dateTime`. Without this, SPARQL range filters silently return nothing.

#### VALUES → NamedIndividual links (`value_map`)

**In plain terms:** when a column holds a value from a fixed vocabulary (e.g. an ethnicity code), it is emitted as a *link to a vocabulary individual* rather than a plain literal — turning the attribute into an `ObjectProperty` (a link from one entity to another). Several guards stop a value that should be an individual from accidentally pointing at a *class*.

When a spec carries a `value_map`, categorical/enumerable values are emitted as **object links to vocabulary individuals** rather than literals (lines 486-540).

Lookup is `value_map.get(str(value))`; if that misses, the value is checked for being an already-materialised URI (`http(s)://`).

Three guards protect join integrity before emitting `(subject, prop, URIRef(mapped))`:

1. **BUG-02 / range-class mapping error** — if the mapped URI equals the property's `rdfs:range` *class* URI (`_range_class_uri`), then either:
   - the range class *has* a gold table (a genuine mapping error → emit `fm:invalidMapping`, lines 502-503), or
   - it does *not* (a missing entity → emit `fm:incompleteReference`, lines 504-505).

   The two predicates are kept distinct so the C5 incompleteness count stays precise.
2. **FK-to-missing-class fallback** — if the mapped/pre-materialised URI is in `_class_uris` (any class, e.g. an unmaterialised `Payer`), emitting it as an object value would corrupt joins, so `fm:incompleteReference` is emitted instead (lines 506-513, 520-522, 533-535).
3. **Otherwise** — the individual URI is emitted normally.

#### FOREIGN KEYS → object-property links (with direction flip)

**In plain terms:** a column that references another table's primary key becomes an `ObjectProperty` edge to that other entity. Detection is careful: a column only mints a target URI if it genuinely carries the referenced entity's PK (not, say, a business code that happens to share a name).

A column without a `value_map` (or whose value isn't a vocabulary hit) may still be a foreign key. There are **two detection paths**, both guarded so a column only mints a target URI when it actually carries the referenced entity's PK:

- **Path 1 — name match** (lines 429-444): `spec.source_column` matches an entity name in `_entity_map`. The mint guard fires only if:
  - the target entity has no known `id_column`, *or*
  - the source/gold column name ends in `_id`, *or*
  - the value matches `_UUID_RE`.

  (BUG-01: in UUID-keyed schemas a column that holds a UUID is carrying the PK, not an alternative code like an IATA code. A bare `airline` column holding IATA `"2B"` correctly falls through to literal.)
- **Path 2 — range match** (lines 452-477): even if the column name doesn't match, the property's `rdfs:range` (via `_prop_range` → `_class_to_entity`) identifies the target entity (e.g. `hasDiagnosesIcdAdmission` has range `Admission` while the column is `hadm_id`). The same `_id`/UUID guard applies, plus a **bare-PK guard**: the column matches when its name equals the target entity's `id_column` (handling MIMIC's `cgid`, `itemid`). A non-`_id`, non-PK-name column whose property is an object property is skipped entirely (lines 472-477) rather than emitting a semantically-invalid literal for an object property — the sibling `_id` column will emit the correct URI.

In both paths the FK target URI is minted with the **same** `UriFactory.individual(entity, str(value))` used for subjects, so the hash matches the referenced subject and the edge resolves.

##### Direction flip

This is the key generalisation (class docstring, lines 163-172).

After minting `obj_uri`, the builder checks whether the property's `rdfs:range` equals the *current* row's `primary_class_uri`:

- **If it matches** — the triple is emitted **with the FK target as subject**: `(obj_uri, prop, subject)` (lines 438-441, 467-470). Thus while processing an *encounter* row, `patientHasEncounter` (whose range matches the current class) correctly produces `<patient> patientHasEncounter <encounter>`.
- **If it does not match** — the natural direction `(subject, prop, obj_uri)` is used.

This domain-agnostic rule lets a property declared on the "one" side be materialised correctly from the "many" side table.

#### Null FKs / incompleteness (`fm:incompleteReference`)

**In plain terms:** when a foreign-key column is empty, that may mean a *reference* is broken (not just data missing). The builder records the broken-reference case explicitly so the graph stays honestly incomplete and countable.

When an object-property column's value is `None` (lines 378-417), the builder decides whether the *absence* is a broken reference worth recording.

It emits `(subject, fm:incompleteReference, Literal(str(prop)))` when the property's `rdfs:range` either:

- (a) has a corresponding gold table (a true FK join column), or
- (b) has no gold table but is named as a `value_map` target class (FK-to-missing-entity, e.g. Synthea payer UUIDs with no `gold_payer`).

DatatypeProperties and terminal categoricals (NamedIndividual ranges with no gold table / no value_map target) are deliberately excluded — a null there is missing *data*, not a broken *reference*.

This keeps the subject visibly incomplete in the graph and countable by check C5.

#### Provenance

Finally (lines 542-550) every subject gets `fm:bronzeSourceRef` and `fm:goldRunId` literals from `row["_bronze_source_ref"]` / `row["_gold_run_id"]`.

Without these the `prove_answer` lineage queries have no edge from a triple subject back to a gold row and return `proof_failed` with an empty trace — so this is the explicit hook the PROOF layer joins on.

### 12.3 Nested expansion — packed cells to first-class entities

**In plain terms:** sometimes one gold cell holds a whole list of sub-records packed as text (e.g. all the events an athlete competed in). This step "opens up" that text and turns each packed record into its own real entity with its own URI and edges.

`nested_expansion.py` handles a denormalised 1-to-N group packed into one gold cell as a stringified list of flat dicts, e.g. `[{'discipline': 'Alpine Skiing', 'event': "Men's Slalom"}, ...]`. Gold keeps the opaque text; the RDF boundary "opens it up".

#### Detection

`detect_nested_columns` (line 234): a non-audit column qualifies when ≥ `min_fraction` (default 0.5) of its sampled non-null cells `parse_nested_cell` as a list of homogeneous dicts.

`parse_nested_cell` (line 209) accepts both JSON and Python-repr, requires `[...]` wrapping, and returns `None` for scalars/list-of-scalars. Discovered keys are the union of object keys — fully **data-driven, never hardcoded**.

#### Term naming

`nested_term_names` (line 40) derives the new ontology terms:

- `events → child class Event`;
- parent property `hasEvent`;
- per-key literal props (`discipline → eventDiscipline`);
- a key equal to the entity name becomes `<child>Name`.

#### Reconciliation

`reconcile_nested_keys` (line 62): a child-key is *promoted* from a literal to an object link into an **existing** value-mapped vocabulary when ≥ `min_fraction` (default 0.95) of its sampled values resolve through `reconcile_by_key[key]` to individuals of a **single** class.

Reconciliation is scoped per field name so homonyms (event "Men" vs gender "Men") cannot cross-link. Promoted keys move from `key_props` to a `key_links` map and reuse an existing `has<Class>` property when one exists.

#### Explosion

`explode_nested_cell` (line 115): for each packed object it computes a **shared identity** `"|".join(f"{k}={v}" ...)` over sorted non-null keys and mints a child URI from it via the same `UriFactory`. So 96 athletes referencing `Men's Slalom` all point at **one** Event individual.

It emits:

- `(subject, parent_prop, child_uri)`;
- `(child_uri, rdf:type, Child)`;
- each key as a literal;
- reconciled keys as links to existing vocabulary individuals (no triple if unresolved, to keep the object property from ever carrying a literal).

`parent_prop` is the packed column's **own** mapped property URI — unpacking *repurposes* that property from a string DatatypeProperty into an ObjectProperty.

In `TripleBuilder.build` (lines 345-371) a column listed in `self._nested[gold_table]` is exploded instead of literal-emitted, using `spec.property_uri` as the edge.

#### Ontology enrichment

`_detect_and_enrich_nested` (`pipeline.py:138`) runs *before* `initialise()` so the schema is persisted to the `.owl` and loaded into Fuseki/Protégé. It:

- builds `reconcile_by_key` (field → {value: individual URI});
- removes the column's `owl:DatatypeProperty` / old range;
- drops orphan literal props that were reconciled away;
- adds the child class, the repurposed ObjectProperty, and per-key declarations via `nested_ontology_triples` (line 160).

Exploded properties are stamped `fm:multiValued true` (lines 182-183) so the C2 cardinality check exempts the expected fan-out from its double-mint heuristic.

The enriched graph is re-serialised to `ONTOLOGY_PATH` (xml for `.owl/.rdf`, else turtle).

### 12.4 Pipeline orchestration — layering, named graphs, Fuseki load

**In plain terms:** this is the conductor. It loads the ontology, enriches it for nested columns, builds the shared transformer, reads gold tables in parallel batches, writes triples into Fuseki, and stamps the run.

#### Run sequence

`RdfPipeline.run` (`pipeline.py:40`) sequences the run:

1. Generate `run_id`; load the ontology graph (for direction detection).
2. `_detect_and_enrich_nested` — enrich the `.owl` first (so the initialise step loads the enriched schema).
3. `writer.initialise(RDF_DATASET_NAME, RDF_NAMED_GRAPH, ontology_path)`.
4. Construct the shared `TripleBuilder`.
5. **Fan out per definition.** SQLite engines run sequentially (lines 90-95); other dialects use a `ThreadPoolExecutor` with `PIPELINE_WORKERS` (lines 97-116). Any worker exception is logged and re-raised.
6. `writer.finalise(...)` returns `RdfPipelineStats`.

##### A note on layering and ordering

Note that **bronze/silver/gold layering is upstream of this module**. By the time `RdfPipeline` runs, only the gold tables (`definitions`) are read, in no guaranteed order, because FK joins resolve through deterministic hashing rather than processing order.

The "layering" preserved here is the provenance trail (`fm:bronzeSourceRef`, `fm:goldRunId`) that lets the proof layer walk RDF → gold → silver → bronze.

#### Per-definition worker

`_process_definition` (line 280) pages gold with `SELECT * FROM {tbl} LIMIT {RDF_BATCH_SIZE} OFFSET {offset}` (`_read_gold_batch`, line 333; SQLite double-quotes the identifier, others backtick it).

It calls `triple_builder.build(dict(row), definition)`, buffers, and flushes via `writer.write_batch` once the buffer reaches `RDF_BATCH_SIZE` (default 1000). It tracks `seen_subjects` for merge detection.

#### Named graphs and the Fuseki load

The relevant code is in `writer/fuseki.py`. The `FusekiWriter` keeps **data** and **ontology** in separate named graphs:

- data goes to `RDF_NAMED_GRAPH` (default `http://mesh.local/graph/current`);
- the ontology goes to its own `ontology_named_graph`.

##### `initialise` (line 94)

- creates the TDB2 dataset (idempotent on 409);
- `DROP SILENT GRAPH` the data graph;
- if an ontology path is given, `DROP SILENT` then GSP-`POST`es the ontology into the ontology graph.

The drop-before-load prevents stale OWL-Full leftovers when the nested-column repurposing re-types a property between runs.

##### `write_batch` (line 143)

Serialises triples to N-Triples and wraps them in `INSERT DATA { GRAPH <named_graph> { ... } }` POSTed to `/update`.

Data writes deliberately avoid GSP `/data` because a Docker-injected stale `RDF_NAMED_GRAPH` env var could silently deposit triples in the wrong graph; embedding the graph URI in the SPARQL string is resilient to that. (The ontology load is the one exception — it targets a different graph and runs once.)

##### `finalise` (line 174)

Writes a graph-level load stamp onto the named-graph node:

- `fm:loadRunId`, `fm:loadTimestamp`;
- optional `fm:projectName`;
- optional `fm:ontologySource` (for C6 drift detection — non-fatal if it fails).

It then triggers TDB2 compaction and returns the populated `RdfPipelineStats`.

### 12.5 Handling summary

| Stage element | How it is handled here |
| --- | --- |
| **TABLES** | Each `GoldTableDefinition` → a stream of instances of `primary_class_uri`; tables read by paged `SELECT *` and fanned out across threads. |
| **PRIMARY KEYS** | Establish *identity*: `id_column` (single / `|`-composite) → `UriFactory.individual` → `sha256[:16]` hash; falls back to `fm:entityIdentifierColumn`, then `_bronze_source_ref` value, then `_gold_id`. Always also drives `rdf:type` (primary class + transitive ancestors). |
| **FIELDS/COLUMNS** | Datatype-property literals typed by `_xsd_type` (declared SQL type, with value-sniffing for string columns) and normalised (`_normalise_literal`). Collision-loser columns (empty `property_uri`) skipped. |
| **VALUES** | Categorical values via `value_map` → links to `NamedIndividual` URIs; guards emit `fm:invalidMapping` / `fm:incompleteReference` when a value resolves to a class instead of an individual. |
| **FOREIGN KEYS** | Object-property links via name-match (`entity_map`) or range-match (`_prop_range`/`_class_to_entity`), guarded so only PK-carrying (`_id`/UUID/bare-PK) columns mint target URIs; **direction flips** when the property's range equals the current class. Null FKs → `fm:incompleteReference`. |
| **Nested/packed cells** | Exploded into shared child entities + edges (`nested_expansion`), repurposing the column's own property; schema enriched into the `.owl` and stamped `fm:multiValued`. |
| **Provenance** | `fm:bronzeSourceRef` + `fm:goldRunId` per subject; graph-level `fm:loadRunId`/`fm:loadTimestamp`/`fm:projectName`/`fm:ontologySource` at finalise. |

---

## 13. Querying & Proving (Consumption Layer)

**In plain terms:** This is the last stage, where someone asks a question in plain English, ForgeMesh answers it by running a query against the data, and then *proves* the answer by assembling an audit record that traces it all the way back through the gold/silver/bronze data layers to the ontology (the formal schema of classes and properties) that justified it.

The consumption layer is where every upstream decision becomes observable. A user — or an LLM agent — asks a natural-language question. ForgeMesh answers it by querying the materialised data, then *proves* the answer by assembling a record that ties the result back through gold/silver/bronze lineage to the ontology that licensed it.

This section documents two cooperating stacks:

- The **query engine** (`semantic_mapper/query/`), which translates questions into SQL/SPARQL and executes them safely.
- The **proof/grounding stack** (`semantic_mapper/prove/` and `semantic_mapper/rdf/proof/`), which constructs the auditable `ProofRecord`.

The recurring theme: the table/field/value/PK/FK choices made in BRONZE→SILVER→GOLD→RDF do not disappear. They resurface here as schema hints, join pairs, lineage hops, scope assumptions, and limitations.

### 13.1 Inputs and outputs at a glance

**Query engine**

- **Inputs:** a `SemanticMappingReport` (silver mode) *or* a `list[GoldTableDefinition]` (gold mode); live SQLAlchemy `Engine`s for the silver and/or gold MySQL databases; and `Settings`.
- **Output:** each `query(question)` call produces a `QueryResult` carrying `generated_sql`, `explanation`, `rows`, `row_count`, `tables_used`, token telemetry, and `error`.

**Proof stack**

- **Inputs:** the executed query; its `mode` (`"sparql"` / `"gold"` / `"silver"`); the scalar `answer`; the `run_id`; plus the pre-computed outputs of the proof phases — `ProofQuery`, subject URIs (IRI/URI: a globally-unique name for a thing), `LineageChain`s, `OntologySubgraph`, `MappingDecisionRef`s, `InterpretationNote`, and `ProofCaveat`s.
- **Output:** an immutable `ProofRecord` (or a `RowProofRecord` / `ProofTree`).

### 13.2 Natural-language → SQL: `SemanticQueryEngine`

`SemanticQueryEngine` (`semantic_mapper/query/engine.py`) is the SQL-mode entry point.

#### Choosing a layer up front

The constructor (lines 126–172) selects a layer at construction:

- If a `gold_engine` *and* `gold_definitions` are both supplied → **gold mode** (and `_exec_engine` is the gold engine, line 138).
- Otherwise → **silver mode** against the silver engine.

#### Three things pre-built so the LLM never has to guess

1. **Schema context.** Gold mode calls `build_gold_schema_context(...)`; silver mode calls `build_schema_context(...)`. Both render a compact natural-language description of the tables.
2. **Actual-value hints.** `_actual_value_hints[_gold]` queries the live tables for distinct low-cardinality column values and appends them to the schema context (see VALUES, §13.5).
3. **A frozen system prompt** built from `_SYSTEM_PROMPT_TEMPLATE` (lines 44–76), with either `_RULES_GOLD` or `_RULES_SILVER` spliced into rule slot 3.

#### The `query()` pipeline (never raises)

`query()` (lines 177–293) is deliberately total — its docstring states *"Never raises — all failures are captured in `QueryResult.error`."* The pipeline:

1. **Call the LLM** (`_call_llm`, OpenAI chat completion in JSON mode, `temperature=0`, model `Settings.QUERY_MODEL`).
2. **If the LLM returns null `sql`**, surface `Cannot answer: {explanation}` — the deliberate "the data isn't there" path mandated by prompt rule 9.
3. **Safety check** via `validate_select_only`.
4. **Enforce the row cap.**
   - **(4a)** In gold mode, **validate referenced columns**.
5. **Execute.**
6. **Determine `tables_used`.**

Every branch records the turn into `_conversation_history`, so follow-up questions are multi-turn. `reset_conversation()` clears it; `turn_count` reports completed turns.

#### The prompt makes the upstream mapping explicit

- **Rule 6** warns that columns "marked 'contains full URI' store values like `'...#ClassName'`" and must be displayed with `SUBSTRING_INDEX(col, '#', -1)` and filtered with `LIKE` — and that this must **never** be applied to numeric columns.
- **Rule 8** ("use ONLY those exact values when filtering") binds generated WHERE clauses to the sampled VALUES.

### 13.3 Gold vs silver SQL modes (how TABLES surface)

The two rule blocks encode the structural difference established in the GOLD stage:

| Concern | Silver mode (`_RULES_SILVER`) | Gold mode (`_RULES_GOLD`) |
|---|---|---|
| Class identity | A silver table may hold multiple classes; filter by `_ontology_class_uri LIKE '%ClassName'`; GROUP BY it for class counts | Each gold table is exactly one class (the table name encodes it, e.g. `gold_patient` = `Patient`); **never** filter or GROUP BY `_ontology_class_uri` |
| Ranking | — | `ORDER BY … DESC LIMIT N` on rows, not MAX/GROUP BY |
| Grouping | GROUP BY `_ontology_class_uri` with `SUBSTRING_INDEX(...)` alias | GROUP BY real business columns only |
| Joins | (not specified) | Use the explicit `JOINS` block of FK column pairs; never guess from `fips`/`zip`/`lat` |

**What this means:**

- A **silver TABLE** is a typed projection that can carry several ontology classes, distinguished only by the `_ontology_class_uri` metadata column.
- A **gold TABLE** is a single-class, ontology-mapped view whose *name is the class fragment* (e.g. `gold_patient` → `Patient`).

### 13.4 The gold schema context and JOINS (how FIELDS, PKs, and FKs surface)

`build_gold_schema_context` (`semantic_mapper/query/gold_context_builder.py`) renders one `TABLE:` block per `GoldTableDefinition`.

#### What each table block contains

- **Header:** `TABLE: gold_x  (class: <fragment>)`, where the class fragment comes from `definition.primary_class_uri` — or, if live `actual_class_uris` were sampled, the pipe-joined list of class fragments actually present.
- **Two always-documented audit columns:**
  - `_ontology_class_uri` ("filter with `LIKE '%ClassName'`")
  - `_bronze_source_ref` ("audit: source row reference")
- **One line per `column_spec`:** `  <col> <source_data_type>  — <property_fragment>`, where the property fragment is `_uri_fragment(spec.property_uri)`.

That last line is exactly how a GOLD column's bound ontology property resurfaces at query time: the LLM sees the column name, its SQL type, and the human-readable ontology property it materialises.

#### How foreign keys (FKs) are reconstructed

Foreign keys are rebuilt *structurally* (lines 28–34, 64–89):

- The builder builds an `entity_map` of `entity-short-name → gold_table_name` (stripping the `gold_` prefix).
- For every column, it checks whether the column name matches another gold entity name.
- If so, it annotates the line `(FK → gold_<other>.id)` and appends `gold_x.<col> = gold_<other>.id` to a `JOINS` block.
- The final `JOINS — use these exact column pairs when joining gold tables:` section is the deterministic backbone behind gold rule 5a.

#### Primary keys (PKs) are implicit and uniform

The convention is that each gold entity is keyed by an `id` column — which is why every reconstructed FK points at `<table>.id`.

> **Honestly-flagged heuristic / false positive.** FK detection is *name-based* (a column name equal to another entity's short name). So a column that merely *happens* to share an entity's name will be reported as an FK even if it is not one. This section documents what the code does, and this is the one place where a false-positive join hint could be produced.

### 13.5 VALUES: live-sampled column hints

Both `_fetch_actual_class_uris[_gold]` and `_actual_value_hints[_gold]` (`engine.py`) connect to the live DB and `SELECT DISTINCT` per column (capped at `_MAX_DISTINCT = 20`; metadata columns starting with `_` are skipped). VALUES surface three ways:

- **Class fragments.** Distinct `_ontology_class_uri` values are reduced to their `#`/`/` fragment and listed per table, so the LLM knows the exact `ClassName`s available for `LIKE` filtering.
- **Categorical literals.** Low-cardinality non-URI columns are listed verbatim (`gold_x.col: 'M', 'F'`). The `_is_categorical` gate (lines 103–120) suppresses PII/identifier noise via `_SKIP_COL_RE` (email/addr/dob/birth/nm/name/id) and value-shape regexes (`_DATE_RE`, `_EMAIL_RE`, `_REF_CODE_RE`, all-digits) — these would not help the LLM write better SQL and may be sensitive.
- **URI-valued columns.** Columns whose sampled values start with `http` are exposed as groupable categories: `... stores URIs — use LIKE '%Fragment' ... Actual fragments: 'A', 'B'`. This directly reflects the GOLD/RDF decision to materialise an object property (a link from one entity to another) or an enumerated value as a URI rather than a literal.

#### Catching a SILVER transform that never fired

A distinctive silver-mode check (lines 394–407): if the schema context *claims* a column "contains full URI after transformation" but the sampled values are **not** URIs, the appendix emits:

> `TRANSFORMATION DID NOT FIRE — actual values are … Do NOT use this column for class filtering; use _ontology_class_uri instead.`

This is the query layer catching a SILVER transform that did not execute, and steering the LLM to the reliable metadata column.

### 13.6 Safety, row caps, and pre-execution column validation

- **`validate_select_only`** (`semantic_mapper/query/safety.py`) strips `--` and `/* */` comments, requires the statement to begin with `SELECT`, and rejects any banned keyword (`INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|REPLACE|MERGE|CALL|EXEC`) anywhere in the stripped text, raising `QuerySafetyError`. This is a coarse allow-list guard, not a full SQL parser — it is the line of defence that keeps generated SQL read-only.
- **`_enforce_limit`** (lines 636–646) guarantees a `LIMIT` no larger than `Settings.QUERY_MAX_ROWS`: it clamps an existing oversized `LIMIT` or appends one if absent.
- **`_validate_gold_columns`** (gold mode only, lines 609–634) runs `_QUALIFIED_COL_RE` over the SQL to find `` `table`.`column` `` references and checks each against `_gold_columns` (a `{table: set(column_names)}` index built by `SELECT * … LIMIT 0`). Unknown columns produce an error listing the available columns, short-circuiting before execution. This is a hard backstop for prompt rule 9 ("never invent column names") — a fielded enforcement of the FIELDS that actually exist in GOLD.

`_extract_tables_used` (`_TABLE_RE`, matching identifiers after `FROM`/`JOIN`) provides the `tables_used` fallback when the LLM omits it.

### 13.7 SPARQL execution and proof framing (where the ontology bites)

**In plain terms:** SQL modes hit the relational gold/silver tables; SPARQL mode hits the actual RDF triples (a triple is one subject–predicate–object fact), and this is the first point where the ontology's own domain/range rules are checked directly.

SQL modes query the relational gold/silver views. **SPARQL mode** queries the materialised triples in Fuseki, and is where ontology domains/ranges are validated directly.

#### The grounding gate

The grounding gate lives in `semantic_mapper/rdf/proof/framing.py::build_framing` (read at lines 184–236). It:

1. Expands PREFIX declarations.
2. Extracts type URIs, property URIs, FILTER texts, and literal-binding URIs.
3. Computes the set of **constrained** property URIs — those bound by a FILTER or to a literal.
4. For each constrained property, calls `_ontology_has_property(uri, ontology_graph)`.

If a property URI is absent from the supplied ontology subgraph it raises **`OntologyGroundingError`** (`semantic_mapper/core/exceptions.py:80`, a `QueryError` subclass):

> "FILTER/constraint references property `<uri>` which is absent from the ontology subgraph. The narrator cannot produce a warranted AppliedConstraint."

This is the proof system refusing to certify a constraint it cannot tie to a real ontology property — a directly auditable consequence of which properties the ontology generator actually minted.

#### Inferring the primary class

`build_framing` also derives the **primary class**:

- From the first type URI, or
- When no `rdf:type` (the predicate stating what class an entity belongs to) is asserted, by following the `rdfs:range` (the type of value a property points at — a class, or a literal type) of a referenced property (lines 229–236).

In other words, the ontology range from the GOLD/ontology stage is used to infer "what kind of thing is being asked about."

### 13.8 The proof composer: assembling the `ProofRecord`

`compose_proof` (`semantic_mapper/prove/composer.py`, lines 24–74) is **pure assembly** — its module docstring stresses it "does not call any LLMs, databases, or external services; all inputs arrive pre-computed." It serialises the phase outputs into a frozen pydantic `ProofRecord` (`semantic_mapper/prove/models.py`).

#### What goes into the `ProofRecord`

- **`interpretation`** — from the narrator's `InterpretationNote`, plus a flag for whether a subgraph was attached.
- **`ontology_subgraph`** — classes (uri/label/comment/`source_table`) and **properties** (uri/label/kind/`source_column`/`source_fk`/`domain_uri`/`range_uri`). This is where the ontology's domain (`rdfs:domain` — the class a property is allowed to be used on) and range, plus the source FIELD/FK that produced each property, are carried into the proof.
- **`lineage`** — serialised `LineageChain`s (see §13.9).
- **`mapping_decisions`** — `MappingDecisionRef`s including `winning_strategy`, `score`, `confirmation`, and the full `evidence_trail`: the audit of *why* a URI was chosen during mapping.
- **`caveats`**, and a hard-coded `limitations=list(REQUIRED_LIMITATIONS)`.

#### Limitations you cannot remove

`REQUIRED_LIMITATIONS` (models.py lines 18–22) are three non-removable strings:

1. "this proof verifies the answer given the query that was executed"
2. "it does not verify that the question was correctly translated"
3. "it does not verify the semantic correctness of the underlying ontology"

A pydantic `model_validator` *raises* if any are missing, so a `ProofRecord` **cannot be constructed** without honestly stating what it does not prove. The same contract is enforced on `RowProofRecord`.

#### Bridging the SQL/SPARQL divide for the UI

`_queried_uris` (lines 180–199) decides which URIs a proof "touched", for UI filtering:

- **In SPARQL mode**, it pulls angle-bracket URIs straight from the query text.
- **In SQL mode** — where URIs never appear — it intersects each decision's `source` column name against the SQL identifiers, the best available proxy for "which mapped properties did this SQL actually touch."

#### Per-row proofs and match links

- `compose_row_proof` builds a per-row `RowProofRecord` (adding `rdf_neighbourhood`, `sparql_funnel`, `sparql_matches`).
- `infer_match_links` (lines 243–281) reads the SPARQL WHERE body via `sparql_funnel._decompose` and reports triple patterns whose subject and object are both bound URI variables as `{from_role, to_role, via_property, via_property_uri}` — i.e. how two entities are joined *through an ontology object property* in the answer.

### 13.9 Lineage walking: gold → silver → bronze (PK/FK at proof time)

`LineageWalker` (`semantic_mapper/prove/lineage_walker.py`) physically re-traverses the layers. It has three entry points matching the three query modes:

- **`walk_uris`** (SPARQL) — parse the id out of a subject URI, then look it up by `id_column`.
- **`walk_gold`** — by `_gold_id` autoincrement.
- **`walk_silver`** — by `_silver_id`, one hop to bronze only.

All lookups are **batched** with `WHERE … IN (…)` using named parameters (`_fetch_by_column`, lines 292–315), never row-by-row. The walker "never raises on broken references; sets `broken=True` with a reason."

#### The FK chain between layers (provenance written upstream, consumed here)

- **gold → silver.** Each gold row carries `_silver_refs`, a JSON map `{silver_table: [silver_ids]}`. `_collect_silver_refs` (lines 317–329) merges these across matched gold rows, and `_fetch_all_silver` batch-loads them.
- **silver → bronze.** Each silver row carries `_bronze_source_ref`, parsed by `_parse_bronze_ref` (lines 504–514) into `(table, column, value)` from the form `raw_admissions.row_id=12345`. `_resolve_bronze` keys the bronze row by that **PK column/value pair** — this is the bronze PRIMARY KEY surfacing at proof time.

> **Edge case — positional fallback.** When a bronze table has no usable named key, the ref uses a positional `_idx` fallback resolved by `SELECT * … LIMIT 1 OFFSET N` (lines 379–401), with the explicit caveat that it is "safe only when the source table is stable (append-only bronze tables)."

#### What gets surfaced (and an honest TODO)

- Audit columns are stripped from the surfaced row values (`_AUDIT_COLS`, `_SILVER_AUDIT_COLS`).
- The gold record records `fields_used` (every non-audit column).
- The walker notes that `TransformationRecord` is an **intentional Phase-4 stub** — silver transform provenance is not yet captured per-field, an honest TODO in the code.

### 13.10 Scope assumptions: what the query did *not* constrain

**In plain terms:** For every property a question *could* have filtered on but didn't, the system records an explicit "all values included" assumption — this is what produces honesty boilerplate like "this count includes all admission types, all genders, …".

`semantic_mapper/rdf/proof/scope.py::build_scope_assumptions` mechanically derives the "all values included" assumptions.

#### Finding the properties on a class

For every queried class it calls `_properties_on_class` (lines 55–79), which collects all `owl:ObjectProperty` (a link from one entity to another) / `owl:DatatypeProperty` (an attribute whose value is a literal, e.g. a number or string) whose `rdfs:domain` is the class or a direct superclass. It adds direct `rdfs:subClassOf` parents only — "no full inference."

#### Expanding `owl:unionOf` so domains don't break

Critically it uses `domain_class_uris` (`semantic_mapper/ontology/owl_utils.py`) to **expand `owl:unionOf`** (an OR over classes):

- A shared abstract property such as `hasPatient` carries its domain as `rdfs:domain [ owl:unionOf (ClassA ClassB …) ]`, so OWL does not AND the members into an unsatisfiable conjunction.
- The helper returns the union members (not the blank node).
- An **empty domain set means "applies to all classes."**

#### Turning unconstrained properties into assumptions

Any such property *not* in `constrained_property_uris` becomes a `ScopeAssumption`:

- `unconstrained_property_uri`
- `property_label`
- `implied_inclusion = "all values of <range_label> included"`
- plus an optional `annotated_default` read from the **`fm:scope_default`** annotation.

This is precisely how the ontology generator's domain/range decisions — and the union-of grouping used for FK-derived shared properties — determine the honesty boilerplate "this count includes all admission types, all genders, …" attached to an answer.

### 13.11 Limitations: OPEN_WORLD, CARDINALITY, ANNOTATED

`semantic_mapper/rdf/proof/limitations.py::build_limitations` emits three mechanical kinds, all grounded in the ontology graph (and degrading to a single generic OPEN_WORLD limitation when no ontology is available):

- **OPEN_WORLD** — one per object property on the queried classes. `_object_properties_on_class` (lines 44–52) again uses `domain_class_uris` to expand `owl:unionOf` (empty domain ⇒ applies to all classes). Each yields *"the absence of a `<prop>` triple on a `<Class>` instance does not mean it has no associated `<range>`,"* with the range label taken from the property's `rdfs:range`. This is where the ontology's object-property domains/ranges turn into explicit open-world caveats.
- **CARDINALITY** — `_cardinality_limitations` walks `rdfs:subClassOf` blank-node `owl:Restriction`s on the class, reads `owl:minCardinality` / `owl:maxCardinality` (the minimum/maximum number of values a property may have) on the `owl:onProperty`, and warns that min-cardinality may cause undercounting and max-cardinality is an assumed upper bound.
- **ANNOTATED** — `_annotated_limitations` reads any **`fm:limitation`** annotation off the queried classes and constrained properties verbatim. These `fm:` annotations are author-supplied provenance/limitation notes minted during ontology generation and faithfully propagated to the proof.

Results are deduplicated by text.

> **Division of labour.** These ontology-derived limitations live in the SPARQL/RDF proof path (`rdf/proof/`), while the `REQUIRED_LIMITATIONS` in §13.8 are the universal, query-mode-independent disclaimers enforced on every `ProofRecord`.

### 13.12 Narration and the grounding guard

#### What the narrator produces

`semantic_mapper/prove/narrator.py::narrate` makes a single LLM call (gated by `Settings.PROVE_NARRATE`; model `PROVE_NARRATOR_MODEL`) to produce an `InterpretationNote`:

- `interpreted_as`
- `scope_assumptions`
- `not_included`
- `interpretation_confidence` (HIGH/MEDIUM/LOW, with a mandatory reason for MEDIUM/LOW)

The system prompt instructs the model to "Assemble — do not infer," generating one scope assumption per property the adjacency notes mark "not filtered."

Like the query engine, narration **never raises** — any LLM/parse failure degrades to a `_degraded_note()` plus a `NARRATION_FAILED` caveat, and `PROVE_NARRATE=False` returns `narrated=False`.

#### The grounding guard (the key integrity mechanism)

`_apply_grounding_guard` (lines 226–259):

1. Builds `_grounding_tokens` from the subgraph properties' local names and labels (length > 2).
2. Checks each LLM-produced `scope_assumption` against those tokens.
3. Any assumption that mentions no real property token is **stripped** and replaced with a `NARRATOR_UNGROUNDED` caveat recording the rejected text.

This is the SQL-path analogue of the SPARQL-path `OntologyGroundingError`: the narrator is not allowed to assert a scope claim that is not anchored to a property the ontology actually declares.

#### The layer's central promise

Together the two guards enforce that every statement in a proof is one of:

- a verbatim value from the materialised data,
- a deterministic walk of the recorded lineage, or
- a claim grounded in the ontology subgraph.

Anything the LLM invents beyond that is demoted to an explicit caveat rather than presented as fact.

### 13.13 How upstream decisions surface here (summary)

- **TABLES** — silver tables are multi-class (distinguished by `_ontology_class_uri`); gold tables are single-class, name-encoded (`gold_patient` ⇒ `Patient`). The rule blocks and schema headers encode this distinction for the LLM.
- **FIELDS/COLUMNS** — gold columns are rendered with their bound ontology property fragment; `_validate_gold_columns` hard-rejects any column the LLM invents; the subgraph carries each property's `source_column`.
- **VALUES** — live-sampled distinct values become categorical hints and `LIKE` fragments; URI-valued columns are flagged as groupable URI categories; non-firing SILVER transforms are caught and flagged.
- **PRIMARY KEYS** — gold entities are assumed keyed by `id` (the FK target); `_silver_id`/`_gold_id` autoincrements key the lineage hops; bronze PKs are recovered from `_bronze_source_ref` (with an `_idx` positional fallback).
- **FOREIGN KEYS** — gold FKs are reconstructed by name into an explicit `JOINS` block and used to ground joins; cross-layer FKs (`_silver_refs`, `_bronze_source_ref`) drive the lineage walker; object-property FKs surface as `infer_match_links`, OPEN_WORLD limitations, and union-expanded scope assumptions.
