# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Run the iterative ontology generator and record a durable metrics report.

This wraps the SAME IterativeOntologyGenerator that ``main.py`` drives, but
captures everything you'd want to keep as provenance for a generated ontology —
so you generate once, archive the report, and never have to repeat the run:

  - backend (endpoint, pod id, gen/judge model, context window, timeout)
  - generator config (retries, top-k, temperatures, judge floor)
  - wall-clock time, overall and per-table
  - token counts (prompt / completion / total) reported by the server
  - GPU-hour cost estimate (the correct cost model for self-hosting — token
    pricing does not apply to a model you rent by the hour)
  - ontology structure (classes, object/datatype properties, named individuals,
    enumeration classes, total triples) parsed from the written OWL

Outputs a JSON + Markdown report and a full DEBUG log under
``output/ontology_runs/<project>_<timestamp>.*``.

Generation needs only ``output/inspection_report.json`` and the LLM endpoint —
no live database connection.

Usage:
    python scripts/generate_ontology_with_metrics.py --project mimic
    python scripts/generate_ontology_with_metrics.py --project mimic --gpu-rate 0.49
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import rdflib
from rdflib import OWL, RDF

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from semantic_mapper.config.logging_config import setup_logging
from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import TableProfile
from semantic_mapper.project.resolver import apply_project_to_env, resolve_project

INSPECTION_REPORT = ROOT / "output" / "inspection_report.json"
RUNS_DIR = ROOT / "output" / "ontology_runs"


class _TableTimer(logging.Handler):
    """Capture the wall-clock instant each table starts, to derive per-table durations.

    Matches the generator's "Table N/M: 'name' → class URI ..." INFO line rather
    than touching the generator — keeps this a pure observer.
    """

    _LINE = re.compile(r"Table (\d+)/(\d+): '([^']+)'")

    def __init__(self) -> None:
        super().__init__()
        self.marks: list[tuple[float, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        m = self._LINE.search(record.getMessage())
        if m:
            self.marks.append((time.perf_counter(), m.group(3)))

    def per_table(self, end: float) -> list[dict]:
        out: list[dict] = []
        for i, (t, name) in enumerate(self.marks):
            nxt = self.marks[i + 1][0] if i + 1 < len(self.marks) else end
            out.append({"table": name, "seconds": round(nxt - t, 1)})
        return out


def _ontology_stats(owl_path: Path) -> dict:
    g = rdflib.Graph()
    g.parse(str(owl_path), format="xml")

    def count(t) -> int:
        return sum(1 for _ in g.subjects(RDF.type, t))

    return {
        "total_triples": len(g),
        "classes": count(OWL.Class),
        "object_properties": count(OWL.ObjectProperty),
        "datatype_properties": count(OWL.DatatypeProperty),
        "named_individuals": count(OWL.NamedIndividual),
        "annotation_properties": count(OWL.AnnotationProperty),
        "enumeration_classes": sum(1 for _ in g.subject_objects(OWL.equivalentClass)),
    }


def _render_md(r: dict) -> str:
    b, c, o, t = r["backend"], r["generator_config"], r["ontology"], r["tokens"]
    lines = [
        f"# Ontology generation run — {r['project']}",
        "",
        f"- **Started (UTC):** {r['started_utc']}",
        f"- **Wall time:** {r['wall_human']}  ({r['wall_seconds']} s)",
        f"- **Output:** `{o['path']}`",
        "",
        "## Backend",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Endpoint | `{b['llm_base_url']}` |",
        f"| RunPod pod id | `{b['runpod_pod_id']}` |",
        f"| Gen model | `{b['gen_model']}` |",
        f"| Judge model | `{b['judge_model']}` |",
        f"| Context window (num_ctx) | {b['num_ctx']} |",
        f"| Request timeout (s) | {b['request_timeout_s']} |",
        "",
        "## Generator config",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Generator | {c['generator']} |",
        f"| Max retries | {c['max_retries']} |",
        f"| Top-k context classes | {c['top_k_classes']} |",
        f"| Gen temperature | {c['gen_temperature']} |",
        f"| Judge temperature | {c['judge_temperature']} |",
        f"| Judge confidence floor | {c['judge_confidence_floor']} |",
        "",
        "## Cost & tokens",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Tables loaded | {r['tables']['loaded']} |",
        f"| Prompt tokens | {t['prompt']:,} |",
        f"| Completion tokens | {t['completion']:,} |",
        f"| Total tokens | {t['total']:,} |",
        f"| GPU rate (USD/hr) | {r['gpu_cost']['rate_usd_per_hour']} |",
        f"| Estimated GPU cost (USD) | {r['gpu_cost']['estimated_usd']} |",
        "",
        "## Ontology structure",
        "",
        "| Entity | Count |",
        "|---|---|",
        f"| Classes | {o['classes']} |",
        f"| Object properties | {o['object_properties']} |",
        f"| Datatype properties | {o['datatype_properties']} |",
        f"| Named individuals | {o['named_individuals']} |",
        f"| Enumeration classes | {o['enumeration_classes']} |",
        f"| Annotation properties | {o['annotation_properties']} |",
        f"| **Total triples** | **{o['total_triples']}** |",
        "",
    ]
    if r.get("per_table"):
        lines += ["## Per-table wall time", "", "| Table | Seconds |", "|---|---|"]
        lines += [f"| {row['table']} | {row['seconds']} |" for row in r["per_table"]]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True, help="Project name (e.g. mimic)")
    ap.add_argument(
        "--gpu-rate", type=float, default=0.49,
        help="USD/hour for the GPU, used for the cost estimate (RunPod A6000 ≈ 0.49). "
             "Pass 0 to skip cost.",
    )
    args = ap.parse_args()

    config = resolve_project(args.project)
    apply_project_to_env(config)
    settings = Settings()
    setup_logging(settings)
    log = logging.getLogger("generate_with_metrics")

    if settings.ONTOLOGY_GENERATOR != "iterative":
        log.warning(
            "ONTOLOGY_GENERATOR=%r (expected 'iterative'); generating with it anyway.",
            settings.ONTOLOGY_GENERATOR,
        )

    if not INSPECTION_REPORT.exists():
        log.error("Inspection report not found at %s — run the inspect phase first.", INSPECTION_REPORT)
        return 1
    with open(INSPECTION_REPORT) as f:
        profiles = [TableProfile.model_validate(t) for t in json.load(f)]
    log.info("Loaded %d table profiles", len(profiles))

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    log_path = RUNS_DIR / f"{args.project}_{stamp}.log"

    # Capture a full DEBUG log of just this run alongside the metrics.
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    timer = _TableTimer()
    root = logging.getLogger()
    root.addHandler(file_handler)
    root.addHandler(timer)

    from semantic_mapper.ontology.iterative import IterativeOntologyGenerator
    generator = IterativeOntologyGenerator(settings)

    t0 = time.perf_counter()
    try:
        out_path = generator.generate(profiles)
    finally:
        end = time.perf_counter()
        root.removeHandler(file_handler)
        root.removeHandler(timer)
        file_handler.close()
    wall_s = end - t0
    finished = datetime.now(timezone.utc)

    stats = _ontology_stats(Path(out_path))
    default_model = settings.LLM_MODEL
    cost = round(wall_s / 3600.0 * args.gpu_rate, 4) if args.gpu_rate else None

    report = {
        "project": args.project,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "wall_seconds": round(wall_s, 1),
        "wall_human": f"{int(wall_s // 60)}m {int(wall_s % 60)}s",
        "backend": {
            "llm_base_url": settings.LLM_BASE_URL,
            "runpod_pod_id": os.environ.get("RUNPOD_POD_ID"),
            "gen_model": settings.ONTOLOGY_GEN_MODEL or default_model,
            "judge_model": settings.ONTOLOGY_JUDGE_MODEL or default_model,
            "num_ctx": settings.LLM_NUM_CTX,
            "request_timeout_s": settings.LLM_REQUEST_TIMEOUT,
        },
        "generator_config": {
            # This wrapper always drives the iterative (RIGOR) generator, so record
            # that — not settings.ONTOLOGY_GENERATOR, which may default to "full".
            "generator": "iterative",
            "configured_generator_setting": settings.ONTOLOGY_GENERATOR,
            "max_retries": settings.ONTOLOGY_GEN_MAX_RETRIES,
            "top_k_classes": settings.ONTOLOGY_GEN_TOP_K_CLASSES,
            "gen_temperature": settings.ONTOLOGY_GEN_TEMPERATURE,
            "judge_temperature": settings.ONTOLOGY_GEN_JUDGE_TEMPERATURE,
            "judge_confidence_floor": settings.ONTOLOGY_JUDGE_CONFIDENCE_FLOOR,
        },
        "tables": {"loaded": len(profiles)},
        "tokens": {
            "prompt": generator.prompt_tokens,
            "completion": generator.completion_tokens,
            "total": generator.prompt_tokens + generator.completion_tokens,
        },
        "gpu_cost": {
            "rate_usd_per_hour": args.gpu_rate or None,
            "estimated_usd": cost,
        },
        "ontology": {"path": str(out_path), **stats},
        "per_table": timer.per_table(end),
    }

    json_path = RUNS_DIR / f"{args.project}_{stamp}.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = _render_md(report)
    md_path = RUNS_DIR / f"{args.project}_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")

    print("\n" + md)
    print(f"Metrics written to:\n  {json_path}\n  {md_path}\n  {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
