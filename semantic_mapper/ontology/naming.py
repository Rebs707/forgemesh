# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Shared naming for generated ontology output files.

Convention:  ``<project>_<strategy>_<model-short>_<DDMONYY>.owl``

  mimic_rigor_gpt-4o_06JUN26.owl
  mimic_full_qwen2.5-72b_06JUN26.owl

A numeric suffix (``_2``, ``_3``, …) is appended only to avoid clobbering a file
from an earlier same-day run with the same project / strategy / model.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional


def model_short_name(model: str) -> str:
    """Condense a model id into a compact, filename-safe token.

    ``qwen2.5:72b-instruct-q4_K_M`` -> ``qwen2.5-72b``
    ``gpt-4o``                      -> ``gpt-4o``
    """
    s = (model or "model").lower()
    s = re.sub(r"[:/]", "-", s)
    for noise in ("-instruct", "-chat", "-it"):
        s = s.replace(noise, "")
    s = re.sub(r"-q\d+(_[a-z0-9]+)*", "", s)   # quantisation tag: -q4_k_m, -q8_0
    s = re.sub(r"-fp\d+", "", s)               # -fp16
    s = re.sub(r"[^a-z0-9.]+", "-", s).strip("-.")
    return s or "model"


def ontology_filename(
    out_dir: Path,
    project: str,
    strategy: str,
    model: str,
    when: Optional[datetime] = None,
) -> Path:
    """Return a non-clobbering output Path for a generated ontology.

    strategy is the generator marker, e.g. ``rigor`` or ``full``.
    """
    when = when or datetime.now()
    proj = re.sub(r"[^A-Za-z0-9]+", "-", (project or "ontology")).strip("-") or "ontology"
    date = when.strftime("%d%b%y").upper()      # 06JUN26
    stem = f"{proj}_{strategy}_{model_short_name(model)}_{date}"
    path = out_dir / f"{stem}.owl"
    seq = 2
    while path.exists():
        path = out_dir / f"{stem}_{seq}.owl"
        seq += 1
    return path
