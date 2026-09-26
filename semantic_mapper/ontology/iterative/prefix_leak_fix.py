# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Fix for the Ontology: prefix leak in iterative ontology generation.

OBSERVED (run 2, qwen3-32b, MIMIC):
  377 URIs across 95 distinct names carry an invented "Ontology:" prefix baked
  into the local name, e.g.

      http://mesh.local/ontology/mimic_bronze#Ontology:Admission
      http://mesh.local/ontology/mimic_bronze#Ontology:hasIcd9Code
      http://mesh.local/ontology/mimic_bronze#hasOntology:Caregiver   (double-mangled)

ROOT CAUSE:
  qwen3 emits Manchester Syntax that refers to entities by a prefixed name,
  "Ontology:Admission", using "Ontology" as a namespace prefix it invented.
  There is NO such prefix declared in the ontology (only fm, owl, rdf, rdfs,
  skos). The parser has no prefix map entry for "Ontology", so instead of
  resolving or rejecting it, it treats the whole string "Ontology:Admission"
  as a bare local name and concatenates it onto the base namespace.

  This is a harness bug, not a model-quality result. The iterative design
  derives every class URI deterministically from the table name, so every
  entity SHOULD resolve to the base namespace regardless of any prefix the
  model decorates it with. The parser must normalise the name before building
  the URI.

WHY normalise rather than just add the prefix to the prefix map:
  Adding "Ontology" -> base-namespace to the prefix map would fix the simple
  case but not the double-mangled "hasOntology:Caregiver", where the verb
  prefix and the invented prefix have already collided in the local name.
  Normalisation handles both, and is robust to whatever prefix the model
  invents next run (it might be "Ont:" or "mimic:" on a different model).
"""

from __future__ import annotations

import re


# Known-legitimate prefixes that must NOT be stripped. Anything else appearing
# as a prefix on an entity name is an invented prefix and gets removed, because
# in this generator every entity resolves to the single base namespace.
KNOWN_PREFIXES = {"owl", "rdf", "rdfs", "xsd", "skos", "fm"}

# A leading verb prefix the generator itself adds to object/datatype property
# local names. We strip an invented prefix that sits AFTER the verb too, to
# catch the double-mangled "hasOntology:Caregiver" case.
_VERB_PREFIXES = ("has", "is", "of")


def normalise_entity_name(raw: str) -> str:
    """
    Strip any invented namespace prefix from an entity name so it resolves to
    the base namespace.

      "Ontology:Admission"        -> "Admission"
      "Ontology:hasIcd9Code"      -> "hasIcd9Code"
      "hasOntology:Caregiver"     -> "hasCaregiver"   (double-mangled)
      "Admission"                 -> "Admission"      (untouched)
      "skos:altLabel"             -> "skos:altLabel"  (known prefix, untouched)

    Only a single leading colon-delimited segment is treated as a prefix, and
    only if it is not a known-legitimate prefix.
    """
    name = raw.strip()

    if ":" not in name:
        return name

    head, _, tail = name.partition(":")

    # A known-legitimate prefix is left untouched.
    if head in KNOWN_PREFIXES:
        return name

    # Case 1 (checked first): double-mangled verb + invented prefix, e.g.
    # "hasOntology:Caregiver". The head itself starts with a verb, so the colon
    # is buried inside what should be one camelCase property name. Strip the
    # invented middle segment and rejoin verb + final segment.
    for verb in _VERB_PREFIXES:
        if head.startswith(verb) and head != verb:
            buried = head[len(verb):]          # "Ontology"
            if (
                buried
                and buried not in KNOWN_PREFIXES
                and _looks_like_prefix(buried)
                and tail
                and ":" not in tail
            ):
                return verb + tail              # "has" + "Caregiver"

    # Case 2: simple "Prefix:Local" where Prefix is invented.
    if _looks_like_prefix(head) and ":" not in tail:
        return tail

    return name


def _looks_like_prefix(token: str) -> bool:
    # A prefix is a bare identifier: letters/digits/underscore, no spaces, not
    # empty. Guards against treating something with a colon for another reason
    # as a prefix.
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token))


def detect_prefix_leak(local_names: list[str]) -> dict[str, int]:
    """
    Diagnostic: count how many names carry an invented prefix. Useful as a
    post-parse assertion in the e2e gate so the leak can never silently return.
    Returns {invented_prefix: count}. Empty dict means clean.
    """
    leaks: dict[str, int] = {}
    for n in local_names:
        if ":" not in n:
            continue
        head = n.split(":", 1)[0]
        # strip a leading verb to expose a buried prefix
        for verb in _VERB_PREFIXES:
            if head.startswith(verb) and head != verb:
                head = head[len(verb):]
                break
        if head and head not in KNOWN_PREFIXES and _looks_like_prefix(head):
            leaks[head] = leaks.get(head, 0) + 1
    return leaks
