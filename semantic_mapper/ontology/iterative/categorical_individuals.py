# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Deterministic materialisation of value-mapped (categorical) vocabulary individuals.

The per-table gen-LLM mints a categorical vocabulary's CLASS and its member
INDIVIDUALS freely, and does so inconsistently across the tables that share one
concept:

  * parallel per-table classes for one concept — ``Gender`` / ``TeamGender`` /
    ``MedallistGender`` — because the class is named from ``{table}{column}``.
  * the SAME source value named differently — ``MGender`` ("M") in one table,
    ``GenderMale`` ("Gender Male") in another — so one real category becomes several
    individuals.
  * hallucinated members with no source value at all (``NonBinaryGender``) and raw
    placeholder passthroughs (``NoGenderTeamGender`` for ``"-"``).

This pass discards all of that and re-derives the vocabulary deterministically from
the SOURCE PROFILE — the only authority on which values actually exist:

  1. One class per concept, named from the source COLUMN (every ``gender`` column →
     one ``Gender``), folding the per-table parallels into it.
  2. Exactly one individual per distinct, non-null source value, with a canonical
     ``{Value}{Class}`` URI and the raw value as ``rdfs:label`` — no semantic
     expansion (``F`` and ``W`` stay distinct), no hallucinated members, no
     placeholder passthrough.

Runs on the merged graph BEFORE ``owl:oneOf`` is built, so the enumeration is built
on the clean individual set.  Fully deterministic (no LLM) and dataset-agnostic:
keyed on source column names and profiled values, never on specific table/column/
value strings.

Supersedes the label-overlap consolidation pass — the LLM's inconsistent member
NAMES (``M`` vs ``Male``) cannot be reconciled by string keys; re-deriving from
source values is the only deterministic fix.  Per the keep-all policy, every
distinct non-null source value yields an individual (no support pruning), so rare
real categories (``Ceremonies``, ``Cancelled``) are preserved.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Set, Tuple

from rdflib import Graph, Literal, OWL, RDF, RDFS, URIRef

from semantic_mapper.ontology.iterative.context_builder import derive_class_uri

logger = logging.getLogger(__name__)

_FM = "http://forgemesh.ai/ontology#"
_FM_SOURCE_FK = URIRef(_FM + "sourceForeignKey")


# ---------------------------------------------------------------------------
# Naming helpers (deterministic, no dataset knowledge)
# ---------------------------------------------------------------------------

def _pascal(name: str) -> str:
    """``discipline_code`` → ``DisciplineCode``; ``eventType`` → ``EventType``.

    Upper-cases the first letter of each alphanumeric token, preserving any internal
    capitals (so an already-camelCase column survives).  Used for the concept class
    name; NOT singularised — a column is an attribute, not a table.
    """
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name) if p]
    return "".join(p[0].upper() + p[1:] for p in parts)


def _value_slug(value: str) -> str:
    """``"Alpine Skiing"`` → ``AlpineSkiing``; ``"M"`` → ``M``; ``"Head Coach"`` →
    ``HeadCoach``.  Deterministic PascalCase of the raw value's alphanumeric tokens."""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", value) if p]
    slug = "".join(p[0].upper() + p[1:] for p in parts)
    if not slug:
        slug = "Value"
    if slug[0].isdigit():
        slug = "N" + slug
    return slug


def _humanize(local: str) -> str:
    """``DisciplineCode`` → ``Discipline Code`` for a readable rdfs:label."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", local)


def _is_null_like(value: object) -> bool:
    """Shared with the mapping engine so generation and mapping agree on what is NOT a
    value.  Reuses the mapper's predicate (the single source of truth) — imported at
    call time to avoid a load-time dependency on the engine layer."""
    from semantic_mapper.engine.strategies.llm_strategy import _is_null_like as _impl
    return _impl(value)


def _looks_packed(value: str) -> bool:
    """A JSON/list-shaped cell (``["BOB","LUG"]``) is unpacked into entities elsewhere —
    never a single categorical value."""
    return value.strip()[:1] in ("[", "{")


# ---------------------------------------------------------------------------
# Conformed-dimension folding (role-variant columns → one vocabulary class)
# ---------------------------------------------------------------------------

# Generic structural role qualifiers — English words that mark a column's ROLE
# relative to a shared dimension (a "current" vs "previous" care unit; a "home"
# vs "away" team; an "original" vs "total" amount unit). These are grammatical
# role markers, NOT domain terms, so stripping them to find a dimension stem is
# dataset-agnostic — the same class of structural heuristic the FK inspector's
# role-prefix strategy already relies on.
_ROLE_QUALIFIERS = frozenset({
    "curr", "current", "prev", "previous", "first", "last", "next",
    "orig", "original", "total", "src", "source", "dest", "destination",
    "home", "away", "new", "old", "primary", "secondary", "from", "to",
})
# Longest-first so "total" strips before "to", "original" before "orig".
_ROLE_PREFIXES = tuple(sorted(_ROLE_QUALIFIERS, key=len, reverse=True))


def _dimension_stem(col_name: str) -> str:
    """Reduce a column name to its conformed-dimension stem by removing generic
    role qualifiers — ``curr_careunit`` → ``careunit``, ``originalamountuom`` →
    ``amountuom``, ``home_team`` → ``team``. Handles both separator-delimited
    names and smashed-together ones (no underscores). Purely structural; never
    keyed on dataset-specific words. Under-stemming is the safe failure — it
    only leaves concepts unmerged; the value-overlap gate guards correctness."""
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", col_name.lower()) if t]
    # Drop tokens that ARE a role word outright (curr_careunit → [careunit]).
    kept = [t for t in tokens if t not in _ROLE_QUALIFIERS]
    if not kept:
        kept = tokens  # all-qualifier name (rare) — leave untouched
    # For a lone smashed token, peel leading role prefixes (longest match first),
    # provided a meaningful remainder (>= 3 chars) survives each peel.
    if len(kept) == 1:
        tok = kept[0]
        changed = True
        while changed:
            changed = False
            for pre in _ROLE_PREFIXES:
                if tok.startswith(pre) and len(tok) - len(pre) >= 3:
                    tok = tok[len(pre):]
                    changed = True
                    break
        kept = [tok]
    return "".join(kept)


def _values_corroborate(
    a: Dict[str, str], b: Dict[str, str], jaccard_min: float = 0.3
) -> bool:
    """Whether two value sets support merging their columns into one dimension:
    Jaccard >= ``jaccard_min``, OR the smaller (non-empty) set is a subset of the
    larger — so a sparsely-profiled member (``{ml}``) still joins its family.
    Keyed on normalised value keys, never on specific value strings."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return False
    inter = len(sa & sb)
    if inter == 0:
        return False
    if inter == min(len(sa), len(sb)):        # subset relationship
        return True
    return inter / len(sa | sb) >= jaccard_min


def _cluster_conformed_dimensions(
    bindings: Dict[str, Dict],
    concept_values: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    """Fold role-variant concepts of one conformed dimension under a single
    canonical key (their shared stem). Returns ``{concept: stem}`` for every
    concept that moves; unmoved concepts are omitted.

    Two concepts merge only when BOTH signals agree: they share a role-stripped
    name stem AND their value sets corroborate (``_values_corroborate``). The
    stem gate stops coincidental tiny-set overlaps (``{ml}`` ~ ``{ml}`` across
    unrelated columns) from fusing; the value gate stops same-stemmed but
    genuinely different columns (amount vs rate units) from fusing. Only concepts
    with real profiled values participate — an unvalued concept cannot be
    confirmed, so it is never merged."""
    by_stem: Dict[str, List[str]] = {}
    for concept, b in bindings.items():
        if concept not in concept_values:
            continue
        by_stem.setdefault(_dimension_stem(b["col_name"]), []).append(concept)

    merge_map: Dict[str, str] = {}
    for stem, concepts in by_stem.items():
        if len(concepts) < 2:
            continue
        # Union-find over value corroboration — transitive WITHIN the stem group.
        parent = {c: c for c in concepts}

        def _find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(concepts)):
            for j in range(i + 1, len(concepts)):
                ci, cj = concepts[i], concepts[j]
                if _values_corroborate(concept_values[ci], concept_values[cj]):
                    parent[_find(ci)] = _find(cj)

        comps: Dict[str, List[str]] = {}
        for c in concepts:
            comps.setdefault(_find(c), []).append(c)
        for members in comps.values():
            if len(members) >= 2:
                for c in members:
                    merge_map[c] = stem
    return merge_map


def _disambiguate_vocab_canon(
    col_name: str,
    bound_classes: Set[URIRef],
    entity_uris: Set[URIRef],
    base_iri: str,
) -> URIRef:
    """Pick a vocabulary-class URI for a concept whose natural stem name collides
    with a table-entity class — e.g. a ``service`` value vocabulary whose stem
    ``Service`` clashes with the ``raw_services`` entity class. Preference order:

      1. a class the properties already point at that is itself non-entity — reuses
         the LLM's own disambiguated name (``ServiceType``);
      2. else the stem with a disambiguating suffix (``Type``/``Value``/…);
      3. else a numeric suffix — always terminating on a non-colliding URI.

    Deterministic and dataset-agnostic (keyed on names only). Re-deriving into this
    class keeps the completeness guarantee — every source value yields an individual —
    which bailing on the collision would forfeit."""
    candidates = sorted(
        (c for c in bound_classes if c not in entity_uris),
        key=lambda u: (len(str(u)), str(u)),
    )
    if candidates:
        return candidates[0]
    stem = _pascal(col_name)
    for suffix in ("Type", "Value", "Category", "Kind"):
        cand = URIRef(base_iri + stem + suffix)
        if cand not in entity_uris:
            return cand
    n = 2
    while True:
        cand = URIRef(base_iri + stem + str(n))
        if cand not in entity_uris:
            return cand
        n += 1


def _apply_concept_merge(
    bindings: Dict[str, Dict],
    concept_values: Dict[str, Dict[str, str]],
    merge_map: Dict[str, str],
) -> Tuple[Dict[str, Dict], Dict[str, Dict[str, str]]]:
    """Rewrite ``bindings`` and ``concept_values`` so every merged concept folds
    into its canonical stem key: union the parallel vocab classes and source
    columns, name the dimension from the stem, and merge the value maps (keeping
    the lexicographically smallest raw spelling per value, as elsewhere)."""
    new_bindings: Dict[str, Dict] = {}
    new_values: Dict[str, Dict[str, str]] = {}
    for concept, b in bindings.items():
        key = merge_map.get(concept, concept)
        nb = new_bindings.setdefault(key, {"classes": set(), "cols": [], "col_name": None})
        nb["classes"].update(b["classes"])
        for c in b["cols"]:
            if c not in nb["cols"]:
                nb["cols"].append(c)
        nb["col_name"] = key if concept in merge_map else b["col_name"]
        if concept in concept_values:
            dst = new_values.setdefault(key, {})
            for vk, raw in concept_values[concept].items():
                if vk not in dst or raw < dst[vk]:
                    dst[vk] = raw
    return new_bindings, new_values


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _parse_source_fk(graph: Graph, prop: URIRef) -> Optional[Tuple[str, str]]:
    """Recover ``(table, column)`` from a property's ``fm:sourceForeignKey`` annotation
    (``"raw_athletes.gender -> Gender"``).  Returns None for ``"not recoverable"``."""
    for ann in graph.objects(prop, _FM_SOURCE_FK):
        s = str(ann)
        if "->" not in s:
            continue
        left = s.split("->", 1)[0].strip()
        if "." in left:
            table, col = left.split(".", 1)
            table, col = table.strip(), col.strip()
            if table and col:
                return (table, col)
    return None


def _distinct_values(col_profile) -> List[str]:
    """The distinct profiled values of a column, from ``top_values`` (the same set the
    gen-LLM saw).  Order-preserving; raw strings."""
    out: List[str] = []
    for item in (getattr(col_profile, "top_values", None) or []):
        v = item.get("value") if isinstance(item, dict) else item
        if v is None:
            continue
        out.append(str(v))
    return out


def _remove_rdf_list(graph: Graph, head) -> None:
    """Remove an entire rdf:List collection (walks rdf:rest), so a stripped owl:oneOf
    leaves no orphan list nodes behind."""
    node = head
    seen: Set = set()
    while node is not None and node != RDF.nil and node not in seen:
        seen.add(node)
        nxt = next(graph.objects(node, RDF.rest), None)
        for t in list(graph.triples((node, None, None))):
            graph.remove(t)
        node = nxt


def _purge_enumeration(graph: Graph, cls: URIRef) -> None:
    """Drop any existing ``owl:equivalentClass [ owl:oneOf (...) ]`` on a class so the
    enumeration is rebuilt cleanly from the re-derived individuals."""
    for ec in list(graph.objects(cls, OWL.equivalentClass)):
        for lst in list(graph.objects(ec, OWL.oneOf)):
            _remove_rdf_list(graph, lst)
        for t in list(graph.triples((ec, None, None))):
            graph.remove(t)
        graph.remove((cls, OWL.equivalentClass, ec))


def _remove_subject(graph: Graph, node) -> None:
    """Remove every triple in which ``node`` is the subject or the object."""
    for t in list(graph.triples((node, None, None))):
        graph.remove(t)
    for t in list(graph.triples((None, None, node))):
        graph.remove(t)


def _vocab_class_comment(inherited: List[Literal], cols: List[Tuple[str, str]]) -> Literal:
    """rdfs:comment for a canonical vocabulary class: the (deterministically first)
    description inherited from a superseded parallel class, else one derived from the
    source columns the vocabulary is materialised from."""
    if inherited:
        return sorted(inherited, key=str)[0]
    src = ", ".join(f"{t}.{c}" for t, c in sorted(cols))
    return Literal(
        f"Controlled vocabulary of the distinct values of source column(s) {src}.",
        lang="en",
    )


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

def materialise_categorical_individuals(
    graph: Graph,
    profiles: List,
    base_iri: str,
) -> int:
    """Re-derive every value-mapped vocabulary deterministically from source values.

    Returns the number of individuals minted.  Idempotent on its own output (re-running
    produces the same graph).  Operates only on vocabulary classes — classes that are the
    ``rdfs:range`` of an ObjectProperty and are NOT a table-entity class; entity FKs
    (``gender`` → Gender is a vocab; ``venue_code`` → Venue is an entity) are untouched.
    """
    entity_uris: Set[URIRef] = {
        URIRef(derive_class_uri(p.table_name, base_iri)) for p in profiles
    }
    col_profiles: Dict[Tuple[str, str], object] = {}
    for p in profiles:
        for cn, cp in p.columns.items():
            col_profiles[(p.table_name, cn.lower())] = cp

    # ── 1. Discover concept bindings from vocab ObjectProperties ───────────
    # concept_key (normalised column name) → {classes: {old vocab class URIs},
    #                                          cols: [(table, col)], col_name: str}
    bindings: Dict[str, Dict] = {}
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        vocab_ranges = [
            r for r in graph.objects(prop, RDFS.range)
            if isinstance(r, URIRef)
            and (r, RDF.type, OWL.Class) in graph
            and r not in entity_uris
        ]
        if not vocab_ranges:
            continue
        tc = _parse_source_fk(graph, prop)
        if tc is None:
            continue  # abstract parent / unrecoverable — swept along in the repoint below
        table, col = tc
        if (table, col.lower()) not in col_profiles:
            continue
        concept = col.lower()
        b = bindings.setdefault(concept, {"classes": set(), "cols": [], "col_name": col})
        b["classes"].update(vocab_ranges)
        if (table, col) not in b["cols"]:
            b["cols"].append((table, col))

    if not bindings:
        return 0

    # ── 2. Gather distinct non-null, non-packed values per concept ─────────
    concept_values: Dict[str, Dict[str, str]] = {}  # concept → {norm_value: raw_value}
    for concept, b in bindings.items():
        raws_by_key: Dict[str, Set[str]] = {}
        for (table, col) in b["cols"]:
            for raw in _distinct_values(col_profiles[(table, col.lower())]):
                if _is_null_like(raw) or _looks_packed(raw):
                    continue
                key = str(raw).strip().lower()
                if not key:
                    continue
                raws_by_key.setdefault(key, set()).add(str(raw).strip())
        if raws_by_key:
            # Deterministic raw spelling per value (lexicographically smallest).
            concept_values[concept] = {k: min(v) for k, v in raws_by_key.items()}

    # ── 2b. Fold role-variant concepts into conformed dimensions ───────────
    # curr_careunit / first_careunit / last_careunit / prev_careunit are four
    # columns but ONE dimension; without this they mint four parallel classes,
    # each with a duplicate copy of every care unit. Merge them into one keyed
    # on the shared stem, confirmed by value overlap.
    merge_map = _cluster_conformed_dimensions(bindings, concept_values)
    if merge_map:
        bindings, concept_values = _apply_concept_merge(bindings, concept_values, merge_map)
        logger.info(
            "categorical: folded %d role-variant concept(s) into %d conformed "
            "dimension(s): {%s}",
            len(merge_map), len(set(merge_map.values())),
            ", ".join(sorted(set(merge_map.values()))),
        )

    # ── 3. Resolve the canonical class per concept + reverse map ───────────
    concept_canon: Dict[str, URIRef] = {}
    class_to_concept: Dict[URIRef, str] = {}
    for concept, b in bindings.items():
        if concept not in concept_values:
            continue  # no real values (e.g. fully-packed column) — leave the LLM output
        canon = URIRef(base_iri + _pascal(b["col_name"]))
        if canon in entity_uris:
            # The stem name collides with a table-entity class. Two cases:
            #   (a) it collides with the concept's OWN source-table entity — a value
            #       vocab in 'raw_services' whose stem 'Service' clashes with that
            #       table's own entity. A column cannot be a value-FK to its own
            #       table, so this is a coincidental clash: re-derive into a
            #       non-colliding vocab class rather than abandon the vocabulary.
            #   (b) it collides with a DIFFERENT table's entity — a genuine external
            #       dimension the column references (athletes.gender → a real
            #       'raw_genders' entity). Materialising here would duplicate that
            #       dimension, so leave the LLM vocabulary untouched (unchanged).
            own_entities = {
                URIRef(derive_class_uri(t, base_iri)) for (t, _c) in b["cols"]
            }
            if canon in own_entities:
                canon = _disambiguate_vocab_canon(
                    b["col_name"], b["classes"], entity_uris, base_iri
                )
                logger.info(
                    "categorical: concept %r stem collides with its own table entity "
                    "— re-deriving into non-colliding vocab class <%s>", concept, canon,
                )
            else:
                logger.warning(
                    "categorical: concept %r canonical <%s> collides with an external "
                    "entity class — leaving LLM vocabulary untouched", concept, canon,
                )
                continue
        concept_canon[concept] = canon
        for cls in b["classes"] | {canon}:
            if cls in class_to_concept and class_to_concept[cls] != concept:
                logger.warning(
                    "categorical: class <%s> claimed by concepts %r and %r — keeping %r",
                    cls, class_to_concept[cls], concept, class_to_concept[cls],
                )
                continue
            class_to_concept[cls] = concept

    if not concept_canon:
        return 0

    # ── 4. Strip stale enumerations + remove the LLM's vocab individuals ───
    affected_classes = set(class_to_concept)
    old_individuals: Set = set()
    for cls in affected_classes:
        _purge_enumeration(graph, cls)
        for ind in graph.subjects(RDF.type, cls):
            old_individuals.add(ind)
    for ind in old_individuals:
        _remove_subject(graph, ind)

    # ── 5. Repoint every range onto the canonical class, then drop parallels ─
    # Carry the superseded classes' descriptions over: the canonical class is often
    # freshly minted (named from the column), so dropping the parallels would
    # otherwise drop the only rdfs:comment the concept had.
    inherited_comments: Dict[str, List[Literal]] = {}
    for old_cls, concept in class_to_concept.items():
        canon = concept_canon[concept]
        if old_cls == canon:
            continue
        inherited_comments.setdefault(concept, []).extend(
            o for o in graph.objects(old_cls, RDFS.comment) if isinstance(o, Literal)
        )
        for prop in list(graph.subjects(RDFS.range, old_cls)):
            graph.remove((prop, RDFS.range, old_cls))
            graph.add((prop, RDFS.range, canon))
        _remove_subject(graph, old_cls)  # drop the now-orphaned parallel class

    # ── 6. Mint exactly one individual per distinct value ──────────────────
    minted = 0
    for concept, canon in concept_canon.items():
        c_local = str(canon).split("#")[-1].split("/")[-1]
        if (canon, RDF.type, OWL.Class) not in graph:
            graph.add((canon, RDF.type, OWL.Class))
        if not list(graph.objects(canon, RDFS.label)):
            graph.add((canon, RDFS.label, Literal(_humanize(c_local), lang="en")))
        if not list(graph.objects(canon, RDFS.comment)):
            graph.add((canon, RDFS.comment, _vocab_class_comment(
                inherited_comments.get(concept, []), bindings[concept]["cols"],
            )))

        seen_uris: Set[URIRef] = set()
        for key in sorted(concept_values[concept]):
            raw = concept_values[concept][key]
            ind = URIRef(base_iri + _value_slug(raw) + c_local)
            if ind in seen_uris:
                continue  # two raw spellings collapsed to one slug — first (sorted) wins
            seen_uris.add(ind)
            graph.add((ind, RDF.type, OWL.NamedIndividual))
            graph.add((ind, RDF.type, canon))
            graph.add((ind, RDFS.label, Literal(raw)))
            minted += 1
        logger.info(
            "categorical: <%s> ← %d individual(s) from %d source column(s): {%s}",
            c_local, len(seen_uris), len(bindings[concept]["cols"]),
            ", ".join(sorted(str(i).split("#")[-1] for i in seen_uris)),
        )

    return minted
