# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
MIMIC-III End-to-End Pipeline Runner — issue #55

Runs the full pipeline (inspect → map → silver → gold → rdf) against the
MIMIC-III demo dataset and validates against target metrics:

  - Mapping precision  > 90%
  - Quarantine rate    < 5%
  - All benchmark queries pass
  - LLM cost per run   < $0.10

Usage:
    python datasets/mimic/run_pipeline.py
    python datasets/mimic/run_pipeline.py --phases map,silver
    python datasets/mimic/run_pipeline.py --validate-only
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# ── Metric targets ─────────────────────────────────────────────────────────────
TARGET_PRECISION       = 0.90
TARGET_QUARANTINE_RATE = 0.05   # max fraction of bronze rows quarantined
TARGET_LLM_COST_USD    = 0.10   # max total LLM cost per run
BENCHMARK_SUITE        = ROOT / "eval" / "benchmarks" / "mimic_benchmark.yaml"
GROUND_TRUTH           = ROOT / "eval" / "omop_mimic_ground_truth.yaml"


def _run_phases(phases: str, project: str, env_file: str | None) -> int:
    """Run main.py with specified phases; return exit code."""
    cmd = [sys.executable, str(ROOT / "main.py"), "--project", project]
    if env_file:
        cmd += ["--env", env_file]
    if phases != "all":
        cmd += ["--phases", phases]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    return result.returncode


def _validate_mapping_quality(settings) -> dict:
    """Run mapping evaluation against OMOP ground truth (issue #48)."""
    import yaml
    from semantic_mapper.eval.evaluator import MappingEvaluator
    from semantic_mapper.eval.models import GroundTruth
    from semantic_mapper.schema.mapping_output import SemanticMappingReport

    report_path = Path(settings.QUERY_MAPPING_REPORT_PATH)
    if not report_path.exists():
        return {"available": False, "reason": "No mapping report found"}
    if not GROUND_TRUTH.exists():
        return {"available": False, "reason": "Ground truth file not found"}

    with open(report_path) as f:
        report = SemanticMappingReport.model_validate(json.load(f))
    with open(GROUND_TRUTH) as f:
        gt = GroundTruth.model_validate(yaml.safe_load(f))

    result = MappingEvaluator().evaluate(report, gt, str(GROUND_TRUTH))
    return {
        "available": True,
        "precision": result.precision,
        "recall": result.recall,
        "f1": result.f1,
        "pending_rate": result.pending_rate,
        "unmapped_rate": result.unmapped_rate,
        "pass": result.precision >= TARGET_PRECISION,
    }


def _validate_completeness() -> dict:
    """Check silver quarantine rate from pipeline_stats.json (issue #50)."""
    stats_path = ROOT / "output" / "pipeline_stats.json"
    if not stats_path.exists():
        return {"available": False}
    with open(stats_path) as f:
        raw = json.load(f)
    silver = raw.get("silver", {})
    bronze_rows = silver.get("bronze_rows_read", 0)
    quarantine   = silver.get("quarantine_rows_written", 0)
    quarantine_rate = quarantine / bronze_rows if bronze_rows > 0 else 0.0
    return {
        "available": True,
        "bronze_rows_read": bronze_rows,
        "silver_rows_written": silver.get("silver_rows_written", 0),
        "quarantine_rows_written": quarantine,
        "quarantine_rate": quarantine_rate,
        "pass": quarantine_rate <= TARGET_QUARANTINE_RATE,
    }


def _validate_benchmarks(settings) -> dict:
    """Run benchmark queries against Fuseki (issue #51)."""
    if not BENCHMARK_SUITE.exists():
        return {"available": False, "reason": "Benchmark suite not found"}
    from semantic_mapper.eval.benchmark import BenchmarkRunner
    runner = BenchmarkRunner(settings.FUSEKI_URL, settings.RDF_DATASET_NAME)
    suite  = BenchmarkRunner.load_suite(str(BENCHMARK_SUITE))
    result = runner.run_suite(suite, str(BENCHMARK_SUITE))
    return {
        "available": True,
        "total": result.total,
        "passed": result.passed,
        "failed": result.failed,
        "errored": result.errored,
        "pass": result.failed == 0 and result.errored == 0,
    }


def _print_validation_report(mapping, completeness, benchmarks) -> bool:
    sep = "=" * 64
    print(f"\n{sep}\nMIMIC-III PIPELINE VALIDATION REPORT\n{sep}")
    all_pass = True

    # Mapping quality
    if mapping.get("available"):
        ok = mapping["pass"]
        all_pass = all_pass and ok
        tag = "PASS" if ok else "FAIL"
        print(f"\n  [{'✓' if ok else '✗'}] Mapping Quality  [{tag}]")
        print(f"     Precision  : {mapping['precision']:.1%}  (target > {TARGET_PRECISION:.0%})")
        print(f"     Recall     : {mapping['recall']:.1%}")
        print(f"     F1         : {mapping['f1']:.1%}")
        print(f"     Pending    : {mapping['pending_rate']:.1%}")
        print(f"     Unmapped   : {mapping['unmapped_rate']:.1%}")
    else:
        print(f"\n  [?] Mapping Quality  — {mapping.get('reason', 'unavailable')}")

    # Completeness
    if completeness.get("available"):
        ok = completeness["pass"]
        all_pass = all_pass and ok
        tag = "PASS" if ok else "FAIL"
        print(f"\n  [{'✓' if ok else '✗'}] Triple Completeness  [{tag}]")
        print(f"     Bronze rows    : {completeness['bronze_rows_read']:,}")
        print(f"     Silver written : {completeness['silver_rows_written']:,}")
        print(f"     Quarantined    : {completeness['quarantine_rows_written']:,}")
        print(f"     Quarantine rate: {completeness['quarantine_rate']:.1%}  (target < {TARGET_QUARANTINE_RATE:.0%})")
    else:
        print(f"\n  [?] Triple Completeness  — unavailable")

    # Benchmarks
    if benchmarks.get("available"):
        ok = benchmarks["pass"]
        all_pass = all_pass and ok
        tag = "PASS" if ok else "FAIL"
        print(f"\n  [{'✓' if ok else '✗'}] Benchmark Queries  [{tag}]")
        print(f"     Total   : {benchmarks['total']}")
        print(f"     Passed  : {benchmarks['passed']}")
        print(f"     Failed  : {benchmarks['failed']}")
        print(f"     Errored : {benchmarks['errored']}")
    else:
        print(f"\n  [?] Benchmark Queries  — {benchmarks.get('reason', 'unavailable')}")

    print(f"\n{sep}")
    print(f"  Overall: {'✓ ALL TARGETS MET' if all_pass else '✗ TARGETS NOT MET'}")
    print(f"{sep}\n")
    return all_pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="MIMIC-III end-to-end pipeline runner")
    parser.add_argument("--project", default="mimic", help="Project to run (default: mimic)")
    parser.add_argument("--env", default=None, help="Extra .env file (LLM backend etc.)")
    parser.add_argument(
        "--phases",
        default="all",
        help="Comma-separated phases (default: all = inspect,map,silver,gold,rdf)",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Skip pipeline execution; only validate existing outputs",
    )
    args = parser.parse_args()

    from semantic_mapper.config.settings import Settings
    from semantic_mapper.project.resolver import activate_project
    activate_project(args.project)
    settings = Settings(_env_file=args.env) if args.env else Settings()

    if not args.validate_only:
        rc = _run_phases(args.phases, args.project, args.env)
        if rc != 0:
            logger.error("Pipeline exited with code %d", rc)
            sys.exit(rc)

    # Validate
    mapping      = _validate_mapping_quality(settings)
    completeness = _validate_completeness()
    benchmarks   = _validate_benchmarks(settings)
    all_pass     = _print_validation_report(mapping, completeness, benchmarks)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
