"""Automated Golden-Dataset Agent Evaluation Harness for JackieAgent CI/CD."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from oura_connect.config import ConfigStore
from oura_connect.orchestrator import MultiAgentCoachOrchestrator
from oura_connect.storage import OuraStorage

DATASET_PATH = Path(__file__).resolve().parent / "golden_dataset.json"


def _seed_baseline_and_fixture(storage: OuraStorage, target_date: str, fixture: dict[str, Any]) -> None:
    target_dt = dt.date.fromisoformat(target_date)
    sleep_rows: list[dict[str, Any]] = []
    for offset in range(16, 1, -1):
        d_iso = (target_dt - dt.timedelta(days=offset)).isoformat()
        sleep_rows.append(
            {
                "id": f"base-{d_iso}",
                "day": d_iso,
                "type": "long_sleep",
                "total_sleep_duration": 28800,
                "average_hrv": 77 + (offset % 4),
                "lowest_heart_rate": 55,
            }
        )

    yday_iso = (target_dt - dt.timedelta(days=1)).isoformat()
    storage.upsert_records(
        "daily_activity",
        [{"id": f"act-{yday_iso}", "day": yday_iso, "score": 90, "steps": int(fixture["yday_steps"])}],
    )
    storage.upsert_records(
        "daily_stress",
        [
            {
                "id": f"str-{yday_iso}",
                "day": yday_iso,
                "stress_high": int(float(fixture["yday_stress_min"]) * 60),
                "recovery_high": 3600,
                "day_summary": "normal",
            }
        ],
    )
    sleep_rows.append(
        {
            "id": f"target-{target_date}",
            "day": target_date,
            "type": "long_sleep",
            "total_sleep_duration": int(float(fixture["sleep_hours"]) * 3600),
            "average_hrv": float(fixture["hrv_ms"]),
            "lowest_heart_rate": float(fixture["rhr_bpm"]),
        }
    )
    storage.upsert_records("sleep", sleep_rows)
    storage.upsert_records(
        "daily_readiness",
        [
            {
                "id": f"rdy-{target_date}",
                "day": target_date,
                "score": 82,
                "temperature_deviation": float(fixture["temp_dev"]),
            }
        ],
    )


def evaluate_all(dataset_path: Path = DATASET_PATH) -> dict[str, Any]:
    cases: list[dict[str, Any]] = json.loads(dataset_path.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []

    for case in cases:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            store = ConfigStore(config_dir=base / "cfg", data_dir=base / "data")
            orchestrator = MultiAgentCoachOrchestrator(store=store)
            try:
                _seed_baseline_and_fixture(orchestrator.storage, case["date"], case["oura_fixture"])
                out = orchestrator.handle_request(
                    user_prompt=case["prompt"],
                    session_id=f"eval-{case['id']}",
                    reference_date=case["date"],
                )
            finally:
                orchestrator.shutdown()

        checks: dict[str, bool] = {}
        checks["intent_match"] = out["intent"] == case["expected_intent"]
        checks["status_match"] = out["status"] == case["expected_status"]

        if case["expected_readiness"] is not None:
            checks["readiness_match"] = out.get("readiness", {}).get("signal") == case["expected_readiness"]
        else:
            checks["readiness_match"] = True

        if case["expected_architect_tier"] is not None:
            actual_tier = str(out.get("routed_models", {}).get("training_architect") or "")
            checks["model_routing_match"] = case["expected_architect_tier"] in actual_tier
        else:
            checks["model_routing_match"] = True

        checks["hitl_gate_match"] = (out.get("pending_approval") is not None) == bool(case["expect_hitl"])

        serialized_out = json.dumps(out)
        forbidden = case.get("forbidden_substrings") or []
        checks["pii_redacted"] = all(sub not in serialized_out for sub in forbidden)

        passed = all(checks.values())
        results.append(
            {
                "id": case["id"],
                "passed": passed,
                "checks": checks,
                "actual_intent": out["intent"],
                "actual_status": out["status"],
            }
        )

    passed_count = sum(1 for r in results if r["passed"])
    pass_rate = passed_count / len(results) if results else 1.0
    return {
        "total_cases": len(results),
        "passed_cases": passed_count,
        "pass_rate": round(pass_rate, 4),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run JackieAgent golden-dataset evaluation harness.")
    parser.add_argument("--fail-under", type=float, default=0.95, help="Minimum pass rate required (0.0..1.0).")
    parser.add_argument("--output-json", default="", help="Optional path to write JSON scorecard.")
    args = parser.parse_args(argv)

    report = evaluate_all()
    if args.output_json:
        Path(args.output_json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    sys.stdout.write(
        f"Golden Evaluation Scorecard: {report['passed_cases']}/{report['total_cases']} "
        f"({report['pass_rate'] * 100:.1f}%)\n"
    )
    for item in report["results"]:
        mark = "PASS" if item["passed"] else "FAIL"
        sys.stdout.write(f"  [{mark}] {item['id']} -> intent={item['actual_intent']}, status={item['actual_status']}\n")
        if not item["passed"]:
            sys.stdout.write(f"         failed_checks={item['checks']}\n")

    return 0 if report["pass_rate"] >= args.fail_under else 1


if __name__ == "__main__":
    raise SystemExit(main())
