"""Project configuration resolver — the single authoritative reader of project files."""
from semantic_mapper.project.resolver import ProjectConfig, resolve_project, list_projects

__all__ = ["ProjectConfig", "resolve_project", "list_projects"]
