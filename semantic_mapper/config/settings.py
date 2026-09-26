# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import os
from typing import Optional
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,  # accept both canonical field names and aliases
    )

    # DB Connectivity
    DATABASE_URL: str = "mysql+pymysql://root:password@localhost:3306/bronze_db"

    # Introspection Settings
    SAMPLE_SIZE: int = 1000
    NULL_RATE_WARNING_THRESHOLD: float = 0.3
    LOW_CARDINALITY_THRESHOLD: int = 20
    # Max ratio of distinct_count / row_count for a column to be considered
    # low-cardinality.  Prevents small ID columns in tiny tables from
    # being mistaken for categoricals (issue #44).
    LOW_CARDINALITY_RATIO: float = 0.05
    # Columns with distinct_count ≤ this threshold AND top_values populated
    # are modelled as owl:Class + owl:NamedIndividual enumerations rather than
    # xsd:string DatatypeProperties (issue #45).
    CATEGORICAL_CLASS_THRESHOLD: int = 10

    # Mapping Thresholds
    AUTO_CONFIRM_THRESHOLD: float = 0.70
    FUZZY_MATCH_THRESHOLD: float = 0.70

    # Aggregation
    # Supported values: "weighted_sum" | "max"
    AGGREGATION_STRATEGY: str = "weighted_sum"
    # Per-strategy weights used in the weighted average.
    # The aggregator uses a per-URI denominator: only the weights of strategies
    # that scored a specific URI are included.  This means a strong single-strategy
    # signal is not diluted by strategies that produced no evidence for that URI.
    # TypeMatcher is weighted low: it scores all type-compatible properties
    # similarly — high coverage, low discrimination.
    # AnnotationMatcher is weighted low: fuzzy label scan, broad not precise.
    # HistoryMatcher bypasses aggregation entirely for HUMAN corrections.
    # FK domain-match boost in NameMatcher.
    # When a FK column's property has rdfs:domain matching the current table's class,
    # this value is added to the re-scored name match (after stripping any For<Class>
    # qualifier suffix from the local name).  Ensures table-qualified subproperties
    # (e.g. hasICUStayForChartEvent) rank above the generic base (hasICUStay) for FK
    # columns in the correct table context.
    DOMAIN_MATCH_BOOST: float = 0.15

    # Score for a case-insensitive (but not verbatim case-exact) annotation match.
    ANNOTATION_NORMALISED_MATCH_SCORE: float = 0.95

    ANNOTATION_MATCHER_WEIGHT: float = 0.20
    NAME_MATCHER_WEIGHT: float = 0.35
    TYPE_MATCHER_WEIGHT: float = 0.25
    SEMANTIC_MATCHER_WEIGHT: float = 0.45

    # If the top NameMatcher score for a URI reaches this threshold, that URI wins
    # immediately (status=CONFIRMED) without entering the weighted-sum calculation.
    # An exact or near-exact lexical match after semantic-prefix stripping means the
    # property was literally named after this column concept — stronger evidence than
    # any combination of weaker signals from other strategies.
    # Set to 0.0 to disable the short-circuit.
    NAME_EXACT_CONFIRM_THRESHOLD: float = 0.97

    # ── LLM backend (vendor-agnostic) ────────────────────────────────────────
    # ForgeMesh talks to any OpenAI-compatible endpoint, so these are named for
    # the *role*, not the vendor. The legacy OPENAI_* names still work as aliases
    # (env vars, project YAML, and constructor kwargs) for backward compatibility.
    LLM_ENABLED: bool = True
    # API key. Local servers (Ollama, vLLM, …) ignore it but the SDK needs a
    # non-empty string — any placeholder works.
    LLM_API_KEY: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY")
    )
    # Default chat model — drives ontology generation, mapping/LLMStrategy, and
    # the proof narrator. For a local backend, name the local model
    # (e.g. "qwen2.5:32b-instruct").
    LLM_MODEL: str = Field(
        default="gpt-4o-mini", validation_alias=AliasChoices("LLM_MODEL", "OPENAI_MODEL")
    )
    # Optional OpenAI-compatible endpoint override. Leave unset for cloud OpenAI
    # (api.openai.com). Point it at a local server to run fully offline /
    # air-gapped — e.g. Ollama: "http://localhost:11434/v1" (also works with
    # vLLM, LM Studio, llama.cpp, etc). Selecting a backend is config-only — no
    # code change. See README "Running fully local".
    LLM_BASE_URL: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_BASE_URL")
    )
    LLM_REQUEST_TIMEOUT: int = 60  # seconds; applies to all LLM API calls
    # Optional context-window size (tokens) for local model servers that accept
    # it. Ollama reads it from options.num_ctx and otherwise defaults to a small
    # 4096, which silently truncates long ontology-generation prompts (dropping
    # the system instructions for wide tables). None = don't send; let the server
    # use its own default. Ignored by cloud OpenAI — only set this when
    # LLM_BASE_URL points at a local server.
    LLM_NUM_CTX: Optional[int] = None
    # Stream chat completions and reassemble the message client-side. Default
    # True: a long single-shot generation on a remote model can exceed the
    # ~100-120s read timeout of a fronting proxy (e.g. RunPod's Cloudflare layer
    # returns HTTP 524) even when LLM_REQUEST_TIMEOUT is higher — the proxy cuts
    # the connection before the origin finishes. Streaming keeps bytes flowing so
    # the proxy never sees a stall. Set False to force a single blocking response.
    LLM_STREAM: bool = True

    # ── Backward-compat read aliases ─────────────────────────────────────────
    # Existing code reads settings.OPENAI_MODEL / OPENAI_API_KEY / OPENAI_BASE_URL.
    # Keep those working as read-only views over the canonical LLM_* fields so no
    # call site has to change. Tests that *set* s.OPENAI_MODEL on a real Settings
    # instance go through the setters below.
    @property
    def OPENAI_API_KEY(self) -> Optional[str]:
        return self.LLM_API_KEY

    @OPENAI_API_KEY.setter
    def OPENAI_API_KEY(self, value: Optional[str]) -> None:
        self.LLM_API_KEY = value

    @property
    def OPENAI_MODEL(self) -> str:
        return self.LLM_MODEL

    @OPENAI_MODEL.setter
    def OPENAI_MODEL(self, value: str) -> None:
        self.LLM_MODEL = value

    @property
    def OPENAI_BASE_URL(self) -> Optional[str]:
        return self.LLM_BASE_URL

    @OPENAI_BASE_URL.setter
    def OPENAI_BASE_URL(self, value: Optional[str]) -> None:
        self.LLM_BASE_URL = value
    # LLMStrategy is the last resort — only called when all cheaper strategies
    # failed to reach the threshold.  Its weight is intentionally much higher
    # than the structural strategies so that a confident LLM result dominates.
    LLM_MATCHER_WEIGHT: float = 2.0

    # File Paths
    ONTOLOGY_PATH: str = "ontology.owl"
    OUTPUT_PATH: str = "semantic_mapping_report.json"
    CORRECTIONS_DB_PATH: str = "corrections.db"
    SEED_OUTPUT_PATH: str = "owl/seed_ontology.owl"

    # Parallelism — number of worker threads for table-parallel pipeline phases.
    # Defaults to physical core count (capped at 12 to avoid MySQL connection exhaustion).
    PIPELINE_WORKERS: int = min(os.cpu_count() or 4, 12)

    # Silver Layer
    SILVER_BATCH_SIZE: int = 500
    SILVER_WRITER: str = "mysql"
    SILVER_DATABASE_URL: str = "mysql+pymysql://root:password@localhost:3306/silver_db"

    # Gold Layer
    GOLD_WRITER: str = "mysql"
    GOLD_DATABASE_URL: str = "mysql+pymysql://root:password@localhost:3306/gold_db"
    GOLD_BATCH_SIZE: int = 1000
    # 1 = Compatibility (keep bronze column names)
    # 2 = Semantic (rename columns to OWL property local names)
    # 3 = Additive (Mode 1 tables + gold_semantic_{entity} views with property names)
    GOLD_MODE: int = 1

    # RDF / Fuseki Layer
    # RDF_BASE_URI is the DATA namespace — it identifies entity instances, e.g.
    #   http://mesh.local/data/patient/abc-123
    # This is distinct from the ontology namespace which identifies classes/properties.
    # RDF_SOURCE_NAME scopes URIs to a source system to avoid collisions in
    # multi-source pipelines: {base_uri}/{source_name}/{entity}/{id} (issue #46).
    # Leave empty for single-source deployments (no scoping applied).
    RDF_SOURCE_NAME: str = ""
    # ONTOLOGY_NAMESPACE names the SCHEMA namespace (classes, properties, and shared
    # value-mapped vocabularies). Unlike the data namespace it is deliberately
    # source/year-agnostic so datasets in one family (e.g. winterolympics 2026, 2030)
    # share a single schema + conformed vocabulary and aggregate across years. A bare
    # token becomes http://mesh.local/ontology/{token}# ; a full http(s) IRI is used
    # verbatim. Empty = derive from DATABASE_URL (strips the _bronze storage token).
    ONTOLOGY_NAMESPACE: str = ""
    FUSEKI_URL: str = "http://localhost:3030"
    FUSEKI_ADMIN_USER: str = "admin"
    FUSEKI_ADMIN_PASSWORD: str = "admin"
    RDF_DATASET_NAME: str = "forgemesh"
    RDF_NAMED_GRAPH: str = "http://mesh.local/graph/current"
    ONTOLOGY_NAMED_GRAPH: str = "http://mesh.local/graph/ontology"
    RDF_BASE_URI: str = "http://mesh.local/data/"
    RDF_BATCH_SIZE: int = 1000
    RDF_WRITER: str = "fuseki"
    # Post-materialisation dangling-object-link gate: "fail" (raise, default),
    # "warn" (log only), or "off" (skip). Union-scoped over data + ontology graphs.
    RDF_DANGLING_GATE: str = "fail"

    # Query Interface
    QUERY_MAX_ROWS: int = 100
    QUERY_PROVIDER: str = "openai"  # "openai" or "anthropic"
    QUERY_MODEL: str = "gpt-4o"
    QUERY_ANTHROPIC_MODEL: str = "claude-haiku-4-5-20251001"
    ANTHROPIC_API_KEY: Optional[str] = None
    QUERY_MAPPING_REPORT_PATH: str = "output/mapping_report.json"
    # Emergency escape hatch — free-text notes appended to the LLM system prompt.
    # This must be empty for any well-modelled dataset. The SPARQL context builder
    # derives join patterns, object-property types, and subtype guidance automatically
    # from the OWL ontology. If you think you need this setting, first check:
    #   1. Are ObjectProperty/DatatypeProperty distinctions declared in the ontology?
    #   2. Does the context builder detect the pattern? If not, extend it generically.
    # See CONTRIBUTING.md — "No Dataset-Specific Knowledge" principle.
    QUERY_DATASET_NOTES: str = ""

    # MCP server — agent-side query surface
    # Set False to disable the deprecated NL-to-query tool (query_forgemesh).
    # When False, OPENAI_API_KEY is not required for the MCP server.
    MCP_LEGACY_QUERY_TOOL: bool = True
    # Ontology subgraph extraction (Feature 07)
    SUBGRAPH_MAX_HOPS: int = 3
    SUBGRAPH_CACHE_SIZE: int = 50
    # MCP query store (Feature 02)
    # Project (projects/<name>.yaml) the web app and MCP server activate when none is
    # given via --project (web app: and none was saved from the UI). Empty = none.
    DEFAULT_PROJECT: str = "synthea"
    MCP_QUERY_RETENTION_DAYS: int = 7
    MCP_PROOF_SUMMARY_MAX_BYTES: int = 50000
    # URL of the ForgeMesh web_app — used by prove_answer to call /api/query/proof.
    # The webapp defaults to port 5173 (start-forgemesh.sh). If the MCP server runs
    # on 8000 (streamable-http) and this is left at 8000 it will call itself.
    FORGEMESH_WEB_APP_URL: str = "http://127.0.0.1:5173"
    # Timeout in seconds for the /api/query/proof HTTP call.
    # Lineage walking samples up to PROVE_SAMPLE_SIZE subjects across three
    # databases — 120s is a safe default for a local deployment.
    FORGEMESH_PROOF_TIMEOUT: int = 120

    # Answer envelope / receipt (FP-1)
    # Public base for receipt_url permalinks. Empty = fall back to
    # FORGEMESH_WEB_APP_URL. Set to the externally-reachable app URL in prod.
    RECEIPT_BASE_URL: str = ""
    # Rows shown inline in the receipt's evidence sample.
    ENVELOPE_SAMPLE_ROWS: int = 3
    # Aggregate groups smaller than this trip the small_group caveat detector.
    ENVELOPE_SMALL_GROUP_THRESHOLD: int = 20
    # Page size for GET /runs/{id}/rows.
    ENVELOPE_ROWS_PAGE_SIZE: int = 100

    # Prove-It — lineage proof settings
    PROVE_MAX_SUBJECTS: int = 1000
    PROVE_SAMPLE_SIZE: int = 200
    PROVE_NARRATE: bool = False
    PROVE_NARRATOR_MODEL: str = "gpt-4o-mini"

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "runtime/logs"
    LOG_FILE_MAX_BYTES: int = 10_000_000
    LOG_FILE_BACKUP_COUNT: int = 5
    LOG_TO_STDOUT: bool = True

    # Iterative Ontology Generator (RIGOR-inspired)
    # ONTOLOGY_GENERATOR selects which generator is used: "full" | "iterative"
    ONTOLOGY_GENERATOR: str = "full"
    # Per-pass model overrides for the iterative generator. When unset, both fall
    # back to LLM_MODEL — so a single OPENAI_MODEL/LLM_MODEL drives everything,
    # but you can run a large model for generation and a cheaper one for the
    # (more frequent) judge pass. None = "use LLM_MODEL".
    ONTOLOGY_GEN_MODEL: Optional[str] = None
    ONTOLOGY_JUDGE_MODEL: Optional[str] = None
    ONTOLOGY_GEN_MAX_RETRIES: int = 2
    ONTOLOGY_GEN_TOP_K_CLASSES: int = 10
    ONTOLOGY_GEN_TEMPERATURE: float = 0.1
    ONTOLOGY_GEN_JUDGE_TEMPERATURE: float = 0.0
    # Judge must return confidence ≥ this floor to be treated as approved.
    # After max_retries, the fragment is merged regardless.
    ONTOLOGY_JUDGE_CONFIDENCE_FLOOR: float = 0.6
    # Domain-specific column name expansions used by gap-fill property naming.
    # Maps bare column name (lowercase) → underscore-separated expansion.
    # Override in .env as JSON: ONTOLOGY_COL_WORD_SPLITS='{"admittime":"admit_time"}'
    ONTOLOGY_COL_WORD_SPLITS: dict = {}
    # Tables with row_count <= this are skipped by the generator (0 = skip empty only).
    ONTOLOGY_MIN_ROWS_FOR_GENERATION: int = 0
    # owl:oneOf enumeration limits for value-mapped vocabularies.
    ONTOLOGY_MAX_ROWS_FOR_ENUMERATION: int = 1000
    ONTOLOGY_MAX_DISTINCT_FOR_ENUMERATION: int = 20
    ONTOLOGY_MAX_DISTINCT_FOR_OPEN_CLASS: int = 100
    # Embedding model shared by the generator's class index and the semantic matcher.
    SEMANTIC_MATCHER_MODEL: str = "all-MiniLM-L6-v2"

    # ── External Terminology Binding (SNOMED/ICD/LOINC/FHIR/OMOP) ─────────────
    # Attach value-mapped individuals (and, where confident, their owning class/
    # property) to authoritative external concepts via skos:exactMatch, with
    # provenance. Detection keys ONLY on value-set overlap — never on column
    # names or dataset identity. See CONTRIBUTING.md "No Dataset-Specific Knowledge".
    #
    # Master switch. False = the binder pass is a no-op; the pipeline behaves
    # exactly as before (no detection, no annotations, no sidecar). Enabled
    # per-project in .env (the Synthea loop turns it on); never a code branch.
    TERMINOLOGY_BINDING_ENABLED: bool = False
    # Pluggable terminology sources as reference data: source_id → adapter config.
    # NOT dataset-specific — each entry names a code system + how to load its
    # code/label set and mint concept IRIs. Adding a code system is adding an
    # entry (and, if novel, an adapter class in ontology/terminology/sources/),
    # never editing pipeline logic. Override in .env as JSON:
    #   TERMINOLOGY_SOURCES='{"snomedct":{"adapter":"offline_codeset",
    #     "system_label":"SNOMED CT","path":"ref/snomedct.tsv",
    #     "iri_template":"http://snomed.info/id/{code}",
    #     "system_iri":"http://snomed.info/sct"}}'
    TERMINOLOGY_SOURCES: dict = {}
    # A coded column binds to a source when the fraction of its distinct non-null
    # values that are valid codes/labels in that source ≥ this overlap threshold.
    # 0.80 matches the existing value-overlap FK inference threshold (Strategy 3b).
    TERM_BIND_OVERLAP_THRESHOLD: float = 0.80
    # Below this per-binding confidence nothing is asserted as skos:exactMatch;
    # sub-floor candidates go to the review sidecar only. The number is
    # calibration-pending (KNOWN-PENDING) — the gate asserts the floor is
    # HONOURED, not that this particular value is correct.
    TERM_BIND_CONFIDENCE_FLOOR: float = 0.50
    # SQLite store for confirmed/proposed terminology bindings — the binding
    # analogue of CORRECTIONS_DB_PATH. Confirmed bindings persist and are reused
    # on re-materialise at $0 (warm-run property; mirrors HistoryMatcher).
    BINDINGS_DB_PATH: str = "bindings.db"

