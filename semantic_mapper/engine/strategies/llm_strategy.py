# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
LLMStrategy: Tier 3 semantic mapping using OpenAI (gpt-4o-mini by default).

Batches ALL pending columns for a table into a single API call, providing:
  - Column name, SQL type, null rate, cardinality, detected formats
  - Up to 10 sample values
  - All sibling column names as table-level context
  - The current best Tier 1+2 candidate URI + confidence
  - Full ontology property catalogue (URI, type, label, range, domain)

Returns MappingEvidence items per column keyed by column name.
Rejects any URI not present in the loaded graph (hallucination guard).

Cost tracking: prompt_tokens, completion_tokens, api_calls are public
attributes on the instance — read after evaluate_pending_batch() calls.
"""
import json
import logging
from typing import Dict, List, Optional, Tuple

import rdflib
from rdflib import RDF, RDFS, OWL

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.schema.mapping_output import MappingEvidence, ValueMappingRule

logger = logging.getLogger(__name__)

# ── Issue #45 helpers ─────────────────────────────────────────────────────────

# Values that represent "no data" and should not be sent to the LLM (issue #45)
_NULL_LIKE_VALUES: frozenset = frozenset({
    "", "null", "none", "n/a", "na", "unknown", "undefined", "0",
    "-", "--", "n/a", "not available", "not applicable",
})


def _is_null_like(value: object) -> bool:
    """Return True for values that represent missing/unknown data."""
    return str(value).strip().lower() in _NULL_LIKE_VALUES


def _preprocess_top_values(top_values: list) -> list:
    """Normalise case variants and remove null-like values (issue #45).

    Steps:
    1. Filter out null-like values.
    2. Normalise to title-case and deduplicate, preserving the highest-count
       representative for each normalised form.
    """
    seen_normalised: Dict[str, dict] = {}
    for tv in top_values:
        raw = tv.get("value")
        if raw is None or _is_null_like(raw):
            continue
        normalised = str(raw).strip().title()
        if normalised not in seen_normalised or tv["count"] > seen_normalised[normalised]["count"]:
            # Keep the canonical (highest-count) form for display
            seen_normalised[normalised] = {"value": str(raw).strip(), "count": tv["count"]}
    return list(seen_normalised.values())


def _is_numeric_only_column(top_values: list) -> bool:
    """Return True when ALL (filtered) values are numeric codes (issue #45)."""
    filtered = [tv for tv in top_values if not _is_null_like(tv.get("value", ""))]
    if not filtered:
        return False
    return all(str(tv["value"]).strip().lstrip("-").replace(".", "", 1).isdigit()
               for tv in filtered)


_VALUE_MAPPING_SYSTEM_PROMPT = (
    "You are an expert in semantic ontology mapping.\n"
    "Your task is to map each distinct value in a low-cardinality database column "
    "to an OWL ontology class URI.\n\n"
    "Rules:\n"
    "- Prefer URIs from the ONTOLOGY CLASSES list when an exact or near-exact semantic "
    "match exists for the specific value.\n"
    "- When a column represents a categorical type (e.g. payment method, shipping method, marital "
    "status) and no specific per-value class exists in the ontology, DERIVE a per-value "
    "URI by combining a PascalCase form of the value with the most relevant parent class "
    "name, in the same ontology namespace shown in the ONTOLOGY CLASSES list.\n"
    "  Example: value 'CREDIT CARD' under namespace http://example.org/shop# and parent class "
    "'PaymentMethod' → 'http://example.org/shop#CreditCardPaymentMethod'.\n"
    "  Example: value 'EXPRESS' under parent class 'ShippingMethod' → "
    "'http://example.org/shop#ExpressShippingMethod'.\n"
    "- Every value in a column MUST receive a distinct URI — never map two different "
    "values to the same URI.\n"
    "- If no class or derivation reasonably fits a value, set mapped_uri to null.\n"
    "- Rationale: one sentence explaining the match.\n"
    "- Confidence: 0.95+ if certain, 0.80-0.94 if probable, below 0.60 for weak matches.\n\n"
    'Respond ONLY with valid JSON matching this exact schema:\n'
    '{"columns": {"col_name": [{"source_value": <value>, "mapped_uri": "uri_or_null", '
    '"confidence": 0.00, "rationale": "reason"}]}}'
)

_SYSTEM_PROMPT = (
    "You are an expert in semantic ontology mapping.\n"
    "Your task is to map database columns from a MySQL bronze table to properties "
    "in an OWL ontology.\n\n"
    "Rules:\n"
    "- Only use URI values from the ONTOLOGY PROPERTIES list. Do not invent URIs.\n"
    "- FK columns (names ending in _id, or flagged as FK below) represent foreign key "
    "references to other entities. For these columns, prefer ObjectProperties whose "
    "domain matches the current table's class. Do NOT map them to DatatypeProperties "
    "unless no ObjectProperty exists.\n"
    "- For non-FK scalar columns, prefer DatatypeProperties.\n"
    "- A strong prior candidate is provided for each column — only override it if you "
    "have high confidence (0.85+) in a different property.\n"
    "- If no property reasonably fits a column, set uri to null.\n"
    "- Rationale: one or two sentences explaining the match. Be specific.\n"
    "- Confidence: 0.95+ if certain, 0.80–0.94 if probable, "
    "0.60–0.79 if possible, below 0.60 for weak matches.\n\n"
    'Respond ONLY with valid JSON matching this exact schema:\n'
    '{"mappings": [{"column": "col_name", "uri": "full_uri_or_null", '
    '"confidence": 0.00, "rationale": "reason"}]}'
)


class LLMStrategy:
    """
    Tier 3 mapping strategy powered by an OpenAI chat model.

    Call evaluate_pending_batch() once per table after Tier 1+2 have run.
    Cost and hallucination counters are accumulated across tables on the
    same instance — read api_calls, prompt_tokens, completion_tokens, and
    hallucinated_uris after run() completes.
    """

    def __init__(self, graph: rdflib.Graph, settings: Settings) -> None:
        self.graph = graph
        self.settings = settings
        self._valid_uris: set = self._build_valid_uris()
        self._valid_class_uris: set = self._build_valid_class_uris()
        self._ontology_namespaces: set = self._build_ontology_namespaces()
        self._ontology_block: str = self._build_ontology_block()
        self._classes_block: str = self._build_classes_block()

        # Telemetry (accumulated across all tables)
        self.api_calls: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.hallucinated_uris: List[str] = []

    # ------------------------------------------------------------------
    # Init helpers
    # ------------------------------------------------------------------

    def _build_valid_uris(self) -> set:
        uris: set = set()
        for prop in self.graph.subjects(RDF.type, OWL.DatatypeProperty):
            uris.add(str(prop))
        for prop in self.graph.subjects(RDF.type, OWL.ObjectProperty):
            uris.add(str(prop))
        logger.debug(f"LLMStrategy: {len(uris)} valid ontology URIs loaded")
        return uris

    def _build_valid_class_uris(self) -> set:
        uris: set = set()
        for cls in self.graph.subjects(RDF.type, OWL.Class):
            uri = str(cls)
            if uri.startswith("http"):
                uris.add(uri)
        logger.debug(f"LLMStrategy: {len(uris)} valid ontology class URIs loaded")
        return uris

    def _build_ontology_namespaces(self) -> set:
        """Extract namespace prefixes (up to # or last /) from all ontology class URIs."""
        prefixes: set = set()
        for uri in self._valid_class_uris:
            if "#" in uri:
                prefixes.add(uri.rsplit("#", 1)[0] + "#")
            else:
                parts = uri.rsplit("/", 1)
                if len(parts) == 2:
                    prefixes.add(parts[0] + "/")
        return prefixes

    def _uri_in_ontology_namespace(self, uri: str) -> bool:
        return any(uri.startswith(p) for p in self._ontology_namespaces)

    def _build_classes_block(self) -> str:
        lines = ["ONTOLOGY CLASSES:"]
        for cls in self.graph.subjects(RDF.type, OWL.Class):
            uri = str(cls)
            if not uri.startswith("http"):
                continue
            label = self.graph.value(cls, RDFS.label)
            comment = self.graph.value(cls, RDFS.comment)
            lines.append(
                f"  URI: {uri}\n"
                f"    label: {label or '(none)'} | "
                f"comment: {str(comment)[:100] if comment else '(none)'}"
            )
        return "\n".join(lines)

    def _build_ontology_block(self) -> str:
        lines = ["ONTOLOGY PROPERTIES:"]
        for prop in self.graph.subjects(RDF.type, OWL.DatatypeProperty):
            label = self.graph.value(prop, RDFS.label)
            rng = self.graph.value(prop, RDFS.range)
            dom = self.graph.value(prop, RDFS.domain)
            lines.append(
                f"  [DatatypeProperty] URI: {prop}\n"
                f"    label: {label or '(none)'} | "
                f"range: {str(rng).split('#')[-1].split('/')[-1] if rng else '(none)'} | "
                f"domain: {str(dom).split('#')[-1].split('/')[-1] if dom else '(none)'}"
            )
        for prop in self.graph.subjects(RDF.type, OWL.ObjectProperty):
            label = self.graph.value(prop, RDFS.label)
            rng = self.graph.value(prop, RDFS.range)
            dom = self.graph.value(prop, RDFS.domain)
            lines.append(
                f"  [ObjectProperty]   URI: {prop}\n"
                f"    label: {label or '(none)'} | "
                f"range: {str(rng).split('#')[-1].split('/')[-1] if rng else '(none)'} | "
                f"domain: {str(dom).split('#')[-1].split('/')[-1] if dom else '(none)'}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_table_class(table_name: str) -> str:
        """Best-guess OWL class local name for this table.
        e.g. raw_chartevents → ChartEvent, raw_admissions → Admission
        """
        import re as _re
        stripped = _re.sub(r"^raw_", "", table_name, flags=_re.IGNORECASE)
        parts = stripped.split("_")
        camel = "".join(p.capitalize() for p in parts if p)
        # Strip trailing 's' from plural — Admissions→Admission, Transfers→Transfer
        if camel.endswith("s") and len(camel) > 4:
            camel = camel[:-1]
        return camel

    def _domain_matched_object_props(self, table_class: str) -> List[str]:
        """Return URIs of ObjectProperties whose rdfs:domain local name matches table_class."""
        from rdflib import OWL as _OWL, RDF as _RDF, RDFS as _RDFS
        matches = []
        for prop in self.graph.subjects(_RDF.type, _OWL.ObjectProperty):
            for dom in self.graph.objects(prop, _RDFS.domain):
                dom_local = str(dom).split("#")[-1].split("/")[-1]
                if dom_local.lower() == table_class.lower():
                    matches.append(str(prop))
        return matches

    def _build_prompt(
        self,
        pending_cols: List[Tuple],
        table_profile: TableProfile,
    ) -> str:
        import re as _re
        all_col_names = ", ".join(table_profile.columns.keys())
        fk_col_names = {fk.get("column") for fk in (table_profile.foreign_keys or [])}
        table_class = self._infer_table_class(table_profile.table_name)
        domain_props = self._domain_matched_object_props(table_class)

        col_blocks = []
        for col_name, col_profile, best_uri, best_score, _ in pending_cols:
            samples = [str(v) for v in (col_profile.sample_data or [])[:10]]
            samples_str = ", ".join(samples) if samples else "(no samples)"

            is_fk = col_name in fk_col_names or col_name.lower().endswith("_id")

            if is_fk:
                # Find ObjectProperties whose local name contains the FK base name
                fk_base = _re.sub(r"_id$", "", col_name.lower())
                matching = [
                    p for p in domain_props
                    if fk_base in p.split("#")[-1].split("/")[-1].lower()
                ]
                fk_note = (
                    f"  *** FK column — prefer ObjectProperty with domain={table_class} ***\n"
                    + (
                        f"  Domain-matched candidates: {', '.join(matching)}\n"
                        if matching else
                        f"  (No domain={table_class} ObjectProperties found for base '{fk_base}')\n"
                    )
                )
            else:
                fk_note = ""

            best_str = (
                f"{best_uri} (confidence {best_score:.4f}) — STRONG PRIOR, only override at 0.85+"
                if best_uri
                else "none"
            )
            col_blocks.append(
                f"Column: {col_name}\n"
                f"{fk_note}"
                f"  SQL type: {col_profile.inferred_type}\n"
                f"  Null rate: {col_profile.null_rate:.1%}\n"
                f"  Distinct values: {col_profile.distinct_count}\n"
                f"  Detected formats: {col_profile.detected_formats or '(none)'}\n"
                f"  Sample values: {samples_str}\n"
                f"  Current best candidate: {best_str}"
            )
        columns_block = "\n\n".join(col_blocks)
        return (
            f"TABLE: {table_profile.table_name} (inferred OWL class: {table_class})\n"
            f"All columns in this table: {all_col_names}\n\n"
            f"{self._ontology_block}\n\n"
            f"COLUMNS TO MAP:\n\n{columns_block}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_pending_batch(
        self,
        pending_cols: List[Tuple],
        # Each tuple: (col_name, col_profile, best_uri, best_score, evidence_trail)
        table_profile: TableProfile,
    ) -> Dict[str, List[MappingEvidence]]:
        """
        Batch all PENDING columns for one table into a single API call.

        Returns {col_name: [MappingEvidence]} for columns that received
        a valid LLM mapping.  Columns for which the LLM returns null URI
        or a hallucinated URI are omitted (evidence_trail stays as-is).
        """
        if not pending_cols:
            return {}

        prompt = self._build_prompt(pending_cols, table_profile)
        logger.info(
            f"LLMStrategy: calling {self.settings.OPENAI_MODEL} "
            f"for {len(pending_cols)} PENDING columns in '{table_profile.table_name}'"
        )

        # --- API call ---
        try:
            from semantic_mapper.core.llm import call_llm
            result = call_llm(prompt, self.settings, system_prompt=_SYSTEM_PROMPT, json_mode=True)
            self.api_calls += 1
            self.prompt_tokens += result.prompt_tokens
            self.completion_tokens += result.completion_tokens
        except Exception as exc:
            logger.error(
                f"LLMStrategy: API call failed for table "
                f"'{table_profile.table_name}': {exc}"
            )
            return {}

        # --- Parse response ---
        try:
            raw_content = result.content
            parsed = json.loads(raw_content)
            mappings = parsed.get("mappings", [])
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            logger.warning(
                f"LLMStrategy: failed to parse JSON for "
                f"'{table_profile.table_name}': {exc}"
            )
            return {}

        # --- Validate and emit MappingEvidence ---
        valid_col_names = {col_name for col_name, *_ in pending_cols}
        result: Dict[str, List[MappingEvidence]] = {}

        for m in mappings:
            col_name = m.get("column")
            uri = m.get("uri")
            confidence = m.get("confidence", 0.0)
            rationale = m.get("rationale", "")

            if col_name not in valid_col_names:
                logger.warning(
                    f"LLMStrategy: unknown column '{col_name}' in response — skipping"
                )
                continue

            if not uri:
                logger.debug(
                    f"LLMStrategy: null URI for '{col_name}' in "
                    f"'{table_profile.table_name}' — no LLM match"
                )
                continue

            if uri not in self._valid_uris:
                logger.warning(
                    f"LLMStrategy: hallucinated URI '{uri}' for column "
                    f"'{col_name}' in '{table_profile.table_name}' — rejecting"
                )
                self.hallucinated_uris.append(uri)
                continue

            score = round(max(0.0, min(1.0, float(confidence))), 4)
            result.setdefault(col_name, []).append(
                MappingEvidence(
                    strategy="LLMStrategy",
                    score=score,
                    rationale=f"[LLM: {self.settings.OPENAI_MODEL}] {rationale}",
                    mapped_uri=uri,
                )
            )

        logger.info(
            f"LLMStrategy: '{table_profile.table_name}' → "
            f"{len(result)}/{len(pending_cols)} columns matched"
        )
        return result

    def generate_value_mappings(
        self,
        low_cardinality_cols: List[Tuple],
        # Each tuple: (col_name, col_profile)
        table_profile: TableProfile,
    ) -> Dict[str, List[ValueMappingRule]]:
        """
        For each low-cardinality column, map its distinct values to ontology class URIs.

        Returns {col_name: [ValueMappingRule]} for columns where at least one
        value matched a valid class URI.  Hallucinated or null URIs are dropped.
        """
        if not low_cardinality_cols:
            return {}

        col_blocks = []
        skipped_numeric: list = []
        preprocessed: Dict[str, list] = {}  # col_name → cleaned top_values

        for col_name, col_profile in low_cardinality_cols:
            clean = _preprocess_top_values(col_profile.top_values)
            if not clean:
                logger.debug(
                    "LLMStrategy: '%s' has no values after null-like filtering — skipping",
                    col_name,
                )
                continue
            if _is_numeric_only_column(clean):
                logger.warning(
                    "LLMStrategy: '%s.%s' has only numeric values — skipping "
                    "auto value-mapping; flag for human review",
                    table_profile.table_name, col_name,
                )
                skipped_numeric.append(col_name)
                continue
            preprocessed[col_name] = clean
            values_str = ", ".join(
                f"{tv['value']!r} (n={tv['count']})"
                for tv in clean
            )
            col_blocks.append(
                f"Column: {col_name}\n"
                f"  SQL type: {col_profile.inferred_type}\n"
                f"  Distinct values: {values_str}"
            )

        if not col_blocks:
            logger.info(
                "LLMStrategy: no columns remain for value_mappings in '%s' after pre-processing",
                table_profile.table_name,
            )
            return {}

        prompt = (
            f"TABLE: {table_profile.table_name}\n\n"
            f"{self._classes_block}\n\n"
            f"COLUMNS WITH LOW-CARDINALITY VALUES TO MAP:\n\n"
            + "\n\n".join(col_blocks)
        )

        logger.info(
            "LLMStrategy: generating value_mappings for %d/%d low-cardinality columns "
            "in '%s' (%d numeric-only skipped)",
            len(preprocessed), len(low_cardinality_cols),
            table_profile.table_name, len(skipped_numeric),
        )

        try:
            from semantic_mapper.core.llm import call_llm
            result = call_llm(
                prompt, self.settings,
                system_prompt=_VALUE_MAPPING_SYSTEM_PROMPT,
                json_mode=True,
            )
            self.api_calls += 1
            self.prompt_tokens += result.prompt_tokens
            self.completion_tokens += result.completion_tokens
        except Exception as exc:
            logger.error(
                f"LLMStrategy: value_mappings API call failed for "
                f"'{table_profile.table_name}': {exc}"
            )
            return {}

        try:
            raw_content = result.content
            parsed = json.loads(raw_content)
            columns_data = parsed.get("columns", {})
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            logger.warning(
                f"LLMStrategy: failed to parse value_mappings JSON for "
                f"'{table_profile.table_name}': {exc}"
            )
            return {}

        # Only accept columns that survived pre-processing (preprocessed keys)
        result: Dict[str, List[ValueMappingRule]] = {}

        for col_name, rules in columns_data.items():
            if col_name not in preprocessed:
                logger.warning(
                    "LLMStrategy: unexpected column '%s' in value_mappings response — skipping",
                    col_name,
                )
                continue

            valid_rules = []
            for rule in rules:
                uri = rule.get("mapped_uri")
                if not uri:
                    continue
                if uri not in self._valid_class_uris and not self._uri_in_ontology_namespace(uri):
                    logger.warning(
                        f"LLMStrategy: out-of-namespace URI '{uri}' for "
                        f"'{col_name}' value_mappings — rejecting"
                    )
                    self.hallucinated_uris.append(uri)
                    continue
                valid_rules.append(ValueMappingRule(
                    source_value=rule.get("source_value"),
                    mapped_uri=uri,
                    rationale=rule.get("rationale", ""),
                    confidence_score=round(
                        max(0.0, min(1.0, float(rule.get("confidence", 0.0)))), 4
                    ),
                ))

            if valid_rules:
                distinct_uris = {r.mapped_uri for r in valid_rules}
                if len(distinct_uris) == 1:
                    logger.warning(
                        "LLMStrategy: all %d value_mapping rules for '%s.%s' resolve to "
                        "the same URI '%s' — no discrimination possible, "
                        "dropping rules so values pass through as string literals",
                        len(valid_rules),
                        table_profile.table_name,
                        col_name,
                        next(iter(distinct_uris)),
                    )
                    continue
                result[col_name] = valid_rules

        logger.info(
            f"LLMStrategy: value_mappings generated for "
            f"{len(result)}/{len(low_cardinality_cols)} columns "
            f"in '{table_profile.table_name}'"
        )
        return result
