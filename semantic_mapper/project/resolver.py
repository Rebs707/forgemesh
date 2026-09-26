"""
Shared project resolver — the only code in the system that reads project files.

Both the CLI (main.py) and the webapp (web_app.py) call this module.
No other code parses project files directly.

A project is a named bundle of the six values that vary between datasets:
  - bronze_database_url
  - silver_database_url
  - gold_database_url
  - ontology_path
  - rdf_dataset_name
  - named_graph_uri

Optional fields:
  - rdf_source_name   : URI source scope (default: project name).  Controls the
                        ``{base}/{source}/{entity}/{id}`` URI segment.  Every project
                        gets its own default, preventing cross-project URI contamination
                        when RDF_SOURCE_NAME is left in the environment from a prior run.
  - llm_base_url      : OpenAI-compatible endpoint for this project (e.g. a local
                        Ollama server).  When set, every LLM call for this project
                        is routed there — letting one project run fully local while
                        another uses the cloud.  Unset = inherit the ambient env.
  - llm_api_key       : API key for the endpoint above (placeholder string for
                        local servers that ignore it).
  - llm_model         : default chat model (ontology gen / mapping / narrator).
  - query_model       : model for NL→SQL / NL→SPARQL / MCP queries.
  - ontology_gen_model / ontology_judge_model : optional per-pass overrides for
                        the iterative generator (unset = use llm_model).
  - llm_num_ctx       : context window (tokens) for a local server (Ollama
                        options.num_ctx).  Raise it so long ontology prompts for
                        wide tables don't overflow the server's small default.

Project files live in ``projects/<name>.yaml`` relative to the repository root.
Adding a dataset means dropping a new file there — no registration step required.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import List

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Matches ${VAR} references inside project-file string values. Only the braced
# form is expanded (not bare $VAR), so Windows paths, connection strings, and
# SPARQL/SQL fragments containing a stray '$' are left untouched.
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(value, *, key: str):
    """Recursively expand ``${VAR}`` references in project-file values from os.environ.

    Lets a committed project file keep volatile, environment-specific fragments
    out of version control. The motivating case is a cloud GPU host whose URL
    changes on every redeploy::

        llm_base_url: https://${RUNPOD_POD_ID}-11434.proxy.runpod.net/v1

    so only the host id (set once in .env.local or the shell) needs editing, not
    the project YAML. A referenced variable that is unset or empty is a hard
    error — fail loud, consistent with the rest of resolve_project — rather than
    silently producing a malformed URL.
    """
    if isinstance(value, str):
        def _sub(match: re.Match) -> str:
            name = match.group(1)
            resolved = os.environ.get(name)
            if not resolved:
                raise ValueError(
                    f"Project key '{key}' references ${{{name}}} but the environment "
                    f"variable {name} is unset. Set it (e.g. in .env.local or your "
                    f"shell) before activating this project."
                )
            return resolved
        return _ENV_VAR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(v, key=key) for v in value]
    return value

# Repository root — two levels up from this file (semantic_mapper/project/resolver.py)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROJECTS_DIR = _REPO_ROOT / "projects"

class OntologyMissingWarning(Exception):
    """Raised when a project's ontology_path does not exist on disk.

    Carries a partial ProjectConfig with ontology_path="" so the caller can
    still activate the project (for inspect/map phases) and let the user pick
    an existing ontology separately.
    """
    def __init__(self, message: str, partial_config: "ProjectConfig") -> None:
        super().__init__(message)
        self.partial_config = partial_config


REQUIRED_KEYS: frozenset[str] = frozenset({
    "bronze_database_url",
    "silver_database_url",
    "gold_database_url",
    "ontology_path",
    "rdf_dataset_name",
    "named_graph_uri",
})


class ProjectConfig(BaseModel, frozen=True):
    """Resolved configuration for one project/dataset.

    All six required fields are populated and validated by resolve_project().
    Consumers must not parse project files directly — use resolve_project().
    """
    name: str
    description: str = ""
    bronze_database_url: str
    silver_database_url: str
    gold_database_url: str
    ontology_path: str
    rdf_dataset_name: str
    named_graph_uri: str
    ontology_named_graph_uri: str = ""
    # rdf_source_name scopes entity URIs to this project's namespace.
    # When absent in the YAML, resolve_project() fills it with the project name
    # so every project has an isolated URI prefix by default.
    rdf_source_name: str = ""
    # ontology_namespace names the SCHEMA namespace (classes/properties/shared vocab),
    # deliberately source/year-agnostic so a family of datasets (e.g. winterolympics
    # 2026 + 2030) share one schema and conformed vocabulary. Empty = derive from the
    # bronze DB name (strips the _bronze storage token).
    ontology_namespace: str = ""
    # Optional per-project LLM backend. Empty string = inherit ambient env
    # (the .env / shell the process was launched with). Set these to pin a
    # project to a specific endpoint/model — e.g. a local Ollama server.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    query_model: str = ""
    ontology_gen_model: str = ""
    ontology_judge_model: str = ""
    # Per-call LLM timeout (seconds). 0 = inherit ambient (settings default 60s).
    # Local big models (32B w/ CPU offload) need far more than 60s, so pin it here.
    llm_request_timeout: int = 0
    # Context window (tokens) for a local model server (Ollama options.num_ctx).
    # 0 = inherit ambient / server default (Ollama's 4096 truncates long ontology
    # prompts). Pin higher here to stop wide-table prompts from overflowing.
    llm_num_ctx: int = 0
    # External terminology binding sources for this dataset (which code systems
    # apply is a per-dataset fact, so it lives with the project). Maps source_id →
    # adapter config; see settings.TERMINOLOGY_SOURCES. When non-empty, binding is
    # enabled for the project. Empty = no terminology binding.
    terminology_sources: dict = {}


def resolve_project(name: str) -> ProjectConfig:
    """Read ``projects/<name>.yaml`` and return a validated ProjectConfig.

    Fails loudly if:
    - the file does not exist
    - any required key is missing
    - the ontology_path does not exist on disk

    The project file always wins over the shell environment.  Callers should
    apply the resolved config to os.environ AFTER calling this function.
    """
    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for project resolution. "
            "Install it with: pip install pyyaml"
        ) from exc

    path = PROJECTS_DIR / f"{name}.yaml"
    if not path.exists():
        available = list_projects()
        raise FileNotFoundError(
            f"Project '{name}' not found at {path}. "
            f"Available projects: {available or ['(none)']}"
        )

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    # Expand ${VAR} references (e.g. ${RUNPOD_POD_ID} in a cloud LLM URL) before
    # validation, so the resolved config carries concrete values.
    data = _expand_env_vars(data, key="<root>")

    missing = REQUIRED_KEYS - set(data.keys())
    if missing:
        raise ValueError(
            f"Project file '{path}' is missing required keys: {sorted(missing)}"
        )

    # Validate ontology_path exists on disk (relative to repo root).
    # An empty string is allowed — it means no ontology has been generated yet;
    # the inspect and seed phases work without one.
    ont = (data["ontology_path"].replace("\\", "/") if data["ontology_path"] else data["ontology_path"])
    ont_missing = False
    if ont:
        ont_path = (_REPO_ROOT / ont) if not Path(ont).is_absolute() else Path(ont)
        if not ont_path.exists():
            ont_missing = True

    # rdf_source_name defaults to the project name if not explicitly set.
    # This isolates each project's entity URIs by default.
    rdf_source_name = data.get("rdf_source_name") or name

    config = ProjectConfig(
        name=name,
        description=data.get("description", ""),
        bronze_database_url=data["bronze_database_url"],
        silver_database_url=data["silver_database_url"],
        gold_database_url=data["gold_database_url"],
        ontology_path="" if ont_missing else data["ontology_path"],
        rdf_dataset_name=data["rdf_dataset_name"],
        named_graph_uri=data["named_graph_uri"],
        ontology_named_graph_uri=data.get("ontology_named_graph_uri", ""),
        rdf_source_name=rdf_source_name,
        ontology_namespace=data.get("ontology_namespace", "") or "",
        llm_base_url=data.get("llm_base_url", "") or "",
        llm_api_key=data.get("llm_api_key", "") or "",
        llm_model=data.get("llm_model", "") or "",
        query_model=data.get("query_model", "") or "",
        ontology_gen_model=data.get("ontology_gen_model", "") or "",
        ontology_judge_model=data.get("ontology_judge_model", "") or "",
        llm_request_timeout=int(data.get("llm_request_timeout", 0) or 0),
        llm_num_ctx=int(data.get("llm_num_ctx", 0) or 0),
        terminology_sources=data.get("terminology_sources", {}) or {},
    )
    logger.debug("Resolved project '%s' from %s", name, path)
    if ont_missing:
        raise OntologyMissingWarning(
            f"Project '{name}': ontology_path '{ont}' does not exist on disk. "
            "Choose an existing ontology or regenerate one.",
            partial_config=config,
        )
    return config


def list_projects() -> List[str]:
    """Return the names of all available projects (stems of *.yaml in projects/)."""
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.stem for p in PROJECTS_DIR.glob("*.yaml"))


def apply_project_to_env(config: ProjectConfig) -> None:
    """Write the resolved project values into os.environ.

    Called by both the CLI and webapp after resolve_project() succeeds.
    Values set here override any previously loaded .env file or shell variable.
    FORGEMESH_DB_HOST rewriting (for Docker) runs afterwards in main.py.
    """
    import os
    os.environ["DATABASE_URL"]               = config.bronze_database_url
    os.environ["SILVER_DATABASE_URL"]        = config.silver_database_url
    os.environ["GOLD_DATABASE_URL"]          = config.gold_database_url
    os.environ["ONTOLOGY_PATH"]              = config.ontology_path
    os.environ["RDF_DATASET_NAME"]           = config.rdf_dataset_name
    os.environ["RDF_NAMED_GRAPH"]            = config.named_graph_uri
    if config.ontology_named_graph_uri:
        os.environ["ONTOLOGY_NAMED_GRAPH"]   = config.ontology_named_graph_uri
    os.environ["FORGEMESH_PROJECT"]          = config.name
    # rdf_source_name scopes entity URIs: {base}/{source}/{entity}/{id}.
    # Always set explicitly so a leftover shell value from a previous project
    # cannot contaminate this run's URI namespace.
    os.environ["RDF_SOURCE_NAME"]            = config.rdf_source_name
    # Schema namespace (classes/properties/shared vocab) — family-scoped, year-agnostic.
    if config.ontology_namespace:
        os.environ["ONTOLOGY_NAMESPACE"]     = config.ontology_namespace
    # Derive per-project output paths so each dataset has its own mapping report
    # and value-mappings sidecar.  Consistent with .env.* file conventions.
    os.environ["QUERY_MAPPING_REPORT_PATH"]  = f"output/mapping_report_{config.name}.json"

    # Optional per-project LLM backend. Only set vars that the project pins;
    # anything left empty inherits the ambient env (e.g. .env.local launched
    # alongside the webapp). Canonical LLM_* names — settings.py aliases the
    # legacy OPENAI_* forms onto these.
    _llm_env = {
        "LLM_BASE_URL":         config.llm_base_url,
        "LLM_API_KEY":          config.llm_api_key,
        "LLM_MODEL":            config.llm_model,
        "QUERY_MODEL":          config.query_model,
        "ONTOLOGY_GEN_MODEL":   config.ontology_gen_model,
        "ONTOLOGY_JUDGE_MODEL": config.ontology_judge_model,
    }
    for _key, _val in _llm_env.items():
        if _val:
            os.environ[_key] = _val
    if config.llm_request_timeout:
        os.environ["LLM_REQUEST_TIMEOUT"] = str(config.llm_request_timeout)
    if config.llm_num_ctx:
        os.environ["LLM_NUM_CTX"] = str(config.llm_num_ctx)

    # External terminology binding: which code systems apply is a per-dataset fact,
    # so it travels with the project. Exporting it here means both generation (binder)
    # and materialisation (RDF code-individual minting) see the same config — the gate,
    # which resolves projects rather than sourcing .env.<project>, included.
    if config.terminology_sources:
        import json
        os.environ["TERMINOLOGY_SOURCES"] = json.dumps(config.terminology_sources)
        os.environ["TERMINOLOGY_BINDING_ENABLED"] = "true"


def activate_project(name: str) -> ProjectConfig:
    """Resolve *name* and apply it to os.environ, for scripts that need a project's
    databases whether or not its ontology has been generated yet."""
    try:
        config = resolve_project(name)
    except OntologyMissingWarning as exc:
        config = exc.partial_config
    apply_project_to_env(config)
    return config
