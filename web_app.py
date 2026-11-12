# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""ForgeMesh — local web app.

Serves a single-page query interface backed by SparqlQueryEngine.

Usage:
    python web_app.py                    # uses .env
    python web_app.py --env .env.synthea
    python web_app.py --env .env.synthea --port 8080
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ── Parse args early so Settings gets the right env file ─────────────────────
_parser = argparse.ArgumentParser(description="ForgeMesh web app")
_parser.add_argument("--env",  default=os.environ.get("FORGEMESH_ENV"), metavar="FILE")
_parser.add_argument("--project", default=None, metavar="NAME",
                     help="Activate projects/<NAME>.yaml on startup (e.g. synthea). "
                          "Default: last project selected in the UI, else DEFAULT_PROJECT (synthea)")
_parser.add_argument("--port", default=5173, type=int)
_parser.add_argument("--host", default="127.0.0.1")
_args, _ = _parser.parse_known_args()

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from semantic_mapper.config.logging_config import setup_logging
from semantic_mapper.config.settings import Settings
from semantic_mapper.db.corrections_store import CorrectionsStore
from semantic_mapper.gold.builder import GoldDefinitionBuilder, refine_id_columns
from semantic_mapper.rdf.query.engine import SparqlQueryEngine
from semantic_mapper.schema.mapping_output import (
    MappingStatus, SemanticMappingReport, compute_coverage,
)

# ── Bootstrap engine ──────────────────────────────────────────────────────────
settings = Settings(_env_file=_args.env) if _args.env else Settings()
# The app calls its own /api/query/proof to build full receipts and links to its
# own /runs pages. Unless FORGEMESH_WEB_APP_URL was set explicitly, point it at the
# host and port this process is actually serving on, not the 5173 default.
from semantic_mapper.config.settings import apply_web_app_url
apply_web_app_url(settings, _args.host, _args.port)
setup_logging(settings)

def _empty_report() -> SemanticMappingReport:
    """An empty placeholder report so the app can boot before any pipeline run.

    The webapp must start on a fresh checkout with no generated artefacts. When
    the mapping report is absent we serve an empty report; the query interface
    has nothing to query yet, but the UI loads and the pipeline can be driven
    from it. `_reinit_engine()` swaps in the real report once it exists.
    """
    return SemanticMappingReport(
        ontology_uri=settings.ONTOLOGY_PATH or "",
        database_name="",
        settings_used={},
        tables=[],
        total_pending_review=0,
    )


def _read_report(path: Path) -> SemanticMappingReport:
    """Load a mapping report from disk, or return an empty report if absent."""
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            return SemanticMappingReport.model_validate(json.load(fh))
    logging.warning(
        "Mapping report not found at %s — starting with an empty report. "
        "Run the mapping pipeline to populate the query interface.",
        path,
    )
    return _empty_report()


report = _read_report(Path(settings.QUERY_MAPPING_REPORT_PATH))

# ── Catalog / multi-ontology state ────────────────────────────────────────────
_catalog_lock = threading.Lock()
_catalog_entries: List[Dict[str, Any]] = []   # {filename, logical_uri, enabled}
_catalog_graphs: Dict[str, Any] = {}          # filename -> rdflib.Graph (not JSON-serialised)
_ontology_sources: Dict[str, str] = {}        # URI str -> source filename
_catalog_path: Optional[Path] = None          # path to catalog XML, if applicable
_catalog_state_path: Optional[Path] = None    # sidecar JSON for toggle persistence
ontology_graph: Optional[Any] = None          # unified rdflib.Graph
_active_ontology_path: str = ""               # currently loaded path; overrides settings.ONTOLOGY_PATH in status
_active_database: str = ""                    # user-selected DB name; empty = use DATABASE_URL as-is
_active_project: str = ""                     # active project name (e.g. "mimic"); "" = no project selected
_active_write_target: str = ""               # filename (within catalog dir) that suggestions/edits write to
_active_model: str = settings.QUERY_MODEL       # OpenAI model for pipeline (ontology generation)
_active_query_model: str = settings.QUERY_MODEL  # query model — may be OpenAI or Claude

# ── Persistent app state (DB selection, ontology, token totals, pipeline state) ─
_APP_STATE_PATH = Path(settings.CORRECTIONS_DB_PATH).parent / "app_state.json"
_base_prompt_tokens: int = 0
_base_completion_tokens: int = 0
_mapping_threshold: float = settings.AUTO_CONFIRM_THRESHOLD

# Pipeline lifecycle timestamps (issue #47)
_last_map_at: Optional[str] = None
_last_correction_at: Optional[str] = None
_last_silver_at: Optional[str] = None
_last_gold_at: Optional[str] = None
_last_rdf_at: Optional[str] = None


def _apply_active_project_from_config(config) -> None:
    """Apply an already-resolved ProjectConfig to the webapp's in-process settings.

    Used when resolve_project raised OntologyMissingWarning and the caller already
    holds the partial config — avoids a second resolve call that would raise again.
    """
    from semantic_mapper.project.resolver import apply_project_to_env
    import re as _re

    apply_project_to_env(config)
    host = os.environ.get("FORGEMESH_DB_HOST")
    port = os.environ.get("FORGEMESH_DB_PORT", "3306")
    if host:
        for key in ("DATABASE_URL", "SILVER_DATABASE_URL", "GOLD_DATABASE_URL"):
            val = os.environ.get(key)
            if val and "@" in val:
                os.environ[key] = _re.sub(r"@[^@/]+/", f"@{host}:{port}/", val)

    settings.DATABASE_URL               = os.environ["DATABASE_URL"]
    settings.SILVER_DATABASE_URL        = os.environ["SILVER_DATABASE_URL"]
    settings.GOLD_DATABASE_URL          = os.environ["GOLD_DATABASE_URL"]
    settings.ONTOLOGY_PATH              = config.ontology_path
    settings.RDF_DATASET_NAME           = config.rdf_dataset_name
    settings.RDF_NAMED_GRAPH            = config.named_graph_uri
    # RDF_SOURCE_NAME scopes entity URIs ({base}/{source}/{entity}/{id}). Without
    # patching it here, the lineage walker parses the source segment as the entity
    # name and can never resolve a gold table, so graph→gold→silver→bronze tracing
    # silently yields no chains (the receipt falls back to graph-only sources).
    settings.RDF_SOURCE_NAME            = config.rdf_source_name
    if config.ontology_named_graph_uri:
        settings.ONTOLOGY_NAMED_GRAPH   = config.ontology_named_graph_uri
    settings.QUERY_MAPPING_REPORT_PATH  = os.environ["QUERY_MAPPING_REPORT_PATH"]
    global _mapping_report_path
    _mapping_report_path = Path(settings.QUERY_MAPPING_REPORT_PATH)


def _apply_active_project(name: str) -> None:
    """Apply a project's resolved config to the webapp's in-process settings.

    Called on project selection and on startup when a project is restored from
    app_state.json.  Fails loudly (raises) if the project file is invalid so
    the caller can surface the error rather than running with bad config.
    """
    from semantic_mapper.project.resolver import resolve_project, apply_project_to_env
    import re as _re

    config = resolve_project(name)          # validates file exists + ontology exists
    apply_project_to_env(config)            # writes to os.environ

    # Apply FORGEMESH_DB_HOST rewrite for Docker (same as CLI path)
    host = os.environ.get("FORGEMESH_DB_HOST")
    port = os.environ.get("FORGEMESH_DB_PORT", "3306")
    if host:
        for key in ("DATABASE_URL", "SILVER_DATABASE_URL", "GOLD_DATABASE_URL"):
            val = os.environ.get(key)
            if val and "@" in val:
                os.environ[key] = _re.sub(r"@[^@/]+/", f"@{host}:{port}/", val)

    # Patch the live settings object so this process uses the project values
    settings.DATABASE_URL               = os.environ["DATABASE_URL"]
    settings.SILVER_DATABASE_URL        = os.environ["SILVER_DATABASE_URL"]
    settings.GOLD_DATABASE_URL          = os.environ["GOLD_DATABASE_URL"]
    settings.ONTOLOGY_PATH              = config.ontology_path
    settings.RDF_DATASET_NAME           = config.rdf_dataset_name
    settings.RDF_NAMED_GRAPH            = config.named_graph_uri
    # RDF_SOURCE_NAME scopes entity URIs ({base}/{source}/{entity}/{id}). Without
    # patching it here, the lineage walker parses the source segment as the entity
    # name and can never resolve a gold table, so graph→gold→silver→bronze tracing
    # silently yields no chains (the receipt falls back to graph-only sources).
    settings.RDF_SOURCE_NAME            = config.rdf_source_name
    if config.ontology_named_graph_uri:
        settings.ONTOLOGY_NAMED_GRAPH   = config.ontology_named_graph_uri
    settings.QUERY_MAPPING_REPORT_PATH  = os.environ["QUERY_MAPPING_REPORT_PATH"]
    global _mapping_report_path
    _mapping_report_path = Path(settings.QUERY_MAPPING_REPORT_PATH)

    return config


def _load_app_state() -> None:
    """Restore active DB, ontology path, accumulated token counts, and pipeline timestamps."""
    global _active_database, _active_ontology_path, _base_prompt_tokens, _base_completion_tokens
    global _last_map_at, _last_correction_at, _last_silver_at, _last_gold_at, _last_rdf_at
    global _mapping_threshold, _active_model, _active_query_model, _active_project
    if not _APP_STATE_PATH.exists():
        return
    try:
        state = json.loads(_APP_STATE_PATH.read_text(encoding="utf-8"))
        # Restore project first — it overwrites DB/ontology fields below
        saved_project = state.get("active_project", "")
        if saved_project:
            try:
                _apply_active_project(saved_project)
                _active_project = saved_project
            except Exception as exc:
                logging.warning(
                    "Could not restore active project '%s': %s — running without project",
                    saved_project, exc
                )
        _active_database = state.get("active_database", "")
        _active_ontology_path = state.get("active_ontology_path", "")
        _base_prompt_tokens = int(state.get("prompt_tokens", 0))
        _base_completion_tokens = int(state.get("completion_tokens", 0))
        _last_map_at        = state.get("last_map_at")
        _last_correction_at = state.get("last_correction_at")
        _last_silver_at     = state.get("last_silver_at")
        _last_gold_at       = state.get("last_gold_at")
        _last_rdf_at        = state.get("last_rdf_at")
        if "mapping_threshold" in state:
            _mapping_threshold = float(state["mapping_threshold"])
        if "active_model" in state:
            _active_model = state["active_model"]
            settings.QUERY_MODEL = _active_model
        if "active_query_model" in state:
            _active_query_model = state["active_query_model"]
            if _active_query_model.startswith("claude-"):
                settings.QUERY_PROVIDER = "claude_code"
                settings.QUERY_ANTHROPIC_MODEL = _active_query_model
            else:
                settings.QUERY_PROVIDER = "openai"
                settings.QUERY_MODEL = _active_query_model
    except Exception as exc:
        logging.warning("Could not load app state: %s", exc)


def _save_app_state() -> None:
    """Persist active DB, ontology path, accumulated token counts, and pipeline timestamps."""
    try:
        engine_prompt = _engine.prompt_tokens if "_engine" in globals() else 0
        engine_completion = _engine.completion_tokens if "_engine" in globals() else 0
        state = {
            "active_project": _active_project,
            "active_database": _active_database,
            "active_ontology_path": _active_ontology_path,
            "prompt_tokens": _base_prompt_tokens + engine_prompt,
            "completion_tokens": _base_completion_tokens + engine_completion,
            "last_map_at":        _last_map_at,
            "last_correction_at": _last_correction_at,
            "last_silver_at":     _last_silver_at,
            "last_gold_at":       _last_gold_at,
            "last_rdf_at":        _last_rdf_at,
            "mapping_threshold":  _mapping_threshold,
            "active_model":       _active_model,
            "active_query_model": _active_query_model,
        }
        _APP_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception as exc:
        logging.warning("Could not save app state: %s", exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _touch_correction_ts() -> None:
    """Record that a correction was modified and persist state (issue #47)."""
    global _last_correction_at
    _last_correction_at = _now_iso()
    _save_app_state()


def _current_pipeline_state() -> Dict[str, Any]:
    """Derive and return the current pipeline state dict (issue #47)."""
    from semantic_mapper.pipeline_state import derive_state, state_meta
    mapping_report_path = Path(settings.OUTPUT_PATH)
    report_exists = mapping_report_path.exists()
    try:
        corrections_count = len(_corrections_store.get_all()) if "_corrections_store" in globals() else 0
    except Exception:
        corrections_count = 0
    state = derive_state(
        mapping_report_exists=report_exists,
        corrections_count=corrections_count,
        last_map_at=_last_map_at,
        last_correction_at=_last_correction_at,
        last_silver_at=_last_silver_at,
        last_gold_at=_last_gold_at,
        last_rdf_at=_last_rdf_at,
    )
    meta = state_meta(state)
    return {
        "state": state.value,
        "label": meta["label"],
        "level": meta["level"],
        "last_map_at":        _last_map_at,
        "last_correction_at": _last_correction_at,
        "last_silver_at":     _last_silver_at,
        "last_gold_at":       _last_gold_at,
        "last_rdf_at":        _last_rdf_at,
    }


# Restore persisted state before catalog init so the correct ontology is loaded.
_load_app_state()
if _args.project:
    # --project wins over the restored selection, and is persisted so page
    # reloads (which re-run _load_app_state) keep it.
    _cfg = _apply_active_project(_args.project)
    _active_project = _args.project
    _active_database = _cfg.bronze_database_url.rstrip("/").rsplit("/", 1)[-1]
    _active_ontology_path = _cfg.ontology_path
    _save_app_state()
elif not _active_project and settings.DEFAULT_PROJECT:
    # Nothing given or saved: start on the default project. Tolerate a default whose
    # data isn't set up here (e.g. before its pipeline has run) — start without a project.
    try:
        _cfg = _apply_active_project(settings.DEFAULT_PROJECT)
        _active_project = settings.DEFAULT_PROJECT
        _active_database = _cfg.bronze_database_url.rstrip("/").rsplit("/", 1)[-1]
        _active_ontology_path = _cfg.ontology_path
    except Exception as exc:
        logging.warning("Default project '%s' not activated: %s", settings.DEFAULT_PROJECT, exc)


def _current_db_name() -> str:
    """Return the active database name (user-selected or from DATABASE_URL)."""
    if _active_database:
        return _active_database
    url = settings.DATABASE_URL
    return url.rstrip("/").rsplit("/", 1)[-1]


def _override_db_url(db_name: str) -> str:
    """Return DATABASE_URL with the database component replaced by db_name."""
    url = settings.DATABASE_URL
    # Everything up to (but not including) the last path segment
    base = url.rstrip("/").rsplit("/", 1)[0]
    return f"{base}/{db_name}"


def _find_env_file_for_database(db_name: str) -> Optional[str]:
    """Scan .env.* files and return the path of the one whose DATABASE_URL targets db_name.

    Used so that selecting a database in the UI automatically picks the matching
    end-to-end env file (silver/gold/rdf settings, ontology path, etc.) rather
    than only patching DATABASE_URL while leaving the rest pointing at another dataset.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    for env_path in sorted(ROOT.glob(".env.*")):
        vals = dotenv_values(str(env_path))
        db_url = vals.get("DATABASE_URL", "")
        if db_url and db_url.rstrip("/").rsplit("/", 1)[-1] == db_name:
            return str(env_path)
    return None


def _list_available_databases() -> List[str]:
    """Connect to the MySQL server and return user-accessible database names."""
    _SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys"}
    try:
        from sqlalchemy import create_engine, text
        # Connect to information_schema to list all schemas without specifying a target db
        server_url = _override_db_url("information_schema")
        engine = create_engine(server_url, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME")).fetchall()
        engine.dispose()
        return [row[0] for row in rows if row[0].lower() not in _SYSTEM_DBS]
    except Exception as exc:
        logging.warning("Could not list databases: %s", exc)
        return []


def _is_catalog(path: str) -> bool:
    return Path(path).suffix.lower() == ".xml"


def _parse_catalog(catalog_xml: Path) -> List[Dict[str, str]]:
    """Return [{filename, logical_uri}] from a Protégé catalog-v001.xml."""
    entries: List[Dict[str, str]] = []
    try:
        tree = ET.parse(str(catalog_xml))
        root = tree.getroot()
        ns = "urn:oasis:names:tc:entity:xmlns:xml:catalog"
        # Find all <uri> elements regardless of nesting depth
        for el in root.iter(f"{{{ns}}}uri"):
            raw_uri = el.get("uri", "")
            file_attr = urllib.parse.unquote(raw_uri)
            if not Path(file_attr).suffix.lower() in (".owl", ".rdf"):
                continue
            logical = el.get("name", "").removeprefix("duplicate:")
            entries.append({"filename": file_attr, "logical_uri": logical})
    except Exception as exc:
        logging.warning("Could not parse catalog %s: %s", catalog_xml, exc)
    return entries


def _load_catalog_state(state_path: Path, entries: List[Dict]) -> None:
    """Merge persisted enabled flags into entries (in-place). Defaults to all enabled."""
    if state_path.exists():
        try:
            disabled = set(json.loads(state_path.read_text()).get("disabled", []))
            for e in entries:
                e["enabled"] = e["filename"] not in disabled
            return
        except Exception:
            pass
    for e in entries:
        e["enabled"] = True


def _save_catalog_state(state_path: Path) -> None:
    disabled = [e["filename"] for e in _catalog_entries if not e["enabled"]]
    state_path.write_text(json.dumps({"disabled": disabled}))


def _load_owl_file(catalog_dir: Path, filename: str) -> Optional[Any]:
    import rdflib
    fp = catalog_dir / filename
    if not fp.exists():
        logging.warning("OWL file not found: %s", fp)
        return None
    g = rdflib.Graph()
    try:
        g.parse(str(fp), format="xml")
        return g
    except Exception as exc:
        logging.warning("Could not parse %s: %s", filename, exc)
        return None


def _resolve_imports(catalog_dir: Path) -> None:
    """For every loaded graph, follow owl:imports and add missing files to the catalog.

    rdflib does not resolve owl:imports automatically.  When an OWL file like
    OrgHealth.owl declares ``owl:imports OrgCore.owl``, all of its classes live
    in OrgCore.owl and won't appear unless that file is also loaded.  This
    function discovers those references and adds them as enabled catalog entries
    so _rebuild_sources() picks them up.
    """
    from rdflib import OWL
    added = True
    while added:  # repeat until no new files are discovered
        added = False
        for entry in list(_catalog_entries):
            g = _catalog_graphs.get(entry["filename"])
            if g is None:
                continue
            for import_uri in g.objects(None, OWL.imports):
                import_fname = str(import_uri).rstrip("/").split("/")[-1]
                if not import_fname:
                    continue
                # Already known?
                if any(e["filename"] == import_fname for e in _catalog_entries):
                    continue
                imported_g = _load_owl_file(catalog_dir, import_fname)
                if imported_g is not None:
                    _catalog_entries.append({"filename": import_fname, "logical_uri": str(import_uri), "enabled": True})
                    _catalog_graphs[import_fname] = imported_g
                    logging.info("Auto-loaded import %s (referenced by %s)", import_fname, entry["filename"])
                    added = True


def _rebuild_sources() -> None:
    """Rebuild ontology_graph and _ontology_sources from enabled _catalog_graphs."""
    global _ontology_sources, ontology_graph
    import rdflib
    from rdflib import OWL, RDF, RDFS, URIRef
    new_sources: Dict[str, str] = {}
    combined = rdflib.Graph()
    for entry in _catalog_entries:
        if not entry.get("enabled"):
            continue
        g = _catalog_graphs.get(entry["filename"])
        if g is None:
            continue
        combined += g
        for cls_type in (OWL.Class, RDFS.Class):
            for s in g.subjects(RDF.type, cls_type):
                if isinstance(s, URIRef):
                    new_sources.setdefault(str(s), entry["filename"])
        for prop_type in (OWL.DatatypeProperty, OWL.ObjectProperty):
            for s in g.subjects(RDF.type, prop_type):
                if isinstance(s, URIRef):
                    new_sources.setdefault(str(s), entry["filename"])
    _ontology_sources = new_sources
    ontology_graph = combined if len(combined) > 0 else None


def _init_catalog(path: Optional[str] = None) -> None:
    global _catalog_path, _catalog_state_path, _active_ontology_path
    ont_path_str = path or settings.ONTOLOGY_PATH
    if ont_path_str:
        _active_ontology_path = ont_path_str
    if not ont_path_str:
        return
    from semantic_mapper.ontology.loader import OntologyLoader
    ont_path = Path(ont_path_str)
    if not ont_path.is_absolute():
        ont_path = ROOT / ont_path

    if _is_catalog(str(ont_path)):
        _catalog_path = ont_path
        _catalog_state_path = ont_path.parent / "catalog_state.json"
        raw_entries = _parse_catalog(ont_path)
        catalog_dir = ont_path.parent
        _catalog_entries.extend(raw_entries)
        _load_catalog_state(_catalog_state_path, _catalog_entries)
        for entry in _catalog_entries:
            g = _load_owl_file(catalog_dir, entry["filename"])
            if g is not None:
                _catalog_graphs[entry["filename"]] = g
    else:
        # Single OWL file — wrap in synthetic one-entry catalog
        catalog_dir = ont_path.parent
        try:
            g = OntologyLoader.load(str(ont_path))
            fname = ont_path.name
            _catalog_entries.append({"filename": fname, "logical_uri": "", "enabled": True})
            _catalog_graphs[fname] = g
        except Exception as exc:
            logging.warning("Could not load ontology: %s", exc)
    _resolve_imports(catalog_dir)
    _rebuild_sources()


def _reinit_catalog(new_path: str) -> None:
    """Clear all catalog state and reload from a new path."""
    global _catalog_path, _catalog_state_path, _active_ontology_path, _active_write_target
    _active_ontology_path = new_path
    _active_write_target = ""  # cleared; callers set it explicitly after this call
    with _catalog_lock:
        _catalog_entries.clear()
        _catalog_graphs.clear()
        _catalog_path = None
        _catalog_state_path = None
    _init_catalog(new_path)


def _resolve_write_target() -> Optional[Path]:
    """Return the Path that suggestions and property edits should write to.

    Priority:
    1. ``_active_write_target`` (filename set when user explicitly selects a file)
    2. ``_active_ontology_path`` basename, if it is a direct OWL/RDF/TTL file
    3. First enabled catalog entry
    4. ``_active_ontology_path`` itself as a last resort
    """
    ont_path_str = _active_ontology_path or settings.ONTOLOGY_PATH
    if not ont_path_str:
        return None
    ont_path = Path(ont_path_str)
    if not ont_path.is_absolute():
        ont_path = ROOT / ont_path
    ont_dir = ont_path.parent if ont_path.suffix.lower() in {".owl", ".rdf", ".ttl"} else ont_path

    if _active_write_target:
        candidate = ont_dir / _active_write_target
        if candidate.exists():
            return candidate

    # If the active path itself is a direct OWL file, use it
    if ont_path.suffix.lower() in {".owl", ".rdf", ".ttl"} and ont_path.exists():
        return ont_path

    # First enabled catalog entry
    with _catalog_lock:
        for entry in _catalog_entries:
            if entry.get("enabled"):
                candidate = ont_dir / entry["filename"]
                if candidate.exists():
                    return candidate

    return ont_path if ont_path.exists() else None


def _list_owl_files() -> List[str]:
    """Return .owl/.rdf filenames available in the ontology directory."""
    ont_path_str = settings.ONTOLOGY_PATH
    if not ont_path_str:
        return []
    ont_path = Path(ont_path_str)
    if not ont_path.is_absolute():
        ont_path = ROOT / ont_path
    # Use the parent when the path looks like a file (has an OWL extension),
    # even if that specific file has since been deleted.
    if ont_path.suffix.lower() in {".owl", ".rdf", ".ttl"}:
        directory = ont_path.parent
    else:
        directory = ont_path
    if not directory.exists():
        return []
    return sorted(
        f.name for f in directory.iterdir()
        if f.suffix.lower() in {".owl", ".rdf", ".ttl"} and f.is_file()
    )


_init_catalog(_active_ontology_path or None)


def _load_report_ontology(r: SemanticMappingReport) -> Optional[Any]:
    """Load the specific OWL file that was used during the map phase for *r*.

    GoldDefinitionBuilder._resolve_fk_properties() must use the same ontology
    namespace as the mapping report's property URIs.  Using the combined catalog
    graph (which may include OWL files from different generations with different
    base IRIs) causes it to inject property URIs from the wrong namespace,
    producing SPARQL queries that return zero results against the triplestore.
    """
    from semantic_mapper.ontology.loader import OntologyLoader
    ont_path_str = r.ontology_uri or settings.ONTOLOGY_PATH
    if not ont_path_str:
        return None
    ont_path = Path(ont_path_str)
    if not ont_path.is_absolute():
        ont_path = ROOT / ont_path
    if not ont_path.exists():
        logging.warning("Report ontology not found at %s — skipping FK resolution", ont_path)
        return None
    try:
        return OntologyLoader.load(str(ont_path))
    except Exception as exc:
        logging.warning("Could not load report ontology %s: %s", ont_path, exc)
        return None


# _load_app_state() / --project above may have activated a project, repointing
# QUERY_MAPPING_REPORT_PATH away from the default. Re-read the report so the
# engines below build from the active project's mappings rather than the default.
report = _read_report(Path(settings.QUERY_MAPPING_REPORT_PATH))
gold_definitions = GoldDefinitionBuilder().build(report, ontology_graph=_load_report_ontology(report))
try:
    from sqlalchemy import create_engine as _sa_create_engine_init
    _init_gold_eng = _sa_create_engine_init(settings.GOLD_DATABASE_URL, pool_pre_ping=True)
    gold_definitions = refine_id_columns(gold_definitions, _init_gold_eng)
    _init_gold_eng.dispose()
    del _init_gold_eng, _sa_create_engine_init
except Exception as _refine_exc:
    logging.warning("startup refine_id_columns failed: %s", _refine_exc)
_engine = SparqlQueryEngine(definitions=gold_definitions, settings=settings)
_engine_lock = threading.Lock()

_corrections_store = CorrectionsStore(settings.CORRECTIONS_DB_PATH)
_mapping_report_path = Path(settings.QUERY_MAPPING_REPORT_PATH)


_sql_engine_cache: Dict[str, Any] = {}  # layer → SemanticQueryEngine instance

_BRONZE_SYSTEM_PROMPT = """\
You are a SQL expert querying raw (bronze) source MySQL tables.
Translate the natural language question into a MySQL SELECT statement.

RULES:
1. Only generate SELECT statements. Never INSERT, UPDATE, DELETE, DROP, or DDL.
2. Always add LIMIT {max_rows} unless the user explicitly asks for all rows.
3. Use backtick-quoted identifiers: `table`.`column`.
4. Only use tables and columns listed in the SCHEMA below. Never invent names.
5. Prefer COUNT, AVG, GROUP BY, ORDER BY where meaningful.

SCHEMA:
{schema_context}

Respond ONLY with valid JSON:
{{"sql": "SELECT ...", "explanation": "one sentence", "tables_used": ["raw_patients"]}}
"""


def _build_bronze_context() -> str:
    """Derive a schema description of bronze (raw) tables from the mapping report."""
    from collections import defaultdict
    tables: Dict[str, list] = defaultdict(list)
    for tbl in report.tables:
        seen: set = set()
        for col in tbl.column_mappings:
            if col.source_column not in seen:
                tables[tbl.source_table].append((col.source_column, col.source_data_type))
                seen.add(col.source_column)
    if not tables:
        return "(No tables found in mapping report)"
    lines: List[str] = []
    for tbl_name in sorted(tables):
        lines.append(f"Table: `{tbl_name}`")
        for col_name, col_type in tables[tbl_name]:
            lines.append(f"  `{col_name}` {col_type}")
        lines.append("")
    return "\n".join(lines)


def _get_sql_engine(layer: str):
    """Return a lazily-created SemanticQueryEngine for the requested layer."""
    from sqlalchemy import create_engine as _sa_create_engine
    from semantic_mapper.query.engine import SemanticQueryEngine as _SemanticQueryEngine

    if layer in _sql_engine_cache:
        return _sql_engine_cache[layer]

    if layer == "gold":
        db_engine = _sa_create_engine(settings.GOLD_DATABASE_URL)
        eng = _SemanticQueryEngine(
            report, db_engine, settings,
            gold_engine=db_engine, gold_definitions=gold_definitions,
        )
    elif layer == "silver":
        db_engine = _sa_create_engine(settings.SILVER_DATABASE_URL)
        eng = _SemanticQueryEngine(report, db_engine, settings)
    else:  # bronze — raw source tables; build a bespoke context from the report
        db_url = _override_db_url(_active_database) if _active_database else settings.DATABASE_URL
        db_engine = _sa_create_engine(db_url)
        eng = _SemanticQueryEngine(report, db_engine, settings)
        bronze_ctx = _build_bronze_context()
        eng._schema_context = bronze_ctx
        eng._system_prompt = _BRONZE_SYSTEM_PROMPT.format(
            max_rows=settings.QUERY_MAX_ROWS,
            schema_context=bronze_ctx,
        )

    _sql_engine_cache[layer] = eng
    return eng


def _reinit_engine() -> int:
    """Reload the mapping report from disk and rebuild the SparqlQueryEngine.

    Called after successful pipeline runs so the query interface reflects the
    latest mappings without requiring an app restart.  Returns the number of
    gold table definitions loaded.
    """
    global _engine, gold_definitions, report, _sql_engine_cache
    report_path = Path(settings.QUERY_MAPPING_REPORT_PATH)
    if not report_path.exists():
        logging.warning("reinit_engine: report not found at %s", report_path)
        return 0
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            new_report = SemanticMappingReport.model_validate(json.load(fh))
        from sqlalchemy import create_engine as _sa_create_engine_reinit
        new_defs = GoldDefinitionBuilder().build(new_report, ontology_graph=_load_report_ontology(new_report))
        _reinit_gold_eng = _sa_create_engine_reinit(settings.GOLD_DATABASE_URL, pool_pre_ping=True)
        new_defs = refine_id_columns(new_defs, _reinit_gold_eng)
        _reinit_gold_eng.dispose()
        new_engine = SparqlQueryEngine(definitions=new_defs, settings=settings)
        with _engine_lock:
            report = new_report
            gold_definitions = new_defs
            _engine = new_engine
            _sql_engine_cache = {}  # invalidate SQL engines — schema may have changed
        logging.info("reinit_engine: loaded %d gold definitions", len(new_defs))
        return len(new_defs)
    except Exception as exc:
        logging.warning("reinit_engine failed: %s", exc)
        return 0

# ── Pipeline runs store ────────────────────────────────────────────────────────
_pipeline_runs_db_path = Path(settings.CORRECTIONS_DB_PATH).parent / "pipeline_runs.db"


def _init_pipeline_runs_db() -> None:
    con = sqlite3.connect(str(_pipeline_runs_db_path))
    con.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id      TEXT PRIMARY KEY,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            status      TEXT NOT NULL,
            return_code INTEGER,
            phases      TEXT,
            threshold   REAL,
            log_content TEXT
        )
    """)
    try:
        con.execute("ALTER TABLE pipeline_runs ADD COLUMN ontology_hash TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        con.execute("ALTER TABLE pipeline_runs ADD COLUMN gold_mode INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        con.execute("ALTER TABLE pipeline_runs ADD COLUMN mapping_report_json TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    con.execute("""
        CREATE TABLE IF NOT EXISTS ontology_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            snapped_at     TEXT NOT NULL,
            run_id         TEXT,
            hash           TEXT NOT NULL,
            snapshot_files TEXT NOT NULL
        )
    """)
    try:
        con.execute("ALTER TABLE ontology_snapshots ADD COLUMN description TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    con.commit()
    con.close()
    from semantic_mapper.mcp.query_store import ensure_table as _ensure_mcp_queries
    _ensure_mcp_queries(settings)


_init_pipeline_runs_db()

_SNAPSHOTS_DIR = _pipeline_runs_db_path.parent / "snapshots"


def _compute_ontology_hash() -> str:
    """MD5 of all enabled OWL file contents (sorted by filename for stability)."""
    h = hashlib.md5()
    ont_path_str = settings.ONTOLOGY_PATH
    if not ont_path_str:
        return ""
    ont_path = Path(ont_path_str)
    if not ont_path.is_absolute():
        ont_path = ROOT / ont_path
    catalog_dir = ont_path.parent
    for entry in sorted(_catalog_entries, key=lambda e: e["filename"]):
        if not entry.get("enabled"):
            continue
        fp = catalog_dir / entry["filename"]
        if fp.exists():
            h.update(fp.read_bytes())
    return h.hexdigest()


def _snapshot_ontology(run_id: str, hash_val: str, description: str = "") -> None:
    """Copy enabled OWL files to snapshots/<run_id>/ and record in DB."""
    ont_path_str = settings.ONTOLOGY_PATH
    if not ont_path_str:
        return
    ont_path = Path(ont_path_str)
    if not ont_path.is_absolute():
        ont_path = ROOT / ont_path
    catalog_dir = ont_path.parent
    snap_dir = _SNAPSHOTS_DIR / run_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for entry in _catalog_entries:
        if not entry.get("enabled"):
            continue
        src = catalog_dir / entry["filename"]
        if src.exists():
            try:
                shutil.copy2(str(src), str(snap_dir / entry["filename"]))
                copied.append(entry["filename"])
            except Exception as exc:
                logging.warning("Could not snapshot %s: %s", entry["filename"], exc)
    try:
        con = sqlite3.connect(str(_pipeline_runs_db_path))
        con.execute(
            "INSERT INTO ontology_snapshots (snapped_at, run_id, hash, snapshot_files, description) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), run_id, hash_val, json.dumps(copied), description),
        )
        con.commit()
        con.close()
    except Exception as exc:
        logging.warning("Could not record ontology snapshot: %s", exc)

# ── Pipeline runner state ──────────────────────────────────────────────────────
_pipeline: Dict[str, Any] = {
    "status": "idle",   # idle | running | done | failed
    "run_id": None,
    "log": [],          # accumulated output lines
    "return_code": None,
    "started_at": None,
    "phases": None,
    "threshold": None,
    "proc": None,       # subprocess.Popen reference (for stop)
}
_pipeline_lock = threading.Lock()


def _run_pipeline_thread(
    cmd: List[str],
    run_id: str,
    started_at: str,
    phases: str,
    threshold: Optional[float],
    ont_hash: str = "",
    env: Optional[Dict[str, str]] = None,
    gold_mode: Optional[int] = None,
) -> None:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(ROOT),
        env=env,
    )
    with _pipeline_lock:
        _pipeline["proc"] = proc

    tail_deque: collections.deque = collections.deque(maxlen=50)
    capture_thread = threading.Thread(
        target=capture_subprocess_logs,
        args=(proc, run_id, _pipeline["log"], tail_deque, _pipeline_lock),
        daemon=True,
    )
    capture_thread.start()
    proc.wait()
    capture_thread.join(timeout=2.0)

    if proc.returncode != 0:
        logging.getLogger("pipeline.subprocess").error(
            f"Pipeline run {run_id} failed with exit code {proc.returncode}",
            extra={"run_id": run_id, "exit_code": proc.returncode, "last_lines": list(tail_deque)},
        )
    finished_at = datetime.now(timezone.utc).isoformat()

    # Extract the generated ontology path from the log (seed/full phases print
    # "Output written to  : <path>"). Store it so the UI can auto-select it.
    generated_ontology: Optional[str] = None
    for line in reversed(_pipeline.get("log", [])):
        if "Output written to" in line and ".owl" in line:
            # Format: "  Output written to  : owl/mimic_bronze_full_260417_003.owl"
            parts = line.split(":", 1)
            if len(parts) == 2:
                generated_ontology = parts[1].strip()
            break

    with _pipeline_lock:
        _pipeline["return_code"] = proc.returncode
        _pipeline["status"] = "done" if proc.returncode == 0 else "failed"
        _pipeline["proc"] = None
        _pipeline["generated_ontology"] = generated_ontology
        log_lines = list(_pipeline["log"])

    status = "done" if proc.returncode == 0 else "failed"

    # Read the mapping report JSON from disk so it can be stored in the same
    # INSERT that creates the row.  The subprocess write_mapping_report() call
    # runs an UPDATE before this row exists and silently affects 0 rows, so the
    # web_app thread is the only reliable place to persist it.
    mapping_report_json: Optional[str] = None
    if proc.returncode == 0:
        try:
            mapping_report_json = _mapping_report_path.read_text(encoding="utf-8")
        except Exception:
            pass  # report absent when only non-mapping phases ran

    try:
        con = sqlite3.connect(str(_pipeline_runs_db_path))
        con.execute(
            "INSERT OR REPLACE INTO pipeline_runs "
            "(run_id, started_at, finished_at, status, return_code, phases, threshold, log_content, ontology_hash, gold_mode, mapping_report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, started_at, finished_at, status, proc.returncode, phases, threshold,
             "\n".join(log_lines), ont_hash, gold_mode, mapping_report_json),
        )
        con.commit()
        con.close()
    except Exception as exc:
        logging.warning("Could not persist pipeline run: %s", exc)

    # Reload query engine if a mapping-producing phase completed successfully
    if proc.returncode == 0 and any(
        p.strip() in {"map", "gold", "rdf", "map,rdf"} for p in phases.split(",")
    ):
        _reinit_engine()

    # Update pipeline lifecycle timestamps (issue #47)
    if proc.returncode == 0:
        global _last_map_at, _last_silver_at, _last_gold_at, _last_rdf_at
        _finished = finished_at
        phase_set = {p.strip().lower() for p in phases.replace("all", "inspect,map,silver,gold,rdf").split(",")}
        if "map" in phase_set:
            _last_map_at = _finished
        if "silver" in phase_set:
            _last_silver_at = _finished
        if "gold" in phase_set:
            _last_gold_at = _finished
        if "rdf" in phase_set:
            _last_rdf_at = _finished
        _save_app_state()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="ForgeMesh", docs_url=None, redoc_url=None)

from semantic_mapper.webapp.middleware import request_logging_middleware  # noqa: E402
from semantic_mapper.webapp.log_helpers import QueryLifecycleLogger, capture_subprocess_logs  # noqa: E402
app.middleware("http")(request_logging_middleware)


# ── Request / response models ─────────────────────────────────────────────────
class QueryRequest(BaseModel):
    question: str
    limit: Optional[int] = None
    layer: str = "graph"  # "bronze" | "silver" | "gold" | "graph"


class QueryResponse(BaseModel):
    question: str
    sparql: str
    explanation: str
    rows: List[Dict[str, Any]]
    row_count: int
    classes_used: List[str]
    prompt_tokens: int
    completion_tokens: int
    error: Optional[str]
    # Answer-envelope fields (FP-1/FP-2). run_id is the stable per-answer receipt id;
    # receipt is the inline four-block receipt; the front-end fetches the full
    # receipt from /api/runs/{run_id} on demand.
    run_id: Optional[str] = None
    receipt_url: Optional[str] = None
    receipt: Optional[Dict[str, Any]] = None


# ── Answer-envelope / receipt store (FP-1) ────────────────────────────────────
# Web-issued queries land in the SAME store as MCP-issued queries (mcp_queries in
# pipeline_runs.db) so both appear in one run log (FP-3) and both are resolvable by
# the /runs/{id} permalink (FP-2).
from semantic_mapper.mcp import query_store as _qstore  # noqa: E402

try:
    _qstore.ensure_table(settings)
except Exception as _qs_exc:  # pragma: no cover - defensive
    logger.warning("Could not initialise query store: %s", _qs_exc)


def _serialise_v2_proof(result: Any) -> Optional[str]:
    """Serialise a QueryResult's v2 proof_record to JSON, if present."""
    pr = getattr(result, "proof_record", None)
    if pr is None:
        return None
    try:
        if hasattr(pr, "model_dump_json"):
            return pr.model_dump_json()
        return json.dumps(pr, default=str)
    except Exception:
        return None


def _persist_query_run(question: str, result: Any, mode: str) -> Optional[str]:
    """Persist a successful web query to the shared store; return its run id."""
    if getattr(result, "error", None):
        return None
    try:
        from datetime import datetime, timezone
        rows = list(getattr(result, "rows", None) or [])
        columns = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
        query_id = _qstore.new_query_id()
        record = _qstore.MCPQueryRecord(
            query_id=query_id,
            session_id="web_default",
            question=question,
            executed_query=getattr(result, "generated_sql", "") or "",
            mode=mode,
            answer={
                "rows": rows,
                "row_count": getattr(result, "row_count", len(rows)),
                "query_rewrites": list(getattr(result, "query_rewrites", None) or []),
                "columns": columns,
            },
            run_id=_qstore.get_latest_run_id(settings) or "unknown",
            created_at=datetime.now(timezone.utc).isoformat(),
            proof_record_json=_serialise_v2_proof(result),
        )
        _qstore.write_query(record, settings)
        return query_id
    except Exception as exc:
        logger.warning("Could not persist web query run: %s", exc)
        return None


def _assemble_run_envelope(query_id: str, deep: bool = True) -> Optional[Any]:
    """Build the AnswerEnvelope for a stored run id. None if unknown. Never raises."""
    from semantic_mapper.envelope import build_envelope
    record = _qstore.read_query(query_id, settings)
    if record is None:
        return None
    v2_proof: Optional[Dict[str, Any]] = None
    if getattr(record, "proof_record_json", None):
        try:
            v2_proof = json.loads(record.proof_record_json)
        except Exception:
            v2_proof = None
    prove_result: Optional[Dict[str, Any]] = None
    if deep:
        try:
            from semantic_mapper.mcp.tools.prove_answer import run_prove_answer
            pr = run_prove_answer(query_id, settings)
            if isinstance(pr, dict) and (pr.get("semantic_proof") or pr.get("proof_presentation")):
                prove_result = pr
        except Exception:
            prove_result = None
    ont_version = getattr(report, "ontology_uri", None)
    return build_envelope(
        run_id=query_id,
        question=record.question,
        executed_query=record.executed_query,
        mode=record.mode,
        answer=record.answer,
        settings=settings,
        v2_proof=v2_proof,
        prove_result=prove_result,
        ontology_version=ont_version,
    )


# ── API routes ────────────────────────────────────────────────────────────────
@app.post("/api/query", response_model=QueryResponse)
def api_query(body: QueryRequest) -> QueryResponse:
    layer = body.layer or "graph"
    mode = "sparql" if layer == "graph" else layer
    query_id = uuid.uuid4().hex[:8]
    result = None
    with QueryLifecycleLogger(query_id, mode) as qlog:
        qlog.received(body.question)
        if layer == "graph":
            result = _engine.query(body.question, limit=body.limit)
            if result.error:
                qlog.failed(Exception(result.error), generated_query=result.generated_sql or None)
            else:
                qlog.generated(result.generated_sql)
                qlog.completed(result.row_count, result.prompt_tokens, result.completion_tokens)
        else:
            try:
                sql_eng = _get_sql_engine(layer)
                result = sql_eng.query(body.question)
                if result.error:
                    qlog.failed(Exception(result.error), generated_query=result.generated_sql or None)
                else:
                    qlog.generated(result.generated_sql)
                    qlog.completed(result.row_count, result.prompt_tokens, result.completion_tokens)
            except Exception as exc:
                from semantic_mapper.query.models import QueryResult as _QR
                result = _QR(
                    question=body.question, generated_sql="", explanation="",
                    rows=[], row_count=0, tables_used=[],
                    prompt_tokens=0, completion_tokens=0,
                    error=f"Could not connect to {layer} layer: {exc}",
                )
                qlog.failed(exc)

    # Answer envelope (FP-1): persist the run and attach an inline receipt so the
    # Query tab can render it directly beneath the grid. The full receipt (bronze
    # lineage) is fetched lazily from /api/runs/{run_id} when the reader drills in.
    run_id_out: Optional[str] = None
    receipt_url_out: Optional[str] = None
    receipt_out: Optional[Dict[str, Any]] = None
    if not result.error:
        run_id_out = _persist_query_run(result.question, result, mode)
        if run_id_out:
            try:
                from semantic_mapper.envelope import build_envelope
                _env = build_envelope(
                    run_id=run_id_out,
                    question=result.question,
                    executed_query=result.generated_sql or "",
                    mode=mode,
                    answer={"rows": result.rows, "row_count": result.row_count,
                            "columns": list(result.rows[0].keys()) if result.rows else [],
                            "query_rewrites": list(result.query_rewrites or [])},
                    settings=settings,
                    v2_proof=(json.loads(_serialise_v2_proof(result))
                              if _serialise_v2_proof(result) else None),
                    ontology_version=getattr(report, "ontology_uri", None),
                )
                receipt_url_out = _env.receipt_url
                receipt_out = _env.receipt.model_dump()
            except Exception as exc:
                logger.warning("Could not build inline receipt: %s", exc)

    return QueryResponse(
        question=result.question,
        sparql=result.generated_sql,
        explanation=result.explanation,
        rows=result.rows,
        row_count=result.row_count,
        classes_used=result.tables_used,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        error=result.error,
        run_id=run_id_out,
        receipt_url=receipt_url_out,
        receipt=receipt_out,
    )


@app.post("/api/reset")
def api_reset() -> Dict[str, str]:
    _engine.reset_conversation()
    for eng in _sql_engine_cache.values():
        try:
            eng.reset_conversation()
        except Exception:
            pass
    return {"status": "ok"}


@app.get("/api/schema")
def api_schema() -> Dict[str, str]:
    return {"schema": _engine._schema_context}


@app.post("/api/reload-engine")
def api_reload_engine() -> Dict[str, Any]:
    """Reload the query engine from the current mapping report on disk."""
    n = _reinit_engine()
    return {"gold_tables": n, "schema_length": len(_engine._schema_context)}


# ── Prove-It endpoints ────────────────────────────────────────────────────────
from semantic_mapper.db.run_store import get_mapping_report as _get_mapping_report  # noqa: E402
from semantic_mapper.prove.composer import compose_proof, compose_row_proof  # noqa: E402
from semantic_mapper.prove.decision_lookup import ProofEvidenceMissing, get_mapping_decisions  # noqa: E402
from semantic_mapper.prove.lineage_walker import LineageWalker  # noqa: E402
from semantic_mapper.prove.models import RowProofRecord  # noqa: E402
from semantic_mapper.prove.narrator import InterpretationNote, ProofCaveat, narrate  # noqa: E402
from semantic_mapper.prove.query_rewriter import ProofRewriteError, rewrite, rewrite_for_row_proof  # noqa: E402
from semantic_mapper.prove.render.markdown import render_proof  # noqa: E402
from semantic_mapper.prove.subgraph import OntologySubgraph, extract_subgraph  # noqa: E402


class ProofRequest(BaseModel):
    question: str
    query: str
    mode: str                        # "sparql" | "gold" | "silver"
    answer: Dict[str, Any]
    run_id: Optional[str] = None     # resolved server-side if omitted
    explanation: str = ""
    project: Optional[str] = None   # project name (e.g. "mimic", "openflights");
                                    # when provided, databases are resolved from
                                    # projects/{project}.yaml so the proof endpoint
                                    # can serve any project, not just the web app's
                                    # configured default (ISSUE-01)


class RowProofRequest(BaseModel):
    question: str
    query: str
    mode: str
    answer: Dict[str, Any]
    run_id: Optional[str] = None
    row_grouping_values: Dict[str, Any] = {}


def _latest_pipeline_run_id() -> Optional[str]:
    """Return the run_id of the most recently completed successful pipeline run, or None."""
    try:
        con = sqlite3.connect(str(_pipeline_runs_db_path))
        row = con.execute(
            "SELECT run_id FROM pipeline_runs WHERE status='done' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


@app.get("/api/query/proof/supported-patterns")
def api_proof_supported_patterns() -> Dict[str, Any]:
    return {
        "sparql": ["COUNT", "SUM", "AVG", "GROUP BY", "SELECT (no aggregate)"],
        "sql": ["SELECT (any projection)", "COUNT(*)", "ORDER BY (stripped)"],
        "unsupported": ["HAVING", "MINUS", "subquery"],
    }


# ── Receipt store endpoints (FP-1) ────────────────────────────────────────────

@app.get("/api/runs")
def api_runs(limit: int = 25) -> Dict[str, Any]:
    """Recent runs across every surface (web + MCP), newest first — the run log."""
    limit = max(1, min(int(limit), 200))
    out: List[Dict[str, Any]] = []
    try:
        con = sqlite3.connect(str(_pipeline_runs_db_path))
        rows = con.execute(
            "SELECT query_id, session_id, question, mode, created_at "
            "FROM mcp_queries ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        con.close()
        for r in rows:
            out.append({
                "run_id": r[0],
                "surface": "mcp" if (r[1] or "").startswith("mcp") else "web",
                "question": r[2],
                "mode": r[3],
                "created_at": r[4],
                "receipt_url": f"{_receipt_base_url()}/runs/{r[0]}",
            })
    except Exception as exc:
        logger.warning("api_runs failed: %s", exc)
    return {"runs": out, "count": len(out)}


@app.get("/api/runs/{run_id}")
def api_run_envelope(run_id: str, deep: bool = True) -> Any:
    """Return the AnswerEnvelope for a run id. 404 if unknown."""
    env = _assemble_run_envelope(run_id, deep=deep)
    if env is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_run_id", "run_id": run_id})
    return env.model_dump()


@app.get("/api/runs/{run_id}/rows")
def api_run_rows(run_id: str, page: int = 1, page_size: Optional[int] = None) -> Dict[str, Any]:
    """Resolve the row-set handle to the exact rows behind the answer, paged."""
    record = _qstore.read_query(run_id, settings)
    if record is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_run_id", "run_id": run_id})
    all_rows = list((record.answer or {}).get("rows") or [])
    columns = list((record.answer or {}).get("columns") or [])
    if not columns and all_rows and isinstance(all_rows[0], dict):
        columns = list(all_rows[0].keys())
    size = page_size or getattr(settings, "ENVELOPE_ROWS_PAGE_SIZE", 100)
    size = max(1, min(int(size), 1000))
    page = max(1, int(page))
    start = (page - 1) * size
    window = all_rows[start:start + size]
    total = len(all_rows)
    return {
        "run_id": run_id,
        "columns": columns,
        "rows": window,
        "page": page,
        "page_size": size,
        "total_rows": total,
        "total_pages": max(1, (total + size - 1) // size),
    }


def _receipt_base_url() -> str:
    base = getattr(settings, "RECEIPT_BASE_URL", "") or settings.FORGEMESH_WEB_APP_URL
    return base.rstrip("/")


@app.post("/api/query/proof")
def api_query_proof(body: ProofRequest, request: Request) -> Any:
    from fastapi.responses import PlainTextResponse
    from sqlalchemy import create_engine as _sa_engine, text as _sa_text

    run_id = body.run_id or _latest_pipeline_run_id() or "unknown"

    # Phase 3 — rewrite query
    try:
        proof_query = rewrite(body.query, body.mode)
    except ProofRewriteError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "unsupported_pattern", "pattern": exc.unsupported_pattern},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)})

    # Resolve database URLs — use project-specific databases when project is
    # provided in the request, otherwise fall back to the webapp's configured
    # defaults.  This allows the single web app to serve proofs for any project
    # (ISSUE-01: openflights proof was hitting MIMIC databases).
    _bronze_url = settings.DATABASE_URL
    _silver_url = settings.SILVER_DATABASE_URL
    _gold_url = settings.GOLD_DATABASE_URL
    if body.project:
        try:
            import re as _re_proof
            from semantic_mapper.project.resolver import resolve_project
            _proj_cfg = resolve_project(body.project)
            _bronze_url = _proj_cfg.bronze_database_url
            _silver_url = _proj_cfg.silver_database_url
            _gold_url = _proj_cfg.gold_database_url
            # Apply Docker host rewrite so container-internal MySQL (db:3306)
            # is used instead of the host-facing localhost:3307 from project YAMLs.
            _db_host = os.environ.get("FORGEMESH_DB_HOST")
            _db_port = os.environ.get("FORGEMESH_DB_PORT", "3306")
            if _db_host:
                def _rewrite(u: str) -> str:
                    return _re_proof.sub(r"@[^@/]+/", f"@{_db_host}:{_db_port}/", u) if u and "@" in u else u
                _bronze_url = _rewrite(_bronze_url)
                _silver_url = _rewrite(_silver_url)
                _gold_url   = _rewrite(_gold_url)
        except Exception as _proj_exc:
            logger.warning("proof: could not resolve project %r: %s", body.project, _proj_exc)

    # Build engines — test bronze connectivity first
    try:
        bronze_url = _override_db_url(_active_database) if _active_database else _bronze_url
        bronze_engine = _sa_engine(bronze_url, pool_pre_ping=True)
        with bronze_engine.connect():
            pass
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "bronze_unavailable", "detail": str(exc)},
        )
    silver_engine = _sa_engine(_silver_url)
    gold_engine = _sa_engine(_gold_url)

    # Phase 4 — execute proof query → subjects, walk lineage
    subjects: list[str] = []
    chains: list = []
    caveats: list[ProofCaveat] = []

    try:
        if body.mode == "sparql":
            rows = _engine._execute_sparql(proof_query.proof_query)
            subjects = _extract_sparql_subjects(rows)
            gold_def = _gold_def_for_subjects(subjects)
            if gold_def is None:
                # The projected subject is not walkable (e.g. a query grouped by a
                # categorical dimension such as care unit — Careunit has no gold
                # table). Anchor the walk on the finest-grained walkable entity
                # among the other bound variables so grouped/aggregate queries
                # still trace real graph→gold→silver→bronze lineage.
                subjects, gold_def = _walkable_subjects_for_rows(rows)
            if gold_def and subjects:
                walker = LineageWalker(
                    gold_engine=gold_engine, silver_engine=silver_engine,
                    bronze_engine=bronze_engine,
                    base_uri=settings.RDF_BASE_URI, source_name=settings.RDF_SOURCE_NAME,
                )
                id_col = gold_def.id_column or "_gold_id"
                from semantic_mapper.prove.subject_refs import bronze_source_refs
                chains = walker.walk_uris(
                    subjects, gold_def.gold_table_name, id_col,
                    bronze_refs=bronze_source_refs(subjects, _engine._execute_sparql),
                )
        else:
            sa_eng = gold_engine if body.mode == "gold" else silver_engine
            pk_col = "_gold_id" if body.mode == "gold" else "_silver_id"
            sql_rows = _execute_proof_sql(proof_query.proof_query, sa_eng, _sa_text)
            subjects = [str(r[pk_col]) for r in sql_rows if r.get(pk_col) is not None]
            table = _parse_table_from_sql(proof_query.proof_query)
            walker = LineageWalker(
                gold_engine=gold_engine, silver_engine=silver_engine,
                bronze_engine=bronze_engine,
                base_uri=settings.RDF_BASE_URI, source_name=settings.RDF_SOURCE_NAME,
            )
            ids = [int(s) for s in subjects if s.isdigit()]
            if body.mode == "gold":
                chains = walker.walk_gold(table, ids)
            else:
                chains = walker.walk_silver(table, ids)
    except Exception as exc:
        logger.warning("proof lineage walk failed: %s", exc)
        caveats.append(ProofCaveat(
            type="LINEAGE_BROKEN", count=None, table=None,
            property_uri=None, reason=str(exc),
        ))

    if chains and all(c.broken for c in chains):
        caveats.append(ProofCaveat(
            type="LINEAGE_BROKEN", count=len(chains), table=None,
            property_uri=None,
            reason="All lineage chains are broken — subject URIs could not be traced back to source rows.",
        ))
        # Do NOT clear chains here — broken chains must survive into the
        # serialised proof record so that broken_lineage_count and
        # result_confidence.broken can count them correctly (ISSUE-02).

    # Phase 5 — mapping decisions
    class_uris = _class_uris_from_chains(chains)
    if not class_uris:
        # No lineage traced (e.g. SPARQL grouped query) — show all decisions for this run
        class_uris = {d.primary_class_uri for d in gold_definitions if d.primary_class_uri}
    try:
        decisions = get_mapping_decisions(
            run_id=run_id,
            class_uris=class_uris,
            property_uris=set(),
            settings=settings,
            include_adjacent=True,
        )
    except (ProofEvidenceMissing, Exception):
        decisions = []

    # Phase 1 — subgraph
    if ontology_graph is not None:
        subgraph = extract_subgraph(ontology_graph, class_uris, set())
    else:
        subgraph = OntologySubgraph(classes={}, properties={}, adjacency_notes=[])

    # Phase 6 — narrate (or use explanation passthrough when narration disabled)
    if not settings.PROVE_NARRATE:
        note = InterpretationNote(
            interpreted_as=body.explanation or body.question,
            narrated=True,
            narrator_model=None,
            scope_assumptions=[],
            not_included=[],
            interpretation_confidence="HIGH",
            interpretation_confidence_reason="",
        )
    else:
        note, caveats = narrate(
            question=body.question, query=body.query, answer=body.answer,
            subgraph=subgraph, decisions=decisions, caveats=caveats,
            settings=settings,
        )

    # Phase 7 — compose
    proof_record = compose_proof(
        question=body.question, query=body.query, mode=body.mode,
        answer=body.answer, run_id=run_id,
        proof_query=proof_query, subjects=subjects, chains=chains,
        subgraph=subgraph, decisions=decisions, note=note,
        caveats=caveats, settings=settings,
    )

    # Phase 8 — render or return JSON
    if "text/markdown" in request.headers.get("accept", ""):
        return PlainTextResponse(render_proof(proof_record), media_type="text/markdown")
    return proof_record.model_dump()


@app.post("/api/query/proof/row")
def api_query_proof_row(body: RowProofRequest) -> Any:
    import traceback as _tb
    from sqlalchemy import create_engine as _sa_engine, text as _sa_text

    try:
        return _api_query_proof_row_inner(body, _sa_engine, _sa_text)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("api_query_proof_row unhandled: %s\n%s", exc, _tb.format_exc())
        raise HTTPException(500, detail={"error": "internal_error", "detail": str(exc)})


def _api_query_proof_row_inner(body: RowProofRequest, _sa_engine: Any, _sa_text: Any) -> Any:
    run_id = body.run_id or _latest_pipeline_run_id() or "unknown"

    # Rewrite for row proof
    try:
        row_pq = rewrite_for_row_proof(body.query, body.mode, body.row_grouping_values)
    except ProofRewriteError as exc:
        raise HTTPException(422, detail={"error": "unsupported_pattern", "pattern": exc.unsupported_pattern})
    except ValueError as exc:
        raise HTTPException(422, detail={"error": str(exc)})

    # Test bronze connectivity
    try:
        bronze_url = _override_db_url(_active_database) if _active_database else settings.DATABASE_URL
        bronze_engine = _sa_engine(bronze_url, pool_pre_ping=True)
        with bronze_engine.connect():
            pass
    except Exception as exc:
        raise HTTPException(503, detail={"error": "bronze_unavailable", "detail": str(exc)})

    try:
        silver_engine = _sa_engine(settings.SILVER_DATABASE_URL)
        gold_engine = _sa_engine(settings.GOLD_DATABASE_URL)
    except Exception as exc:
        raise HTTPException(503, detail={"error": "engine_init_failed", "detail": str(exc)})

    subjects: list[str] = []
    chains: list = []
    caveats: list[ProofCaveat] = []
    rdf_neighbourhood: dict = {}
    sparql_funnel: list | None = None
    funnel_unsupported_reason: str | None = None
    sparql_matches: list | None = None

    try:
        if body.mode == "sparql":
            rows = _engine._execute_sparql(row_pq.proof_query)
            # Capture all URI-typed bound variables per solution row
            match_tuples = _extract_sparql_match_tuples(rows)
            # Primary subjects (backward compat — first URI per row)
            subjects = _extract_sparql_subjects(rows)

            # Walk lineage for every unique URI across all match tuples,
            # grouped by gold table so walk_uris sees homogeneous input.
            all_uris = list(dict.fromkeys(uri for mt in match_tuples for uri in mt.values()))
            all_chains_by_uri: dict = {}
            if all_uris:
                walker = LineageWalker(
                    gold_engine=gold_engine, silver_engine=silver_engine,
                    bronze_engine=bronze_engine,
                    base_uri=settings.RDF_BASE_URI, source_name=settings.RDF_SOURCE_NAME,
                )
                # Group URIs by which gold table they belong to
                uri_groups: dict = {}
                for uri in all_uris:
                    gdef = _gold_def_for_subjects([uri])
                    if gdef:
                        grp = uri_groups.setdefault(gdef.gold_table_name, {"gdef": gdef, "uris": []})
                        grp["uris"].append(uri)
                from semantic_mapper.prove.subject_refs import bronze_source_refs
                _refs = bronze_source_refs(all_uris, _engine._execute_sparql)
                for grp in uri_groups.values():
                    gdef = grp["gdef"]
                    id_col = gdef.id_column or "_gold_id"
                    try:
                        for ch in walker.walk_uris(
                            grp["uris"], gdef.gold_table_name, id_col, bronze_refs=_refs,
                        ):
                            if ch.subject_uri:
                                all_chains_by_uri[ch.subject_uri] = ch
                    except Exception as _wc_exc:
                        logger.warning("walk_uris %s failed: %s", gdef.gold_table_name, _wc_exc)
                # All chains for the lineage field — include FK entities (e.g.
                # diagnoses joined to admissions) so the proof trace can render
                # coloured join lines between related entity types.
                chains = list(all_chains_by_uri.values())
                # Extend subjects with any FK URIs from match tuples that were
                # not already captured as primary subjects, so the JS bronze tree
                # has chain data for every entity it needs to render.
                if match_tuples:
                    _subject_set = set(subjects)
                    _fk_uris = list(dict.fromkeys(
                        u for mt in match_tuples for u in mt.values()
                        if u not in _subject_set
                    ))
                    subjects = subjects + _fk_uris

            # Build match records (all bound URIs per solution row as an entity chain)
            try:
                sparql_matches = _build_sparql_matches(body.query, match_tuples, all_chains_by_uri)
                if match_tuples and not sparql_matches:
                    logger.warning(
                        "sparql_matches empty despite %d match tuples; bound_vars=%s",
                        len(match_tuples),
                        sorted({v for mt in match_tuples for v in mt}),
                    )
            except Exception as _mx_exc:
                logger.warning("sparql match building failed: %s", _mx_exc, exc_info=True)

            # Fetch outgoing RDF triples for each subject (1-hop, for Linked entities display)
            if subjects:
                try:
                    uri_list = " ".join(f"<{s}>" for s in subjects[:20])
                    nbr_rows = _engine._execute_sparql(
                        f"SELECT ?s ?p ?o WHERE {{ VALUES ?s {{ {uri_list} }} ?s ?p ?o }}"
                    )
                    for r in nbr_rows:
                        s_val, p_val, o_val = r.get("s") or "", r.get("p") or "", r.get("o") or ""
                        if s_val and p_val:
                            rdf_neighbourhood.setdefault(s_val, []).append({"p": p_val, "o": o_val})
                except Exception as _nbr_exc:
                    logger.warning("rdf neighbourhood fetch failed: %s", _nbr_exc)
            # Compute SPARQL funnel (progressive per-pattern counts)
            try:
                from semantic_mapper.prove.sparql_funnel import FunnelStep, compute_funnel
                _funnel_steps, funnel_unsupported_reason = compute_funnel(
                    body.query, _engine,
                    row_grouping_values=dict(body.row_grouping_values),
                )
                logger.info(
                    "funnel: steps=%d unsupported=%s subjects=%d",
                    len(_funnel_steps), funnel_unsupported_reason, len(subjects)
                )
                if _funnel_steps:
                    sparql_funnel = [
                        {
                            "pattern_text": s.pattern_text,
                            "pattern_type": s.pattern_type,
                            "cumulative_subject_count": s.cumulative_subject_count,
                            "sample_subjects": list(s.sample_subjects),
                        }
                        for s in _funnel_steps
                    ]
                    # Append a synthetic row-specific step showing the actual count
                    # and subject sample for this result row (differs per row because
                    # row_grouping_values binds to specific diagnosis / filter values).
                    if subjects:
                        row_specific_count = len(set(subjects))
                        rgv = {k: v for k, v in body.row_grouping_values.items()
                               if not str(k).startswith("_")}
                        # Build SPARQL-style filter text from grouping values
                        filter_parts = []
                        for k, v in rgv.items():
                            val_str = f'"{v}"' if isinstance(v, str) else str(v)
                            filter_parts.append(f"?{k} = {val_str}")
                        row_pattern = " . ".join(filter_parts) or "this result row"
                        sparql_funnel.append({
                            "pattern_text": row_pattern,
                            "pattern_type": "filter",
                            "cumulative_subject_count": row_specific_count,
                            "sample_subjects": list(dict.fromkeys(subjects))[:50],
                            "is_row_specific": True,
                        })
                        logger.info(
                            "funnel: added row-specific step count=%d subjects=%s",
                            row_specific_count, subjects[:5]
                        )
            except Exception as _funnel_exc:
                logger.warning("sparql funnel computation failed: %s", _funnel_exc)
        else:
            sa_eng = gold_engine if body.mode == "gold" else silver_engine
            pk_col = "_gold_id" if body.mode == "gold" else "_silver_id"
            sql_rows = _execute_proof_sql(row_pq.proof_query, sa_eng, _sa_text)
            subjects = [str(r[pk_col]) for r in sql_rows if r.get(pk_col) is not None]
            table = _parse_table_from_sql(row_pq.proof_query)
            walker = LineageWalker(
                gold_engine=gold_engine, silver_engine=silver_engine,
                bronze_engine=bronze_engine,
                base_uri=settings.RDF_BASE_URI, source_name=settings.RDF_SOURCE_NAME,
            )
            ids = [int(s) for s in subjects if s.isdigit()]
            if body.mode == "gold":
                chains = walker.walk_gold(table, ids)
                # Also walk FK-related gold entities (e.g. admissions for diagnoses).
                fk_chains = _walk_fk_chains(walker, gold_engine, table, sql_rows)
                for fc in fk_chains:
                    if fc.row_id is not None:
                        subjects.append(str(fc.row_id))
                chains.extend(fk_chains)
            else:
                chains = walker.walk_silver(table, ids)
    except Exception as exc:
        logger.warning("row proof lineage walk failed: %s", exc)
        caveats.append(ProofCaveat(type="LINEAGE_BROKEN", count=None, table=None, property_uri=None, reason=str(exc)))

    try:
        if chains and all(c.broken for c in chains):
            caveats.append(ProofCaveat(type="LINEAGE_BROKEN", count=len(chains), table=None, property_uri=None, reason="All lineage chains broken."))
            chains = []
    except Exception:
        chains = []

    # Mapping decisions — filter to grouping properties only
    try:
        class_uris = _class_uris_from_chains(chains)
        grouping_keys = set(body.row_grouping_values.keys())
        all_decisions = get_mapping_decisions(run_id=run_id, class_uris=class_uris, property_uris=set(), settings=settings, include_adjacent=True)
        decisions = [d for d in all_decisions if any(k in d.source for k in grouping_keys)] if grouping_keys else all_decisions
    except Exception:
        decisions = []

    row_proof = compose_row_proof(
        question=body.question,
        query=body.query,
        mode=body.mode,
        row_grouping_values=body.row_grouping_values,
        run_id=run_id,
        subjects=subjects,
        chains=chains,
        decisions=decisions,
        caveats=caveats,
        primary_table=table if body.mode in ("gold", "silver") else "",
        rdf_neighbourhood=rdf_neighbourhood,
        sparql_funnel=sparql_funnel,
        funnel_unsupported_reason=funnel_unsupported_reason,
        sparql_matches=sparql_matches,
    )
    return row_proof.model_dump()


def _extract_sparql_subjects(rows: list[dict]) -> list[str]:
    """Extract the first URI value from each SPARQL SELECT result row (primary subjects)."""
    subjects = []
    for row in rows:
        for val in row.values():
            if val and str(val).startswith("http"):
                subjects.append(str(val))
                break
    return subjects


def _walkable_subjects_for_rows(rows: list[dict]) -> tuple[list[str], Optional[Any]]:
    """Pick the walkable entity to anchor the lineage walk on.

    Among all URI-valued SELECT variables, choose the finest-grained (most
    distinct) one that resolves to a gold table. That is the fact entity the
    answer aggregates over — e.g. for "ICU stays per care unit" the projected
    subject is the categorical Careunit (no gold table), but ?icustayURI resolves
    to gold_icustay. Returns (subject URIs, GoldTableDefinition) or ([], None).
    """
    by_var: dict[str, list[str]] = {}
    for row in rows:
        for var, val in row.items():
            if val and str(val).startswith("http"):
                by_var.setdefault(var, []).append(str(val))
    best_subjects: list[str] = []
    best_def: Optional[Any] = None
    best_distinct = -1
    for uris in by_var.values():
        gdef = _gold_def_for_subjects(uris)
        if gdef is None:
            continue
        distinct = len(set(uris))
        if distinct > best_distinct:
            best_distinct, best_subjects, best_def = distinct, uris, gdef
    return best_subjects, best_def


def _extract_sparql_match_tuples(rows: list[dict]) -> list[dict]:
    """Return [{var_name: uri}, ...] capturing every URI-typed column per row."""
    result = []
    for row in rows:
        match = {
            var: str(val)
            for var, val in row.items()
            if val and str(val).startswith(("http://", "https://", "urn:"))
        }
        if match:
            result.append(match)
    return result


def _entity_type_from_uri(uri: str) -> str:
    """Extract readable entity type from a data URI path (penultimate non-numeric segment)."""
    parts = [p for p in uri.rstrip("/").split("/") if p]
    for part in reversed(parts):
        if not part.isdigit() and part not in ("http:", "https:", "data", "urn"):
            return part
    return parts[-1] if parts else uri


def _build_sparql_matches(
    sparql_query: str,
    match_tuples: list[dict],
    chains_by_uri: dict,
) -> list[dict] | None:
    """Assemble MatchRecord dicts from SPARQL solution tuples and lineage chains."""
    if not match_tuples:
        return None

    from semantic_mapper.prove.composer import infer_match_links
    from semantic_mapper.prove.query_rewriter import _extract_where_parts
    from semantic_mapper.prove.sparql_funnel import _decompose, _detect_primary_var, _strip_graph_block

    # Extract WHERE body for link inference and primary var detection
    where_body = ""
    try:
        _, where_body, _ = _extract_where_parts(sparql_query)
    except Exception:
        pass

    # Strip GRAPH wrapper so _decompose sees plain triple patterns
    _, inner = _strip_graph_block(where_body)
    where_body_parsed = inner if inner else where_body

    bound_vars = {var for mt in match_tuples for var in mt.keys()}

    # Infer property links between bound variables
    links = infer_match_links(where_body_parsed, bound_vars) if where_body_parsed else []

    # Detect primary role (first subject variable in WHERE clause)
    primary_role = ""
    if where_body_parsed:
        try:
            pv = _detect_primary_var(_decompose(where_body_parsed))
            primary_role = pv.lstrip("?") if pv else ""
        except Exception:
            pass
    if not primary_role and match_tuples:
        primary_role = next(iter(match_tuples[0].keys()), "")

    matches = []
    for idx, match_tuple in enumerate(match_tuples):
        entities = []
        for var, uri in match_tuple.items():
            chain = chains_by_uri.get(uri)
            bronze = (chain.bronze[0] if chain and chain.bronze else None)
            entities.append({
                "uri": uri,
                "role": var,
                "entity_type": _entity_type_from_uri(uri),
                "properties": dict(bronze.raw_values) if bronze else {},
                "bronze_source": bronze.table if bronze else "",
                "bronze_pk": dict(bronze.pk) if bronze else {},
            })
        matches.append({
            "match_index": idx + 1,
            "entities": entities,
            "links": links,
            "primary_role": primary_role or (entities[0]["role"] if entities else ""),
        })

    return matches or None


def _build_funnel_row_binding(sparql_query: str, match_tuples: list[dict]) -> str:
    """Build a SPARQL VALUES clause that binds non-primary, non-adjacent URI vars.

    Prepending this inside the funnel WHERE clause makes counts narrow at the step
    where the row-specific entity first appears, producing a unique funnel per result row.
    Non-adjacent means: not directly linked to the primary subject variable in the WHERE
    clause, so we skip junction/intermediate entities (which bind to count=1 immediately)
    and only bind the leaf/output entities (which narrow at the right depth).
    """
    if not match_tuples:
        return ""
    try:
        from semantic_mapper.prove.query_rewriter import _extract_where_parts
        from semantic_mapper.prove.sparql_funnel import _decompose, _detect_primary_var, _strip_graph_block
        from semantic_mapper.prove.composer import infer_match_links

        _, where_body, _ = _extract_where_parts(sparql_query)
        _, inner_wb = _strip_graph_block(where_body)
        where_body = inner_wb if inner_wb else where_body

        bound_vars = {k for mt in match_tuples for k in mt.keys()}
        links = infer_match_links(where_body, bound_vars)

        pv = _detect_primary_var(_decompose(where_body))
        primary_role = pv.lstrip("?") if pv else ""

        # Variables directly linked to/from primary (intermediate/junction entities)
        primary_adjacent: set[str] = set()
        for lnk in links:
            if lnk.get("from_role") == primary_role:
                primary_adjacent.add(lnk.get("to_role", ""))
            if lnk.get("to_role") == primary_role:
                primary_adjacent.add(lnk.get("from_role", ""))

        # Bind leaf vars (non-primary, non-adjacent) from the first match tuple
        mt0 = match_tuples[0]
        bind_vars = {k: v for k, v in mt0.items()
                     if k != primary_role and k not in primary_adjacent}

        # Fall back to all non-primary if nothing leaf-level detected
        if not bind_vars:
            bind_vars = {k: v for k, v in mt0.items() if k != primary_role}
        if not bind_vars:
            return ""

        if len(bind_vars) == 1:
            var, uri = next(iter(bind_vars.items()))
            return f"VALUES ?{var} {{ <{uri}> }}"
        vars_str = " ".join(f"?{k}" for k in bind_vars)
        vals_str = " ".join(f"<{v}>" for v in bind_vars.values())
        return f"VALUES ({vars_str}) {{ ({vals_str}) }}"
    except Exception:
        return ""


def _execute_proof_sql(query: str, engine: Any, text_fn: Any) -> list[dict]:
    try:
        with engine.connect() as conn:
            rows = conn.execute(text_fn(query)).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.warning("proof SQL query failed: %s", exc)
        return []


def _walk_fk_chains(walker: Any, gold_eng: Any, table: str, sql_rows: list) -> list:
    """Walk FK-related gold entities for GOLD mode row traces.

    For each ObjectProperty column in the primary gold table, looks up the
    related gold table rows by the FK value and returns their lineage chains.
    Capped at 50 related rows per FK link to keep the UI manageable.
    """
    if not sql_rows or not gold_definitions or ontology_graph is None:
        return []
    primary_def = next((d for d in gold_definitions if d.gold_table_name == table), None)
    if not primary_def:
        return []
    try:
        from semantic_mapper.rdf.triple_builder import _build_prop_range
        prop_range = _build_prop_range(ontology_graph)
        class_to_def = {d.primary_class_uri: d for d in gold_definitions if d.primary_class_uri}
        extra: list = []
        for spec in primary_def.column_specs:
            range_cls = prop_range.get(spec.property_uri)
            if not range_cls or range_cls == primary_def.primary_class_uri:
                continue
            rel_def = class_to_def.get(range_cls)
            if not rel_def:
                continue
            fk_vals = list({str(r[spec.gold_column_name]) for r in sql_rows if r.get(spec.gold_column_name) is not None})
            if not fk_vals:
                continue
            rel_rows = walker._fetch_by_column(gold_eng, rel_def.gold_table_name, spec.gold_column_name, fk_vals)
            rel_ids = [int(r["_gold_id"]) for r in rel_rows if r.get("_gold_id") is not None]
            if rel_ids:
                extra.extend(walker.walk_gold(rel_def.gold_table_name, rel_ids[:50]))
        return extra
    except Exception as exc:
        logger.debug("_walk_fk_chains: %s", exc)
        return []


def _parse_table_from_sql(query: str) -> str:
    import re
    m = re.search(r'\bFROM\s+[`"\']?(\w+)[`"\']?', query, re.IGNORECASE)
    return m.group(1) if m else ""


def _gold_def_for_subjects(subjects: list[str]) -> Optional[Any]:
    """Find the GoldTableDefinition whose entity name matches the URI entity_name."""
    if not subjects or not gold_definitions:
        return None
    base = settings.RDF_BASE_URI.rstrip("/") + "/"
    source_prefix = (settings.RDF_SOURCE_NAME.strip() + "/") if settings.RDF_SOURCE_NAME.strip() else ""
    for uri in subjects:
        path = uri
        if path.startswith(base):
            path = path[len(base):]
        if source_prefix and path.startswith(source_prefix):
            path = path[len(source_prefix):]
        entity_name = path.split("/")[0] if path else ""
        for d in gold_definitions:
            if d.gold_table_name[len("gold_"):] == entity_name:
                return d
    return None


def _class_uris_from_chains(chains: list) -> set[str]:
    gold_table_to_uri = {d.gold_table_name: d.primary_class_uri for d in gold_definitions}
    uris: set[str] = set()
    for chain in chains:
        if chain.gold.table:
            uri = gold_table_to_uri.get(chain.gold.table)
            if uri:
                uris.add(uri)
    return uris


@app.get("/api/query/suggestions")
def api_query_suggestions() -> Dict[str, Any]:
    """Return example questions derived from the current gold definitions' structure."""
    from rdflib import RDFS, URIRef
    from semantic_mapper.webapp.suggestions import build_suggestions

    classes = [
        d.gold_table_name[len("gold_"):]
        for d in gold_definitions
        if d.gold_table_name.startswith("gold_")
    ]
    if not classes:
        return {"suggestions": [], "schema_loaded": False}

    class_labels: Dict[str, str] = {}
    for d in gold_definitions:
        for g in list(_catalog_graphs.values()):
            label = g.value(URIRef(d.primary_class_uri), RDFS.label)
            if label:
                class_labels[d.primary_class_uri] = str(label)
                break

    prop_ranges: Dict[str, str] = {}
    for d in gold_definitions:
        for spec in d.column_specs:
            for g in list(_catalog_graphs.values()):
                rng = g.value(URIRef(spec.property_uri), RDFS.range)
                if rng is not None:
                    prop_ranges[spec.property_uri] = str(rng)
                    break
    suggestions = build_suggestions(gold_definitions, prop_ranges, class_labels)
    return {"suggestions": suggestions, "schema_loaded": True, "classes": classes}


class MappingAction(BaseModel):
    source_table: str
    source_column: str
    confirmed_uri: Optional[str] = None  # required for confirm, ignored for reject


@app.get("/api/mappings")
def api_mappings() -> Dict[str, Any]:
    """Return all column mappings from the report, grouped by table."""
    current_report = _load_report()
    result = []
    for table in current_report.tables:
        cols = [
            {
                "source_column":         col.source_column,
                "source_data_type":      col.source_data_type,
                "status":                col.status.value,
                "confidence_score":      col.confidence_score,
                "mapped_property_uri":   col.mapped_property_uri,
                "mapped_property_label": col.mapped_property_label,
                "rationale":             col.rationale,
                "evidence_trail": [
                    {"strategy": ev.strategy, "score": ev.score, "rationale": ev.rationale, "mapped_uri": ev.mapped_uri}
                    for ev in col.evidence_trail
                ],
            }
            for col in table.column_mappings
        ]
        if cols:
            result.append({"source_table": table.source_table, "columns": cols})
    pending_total = sum(
        1 for t in current_report.tables
        for c in t.column_mappings
        if c.status == MappingStatus.PENDING_REVIEW
    )
    return {
        "tables": result,
        "total": sum(len(t["columns"]) for t in result),
        "pending_total": pending_total,
    }


def _load_report() -> SemanticMappingReport:
    with open(_mapping_report_path, "r", encoding="utf-8") as fh:
        return SemanticMappingReport.model_validate(json.load(fh))


def _save_report(r: SemanticMappingReport) -> None:
    with open(_mapping_report_path, "w", encoding="utf-8") as fh:
        json.dump(r.model_dump(), fh, indent=2)


def _update_report_column(
    source_table: str,
    source_column: str,
    new_status: MappingStatus,
    confirmed_uri: Optional[str] = None,
) -> SemanticMappingReport:
    """Reload report, update one column status, save back to disk, return new report."""
    r = _load_report()
    updated_tables = []
    for table in r.tables:
        if table.source_table != source_table:
            updated_tables.append(table)
            continue
        updated_cols = []
        for col in table.column_mappings:
            if col.source_column != source_column:
                updated_cols.append(col)
                continue
            kwargs: Dict[str, Any] = {"status": new_status}
            if confirmed_uri is not None:
                kwargs["mapped_property_uri"] = confirmed_uri
            updated_cols.append(col.model_copy(update=kwargs))
        updated_tables.append(table.model_copy(update={"column_mappings": updated_cols}))

    pending_count = sum(
        1
        for t in updated_tables
        for c in t.column_mappings
        if c.status == MappingStatus.PENDING_REVIEW
    )
    new_report = r.model_copy(update={"tables": updated_tables, "total_pending_review": pending_count})
    _save_report(new_report)
    return new_report


@app.post("/api/mappings/confirm")
def api_mappings_confirm(body: MappingAction) -> Dict[str, str]:
    """Confirm a pending mapping, update the report on disk, write to corrections store."""
    if not body.confirmed_uri:
        raise HTTPException(status_code=400, detail="confirmed_uri is required")

    new_report = _update_report_column(
        body.source_table, body.source_column,
        MappingStatus.CONFIRMED, confirmed_uri=body.confirmed_uri,
    )

    # Find the column's data type for the corrections store entry
    source_data_type = None
    for table in new_report.tables:
        if table.source_table == body.source_table:
            for col in table.column_mappings:
                if col.source_column == body.source_column:
                    source_data_type = col.source_data_type
                    break

    # Overwrite any prior AUTO entries so the human choice always wins next run
    _corrections_store.overwrite_human_correction(
        source_table=body.source_table,
        source_column=body.source_column,
        confirmed_uri=body.confirmed_uri,
        source_data_type=source_data_type,
        ontology_source=new_report.ontology_uri,
        db_source=new_report.database_name,
    )
    _touch_correction_ts()
    return {"status": "confirmed"}


class PipelineRunRequest(BaseModel):
    phases: str = "all"  # "all" | "silver,gold,rdf" | any comma-separated subset
    threshold: Optional[float] = None  # AUTO_CONFIRM_THRESHOLD override (0.0–1.0)
    ontology_path: Optional[str] = None  # override ONTOLOGY_PATH for this run
    gold_mode: Optional[int] = None  # 1=compat, 2=semantic, 3=additive views
    ontology_generator: Optional[str] = None  # "full" | "iterative" (RIGOR)


@app.post("/api/pipeline/run")
def api_pipeline_run(body: PipelineRunRequest) -> Dict[str, Any]:
    """Start the pipeline in a background thread. Rejects if already running."""
    if body.threshold is not None and not (0.0 <= body.threshold <= 1.0):
        raise HTTPException(status_code=400, detail="threshold must be between 0.0 and 1.0")

    started_at = datetime.now(timezone.utc).isoformat()
    with _pipeline_lock:
        if _pipeline["status"] == "running":
            raise HTTPException(status_code=409, detail="Pipeline is already running")
        run_id = str(uuid.uuid4())
        _pipeline["status"] = "running"
        _pipeline["run_id"] = run_id
        _pipeline["log"] = []
        _pipeline["generated_ontology"] = None
        _pipeline["return_code"] = None
        _pipeline["started_at"] = started_at
        _pipeline["phases"] = body.phases
        _pipeline["threshold"] = body.threshold

    cmd = [sys.executable, str(ROOT / "main.py")]
    if _active_project:
        # Project is active — use --project so the subprocess calls resolve_project()
        # and gets the full validated config (not just DATABASE_URL).
        cmd += ["--project", _active_project]
    elif _args.env:
        # Legacy: fall back to --env when no project is selected
        _pipeline_env_file = _args.env
        if _active_database:
            _matched = _find_env_file_for_database(_active_database)
            if _matched:
                _pipeline_env_file = _matched
        cmd += ["--env", _pipeline_env_file]
    if body.phases != "all":
        cmd += ["--phases", body.phases]

    # Pass overrides via environment variable — pydantic-settings picks them up automatically
    env = os.environ.copy()
    if body.threshold is not None:
        env["AUTO_CONFIRM_THRESHOLD"] = str(body.threshold)
    active_ont = body.ontology_path or _active_ontology_path
    if active_ont:
        env["ONTOLOGY_PATH"] = active_ont
    if not _active_project and _active_database:
        # Only inject DATABASE_URL override when no project is active;
        # when a project is active, --project sets all DB URLs authoritatively.
        env["DATABASE_URL"] = _override_db_url(_active_database)
    if body.gold_mode is not None:
        env["GOLD_MODE"] = str(body.gold_mode)
    if body.ontology_generator is not None:
        env["ONTOLOGY_GENERATOR"] = body.ontology_generator
    # Model selection: a project that pins its own llm_model (e.g. a local Ollama
    # backend) wins — do NOT force --model, which would override it with the
    # webapp's persisted cloud selection. apply_project_to_env() in the spawned
    # subprocess sets LLM_MODEL from the project. Otherwise pass the UI's choice.
    _project_pins_model = False
    if _active_project:
        try:
            from semantic_mapper.project.resolver import resolve_project
            _project_pins_model = bool(resolve_project(_active_project).llm_model)
        except Exception:
            _project_pins_model = False
    if not _project_pins_model:
        cmd += ["--model", _active_model]
    env["QUERY_PROVIDER"] = settings.QUERY_PROVIDER
    if settings.QUERY_PROVIDER == "claude_code":
        env["QUERY_ANTHROPIC_MODEL"] = settings.QUERY_ANTHROPIC_MODEL
    env["PIPELINE_RUN_ID"] = run_id

    ont_hash = _compute_ontology_hash()
    _snapshot_ontology(run_id, ont_hash)

    threading.Thread(
        target=_run_pipeline_thread,
        args=(cmd, run_id, started_at, body.phases, body.threshold, ont_hash, env, body.gold_mode),
        daemon=True,
    ).start()
    return {"status": "started", "run_id": run_id}


@app.post("/api/pipeline/stop")
def api_pipeline_stop() -> Dict[str, Any]:
    """Terminate the running pipeline subprocess."""
    with _pipeline_lock:
        proc = _pipeline.get("proc")
        if proc is None or _pipeline["status"] != "running":
            raise HTTPException(status_code=409, detail="No pipeline is currently running")
        proc.terminate()
    return {"status": "stopping"}


@app.get("/api/pipeline/status")
def api_pipeline_status() -> Dict[str, Any]:
    with _pipeline_lock:
        return {
            "status": _pipeline["status"],
            "run_id": _pipeline["run_id"],
            "return_code": _pipeline["return_code"],
            "log_lines": len(_pipeline["log"]),
            "pipeline_state": _current_pipeline_state(),
            "generated_ontology": _pipeline.get("generated_ontology"),
        }


@app.get("/api/pipeline/state")
def api_pipeline_state() -> Dict[str, Any]:
    """Return the current pipeline lifecycle state (issue #47)."""
    return _current_pipeline_state()


@app.get("/api/pipeline/stream")
async def api_pipeline_stream(cursor: int = 0) -> StreamingResponse:
    """SSE endpoint: stream log lines from `cursor` onward, then signal completion."""
    async def _generate():
        pos = cursor
        while True:
            with _pipeline_lock:
                new_lines = _pipeline["log"][pos:]
                pos += len(new_lines)
                status = _pipeline["status"]
                done = status in ("done", "failed") and pos >= len(_pipeline["log"])

            for line in new_lines:
                yield f"data: {json.dumps({'line': line})}\n\n"

            if done:
                yield f"data: {json.dumps({'done': True, 'status': status, 'return_code': _pipeline['return_code'], 'generated_ontology': _pipeline.get('generated_ontology')})}\n\n"
                return

            await asyncio.sleep(0.15)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/inspection")
def api_inspection() -> Dict[str, Any]:
    """Return the cached inspection report (output/inspection_report.json)."""
    report_path = ROOT / "output" / "inspection_report.json"
    if not report_path.exists():
        return {"tables": [], "stale": True, "message": "No inspection report found. Run the inspect phase first."}
    import json as _json
    with open(report_path) as f:
        tables = _json.load(f)
    # stale if older than the DB or if a run is in progress
    with _pipeline_lock:
        running = _pipeline.get("status") == "running"
    return {
        "tables": tables,
        "stale": running,
        "message": None,
        "active_database": _current_db_name(),
    }


def _masked_db_url(db_name: str = "") -> str:
    """Return the DATABASE_URL (optionally with db_name replaced) with password masked."""
    import re as _re
    url = _override_db_url(db_name) if db_name else settings.DATABASE_URL
    return _re.sub(r"(:)[^:@]+(@)", r"\1****\2", url)


@app.get("/api/datasources")
def api_datasources() -> Dict[str, Any]:
    """List accessible MySQL databases and return the currently active one."""
    databases = _list_available_databases()
    active = _current_db_name()
    # Ensure the active db appears in the list even if listing failed
    if active and active not in databases:
        databases.insert(0, active)
    return {
        "databases": databases,
        "active": active,
        "connection_url": _masked_db_url(active),
    }


class DataSourceSelectRequest(BaseModel):
    database: str


@app.post("/api/datasources/select")
def api_datasources_select(body: DataSourceSelectRequest) -> Dict[str, Any]:
    """Switch the working database. Marks any cached inspection report as stale."""
    global _active_database
    if not body.database:
        raise HTTPException(status_code=400, detail="database name is required")
    _active_database = body.database
    _save_app_state()
    # Remove the stale inspection report so the UI reflects the new source
    report_path = ROOT / "output" / "inspection_report.json"
    if report_path.exists():
        report_path.unlink()
    return {"status": "ok", "active": body.database, "connection_url": _masked_db_url(body.database)}


@app.get("/api/projects")
def api_projects_list() -> Dict[str, Any]:
    """List available projects and which one is currently active."""
    from semantic_mapper.project.resolver import list_projects
    return {
        "projects": list_projects(),
        "active": _active_project,
    }


class ProjectSelectRequest(BaseModel):
    project: str


@app.post("/api/projects/select")
def api_projects_select(body: ProjectSelectRequest) -> Dict[str, Any]:
    """Activate a project. Applies all dataset-specific settings for this session."""
    from semantic_mapper.project.resolver import OntologyMissingWarning
    global _active_project, _active_database, _active_ontology_path
    if not body.project:
        raise HTTPException(status_code=400, detail="project name is required")

    warning: Optional[str] = None
    try:
        config = _apply_active_project(body.project)
    except OntologyMissingWarning as exc:
        # Activate the project without an ontology — inspect/map phases still work.
        # Return a warning and the list of available OWL files so the UI can let
        # the user pick one without blocking.
        config = exc.partial_config
        warning = str(exc)
        _apply_active_project_from_config(config)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, Exception) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _active_project = body.project
    _active_database = config.bronze_database_url.rstrip("/").rsplit("/", 1)[-1]
    _active_ontology_path = config.ontology_path
    report_path = ROOT / "output" / "inspection_report.json"
    if report_path.exists():
        report_path.unlink()

    # Rebuild the query engines from the newly-activated project's mapping report
    # and ontology. Selecting a project only patches `settings`; without this the
    # SPARQL and gold engines keep the previous project's schema (and ontology
    # namespace), so every query fails or silently returns 0 rows.
    n_defs = _reinit_engine()
    logging.info(
        "project select '%s': rebuilt engines with %d gold definitions",
        body.project, n_defs,
    )

    _save_app_state()

    resp: Dict[str, Any] = {
        "status": "ok",
        "active": body.project,
        "resolved": {
            "bronze_database_url": config.bronze_database_url,
            "silver_database_url": config.silver_database_url,
            "gold_database_url":   config.gold_database_url,
            "ontology_path":       config.ontology_path,
            "rdf_dataset_name":    config.rdf_dataset_name,
            "named_graph_uri":     config.named_graph_uri,
        },
    }
    if warning:
        owl_dir = ROOT / "owl"
        available = sorted(
            str(p.relative_to(ROOT))
            for p in owl_dir.glob("*.owl")
            if p.name != "fm_annotations.owl"
        ) if owl_dir.exists() else []
        resp["warning"] = warning
        resp["available_ontologies"] = available
    return resp


@app.get("/api/corrections")
def api_corrections() -> Dict[str, Any]:
    """Return all rows from the corrections store."""
    rows = _corrections_store.fetch_all()
    return {"corrections": rows, "total": len(rows)}


@app.post("/api/corrections/prune")
def api_corrections_prune() -> Dict[str, Any]:
    """Delete all non-winning corrections rows, keeping one winner per column group."""
    deleted = _corrections_store.prune_to_winning()
    return {"status": "pruned", "deleted": deleted}


@app.delete("/api/corrections")
def api_corrections_delete_all() -> Dict[str, Any]:
    """Delete every row in the corrections store."""
    _corrections_store.clear()
    _touch_correction_ts()
    return {"status": "cleared"}


@app.delete("/api/corrections/by-dataset")
def api_corrections_delete_by_dataset(
    ontology_source: str,
    db_source: str,
) -> Dict[str, Any]:
    """Delete all corrections for a specific ontology + database pair."""
    deleted = _corrections_store.delete_by_ontology(ontology_source, db_source)
    _touch_correction_ts()
    return {"status": "cleared", "deleted": deleted, "ontology_source": ontology_source, "db_source": db_source}


@app.delete("/api/corrections/{correction_id}")
def api_corrections_delete(correction_id: int) -> Dict[str, Any]:
    """Delete a single corrections store entry by id."""
    deleted = _corrections_store.delete_by_id(correction_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Correction not found")
    _touch_correction_ts()
    return {"status": "deleted", "id": correction_id}


@app.get("/api/pipeline/history")
def api_pipeline_history() -> Dict[str, Any]:
    """Return the 50 most recent completed pipeline runs."""
    con = sqlite3.connect(str(_pipeline_runs_db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT run_id, started_at, finished_at, status, return_code, phases, threshold, ontology_hash, gold_mode "
        "FROM pipeline_runs ORDER BY started_at DESC LIMIT 50"
    ).fetchall()
    con.close()
    return {"runs": [dict(r) for r in rows]}


@app.get("/api/pipeline/runs/{run_id}/log")
def api_pipeline_run_log(run_id: str) -> Dict[str, Any]:
    """Return the full log for a completed pipeline run."""
    con = sqlite3.connect(str(_pipeline_runs_db_path))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT run_id, started_at, finished_at, status, return_code, phases, threshold, log_content, gold_mode "
        "FROM pipeline_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    con.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    d = dict(row)
    d["log_lines"] = d.pop("log_content", "").split("\n") if d.get("log_content") else []
    return d


def _ontology_status() -> Dict[str, Any]:
    """Current state of the loaded ontology — path configured, files, load status."""
    # Use _active_ontology_path so the UI reflects the currently loaded file,
    # not the static .env value (which never changes at runtime).
    path = _active_ontology_path or settings.ONTOLOGY_PATH or ""
    resolved = Path(path) if Path(path).is_absolute() else ROOT / path if path else None
    # Derive the directory that holds the OWL files so we can add mtime + full path.
    ont_dir: Optional[Path] = None
    if resolved is not None:
        ont_dir = resolved.parent if resolved.suffix.lower() in {".owl", ".rdf", ".ttl", ".xml"} else resolved

    def _file_entry(e: Dict[str, Any]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"filename": e["filename"], "enabled": e.get("enabled", True)}
        if ont_dir is not None:
            fp = ont_dir / e["filename"]
            try:
                rel = str(fp.relative_to(ROOT))
            except ValueError:
                rel = str(fp)
            entry["path"] = rel
            if fp.exists():
                from datetime import datetime as _dt
                entry["last_modified"] = _dt.fromtimestamp(fp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        return entry

    return {
        "configured_path": path,
        "path_exists": resolved.exists() if resolved else False,
        "loaded": ontology_graph is not None,
        "files": [_file_entry(e) for e in _catalog_entries],
        "active_write_target": _active_write_target,
    }


@app.post("/api/ui-reset")
def api_ui_reset() -> Dict[str, Any]:
    """Reset all server-side UI state: active database, active ontology path, and write target.

    Called by the frontend on page load so a browser refresh always starts from a clean slate
    rather than leftover state from a previous session.
    """
    global _active_database, _active_ontology_path, _active_write_target, _catalog_path, _catalog_state_path
    # Restore persisted selections (DB + ontology) rather than blanking them.
    _load_app_state()
    _active_write_target = ""
    with _catalog_lock:
        _catalog_entries.clear()
        _catalog_graphs.clear()
        _catalog_path = None
        _catalog_state_path = None
    # Pass the restored path explicitly — _init_catalog(path=None) ignores _active_ontology_path
    # and falls back to settings.ONTOLOGY_PATH, which would lose the persisted selection.
    _init_catalog(_active_ontology_path or None)
    return {"status": "reset", "ontology_status": _ontology_status()}


@app.get("/api/ontology")
def api_ontology() -> Dict[str, Any]:
    """Return parsed ontology: all classes with their properties, plus orphan properties."""
    status = _ontology_status()
    if ontology_graph is None:
        return {"classes": [], "orphan_properties": [], "status": status}
    from rdflib import OWL, RDF, RDFS, URIRef

    # Collect all named classes
    class_uris: set = set()
    for s in ontology_graph.subjects(RDF.type, OWL.Class):
        if isinstance(s, URIRef):
            class_uris.add(s)
    for s in ontology_graph.subjects(RDF.type, RDFS.Class):
        if isinstance(s, URIRef):
            class_uris.add(s)

    # Build a map of class URI → list of NamedIndividuals
    individuals_by_class: Dict[str, list] = {}
    for ind in ontology_graph.subjects(RDF.type, OWL.NamedIndividual):
        if not isinstance(ind, URIRef):
            continue
        for cls_uri_ref in ontology_graph.objects(ind, RDF.type):
            if not isinstance(cls_uri_ref, URIRef) or cls_uri_ref == OWL.NamedIndividual:
                continue
            cls_key = str(cls_uri_ref)
            ind_labels = list(ontology_graph.objects(ind, RDFS.label))
            ind_comments = list(ontology_graph.objects(ind, RDFS.comment))
            ind_local = str(ind).split('#')[-1] if '#' in str(ind) else str(ind).split('/')[-1]
            entry = {
                "uri": str(ind),
                "label": str(ind_labels[0]) if ind_labels else ind_local,
                "comment": str(ind_comments[0]) if ind_comments else "",
            }
            individuals_by_class.setdefault(cls_key, []).append(entry)

    class_map: Dict[str, Any] = {}
    for cls_uri in class_uris:
        uri_str = str(cls_uri)
        labels = list(ontology_graph.objects(cls_uri, RDFS.label))
        comments = list(ontology_graph.objects(cls_uri, RDFS.comment))
        parent_uris = [
            str(p) for p in ontology_graph.objects(cls_uri, RDFS.subClassOf)
            if isinstance(p, URIRef)
        ]
        inds = sorted(individuals_by_class.get(uri_str, []), key=lambda i: i["label"].lower())
        class_map[uri_str] = {
            "uri": uri_str,
            "label": str(labels[0]) if labels else "",
            "comment": str(comments[0]) if comments else "",
            "parent_uris": parent_uris,
            "properties": [],
            "instances": inds,
            "source_file": _ontology_sources.get(uri_str, ""),
        }

    # Assign properties to classes via rdfs:domain
    all_props = (
        [(p, "datatype") for p in ontology_graph.subjects(RDF.type, OWL.DatatypeProperty)]
        + [(p, "object") for p in ontology_graph.subjects(RDF.type, OWL.ObjectProperty)]
    )
    orphan_properties = []
    for prop, prop_type in all_props:
        if not isinstance(prop, URIRef):
            continue
        uri_str = str(prop)
        labels = list(ontology_graph.objects(prop, RDFS.label))
        comments = list(ontology_graph.objects(prop, RDFS.comment))
        domains = [str(d) for d in ontology_graph.objects(prop, RDFS.domain) if isinstance(d, URIRef)]
        ranges = [str(r) for r in ontology_graph.objects(prop, RDFS.range) if isinstance(r, URIRef)]
        prop_obj = {
            "uri": uri_str,
            "label": str(labels[0]) if labels else "",
            "comment": str(comments[0]) if comments else "",
            "type": prop_type,
            "domain_uris": domains,
            "range_uris": ranges,
            "source_file": _ontology_sources.get(uri_str, ""),
        }
        if domains:
            for domain_uri in domains:
                if domain_uri in class_map:
                    class_map[domain_uri]["properties"].append(prop_obj)
                else:
                    orphan_properties.append(prop_obj)
        else:
            orphan_properties.append(prop_obj)

    classes = sorted(class_map.values(), key=lambda c: (c["label"] or c["uri"]).lower())
    for cls in classes:
        cls["properties"].sort(key=lambda p: (p["label"] or p["uri"]).lower())
    orphan_properties.sort(key=lambda p: (p["label"] or p["uri"]).lower())
    return {"classes": classes, "orphan_properties": orphan_properties, "status": _ontology_status()}


def _build_suggestion_why(s: Dict[str, Any]) -> str:
    """Return a rule-based explanation of why this column is a suggestion."""
    col = s["column"]
    table = s["table"]
    best_score = s.get("_best_score", 0.0)
    candidates = s.get("candidates", [])
    is_fk = s.get("is_fk_like", False)

    parts = []
    if is_fk:
        parts.append(
            f"'{col}' looks like a reference to another entity (foreign-key pattern)."
        )
    if candidates and best_score > 0:
        top = candidates[0]
        top_local = top["uri"].split("#")[-1].split("/")[-1]
        parts.append(
            f"The mapping engine's best match was '{top_local}' at {best_score:.0%} — "
            f"below the auto-confirm threshold, so it was left for human review."
        )
    elif not candidates:
        parts.append(
            f"No existing ontology property matched '{col}' — it has no mapping candidate at all."
        )
    else:
        parts.append(
            f"The best match scored {best_score:.0%}, which is too low to confirm automatically."
        )
    return " ".join(parts)


def _enrich_suggestion_labels(
    prop_suggestions: List[Dict[str, Any]],
    cfg: Any,
    known_tables: List[str],
) -> Tuple[int, int]:
    """Call gpt-4o-mini once to generate label, comment, and a practical hint per suggestion.

    Mutates each suggestion dict in-place. Falls back silently on any error.
    known_tables: list of source table names in the schema (for FK/missing-table hints).
    Returns (prompt_tokens, completion_tokens).
    """
    import json as _json
    import re as _re2

    # Strip raw_ prefix to get the bare entity names the LLM should reason about
    bare_tables = [_re2.sub(r"^raw_", "", t, flags=_re2.IGNORECASE) for t in known_tables]

    items = [
        {
            "key": s["id"],
            "table": _re2.sub(r"^raw_", "", s["table"], flags=_re2.IGNORECASE),
            "column": s["column"],
            "data_type": s.get("_data_type", "TEXT"),
            "is_fk_like": s.get("is_fk_like", False),
        }
        for s in prop_suggestions
    ]
    prompt = (
        "You are an ontology engineer reviewing unmapped database columns.\n"
        f"The schema contains these entities: {', '.join(bare_tables)}.\n\n"
        "For each column, return:\n"
        "  label   — short rdfs:label (2–5 words, Title Case)\n"
        "  comment — one sentence describing the property semantically\n"
        "  hint    — one or two sentences of practical advice to the user about whether and how "
        "to model this column in OWL. Consider: Is there a matching entity table to link to as "
        "an ObjectProperty? If no matching table exists, should it stay as a literal? Should the "
        "user skip or defer this column?\n\n"
        "Return a JSON object mapping each 'key' to {\"label\":\"...\",\"comment\":\"...\",\"hint\":\"...\"}.\n\n"
        "Columns:\n"
        + _json.dumps(items, indent=2)
    )
    from semantic_mapper.core.llm import make_openai_client
    client = make_openai_client(cfg)
    resp = client.chat.completions.create(
        model=cfg.OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        timeout=30,
    )
    prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
    completion_tokens = resp.usage.completion_tokens if resp.usage else 0
    enriched: Dict[str, Any] = _json.loads(resp.choices[0].message.content)

    for s in prop_suggestions:
        meta = enriched.get(s["id"])
        if not meta:
            continue
        label = str(meta.get("label", "")).strip()
        comment = str(meta.get("comment", "")).strip()
        hint = str(meta.get("hint", "")).strip()
        if not label:
            continue
        stub = s["owl_stub"]
        stub = _re2.sub(
            r'<rdfs:label xml:lang="en">[^<]*</rdfs:label>',
            f'<rdfs:label xml:lang="en">{label}</rdfs:label>',
            stub,
        )
        if comment:
            stub = _re2.sub(
                r'<rdfs:comment xml:lang="en">[^<]*</rdfs:comment>',
                f'<rdfs:comment xml:lang="en">{comment}</rdfs:comment>',
                stub,
            )
        s["owl_stub"] = stub
        s["label"] = label
        s["comment"] = comment
        if hint:
            s["hint"] = hint
    return prompt_tokens, completion_tokens


_SUGGESTIONS_CACHE_PATH = ROOT / "output" / "suggestions_cache.json"


def _suggestions_cache_key() -> str:
    """A string that uniquely identifies the current ontology + inspection state."""
    import json as _jc
    ont_hash = _compute_ontology_hash() or "none"
    insp_path = ROOT / "output" / "inspection_report.json"
    insp_mtime = str(insp_path.stat().st_mtime) if insp_path.exists() else "none"
    return f"{ont_hash}::{insp_mtime}"


def _load_suggestions_cache() -> Optional[Dict[str, Any]]:
    if not _SUGGESTIONS_CACHE_PATH.exists():
        return None
    try:
        import json as _jc
        data = _jc.loads(_SUGGESTIONS_CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("cache_key") == _suggestions_cache_key():
            return data
    except Exception:
        pass
    return None


def _save_suggestions_cache(suggestions: list) -> None:
    import json as _jc
    _SUGGESTIONS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SUGGESTIONS_CACHE_PATH.write_text(
        _jc.dumps({"cache_key": _suggestions_cache_key(), "suggestions": suggestions}, indent=2),
        encoding="utf-8",
    )


@app.get("/api/ontology/suggestions")
def api_ontology_suggestions(rebuild: bool = False) -> Dict[str, Any]:
    """Analyse the mapping report for gaps and return property/class stubs as suggestions.

    Returns the cached result unless rebuild=true is passed.
    """
    if not rebuild:
        cached = _load_suggestions_cache()
        if cached is not None:
            slist = cached["suggestions"]
            return {"suggestions": slist, "total": len(slist), "cached": True}

    current_report = _load_report()
    suggestions = []

    # Build set of property local names already in the ontology — never re-suggest these
    _existing_prop_locals: set = set()
    if ontology_graph is not None:
        from rdflib import OWL as _OWL2, RDF as _RDF2
        for _ptype in (_OWL2.DatatypeProperty, _OWL2.ObjectProperty):
            for _p in ontology_graph.subjects(_RDF2.type, _ptype):
                _existing_prop_locals.add(str(_p).split("#")[-1].split("/")[-1].lower())

    # Common qualifier words that distinguish e.g. hasMaiden from hasMaidenName —
    # if the only difference between a proposed name and an existing name is one of
    # these suffixes, the concept is already covered.
    _PROP_QUALIFIERS = {
        "name", "date", "time", "datetime", "value", "id", "code", "type",
        "number", "flag", "text", "uri", "ref", "key", "label", "description",
        "status", "reason", "amount", "cost", "count", "rate",
    }

    # Generic stems that the full_generator qualifies with a class prefix — if the
    # proposed name's stem is one of these AND any existing property ends with that
    # stem, the concept is already covered by the class-qualified version.
    # e.g. hasCode proposed, hasConditionCode exists → stem "code" matches suffix → suppress.
    _GENERIC_STEMS = {
        "code", "type", "status", "class", "name", "date", "id",
        "description", "reason", "cost", "amount",
    }

    def _prop_already_covered(proposed_lower: str) -> bool:
        """Return True if an existing ontology property already covers this concept.

        Handles three cases:
          1. Exact match:          hasLatitude == hasLatitude
          2. Prefix match:         proposed is a prefix of existing — the existing
             property IS the full semantic name for an abbreviated column.
             e.g. hasLat → hasLatitude, hasLon → hasLongitude,
                  hasStart → hasStartDate, hasMaiden → hasMaidenName.
             Reverse direction (proposed extends existing) only suppresses when the
             extra suffix is a known qualifier word.
          3. Class-qualified stem: proposed stem is generic and an existing property
             ends with that stem (hasCode → hasConditionCode, hasEncounterCode, …).
        """
        if proposed_lower in _existing_prop_locals:
            return True
        for ep in _existing_prop_locals:
            # Case 2a: existing is the full expansion of an abbreviated proposed name.
            # No restriction on the suffix — any extension means the concept is named.
            if ep.startswith(proposed_lower) and len(ep) > len(proposed_lower):
                return True
            # Case 2b: proposed extends existing — only suppress for known qualifier suffixes.
            if proposed_lower.startswith(ep) and proposed_lower[len(ep):] in _PROP_QUALIFIERS:
                return True
        # Case 3: proposed stem is generic and already modelled under a class-qualified name.
        stem = proposed_lower[3:] if proposed_lower.startswith("has") else proposed_lower
        if stem in _GENERIC_STEMS:
            if any(ep.endswith(stem) for ep in _existing_prop_locals):
                return True
        return False

    # Build set of class local names already in the ontology — never re-suggest these
    _existing_class_locals: set = set()
    if ontology_graph is not None:
        from rdflib import OWL as _OWL2, RDF as _RDF2
        for _c in ontology_graph.subjects(_RDF2.type, _OWL2.Class):
            _existing_class_locals.add(str(_c).split("#")[-1].split("/")[-1].lower())

    def _xsd_range(data_type: str) -> str:
        t = (data_type or "").upper()
        if "INT" in t:
            return "xsd:integer"
        if "DECIMAL" in t or "NUMERIC" in t:
            return "xsd:decimal"
        if "FLOAT" in t or "DOUBLE" in t:
            return "xsd:double"
        if "BOOLEAN" in t or "BIT" in t:
            return "xsd:boolean"
        if "DATETIME" in t:
            return "xsd:dateTime"
        if "DATE" in t:
            return "xsd:date"
        return "xsd:string"

    def _camel(name: str) -> str:
        return "".join(w.capitalize() for w in name.replace("-", "_").split("_") if w)

    import re as _re

    def _guess_class_for_table(table_name: str) -> Dict[str, Any]:
        """Best-guess OWL class for the domain of properties from this table.

        Priority:
        1. rdfs:domain of a confirmed/mapped property already in this table's report.
        2. Ontology class whose local name matches the stripped table name
           (handles raw_ prefix, plurals, and substring containment).
        3. A class suggestion already queued for the same table in this batch.
        4. Derive a local name from the table name (strip raw_, camelCase).
        """
        from rdflib import OWL as _OWL, RDF as _RDF, RDFS as _RDFS, URIRef as _URIRef

        # 1. Use rdfs:domain of an already-confirmed mapping for this table
        if ontology_graph is not None:
            for tbl in current_report.tables:
                if tbl.source_table != table_name:
                    continue
                for col in tbl.column_mappings:
                    if col.status == MappingStatus.CONFIRMED \
                            and getattr(col, "mapped_property_uri", None):
                        domains = list(ontology_graph.objects(
                            _URIRef(col.mapped_property_uri), _RDFS.domain
                        ))
                        if domains:
                            dom_uri = str(domains[0])
                            local = dom_uri.split("#")[-1].split("/")[-1]
                            return {"uri": dom_uri, "local_name": local}

        # 2. Name-match against ontology classes
        if ontology_graph is not None:
            stripped = _re.sub(r"^raw_", "", table_name, flags=_re.IGNORECASE)
            # Build a set of candidate normalised forms to match against
            # e.g. raw_encounters → encounters, encounter (de-pluralised)
            norm_forms = {stripped.lower(), stripped.rstrip("s").lower()}

            # Collect all class local names once
            all_classes = [
                (str(uri), str(uri).split("#")[-1].split("/")[-1])
                for uri in ontology_graph.subjects(_RDF.type, _OWL.Class)
            ]

            # Exact match (case-insensitive, with/without trailing s)
            for uri, local in all_classes:
                if local.lower() in norm_forms:
                    return {"uri": uri, "local_name": local}

            # Substring: class name appears inside the stripped table segment
            for uri, local in all_classes:
                if local.lower() in stripped.lower():
                    return {"uri": uri, "local_name": local}

        # 3. Batch class suggestion already queued for the same table
        for s in suggestions:
            if s.get("type") == "class" and s.get("table") == table_name:
                return {"uri": None, "local_name": s["proposed_name"]}

        # 4. Derive from table name (strip raw_, camelCase)
        stripped = _re.sub(r"^raw_", "", table_name, flags=_re.IGNORECASE)
        return {"uri": None, "local_name": "".join(p.capitalize() for p in stripped.split("_"))}

    # ── Load FK reference data from inspection report (used in both loops) ──────
    _insp_fk_ref: Dict[str, Dict[str, str]] = {}   # {table: {col: ref_table}}
    _insp_path_early = ROOT / "output" / "inspection_report.json"
    if _insp_path_early.exists():
        import json as _json_fk
        for _t in _json_fk.loads(_insp_path_early.read_text()):
            _insp_fk_ref[_t["table_name"]] = {
                fk["column"]: fk["ref_table"] for fk in (_t.get("foreign_keys") or [])
            }

    def _object_property_exists(domain_local: str, range_local: str) -> bool:
        """Return True if the ontology already has an owl:ObjectProperty linking
        domain_local ↔ range_local (in either direction, case-insensitive)."""
        if ontology_graph is None:
            return False
        from rdflib import OWL as _OWL, RDF as _RDF, RDFS as _RDFS
        d_lo = domain_local.lower()
        r_lo = range_local.lower()
        for _prop in ontology_graph.subjects(_RDF.type, _OWL.ObjectProperty):
            _doms = {str(d).split("#")[-1].split("/")[-1].lower()
                     for d in ontology_graph.objects(_prop, _RDFS.domain)}
            _rngs = {str(r).split("#")[-1].split("/")[-1].lower()
                     for r in ontology_graph.objects(_prop, _RDFS.range)}
            if (d_lo in _doms and r_lo in _rngs) or (r_lo in _doms and d_lo in _rngs):
                return True
        return False

    def _ref_class_for_fk(table_name: str, col_name: str) -> Dict[str, Any]:
        """Resolve the OWL range class for a FK-like column.

        Uses actual FK metadata when available, otherwise infers from column name
        by stripping a trailing _id suffix and matching against known tables.
        """
        # 1. Actual FK ref_table from inspection data
        ref_table = _insp_fk_ref.get(table_name, {}).get(col_name)
        if ref_table:
            return _guess_class_for_table(ref_table)

        # 2. Column name ends with _id → strip suffix and name-match
        col_lo = col_name.lower()
        if col_lo.endswith("_id"):
            base = col_lo[:-3]  # e.g. "patient_id" → "patient"
            for _t in list(_insp_fk_ref.keys()) + [t.source_table for t in current_report.tables]:
                stripped = _re.sub(r"^raw_", "", _t, flags=_re.IGNORECASE).lower()
                if stripped == base or stripped.rstrip("s") == base:
                    return _guess_class_for_table(_t)

        # 3. Column name matches a table name directly (e.g. "patient" → patients table)
        col_lo_stripped = _re.sub(r"_id$", "", col_lo)
        for _t in list(_insp_fk_ref.keys()) + [t.source_table for t in current_report.tables]:
            stripped = _re.sub(r"^raw_", "", _t, flags=_re.IGNORECASE).lower()
            if stripped == col_lo_stripped or stripped.rstrip("s") == col_lo_stripped:
                return _guess_class_for_table(_t)

        # 4. Fallback: derive class name from column name itself
        base_name = _re.sub(r"_id$", "", col_name, flags=_re.IGNORECASE)
        return {"uri": None, "local_name": _camel(base_name)}

    def _make_object_property_stub(local_name: str, col_name: str, table_name: str,
                                   domain_ref: str, range_class: Dict[str, Any]) -> str:
        range_ref = (
            f'rdf:resource="{range_class["uri"]}"'
            if range_class.get("uri")
            else f'rdf:resource="&ont;{range_class["local_name"]}"'
        )
        return (
            f'<owl:ObjectProperty rdf:about="&ont;{local_name}">\n'
            f'  <rdfs:label xml:lang="en">{col_name.replace("_", " ")}</rdfs:label>\n'
            f'  <rdfs:comment xml:lang="en">Relates {table_name} to its associated {col_name}</rdfs:comment>\n'
            f'  <rdfs:domain {domain_ref}/>\n'
            f'  <rdfs:range {range_ref}/>\n'
            f'</owl:ObjectProperty>'
        )

    unmapped_by_table: Dict[str, list] = {}
    for table in current_report.tables:
        gap_cols = [
            c for c in table.column_mappings
            if (c.status == MappingStatus.UNMAPPED
                or (c.status == MappingStatus.PENDING_REVIEW and c.confidence_score < 0.5))
            and c.source_column.lower() != "id"   # PK — never a property suggestion
        ]
        if gap_cols:
            unmapped_by_table[table.source_table] = gap_cols

    for table_name, cols in unmapped_by_table.items():
        # Suggest new Class when many columns in one table are unmapped
        if len(cols) >= 3:
            class_name = _camel(_re.sub(r"^raw_", "", table_name, flags=_re.IGNORECASE))
            if class_name.lower() not in _existing_class_locals:
                # Check whether an existing ontology class is a near-match for this table
                # so the UI can offer "Add properties to <ExistingClass>" as an alternative.
                _dom = _guess_class_for_table(table_name)
                near_match_class = _dom if _dom.get("uri") else None
                suggestions.append({
                    "id": f"class__{table_name}",
                    "type": "class",
                    "table": table_name,
                    "column": None,
                    "proposed_name": class_name,
                    "rationale": f"{len(cols)} unmapped/low-confidence columns in {table_name} suggest a missing class",
                    "owl_stub": (
                        f'<owl:Class rdf:about="&ont;{class_name}">\n'
                        f'  <rdfs:label xml:lang="en">{class_name}</rdfs:label>\n'
                        f'  <rdfs:comment xml:lang="en">Represents entities from the {table_name} table.</rdfs:comment>\n'
                        f'</owl:Class>'
                    ),
                    "candidates": [],
                    "near_match_class": near_match_class,
                })

        for col in cols:
            col_lower = col.source_column.lower()
            if col_lower == "id":
                continue

            is_fk_like = (
                col_lower.endswith("_id")
                or col_lower in _insp_fk_ref.get(table_name, {})
                or any(
                    col_lower == _re.sub(r"^raw_", "", t.source_table, flags=_re.IGNORECASE)
                    or col_lower == _re.sub(r"^raw_", "", t.source_table, flags=_re.IGNORECASE).rstrip("s")
                    for t in current_report.tables
                )
            )

            domain = _guess_class_for_table(table_name)
            domain_ref = (
                f'rdf:resource="{domain["uri"]}"'
                if domain.get("uri")
                else f'rdf:resource="&ont;{domain["local_name"]}"'
            )
            local_name = "has" + _camel(col.source_column)

            # FK-like columns → ObjectProperty check must run before the generic name check.
            # hasPatient exists (domain=Encounter) but Procedure→Patient is not yet modelled —
            # _prop_already_covered would suppress it on name alone; _object_property_exists
            # is the correct domain-specific gate for FK relationships.
            if is_fk_like:
                ref_class = _ref_class_for_fk(table_name, col.source_column)
                if _object_property_exists(domain["local_name"], ref_class["local_name"]):
                    continue  # already modelled (either direction)
                suggestions.append({
                    "id": f"prop__{table_name}__{col.source_column}",
                    "type": "object_property",
                    "table": table_name,
                    "column": col.source_column,
                    "proposed_name": local_name,
                    "is_fk_like": True,
                    "_best_score": 0.0,
                    "_data_type": col.source_data_type,
                    "suggested_range": ref_class["local_name"],
                    "rationale": f"{col.source_column} is a FK — missing owl:ObjectProperty to {ref_class['local_name']}",
                    "suggested_domain": domain,
                    "owl_stub": _make_object_property_stub(local_name, col.source_column, table_name, domain_ref, ref_class),
                    "candidates": [],
                })
                continue

            xsd = _xsd_range(col.source_data_type)
            xsd_ref = xsd.replace("xsd:", "&xsd;")
            best_score = max((e.score for e in col.evidence_trail), default=0.0)
            near_miss = (
                f"\n  <!-- Best near-miss: {best_score:.0%} — below threshold -->"
                if best_score > 0 else ""
            )

            uri_best: Dict[str, Any] = {}
            for ev in col.evidence_trail:
                if ev.mapped_uri:
                    if ev.mapped_uri not in uri_best or ev.score > uri_best[ev.mapped_uri]["score"]:
                        lbl = ev.mapped_uri.split("/")[-1].split("#")[-1]
                        if ontology_graph is not None:
                            from rdflib import URIRef as _URIRef, RDFS as _RDFS
                            lbs = list(ontology_graph.objects(_URIRef(ev.mapped_uri), _RDFS.label))
                            if lbs:
                                lbl = str(lbs[0])
                        uri_best[ev.mapped_uri] = {
                            "uri": ev.mapped_uri,
                            "label": lbl,
                            "score": ev.score,
                            "strategy": ev.strategy,
                        }
            candidates = sorted(uri_best.values(), key=lambda c: c["score"], reverse=True)[:3]

            suggestions.append({
                "id": f"prop__{table_name}__{col.source_column}",
                "type": "property",
                "table": table_name,
                "column": col.source_column,
                "proposed_name": local_name,
                "is_fk_like": False,
                "_best_score": best_score,
                "_data_type": col.source_data_type,
                "suggested_range": xsd.split(":")[-1],
                "rationale": (
                    f"{col.source_column} ({col.source_data_type}) is unmapped"
                    + (f" — best candidate scored {best_score:.0%}" if best_score > 0 else "")
                ),
                "suggested_domain": domain,
                "owl_stub": (
                    f'<owl:DatatypeProperty rdf:about="&ont;{local_name}">{near_miss}\n'
                    f'  <rdfs:label xml:lang="en">{col.source_column.replace("_", " ")}</rdfs:label>\n'
                    f'  <rdfs:comment xml:lang="en">Mapped from {_re.sub(r"^raw_", "", table_name, flags=_re.IGNORECASE)}.{col.source_column} ({col.source_data_type})</rdfs:comment>\n'
                    f'  <rdfs:domain {domain_ref}/>\n'
                    f'  <rdfs:range rdf:resource="{xsd_ref}"/>\n'
                    f'</owl:DatatypeProperty>'
                ),
                "candidates": candidates,
            })

    # ── Inspection-based gap fill ─────────────────────────────────────────────
    # For columns not yet in the mapping report (e.g. before a map phase has run),
    # read the inspection report directly and generate suggestions for every
    # non-PK, non-ID column not already covered above.
    _inspection_path = ROOT / "output" / "inspection_report.json"
    if _inspection_path.exists():
        import json as _json2
        _insp_tables = _json2.loads(_inspection_path.read_text())

        # Sets of (table, column) already handled
        _already_suggested = {
            (s["table"], s["column"])
            for s in suggestions if s["type"] == "property"
        }
        _confirmed_mapped = set()
        try:
            for _tbl in current_report.tables:
                for _col in _tbl.column_mappings:
                    if _col.status == MappingStatus.CONFIRMED:
                        _confirmed_mapped.add((_tbl.source_table, _col.source_column))
        except Exception:
            pass

        # FK column names per table (already in _insp_fk_ref built above)
        _fk_col_names: Dict[str, set] = {
            tbl: set(cols.keys()) for tbl, cols in _insp_fk_ref.items()
        }

        for _itbl in _insp_tables:
            _tname = _itbl["table_name"]
            _fks_here = _fk_col_names.get(_tname, set())
            for _cname, _cmeta in (_itbl.get("columns") or {}).items():
                if _cmeta.get("is_primary_key") or _cname.lower() == "id":
                    continue
                key = (_tname, _cname)
                if key in _already_suggested or key in _confirmed_mapped:
                    continue
                _already_suggested.add(key)

                _cname_lo = _cname.lower()
                _known_tables = list(_insp_fk_ref.keys())
                _is_fk = (
                    _cname in _fks_here
                    or _cname_lo.endswith("_id")
                    or any(
                        _cname_lo == _re.sub(r"^raw_", "", _t, flags=_re.IGNORECASE).lower()
                        or _cname_lo == _re.sub(r"^raw_", "", _t, flags=_re.IGNORECASE).lower().rstrip("s")
                        for _t in _known_tables
                    )
                )
                _dtype = _cmeta.get("inferred_type", "VARCHAR")
                _local = "has" + _camel(_cname)
                _local_check = _local.lower()
                _domain = _guess_class_for_table(_tname)
                _domain_ref = (
                    f'rdf:resource="{_domain["uri"]}"'
                    if _domain.get("uri")
                    else f'rdf:resource="&ont;{_domain["local_name"]}"'
                )

                if _is_fk:
                    # FK: check domain+range specifically — hasPatient (domain=Encounter)
                    # must not suppress a Procedure→Patient suggestion.
                    _ref_class = _ref_class_for_fk(_tname, _cname)
                    if _object_property_exists(_domain["local_name"], _ref_class["local_name"]):
                        continue  # already modelled (either direction)
                    suggestions.append({
                        "id": f"prop__{_tname}__{_cname}",
                        "type": "object_property",
                        "table": _tname,
                        "column": _cname,
                        "proposed_name": _local,
                        "is_fk_like": True,
                        "_best_score": 0.0,
                        "_data_type": _dtype,
                        "suggested_range": _ref_class["local_name"],
                        "rationale": f"{_cname} is a FK — missing owl:ObjectProperty to {_ref_class['local_name']}",
                        "suggested_domain": _domain,
                        "owl_stub": _make_object_property_stub(_local, _cname, _tname, _domain_ref, _ref_class),
                        "candidates": [],
                    })
                else:
                    # Non-FK datatype property: apply fuzzy name check here
                    if _prop_already_covered(_local_check):
                        continue
                    _xsd = _xsd_range(_dtype)
                    _xsd_ref = _xsd.replace("xsd:", "&xsd;")
                    suggestions.append({
                        "id": f"prop__{_tname}__{_cname}",
                        "type": "property",
                        "table": _tname,
                        "column": _cname,
                        "proposed_name": _local,
                        "is_fk_like": False,
                        "_best_score": 0.0,
                        "_data_type": _dtype,
                        "suggested_range": _xsd.split(":")[-1],
                        "rationale": f"{_cname} ({_dtype}) has no ontology mapping yet",
                        "suggested_domain": _domain,
                        "owl_stub": (
                            f'<owl:DatatypeProperty rdf:about="&ont;{_local}">\n'
                            f'  <rdfs:label xml:lang="en">{_cname.replace("_", " ")}</rdfs:label>\n'
                            f'  <rdfs:comment xml:lang="en">Mapped from {_re.sub(r"^raw_", "", _tname, flags=_re.IGNORECASE)}.{_cname} ({_dtype})</rdfs:comment>\n'
                            f'  <rdfs:domain {_domain_ref}/>\n'
                            f'  <rdfs:range rdf:resource="{_xsd_ref}"/>\n'
                            f'</owl:DatatypeProperty>'
                        ),
                        "candidates": [],
                    })

    # ── LLM enrichment: generate human-readable labels and comments ───────────
    # Cover both datatype and object property suggestions; cap batch to avoid timeout.
    prop_suggestions = [s for s in suggestions if s["type"] in ("property", "object_property")]
    prop_suggestions = sorted(prop_suggestions, key=lambda x: -x.get("_best_score", 0))[:50]

    # Build rule-based 'why' for every property suggestion before LLM enrichment
    for s in prop_suggestions:
        s["why"] = _build_suggestion_why(s)

    prompt_tokens = 0
    completion_tokens = 0
    if prop_suggestions and settings.OPENAI_API_KEY:
        known_tables = [t.source_table for t in current_report.tables]
        try:
            prompt_tokens, completion_tokens = _enrich_suggestion_labels(
                prop_suggestions, settings, known_tables
            )
        except Exception as _exc:
            logging.warning("Suggestion LLM enrichment failed: %s", _exc)

    # Strip internal helper fields before sending to client
    for s in suggestions:
        s.pop("_best_score", None)
        s.pop("_data_type", None)

    _save_suggestions_cache(suggestions)

    # Estimate cost (gpt-4o-mini: $0.15/1M input, $0.60/1M output)
    cost_usd = (prompt_tokens * 0.15 + completion_tokens * 0.60) / 1_000_000

    return {
        "suggestions": suggestions,
        "total": len(suggestions),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": cost_usd,
    }


@app.get("/api/ontology/properties")
def api_ontology_properties() -> Dict[str, Any]:
    """Return all DatatypeProperty and ObjectProperty URIs with labels from the loaded ontology."""
    if ontology_graph is None:
        return {"properties": []}
    from rdflib import OWL, RDF, RDFS
    props = list(ontology_graph.subjects(RDF.type, OWL.DatatypeProperty)) + list(
        ontology_graph.subjects(RDF.type, OWL.ObjectProperty)
    )
    result = []
    for prop in props:
        labels = list(ontology_graph.objects(prop, RDFS.label))
        comments = list(ontology_graph.objects(prop, RDFS.comment))
        result.append(
            {
                "uri": str(prop),
                "label": str(labels[0]) if labels else "",
                "comment": str(comments[0]) if comments else "",
            }
        )
    result.sort(key=lambda x: x["uri"])
    return {"properties": result}


@app.get("/api/ontology/catalog")
def api_ontology_catalog() -> Dict[str, Any]:
    """Return list of loaded ontology files with enabled state, path, and last_modified."""
    status = _ontology_status()
    file_map = {f["filename"]: f for f in status.get("files", [])}
    with _catalog_lock:
        entries = []
        for e in _catalog_entries:
            entry = {k: v for k, v in e.items() if k != "graph"}
            extra = file_map.get(e["filename"], {})
            if "path" in extra:
                entry["path"] = extra["path"]
            if "last_modified" in extra:
                entry["last_modified"] = extra["last_modified"]
            entries.append(entry)
        return {"entries": entries}


class CatalogToggleRequest(BaseModel):
    filename: str
    enabled: bool


@app.post("/api/ontology/catalog/toggle")
def api_ontology_catalog_toggle(body: CatalogToggleRequest) -> Dict[str, Any]:
    with _catalog_lock:
        for entry in _catalog_entries:
            if entry["filename"] == body.filename:
                entry["enabled"] = body.enabled
                _rebuild_sources()
                if _catalog_state_path:
                    _save_catalog_state(_catalog_state_path)
                return {"status": "ok", "filename": body.filename, "enabled": body.enabled}
    raise HTTPException(status_code=404, detail="File not in catalog")


class CatalogAddRequest(BaseModel):
    path: str  # absolute or ROOT-relative path to a new OWL/RDF file


class SuggestionApplyRequest(BaseModel):
    owl_stub: str
    proposed_name: str


@app.post("/api/ontology/catalog/add")
def api_ontology_catalog_add(body: CatalogAddRequest) -> Dict[str, Any]:
    p = Path(body.path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise HTTPException(status_code=400, detail="File not found")
    fname = p.name
    with _catalog_lock:
        if any(e["filename"] == fname for e in _catalog_entries):
            raise HTTPException(status_code=409, detail="File already in catalog")
        g = _load_owl_file(p.parent, fname)
        if g is None:
            raise HTTPException(status_code=422, detail="Could not parse OWL/RDF file")
        _catalog_entries.append({"filename": fname, "logical_uri": "", "enabled": True})
        _catalog_graphs[fname] = g
        _resolve_imports(p.parent)
        _rebuild_sources()
        if _catalog_state_path:
            _save_catalog_state(_catalog_state_path)
    return {"status": "added", "filename": fname}


@app.get("/api/ontology/available-files")
def api_ontology_available_files() -> Dict[str, Any]:
    """List OWL/RDF files available in the ontology directory."""
    files = _list_owl_files()
    status = _ontology_status()
    return {"files": files, "active": status.get("configured_path", ""), "status": status}


class OntologySelectRequest(BaseModel):
    path: str  # filename (relative to ontology dir) or full path


@app.post("/api/ontology/select")
def api_ontology_select(body: OntologySelectRequest) -> Dict[str, Any]:
    """Switch the working ontology to a different file and reload."""
    p = Path(body.path)
    if not p.is_absolute():
        # Try relative to ontology dir first, then ROOT
        ont_path_str = settings.ONTOLOGY_PATH
        if ont_path_str:
            base = Path(ont_path_str)
            if not base.is_absolute():
                base = ROOT / base
            candidate = base.parent / p
            if candidate.exists():
                p = candidate
            else:
                p = ROOT / p
        else:
            p = ROOT / p
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"File not found: {p}")
    rel = str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)
    _reinit_catalog(rel)
    global _active_write_target
    if p.suffix.lower() in {".owl", ".rdf", ".ttl"}:
        _active_write_target = p.name
    _save_app_state()
    # Persist the new ontology path back into the active project file so the
    # selection survives a project re-select or page reload.
    if _active_project:
        try:
            import yaml as _yaml
            proj_file = ROOT / "projects" / f"{_active_project}.yaml"
            with open(proj_file) as _f:
                _proj_data = _yaml.safe_load(_f) or {}
            _proj_data["ontology_path"] = rel
            with open(proj_file, "w") as _f:
                _yaml.dump(_proj_data, _f, default_flow_style=False, allow_unicode=True)
            logger.info("Updated project '%s' ontology_path → %s", _active_project, rel)
        except Exception as _exc:
            logger.warning("Could not update project YAML for '%s': %s", _active_project, _exc)
    return {"status": "ok", "path": rel, "ontology_status": _ontology_status()}


@app.post("/api/ontology/upload")
async def api_ontology_upload(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Upload an OWL/RDF file into the ontology directory and activate it."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".owl", ".rdf", ".ttl", ".xml"}:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    # Save to ontology directory (or owl/ fallback)
    ont_path_str = settings.ONTOLOGY_PATH
    if ont_path_str:
        ont_path = Path(ont_path_str)
        if not ont_path.is_absolute():
            ont_path = ROOT / ont_path
        dest_dir = ont_path.parent
    else:
        dest_dir = ROOT / "owl"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file.filename
    content = await file.read()
    dest.write_bytes(content)
    rel = str(dest.relative_to(ROOT))
    _reinit_catalog(rel)
    global _active_write_target
    _active_write_target = file.filename
    return {"status": "uploaded", "filename": file.filename, "path": rel, "ontology_status": _ontology_status()}


@app.get("/api/ontology/history")
def api_ontology_history() -> Dict[str, Any]:
    """Return list of ontology snapshots taken at pipeline run time."""
    con = sqlite3.connect(str(_pipeline_runs_db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, snapped_at, run_id, hash, snapshot_files, description "
        "FROM ontology_snapshots ORDER BY snapped_at DESC LIMIT 50"
    ).fetchall()
    con.close()
    snaps = []
    for r in rows:
        d = dict(r)
        d["snapshot_files"] = json.loads(d.get("snapshot_files") or "[]")
        snaps.append(d)
    return {"snapshots": snaps}


@app.post("/api/ontology/history/{snapshot_id}/restore")
def api_ontology_history_restore(snapshot_id: int) -> Dict[str, Any]:
    """Restore OWL files from a snapshot and reload in-memory graphs."""
    con = sqlite3.connect(str(_pipeline_runs_db_path))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, run_id, hash, snapshot_files FROM ontology_snapshots WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    con.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    snap_dir = _SNAPSHOTS_DIR / row["run_id"]
    files: List[str] = json.loads(row["snapshot_files"] or "[]")

    ont_path_str = settings.ONTOLOGY_PATH
    if not ont_path_str:
        raise HTTPException(status_code=400, detail="No ONTOLOGY_PATH configured")
    ont_path = Path(ont_path_str)
    if not ont_path.is_absolute():
        ont_path = ROOT / ont_path
    catalog_dir = ont_path.parent

    restored = []
    for fname in files:
        src = snap_dir / fname
        if src.exists():
            shutil.copy2(str(src), str(catalog_dir / fname))
            restored.append(fname)

    with _catalog_lock:
        for entry in _catalog_entries:
            if entry["filename"] in restored:
                g = _load_owl_file(catalog_dir, entry["filename"])
                if g is not None:
                    _catalog_graphs[entry["filename"]] = g
        _resolve_imports(catalog_dir)
        _rebuild_sources()

    return {"status": "restored", "snapshot_id": snapshot_id, "hash": row["hash"], "files": restored}


@app.post("/api/ontology/suggestions/{suggestion_id}/apply")
def api_ontology_suggestion_apply(suggestion_id: str, body: SuggestionApplyRequest) -> Dict[str, Any]:
    """Merge a suggestion stub into the target OWL file using rdflib, then re-serialize."""
    import re as _re
    from rdflib import Graph as _Graph, Namespace as _Namespace
    from rdflib.namespace import OWL as _OWL, RDF as _RDF, RDFS as _RDFS, XSD as _XSD

    target_file = _resolve_write_target()
    if target_file is None:
        raise HTTPException(status_code=400, detail="No active ontology file to write to")
    if not target_file.exists():
        raise HTTPException(status_code=400, detail=f"Ontology file not found: {target_file.name}")

    with _catalog_lock:
        # ── Determine the ontology namespace from the existing file ──────────
        content = target_file.read_text(encoding="utf-8")
        ont_ns = ""
        base_m = _re.search(r'xml:base="([^"]+)"', content)
        if base_m:
            ont_ns = base_m.group(1).rstrip("#/") + "#"
        if not ont_ns:
            xmlns_m = _re.search(r'<rdf:RDF\b[^>]+xmlns="([^"]+)"', content, _re.DOTALL)
            if xmlns_m:
                ont_ns = xmlns_m.group(1)
        if not ont_ns:
            about_m = _re.search(r'rdf:about="(https?://[^"]+/|https?://[^"#]+#)', content)
            if about_m:
                ont_ns = about_m.group(1)

        # ── Resolve entity references in the stub ────────────────────────────
        resolved_stub = body.owl_stub
        if ont_ns:
            resolved_stub = resolved_stub.replace("&ont;", ont_ns)
        resolved_stub = resolved_stub.replace("&xsd;", str(_XSD))

        # ── Wrap stub in a minimal valid RDF/XML envelope and parse it ───────
        stub_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<rdf:RDF\n'
            '   xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
            '   xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"\n'
            '   xmlns:owl="http://www.w3.org/2002/07/owl#"\n'
            '   xmlns:xsd="http://www.w3.org/2001/XMLSchema#"\n'
            + (f'   xmlns:ont="{ont_ns}"\n' if ont_ns else '')
            + '>\n'
            + resolved_stub
            + '\n</rdf:RDF>'
        )
        stub_graph = _Graph()
        try:
            stub_graph.parse(data=stub_xml, format="xml")
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Could not parse suggestion stub as RDF/XML: {exc}")

        if len(stub_graph) == 0:
            raise HTTPException(status_code=422, detail="Suggestion stub produced no triples")

        # ── Load the existing ontology file, merge, and re-serialize ─────────
        existing = _Graph()
        existing.parse(str(target_file), format="xml")

        # Copy namespace bindings from existing graph so they are preserved
        for prefix, ns_uri in existing.namespaces():
            stub_graph.bind(prefix, ns_uri)

        # Remove existing annotation triples for any subject in the stub so we
        # don't end up with duplicate rdfs:label / rdfs:comment entries.
        stub_subjects = set(stub_graph.subjects())
        for subj in stub_subjects:
            for pred in (_RDFS.label, _RDFS.comment):
                for obj in list(existing.objects(subj, pred)):
                    existing.remove((subj, pred, obj))

        # Merge new triples into existing graph
        for triple in stub_graph:
            existing.add(triple)

        existing.serialize(destination=str(target_file), format="xml")

        # Reload the in-memory catalog graph
        fname = target_file.name
        g = _load_owl_file(target_file.parent, fname)
        if g is not None:
            _catalog_graphs[fname] = g
        _resolve_imports(target_file.parent)
        _rebuild_sources()

    # Take a snapshot for this change
    run_id = f"suggestion_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{suggestion_id[:20]}"
    hash_val = _compute_ontology_hash()
    description = f"Added {body.proposed_name} via suggestion"
    _snapshot_ontology(run_id, hash_val, description)

    # Invalidate suggestions cache — ontology has changed
    if _SUGGESTIONS_CACHE_PATH.exists():
        _SUGGESTIONS_CACHE_PATH.unlink(missing_ok=True)

    return {"status": "applied", "suggestion_id": suggestion_id, "rerun_needed": True}


class PropertyUpdateRequest(BaseModel):
    old_uri: str
    new_local_name: Optional[str] = None
    new_label: Optional[str] = None
    new_range_uri: Optional[str] = None


@app.post("/api/ontology/property/update")
def api_ontology_property_update(body: PropertyUpdateRequest) -> Dict[str, Any]:
    """Edit a datatype property's local name, label and/or range in the target OWL file."""
    import re as _re2
    from rdflib import Graph as _Graph, URIRef as _URIRef, Literal as _Literal
    from rdflib.namespace import OWL as _OWL, RDF as _RDF, RDFS as _RDFS

    target_file = _resolve_write_target()
    if target_file is None or not target_file.exists():
        raise HTTPException(status_code=400, detail="No active ontology file to write to")

    with _catalog_lock:

        g = _Graph()
        g.parse(str(target_file), format="xml")

        old_ref = _URIRef(body.old_uri)

        # Derive new URI if renaming
        if body.new_local_name:
            old_str = str(old_ref)
            base = (old_str.rsplit("#", 1)[0] + "#") if "#" in old_str else (old_str.rsplit("/", 1)[0] + "/")
            new_ref = _URIRef(base + body.new_local_name)
        else:
            new_ref = old_ref

        # Rename: copy all triples to new URI, remove old
        if new_ref != old_ref:
            for pred, obj in list(g.predicate_objects(old_ref)):
                g.add((new_ref, pred, obj))
            g.remove((old_ref, None, None))

        # Update label
        if body.new_label is not None:
            for obj in list(g.objects(new_ref, _RDFS.label)):
                g.remove((new_ref, _RDFS.label, obj))
            if body.new_label:
                g.add((new_ref, _RDFS.label, _Literal(body.new_label, lang="en")))

        # Update range
        if body.new_range_uri is not None:
            for obj in list(g.objects(new_ref, _RDFS.range)):
                g.remove((new_ref, _RDFS.range, obj))
            g.add((new_ref, _RDFS.range, _URIRef(body.new_range_uri)))

        g.serialize(destination=str(target_file), format="xml")

        fname = target_file.name
        gl = _load_owl_file(target_file.parent, fname)
        if gl is not None:
            _catalog_graphs[fname] = gl
        _rebuild_sources()

    run_id = f"prop_edit_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    _snapshot_ontology(run_id, _compute_ontology_hash(), f"Edited property {body.new_local_name or body.old_uri.split('/')[-1].split('#')[-1]}")

    return {"ok": True, "new_uri": str(new_ref)}


class PropertyDeleteRequest(BaseModel):
    uri: str


@app.post("/api/ontology/property/delete")
def api_ontology_property_delete(body: PropertyDeleteRequest) -> Dict[str, Any]:
    """Remove a datatype property and all its triples from the target OWL file."""
    from rdflib import Graph as _Graph, URIRef as _URIRef
    from rdflib.namespace import OWL as _OWL, RDF as _RDF

    target_file = _resolve_write_target()
    if target_file is None or not target_file.exists():
        raise HTTPException(status_code=400, detail="No active ontology file to write to")

    with _catalog_lock:

        g = _Graph()
        g.parse(str(target_file), format="xml")
        ref = _URIRef(body.uri)

        # Verify it is a DatatypeProperty before deleting
        if (ref, _RDF.type, _OWL.DatatypeProperty) not in g:
            raise HTTPException(status_code=400, detail="URI is not a DatatypeProperty in this ontology")

        g.remove((ref, None, None))   # remove all triples where it is the subject
        g.remove((None, None, ref))   # remove references to it as object

        g.serialize(destination=str(target_file), format="xml")

        fname = target_file.name
        gl = _load_owl_file(target_file.parent, fname)
        if gl is not None:
            _catalog_graphs[fname] = gl
        _rebuild_sources()

    local = body.uri.split("#")[-1].split("/")[-1]
    run_id = f"prop_del_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    _snapshot_ontology(run_id, _compute_ontology_hash(), f"Deleted property {local}")
    _SUGGESTIONS_CACHE_PATH.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/mappings/reject")
def api_mappings_reject(body: MappingAction) -> Dict[str, str]:
    """Reject a pending mapping and update the report on disk."""
    _update_report_column(
        body.source_table, body.source_column,
        MappingStatus.UNMAPPED,
    )
    return {"status": "rejected"}


@app.post("/api/mappings/clear")
def api_mappings_clear() -> Dict[str, str]:
    """Overwrite the mapping report with an empty placeholder (used after ontology regeneration)."""
    import json as _json
    empty = {
        "schema_version": "1.2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "SemanticMapper-v1",
        "ontology_uri": "",
        "database_name": "",
        "settings_used": {},
        "tables": [],
        "total_pending_review": 0,
    }
    _mapping_report_path.parent.mkdir(parents=True, exist_ok=True)
    _mapping_report_path.write_text(_json.dumps(empty, indent=2), encoding="utf-8")
    return {"status": "cleared"}


@app.get("/api/status")
def api_status() -> Dict[str, Any]:
    return {
        "fuseki_url": settings.FUSEKI_URL,
        "dataset": settings.RDF_DATASET_NAME,
        "named_graph": settings.RDF_NAMED_GRAPH,
        "model": _active_query_model,
        "available_models": _available_models_payload(),
        "max_rows": settings.QUERY_MAX_ROWS,
        "api_calls": _engine.api_calls,
        "prompt_tokens": _base_prompt_tokens + _engine.prompt_tokens,
        "completion_tokens": _base_completion_tokens + _engine.completion_tokens,
        "default_limit": settings.QUERY_MAX_ROWS,
        "default_threshold": _mapping_threshold,
    }


class ModelBody(BaseModel):
    model: str


_OPENAI_QUERY_MODELS = {"gpt-4o", "gpt-4o-mini"}
_CLAUDE_QUERY_MODELS = {"claude-sonnet-4-6", "claude-haiku-4-5-20251001"}


def _local_models() -> List[str]:
    """List models available on the configured local endpoint (Ollama).

    Returns [] when no local backend is configured or the server is unreachable,
    so a cloud deployment is unaffected. Never raises.
    """
    base_url = getattr(settings, "LLM_BASE_URL", None) or getattr(settings, "OPENAI_BASE_URL", None)
    if not base_url:
        return []
    # Ollama exposes a native /api/tags listing at the server root (not /v1).
    tags_url = base_url.rstrip("/")
    if tags_url.endswith("/v1"):
        tags_url = tags_url[:-3]
    tags_url = tags_url.rstrip("/") + "/api/tags"
    try:
        import requests
        resp = requests.get(tags_url, timeout=3)
        resp.raise_for_status()
        return sorted(m["name"] for m in resp.json().get("models", []) if m.get("name"))
    except Exception:  # noqa: BLE001 — best-effort discovery
        return []


def _allowed_query_models() -> set:
    """Models the picker accepts: cloud presets + whatever is installed locally."""
    return _OPENAI_QUERY_MODELS | _CLAUDE_QUERY_MODELS | set(_local_models())


def _available_models_payload() -> Dict[str, List[str]]:
    """Structured model list for the UI dropdown, grouped by backend."""
    local = _local_models()
    return {
        "local": local,
        "openai": sorted(_OPENAI_QUERY_MODELS),
        "claude": sorted(_CLAUDE_QUERY_MODELS),
    }


@app.post("/api/model")
def api_set_model(body: ModelBody) -> Dict[str, Any]:
    """Switch the active query model at runtime.

    Accepts cloud presets (OpenAI/Claude) and any model installed on the local
    endpoint. Claude models use the claude_code provider (Max plan); everything
    else (cloud OpenAI or local Ollama) uses the OpenAI-compatible 'openai' path.
    """
    global _active_model, _active_query_model
    allowed = _allowed_query_models()
    if body.model not in allowed:
        raise HTTPException(status_code=400, detail=f"model must be one of {sorted(allowed)}")
    _active_query_model = body.model
    if body.model in _CLAUDE_QUERY_MODELS:
        settings.QUERY_PROVIDER = "claude_code"
        settings.QUERY_ANTHROPIC_MODEL = body.model
    else:
        settings.QUERY_PROVIDER = "openai"
        settings.QUERY_MODEL = body.model
        _active_model = body.model  # also update pipeline model for OpenAI/local selections
    _save_app_state()
    return {"model": _active_query_model}


@app.get("/api/models")
def api_list_models() -> Dict[str, Any]:
    """Return the models the UI dropdown should offer, grouped by backend."""
    return {"active": _active_query_model, "models": _available_models_payload()}


class ThresholdBody(BaseModel):
    threshold: float


@app.post("/api/threshold")
def api_set_threshold(body: ThresholdBody) -> Dict[str, Any]:
    """Persist the mapping threshold so it survives page refreshes and restarts."""
    global _mapping_threshold
    if not (0.0 <= body.threshold <= 1.0):
        raise HTTPException(status_code=400, detail="threshold must be between 0.0 and 1.0")
    _mapping_threshold = body.threshold
    _save_app_state()
    return {"threshold": _mapping_threshold}


@app.post("/api/tokens/reset")
def api_tokens_reset() -> Dict[str, Any]:
    """Zero the accumulated token counter and persist the reset."""
    global _base_prompt_tokens, _base_completion_tokens
    _base_prompt_tokens = 0
    _base_completion_tokens = 0
    _engine.prompt_tokens = 0
    _engine.completion_tokens = 0
    _engine.api_calls = 0
    _save_app_state()
    return {"status": "ok", "prompt_tokens": 0, "completion_tokens": 0}


# ── Issue #49: Ontology Coverage Metrics ─────────────────────────────────────

@app.get("/api/coverage")
def api_coverage() -> Dict[str, Any]:
    """Return per-table and aggregate mapping coverage metrics."""
    r = _load_report()
    cov = r.coverage or compute_coverage(r)
    return cov.model_dump()


# ── Issue #50: Triple Completeness ────────────────────────────────────────────

_PIPELINE_STATS_PATH = ROOT / "output" / "pipeline_stats.json"


@app.get("/api/pipeline/completeness")
def api_pipeline_completeness() -> Dict[str, Any]:
    """Return silver/gold/rdf pipeline stats for triple completeness (issue #50)."""
    from semantic_mapper.eval.models import (
        GoldCompletenessStats, PipelineCompletenessReport,
        RdfCompletenessStats, SilverCompletenessStats,
    )
    if not _PIPELINE_STATS_PATH.exists():
        return PipelineCompletenessReport(available=False).model_dump()

    with open(_PIPELINE_STATS_PATH) as f:
        raw = json.load(f)

    silver = SilverCompletenessStats(**raw["silver"]) if "silver" in raw else None
    gold   = GoldCompletenessStats(**raw["gold"])     if "gold"   in raw else None
    rdf    = RdfCompletenessStats(**raw["rdf"])       if "rdf"    in raw else None

    result = PipelineCompletenessReport(available=True, silver=silver, gold=gold, rdf=rdf)
    d = result.model_dump()
    # Inject computed properties (Pydantic @property not serialised automatically)
    if silver:
        d["silver"]["completeness_pct"] = silver.completeness_pct
        d["silver"]["quarantine_pct"]   = silver.quarantine_pct
    return d


@app.get("/api/quarantine")
def api_quarantine_tables() -> Dict[str, Any]:
    """List quarantine tables and row counts from the silver database (issue #50)."""
    try:
        from sqlalchemy import create_engine, inspect as sa_inspect, text
        engine = create_engine(settings.SILVER_DATABASE_URL)
        with engine.connect() as conn:
            ins = sa_inspect(engine)
            tables = [t for t in ins.get_table_names() if t.startswith("quarantine_")]
            result = []
            for t in sorted(tables):
                count = conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar() or 0
                result.append({"table": t, "row_count": count})
        return {"tables": result}
    except Exception as exc:
        return {"tables": [], "error": str(exc)}


@app.get("/api/quarantine/{table_name}")
def api_quarantine_rows(
    table_name: str, offset: int = 0, limit: int = 50
) -> Dict[str, Any]:
    """Return paginated rows from a quarantine table (issue #50)."""
    if not table_name.startswith("quarantine_"):
        raise HTTPException(status_code=400, detail="Not a quarantine table")
    if limit > 500:
        limit = 500
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(settings.SILVER_DATABASE_URL)
        with engine.connect() as conn:
            total = conn.execute(
                text(f"SELECT COUNT(*) FROM `{table_name}`")
            ).scalar() or 0
            rows = conn.execute(
                text(f"SELECT * FROM `{table_name}` LIMIT :lim OFFSET :off"),
                {"lim": limit, "off": offset},
            ).mappings().fetchall()
        return {
            "table": table_name,
            "total": total,
            "offset": offset,
            "limit": limit,
            "rows": [dict(r) for r in rows],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Issue #52: Ontology Coherence Check ──────────────────────────────────────

@app.get("/api/ontology/coherence")
def api_ontology_coherence() -> Dict[str, Any]:
    """Run structural coherence checks on the currently loaded ontology (issue #52)."""
    from semantic_mapper.ontology.coherence import OntologyCoherenceChecker

    if ontology_graph is None:
        return {
            "available": False,
            "message": "No ontology loaded. Load an ontology file first.",
        }
    checker = OntologyCoherenceChecker(ontology_graph)
    report = checker.check()
    return {"available": True, **report.to_dict()}


# ── Issue #48: Mapping Quality Evaluation ────────────────────────────────────

class EvalRequest(BaseModel):
    path: str   # path to ground truth YAML, relative to project root


@app.get("/api/eval/ground-truth")
def api_eval_list_ground_truth() -> Dict[str, Any]:
    """List available ground truth files under eval/."""
    eval_dir = ROOT / "eval"
    files = sorted(
        str(p.relative_to(ROOT))
        for p in eval_dir.rglob("*.yaml")
        if not p.name.startswith("benchmark")
        and "benchmarks" not in p.parts
    ) if eval_dir.exists() else []
    return {"files": files}


@app.post("/api/eval/mapping-quality")
def api_eval_mapping_quality(body: EvalRequest) -> Dict[str, Any]:
    """Run mapping quality evaluation against a ground truth file (issue #48)."""
    import yaml
    from semantic_mapper.eval.evaluator import MappingEvaluator
    from semantic_mapper.eval.models import GroundTruth

    gt_path = ROOT / body.path
    if not gt_path.exists():
        raise HTTPException(status_code=404, detail=f"Ground truth file not found: {body.path}")

    with open(gt_path, encoding="utf-8") as fh:
        gt_data = yaml.safe_load(fh)
    ground_truth = GroundTruth.model_validate(gt_data)

    mapping_report = _load_report()
    result = MappingEvaluator().evaluate(
        mapping_report, ground_truth, ground_truth_path=body.path
    )
    return result.model_dump()


# ── Issue #51: Benchmark Query Set ───────────────────────────────────────────

class BenchmarkRequest(BaseModel):
    path: str   # path to benchmark YAML, relative to project root


@app.get("/api/eval/benchmarks")
def api_eval_list_benchmarks() -> Dict[str, Any]:
    """List available benchmark suite files under eval/benchmarks/."""
    bench_dir = ROOT / "eval" / "benchmarks"
    files = sorted(
        str(p.relative_to(ROOT))
        for p in bench_dir.rglob("*.yaml")
    ) if bench_dir.exists() else []
    return {"files": files}


@app.post("/api/eval/benchmark/run")
def api_eval_benchmark_run(body: BenchmarkRequest) -> Dict[str, Any]:
    """Execute a benchmark suite against Fuseki (issue #51)."""
    from semantic_mapper.eval.benchmark import BenchmarkRunner

    suite_path = ROOT / body.path
    if not suite_path.exists():
        raise HTTPException(status_code=404, detail=f"Benchmark file not found: {body.path}")

    runner = BenchmarkRunner(settings.FUSEKI_URL, settings.RDF_DATASET_NAME)
    suite = BenchmarkRunner.load_suite(str(suite_path))
    result = runner.run_suite(suite, suite_path=body.path)
    return result.model_dump()


# ── Serve the SPA ─────────────────────────────────────────────────────────────
STATIC_DIR = ROOT / "web_static"
app.mount("/js", StaticFiles(directory=str(STATIC_DIR / "js")), name="js-static")
app.mount("/fonts", StaticFiles(directory=str(STATIC_DIR / "fonts")), name="fonts-static")


@app.middleware("http")
async def _no_cache_js(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/js/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        (STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_permalink(run_id: str) -> HTMLResponse:
    """Receipt permalink: serves the standalone receipt page, which fetches
    /api/runs/{id} and renders the answer envelope. Target of every receipt_url
    deep link (web app and agentic clients over MCP)."""
    return HTMLResponse(
        (STATIC_DIR / "receipt.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache"},
    )


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print(f"\n  ForgeMesh  →  http://{_args.host}:{_args.port}\n")
    uvicorn.run(app, host=_args.host, port=_args.port, log_level="warning")
