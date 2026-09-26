# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Per-table prompt context builder and deterministic class URI derivation.

The deterministic URI is the single most important architectural decision —
it eliminates the entire family of "wrong class name" bugs that plague
the single-pass generator.
"""
import re
from typing import Optional, Set

from semantic_mapper.db.models import TableProfile, ColumnProfile
from semantic_mapper.ontology.iterative.models import ColumnContext, TableContext


def _singularize(word: str) -> str:
    """Return the singular form of *word* using inflect, or a rule-based fallback.

    Words of length ≤ 3 are never singularized — they are likely abbreviations
    (e.g. "mv", "ias") that inflect would misidentify.
    """
    if len(word) <= 3:
        return word
    try:
        import inflect as _inflect
        _engine = _inflect.engine()
        singular = _engine.singular_noun(word)
        return singular if singular else word
    except ImportError:
        pass
    # Rule-based fallback covers the most common English plural forms.
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:            # countries → country
        return word[:-3] + "y"
    if w.endswith("ses") or w.endswith("xes") \
            or w.endswith("ches") or w.endswith("shes"):  # boxes/churches → box/church
        return word[:-2]
    if w.endswith("oes") and not w.endswith("shoes"):     # heroes → hero
        return word[:-2]
    if w.endswith("ves") and len(w) > 4:            # leaves → leaf
        return word[:-3] + "f"
    if w.endswith("s") and not w.endswith("ss"):    # simple plural, not "class"
        return word[:-1]
    return word


# Optional per-run overrides: {table_name: class_local_name}. Populated once at the
# start of generation by compute_class_name_overrides() so an entity class can be
# named after the entity its primary key denotes (e.g. a per-country medal-tally
# table literally named `medals` → class `Country`) rather than the table's surface
# name. Empty by default, so all existing behaviour and tests are unaffected.
_CLASS_NAME_OVERRIDES: dict = {}

# PK stems that identify a surrogate row, not a real entity — never used to rename.
_SURROGATE_ENTITY_STEMS = frozenset({"row", "record", "entry", "line", "seq", "rownum", ""})


def set_class_name_overrides(mapping: dict) -> None:
    """Replace the active class-name override map (call once per generation run)."""
    global _CLASS_NAME_OVERRIDES
    _CLASS_NAME_OVERRIDES = dict(mapping or {})


def _class_local_from_table(table_name: str) -> str:
    """The deterministic table-name → PascalCase class local name (no overrides)."""
    name = re.sub(r"^raw_", "", table_name, flags=re.IGNORECASE)
    # A leading single-letter naming-convention prefix (d_, t_, v_ …) isn't part of the concept.
    name = re.sub(r"^[a-z]_(?=.)", "", name, flags=re.IGNORECASE)
    parts = [p for p in name.split("_") if p]
    return "".join(_singularize(p).capitalize() for p in parts)


def _entity_to_class_name(entity: str) -> str:
    """PascalCase an entity stem (e.g. 'country' → 'Country', 'home_team' → 'HomeTeam')."""
    return "".join(_singularize(p).capitalize() for p in entity.split("_") if p)


def compute_class_name_overrides(profiles) -> dict:
    """Return {table_name: class_local_name} for tables whose entity is better named
    by their primary key than by the table name.

    A dimension table is sometimes named after a measure (a per-country medal-tally
    table named `medals`) while its PRIMARY KEY identifies a different entity
    (`country_code`). When the single PK is `{X}_code`/`{X}_id`, X is a meaningful
    (non-surrogate) noun, AND the table also carries a human-readable sibling column
    named `{X}`, the rows ARE X — so the class is named after X.

    Guarded so it never fires on identity tables: `raw_patients` (PK row_id → 'row',
    surrogate) and `raw_caregivers` (PK cgid → no `{X}_id` split, no 'caregiver'
    sibling) keep their table-derived names. Dataset-agnostic — keyed on structure.
    """
    overrides: dict = {}
    for p in profiles:
        pk_cols = [n for n, c in p.columns.items() if c.is_primary_key]
        if len(pk_cols) != 1:
            continue
        m = re.match(r"^(.+?)_(?:code|id)$", pk_cols[0].lower())
        if not m:
            continue
        entity = m.group(1)
        if entity in _SURROGATE_ENTITY_STEMS:
            continue
        if entity not in {n.lower() for n in p.columns}:  # require the readable sibling
            continue
        entity_class = _entity_to_class_name(entity)
        if entity_class and entity_class.lower() != _class_local_from_table(p.table_name).lower():
            overrides[p.table_name] = entity_class
    return overrides


def derive_class_local_name(table_name: str) -> str:
    """
    Derive a deterministic PascalCase class local name from a table name.

    Honours any override set via set_class_name_overrides() (so a table whose PK
    denotes a different entity can be renamed); otherwise singularizes each
    underscore-separated word part of the table name.

    Examples:
        raw_labevents       → Labevent
        raw_d_icd_diagnoses → IcdDiagnosis
        raw_inputevents_mv  → InputeventMv
        raw_d_labitems      → Labitem
        raw_countries       → Country
        patients            → Patient
    """
    return _CLASS_NAME_OVERRIDES.get(table_name) or _class_local_from_table(table_name)


def derive_class_uri(table_name: str, base_iri: str) -> str:
    """Return the full class URI for a table."""
    return f"{base_iri}{derive_class_local_name(table_name)}"


def _is_fk(
    col_profile: ColumnProfile,
    table_profile: TableProfile,
) -> tuple[bool, Optional[str]]:
    """Return (is_fk, ref_table) for a column.

    Checks the TableProfile.foreign_keys list, which by the time this is called
    already includes FKs inferred by MySQLInspector (Strategies 1–3).  No
    dataset-specific override map is needed or accepted here.
    """
    for fk in table_profile.foreign_keys:
        col_name = fk.get("constrained_column") or fk.get("column")
        ref_table = fk.get("referred_table") or fk.get("ref_table")
        if col_name == col_profile.name and ref_table:
            return True, ref_table
    return False, None


_FLAG_TYPES = frozenset({
    "INTEGER", "INT", "TINYINT", "SMALLINT", "MEDIUMINT", "BIGINT",
    "BOOLEAN", "BOOL", "BIT",
})
_TEXT_TYPES = frozenset({
    "VARCHAR", "CHAR", "TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT", "STRING",
})
_NUMERIC_TYPES = frozenset({
    "INTEGER", "INT", "TINYINT", "SMALLINT", "MEDIUMINT", "BIGINT",
    "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL",
})
_DATE_TYPES = frozenset({"DATETIME", "DATE", "TIMESTAMP", "TIME"})

# Defaults for the proportional class-vs-literal decision (see is_closed_vocabulary).
# null-rate is the primary, safe signal: a mostly-null column carries too little
# signal to anchor a class (e.g. originalsite — distinct=1 but 100% null). The
# min_support (absolute non-null floor) and max_uniqueness (distinct/non_null cap)
# guards are optional backstops, OFF by default, because on real schemas null-rate
# already separates sparse noise from genuine vocabularies, while a support floor or
# uniqueness cap would wrongly demote a small but fully-populated dimension table
# (e.g. a 10-row lookup with 10 distinct categories). Tune via settings if needed.
#
# POLICY (deliberate): a low-null, low-cardinality short-text column is modelled as a
# controlled-vocabulary owl:Class, NOT a plain xsd:string. This diverges on purpose
# from the gpt-4o GOLD MIMIC ontology, which modelled e.g. `category` as xsd:string:
# we prefer the class interpretation for genuine code fields (ethnicity, dbsource,
# param_type, category) because it makes the values first-class, queryable entities.
# The choice is intentional and fully tunable — raise ONTOLOGY_CATEGORICAL_MAX_NULL_RATE
# toward 0, or lower ONTOLOGY_MAX_DISTINCT_FOR_ENUMERATION, to push such columns back to
# literals.
DEFAULT_MAX_NULL_RATE_FOR_CLASS = 0.90    # > this fraction null -> literal
DEFAULT_MIN_SUPPORT_FOR_CLASS = 0         # 0 = no absolute support floor (off)
DEFAULT_MAX_UNIQUENESS_FOR_CLASS = 1.0    # 1.0 = no uniqueness cap (off)


def is_closed_vocabulary(
    distinct_count: int,
    null_rate: float,
    non_null_count: float,
    *,
    max_distinct: int,
    max_null_rate: float = DEFAULT_MAX_NULL_RATE_FOR_CLASS,
    min_support: int = DEFAULT_MIN_SUPPORT_FOR_CLASS,
    max_uniqueness: float = DEFAULT_MAX_UNIQUENESS_FOR_CLASS,
) -> bool:
    """Decide whether a low-cardinality text column is a genuine controlled
    vocabulary (-> owl:Class) rather than a sparse/noise field (-> xsd literal).

    A real vocabulary has few distinct values that RECUR across many non-null
    rows. A column that is mostly null, barely populated, or whose values are
    almost all unique carries too little signal to anchor a class — even when its
    raw distinct_count is small (e.g. originalsite: distinct=1 but 100% null).
    """
    if distinct_count <= 0 or distinct_count > max_distinct:
        return False
    if null_rate is not None and null_rate > max_null_rate:
        return False
    if non_null_count is not None and non_null_count < min_support:
        return False
    if non_null_count and (distinct_count / non_null_count) > max_uniqueness:
        return False
    return True


def _values_look_descriptive(values: list, distinct_count: int, non_null_count: float) -> bool:
    """True when low-distinct text values are descriptive ATTRIBUTES, not a controlled
    vocabulary — so the column should become a DatatypeProperty, not an enumeration class.

    A genuine vocabulary is a small set of short labels that recur across many rows
    (Gold/Silver/Bronze, M/F, FINISHED). The following shapes are NOT vocabularies even
    when distinct_count is small (typically because the table is a small dimension):

      - URL-valued columns (http/https) — descriptive references, e.g. a venue URL.
      - JSON-array / list-shaped cells ('["a","b"]') — a multi-valued column, which
        would otherwise mint nonsensical compound members ("Bobsleigh, Luge, Skeleton").
      - near-unique multi-word free text (≈one value per row, e.g. a venue NAME) — an
        entity's descriptive attribute, not a shared category.

    Kept as vocabularies: short recurring labels, including multi-word ones like
    "Head Coach" (which recur and so are NOT near-unique).
    """
    vals = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not vals:
        return False
    n = len(vals)
    if sum(1 for v in vals if v.lower().startswith(("http://", "https://"))) / n >= 0.5:
        return True
    if any(v.startswith("[") and v.endswith("]") for v in vals):
        return True
    near_unique = bool(non_null_count) and (distinct_count / non_null_count) >= 0.9
    multiword = sum(1 for v in vals if " " in v) / n
    if near_unique and multiword >= 0.5:
        return True
    return False


def _compute_cardinality_hint(
    col_profile: ColumnProfile,
    is_fk: bool,
    top_vals: list,
    max_distinct_for_enumeration: int,
    max_distinct_for_open_class: int,
    row_count: int = 0,
    max_null_rate_for_class: float = DEFAULT_MAX_NULL_RATE_FOR_CLASS,
    min_support_for_class: int = DEFAULT_MIN_SUPPORT_FOR_CLASS,
    max_uniqueness_for_class: float = DEFAULT_MAX_UNIQUENESS_FOR_CLASS,
) -> str:
    """
    Return a cardinality hint string for a column, or "" when no hint applies.

    Hint values: CATEGORICAL_CLOSED, CATEGORICAL_OPEN, FLAG, FREE_TEXT, IDENTIFIER

    A low-distinct text column is only CATEGORICAL_CLOSED when it passes
    is_closed_vocabulary() — i.e. its values recur across enough non-null rows.
    Sparse/mostly-null fields fall through to a plain literal.
    """
    if col_profile.is_primary_key or is_fk:
        return ""

    t = col_profile.inferred_type.upper().split("(")[0].strip()
    d = col_profile.distinct_count

    if t in _DATE_TYPES:
        return ""

    if t in _FLAG_TYPES and d == 2:
        return "FLAG"

    if t in _TEXT_TYPES:
        null_rate = col_profile.null_rate or 0.0
        non_null = row_count * (1.0 - null_rate)
        if (
            d <= max_distinct_for_enumeration
            and top_vals
            and not _values_look_descriptive(top_vals, d, non_null)
            and is_closed_vocabulary(
                d, null_rate, non_null,
                max_distinct=max_distinct_for_enumeration,
                max_null_rate=max_null_rate_for_class,
                min_support=min_support_for_class,
                max_uniqueness=max_uniqueness_for_class,
            )
        ):
            return "CATEGORICAL_CLOSED"
        if d > max_distinct_for_open_class:
            return "FREE_TEXT"
        if d > max_distinct_for_enumeration:
            return "CATEGORICAL_OPEN"
        return ""

    if t in _NUMERIC_TYPES and d > max_distinct_for_open_class:
        return "IDENTIFIER"

    return ""


def _top_values_as_strings(col_profile: ColumnProfile, max_n: int = 10) -> list[str]:
    """Extract top_values strings from ColumnProfile.top_values list-of-dicts."""
    result = []
    for item in col_profile.top_values[:max_n]:
        if isinstance(item, dict):
            result.append(str(item.get("value", "")))
        else:
            result.append(str(item))
    return result


def _shadows_coded_fk(col_name: str, fk_columns: set) -> bool:
    """True when a sibling `{col}_code`/`{col}_id` column is a foreign key — meaning
    this column is the human-readable form of that entity reference (e.g. a `venue`
    name alongside a `venue_code` FK to a Venue table), not its own vocabulary.
    """
    c = col_name.lower()
    return f"{c}_code" in fk_columns or f"{c}_id" in fk_columns


def build_table_context(
    profile: TableProfile,
    processing_index: int,
    total_tables: int,
    max_distinct_for_enumeration: int = 20,
    max_distinct_for_open_class: int = 100,
    skipped_tables: Optional[Set[str]] = None,
    max_null_rate_for_class: float = DEFAULT_MAX_NULL_RATE_FOR_CLASS,
    min_support_for_class: int = DEFAULT_MIN_SUPPORT_FOR_CLASS,
    max_uniqueness_for_class: float = DEFAULT_MAX_UNIQUENESS_FOR_CLASS,
) -> TableContext:
    """
    Convert a TableProfile into a TableContext ready for prompt assembly.

    Detects categorical columns (distinct_count ≤ max_distinct_for_enumeration
    and has top_values).  Sets fk_ref_skipped when the referenced table is in
    skipped_tables.

    FK relationships are read entirely from profile.foreign_keys, which is
    populated by MySQLInspector including all three inference strategies.
    No dataset-specific override map is accepted here.
    """
    _skip: Set[str] = set(skipped_tables) if skipped_tables else set()
    # FK column names in this table — used to suppress a categorical class for a
    # column that is only the descriptive form of a coded FK reference.
    _fk_cols = {
        ((fk.get("constrained_column") or fk.get("column")) or "").lower()
        for fk in profile.foreign_keys
    }
    col_contexts = []
    for col_name, col_profile in profile.columns.items():
        is_fk, fk_ref_table = _is_fk(col_profile, profile)
        top_vals = _top_values_as_strings(col_profile)
        fk_ref_skipped = bool(is_fk and fk_ref_table and fk_ref_table in _skip)
        hint = _compute_cardinality_hint(
            col_profile, is_fk, top_vals,
            max_distinct_for_enumeration, max_distinct_for_open_class,
            row_count=profile.row_count,
            max_null_rate_for_class=max_null_rate_for_class,
            min_support_for_class=min_support_for_class,
            max_uniqueness_for_class=max_uniqueness_for_class,
        )
        is_categorical = hint == "CATEGORICAL_CLOSED"
        # Demote a categorical that merely shadows a coded FK: a `venue` name column
        # alongside a `venue_code` FK to a Venue entity is the descriptive form of
        # that reference (a DatatypeProperty label), not its own vocabulary class.
        if is_categorical and _shadows_coded_fk(col_name, _fk_cols):
            hint = ""
            is_categorical = False
        col_contexts.append(ColumnContext(
            table_name=profile.table_name,
            col_name=col_name,
            inferred_type=col_profile.inferred_type,
            is_pk=col_profile.is_primary_key,
            is_fk=is_fk,
            fk_ref_table=fk_ref_table,
            fk_ref_skipped=fk_ref_skipped,
            is_nullable=col_profile.is_nullable,
            null_rate=col_profile.null_rate,
            distinct_count=col_profile.distinct_count,
            top_values=top_vals,
            detected_formats=col_profile.detected_formats,
            is_categorical=is_categorical,
            categorical_values=top_vals if is_categorical else [],
            cardinality_hint=hint,
        ))

    explicit_fks = []
    for fk in profile.foreign_keys:
        col = fk.get("constrained_column") or fk.get("column")
        ref = fk.get("referred_table") or fk.get("ref_table")
        if col and ref:
            explicit_fks.append({"column": col, "ref_table": ref})

    return TableContext(
        table_name=profile.table_name,
        row_count=profile.row_count,
        columns=col_contexts,
        explicit_fks=explicit_fks,
        processing_index=processing_index,
        total_tables=total_tables,
    )
