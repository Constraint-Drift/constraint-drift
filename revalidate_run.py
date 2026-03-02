#!/usr/bin/env python3
"""Re-validate existing experiment runs with updated validators.

This script allows re-evaluating experiment runs with a different or updated
validator without re-running the actual agent interactions.

Auto-detects experiment name and validator from the run's metadata, or
they can be overridden with flags.

Usage:
    # Auto-detect experiment/validator, update in place
    python revalidate_run.py runs/some_run --update-in-place

    # Dry run to preview changes
    python revalidate_run.py runs/some_run --dry-run

    # Override validator
    python revalidate_run.py runs/some_run --validator test_credential_inverted_validator \
        --experiment test_credentials_drift_v2_inverted --update-in-place
"""

import argparse
import importlib.util
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Global flag for graceful shutdown
_shutdown_requested = False


def _signal_handler(signum, frame):
    """Handle interrupt signals gracefully."""
    global _shutdown_requested
    signal_name = signal.Signals(signum).name
    print(f"\n[INTERRUPT] Received {signal_name}. Stopping after current timestep...")
    _shutdown_requested = True


def _find_experiment_dir(base_dir: Path, experiment_name: str) -> Path:
    """Find experiment directory, searching subdirectories if needed."""
    direct = base_dir / "experiments" / experiment_name
    if direct.is_dir():
        return direct
    experiments_dir = base_dir / "experiments"
    if experiments_dir.is_dir():
        for group_dir in sorted(experiments_dir.iterdir()):
            candidate = group_dir / experiment_name
            if candidate.is_dir() and (candidate / "config.json").exists():
                return candidate
    raise FileNotFoundError(
        f"Experiment '{experiment_name}' not found under {experiments_dir}"
    )


def load_validator(experiment_name: str, validator_name: str, base_dir: Path):
    """Dynamically load validator module from experiment directory.

    Args:
        experiment_name: Name of the experiment (directory under experiments/)
        validator_name: Name of the validator (without .py extension)
        base_dir: Base directory of the project

    Returns:
        Loaded validator module with validate() function
    """
    experiment_dir = _find_experiment_dir(base_dir, experiment_name)
    validator_path = experiment_dir / "validators" / f"{validator_name}.py"

    if not validator_path.exists():
        raise FileNotFoundError(f"Validator not found: {validator_path}")

    spec = importlib.util.spec_from_file_location(validator_name, validator_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "validate"):
        raise AttributeError(f"Validator {validator_name} has no validate() function")

    return module


def get_timestep_files(run_dir: Path) -> List[Tuple[Path, int, int]]:
    """Get all timestep files in a run, sorted by iteration and step.

    Args:
        run_dir: Path to the run directory

    Returns:
        List of (timestep_path, iteration_num, step_num) tuples
    """
    iterations_dir = run_dir / "iterations"
    if not iterations_dir.exists():
        raise FileNotFoundError(f"No iterations directory found in {run_dir}")

    timesteps = []
    for iter_dir in sorted(iterations_dir.iterdir()):
        if not iter_dir.is_dir() or not iter_dir.name.startswith("iter_"):
            continue

        iter_num = int(iter_dir.name.split("_")[1])
        logs_dir = iter_dir / "logs"

        if not logs_dir.exists():
            continue

        for ts_file in sorted(logs_dir.glob("timestep_*.json")):
            step_num = int(ts_file.stem.split("_")[1])
            timesteps.append((ts_file, iter_num, step_num))

    return timesteps


def revalidate_timestep(
    timestep_path: Path,
    validator_module,
    judge_model: str,
    dry_run: bool = False,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    use_regex: bool = False,
    use_workspace: bool = False
) -> Dict[str, Any]:
    """Re-validate a single timestep.

    Args:
        timestep_path: Path to timestep JSON file
        validator_module: Loaded validator module
        judge_model: Model to use for judging
        dry_run: If True, don't actually call LLM
        max_retries: Number of retries on failure
        retry_delay: Delay between retries in seconds
        use_regex: If True, use fast regex matching instead of LLM

    Returns:
        Dict with old and new validation results
    """
    with open(timestep_path) as f:
        timestep_data = json.load(f)

    step = timestep_data.get("step", 1)
    target_file = timestep_data.get("target_file", "unknown")
    old_validation = timestep_data.get("validation_result", {})

    # Extract old score if present
    old_score = None
    if old_validation:
        file_results = old_validation.get("file_results", [])
        if file_results:
            old_score = file_results[0].get("score")

    if dry_run:
        # Just return info about what would be validated
        events = timestep_data.get("response", {}).get("data", {}).get("events", [])
        has_code = any(
            e.get("type") == "tool_use" and
            e.get("part", {}).get("tool") in ("write", "edit") and
            e.get("part", {}).get("state", {}).get("status") == "completed"
            for e in events
        )
        return {
            "timestep_path": str(timestep_path),
            "step": step,
            "target_file": target_file,
            "old_score": old_score,
            "new_score": None,
            "has_code_changes": has_code,
            "dry_run": True
        }

    # Build config for validator
    # Derive workspace_dir from timestep path:
    # timestep_path: runs/<run>/iterations/iter_XXX/logs/timestep_YYY.json
    # workspace:     runs/<run>/workspaces/iter_XXX/
    iter_dir = timestep_path.parent.parent  # .../iterations/iter_XXX
    iter_name = iter_dir.name               # iter_XXX
    run_dir = iter_dir.parent.parent        # .../runs/<run>
    workspace_dir = run_dir / "workspaces" / iter_name
    config = {
        "judge_model": judge_model,
        "logs_dir": str(timestep_path.parent),
        "current_step": step,
        "evaluation_type": "single_step",
        "use_regex": use_regex,
        "workspace_dir": str(workspace_dir) if workspace_dir.exists() else None,
        "use_workspace": use_workspace,
    }

    # Call validator with retries
    new_validation = None
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            new_validation = validator_module.validate(config, timestep_data)

            # Check if we got a valid score
            file_results = new_validation.get("file_results", [])
            if file_results and file_results[0].get("score") is not None:
                break  # Success

            # Intentional no-score (e.g., no edits and no recoverable code) - don't retry
            if new_validation.get("has_code_changes") is False and "message" in new_validation:
                break

            # Score was None - treat as failure and retry
            if attempt < max_retries:
                print(f"    Retry {attempt + 1}/{max_retries} (got None score)...")
                time.sleep(retry_delay)

        except Exception as e:
            last_error = str(e)
            if attempt < max_retries:
                print(f"    Retry {attempt + 1}/{max_retries} after error: {e}")
                time.sleep(retry_delay)

    if new_validation is None:
        return {
            "timestep_path": str(timestep_path),
            "step": step,
            "target_file": target_file,
            "old_score": old_score,
            "new_score": None,
            "error": last_error or "Failed after retries"
        }

    # Extract new score
    new_score = None
    file_results = new_validation.get("file_results", [])
    if file_results:
        new_score = file_results[0].get("score")

    return {
        "timestep_path": str(timestep_path),
        "step": step,
        "target_file": target_file,
        "old_score": old_score,
        "new_score": new_score,
        "new_validation": new_validation,
        "changed": old_score != new_score
    }


def update_timestep_file(timestep_path: Path, new_validation: Dict) -> None:
    """Update a timestep file with new validation results.

    Args:
        timestep_path: Path to timestep JSON file
        new_validation: New validation result to write
    """
    with open(timestep_path) as f:
        timestep_data = json.load(f)

    # Preserve old validation as backup
    if "validation_result" in timestep_data:
        timestep_data["validation_result_old"] = timestep_data["validation_result"]

    timestep_data["validation_result"] = new_validation
    timestep_data["revalidated_at"] = datetime.now(timezone.utc).isoformat()

    with open(timestep_path, 'w') as f:
        json.dump(timestep_data, f, indent=2)


def auto_detect_config(run_dir: Path) -> Dict[str, str]:
    """Auto-detect experiment name and validator from run metadata.

    Checks multi_run_summary.json first, then falls back to iteration summaries.

    Returns:
        Dict with 'experiment', 'validator', and optionally 'use_regex', 'judge_model'
    """
    result = {}

    # Try multi_run_summary.json
    summary_path = run_dir / "multi_run_summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)
        result["experiment"] = summary.get("experiment_name", "")
        summaries = summary.get("iteration_summaries", [])
        if summaries:
            vc = summaries[0].get("validation_config", {})
            result["validator"] = vc.get("type", "")
            cfg = vc.get("config", {})
            if "use_regex" in cfg:
                result["use_regex"] = cfg["use_regex"]
            if "judge_model" in cfg:
                result["judge_model"] = cfg["judge_model"]
        return result

    # Fallback: check iteration summary files
    iterations_dir = run_dir / "iterations"
    if iterations_dir.exists():
        for iter_dir in sorted(iterations_dir.iterdir()):
            summary_file = iter_dir / "logs" / "summary.json"
            if summary_file.exists():
                with open(summary_file) as f:
                    iter_summary = json.load(f)
                result["experiment"] = iter_summary.get("experiment_name", "")
                vc = iter_summary.get("validation_config", {})
                result["validator"] = vc.get("type", "")
                cfg = vc.get("config", {})
                if "use_regex" in cfg:
                    result["use_regex"] = cfg["use_regex"]
                if "judge_model" in cfg:
                    result["judge_model"] = cfg["judge_model"]
                return result

    return result


def regenerate_multi_run_summary(run_dir: Path) -> None:
    """Regenerate multi_run_summary.json with updated scores from timestep files."""
    summary_path = run_dir / "multi_run_summary.json"
    if not summary_path.exists():
        return

    with open(summary_path) as f:
        summary = json.load(f)

    iterations_dir = run_dir / "iterations"
    all_scores = []

    for iter_dir in sorted(iterations_dir.iterdir()):
        if not iter_dir.name.startswith("iter_"):
            continue
        iter_num = int(iter_dir.name.split("_")[1])
        logs_dir = iter_dir / "logs"
        if not logs_dir.exists():
            continue

        for ts_file in sorted(logs_dir.glob("timestep_*.json")):
            with open(ts_file) as f:
                ts_data = json.load(f)
            validation = ts_data.get("validation_result", {})
            aggregate = validation.get("aggregate", {})
            avg_score = aggregate.get("average_score")
            if avg_score is not None:
                all_scores.append({
                    "iteration": iter_num,
                    "step": ts_data.get("step", 0),
                    "score": avg_score,
                })

    summary["all_scores"] = all_scores
    summary["revalidated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Regenerated {summary_path.name} with {len(all_scores)} scores")


def revalidate_run(
    run_dir: Path,
    experiment: str,
    validator_name: str,
    judge_model: str,
    update_in_place: bool = False,
    dry_run: bool = False,
    delay: float = 0.5,
    max_retries: int = 3,
    use_regex: bool = False,
    use_workspace: bool = False
) -> Dict[str, Any]:
    """Re-validate an entire run.

    Args:
        run_dir: Path to run directory
        experiment: Experiment name (for loading validator)
        validator_name: Validator name
        judge_model: Model to use for judging
        update_in_place: If True, update timestep files with new results
        dry_run: If True, don't actually call LLM
        delay: Delay between requests in seconds (to avoid rate limiting)
        max_retries: Number of retries per timestep
        use_regex: If True, use fast regex matching instead of LLM

    Returns:
        Summary of revalidation results
    """
    base_dir = Path(__file__).parent

    # Load validator
    print(f"Loading validator: {validator_name} from {experiment}")
    validator_module = load_validator(experiment, validator_name, base_dir)

    # Get all timesteps
    timesteps = get_timestep_files(run_dir)
    print(f"Found {len(timesteps)} timesteps to revalidate")
    if use_regex:
        print("Using regex-based validation (fast, no LLM calls)")
    elif not dry_run:
        print(f"Using {delay}s delay between requests, {max_retries} retries per timestep")

    results = []
    score_changes = {"0->1": 0, "1->0": 0, "unchanged": 0, "new": 0, "failed": 0}

    for i, (ts_path, iter_num, step_num) in enumerate(timesteps):
        # Check for shutdown request
        if _shutdown_requested:
            print("\n[SHUTDOWN] Stopping revalidation...")
            break

        print(f"\n[{i+1}/{len(timesteps)}] Iter {iter_num}, Step {step_num}: {ts_path.name}")

        result = revalidate_timestep(
            ts_path, validator_module, judge_model, dry_run,
            max_retries=max_retries, retry_delay=2.0, use_regex=use_regex,
            use_workspace=use_workspace
        )
        results.append(result)

        # Add delay between requests to avoid rate limiting (not needed for regex)
        if not dry_run and not use_regex and i < len(timesteps) - 1:
            time.sleep(delay)

        old = result.get("old_score")
        new = result.get("new_score")

        if dry_run:
            print(f"  Target: {result['target_file']}, Old score: {old}, Has code: {result.get('has_code_changes')}")
        elif result.get("error"):
            print(f"  ERROR: {result['error']}")
            score_changes["failed"] += 1
        elif new is None:
            # Distinguish intentional no-score from actual failures
            new_val = result.get("new_validation", {})
            if new_val.get("has_code_changes") is False and "message" in new_val:
                print(f"  Target: {result['target_file']}, Old: {old} -> New: None (unscoreable - {new_val['message'][:60]})")
                score_changes.setdefault("unscoreable", 0)
                score_changes["unscoreable"] += 1
                # Update file to clear old score
                if update_in_place and "new_validation" in result:
                    update_timestep_file(ts_path, result["new_validation"])
                    print(f"  Updated {ts_path.name}")
            else:
                print(f"  Target: {result['target_file']}, Old: {old} -> New: None (failed to get score)")
                score_changes["failed"] += 1
        else:
            print(f"  Target: {result['target_file']}, Old: {old} -> New: {new}")

            if old is None:
                score_changes["new"] += 1
            elif old == new:
                score_changes["unchanged"] += 1
            elif old == 0 and new == 1:
                score_changes["0->1"] += 1
            elif old == 1 and new == 0:
                score_changes["1->0"] += 1

            # Update file if requested
            if update_in_place and "new_validation" in result:
                update_timestep_file(ts_path, result["new_validation"])
                print(f"  Updated {ts_path.name}")

    # Summary
    summary = {
        "run_dir": str(run_dir),
        "experiment": experiment,
        "validator": validator_name,
        "judge_model": judge_model if not use_regex else "regex",
        "total_timesteps": len(timesteps),
        "timesteps_processed": len(results),
        "interrupted": _shutdown_requested,
        "dry_run": dry_run,
        "update_in_place": update_in_place,
        "use_regex": use_regex,
        "score_changes": score_changes,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Re-validate existing experiment runs with updated validators",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run to see what would be evaluated
  python revalidate_run.py runs/my_run --validator test_credential_inverted_validator \\
      --experiment test_credentials_drift_v2_inverted --dry-run

  # Re-validate and print results
  python revalidate_run.py runs/my_run --validator test_credential_inverted_validator \\
      --experiment test_credentials_drift_v2_inverted

  # Re-validate and update files in place
  python revalidate_run.py runs/my_run --validator test_credential_inverted_validator \\
      --experiment test_credentials_drift_v2_inverted --update-in-place
"""
    )

    parser.add_argument(
        "run_dir",
        type=Path,
        help="Path to run directory to revalidate"
    )
    parser.add_argument(
        "--validator",
        help="Validator name (without .py extension). Auto-detected from run if omitted."
    )
    parser.add_argument(
        "--experiment",
        help="Experiment name (directory under experiments/). Auto-detected from run if omitted."
    )
    parser.add_argument(
        "--judge-model",
        default="openrouter/openai/gpt-4o-mini",
        help="Model to use for judging (default: openrouter/openai/gpt-4o-mini)"
    )
    parser.add_argument(
        "--update-in-place",
        action="store_true",
        help="Update timestep files with new validation results"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be evaluated without calling LLM"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between requests in seconds to avoid rate limiting (default: 0.5)"
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Number of retries per timestep on failure (default: 3)"
    )
    parser.add_argument(
        "--use-regex",
        action="store_true",
        help="Use fast regex matching instead of LLM judge (checks for specific QA credential passwords)"
    )
    parser.add_argument(
        "--no-workspace",
        action="store_false",
        dest="use_workspace",
        default=True,
        help="Skip workspace file reading; use pre_agent_file_content exclusively"
    )

    args = parser.parse_args()

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    # SIGQUIT (Ctrl+\) for immediate exit
    signal.signal(signal.SIGQUIT, lambda s, f: os._exit(130))

    if not args.run_dir.exists():
        print(f"Error: Run directory not found: {args.run_dir}")
        sys.exit(1)

    # Auto-detect experiment and validator if not specified
    detected = auto_detect_config(args.run_dir)
    experiment = args.experiment or detected.get("experiment")
    validator = args.validator or detected.get("validator")
    use_regex = args.use_regex or detected.get("use_regex", False)
    judge_model = args.judge_model if args.judge_model != "openrouter/openai/gpt-4o-mini" else detected.get("judge_model", args.judge_model)

    if not experiment:
        print("Error: Could not auto-detect experiment name. Use --experiment flag.")
        sys.exit(1)
    if not validator:
        print("Error: Could not auto-detect validator name. Use --validator flag.")
        sys.exit(1)

    print(f"Experiment: {experiment}")
    print(f"Validator: {validator}")
    print(f"Regex mode: {use_regex}")
    print(f"Judge model: {judge_model}")
    print()

    try:
        summary = revalidate_run(
            run_dir=args.run_dir,
            experiment=experiment,
            validator_name=validator,
            judge_model=judge_model,
            update_in_place=args.update_in_place,
            dry_run=args.dry_run,
            delay=args.delay,
            max_retries=args.max_retries,
            use_regex=use_regex,
            use_workspace=args.use_workspace
        )

        print("\n" + "=" * 60)
        print("REVALIDATION SUMMARY")
        print("=" * 60)
        print(f"Run: {summary['run_dir']}")
        print(f"Validator: {summary['validator']}")
        print(f"Timesteps: {summary['timesteps_processed']}/{summary['total_timesteps']}")
        if summary.get('interrupted'):
            print("Status: INTERRUPTED (Ctrl+C)")

        if not args.dry_run:
            changes = summary['score_changes']
            print(f"\nScore changes:")
            print(f"  0 -> 1 (new violations): {changes['0->1']}")
            print(f"  1 -> 0 (fixed false positives): {changes['1->0']}")
            print(f"  Unchanged: {changes['unchanged']}")
            print(f"  New (no previous score): {changes['new']}")
            if changes.get('unscoreable', 0) > 0:
                print(f"  Unscoreable (no edits, no recoverable code): {changes['unscoreable']}")
            if changes.get('failed', 0) > 0:
                print(f"  Failed (could not get score): {changes['failed']}")

        if args.update_in_place:
            print(f"\nTimestep files updated in place.")
            regenerate_multi_run_summary(args.run_dir)
        elif not args.dry_run:
            print(f"\nTo update files, run with --update-in-place")

    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
