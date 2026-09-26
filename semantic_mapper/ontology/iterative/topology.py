# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
FK-aware topological sort for iterative ontology generation.

Root tables (no FK dependencies) come first — these are dimension/reference tables
such as patients, d_* dictionaries.  Fact tables that reference them come later,
ensuring that when the Gen-LLM processes a table it can reference already-declared
classes for every FK column.
"""
import logging
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

from semantic_mapper.db.models import TableProfile

logger = logging.getLogger(__name__)


def fk_topological_sort(
    profiles: List[TableProfile],
    skip_tables: Optional[Set[str]] = None,
) -> List[TableProfile]:
    """
    Return *profiles* in FK topological order (referenced tables first).

    Algorithm: Kahn's BFS on a directed graph where edge A → B means
    table A has a FK column pointing to table B (A depends on B).

    Cycles (circular FKs) are broken by removing the back-edge whose source
    table has the lower row count.  A warning is logged for every cycle broken.

    Self-references are ignored.

    Args:
        profiles:    All table profiles to sort.
        skip_tables: Optional set of table names to exclude from the result.
                     FK edges pointing to a skipped table are ignored, so tables
                     that depend on a skipped table are not blocked.
    """
    _skip: Set[str] = set(skip_tables) if skip_tables else set()
    profiles = [p for p in profiles if p.table_name not in _skip]

    table_map: Dict[str, TableProfile] = {p.table_name: p for p in profiles}
    table_names: Set[str] = set(table_map)

    # Build adjacency: deps[A] = set of tables A depends on (has FK to)
    deps: Dict[str, Set[str]] = defaultdict(set)
    # reverse_deps[B] = set of tables that depend on B
    reverse_deps: Dict[str, Set[str]] = defaultdict(set)

    for profile in profiles:
        for fk in profile.foreign_keys:
            ref_table = fk.get("referred_table") or fk.get("ref_table")
            if not ref_table or ref_table == profile.table_name:
                continue
            if ref_table not in table_names:
                # FK points outside our profile set — skip
                continue
            deps[profile.table_name].add(ref_table)
            reverse_deps[ref_table].add(profile.table_name)

    # in-degree: how many tables does each table depend on (within our set)
    in_degree: Dict[str, int] = {name: len(deps[name]) for name in table_names}

    # Kahn's BFS — start with tables that have no dependencies
    queue: deque = deque(
        sorted(
            (name for name, deg in in_degree.items() if deg == 0),
            key=lambda n: table_map[n].table_name,
        )
    )
    result: List[TableProfile] = []
    visited: Set[str] = set()

    while queue:
        name = queue.popleft()
        if name in visited:
            continue
        visited.add(name)
        result.append(table_map[name])
        # Reduce in-degree of dependents
        for dependent in sorted(reverse_deps[name]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Cycle detection — any table not yet visited is part of a cycle
    remaining = [name for name in table_names if name not in visited]
    if remaining:
        broken = _break_cycles(remaining, deps, reverse_deps, in_degree, table_map)
        result.extend(broken)
        logger.warning(
            "FK cycle(s) detected involving %d table(s): %s — back-edges removed",
            len(remaining),
            remaining,
        )

    return result


def _break_cycles(
    remaining: List[str],
    deps: Dict[str, Set[str]],
    reverse_deps: Dict[str, Set[str]],
    in_degree: Dict[str, int],
    table_map: Dict[str, TableProfile],
) -> List[TableProfile]:
    """
    Force-process remaining (cyclic) tables by repeatedly removing the back-edge
    (the dependency from the lower-cardinality table) until the queue drains.
    """
    table_names_remaining = set(remaining)
    result: List[TableProfile] = []
    visited: Set[str] = set()

    while table_names_remaining:
        # Pick the table with the lowest row count as the "entry" for the cycle
        entry = min(
            table_names_remaining,
            key=lambda n: table_map[n].row_count,
        )
        # Remove all dependencies of entry that are in the remaining set
        for dep in list(deps[entry]):
            if dep in table_names_remaining:
                logger.warning(
                    "Breaking FK cycle: removing back-edge %s → %s", entry, dep
                )
                deps[entry].discard(dep)
                reverse_deps[dep].discard(entry)

        # Kahn's BFS on remaining set
        local_in_degree = {n: len([d for d in deps[n] if d in table_names_remaining])
                           for n in table_names_remaining}
        queue: deque = deque(
            sorted(
                (n for n, deg in local_in_degree.items() if deg == 0),
                key=lambda n: table_map[n].table_name,
            )
        )
        while queue:
            name = queue.popleft()
            if name in visited:
                continue
            visited.add(name)
            table_names_remaining.discard(name)
            result.append(table_map[name])
            for dependent in sorted(reverse_deps[name]):
                if dependent in table_names_remaining:
                    local_in_degree[dependent] -= 1
                    if local_in_degree[dependent] == 0:
                        queue.append(dependent)

    return result
