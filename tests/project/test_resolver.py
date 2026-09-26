"""Unit tests for the project resolver.

All tests use tmp_path (no live MySQL, no live Fuseki).
"""
import os
from pathlib import Path

import pytest
import yaml

from semantic_mapper.project.resolver import (
    REQUIRED_KEYS,
    OntologyMissingWarning,
    ProjectConfig,
    apply_project_to_env,
    list_projects,
    resolve_project,
)

# ── helpers ─────────────────────────────────────────────────────────────────


def _write_project(tmp_path: Path, name: str, extra: dict | None = None) -> Path:
    """Write a minimal valid project YAML and a stub ontology file."""
    ont = tmp_path / "ont.owl"
    ont.write_text("<ontology/>")
    data = {
        "bronze_database_url": f"mysql+pymysql://root:pw@localhost:3307/bronze_{name}",
        "silver_database_url": f"mysql+pymysql://root:pw@localhost:3307/silver_{name}",
        "gold_database_url":   f"mysql+pymysql://root:pw@localhost:3307/gold_{name}",
        "ontology_path":       str(ont),
        "rdf_dataset_name":    f"{name}-mesh",
        "named_graph_uri":     f"http://mesh.local/graph/{name}",
    }
    if extra:
        data.update(extra)
    project_dir = tmp_path / "projects"
    project_dir.mkdir(exist_ok=True)
    path = project_dir / f"{name}.yaml"
    path.write_text(yaml.dump(data))
    return path


# ── resolver tests ────────────────────────────────────────────────────────


class TestResolveProject:
    def test_returns_project_config(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        assert isinstance(cfg, ProjectConfig)
        assert cfg.name == "demo"

    def test_all_required_fields_populated(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        for key in REQUIRED_KEYS:
            assert getattr(cfg, key), f"Field '{key}' should be non-empty"

    def test_optional_description_populated(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo", extra={"description": "Test dataset"})
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        assert cfg.description == "Test dataset"

    def test_optional_description_defaults_empty(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        assert cfg.description == ""

    def test_missing_project_raises_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        (tmp_path / "projects").mkdir()
        with pytest.raises(FileNotFoundError, match="not found"):
            resolve_project("nonexistent")

    def test_error_lists_available_projects(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "alpha")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        with pytest.raises(FileNotFoundError, match="alpha"):
            resolve_project("missing")

    def test_missing_required_key_raises_value_error(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        # Corrupt the file by removing a required key
        path = tmp_path / "projects" / "demo.yaml"
        data = yaml.safe_load(path.read_text())
        del data["rdf_dataset_name"]
        path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="rdf_dataset_name"):
            resolve_project("demo")

    def test_missing_ontology_file_raises_with_partial_config(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        path = tmp_path / "projects" / "demo.yaml"
        data = yaml.safe_load(path.read_text())
        data["ontology_path"] = "/nonexistent/path/ont.owl"
        path.write_text(yaml.dump(data))
        with pytest.raises(OntologyMissingWarning, match="ontology_path") as exc_info:
            resolve_project("demo")
        # The partial config lets the caller still activate the project.
        assert exc_info.value.partial_config.name == "demo"
        assert exc_info.value.partial_config.ontology_path == ""

    def test_config_is_frozen(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        with pytest.raises(Exception):  # ValidationError or TypeError
            cfg.name = "mutated"  # type: ignore[misc]

    def test_optional_ontology_named_graph_uri(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo", extra={
            "ontology_named_graph_uri": "http://mesh.local/graph/demo/ontology"
        })
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        assert cfg.ontology_named_graph_uri == "http://mesh.local/graph/demo/ontology"


# ── env-var expansion tests ───────────────────────────────────────────────


class TestEnvVarExpansion:
    def test_expands_braced_var_in_value(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo", extra={
            "llm_base_url": "https://${HOST_ID}-11434.proxy.example/v1"
        })
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        monkeypatch.setenv("HOST_ID", "abc123")
        cfg = resolve_project("demo")
        assert cfg.llm_base_url == "https://abc123-11434.proxy.example/v1"

    def test_unset_var_raises_value_error_naming_it(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo", extra={
            "llm_base_url": "https://${HOST_ID}-11434.proxy.example/v1"
        })
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        monkeypatch.delenv("HOST_ID", raising=False)
        with pytest.raises(ValueError, match="HOST_ID"):
            resolve_project("demo")

    def test_empty_var_is_treated_as_unset(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo", extra={
            "llm_base_url": "https://${HOST_ID}-11434.proxy.example/v1"
        })
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        monkeypatch.setenv("HOST_ID", "")
        with pytest.raises(ValueError, match="HOST_ID"):
            resolve_project("demo")

    def test_bare_dollar_is_left_untouched(self, tmp_path, monkeypatch):
        # Only ${VAR} is expanded; a stray '$' must survive verbatim so that
        # connection strings / query fragments are never mangled.
        _write_project(tmp_path, "demo", extra={"rdf_source_name": "a$b_c"})
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        assert cfg.rdf_source_name == "a$b_c"


# ── list_projects tests ───────────────────────────────────────────────────


class TestListProjects:
    def test_returns_sorted_names(self, tmp_path, monkeypatch):
        for name in ("zebra", "alpha", "beta"):
            _write_project(tmp_path, name)
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        assert list_projects() == ["alpha", "beta", "zebra"]

    def test_empty_when_no_projects(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        assert list_projects() == []

    def test_empty_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR",
            tmp_path / "no_such_dir",
        )
        assert list_projects() == []


# ── apply_project_to_env tests ────────────────────────────────────────────


class TestApplyProjectToEnv:
    def test_sets_database_url(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        apply_project_to_env(cfg)
        assert os.environ["DATABASE_URL"] == cfg.bronze_database_url
        assert os.environ["SILVER_DATABASE_URL"] == cfg.silver_database_url
        assert os.environ["GOLD_DATABASE_URL"] == cfg.gold_database_url

    def test_sets_rdf_vars(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        apply_project_to_env(cfg)
        assert os.environ["RDF_DATASET_NAME"] == cfg.rdf_dataset_name
        assert os.environ["RDF_NAMED_GRAPH"] == cfg.named_graph_uri

    def test_sets_forgemesh_project(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("demo")
        apply_project_to_env(cfg)
        assert os.environ["FORGEMESH_PROJECT"] == "demo"

    def test_sets_mapping_report_path(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "myproject")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("myproject")
        apply_project_to_env(cfg)
        assert os.environ["QUERY_MAPPING_REPORT_PATH"] == "output/mapping_report_myproject.json"

    def test_mapping_report_path_uses_project_name(self, tmp_path, monkeypatch):
        """Different project names produce different report paths."""
        for name in ("alpha", "beta"):
            _write_project(tmp_path, name)
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        for name in ("alpha", "beta"):
            cfg = resolve_project(name)
            apply_project_to_env(cfg)
            assert os.environ["QUERY_MAPPING_REPORT_PATH"] == f"output/mapping_report_{name}.json"

    def test_wins_over_existing_env(self, tmp_path, monkeypatch):
        _write_project(tmp_path, "demo")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://stale@old/stale_db")
        cfg = resolve_project("demo")
        apply_project_to_env(cfg)
        assert os.environ["DATABASE_URL"] == cfg.bronze_database_url

    def test_sets_rdf_source_name_from_yaml(self, tmp_path, monkeypatch):
        """Explicit rdf_source_name in YAML is applied to RDF_SOURCE_NAME env var."""
        _write_project(tmp_path, "flights", extra={"rdf_source_name": "openflights"})
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("flights")
        apply_project_to_env(cfg)
        assert os.environ["RDF_SOURCE_NAME"] == "openflights"

    def test_rdf_source_name_defaults_to_project_name(self, tmp_path, monkeypatch):
        """If rdf_source_name is absent from the YAML, it defaults to the project name."""
        _write_project(tmp_path, "mydata")   # no rdf_source_name in YAML
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        cfg = resolve_project("mydata")
        apply_project_to_env(cfg)
        # Default → project name, so stale env values can never bleed in
        assert os.environ["RDF_SOURCE_NAME"] == "mydata"

    def test_rdf_source_name_overwrites_stale_env(self, tmp_path, monkeypatch):
        """A stale RDF_SOURCE_NAME from a previous project is always overwritten."""
        _write_project(tmp_path, "synthea")
        monkeypatch.setattr(
            "semantic_mapper.project.resolver.PROJECTS_DIR", tmp_path / "projects"
        )
        # Simulate a stale env value from a MIMIC run
        monkeypatch.setenv("RDF_SOURCE_NAME", "mimic")
        cfg = resolve_project("synthea")
        apply_project_to_env(cfg)
        assert os.environ["RDF_SOURCE_NAME"] == "synthea"  # not "mimic"
