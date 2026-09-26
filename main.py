# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Entry point for the Semantic Mapper pipeline.

Phase 0 — seed:     analyses bronze schema via LLM → generates starter OWL ontology
Phase 1 — inspect:  profiles all bronze tables → output/inspection_report.json
Phase 2 — map:      loads inspection report + ontology → output/mapping_report.json
Phase 3 — silver:   reads bronze rows, transforms, writes to silver_db
Phase 4 — gold:     reads silver, materialises per-class gold tables
Phase 5 — rdf:      reads gold, emits RDF triples to Fuseki
Phase 6 — validate: runs C1–C7 validation suite against the named graph
Phase 7 — purge:    drops silver/gold tables and Fuseki named graph (destructive)

Usage:
  python main.py --project synthea                     # all default phases for a project
  python main.py --project mydb --phases inspect,seed  # generate a starter ontology only
  python main.py --project synthea --phases validate   # validation suite against live Fuseki
  python main.py --env .env.local --project synthea    # extra env file (e.g. local LLM)
  python main.py --project synthea --phases purge  # purge Synthea layers only
"""
import argparse
import json
import logging
from pathlib import Path

from sqlalchemy import create_engine

from semantic_mapper.config.settings import Settings
from semantic_mapper.config.logging_config import setup_logging
from semantic_mapper.db.inspector import MySQLInspector
from semantic_mapper.db.models import TableProfile
from semantic_mapper.ontology.loader import OntologyLoader
from semantic_mapper.engine.mapper import MapperOrchestrator
from semantic_mapper.schema.mapping_output import SemanticMappingReport
from semantic_mapper.silver.pipeline import SilverPipeline
from semantic_mapper.silver.writer.factory import get_writer
from semantic_mapper.gold.builder import (
    GoldDefinitionBuilder,
    derive_fk_join_candidates,
    refine_id_columns,
)
from semantic_mapper.gold.pipeline import GoldPipeline
from semantic_mapper.gold.writer.factory import get_gold_writer
from semantic_mapper.rdf.pipeline import RdfPipeline
from semantic_mapper.rdf.writer.factory import get_rdf_writer
from semantic_mapper.ontology.enricher import enrich_ontology_from_value_mappings


INSPECTION_REPORT = Path("output/inspection_report.json")
_PIPELINE_STATS_DEFAULT = Path("output/pipeline_stats.json")


def _pipeline_stats_path() -> Path:
    """Return the project-scoped pipeline_stats file path.

    When FORGEMESH_PROJECT is set (via --project), each project writes and reads
    its own file (e.g. output/pipeline_stats_synthea.json) so that a validate-only
    run for one project never reads a stale run_id left by another project's rdf phase.
    Falls back to the shared path when no project is active (legacy / bare invocation).
    """
    import os as _os_local
    project = _os_local.environ.get("FORGEMESH_PROJECT", "")
    if project:
        return Path(f"output/pipeline_stats_{project}.json")
    return _PIPELINE_STATS_DEFAULT

# OpenAI pricing per 1M tokens (input, output) — update as prices change.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o":            (2.50,  10.00),
    "gpt-4o-mini":       (0.15,   0.60),
    "gpt-4.1":           (2.00,   8.00),
    "gpt-4.1-mini":      (0.40,   1.60),
    "gpt-4.1-nano":      (0.10,   0.40),
    "o3":                (10.00,  40.00),
    "o3-mini":           (1.10,   4.40),
    "o4-mini":           (1.10,   4.40),
}


def _persist_ontology_path(env_file: str, new_path: str) -> None:
    """Rewrite the ONTOLOGY_PATH= line in an env file to point at a new ontology.

    Called after ontology generation so the next run automatically picks up the
    freshly generated file without manual editing.
    """
    import re as _re
    try:
        with open(env_file) as f:
            content = f.read()
        updated = _re.sub(
            r"^ONTOLOGY_PATH=.*$",
            f"ONTOLOGY_PATH={new_path}",
            content,
            flags=_re.MULTILINE,
        )
        if updated == content:
            # No existing ONTOLOGY_PATH line — append one.
            updated = content.rstrip("\n") + f"\nONTOLOGY_PATH={new_path}\n"
        with open(env_file, "w") as f:
            f.write(updated)
        logging.getLogger(__name__).info(
            "Env file updated: ONTOLOGY_PATH=%s in %s", new_path, env_file
        )
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Could not update ONTOLOGY_PATH in %s: %s", env_file, exc
        )


def _preflight_echo(config: "ProjectConfig") -> None:  # noqa: F821
    """Print the resolved project configuration before any pipeline work begins.

    This is the first visible output on every run so the operator can confirm,
    in line one, exactly which database, ontology, dataset and graph will be used.
    The run has already been validated by resolve_project(); this is purely informational.
    """
    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  PROJECT : {config.name}")
    if config.description:
        print(f"  {config.description}")
    print(f"{sep}")
    print(f"  Bronze DB    : {config.bronze_database_url}")
    print(f"  Silver DB    : {config.silver_database_url}")
    print(f"  Gold DB      : {config.gold_database_url}")
    print(f"  Ontology     : {config.ontology_path}")
    print(f"  RDF dataset  : {config.rdf_dataset_name}")
    print(f"  Named graph  : {config.named_graph_uri}")
    print(f"  URI scope    : http://mesh.local/data/{config.rdf_source_name}/...")
    print(f"{sep}\n")


def _apply_docker_host_override() -> None:
    """Rewrite MySQL DATABASE_URL host:port when FORGEMESH_DB_HOST is set.

    Inside Docker, env files carry localhost:PORT which is unreachable from
    within a container. docker-compose injects FORGEMESH_DB_HOST=db and
    FORGEMESH_DB_PORT=3306 so the pipeline resolves to the compose service name.
    Called after load_dotenv — these two vars are never in dotenv files, so
    load_dotenv(override=True) cannot clobber them.
    """
    import os as _os
    import re as _re
    host = _os.environ.get("FORGEMESH_DB_HOST")
    port = _os.environ.get("FORGEMESH_DB_PORT", "3306")
    if not host:
        return
    for key in ("DATABASE_URL", "SILVER_DATABASE_URL", "GOLD_DATABASE_URL"):
        val = _os.environ.get(key)
        if val and "@" in val:
            _os.environ[key] = _re.sub(r"@[^@/]+/", f"@{host}:{port}/", val)
    logging.getLogger(__name__).info(
        "Docker host override applied: DB → %s:%s", host, port
    )


def _coherence_overrides_path(settings: Settings) -> Path:
    """Derive coherence resolution overrides path from the mapping report path.
    e.g. output/mapping_report_synthea.json → output/coherence_overrides_synthea.json
    """
    report_path = Path(settings.QUERY_MAPPING_REPORT_PATH)
    stem = report_path.stem.replace("mapping_report", "coherence_overrides")
    return report_path.parent / f"{stem}.json"


def _value_mappings_sidecar_path(settings: Settings) -> Path:
    """Derive value_mappings sidecar path from the mapping report path.
    e.g. output/mapping_report_mimic.json → output/value_mappings_mimic.json
    """
    report_path = Path(settings.QUERY_MAPPING_REPORT_PATH)
    stem = report_path.stem.replace("mapping_report", "value_mappings")
    return report_path.parent / f"{stem}.json"


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return estimated USD cost for a given model and token counts."""
    # Sort longest prefix first so "gpt-4o-mini" is checked before "gpt-4o".
    # Without this, "gpt-4o-mini".startswith("gpt-4o") matches the wrong entry.
    for name, (input_price, output_price) in sorted(
        _MODEL_PRICING.items(), key=lambda x: -len(x[0])
    ):
        if model.startswith(name):
            return (
                prompt_tokens    / 1_000_000 * input_price
                + completion_tokens / 1_000_000 * output_price
            )
    # Unknown model — assume gpt-4o pricing as a safe over-estimate.
    return (prompt_tokens / 1_000_000 * 2.50 + completion_tokens / 1_000_000 * 10.00)


def _update_pipeline_stats(phase: str, data: dict) -> None:
    """Persist per-phase stats to the project-scoped pipeline_stats file (issue #50)."""
    path = _pipeline_stats_path()
    existing: dict = {}
    if path.exists():
        with open(path) as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                pass
    existing[phase] = data
    path.parent.mkdir(exist_ok=True)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, default=str)


def _patch_collisions_in_report(report_path: Path, collisions: list) -> None:
    """Patch mapping report on disk: set collision losers to PENDING_REVIEW.

    Called after GoldDefinitionBuilder.build() when self.collisions is non-empty.
    Each collision record has:
      silver_table    — e.g. "silver_raw_chartevents"
      source_column   — e.g. "value"
      property_uri    — the shared URI that caused the collision
      winner_column   — source column that retained the property URI
      loser_confidence / winner_confidence

    The losing column's status is set to "pending" and its rationale is updated
    to cite the collision.  On the next map run the LLM can assign a distinct URI.
    """
    if not collisions:
        return
    if not report_path.exists():
        return
    logger = logging.getLogger(__name__)
    with open(report_path) as f:
        raw: dict = json.load(f)

    patched = 0
    for coll in collisions:
        silver_table: str = coll["silver_table"]
        source_table = silver_table[len("silver_"):] if silver_table.startswith("silver_") else silver_table
        source_column: str = coll["source_column"]
        prop_uri: str = coll["property_uri"]
        winner: str = coll["winner_column"]

        for tbl in raw.get("tables", []):
            if tbl.get("source_table") != source_table:
                continue
            for col in tbl.get("column_mappings", []):
                if col.get("source_column") != source_column:
                    continue
                if col.get("status") != "confirmed":
                    continue
                col["status"] = "pending"
                existing_rationale = col.get("rationale", "")
                col["rationale"] = (
                    f"[C2 COLLISION — PENDING_REVIEW] "
                    f"Column '{source_column}' and '{winner}' both mapped to "
                    f"<{prop_uri}>. '{winner}' (higher confidence) retained the predicate. "
                    f"Assign a distinct mapped_property_uri to re-enable triple emission. "
                    f"Previous rationale: {existing_rationale}"
                )
                patched += 1
                break

    if patched:
        with open(report_path, "w") as f:
            json.dump(raw, f, indent=2)
        logger.warning(
            "C2: patched %d collision loser(s) to PENDING_REVIEW in %s",
            patched, report_path,
        )


def _patch_coherence_violations_in_report(
    report_path: Path,
    collisions: list,  # List[CoherenceIssue] with category SOURCE_COLUMN_COLLISION
) -> int:
    """Patch mapping report on disk: set SOURCE_COLUMN_COLLISION columns to PENDING_REVIEW.

    Each collision's subject_uri is 'source_table.source_column'.  The matching
    PropertyMapping is set to status='pending' so GoldDefinitionBuilder skips it
    and no triple is emitted until the literal-vs-FK ambiguity is resolved.

    Returns the number of columns patched.
    """
    if not collisions:
        return 0
    if not report_path.exists():
        return 0
    logger = logging.getLogger(__name__)
    with open(report_path) as f:
        raw: dict = json.load(f)

    patched = 0
    for issue in collisions:
        subject = issue.subject_uri or ""
        if "." not in subject:
            continue
        source_table, source_column = subject.split(".", 1)
        source_table = source_table.strip()
        source_column = source_column.strip()

        for tbl in raw.get("tables", []):
            if tbl.get("source_table") != source_table:
                continue
            for col in tbl.get("column_mappings", []):
                if col.get("source_column") != source_column:
                    continue
                if col.get("status") != "confirmed":
                    continue
                col["status"] = "pending"
                col["rationale"] = (
                    f"[COHERENCE — PENDING_REVIEW] "
                    f"SOURCE_COLUMN_COLLISION: '{source_table}.{source_column}' is "
                    f"annotated as both a foreign key (ObjectProperty, fm:sourceForeignKey) "
                    f"and a scalar (DatatypeProperty, fm:sourceColumn). "
                    f"Cannot resolve automatically; assign one property URI manually "
                    f"(Mappings tab in the web app, or edit the ontology)."
                )
                patched += 1
                break

    if patched:
        with open(report_path, "w") as f:
            json.dump(raw, f, indent=2)
        logger.warning(
            "Coherence: patched %d SOURCE_COLUMN_COLLISION column(s) to PENDING_REVIEW in %s",
            patched, report_path,
        )
    return patched


def run_seed(settings: Settings) -> None:
    logger = logging.getLogger(__name__)
    from semantic_mapper.ontology.seed_generator import SeedGenerator

    if not INSPECTION_REPORT.exists():
        raise FileNotFoundError(
            f"Inspection report not found at {INSPECTION_REPORT}. Run the 'inspect' phase first."
        )
    logger.info("Loading inspection report from %s", INSPECTION_REPORT)
    with open(INSPECTION_REPORT) as f:
        raw = json.load(f)
    profiles = [TableProfile.model_validate(t) for t in raw]
    logger.info("Loaded %d table profiles", len(profiles))

    logger.info("Model: %s", settings.OPENAI_MODEL)
    generator = SeedGenerator(settings)
    out_path = generator.generate(profiles)

    cost = _estimate_cost(settings.OPENAI_MODEL, generator.prompt_tokens, generator.completion_tokens)
    sep = "=" * 64
    print(f"\n{sep}\nSEED ONTOLOGY SUMMARY\n{sep}")
    print(f"  Tables analysed    : {len(profiles)}")
    print(f"  Output written to  : {out_path}")
    print(f"  Model              : {settings.OPENAI_MODEL}")
    print(f"  Prompt tokens      : {generator.prompt_tokens:,}")
    print(f"  Completion tokens  : {generator.completion_tokens:,}")
    print(f"  Estimated cost     : ${cost:.4f} USD")
    settings.ONTOLOGY_PATH = str(out_path)
    print(f"\n  Next steps:")
    print(f"    1. Open {out_path} in Protégé to review and refine")
    print(f"    2. Run: python main.py --phases map  (ONTOLOGY_PATH already updated)")
    print(f"{sep}\n")
    return {"phase": "seed", "model": settings.OPENAI_MODEL,
            "prompt_tokens": generator.prompt_tokens,
            "completion_tokens": generator.completion_tokens, "cost_usd": cost,
            "ontology_path": str(out_path)}


def run_full(settings: Settings) -> None:
    logger = logging.getLogger(__name__)

    if not INSPECTION_REPORT.exists():
        raise FileNotFoundError(
            f"Inspection report not found at {INSPECTION_REPORT}. Run the 'inspect' phase first."
        )
    logger.info("Loading inspection report from %s", INSPECTION_REPORT)
    with open(INSPECTION_REPORT) as f:
        raw = json.load(f)
    profiles = [TableProfile.model_validate(t) for t in raw]
    logger.info("Loaded %d table profiles", len(profiles))

    if settings.ONTOLOGY_GENERATOR == "iterative":
        logger.info("Using IterativeOntologyGenerator (RIGOR-inspired)")
        # Unset gen/judge models fall back to LLM_MODEL inside the generator;
        # resolve them here so the log shows what will actually be called.
        _default_model = getattr(settings, "LLM_MODEL", None) or settings.OPENAI_MODEL
        model_label = settings.ONTOLOGY_GEN_MODEL or _default_model
        judge_label = settings.ONTOLOGY_JUDGE_MODEL or _default_model
        logger.info("Model (gen): %s  |  Model (judge): %s", model_label, judge_label)
        from semantic_mapper.ontology.iterative import IterativeOntologyGenerator
        generator = IterativeOntologyGenerator(settings)
    else:
        from semantic_mapper.ontology.full_generator import FullOntologyGenerator
        generator = FullOntologyGenerator(settings)
        model_label = settings.OPENAI_MODEL
        logger.info("Model: %s", model_label)

    out_path = generator.generate(profiles)

    # Propagate the freshly generated ontology to subsequent phases in this
    # process so map/gold/rdf all use the new file without a separate --env edit.
    settings.ONTOLOGY_PATH = str(out_path)
    logger.info("ONTOLOGY_PATH updated to %s for subsequent phases", out_path)

    cost = _estimate_cost(model_label, generator.prompt_tokens, generator.completion_tokens)
    sep = "=" * 64
    print(f"\n{sep}\nFULL ONTOLOGY SUMMARY\n{sep}")
    print(f"  Tables analysed    : {len(profiles)}")
    print(f"  Output written to  : {out_path}")
    print(f"  Model              : {model_label}")
    print(f"  Prompt tokens      : {generator.prompt_tokens:,}")
    print(f"  Completion tokens  : {generator.completion_tokens:,}")
    print(f"  Estimated cost     : ${cost:.4f} USD")
    print(f"{sep}\n")
    return {"phase": "full", "model": model_label,
            "prompt_tokens": generator.prompt_tokens,
            "completion_tokens": generator.completion_tokens, "cost_usd": cost,
            "ontology_path": str(out_path)}


def run_inspect(settings: Settings) -> None:
    logger = logging.getLogger(__name__)
    db_host = settings.DATABASE_URL.split("@")[-1]
    logger.info(f"Connecting to: {db_host}")

    inspector = MySQLInspector(settings)
    logger.info("Profiling all tables...")
    profiles = inspector.profile_all()

    INSPECTION_REPORT.parent.mkdir(exist_ok=True)
    with open(INSPECTION_REPORT, "w") as f:
        json.dump([p.model_dump() for p in profiles], f, indent=2, default=str)

    logger.info(f"Inspection report written to {INSPECTION_REPORT}")

    total_columns    = sum(len(p.columns) for p in profiles)
    high_null_cols   = []
    multi_fmt_cols   = []
    needs_review     = []

    for profile in profiles:
        for col_name, col in profile.columns.items():
            ref = f"{profile.table_name}.{col_name}"
            if col.null_rate > settings.NULL_RATE_WARNING_THRESHOLD:
                high_null_cols.append(f"{ref}  (null_rate={col.null_rate:.1%})")
            if len(col.detected_formats) > 1:
                multi_fmt_cols.append(f"{ref}  formats={col.detected_formats}")
            critical = [w for w in col.quality_warnings if w.severity == "CRITICAL"]
            if critical:
                needs_review.append(f"{ref}  [{', '.join(w.category for w in critical)}]")

    sep = "=" * 64
    print(f"\n{sep}\nINSPECTION SUMMARY\n{sep}")
    print(f"  Tables profiled        : {len(profiles)}")
    print(f"  Total columns          : {total_columns}")
    print(f"  High null rate (>{settings.NULL_RATE_WARNING_THRESHOLD:.0%})  : {len(high_null_cols)}")
    print(f"  Multi-format columns   : {len(multi_fmt_cols)}")
    print(f"  CRITICAL flags         : {len(needs_review)}")
    if high_null_cols:
        print(f"\n  HIGH NULL RATE:")
        for c in high_null_cols:
            print(f"    • {c}")
    if multi_fmt_cols:
        print(f"\n  MULTI-FORMAT DETECTED:")
        for c in multi_fmt_cols:
            print(f"    • {c}")
    if needs_review:
        print(f"\n  NEEDS HUMAN REVIEW (CRITICAL):")
        for c in needs_review:
            print(f"    • {c}")
    print(f"{sep}\n  Full report → {INSPECTION_REPORT}\n")


def run_map(settings: Settings) -> None:
    logger = logging.getLogger(__name__)

    # --- Load profiles from inspection report (read-only) ---
    if not INSPECTION_REPORT.exists():
        raise FileNotFoundError(
            f"Inspection report not found at {INSPECTION_REPORT}. Run phase 1 first."
        )
    logger.info(f"Loading inspection report from {INSPECTION_REPORT}")
    with open(INSPECTION_REPORT) as f:
        raw = json.load(f)
    profiles = [TableProfile.model_validate(t) for t in raw]
    logger.info(f"Loaded {len(profiles)} table profiles")

    # --- Load ontology ---
    if not settings.ONTOLOGY_PATH:
        raise FileNotFoundError(
            "ONTOLOGY_PATH is not set. The 'map' phase needs an ontology to map "
            "the inspected schema onto. Generate one first: "
            "python main.py --env <your .env file> --phases seed,full "
            "(then re-run with --phases map, or omit --phases to run the rest)."
        )
    logger.info(f"Loading ontology from {settings.ONTOLOGY_PATH}")
    graph = OntologyLoader.load(settings.ONTOLOGY_PATH)
    logger.info(f"Ontology loaded — {len(graph)} triples")

    # --- Run mapper ---
    logger.info("Model: %s", settings.OPENAI_MODEL)
    logger.info("Running mapping engine...")
    orchestrator = MapperOrchestrator(
        profiles, settings, graph,
        ontology_source=settings.ONTOLOGY_PATH,
        db_source="MySQL_Bronze",
    )
    report = orchestrator.run()

    # --- Write report ---
    Path(settings.QUERY_MAPPING_REPORT_PATH).parent.mkdir(exist_ok=True)
    with open(Path(settings.QUERY_MAPPING_REPORT_PATH), "w") as f:
        json.dump(report.model_dump(), f, indent=2, default=str)
    logger.info(f"Mapping report written to {Path(settings.QUERY_MAPPING_REPORT_PATH)}")

    # --- Persist report to pipeline_runs DB (when launched via web_app) ---
    # NOTE: when invoked via web_app.py, this UPDATE is effectively dead code.
    # The pipeline_runs row does not exist yet at this point — web_app.py inserts
    # it only after proc.wait() returns (i.e. after this process has already
    # exited), so the UPDATE silently affects 0 rows.  web_app.py reads the
    # report JSON directly from disk in _run_pipeline_thread and stores it in
    # the same INSERT that creates the row.  This block is kept for direct CLI
    # invocations of main.py where the caller may pre-insert the row.
    import os as _os
    _run_id = _os.environ.get("PIPELINE_RUN_ID")
    if _run_id:
        from semantic_mapper.db.run_store import write_mapping_report
        write_mapping_report(_run_id, report, settings)
        logger.info("Mapping report persisted to pipeline_runs DB (run_id=%s)", _run_id)

    # --- Write value_mappings sidecar ---
    # Extracts all LLM-generated value_mappings into a small, human-editable JSON
    # alongside the main report.  Edits to this file take priority over the report
    # on subsequent gold runs — no need to touch the 20MB report file.
    sidecar: dict = {}
    for tm in report.tables:
        for col in tm.column_mappings:
            if col.value_mappings:
                key = f"{tm.source_table}.{col.source_column}"
                sidecar[key] = [
                    {"source_value": vm.source_value, "mapped_uri": vm.mapped_uri, "rationale": vm.rationale}
                    for vm in col.value_mappings
                ]
    sidecar_path = _value_mappings_sidecar_path(settings)
    with open(sidecar_path, "w") as f:
        json.dump(sidecar, f, indent=2, default=str)
    logger.info(f"Value mappings sidecar written to {sidecar_path}")

    # --- Enrich ontology with derived class URIs from sidecar ---
    if sidecar and settings.ONTOLOGY_PATH:
        n_added, new_uris = enrich_ontology_from_value_mappings(
            sidecar, report, graph, settings.ONTOLOGY_PATH
        )
        if n_added:
            print(f"\n  ONTOLOGY ENRICHMENT:")
            print(f"    New classes added : {n_added}")
            for uri in new_uris[:20]:
                print(f"    + {uri}")
            if len(new_uris) > 20:
                print(f"    ... and {len(new_uris) - 20} more")

    # --- Summary counts ---
    all_mappings = [col for t in report.tables for col in t.column_mappings]
    confirmed     = [m for m in all_mappings if m.status.value == "confirmed"]
    pending       = [m for m in all_mappings if m.status.value == "pending"]
    unmapped      = [m for m in all_mappings if m.status.value == "unmapped"]

    # Split confirmed by whether LLM evidence contributed
    def _has_llm(m) -> bool:
        return any(ev.strategy == "LLMStrategy" for ev in m.evidence_trail)

    confirmed_tier12 = [m for m in confirmed if not _has_llm(m)]
    confirmed_tier3  = [m for m in confirmed if _has_llm(m)]

    # Split confirmed by whether HistoryMatcher (corrections store) contributed
    def _from_store(m) -> bool:
        return any(ev.strategy == "HistoryMatcher" for ev in m.evidence_trail)

    confirmed_via_store = [m for m in confirmed if _from_store(m)]

    sep = "=" * 64
    print(f"\n{sep}\nMAPPING SUMMARY\n{sep}")
    print(f"  Total columns mapped        : {len(all_mappings)}")
    print(f"  CONFIRMED (Tier 1+2 only)   : {len(confirmed_tier12)}")
    print(f"  CONFIRMED (Tier 3 LLM lift) : {len(confirmed_tier3)}")
    print(f"  PENDING REVIEW              : {len(pending)}")
    print(f"  UNMAPPED                    : {len(unmapped)}")
    print(f"  Threshold used              : {settings.AUTO_CONFIRM_THRESHOLD:.0%}")
    print(f"\n  CORRECTIONS STORE:")
    print(f"    Prior mappings loaded     : {orchestrator.corrections_loaded}")
    print(f"    Confirmed via store       : {len(confirmed_via_store)}")
    print(f"    Mappings written to store : {orchestrator.corrections_written}")

    if confirmed_tier12:
        print(f"\n  CONFIRMED (Tier 1+2):")
        for m in confirmed_tier12:
            local = m.mapped_property_uri.split("#")[-1] if m.mapped_property_uri else "—"
            print(f"    • {m.source_column:<25} → #{local:<22}  [{m.confidence_score:.2f}]")

    if confirmed_tier3:
        print(f"\n  CONFIRMED (Tier 3 LLM):")
        for m in confirmed_tier3:
            local = m.mapped_property_uri.split("#")[-1] if m.mapped_property_uri else "—"
            llm_ev = next((e for e in m.evidence_trail if e.strategy == "LLMStrategy"), None)
            print(f"    • {m.source_column:<25} → #{local:<22}  [{m.confidence_score:.2f}]")
            if llm_ev:
                print(f"      LLM: {llm_ev.rationale}")

    # --- LLM telemetry ---
    map_cost = 0.0
    if orchestrator.llm_api_calls > 0:
        map_cost = _estimate_cost(
            settings.OPENAI_MODEL,
            orchestrator.llm_prompt_tokens,
            orchestrator.llm_completion_tokens,
        )
        print(f"\n  LLM TIER 3 TELEMETRY:")
        print(f"    Model              : {settings.OPENAI_MODEL}")
        print(f"    API calls made     : {orchestrator.llm_api_calls}")
        print(f"    Prompt tokens      : {orchestrator.llm_prompt_tokens:,}")
        print(f"    Completion tokens  : {orchestrator.llm_completion_tokens:,}")
        print(f"    Estimated cost     : ${map_cost:.4f} USD")
        if orchestrator.llm_hallucinated_uris:
            print(f"    Hallucinated URIs rejected ({len(orchestrator.llm_hallucinated_uris)}):")
            for uri in orchestrator.llm_hallucinated_uris:
                print(f"      ✗ {uri}")
        else:
            print(f"    Hallucinated URIs  : 0")

    if pending:
        print(f"\n  REMAINING PENDING ({len(pending)}):")
        for m in pending:
            local = m.mapped_property_uri.split("#")[-1] if m.mapped_property_uri else "—"
            print(f"    • {m.source_column:<25} → #{local:<22}  [{m.confidence_score:.2f}]")

    print(f"\n{sep}\n  Full report → {Path(settings.QUERY_MAPPING_REPORT_PATH)}\n")
    return {"phase": "map", "model": settings.OPENAI_MODEL,
            "prompt_tokens": orchestrator.llm_prompt_tokens,
            "completion_tokens": orchestrator.llm_completion_tokens, "cost_usd": map_cost}


def run_silver(settings: Settings) -> None:
    logger = logging.getLogger(__name__)

    if not Path(settings.QUERY_MAPPING_REPORT_PATH).exists():
        raise FileNotFoundError(
            f"Mapping report not found at {Path(settings.QUERY_MAPPING_REPORT_PATH)}. Run phases 1 and 2 first."
        )
    logger.info(f"Loading mapping report from {Path(settings.QUERY_MAPPING_REPORT_PATH)}")
    with open(Path(settings.QUERY_MAPPING_REPORT_PATH)) as f:
        report = SemanticMappingReport.model_validate(json.load(f))

    _pool_kw = dict(pool_size=settings.PIPELINE_WORKERS + 2, max_overflow=settings.PIPELINE_WORKERS)
    bronze_engine = create_engine(
        settings.DATABASE_URL,
        execution_options={"isolation_level": "READ UNCOMMITTED"},
        **_pool_kw,
    )
    silver_engine = create_engine(settings.SILVER_DATABASE_URL, **_pool_kw)
    writer = get_writer(settings, engine=silver_engine)

    # Load the ontology so the silver layer can tell a genuine subtype discriminator
    # (targets are owl:Class subclasses of the entity) from a value-mapped categorical
    # property (targets are vocabulary individuals, e.g. gender → MaleGender).
    ontology_graph = None
    if settings.ONTOLOGY_PATH:
        try:
            ontology_graph = OntologyLoader.load(settings.ONTOLOGY_PATH)
        except Exception as exc:
            logger.warning("could not load ontology for discriminator gating: %s", exc)

    logger.info(f"Running silver pipeline (writer={settings.SILVER_WRITER})...")
    pipeline = SilverPipeline(
        report=report,
        bronze_engine=bronze_engine,
        writer=writer,
        settings=settings,
        ontology_graph=ontology_graph,
    )
    stats = pipeline.run()

    _update_pipeline_stats("silver", {
        "run_id": stats.run_id,
        "bronze_rows_read": stats.bronze_rows_read,
        "silver_rows_written": stats.silver_rows_written,
        "quarantine_rows_written": stats.quarantine_rows_written,
        "null_preserved_count": stats.null_preserved_count,
        "transformation_errors": stats.transformation_errors,
        "duration_seconds": stats.duration_seconds,
        "finished_at": stats.finished_at.isoformat() if stats.finished_at else None,
    })

    sep = "=" * 64
    print(f"\n{sep}\nSILVER SUMMARY\n{sep}")
    print(f"  Run ID                  : {stats.run_id}")
    print(f"  Bronze rows read        : {stats.bronze_rows_read}")
    print(f"  Silver rows written     : {stats.silver_rows_written}")
    print(f"  Quarantine rows written : {stats.quarantine_rows_written}")
    print(f"  NULL preserved          : {stats.null_preserved_count}")
    print(f"  Transformation errors   : {stats.transformation_errors}")
    print(f"  Duration                : {stats.duration_seconds:.2f}s")
    print(f"  Target                  : {settings.SILVER_DATABASE_URL.split('@')[-1]}")
    print(f"{sep}\n")


def run_gold(settings: Settings) -> None:
    logger = logging.getLogger(__name__)

    if not Path(settings.QUERY_MAPPING_REPORT_PATH).exists():
        raise FileNotFoundError(
            f"Mapping report not found at {Path(settings.QUERY_MAPPING_REPORT_PATH)}. Run phases 1 and 2 first."
        )
    logger.info(f"Loading mapping report from {Path(settings.QUERY_MAPPING_REPORT_PATH)}")
    with open(Path(settings.QUERY_MAPPING_REPORT_PATH)) as f:
        report = SemanticMappingReport.model_validate(json.load(f))

    ontology_graph = None
    if settings.ONTOLOGY_PATH:
        try:
            ontology_graph = OntologyLoader.load(settings.ONTOLOGY_PATH)
        except Exception as exc:
            logger.warning("could not load ontology for FK resolution: %s", exc)

    if ontology_graph is not None:
        from semantic_mapper.ontology.coherence import (
            OntologyCoherenceChecker, load_coherence_overrides, apply_coherence_overrides,
        )
        overrides = load_coherence_overrides(_coherence_overrides_path(settings))
        if overrides:
            n_stripped = apply_coherence_overrides(ontology_graph, overrides)
            logger.info(
                "Applied %d coherence resolution override(s) (%d annotation(s) stripped)",
                len(overrides), n_stripped,
            )
        coherence = OntologyCoherenceChecker(ontology_graph).check()
        col_collisions = [
            i for i in coherence.issues if i.category == "SOURCE_COLUMN_COLLISION"
        ]
        if col_collisions:
            for issue in col_collisions:
                logger.warning("Ontology coherence violation: %s", issue.message)
            patched = _patch_coherence_violations_in_report(
                Path(settings.QUERY_MAPPING_REPORT_PATH), col_collisions
            )
            if patched:
                with open(Path(settings.QUERY_MAPPING_REPORT_PATH)) as f:
                    report = SemanticMappingReport.model_validate(json.load(f))

    # Load value_mappings sidecar if present — overrides report value_mappings
    value_map_overrides: dict = {}
    sidecar_path = _value_mappings_sidecar_path(settings)
    if sidecar_path.exists():
        with open(sidecar_path) as f:
            value_map_overrides = json.load(f)
        logger.info(f"Value mappings sidecar loaded: {len(value_map_overrides)} column overrides from {sidecar_path}")

    builder = GoldDefinitionBuilder()
    definitions = builder.build(
        report,
        ontology_graph=ontology_graph,
        gold_mode=settings.GOLD_MODE,
        value_map_overrides=value_map_overrides,
    )
    if builder.collisions:
        logger.warning(
            "C2: %d property-URI collision(s) resolved during gold build — "
            "patching mapping report with PENDING_REVIEW status.",
            len(builder.collisions),
        )
        _patch_collisions_in_report(
            Path(settings.QUERY_MAPPING_REPORT_PATH), builder.collisions
        )
    logger.info(f"Built {len(definitions)} gold table definition(s) (mode={settings.GOLD_MODE})")

    _pool_kw = dict(pool_size=settings.PIPELINE_WORKERS + 2, max_overflow=settings.PIPELINE_WORKERS)
    silver_engine = create_engine(settings.SILVER_DATABASE_URL, **_pool_kw)
    gold_engine = create_engine(settings.GOLD_DATABASE_URL, **_pool_kw)
    writer = get_gold_writer(settings, engine=gold_engine)

    logger.info(f"Running gold pipeline (writer={settings.GOLD_WRITER})...")
    pipeline = GoldPipeline(
        definitions=definitions,
        silver_engine=silver_engine,
        writer=writer,
        settings=settings,
    )
    stats = pipeline.run()

    _update_pipeline_stats("gold", {
        "run_id": stats.run_id,
        "total_rows_written": stats.total_rows_written,
        "tables_written": stats.tables_written,
        "rows_per_table": stats.rows_per_table,
        "duration_seconds": stats.duration_seconds,
        "finished_at": stats.finished_at.isoformat() if stats.finished_at else None,
    })

    sep = "=" * 64
    print(f"\n{sep}\nGOLD SUMMARY\n{sep}")
    print(f"  Run ID                  : {stats.run_id}")
    print(f"  Tables written          : {len(stats.tables_written)}")
    for tbl in stats.tables_written:
        print(f"    • {tbl:<40}  {stats.rows_per_table[tbl]} rows")
    print(f"  Total rows written      : {stats.total_rows_written}")
    print(f"  Duration                : {stats.duration_seconds:.2f}s")
    print(f"  Target                  : {settings.GOLD_DATABASE_URL.split('@')[-1]}")
    print(f"{sep}\n")


def run_rdf(settings: Settings) -> None:
    logger = logging.getLogger(__name__)

    if not Path(settings.QUERY_MAPPING_REPORT_PATH).exists():
        raise FileNotFoundError(
            f"Mapping report not found at {Path(settings.QUERY_MAPPING_REPORT_PATH)}. "
            "Run phases 1 and 2 first."
        )
    logger.info(f"Loading mapping report from {Path(settings.QUERY_MAPPING_REPORT_PATH)}")
    with open(Path(settings.QUERY_MAPPING_REPORT_PATH)) as f:
        report = SemanticMappingReport.model_validate(json.load(f))

    ontology_graph = None
    if settings.ONTOLOGY_PATH:
        try:
            ontology_graph = OntologyLoader.load(settings.ONTOLOGY_PATH)
        except Exception as exc:
            logger.warning("could not load ontology for FK resolution: %s", exc)

    builder = GoldDefinitionBuilder()
    definitions = builder.build(report, ontology_graph=ontology_graph)
    if builder.collisions:
        logger.warning(
            "C2: %d property-URI collision(s) resolved during RDF build — "
            "patching mapping report with PENDING_REVIEW status.",
            len(builder.collisions),
        )
        _patch_collisions_in_report(
            Path(settings.QUERY_MAPPING_REPORT_PATH), builder.collisions
        )
    logger.info(f"Built {len(definitions)} gold table definition(s) for RDF")

    gold_engine = create_engine(
        settings.GOLD_DATABASE_URL,
        pool_size=settings.PIPELINE_WORKERS + 2,
        max_overflow=settings.PIPELINE_WORKERS,
    )
    # Tables with a detected PK trust their PK-derived id_column (subject + FK both key
    # off the PK); refine must not override it with a name heuristic.
    _pk_source_tables = {tm.source_table for tm in report.tables if tm.primary_key_columns}
    # FK join keys: an entity referenced by FKs must key its subject URI on the join
    # column the FKs carry (admissions.hadm_id), not a surrogate PK (row_id) — else
    # every incoming object link dangles. Derived from the ontology (per-project),
    # applied with a uniqueness check inside refine_id_columns.
    _fk_join_cols = derive_fk_join_candidates(definitions, ontology_graph)
    definitions = refine_id_columns(
        definitions, gold_engine, _pk_source_tables, _fk_join_cols
    )
    writer = get_rdf_writer(settings)

    import os as _os
    project_name = _os.environ.get("FORGEMESH_PROJECT", "")
    logger.info(f"Running RDF pipeline (writer={settings.RDF_WRITER})...")
    pipeline = RdfPipeline(
        definitions=definitions,
        gold_engine=gold_engine,
        writer=writer,
        settings=settings,
    )
    stats = pipeline.run(project_name=project_name or None)

    _update_pipeline_stats("rdf", {
        "run_id": stats.run_id,
        "triples_written": stats.triples_written,
        "tables_processed": stats.tables_processed,
        "named_graph": stats.named_graph,
        "duration_seconds": stats.duration_seconds,
        "finished_at": stats.finished_at.isoformat() if stats.finished_at else None,
    })

    sep = "=" * 64
    print(f"\n{sep}\nRDF SUMMARY\n{sep}")
    print(f"  Run ID                  : {stats.run_id}")
    print(f"  Tables processed        : {len(stats.tables_processed)}")
    for tbl in stats.tables_processed:
        print(f"    • {tbl}")
    print(f"  Triples written         : {stats.triples_written}")
    print(f"  Named graph             : {stats.named_graph}")
    print(f"  Duration                : {stats.duration_seconds:.2f}s")
    print(f"  Target                  : {settings.FUSEKI_URL}")
    print(f"{sep}\n")

    # Post-materialisation gate: every object link must resolve to a typed subject
    # (union-scoped over data + ontology graphs). A dangling target silently breaks
    # all multi-hop joins, so a non-zero count fails the phase (RDF_DANGLING_GATE).
    if settings.RDF_WRITER == "fuseki":
        from semantic_mapper.rdf.dangling_gate import run_dangling_gate
        run_dangling_gate(settings, stats.named_graph)


def run_purge(settings: Settings) -> None:
    """Purge phase — drop all silver and gold tables for the active project,
    then delete the project's named graph from Fuseki.

    This is a destructive, one-way operation.  Run it only when you want a
    clean re-build from bronze (e.g. before testing a code change).  The
    bronze tables and mapping report are NOT touched.

    Usage:
        python main.py --project openflights --phases purge
    """
    import os as _os
    logger = logging.getLogger(__name__)
    sep = "=" * 64

    # ── 1. Drop silver tables ──────────────────────────────────────────────
    try:
        from sqlalchemy import inspect as _inspect, text as _text
        silver_engine = create_engine(settings.SILVER_DATABASE_URL)
        insp = _inspect(silver_engine)
        silver_tables = [t for t in insp.get_table_names() if t.startswith("silver_")]
        if silver_tables:
            with silver_engine.begin() as conn:
                conn.execute(_text("SET FOREIGN_KEY_CHECKS = 0"))
                for tbl in silver_tables:
                    conn.execute(_text(f"DROP TABLE IF EXISTS `{tbl}`"))
                    logger.info("Dropped silver table: %s", tbl)
                conn.execute(_text("SET FOREIGN_KEY_CHECKS = 1"))
            print(f"  Silver  : dropped {len(silver_tables)} table(s) from {settings.SILVER_DATABASE_URL}")
        else:
            print(f"  Silver  : no silver_* tables found (already clean)")
    except Exception as exc:
        logger.warning("Could not purge silver DB: %s", exc)
        print(f"  Silver  : SKIPPED — {exc}")

    # ── 2. Drop gold tables ────────────────────────────────────────────────
    try:
        gold_engine = create_engine(settings.GOLD_DATABASE_URL)
        insp = _inspect(gold_engine)
        gold_tables = [t for t in insp.get_table_names() if t.startswith("gold_")]
        if gold_tables:
            with gold_engine.begin() as conn:
                conn.execute(_text("SET FOREIGN_KEY_CHECKS = 0"))
                for tbl in gold_tables:
                    conn.execute(_text(f"DROP TABLE IF EXISTS `{tbl}`"))
                    logger.info("Dropped gold table: %s", tbl)
                conn.execute(_text("SET FOREIGN_KEY_CHECKS = 1"))
            print(f"  Gold    : dropped {len(gold_tables)} table(s) from {settings.GOLD_DATABASE_URL}")
        else:
            print(f"  Gold    : no gold_* tables found (already clean)")
    except Exception as exc:
        logger.warning("Could not purge gold DB: %s", exc)
        print(f"  Gold    : SKIPPED — {exc}")

    # ── 3. Drop Fuseki named graphs ────────────────────────────────────────
    # Use the HTTP Graph Store DELETE endpoint (/{dataset}/data?graph=<uri>).
    # For small graphs Fuseki completes quickly; for large graphs (MIMIC-scale,
    # millions of triples) the operation can take several minutes.  The timeout
    # is set to 600 s (10 min) to avoid spurious failures on large datasets.
    # A SPARQL UPDATE DROP fallback handles non-standard Fuseki configurations.
    import requests as _req
    fuseki_auth = (settings.FUSEKI_ADMIN_USER, settings.FUSEKI_ADMIN_PASSWORD)
    base_url = settings.FUSEKI_URL.rstrip("/")
    data_endpoint = f"{base_url}/{settings.RDF_DATASET_NAME}/data"

    def _drop_graph(uri: str, label: str) -> None:
        try:
            resp = _req.delete(
                data_endpoint,
                params={"graph": uri},
                auth=fuseki_auth,
                timeout=600,
            )
            if resp.status_code in (200, 204):
                print(f"  Fuseki  : dropped {label} <{uri}>")
            elif resp.status_code == 404:
                print(f"  Fuseki  : {label} not found (already clean)")
            else:
                # Fall back to SPARQL UPDATE for servers that don't support
                # HTTP Graph Store DELETE (unlikely for Fuseki but defensive)
                update_ep = f"{base_url}/{settings.RDF_DATASET_NAME}/update"
                resp2 = _req.post(
                    update_ep,
                    data={"update": f"DROP SILENT GRAPH <{uri}>"},
                    auth=fuseki_auth,
                    timeout=300,
                )
                if resp2.status_code in (200, 204):
                    print(f"  Fuseki  : dropped {label} <{uri}> (via SPARQL UPDATE)")
                else:
                    print(f"  Fuseki  : {label} — HTTP {resp2.status_code}: {resp2.text[:120]}")
        except Exception as exc:
            logger.warning("Could not drop Fuseki graph %s: %s", uri, exc)
            print(f"  Fuseki  : {label} SKIPPED — {exc}")

    _drop_graph(settings.RDF_NAMED_GRAPH, "named graph")
    if settings.ONTOLOGY_NAMED_GRAPH:
        _drop_graph(settings.ONTOLOGY_NAMED_GRAPH, "ontology graph")

    print(f"\n{sep}")
    print(f"  Purge complete. Re-run silver, gold, rdf to rebuild.")
    print(f"{sep}\n")


def run_validate(settings: Settings) -> None:
    """Phase 6 — run the C1–C7 validation suite against the named graph."""
    import os as _os
    logger = logging.getLogger(__name__)

    # ── Load mapping report ────────────────────────────────────────────────
    report_path = Path(settings.QUERY_MAPPING_REPORT_PATH)
    if not report_path.exists():
        raise FileNotFoundError(
            f"Mapping report not found at {report_path}. Run phases 1 and 2 first."
        )
    logger.info("Loading mapping report from %s", report_path)
    with open(report_path) as f:
        report = SemanticMappingReport.model_validate(json.load(f))

    # ── Load ontology ──────────────────────────────────────────────────────
    ontology_graph = None
    if settings.ONTOLOGY_PATH:
        try:
            ontology_graph = OntologyLoader.load(settings.ONTOLOGY_PATH)
            logger.info("Loaded ontology from %s", settings.ONTOLOGY_PATH)
        except Exception as exc:
            logger.warning("Could not load ontology for validation: %s", exc)

    # ── Gold engine ────────────────────────────────────────────────────────
    gold_engine = create_engine(settings.GOLD_DATABASE_URL)

    # ── SPARQL backend ─────────────────────────────────────────────────────
    from semantic_mapper.validation.sparql_backend import FusekiBackend
    sparql = FusekiBackend(settings.FUSEKI_URL, settings.RDF_DATASET_NAME)
    graph_uri = settings.RDF_NAMED_GRAPH

    # ── Baseline run ID (most recent successful rdf phase) ─────────────────
    _stats_path = _pipeline_stats_path()
    baseline_run_id: str = ""
    if _stats_path.exists():
        try:
            with open(_stats_path) as f:
                stats = json.load(f)
            baseline_run_id = stats.get("rdf", {}).get("run_id", "")
        except Exception:
            pass
    if not baseline_run_id:
        logger.warning(
            "No baseline run_id found in %s — C6 load-stamp check will likely fail",
            _stats_path,
        )

    project_name = _os.environ.get("FORGEMESH_PROJECT", "")

    # ── Build ValidationContext ────────────────────────────────────────────
    from semantic_mapper.validation import ValidationContext, ValidationRunner

    ctx = ValidationContext(
        project_name=project_name,
        graph_uri=graph_uri,
        sparql=sparql,
        gold_engine=gold_engine,
        mapping_report=report,
        ontology=ontology_graph,
        baseline_run_id=baseline_run_id,
    )

    # ── Run checks ─────────────────────────────────────────────────────────
    logger.info("Running validation suite against graph <%s>...", graph_uri)
    runner = ValidationRunner()
    val_report = runner.run(ctx)

    # ── Print results ──────────────────────────────────────────────────────
    sep = "=" * 64
    print(f"\n{sep}")
    print(val_report.summary())
    print(sep)

    # Persist to pipeline_stats
    _update_pipeline_stats("validate", {
        "project": project_name,
        "graph_uri": graph_uri,
        "baseline_run_id": baseline_run_id,
        "passed": val_report.n_passed,
        "failed": val_report.n_failed,
        "all_passed": val_report.all_passed,
        "run_at": val_report.run_at.isoformat(),
        "checks": [
            {"id": c.check_id, "name": c.check_name, "passed": c.passed, "message": c.message}
            for c in val_report.checks
        ],
    })

    if not val_report.all_passed:
        raise SystemExit(f"Validation failed: {val_report.n_failed} check(s) did not pass.")


def main():
    parser = argparse.ArgumentParser(description="Semantic Mapper pipeline")
    parser.add_argument(
        "--project",
        default=None,
        metavar="NAME",
        help=(
            "Project name (e.g. mimic, openflights, synthea). "
            "Reads projects/<name>.yaml and sets all dataset-specific config. "
            "Wins over shell environment and --env."
        ),
    )
    parser.add_argument(
        "--env",
        default=None,
        metavar="FILE",
        help="Path to .env file (e.g. .env.local). With --project, the project's dataset settings override it.",
    )
    parser.add_argument(
        "--clearcorrections",
        action="store_true",
        help="Clear all entries from the corrections store, then exit.",
    )
    parser.add_argument(
        "--phases",
        default="all",
        metavar="PHASES",
        help="Comma-separated phases to run: inspect,map,silver,gold,rdf (default: all)",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Override LLM model for generation and mapping (e.g. gpt-4o, gpt-4o-mini).",
    )
    args = parser.parse_args()

    _active_project = None
    if args.env:
        # Load first so --project (applied below) still wins for dataset keys,
        # while non-dataset settings (LLM backend, thresholds) come from the file.
        from dotenv import load_dotenv
        load_dotenv(args.env, override=True)
    if args.project:
        from semantic_mapper.project.resolver import resolve_project, apply_project_to_env, OntologyMissingWarning
        try:
            _active_project = resolve_project(args.project)
        except OntologyMissingWarning as _omw:
            import logging as _log
            _log.warning("Project '%s': %s — continuing without ontology.", args.project, _omw)
            _active_project = _omw.partial_config
        apply_project_to_env(_active_project)

    _apply_docker_host_override()
    settings = Settings()

    if _active_project:
        _preflight_echo(_active_project)
    setup_logging(settings)
    if args.model:
        settings.ONTOLOGY_GEN_MODEL = args.model
        settings.OPENAI_MODEL = args.model

    if args.clearcorrections:
        from semantic_mapper.db.corrections_store import CorrectionsStore
        store = CorrectionsStore(settings.CORRECTIONS_DB_PATH)
        store.clear()
        print(f"Corrections store cleared: {settings.CORRECTIONS_DB_PATH}")
        return

    _PHASE_MAP = {
        "seed":     run_seed,
        "full":     run_full,
        "inspect":  run_inspect,
        "map":      run_map,
        "silver":   run_silver,
        "gold":     run_gold,
        "rdf":      run_rdf,
        "validate": run_validate,
        "purge":    run_purge,
    }
    # "seed" and "validate" are intentionally excluded from the default run.
    # "seed" is only run when explicitly requested.
    # "validate" is run after confirming a good RDF load, not as part of every run.
    _DEFAULT_PHASES = ["inspect", "map", "silver", "gold", "rdf"]
    phases = (
        _DEFAULT_PHASES
        if args.phases == "all"
        else [p.strip() for p in args.phases.split(",")]
    )
    llm_phases: list[dict] = []
    for phase in phases:
        if phase not in _PHASE_MAP:
            print(f"Unknown phase '{phase}'. Valid phases: {', '.join(_PHASE_MAP)}")
            raise SystemExit(1)
        result = _PHASE_MAP[phase](settings)
        if result and "cost_usd" in result:
            llm_phases.append(result)
        # When full/seed generates a new ontology, persist it as the live selection
        # in the env file so subsequent runs automatically use it.
        if result and "ontology_path" in result and args.env:
            _persist_ontology_path(args.env, result["ontology_path"])

    if llm_phases:
        total_prompt     = sum(p["prompt_tokens"] for p in llm_phases)
        total_completion = sum(p["completion_tokens"] for p in llm_phases)
        total_cost       = sum(p["cost_usd"] for p in llm_phases)
        sep = "=" * 64
        print(f"\n{sep}\nTOTAL LLM COST SUMMARY\n{sep}")
        for p in llm_phases:
            print(f"  {p['phase']:<8}  {p['model']:<20}  "
                  f"{p['prompt_tokens']:>8,} in  {p['completion_tokens']:>7,} out  "
                  f"${p['cost_usd']:.4f}")
        if len(llm_phases) > 1:
            print(f"  {'':─<60}")
            print(f"  {'TOTAL':<8}  {'':20}  "
                  f"{total_prompt:>8,} in  {total_completion:>7,} out  "
                  f"${total_cost:.4f}")
        print(f"{sep}\n")


if __name__ == "__main__":
    main()
