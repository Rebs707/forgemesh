# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""OfflineCodesetSource — a terminology source backed by an offline code/label set.

The first-cut adapter the plan calls for: no live terminology server, just a
bundled or inline set of valid codes (and optional labels) plus an IRI template.
A later ``ServiceSource`` adapter can talk to a live SNOMED/LOINC endpoint behind
the same :class:`TerminologySource` contract without touching pipeline logic.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Set, Tuple

from .base import TerminologySource

logger = logging.getLogger(__name__)


class OfflineCodesetSource(TerminologySource):
    """A code system defined by an in-memory or file-backed code/label set.

    Parameters
    ----------
    source_id, system_label:
        See :class:`TerminologySource`.
    iri_template:
        Concept-IRI template with a ``{code}`` placeholder, e.g.
        ``"http://snomed.info/id/{code}"``.  When omitted, the source can still
        report membership (``contains``) and a ``system_iri`` for system-level
        alignment, but mints no per-concept IRI (``concept_iri`` → ``None``).
    codes:
        Iterable of valid codes, or a mapping ``{code: label}``.  A mapping also
        enables matching a raw value by its (case-folded) label.
    path:
        Optional file of ``code<TAB>label`` (or bare ``code``) lines; ``#``
        comments and blank lines ignored.  Merged with *codes* if both given.
    system_iri:
        IRI naming the code system itself (for ``rdfs:seeAlso`` / ``closeMatch``).
    """

    def __init__(
        self,
        source_id: str,
        system_label: str,
        *,
        iri_template: Optional[str] = None,
        codes: "Optional[Iterable[str] | Mapping[str, str]]" = None,
        path: "Optional[str | Path]" = None,
        system_iri: Optional[str] = None,
    ) -> None:
        self.source_id = source_id
        self.system_label = system_label
        self._iri_template = iri_template
        self._system_iri = system_iri

        # _code_set: canonical codes (stripped).  _label_to_code: casefolded
        # label → canonical code, for matching a raw value by its label.
        self._code_set: Set[str] = set()
        self._label_to_code: Dict[str, str] = {}

        if codes is not None:
            if isinstance(codes, Mapping):
                for code, label in codes.items():
                    self._add(str(code), str(label) if label is not None else None)
            else:
                for code in codes:
                    self._add(str(code), None)
        if path is not None:
            self._load_file(Path(path))

        if not self._code_set:
            logger.warning(
                "OfflineCodesetSource %r initialised with an empty code set "
                "(codes=%s, path=%s) — it will bind nothing",
                source_id, codes is not None, path,
            )

    # ------------------------------------------------------------------
    def _add(self, code: str, label: Optional[str]) -> None:
        code = code.strip()
        if not code:
            return
        self._code_set.add(code)
        if label:
            self._label_to_code.setdefault(label.strip().casefold(), code)

    def _load_file(self, path: Path) -> None:
        if not path.exists():
            logger.warning(
                "OfflineCodesetSource %r: code-set file not found: %s",
                self.source_id, path,
            )
            return
        n = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t", 1)
            code = parts[0].strip()
            label = parts[1].strip() if len(parts) > 1 else None
            self._add(code, label)
            n += 1
        logger.info(
            "OfflineCodesetSource %r: loaded %d code(s) from %s",
            self.source_id, n, path,
        )

    # ------------------------------------------------------------------
    def _resolve_code(self, value: str) -> Optional[str]:
        v = (value or "").strip()
        if not v:
            return None
        if v in self._code_set:
            return v
        return self._label_to_code.get(v.casefold())

    def contains(self, value: str) -> bool:
        return self._resolve_code(value) is not None

    def concept_iri(self, value: str) -> Optional[str]:
        if not self._iri_template:
            return None
        code = self._resolve_code(value)
        if code is None:
            return None
        return self._iri_template.format(code=code)

    def system_iri(self) -> Optional[str]:
        return self._system_iri
