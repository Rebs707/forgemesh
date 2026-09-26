# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import re
import logging
from typing import Any, Dict, List, Optional, Set
from sqlalchemy import create_engine, inspect, text, engine
from semantic_mapper.db.models import TableProfile, ColumnProfile, DataQualityWarning
from semantic_mapper.config.settings import Settings

logger = logging.getLogger(__name__)

# Column-name suffixes that strongly suggest an identifier column (issue #44)
_ID_PATTERN = re.compile(r"(_id|_key|_ref|_token|_hash|_guid|_uuid)$", re.IGNORECASE)

# Heuristic PK detection: column name must match one of these patterns
_PK_NAME_PATTERN = re.compile(r"^(id|uuid|guid)$", re.IGNORECASE)


def _infer_pk(col_name: str, distinct_count: int, row_count: int, null_rate: float) -> bool:
    """Return True when a column looks like a primary key heuristically (issue #70).

    Conditions (all required):
    - Name is exactly 'id', 'uuid', or 'guid' (case-insensitive)
    - No null values
    - All values are distinct (distinct_count == row_count)
    """
    if not _PK_NAME_PATTERN.match(col_name):
        return False
    if null_rate > 0 or row_count == 0:
        return False
    return distinct_count == row_count


_NATURAL_KEY_UNIQUENESS = 0.95   # distinct/row_count threshold to be a natural key
_NATURAL_KEY_NULL_RATE  = 0.05   # max null rate to qualify as a natural key

_FK_OVERLAP_SAMPLE    = 200   # distinct source values to sample for value-overlap FK resolution
_FK_OVERLAP_THRESHOLD = 0.80  # minimum overlap fraction to accept a FK candidate

_FREE_TEXT_FRACTION = 0.5     # min fraction of multi-word sample values to call a column free text


def _looks_free_text(col) -> bool:
    """Return True when a column's values look like natural-language free text.

    Descriptive columns (person names, place names, titles) are frequently
    near-unique in their home table, yet they are *attributes*, not identifiers.
    If such a column seeds the natural-key map, every same-named column elsewhere
    inherits a spurious FK (e.g. a team `name` → an athletes table, or a
    descriptive `venue` → a venues table). The structural tell is internal
    whitespace: identifiers and codes are single tokens; free text is multi-word.
    """
    samples = [s for s in (col.sample_data or []) if isinstance(s, str) and s.strip()]
    if not samples:
        return False
    multiword = sum(1 for s in samples if " " in s.strip())
    return multiword / len(samples) >= _FREE_TEXT_FRACTION


def _build_natural_key_map(profiles: List[TableProfile]) -> Dict[str, str]:
    """Return {col_name_lower: table_name} for columns that are near-unique in their table.

    A "natural key" is a non-PK column whose values are almost entirely distinct
    (≥95% uniqueness, <5% nulls). These are surrogate/business identifiers that
    other tables reference as FKs (e.g. MIMIC's subject_id, hadm_id, icustay_id).

    When multiple tables have a natural key with the same column name, the table
    with the highest uniqueness ratio wins (avoids ambiguity from lookup/reference
    tables that happen to have the same column).
    """
    candidates: Dict[str, tuple] = {}  # col_lower → (uniqueness, table_name)
    for p in profiles:
        if p.row_count == 0:
            continue
        for col_name, col in p.columns.items():
            if col.is_primary_key:
                continue
            # Date/time-formatted columns are never FK targets — a near-unique
            # date column (e.g. encounter start) must not enter the natural-key
            # map, otherwise any other table with a same-named date column gets
            # a spurious FK (e.g. raw_conditions.start → raw_encounters).
            if col.detected_formats:
                continue
            # Free-text descriptive columns (names, titles, place names) are
            # near-unique but are attributes, not identifiers — keep them out of
            # the natural-key map so a same-named column elsewhere doesn't inherit
            # a spurious FK (e.g. team name → athletes, schedule venue → venues).
            if _looks_free_text(col):
                continue
            uniqueness = col.distinct_count / p.row_count
            if uniqueness >= _NATURAL_KEY_UNIQUENESS and col.null_rate < _NATURAL_KEY_NULL_RATE:
                key = col_name.lower()
                if key not in candidates or uniqueness > candidates[key][0]:
                    candidates[key] = (uniqueness, p.table_name)
    return {k: v[1] for k, v in candidates.items()}


def _build_pk_name_map(profiles: List[TableProfile]) -> Dict[str, List[str]]:
    """Return {col_name_lower: [table_name, ...]} for all declared PK columns.

    Unlike _build_natural_key_map (non-PK near-unique columns), this maps
    columns that ARE the primary key of their table.  Used by Strategy 3:
    if a non-PK column's name matches a PK in another table it is very
    likely a FK — regardless of naming convention or _id suffix.
    When multiple tables share the same PK name (e.g. itemid in both
    d_items and d_labitems), all candidates are returned for value-overlap
    resolution in _apply_inferred_fks.
    """
    pk_map: Dict[str, List[str]] = {}
    for p in profiles:
        for col_name, col in p.columns.items():
            if col.is_primary_key:
                pk_map.setdefault(col_name.lower(), []).append(p.table_name)
    return pk_map


def _ambiguous_pk_fks(
    profiles: List[TableProfile],
) -> Dict[str, List[Dict]]:
    """Return columns whose name matches the PK of *multiple* tables.

    These cannot be resolved from schema structure alone and require
    value-overlap sampling against the live database.

    Returns {table_name: [{"column": col, "candidates": [t1, t2, ...]}]}
    """
    pk_name_map = _build_pk_name_map(profiles)
    result: Dict[str, List[Dict]] = {}
    for table in profiles:
        explicit_fk_cols = {fk["column"] for fk in table.foreign_keys}
        pk_cols = {n for n, c in table.columns.items() if c.is_primary_key}
        for col_name in table.columns:
            if col_name in explicit_fk_cols or col_name in pk_cols:
                continue
            candidates = [
                t for t in pk_name_map.get(col_name.lower(), [])
                if t != table.table_name
            ]
            if len(candidates) > 1:
                result.setdefault(table.table_name, []).append(
                    {"column": col_name, "candidates": candidates}
                )
    return result


def _natural_key_candidate_map(profiles: List[TableProfile]) -> Dict[str, List[str]]:
    """Return ``{col_lower: [table, ...]}`` for every table where a column is a
    near-unique natural key — ALL candidates, unlike :func:`_build_natural_key_map`
    which keeps only the highest-uniqueness winner.

    Used to detect natural keys that are near-unique in MULTIPLE tables (e.g.
    ``icd9_code`` in both ``d_icd_diagnoses`` and ``d_icd_procedures``, neither of
    which declares a PK). Such a key cannot be collapsed onto one winner — a
    referencing column must be disambiguated by value-overlap, the same way an
    ambiguous PK-name match is.
    """
    m: Dict[str, List[str]] = {}
    for p in profiles:
        if p.row_count == 0:
            continue
        for col_name, col in p.columns.items():
            if col.is_primary_key or col.detected_formats or _looks_free_text(col):
                continue
            uniqueness = col.distinct_count / p.row_count
            if uniqueness >= _NATURAL_KEY_UNIQUENESS and col.null_rate < _NATURAL_KEY_NULL_RATE:
                m.setdefault(col_name.lower(), []).append(p.table_name)
    return m


def _ambiguous_natural_key_fks(profiles: List[TableProfile]) -> Dict[str, List[Dict]]:
    """Return columns whose name is a near-unique natural key in *multiple* tables.

    Same shape and purpose as :func:`_ambiguous_pk_fks`, but for natural keys (no
    declared PK): the referencing column (non-unique locally → a FK, not the key
    itself) must be resolved by value-overlap sampling against the shared column in
    each candidate. Returns ``{table_name: [{"column": col, "candidates": [...]}]}``.
    """
    nk_cand = _natural_key_candidate_map(profiles)
    result: Dict[str, List[Dict]] = {}
    for table in profiles:
        explicit_fk_cols = {fk["column"] for fk in table.foreign_keys}
        pk_cols = {n for n, c in table.columns.items() if c.is_primary_key}
        for col_name, col in table.columns.items():
            if col_name in explicit_fk_cols or col_name in pk_cols or col.detected_formats:
                continue
            # The referencing column must be non-unique locally — a near-unique
            # column IS a key, not a reference to another table's key.
            if table.row_count > 0 and col.distinct_count / table.row_count >= _NATURAL_KEY_UNIQUENESS:
                continue
            candidates = [
                t for t in nk_cand.get(col_name.lower(), []) if t != table.table_name
            ]
            if len(candidates) > 1:
                result.setdefault(table.table_name, []).append(
                    {"column": col_name, "candidates": candidates}
                )
    return result


_TEXT_SQL_TYPES = ("CHAR", "TEXT", "VARCHAR", "CLOB", "STRING", "ENUM")


def _is_text_type(inferred_type: str) -> bool:
    """True when a column's SQL type is textual (the domain of codes and names).

    A value-overlap entity link is fundamentally text↔text: the referencing column
    repeats a textual identifier (a venue code, a country code, a category name).
    Restricting both ends to text is what separates a real reference from a coincidental
    numeric overlap — small-integer measures (medal counts 0–3) trivially fall inside any
    surrogate integer key's range, and string/int type coercion makes ``'1' IN (1,2,3)``
    spuriously true.  Genuine integer keys are high-cardinality and resolved by
    Strategies 1–4, not by this categorical fallback.
    """
    return any(k in (inferred_type or "").upper() for k in _TEXT_SQL_TYPES)


def _value_overlap_targets(profiles: List[TableProfile]) -> List[tuple]:
    """Return ``[(table_name, col_name, distinct_count), ...]`` for the text-typed
    PRIMARY KEY of every table — the candidate *keys* a value-overlap FK can point at.

    The issue frames inference as "distinct values are a near-subset of the KEY of another
    table", and Option B anchors each entity on its preserved natural key — so the target
    is the table's primary key, not an arbitrary near-unique attribute.  Targeting the key
    (not, say, a near-unique ``country`` NAME column that merely shadows a ``country_code``
    PK) is what stops a same-domain-but-wrong-table link.  Heuristic PKs count; date and
    non-text keys do not (see :func:`_is_text_type`).
    """
    out: List[tuple] = []
    for p in profiles:
        if p.row_count == 0:
            continue
        for col_name, col in p.columns.items():
            if (
                col.is_primary_key
                and not col.detected_formats
                and _is_text_type(col.inferred_type)
            ):
                out.append((p.table_name, col_name, col.distinct_count))
    return out


def _infer_fks(profiles: List[TableProfile]) -> Dict[str, List[Dict[str, str]]]:
    """Return {table_name: [{"column": ..., "ref_table": ..., "inferred": True}]} (issue #70).

    Three complementary strategies, applied in order until one resolves:

    1. Natural-key match: if a column is near-unique in table B, any other table
       that has a column with the same name (but is NOT near-unique there) likely
       holds a FK to table B.  Catches MIMIC-style naming where column names like
       subject_id / hadm_id don't match table names directly.

    2. Table-name match: column name (after stripping a trailing _id suffix) matches
       another table's base name (after stripping raw_ prefix and optionally a
       trailing 's').  Catches the common "patient_id → patients" convention.

    3. PK-name match: column name exactly matches the PK column of exactly one
       other table.  Catches bare identifiers like cgid and itemid that fail
       Strategies 1 and 2 because the PK is excluded from the natural-key map
       and the name carries no _id suffix pointing to a table.
       Ambiguous cases (column matches PKs in *multiple* tables) are NOT resolved
       here — they are returned by _ambiguous_pk_fks() and resolved via
       value-overlap sampling inside _apply_inferred_fks().
    """
    natural_key_map = _build_natural_key_map(profiles)
    nk_candidate_map = _natural_key_candidate_map(profiles)
    pk_name_map     = _build_pk_name_map(profiles)

    # Build base-name → actual table name mapping (strategy 2)
    table_base: Dict[str, str] = {}
    for p in profiles:
        base = re.sub(r"^raw_", "", p.table_name, flags=re.IGNORECASE).lower()
        table_base[base] = p.table_name
        if base.endswith("s"):
            table_base[base[:-1]] = p.table_name

    result: Dict[str, List[Dict[str, str]]] = {}
    for table in profiles:
        explicit_fk_cols = {fk["column"] for fk in table.foreign_keys}
        pk_cols = {n for n, c in table.columns.items() if c.is_primary_key}
        inferred: List[Dict[str, str]] = []
        seen: Set[str] = set()

        for col_name, col in table.columns.items():
            if col_name in explicit_fk_cols or col_name in pk_cols:
                continue
            # Columns with detected date/time formats are never FK references.
            # A date value cannot be a surrogate key into another table.
            if col.detected_formats:
                continue
            cn_lo = col_name.lower()
            ref_table = None

            # Strategy 1: natural key match (UNAMBIGUOUS only). A column that is a
            # near-unique natural key in MULTIPLE tables (e.g. icd9_code in both ICD
            # dimensions) cannot be resolved from the name alone — defer it to
            # value-overlap in _apply_inferred_fks rather than collapse onto the
            # highest-uniqueness winner (which mis-points every referencing table).
            nk_external = [
                t for t in nk_candidate_map.get(cn_lo, []) if t != table.table_name
            ]
            nk_table = natural_key_map.get(cn_lo)
            if nk_table and nk_table != table.table_name and len(nk_external) <= 1:
                if table.row_count > 0:
                    local_uniqueness = col.distinct_count / table.row_count
                    if local_uniqueness < _NATURAL_KEY_UNIQUENESS:
                        ref_table = nk_table

            # Strategy 2: table-name match
            # Skip low-cardinality categoricals: a value column that merely shares
            # a table's (singular) name — e.g. a 'medal' GOLD/SILVER/BRONZE column
            # vs a 'medals' table — is an attribute to be value-mapped, not a FK.
            # top_values is populated only for low-cardinality columns, so its
            # presence marks the column as categorical rather than a foreign key.
            if not ref_table and not col.top_values:
                cn_base = re.sub(r"_id$", "", cn_lo)
                candidate = table_base.get(cn_base) or table_base.get(cn_lo)
                if candidate and candidate != table.table_name:
                    ref_table = candidate

            # Strategy 3: PK-name match (unambiguous case only)
            if not ref_table:
                pk_candidates = [
                    t for t in pk_name_map.get(cn_lo, [])
                    if t != table.table_name
                ]
                if len(pk_candidates) == 1:
                    ref_table = pk_candidates[0]
                # len > 1 → ambiguous, deferred to value-overlap in _apply_inferred_fks

            # Strategy 4: role-prefixed FK pattern — {role}_{entity_col}
            # e.g. dest_airport_id → strip "dest_" → airport_id, which IS a
            # natural key of raw_airports.  Also catches source_airport_id etc.
            # Only activates when the column has a role prefix (≥2 parts after
            # stripping _id suffix) and is non-unique locally.
            if not ref_table:
                col_parts = cn_lo.split("_")
                if table.row_count > 0:
                    local_uniqueness = col.distinct_count / table.row_count
                    if local_uniqueness < _NATURAL_KEY_UNIQUENESS and len(col_parts) >= 3:
                        for i in range(1, len(col_parts)):
                            suffix = "_".join(col_parts[i:])
                            if len(suffix) <= 2:
                                continue  # "id" alone is too generic
                            nk_table = natural_key_map.get(suffix)
                            if nk_table and nk_table != table.table_name:
                                ref_table = nk_table
                                break

            if ref_table and col_name not in seen:
                seen.add(col_name)
                inferred.append({"column": col_name, "ref_table": ref_table, "inferred": True})

        if inferred:
            result[table.table_name] = inferred
    return result


def _prune_redundant_descriptive_fks(profiles: List[TableProfile]) -> None:
    """Drop a redundant non-key-aligned inferred FK in-place (issue: descriptive FK over-firing).

    When a source table references the same target via two columns, and one
    column's name matches the target's PRIMARY KEY name (the genuine key, e.g.
    country_code → medals.country_code) while the other is only a descriptive or
    value match to the same target (e.g. country → medals, or a person `name`
    alongside a `code` that already resolves there), the descriptive one is
    redundant. Keep the key-aligned FK and drop the other.

    Only fires when a key-aligned sibling exists, so a role-prefixed pair such as
    source_airport_id / dest_airport_id → airports (neither matching the airports
    PK name) is left untouched — both legitimate FKs survive. Explicit, DB-declared
    foreign keys (no ``inferred`` flag) are never pruned.
    """
    pk_names = {
        p.table_name: {n.lower() for n, c in p.columns.items() if c.is_primary_key}
        for p in profiles
    }
    for profile in profiles:
        by_target: Dict[str, List[Dict]] = {}
        for fk in profile.foreign_keys:
            if fk.get("inferred"):
                by_target.setdefault(fk["ref_table"], []).append(fk)
        drop_cols: Set[str] = set()
        for ref_table, fks in by_target.items():
            if len(fks) < 2:
                continue
            target_pks = pk_names.get(ref_table, set())
            aligned = [fk for fk in fks if fk["column"].lower() in target_pks]
            if aligned and len(aligned) < len(fks):
                for fk in fks:
                    if fk["column"].lower() not in target_pks:
                        drop_cols.add(fk["column"])
        if drop_cols:
            profile.foreign_keys = [
                fk for fk in profile.foreign_keys
                if not (fk.get("inferred") and fk["column"] in drop_cols)
            ]
            logger.debug(
                "Pruned %d redundant descriptive FK(s) from %s: %s",
                len(drop_cols), profile.table_name, sorted(drop_cols),
            )


def _is_low_cardinality(
    col_name: str,
    distinct_count: int,
    row_count: int,
    is_primary_key: bool,
    settings: Settings,
) -> bool:
    """Return True when a column warrants top_values population (issue #44).

    Requires ALL of:
    - Not a primary key
    - Column name does not match identifier patterns
    - distinct_count <= LOW_CARDINALITY_THRESHOLD (absolute ceiling)
    - distinct_count / row_count <= LOW_CARDINALITY_RATIO (or row_count == 0)
    """
    if is_primary_key:
        return False
    if _ID_PATTERN.search(col_name):
        return False
    if distinct_count > settings.LOW_CARDINALITY_THRESHOLD:
        return False
    # Ratio check only makes sense on tables large enough to distinguish ENUM-like
    # columns from free-text columns. On small tables the ratio is always high even
    # for genuine categoricals, so skip it below 500 rows.
    if row_count >= 500 and distinct_count / row_count > settings.LOW_CARDINALITY_RATIO:
        return False
    return True

class MySQLInspector:
    DATE_PATTERNS = {
        # Date-only formats
        r"^\d{4}-\d{2}-\d{2}$":          "YYYY-MM-DD",
        r"^\d{2}/\d{2}/\d{4}$":          "DD/MM/YYYY",
        r"^\d{4}/\d{2}/\d{2}$":          "YYYY/MM/DD",
        # ISO 8601 datetime (with/without timezone, with/without milliseconds)
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$":            "ISO8601-UTC",
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$":       "ISO8601-UTC-ms",
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$": "ISO8601-TZ",
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$":             "ISO8601",
        # Space-separated datetime (MySQL DATETIME canonical form)
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$":             "YYYY-MM-DD HH:MM:SS",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = create_engine(
            settings.DATABASE_URL,
            execution_options={"isolation_level": "READ UNCOMMITTED"}
        )

    def profile_all(self) -> List[TableProfile]:
        inspector = inspect(self.engine)
        profiles = [self._profile_table(name, inspector) for name in inspector.get_table_names()]
        self._apply_inferred_fks(profiles)
        return profiles

    def _apply_inferred_fks(self, profiles: List[TableProfile]) -> None:
        """Merge heuristically-inferred FKs into profiles (issue #70).

        Runs all three inference strategies and merges the results:
        - Strategies 1–3 (pure schema analysis) via _infer_fks().
        - Strategy 3 ambiguous cases (same PK name in multiple tables) resolved
          by value-overlap sampling against the live database.
        """
        inferred_map = _infer_fks(profiles)

        # Resolve ambiguous PK-name matches via value-overlap sampling
        for tbl_name, cases in _ambiguous_pk_fks(profiles).items():
            for case in cases:
                col = case["column"]
                # Skip if already resolved by Strategies 1 or 2
                if any(e["column"] == col for e in inferred_map.get(tbl_name, [])):
                    continue
                best = self._resolve_by_value_overlap(
                    tbl_name, col, case["candidates"], profiles
                )
                if best:
                    inferred_map.setdefault(tbl_name, []).append(
                        {"column": col, "ref_table": best, "inferred": True}
                    )

        # Resolve ambiguous natural-key matches (a column near-unique in multiple
        # tables, e.g. icd9_code in both ICD dimensions) by value-overlap on the
        # SHARED column — the candidate tables have no PK, so overlap is measured
        # against the same-named column, not a PK.
        for tbl_name, cases in _ambiguous_natural_key_fks(profiles).items():
            for case in cases:
                col = case["column"]
                if any(e["column"] == col for e in inferred_map.get(tbl_name, [])):
                    continue
                best, best_score = None, 0.0
                for cand in case["candidates"]:
                    score = self._value_overlap_fraction(tbl_name, col, cand, col)
                    if score > best_score:
                        best_score, best = score, cand
                if best and best_score >= _FK_OVERLAP_THRESHOLD:
                    inferred_map.setdefault(tbl_name, []).append(
                        {"column": col, "ref_table": best, "inferred": True}
                    )
                    logger.info(
                        "FK inferred by value overlap (ambiguous natural key): "
                        "%s.%s → %s (score=%.2f)", tbl_name, col, best, best_score,
                    )

        # Strategy 5 — value-overlap entity link.
        # A low-cardinality column that would otherwise be discriminated into per-value
        # individuals, but whose distinct values are a near-subset of another table's
        # IDENTIFYING column, is a foreign key — not a categorical attribute.  This is the
        # general fallback for relationships that are real in the data yet never declared:
        # the reference is by value (e.g. a `category` column repeating a categories
        # table's name/code), so value overlap — not a name or _id convention — is the
        # evidence.  Skipped when a coded sibling (`{col}_code` / `{col}_id`) is already an
        # FK: that reference is carried by the coded column and this one is its descriptive
        # shadow (demoted to a literal downstream, never re-inferred as a second link).
        self._infer_value_overlap_fks(profiles, inferred_map)

        for profile in profiles:
            extra = inferred_map.get(profile.table_name, [])
            if not extra:
                continue
            existing_cols = {fk["column"] for fk in profile.foreign_keys}
            new_fks = [e for e in extra if e["column"] not in existing_cols]
            if new_fks:
                profile.foreign_keys.extend(new_fks)
                logger.debug(
                    "Inferred %d FK(s) for %s: %s",
                    len(new_fks), profile.table_name,
                    [e["column"] for e in new_fks],
                )

        # Drop redundant descriptive FKs left after all strategies have merged.
        _prune_redundant_descriptive_fks(profiles)

    def _infer_value_overlap_fks(
        self,
        profiles: List[TableProfile],
        inferred_map: Dict[str, List[Dict[str, str]]],
    ) -> None:
        """Strategy 5 — add value-overlap entity-link FKs to ``inferred_map`` in place.

        For each categorical (``top_values``-bearing) column not already resolved by
        Strategies 1–4 and not the descriptive shadow of a coded FK, sample its distinct
        values against the identifying columns of every other table.  The candidate with
        the highest overlap ≥ ``_FK_OVERLAP_THRESHOLD`` becomes an inferred FK.
        """
        targets = _value_overlap_targets(profiles)
        for profile in profiles:
            fk_cols = {fk["column"] for fk in profile.foreign_keys}
            fk_cols |= {e["column"] for e in inferred_map.get(profile.table_name, [])}
            fk_cols_lo = {c.lower() for c in fk_cols}
            pk_cols = {n for n, c in profile.columns.items() if c.is_primary_key}
            for col_name, col in profile.columns.items():
                if col_name in fk_cols or col_name in pk_cols or col.detected_formats:
                    continue
                if not col.top_values:
                    continue  # only low-cardinality discrimination candidates
                if not _is_text_type(col.inferred_type):
                    continue  # entity references by value are textual codes/names
                cl = col_name.lower()
                if f"{cl}_code" in fk_cols_lo or f"{cl}_id" in fk_cols_lo:
                    continue  # descriptive shadow of an existing coded FK
                best_table: Optional[str] = None
                best_score = 0.0
                for t_table, t_col, t_distinct in targets:
                    if t_table == profile.table_name:
                        continue
                    # A near-subset needs the target to hold ≥ threshold·|source| distinct
                    # values — cheap pre-filter before touching the database.
                    if t_distinct < col.distinct_count * _FK_OVERLAP_THRESHOLD:
                        continue
                    score = self._value_overlap_fraction(
                        profile.table_name, col_name, t_table, t_col
                    )
                    if score > best_score:
                        best_score, best_table = score, t_table
                if best_table and best_score >= _FK_OVERLAP_THRESHOLD:
                    inferred_map.setdefault(profile.table_name, []).append(
                        {"column": col_name, "ref_table": best_table, "inferred": True}
                    )
                    logger.info(
                        "FK inferred by value overlap (entity link): %s.%s → %s (score=%.2f)",
                        profile.table_name, col_name, best_table, best_score,
                    )

    def _value_overlap_fraction(
        self, source_table: str, source_col: str, target_table: str, target_col: str
    ) -> float:
        """Fraction of distinct non-null ``source_col`` values that also appear in
        ``target_col``.  Samples up to ``_FK_OVERLAP_SAMPLE`` distinct source values.
        Returns 0.0 on error or empty sample.
        """
        q = '"' if self.engine.dialect.name == "sqlite" else "`"
        try:
            with self.engine.connect() as conn:
                sample_sql = text(
                    f"SELECT COUNT(*) FROM ("
                    f"  SELECT DISTINCT {q}{source_col}{q} AS val FROM {q}{source_table}{q}"
                    f"  WHERE {q}{source_col}{q} IS NOT NULL LIMIT {_FK_OVERLAP_SAMPLE}"
                    f") AS _sample"
                )
                sample_n = conn.execute(sample_sql).scalar() or 0
                if sample_n == 0:
                    return 0.0
                overlap_sql = text(
                    f"SELECT COUNT(*) FROM ("
                    f"  SELECT DISTINCT {q}{source_col}{q} AS val FROM {q}{source_table}{q}"
                    f"  WHERE {q}{source_col}{q} IS NOT NULL LIMIT {_FK_OVERLAP_SAMPLE}"
                    f") AS _sample WHERE _sample.val IN ("
                    f"  SELECT {q}{target_col}{q} FROM {q}{target_table}{q}"
                    f")"
                )
                overlap_n = conn.execute(overlap_sql).scalar() or 0
                return overlap_n / sample_n
        except Exception as exc:
            logger.warning(
                "value-overlap %s.%s → %s.%s failed: %s",
                source_table, source_col, target_table, target_col, exc,
            )
            return 0.0

    def _resolve_by_value_overlap(
        self,
        source_table: str,
        source_col: str,
        candidates: List[str],
        profiles: List[TableProfile],
    ) -> Optional[str]:
        """Pick the FK target from candidates by sampling value overlap.

        Takes up to _FK_OVERLAP_SAMPLE distinct non-null values from
        source_table.source_col and checks what fraction appear in each
        candidate table's PK column.  Returns the candidate with the
        highest overlap that meets _FK_OVERLAP_THRESHOLD, or None.
        """
        q = '"' if self.engine.dialect.name == "sqlite" else "`"
        best_table: Optional[str] = None
        best_score = 0.0

        for candidate_table in candidates:
            cand_profile = next(
                (p for p in profiles if p.table_name == candidate_table), None
            )
            if not cand_profile:
                continue
            pk_cols = [n for n, c in cand_profile.columns.items() if c.is_primary_key]
            if not pk_cols:
                continue
            target_pk = pk_cols[0]

            try:
                with self.engine.connect() as conn:
                    # Count how many sampled source values appear in the target PK.
                    # The outer SELECT gives the actual sample size (may be < _FK_OVERLAP_SAMPLE
                    # when the source column has few distinct values).
                    sample_sql = text(
                        f"SELECT COUNT(*) FROM ("
                        f"  SELECT DISTINCT {q}{source_col}{q} AS val"
                        f"  FROM {q}{source_table}{q}"
                        f"  WHERE {q}{source_col}{q} IS NOT NULL"
                        f"  LIMIT {_FK_OVERLAP_SAMPLE}"
                        f") AS _sample"
                    )
                    sample_n = conn.execute(sample_sql).scalar() or 0
                    if sample_n == 0:
                        continue

                    overlap_sql = text(
                        f"SELECT COUNT(*) FROM ("
                        f"  SELECT DISTINCT {q}{source_col}{q} AS val"
                        f"  FROM {q}{source_table}{q}"
                        f"  WHERE {q}{source_col}{q} IS NOT NULL"
                        f"  LIMIT {_FK_OVERLAP_SAMPLE}"
                        f") AS _sample"
                        f" WHERE _sample.val IN ("
                        f"  SELECT {q}{target_pk}{q} FROM {q}{candidate_table}{q}"
                        f")"
                    )
                    overlap_n = conn.execute(overlap_sql).scalar() or 0
                    score = overlap_n / sample_n

                    logger.debug(
                        "FK overlap %s.%s → %s: %d/%d = %.2f",
                        source_table, source_col, candidate_table,
                        overlap_n, sample_n, score,
                    )
                    if score > best_score:
                        best_score = score
                        best_table = candidate_table
            except Exception as exc:
                logger.warning(
                    "FK overlap check failed %s.%s → %s: %s",
                    source_table, source_col, candidate_table, exc,
                )

        if best_score >= _FK_OVERLAP_THRESHOLD:
            logger.info(
                "FK resolved by value overlap: %s.%s → %s (score=%.2f)",
                source_table, source_col, best_table, best_score,
            )
            return best_table
        return None

    def _profile_table(self, table_name: str, ins: engine.Inspector) -> TableProfile:
        with self.engine.connect() as conn:
            row_count = conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}`")).scalar() or 0
            columns_meta = ins.get_columns(table_name)
            pk_cols = ins.get_pk_constraint(table_name).get("constrained_columns", [])
            fks = ins.get_foreign_keys(table_name)

            profiles = {c['name']: self._profile_column(table_name, c, pk_cols, conn, row_count) for c in columns_meta}

            return TableProfile(
                table_name=table_name,
                row_count=row_count,
                columns=profiles,
                foreign_keys=[{"column": f['constrained_columns'][0], "ref_table": f['referred_table']} for f in fks]
            )

    def _profile_column(self, table_name, col_meta, pk_cols, conn, total_rows) -> ColumnProfile:
        name = col_meta['name']
        sample = [r[0] for r in conn.execute(text(f"SELECT `{name}` FROM `{table_name}` WHERE `{name}` IS NOT NULL LIMIT {self.settings.SAMPLE_SIZE}")).fetchall()]

        null_count = conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}` WHERE `{name}` IS NULL")).scalar() or 0
        null_rate = null_count / total_rows if total_rows > 0 else 0

        # True cardinality from DB (sample-based len(set()) misses values absent from sample)
        distinct_count = conn.execute(
            text(f"SELECT COUNT(DISTINCT `{name}`) FROM `{table_name}`")
        ).scalar() or 0

        # Full value distribution for low-cardinality columns (issue #44)
        # Use declared PK status here; heuristic PK is resolved below.
        top_values = []
        if _is_low_cardinality(name, distinct_count, total_rows, name in pk_cols, self.settings):
            rows = conn.execute(text(
                f"SELECT `{name}`, COUNT(*) as cnt "
                f"FROM `{table_name}` WHERE `{name}` IS NOT NULL "
                f"GROUP BY `{name}` ORDER BY cnt DESC"
            )).fetchall()
            top_values = [{"value": r[0], "count": r[1]} for r in rows]

        # PK detection: use SQLAlchemy constraint first; fall back to heuristic (issue #70)
        declared_pk = name in pk_cols
        heuristic_pk = (not declared_pk) and _infer_pk(name, distinct_count, total_rows, null_rate)
        is_pk = declared_pk or heuristic_pk

        # Distinct value sample for value-overlap tests (terminology binding) on
        # higher-cardinality columns: top_values is empty above the low-cardinality
        # cutoff, and the first-N sample above may repeat a single value. Skip PK /
        # unique-identifier columns (they never match a controlled vocabulary).
        distinct_sample: list = []
        if not is_pk and 2 <= distinct_count:
            distinct_sample = [
                r[0] for r in conn.execute(text(
                    f"SELECT DISTINCT `{name}` FROM `{table_name}` "
                    f"WHERE `{name}` IS NOT NULL LIMIT {_FK_OVERLAP_SAMPLE}"
                )).fetchall()
            ]

        # Format detection
        formats = self._detect_formats(sample)
        profile = ColumnProfile(
            name=name, inferred_type=str(col_meta['type']), is_nullable=col_meta['nullable'],
            is_primary_key=is_pk, null_rate=null_rate,
            distinct_count=distinct_count, sample_data=sample[:5],
            distinct_sample=distinct_sample,
            top_values=top_values, detected_formats=formats
        )

        if null_rate > self.settings.NULL_RATE_WARNING_THRESHOLD:
            profile.quality_warnings.append(DataQualityWarning(category="NULL_RATE", message="High Nulls", severity="WARNING"))
        if len(formats) > 1:
            profile.quality_warnings.append(DataQualityWarning(category="FORMAT_INCONSISTENCY", message="Mixed Dates", severity="CRITICAL"))

        return profile

    def _detect_formats(self, samples: List[Any]) -> List[str]:
        detected = set()
        for val in samples:
            if not isinstance(val, str): continue
            for pattern, label in self.DATE_PATTERNS.items():
                if re.match(pattern, val): detected.add(label)
        return list(detected)
