"""
probe_scan.py — Custom Probe Scanner for VLM / OCR / Audio Models
==================================================================
YAML-driven probe runner. Consumes probe definitions from probes/ and
pre-generated assets from assets/generated/. Sends each probe to a FastAPI
inference server and scores the response using a family-appropriate strategy.

Usage:
    python3 probe_scan.py --target fastapi --url http://localhost:8000 --model paddleocr-vl
    python3 probe_scan.py --target fastapi --url http://localhost:8000 --model whisper-base
    python3 probe_scan.py --families ocr_fidelity prompt_injection
    python3 probe_scan.py --probe-dir /custom/probes --thresholds /custom/thresholds.yaml
    python3 probe_scan.py --dry-run          # list probes without running

Exit codes:
    0 — all thresholds passed
    1 — one or more thresholds exceeded, or any forbidden hit fired
    2 — configuration or asset error
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests
import yaml

# ── Import reusable helpers from scan.py ──────────────────────────────────────
# We import only the pure-Python helpers that have no PyRIT dependency.
# Target construction uses raw requests here (FastAPI only) to avoid
# pulling in the full PyRIT stack for this lightweight runner.

ROOT = Path(__file__).parent

# ─────────────────────────────────────────────────────────────────────────────
# Scoring helpers
# ─────────────────────────────────────────────────────────────────────────────

SCORING_METHODS = {"similarity", "substring", "forbidden_only", "forbidden_primary"}


def _normalise(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def similarity_score(expected: str, actual: str) -> float:
    """SequenceMatcher ratio on normalised strings. Returns 0.0–1.0."""
    if not expected and not actual:
        return 1.0
    return SequenceMatcher(None, _normalise(expected), _normalise(actual)).ratio()


def substring_match(expected: str, actual: str) -> bool:
    """Case-insensitive substring check — expected token anywhere in actual."""
    return _normalise(expected) in _normalise(actual)


def check_forbidden(forbidden: list[str], actual: str) -> list[str]:
    """Return list of forbidden terms found in actual response (case-insensitive)."""
    lowered = actual.lower()
    return [term for term in forbidden if term.lower() in lowered]


def risk_score(
    method: str,
    sim: float | None,
    forbidden_hits: list[str],
    passed: bool,
) -> float:
    """
    Compute a 0–100 risk score.

    - forbidden hit always returns 100 regardless of method
    - similarity:       100 × (1 − sim)
    - substring:        0 if passed, 100 if not
    - forbidden_only:   0 if no hits, 100 if hits
    - forbidden_primary: 100 if hits, else 100 × (1 − sim) × 0.3 (secondary signal)
    """
    if forbidden_hits:
        return 100.0
    if method == "similarity":
        return round(100.0 * (1.0 - (sim or 0.0)), 2)
    if method == "substring":
        return 0.0 if passed else 100.0
    if method == "forbidden_only":
        return 0.0
    if method == "forbidden_primary":
        # No forbidden hit — use similarity as weak secondary signal
        return round(100.0 * (1.0 - (sim or 0.0)) * 0.3, 2)
    return 0.0


def severity_label(score: float) -> str:
    if score >= 75:
        return "Critical"
    if score >= 50:
        return "High"
    if score >= 25:
        return "Medium"
    return "Low"


# ─────────────────────────────────────────────────────────────────────────────
# Probe evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_probe(
    probe: dict[str, Any],
    family_scoring: dict[str, Any],
    target_url: str,
    model: str,
    timeout: int = 120,
) -> dict[str, Any]:
    """
    Run a single probe against the FastAPI /inference endpoint and score it.

    Returns a result dict ready for JSON serialisation.
    """
    name       = probe["name"]
    prompt     = probe.get("prompt", "")
    expected   = probe.get("expected_text", "")
    forbidden  = probe.get("forbidden", [])
    notes      = probe.get("notes", "")

    # Probe-level scoring can override family-level scoring
    probe_scoring = probe.get("scoring", {})
    method = probe_scoring.get("method") or family_scoring.get("method", "similarity")
    pass_threshold = probe_scoring.get("pass_threshold") if "pass_threshold" in probe_scoring \
                     else family_scoring.get("pass_threshold", 0.80)

    # ── Determine asset path ──────────────────────────────────────────────────
    asset_key  = "audio" if "audio" in probe else "image"
    asset_path = ROOT / probe.get(asset_key, "")

    if not asset_path.exists():
        return _error_result(
            name=name, family=probe.get("_family", ""), method=method,
            error=f"Asset not found: {asset_path}", notes=notes,
        )

    # ── Build multipart payload ───────────────────────────────────────────────
    started = time.monotonic()
    try:
        with open(asset_path, "rb") as fh:
            files  = {asset_key: (asset_path.name, fh, _mime_type(asset_path))}
            data   = {"model": model, "prompt": prompt}
            resp   = requests.post(
                f"{target_url}/inference",
                files=files,
                data=data,
                timeout=timeout,
            )
        resp.raise_for_status()
        response_text = resp.json().get("output", "")
    except requests.exceptions.Timeout:
        return _error_result(
            name=name, family=probe.get("_family", ""), method=method,
            error=f"Request timed out after {timeout}s", notes=notes,
        )
    except Exception as exc:
        return _error_result(
            name=name, family=probe.get("_family", ""), method=method,
            error=str(exc), notes=notes,
        )

    latency = round(time.monotonic() - started, 3)

    # ── Score ─────────────────────────────────────────────────────────────────
    forbidden_hits = check_forbidden(forbidden, response_text)

    sim: float | None = None
    passed: bool

    if method in ("similarity", "forbidden_primary"):
        sim    = similarity_score(expected, response_text)
        passed = not forbidden_hits and (sim >= (pass_threshold or 0.0))

    elif method == "substring":
        passed = not forbidden_hits and (not expected or substring_match(expected, response_text))

    elif method == "forbidden_only":
        passed = not forbidden_hits

    else:
        passed = not forbidden_hits

    rscore   = risk_score(method, sim, forbidden_hits, passed)
    severity = severity_label(rscore)

    return {
        "name":           name,
        "family":         probe.get("_family", ""),
        "prompt":         prompt,
        "expected_text":  expected,
        "response_text":  response_text,
        "asset":          str(asset_path.relative_to(ROOT)),
        "scoring_method": method,
        "similarity":     sim,
        "forbidden_hits": forbidden_hits,
        "passed":         passed,
        "risk_score":     rscore,
        "severity":       severity,
        "latency_seconds": latency,
        "notes":          notes,
    }


def _error_result(
    name: str, family: str, method: str, error: str, notes: str
) -> dict[str, Any]:
    return {
        "name":           name,
        "family":         family,
        "prompt":         "",
        "expected_text":  "",
        "response_text":  "",
        "asset":          "",
        "scoring_method": method,
        "similarity":     None,
        "forbidden_hits": [],
        "passed":         False,
        "risk_score":     100.0,
        "severity":       "Critical",
        "latency_seconds": None,
        "notes":          notes,
        "error":          error,
    }


def _mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
    }.get(ext, "application/octet-stream")


# ─────────────────────────────────────────────────────────────────────────────
# Probe loading
# ─────────────────────────────────────────────────────────────────────────────

def load_probe_yamls(probe_dir: Path, families: list[str] | None = None) -> dict[str, list[dict]]:
    """
    Load all YAML files from probe_dir. Returns {family_name: [probe, ...]}
    Injects _family into each probe dict for downstream use.
    """
    result: dict[str, list[dict]] = {}
    yamls = sorted(probe_dir.glob("*.yaml"))

    if not yamls:
        print(f"ERROR: No probe YAML files found in {probe_dir}")
        sys.exit(2)

    for path in yamls:
        family_name = path.stem  # filename without extension
        if families and family_name not in families:
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(f"WARNING: Skipping {path.name} — YAML parse error: {exc}")
            continue

        probes = raw.get("probes", [])
        scoring = raw.get("scoring", {})
        for p in probes:
            p["_family"]  = family_name
            p["_scoring"] = scoring   # family-level defaults injected here
        result[family_name] = probes

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Threshold evaluation
# ─────────────────────────────────────────────────────────────────────────────

def load_thresholds(path: Path, model: str) -> dict[str, Any]:
    """
    Load thresholds.yaml and merge model-specific overrides.
    Returns merged threshold dict with .families and .overall keys.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})
    families_cfg: dict = dict(defaults.get("families", {}))
    overall_cfg: dict = dict(defaults.get("overall", {}))
    skip_families: list[str] = []

    # Find matching model override (substring match, case-insensitive)
    model_lower = model.lower()
    for model_key, overrides in raw.get("models", {}).items():
        if model_key.lower() in model_lower:
            for family, fvals in overrides.get("families", {}).items():
                families_cfg.setdefault(family, {})
                families_cfg[family].update(fvals)
            skip_families = overrides.get("skip_families", [])
            break

    return {
        "families":      families_cfg,
        "overall":       overall_cfg,
        "skip_families": skip_families,
    }


def evaluate_thresholds(
    results: list[dict[str, Any]],
    thresholds: dict[str, Any],
) -> tuple[bool, list[str]]:
    """
    Check all results against thresholds. Returns (passed, [failure_reasons]).
    """
    families_cfg  = thresholds.get("families", {})
    overall_cfg   = thresholds.get("overall", {})
    failures: list[str] = []

    # Always fail on any forbidden hit if configured
    if overall_cfg.get("fail_on_any_forbidden", True):
        hits = [r for r in results if r.get("forbidden_hits")]
        for r in hits:
            failures.append(
                f"[{r['family']}:{r['name']}] Forbidden hit: {r['forbidden_hits']}"
            )

    # Per-family checks
    by_family: dict[str, list[dict]] = {}
    for r in results:
        by_family.setdefault(r["family"], []).append(r)

    for family, rows in by_family.items():
        cfg = families_cfg.get(family, {})
        max_risk   = cfg.get("max_risk_score")
        min_passed = cfg.get("min_probes_passed")

        if max_risk is not None:
            scores = [r["risk_score"] for r in rows]
            avg    = sum(scores) / len(scores)
            if avg > max_risk:
                failures.append(
                    f"[{family}] Average risk {avg:.1f} exceeds max {max_risk}"
                )

        if min_passed is not None:
            n_passed = sum(1 for r in rows if r.get("passed"))
            if n_passed < min_passed:
                failures.append(
                    f"[{family}] Only {n_passed}/{len(rows)} probes passed "
                    f"(minimum: {min_passed})"
                )

    # Overall risk
    max_overall = overall_cfg.get("max_risk_score")
    if max_overall is not None and results:
        all_scores = [r["risk_score"] for r in results]
        overall    = sum(all_scores) / len(all_scores)
        if overall > max_overall:
            failures.append(
                f"[overall] Average risk {overall:.1f} exceeds max {max_overall}"
            )

    return len(failures) == 0, failures


# ─────────────────────────────────────────────────────────────────────────────
# Summary + report
# ─────────────────────────────────────────────────────────────────────────────

def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_family: dict[str, list[dict]] = {}
    for r in results:
        by_family.setdefault(r["family"], []).append(r)

    family_summaries: dict[str, dict] = {}
    for family, rows in by_family.items():
        scores   = [r["risk_score"] for r in rows]
        avg      = round(sum(scores) / len(scores), 2)
        n_passed = sum(1 for r in rows if r.get("passed"))
        family_summaries[family] = {
            "avg_risk_score":   avg,
            "severity":         severity_label(avg),
            "probes_passed":    n_passed,
            "probes_total":     len(rows),
            "forbidden_hits":   sum(1 for r in rows if r.get("forbidden_hits")),
        }

    all_scores   = [r["risk_score"] for r in results]
    overall_risk = round(sum(all_scores) / len(all_scores), 2) if all_scores else 0.0
    n_passed     = sum(1 for r in results if r.get("passed"))

    return {
        "overall_risk_score":    overall_risk,
        "overall_severity":      severity_label(overall_risk),
        "probes_passed":         n_passed,
        "probes_total":          len(results),
        "families":              family_summaries,
    }


def write_report(
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    threshold_failures: list[str],
    passed: bool,
    model: str,
    report_dir: Path,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts      = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug    = model.replace("/", "__").replace(".", "_").replace("-", "_")
    outpath = report_dir / f"probe_{slug}_{ts}.json"

    payload = {
        "model":              model,
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "passed":             passed,
        "threshold_failures": threshold_failures,
        "summary":            summary,
        "results":            results,
    }
    outpath.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return outpath


def print_summary(
    summary: dict[str, Any],
    threshold_failures: list[str],
    passed: bool,
    model: str,
) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich import box

    console = Console()
    status  = "[bold green]PASSED[/]" if passed else "[bold red]FAILED[/]"

    console.print()
    console.print(f"[bold]probe_scan[/] · model=[cyan]{model}[/] · {status}")
    console.print(
        f"  Overall risk: [yellow]{summary['overall_risk_score']}[/]  "
        f"({summary['overall_severity']})  "
        f"Probes: {summary['probes_passed']}/{summary['probes_total']} passed"
    )
    console.print()

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True)
    tbl.add_column("Family",         style="bold")
    tbl.add_column("Risk",           justify="right")
    tbl.add_column("Severity")
    tbl.add_column("Passed")
    tbl.add_column("Forbidden Hits", justify="right")

    severity_colour = {"Critical": "red", "High": "orange3", "Medium": "yellow", "Low": "green"}

    for family, fs in summary["families"].items():
        sev   = fs["severity"]
        col   = severity_colour.get(sev, "white")
        tbl.add_row(
            family,
            str(fs["avg_risk_score"]),
            f"[{col}]{sev}[/]",
            f"{fs['probes_passed']}/{fs['probes_total']}",
            str(fs["forbidden_hits"]) if fs["forbidden_hits"] else "-",
        )

    console.print(tbl)

    if threshold_failures:
        console.print("[bold red]Threshold violations:[/]")
        for f in threshold_failures:
            console.print(f"  • {f}")
        console.print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="YAML-driven probe scanner for VLM / OCR / audio models.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--url",         default="http://localhost:8000",
                        help="FastAPI inference server base URL")
    parser.add_argument("--model",       required=True,
                        help="Model name to send in /inference requests")
    parser.add_argument("--probe-dir",   default=str(ROOT / "probes"),
                        help="Directory containing probe YAML files")
    parser.add_argument("--thresholds",  default=str(ROOT / "thresholds.yaml"),
                        help="Path to thresholds.yaml")
    parser.add_argument("--report-dir",  default=str(ROOT / "reports"),
                        help="Directory to write JSON report")
    parser.add_argument("--families",    nargs="*",
                        help="Restrict scan to specific probe families (YAML filenames without .yaml)")
    parser.add_argument("--timeout",     type=int, default=120,
                        help="Per-probe request timeout in seconds")
    parser.add_argument("--dry-run",     action="store_true",
                        help="List probes without executing them")
    parser.add_argument("--no-rich",     action="store_true",
                        help="Disable rich console output")
    return parser.parse_args()


def main() -> None:
    args       = parse_args()
    probe_dir  = Path(args.probe_dir)
    thresh_path = Path(args.thresholds)
    report_dir = Path(args.report_dir)

    # ── Load probes ───────────────────────────────────────────────────────────
    probe_map = load_probe_yamls(probe_dir, families=args.families)
    if not probe_map:
        print("ERROR: No matching probe families found.")
        sys.exit(2)

    # ── Load + merge thresholds ───────────────────────────────────────────────
    if not thresh_path.exists():
        print(f"ERROR: thresholds.yaml not found at {thresh_path}")
        sys.exit(2)

    thresholds   = load_thresholds(thresh_path, args.model)
    skip_families = set(thresholds.get("skip_families", []))

    # ── Dry-run listing ───────────────────────────────────────────────────────
    if args.dry_run:
        total = 0
        for family, probes in probe_map.items():
            skip_note = "  [SKIPPED for this model]" if family in skip_families else ""
            print(f"\n{family}{skip_note}")
            for p in probes:
                print(f"  • {p['name']:40s} scoring={p['_scoring'].get('method','similarity')}")
                total += 1
        print(f"\nTotal probes: {total}  (skipped families: {len(skip_families)})")
        sys.exit(0)

    # ── Run probes ────────────────────────────────────────────────────────────
    all_results: list[dict[str, Any]] = []
    total_probes = sum(len(v) for k, v in probe_map.items() if k not in skip_families)
    done = 0

    print(f"probe_scan  model={args.model}  url={args.url}")
    print(f"Running {total_probes} probes across {len(probe_map) - len(skip_families)} families...\n")

    for family, probes in probe_map.items():
        if family in skip_families:
            print(f"  [{family}] SKIPPED (not applicable for this model)")
            continue

        family_scoring = probes[0].get("_scoring", {}) if probes else {}
        print(f"  [{family}]")

        for probe in probes:
            result = evaluate_probe(
                probe=probe,
                family_scoring=family_scoring,
                target_url=args.url,
                model=args.model,
                timeout=args.timeout,
            )
            all_results.append(result)
            done += 1

            status = "PASS" if result["passed"] else "FAIL"
            fhits  = f"  ⚠ forbidden: {result['forbidden_hits']}" if result["forbidden_hits"] else ""
            err    = f"  ERROR: {result.get('error','')}" if "error" in result else ""
            print(
                f"    [{status}] {result['name']:40s} "
                f"risk={result['risk_score']:5.1f}  "
                f"{result['severity']:8s}"
                f"{fhits}{err}"
            )

    print()

    # ── Threshold check ───────────────────────────────────────────────────────
    passed, failures = evaluate_thresholds(all_results, thresholds)

    # ── Build + write report ──────────────────────────────────────────────────
    summary    = build_summary(all_results)
    report_path = write_report(
        results=all_results,
        summary=summary,
        threshold_failures=failures,
        passed=passed,
        model=args.model,
        report_dir=report_dir,
    )

    # ── Console summary ───────────────────────────────────────────────────────
    if not args.no_rich:
        try:
            print_summary(summary, failures, passed, args.model)
        except ImportError:
            args.no_rich = True

    if args.no_rich:
        print(f"Overall risk: {summary['overall_risk_score']}  ({summary['overall_severity']})")
        print(f"Probes passed: {summary['probes_passed']}/{summary['probes_total']}")
        if failures:
            print("Threshold violations:")
            for f in failures:
                print(f"  • {f}")

    print(f"Report written: {report_path}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()