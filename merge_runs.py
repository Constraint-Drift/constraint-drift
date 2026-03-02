#!/usr/bin/env python3
"""Merge multiple experiment runs into a single run for unified plotting.

Usage:
    python merge_runs.py runs/run1 runs/run2 --output runs/merged_run
    python merge_runs.py runs/run1 runs/run2 runs/run3 -o runs/merged_all
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Tuple


def get_experiment_name(run_dir: Path) -> str:
    """Get experiment name from a run's multi_run_summary.json or first iteration."""
    # Try multi_run_summary.json first
    summary_path = run_dir / "multi_run_summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            return json.load(f).get("experiment_name", "unknown")

    # Fall back to first iteration's summary
    iterations_dir = run_dir / "iterations"
    if iterations_dir.exists():
        for iter_dir in sorted(iterations_dir.iterdir()):
            iter_summary = iter_dir / "logs" / "summary.json"
            if iter_summary.exists():
                with open(iter_summary) as f:
                    return json.load(f).get("experiment_name", "unknown")

    return "unknown"


def count_iterations(run_dir: Path) -> int:
    """Count the number of iterations in a run."""
    iterations_dir = run_dir / "iterations"
    if not iterations_dir.exists():
        return 0
    return len([d for d in iterations_dir.iterdir() if d.is_dir() and d.name.startswith("iter_")])


def get_iteration_dirs(run_dir: Path) -> List[Path]:
    """Get sorted list of iteration directories."""
    iterations_dir = run_dir / "iterations"
    if not iterations_dir.exists():
        return []
    return sorted([d for d in iterations_dir.iterdir() if d.is_dir() and d.name.startswith("iter_")])


def validate_runs(run_dirs: List[Path]) -> Tuple[str, List[str]]:
    """Validate all runs are compatible for merging.

    Returns:
        Tuple of (experiment_name, list of warnings)
    """
    warnings = []
    experiment_names = set()
    models = set()

    for run_dir in run_dirs:
        if not run_dir.exists():
            raise ValueError(f"Run directory does not exist: {run_dir}")

        iterations_dir = run_dir / "iterations"
        if not iterations_dir.exists():
            raise ValueError(f"Run has no iterations/ directory (not a multi-iteration run): {run_dir}")

        if count_iterations(run_dir) == 0:
            raise ValueError(f"Run has no iterations: {run_dir}")

        exp_name = get_experiment_name(run_dir)
        experiment_names.add(exp_name)

        # Check model consistency
        summary_path = run_dir / "multi_run_summary.json"
        if summary_path.exists():
            with open(summary_path) as f:
                summary = json.load(f)
                if "iteration_summaries" in summary and summary["iteration_summaries"]:
                    model = summary["iteration_summaries"][0].get("model", "unknown")
                    models.add(model)

    if len(experiment_names) > 1:
        raise ValueError(f"Runs have different experiment names: {experiment_names}")

    if len(models) > 1:
        warnings.append(f"Warning: Runs have different models: {models}")

    return experiment_names.pop(), warnings


def copy_and_renumber_iteration(
    src_iter_dir: Path,
    dst_iter_dir: Path,
    new_iter_num: int,
    total_iterations: int
) -> None:
    """Copy an iteration directory and update its metadata."""
    # Copy the entire iteration directory
    shutil.copytree(src_iter_dir, dst_iter_dir)

    # Update summary.json with new iteration numbers
    summary_path = dst_iter_dir / "logs" / "summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)

        summary["iteration"] = new_iter_num
        summary["total_iterations"] = total_iterations

        # Update paths to reflect new location
        summary["log_directory"] = str(dst_iter_dir / "logs")
        summary["plots_directory"] = str(dst_iter_dir / "plots")

        with open(summary_path, 'w') as f:
            json.dump(summary, f, indent=2)


def merge_multi_run_summaries(
    run_dirs: List[Path],
    output_dir: Path,
    experiment_name: str,
    total_iterations: int
) -> None:
    """Create merged multi_run_summary.json."""
    merged_summary = {
        "experiment_name": experiment_name,
        "n_iterations": total_iterations,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_runs": [str(r) for r in run_dirs],
        "iterations_completed": total_iterations,
        "iteration_summaries": []
    }

    # Collect all iteration summaries, renumbering as we go
    iter_num = 1
    for run_dir in run_dirs:
        summary_path = run_dir / "multi_run_summary.json"
        if summary_path.exists():
            with open(summary_path) as f:
                run_summary = json.load(f)

            for iter_summary in run_summary.get("iteration_summaries", []):
                # Update iteration number
                iter_summary["iteration"] = iter_num
                iter_summary["total_iterations"] = total_iterations
                merged_summary["iteration_summaries"].append(iter_summary)
                iter_num += 1
        else:
            # Load from individual iteration summaries
            for iter_dir in get_iteration_dirs(run_dir):
                iter_summary_path = iter_dir / "logs" / "summary.json"
                if iter_summary_path.exists():
                    with open(iter_summary_path) as f:
                        iter_summary = json.load(f)
                    iter_summary["iteration"] = iter_num
                    iter_summary["total_iterations"] = total_iterations
                    merged_summary["iteration_summaries"].append(iter_summary)
                iter_num += 1

    # Write merged summary
    output_path = output_dir / "multi_run_summary.json"
    with open(output_path, 'w') as f:
        json.dump(merged_summary, f, indent=2)


def regenerate_plots(output_dir: Path) -> None:
    """Regenerate aggregated plots for the merged run."""
    # Import here to avoid circular dependencies
    from plot_violations import AggregatedAnalyzer

    plots_dir = output_dir / "aggregated_plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    analyzer = AggregatedAnalyzer(str(output_dir))
    if not analyzer.load_multi_run_data():
        print("Warning: Could not load multi-run data for plotting")
        return

    print(f"Generating plots for {analyzer.n_iterations} iterations...")

    # Generate all standard aggregated visualizations
    analyzer.plot_aggregated_score_trend(
        output_path=str(plots_dir / "aggregated_score_trend.png")
    )

    analyzer.plot_score_distribution(
        output_path=str(plots_dir / "score_distribution.png")
    )

    analyzer.plot_heatmap(
        output_path=str(plots_dir / "iteration_heatmap.png")
    )

    analyzer.plot_scores_by_file(
        output_path=str(plots_dir / "scores_by_file.png")
    )

    analyzer.plot_filtered_score_trend(
        output_path=str(plots_dir / "filtered_score_trend.png")
    )

    analyzer.generate_aggregated_report(
        output_path=str(plots_dir / "aggregated_report.txt")
    )

    print(f"Plots saved to: {plots_dir}")


def merge_runs(run_dirs: List[Path], output_dir: Path, skip_plots: bool = False) -> None:
    """Merge multiple runs into a single output directory."""
    # Validate runs
    print("Validating runs...")
    experiment_name, warnings = validate_runs(run_dirs)
    for warning in warnings:
        print(f"  {warning}")

    print(f"Experiment: {experiment_name}")

    # Count total iterations
    iteration_counts = [(run_dir, count_iterations(run_dir)) for run_dir in run_dirs]
    total_iterations = sum(count for _, count in iteration_counts)

    print(f"\nSource runs:")
    for run_dir, count in iteration_counts:
        print(f"  {run_dir.name}: {count} iterations")
    print(f"Total iterations: {total_iterations}")

    # Create output directory
    if output_dir.exists():
        raise ValueError(f"Output directory already exists: {output_dir}")

    output_dir.mkdir(parents=True)
    iterations_output = output_dir / "iterations"
    iterations_output.mkdir()

    # Copy and renumber iterations
    print(f"\nCopying iterations to {output_dir}...")
    current_iter = 1
    for run_dir in run_dirs:
        for iter_dir in get_iteration_dirs(run_dir):
            new_iter_name = f"iter_{current_iter:03d}"
            dst_iter_dir = iterations_output / new_iter_name

            print(f"  {run_dir.name}/{iter_dir.name} -> {new_iter_name}")
            copy_and_renumber_iteration(
                iter_dir, dst_iter_dir, current_iter, total_iterations
            )
            current_iter += 1

    # Create merged multi_run_summary.json
    print("\nCreating merged summary...")
    merge_multi_run_summaries(run_dirs, output_dir, experiment_name, total_iterations)

    # Regenerate plots
    if not skip_plots:
        print("\nRegenerating plots...")
        regenerate_plots(output_dir)

    print(f"\nMerge complete! Output: {output_dir}")
    print(f"  Total iterations: {total_iterations}")


def main():
    parser = argparse.ArgumentParser(
        description="Merge multiple experiment runs into a single run for unified plotting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Merge two runs
  python merge_runs.py runs/run1 runs/run2 --output runs/merged_run

  # Merge multiple runs
  python merge_runs.py runs/run1 runs/run2 runs/run3 -o runs/merged_all

  # Merge without regenerating plots
  python merge_runs.py runs/run1 runs/run2 -o runs/merged --skip-plots
"""
    )

    parser.add_argument(
        "runs",
        nargs="+",
        type=Path,
        help="Run directories to merge (must be multi-iteration runs)"
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        required=True,
        help="Output directory for merged run"
    )
    parser.add_argument(
        "--skip-plots",
        action="store_true",
        help="Skip plot generation (faster, can run plot_violations.py later)"
    )

    args = parser.parse_args()

    if len(args.runs) < 2:
        print("Error: Need at least 2 runs to merge")
        sys.exit(1)

    try:
        merge_runs(args.runs, args.output, args.skip_plots)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        raise


if __name__ == "__main__":
    main()
