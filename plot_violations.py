#!/usr/bin/env python3
"""
Violation Tracking and Plotting Tool

Analyzes experiment logs to track how agent (A) violates constraints (C)
over time. Supports both:
- Violation-counting validators (linting, preservation)
- Score-based validators (LLM judge with 1-5 violation scores)

Also supports aggregated analysis across multiple experiment iterations.
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from collections import defaultdict


@dataclass
class ViolationTimestep:
    """Represents violations at a single timestep (violation-counting validators)."""
    step: int
    total_lines: int
    agent_written_lines: int
    patch_injected_lines: int
    agent_violations: int
    patch_violations: int
    agent_violation_rate: float
    patch_violation_rate: float
    patch_applied: bool
    is_review_step: bool


@dataclass
class ScoreTimestep:
    """Represents LLM judge scores at a single timestep (score-based validators).

    Score scale (0-1):
    0 = No violation (compliant)
    1 = Violation (non-compliant)
    """
    step: int
    average_score: float  # 0-1 scale (0=compliant, 1=violation)
    total_score: int
    files_scored: int
    violation_level: str
    patch_applied: bool
    file_results: List[Dict]


class ViolationAnalyzer:
    """Analyzes constraint violations across experiment timesteps."""

    def __init__(self, log_dir: str):
        self.log_dir = Path(log_dir)
        self.timesteps: List[ViolationTimestep] = []
        self.score_timesteps: List[ScoreTimestep] = []
        self.experiment_name = "unknown"
        self.constraint_description = ""
        self.validator_type = "unknown"  # "violation" or "score"

    def load_experiment_data(self) -> None:
        """Load all timestep data from logs."""
        # Load summary for metadata
        summary_path = self.log_dir / "summary.json"
        if summary_path.exists():
            with open(summary_path) as f:
                summary = json.load(f)
                self.experiment_name = summary.get("experiment_id", "unknown")
                constraint_file = summary.get("constraint_file", "")
                self.constraint_description = f"Constraints from {constraint_file}"

        # Load each timestep and detect validator type
        timestep_files = sorted(self.log_dir.glob("timestep_*.json"))

        for ts_file in timestep_files:
            with open(ts_file) as f:
                data = json.load(f)
                validation = data.get("validation_result", {})

                # Detect validator type from first timestep with validation
                if validation and self.validator_type == "unknown":
                    validator_name = validation.get("validator", "")
                    if "test_credential" in validator_name or "average_score" in validation.get("aggregate", {}):
                        self.validator_type = "score"
                    else:
                        self.validator_type = "violation"

                # Parse based on validator type
                if self.validator_type == "score":
                    score_ts = self._parse_score_timestep(data)
                    if score_ts:
                        self.score_timesteps.append(score_ts)
                else:
                    timestep = self._parse_timestep(data)
                    if timestep:
                        self.timesteps.append(timestep)

    def _parse_score_timestep(self, data: Dict) -> Optional[ScoreTimestep]:
        """Parse a timestep with score-based validation."""
        step = data.get("step", 0)

        patch_applied_data = data.get("patch_applied")
        patch_applied = (
            patch_applied_data is not None and
            patch_applied_data.get("success", False)
        )

        validation = data.get("validation_result", {})

        if not validation or not validation.get("has_code_changes"):
            return None

        aggregate = validation.get("aggregate", {})
        average_score = aggregate.get("average_score")

        if average_score is None:
            return None

        # Support both old "compliance_level" and new "violation_level" keys
        violation_level = aggregate.get("violation_level") or aggregate.get("compliance_level", "unknown")

        return ScoreTimestep(
            step=step,
            average_score=average_score,
            total_score=aggregate.get("total_score", 0),
            files_scored=aggregate.get("files_scored", 0),
            violation_level=violation_level,
            patch_applied=patch_applied,
            file_results=validation.get("file_results", [])
        )

    def _parse_timestep(self, data: Dict) -> ViolationTimestep:
        """Parse a single timestep's validation data (violation-counting)."""
        step = data.get("step", 0)

        # No review steps in this experiment design
        is_review_step = False

        # Check if patch was applied
        patch_applied_data = data.get("patch_applied")
        patch_applied = (
            patch_applied_data is not None and
            patch_applied_data.get("success", False)
        )

        # Get validation results
        validation = data.get("validation_result", {})

        if not validation or not validation.get("has_code_changes"):
            # No code changes, skip or record as zero violations
            return ViolationTimestep(
                step=step,
                total_lines=0,
                agent_written_lines=0,
                patch_injected_lines=0,
                agent_violations=0,
                patch_violations=0,
                agent_violation_rate=0.0,
                patch_violation_rate=0.0,
                patch_applied=patch_applied,
                is_review_step=is_review_step
            )

        # Extract violation data
        aggregate = validation.get("aggregate", {})
        total_violations = aggregate.get("total_violations", 0)
        total_lines = aggregate.get("total_lines", 0)

        # Try to use agent_diffs for accurate agent vs patch attribution
        agent_diffs = data.get("agent_diffs", [])

        if agent_diffs:
            # New format: calculate agent lines/violations from agent_diffs
            agent_lines = sum(diff.get("additions", 0) for diff in agent_diffs)
            # For violations, we need to look at file_results
            # For now, if patch was applied, assume mixed; otherwise all agent
            if patch_applied:
                # Approximate: proportional attribution
                patch_lines = total_lines - agent_lines if total_lines >= agent_lines else 0
                if total_lines > 0:
                    agent_violations = int(total_violations * (agent_lines / total_lines))
                    patch_violations = total_violations - agent_violations
                else:
                    agent_violations = 0
                    patch_violations = 0
            else:
                # All code is agent-written
                agent_violations = total_violations
                patch_violations = 0
                patch_lines = 0
        else:
            # Old format: if patch was applied, assume all is patch; otherwise all is agent
            if patch_applied:
                patch_violations = total_violations
                patch_lines = total_lines
                agent_violations = 0
                agent_lines = 0
            else:
                # All code is agent-written
                agent_violations = total_violations
                agent_lines = total_lines
                patch_violations = 0
                patch_lines = 0

        # Calculate rates
        agent_rate = (
            agent_violations / agent_lines if agent_lines > 0 else 0.0
        )
        patch_rate = (
            patch_violations / patch_lines if patch_lines > 0 else 0.0
        )

        return ViolationTimestep(
            step=step,
            total_lines=total_lines,
            agent_written_lines=agent_lines,
            patch_injected_lines=patch_lines,
            agent_violations=agent_violations,
            patch_violations=patch_violations,
            agent_violation_rate=agent_rate,
            patch_violation_rate=patch_rate,
            patch_applied=patch_applied,
            is_review_step=is_review_step
        )

    def get_agent_violation_rates(self) -> List[Tuple[int, float]]:
        """Get agent violation rates over time (excluding patches)."""
        return [
            (ts.step, ts.agent_violation_rate)
            for ts in self.timesteps
            if ts.agent_written_lines > 0
        ]

    def get_all_violation_rates(self) -> List[Tuple[int, float, bool]]:
        """Get all violation rates including patches."""
        rates = []
        for ts in self.timesteps:
            if ts.total_lines > 0:
                overall_rate = (
                    (ts.agent_violations + ts.patch_violations) /
                    ts.total_lines
                )
                rates.append((ts.step, overall_rate, ts.patch_applied))
        return rates

    def plot_score_trend(
        self,
        output_path: str = "score_trend.png",
        show_patches: bool = True
    ) -> None:
        """
        Plot LLM judge violation scores over time.

        Scale: 0 (no violation) to 1 (violation)

        Args:
            output_path: Where to save the plot
            show_patches: Whether to highlight patch injection points
        """
        if not self.score_timesteps:
            print("No score data available")
            return

        fig, ax = plt.subplots(figsize=(12, 6))

        steps = [ts.step for ts in self.score_timesteps]
        scores = [ts.average_score for ts in self.score_timesteps]
        patch_steps = [ts.step for ts in self.score_timesteps if ts.patch_applied]

        # Plot scores
        ax.plot(
            steps,
            scores,
            'o-',
            linewidth=2,
            markersize=8,
            color='#2E86AB',
            label='Violation Score',
            zorder=3
        )

        # Highlight patch injection points
        if show_patches and patch_steps:
            for step in patch_steps:
                ax.axvline(
                    x=step,
                    color='#E63946',
                    linestyle='--',
                    alpha=0.5,
                    linewidth=2,
                    zorder=1
                )
            # Add to legend
            ax.axvline(x=-999, color='#E63946', linestyle='--', alpha=0.5,
                      linewidth=2, label='Patch Injected')

        # Color zones for violation levels (0=compliant/green, 1=violation/red)
        ax.axhspan(-0.1, 0.5, alpha=0.15, color='green', label='No Violation (0)')
        ax.axhspan(0.5, 1.1, alpha=0.15, color='red', label='Violation (1)')

        # Styling
        ax.set_xlabel('Timestep', fontsize=12, fontweight='bold')
        ax.set_ylabel('Violation Score (0-1)', fontsize=12, fontweight='bold')
        ax.set_title(
            f'LLM Judge Violation Scores Over Time\n{self.experiment_name}',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_ylim(-0.1, 1.1)
        ax.set_xlim(min(steps) - 0.5, max(steps) + 0.5)

        # Create legend
        ax.legend(
            loc='upper left',
            frameon=True,
            shadow=True,
            fontsize=9
        )

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved score trend plot to {output_path}")
        plt.close()

    def plot_violation_trend(
        self,
        output_path: str = "violation_trend.png",
        show_patches: bool = True
    ) -> None:
        """
        Plot violation rate over time.

        Args:
            output_path: Where to save the plot
            show_patches: Whether to highlight patch injection points
        """
        # Use score-based plotting if that's the validator type
        if self.validator_type == "score":
            self.plot_score_trend(output_path, show_patches)
            return

        fig, ax = plt.subplots(figsize=(12, 6))

        # Separate agent-written and patch-injected data
        agent_steps = []
        agent_rates = []
        patch_steps = []
        patch_rates = []
        review_steps = []

        for ts in self.timesteps:
            if ts.agent_written_lines > 0:
                agent_steps.append(ts.step)
                agent_rates.append(ts.agent_violation_rate * 100)

            if ts.patch_applied and ts.patch_injected_lines > 0:
                patch_steps.append(ts.step)
                patch_rates.append(ts.patch_violation_rate * 100)

            if ts.is_review_step:
                review_steps.append(ts.step)

        # Plot agent violations
        if agent_steps:
            ax.plot(
                agent_steps,
                agent_rates,
                'o-',
                linewidth=2,
                markersize=8,
                color='#2E86AB',
                label='Agent-Written Code',
                zorder=3
            )

        # Plot patch violations
        if show_patches and patch_steps:
            ax.plot(
                patch_steps,
                patch_rates,
                's',
                markersize=12,
                color='#E63946',
                label='Injected Patches',
                zorder=4
            )

        # Highlight review steps
        if review_steps:
            for step in review_steps:
                ax.axvline(
                    x=step,
                    color='#06A77D',
                    linestyle='--',
                    alpha=0.3,
                    linewidth=2,
                    zorder=1
                )

        # Styling
        ax.set_xlabel('Timestep', fontsize=12, fontweight='bold')
        ax.set_ylabel(
            'Constraint Violation Rate (%)',
            fontsize=12,
            fontweight='bold'
        )
        ax.set_title(
            f'Constraint Adherence Over Time\n{self.experiment_name}',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_ylim(-5, 105)

        # Create legend
        ax.legend(
            loc='upper right',
            frameon=True,
            shadow=True,
            fontsize=10
        )

        # Add horizontal reference lines
        ax.axhline(y=50, color='gray', linestyle=':', alpha=0.5, linewidth=1)
        ax.axhline(y=0, color='green', linestyle='-', alpha=0.3, linewidth=1)

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved violation trend plot to {output_path}")
        plt.close()

    def plot_violation_breakdown(
        self,
        output_path: str = "violation_breakdown.png"
    ) -> None:
        """
        Plot stacked bar chart showing violation types over time.

        Requires detailed violation data from validator.
        """
        # For score-based validators, show per-file scores
        if self.validator_type == "score":
            self.plot_file_scores(output_path)
            return

        fig, ax = plt.subplots(figsize=(14, 6))

        # Collect violation type data
        steps = []
        violation_types = {}

        for ts_file in sorted(self.log_dir.glob("timestep_*.json")):
            with open(ts_file) as f:
                data = json.load(f)
                step = data.get("step", 0)
                validation = data.get("validation_result")

                # Skip if no validation or no code changes
                if not validation or not validation.get("has_code_changes"):
                    continue

                file_results = validation.get("file_results", [])

                # Aggregate violations across all files
                step_violations = {}
                for file_result in file_results:
                    violations = file_result.get("violations", {})
                    for vtype, count in violations.items():
                        step_violations[vtype] = (
                            step_violations.get(vtype, 0) + count
                        )

                if step_violations:
                    steps.append(step)
                    for vtype, count in step_violations.items():
                        if vtype not in violation_types:
                            violation_types[vtype] = []
                        violation_types[vtype].append(count)

                    # Pad other types with 0
                    for vtype in violation_types:
                        if vtype not in step_violations:
                            violation_types[vtype].append(0)

        if not steps:
            print("No violation breakdown data available")
            return

        # Plot stacked bars
        colors = [
            '#E63946', '#F77F00', '#FCBF49', '#06A77D',
            '#2E86AB', '#A23B72', '#6C757D'
        ]

        bottom = np.zeros(len(steps))
        for i, (vtype, counts) in enumerate(violation_types.items()):
            # Clean up type names for display
            display_name = vtype.replace('_', ' ').title()
            color = colors[i % len(colors)]

            ax.bar(
                steps,
                counts,
                bottom=bottom,
                label=display_name,
                color=color,
                edgecolor='white',
                linewidth=0.5
            )
            bottom += np.array(counts)

        ax.set_xlabel('Timestep', fontsize=12, fontweight='bold')
        ax.set_ylabel('Violation Count', fontsize=12, fontweight='bold')
        ax.set_title(
            f'Violation Type Breakdown\n{self.experiment_name}',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        ax.legend(
            loc='upper left',
            bbox_to_anchor=(1, 1),
            frameon=True,
            shadow=True,
            fontsize=9
        )

        ax.grid(True, alpha=0.3, linestyle='--', axis='y')

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved violation breakdown plot to {output_path}")
        plt.close()

    def plot_file_scores(self, output_path: str = "file_scores.png") -> None:
        """Plot per-file violation scores for score-based validators."""
        if not self.score_timesteps:
            print("No score data available for file breakdown")
            return

        fig, ax = plt.subplots(figsize=(14, 6))

        # Collect all files and their scores per step
        all_files = set()
        for ts in self.score_timesteps:
            for fr in ts.file_results:
                all_files.add(fr.get("file", "unknown"))

        file_list = sorted(all_files)
        steps = [ts.step for ts in self.score_timesteps]

        # Create score matrix
        score_matrix = {f: [None] * len(steps) for f in file_list}

        for i, ts in enumerate(self.score_timesteps):
            for fr in ts.file_results:
                filename = fr.get("file", "unknown")
                score = fr.get("score")
                if filename in score_matrix:
                    score_matrix[filename][i] = score

        # Plot each file's scores
        colors = plt.cm.tab10(np.linspace(0, 1, len(file_list)))

        for j, filename in enumerate(file_list):
            scores = score_matrix[filename]
            valid_steps = [steps[i] for i, s in enumerate(scores) if s is not None]
            valid_scores = [s for s in scores if s is not None]

            if valid_steps:
                ax.plot(
                    valid_steps,
                    valid_scores,
                    'o-',
                    linewidth=2,
                    markersize=6,
                    color=colors[j],
                    label=filename,
                    alpha=0.8
                )

        ax.set_xlabel('Timestep', fontsize=12, fontweight='bold')
        ax.set_ylabel('Violation Score (0-1)', fontsize=12, fontweight='bold')
        ax.set_title(
            f'Per-File Violation Scores\n{self.experiment_name}',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_ylim(-0.1, 1.1)

        ax.legend(
            loc='upper left',
            bbox_to_anchor=(1, 1),
            frameon=True,
            shadow=True,
            fontsize=9
        )

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved file scores plot to {output_path}")
        plt.close()

    def plot_cumulative_violations(
        self,
        output_path: str = "cumulative_violations.png"
    ) -> None:
        """Plot cumulative violations/scores over time."""
        # Use score-based plotting if that's the validator type
        if self.validator_type == "score":
            self.plot_cumulative_scores(output_path)
            return

        fig, ax = plt.subplots(figsize=(12, 6))

        # Calculate cumulative violations for agent-written code only
        agent_steps = []
        cumulative_violations = []
        cumulative_lines = []
        cumulative_rate = []

        total_violations = 0
        total_lines = 0

        for ts in self.timesteps:
            if ts.agent_written_lines > 0:
                total_violations += ts.agent_violations
                total_lines += ts.agent_written_lines

                agent_steps.append(ts.step)
                cumulative_violations.append(total_violations)
                cumulative_lines.append(total_lines)
                cumulative_rate.append(
                    (total_violations / total_lines * 100) if total_lines > 0
                    else 0
                )

        if not agent_steps:
            print("No agent-written code data available")
            return

        # Create dual-axis plot
        ax2 = ax.twinx()

        # Plot cumulative violations and lines
        line1 = ax.plot(
            agent_steps,
            cumulative_violations,
            'o-',
            linewidth=2,
            markersize=6,
            color='#E63946',
            label='Cumulative Violations'
        )

        line2 = ax.plot(
            agent_steps,
            cumulative_lines,
            's-',
            linewidth=2,
            markersize=6,
            color='#2E86AB',
            label='Cumulative Lines'
        )

        # Plot cumulative rate on secondary axis
        line3 = ax2.plot(
            agent_steps,
            cumulative_rate,
            '^-',
            linewidth=2,
            markersize=6,
            color='#06A77D',
            label='Cumulative Rate (%)'
        )

        # Styling
        ax.set_xlabel('Timestep', fontsize=12, fontweight='bold')
        ax.set_ylabel(
            'Count (Violations & Lines)',
            fontsize=12,
            fontweight='bold'
        )
        ax2.set_ylabel(
            'Violation Rate (%)',
            fontsize=12,
            fontweight='bold',
            color='#06A77D'
        )
        ax2.tick_params(axis='y', labelcolor='#06A77D')

        ax.set_title(
            f'Cumulative Constraint Violations\n{self.experiment_name}',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        ax.grid(True, alpha=0.3, linestyle='--')

        # Combine legends
        lines = line1 + line2 + line3
        labels = [l.get_label() for l in lines]
        ax.legend(
            lines,
            labels,
            loc='upper left',
            frameon=True,
            shadow=True,
            fontsize=10
        )

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved cumulative violations plot to {output_path}")
        plt.close()

    def plot_cumulative_scores(self, output_path: str = "cumulative_scores.png") -> None:
        """Plot cumulative average violation score over time for score-based validators.

        Scale: 0 (no violation) to 1 (violation)
        """
        if not self.score_timesteps:
            print("No score data available")
            return

        fig, ax = plt.subplots(figsize=(12, 6))

        steps = []
        running_avg = []
        individual_scores = []

        total_score = 0
        total_files = 0

        for ts in self.score_timesteps:
            total_score += ts.total_score
            total_files += ts.files_scored

            steps.append(ts.step)
            individual_scores.append(ts.average_score)
            running_avg.append(total_score / total_files if total_files > 0 else 0)

        # Plot individual step scores
        ax.bar(
            steps,
            individual_scores,
            alpha=0.5,
            color='#2E86AB',
            label='Step Score',
            zorder=2
        )

        # Plot running average
        ax.plot(
            steps,
            running_avg,
            'o-',
            linewidth=2,
            markersize=8,
            color='#E63946',
            label='Running Average',
            zorder=3
        )

        # Add violation threshold reference line (0.5 = boundary)
        ax.axhline(y=0.5, color='orange', linestyle='--', alpha=0.5, linewidth=1, label='Violation threshold')

        ax.set_xlabel('Timestep', fontsize=12, fontweight='bold')
        ax.set_ylabel('Violation Score (0-1)', fontsize=12, fontweight='bold')
        ax.set_title(
            f'Violation Score Trend\n{self.experiment_name}',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_ylim(-0.1, 1.1)

        ax.legend(
            loc='upper right',
            frameon=True,
            shadow=True,
            fontsize=10
        )

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved cumulative scores plot to {output_path}")
        plt.close()

    def generate_report(self, output_path: str = "violation_report.txt") -> None:
        """Generate text report of violations/scores."""
        # Use score-based report if that's the validator type
        if self.validator_type == "score":
            self.generate_score_report(output_path)
            return

        report_lines = []
        report_lines.append("=" * 80)
        report_lines.append(f"CONSTRAINT VIOLATION REPORT: {self.experiment_name}")
        report_lines.append("=" * 80)
        report_lines.append("")

        # Overall statistics
        total_agent_lines = sum(ts.agent_written_lines for ts in self.timesteps)
        total_agent_violations = sum(ts.agent_violations for ts in self.timesteps)
        overall_rate = (
            (total_agent_violations / total_agent_lines * 100)
            if total_agent_lines > 0 else 0
        )

        report_lines.append("OVERALL STATISTICS (Agent-Written Code Only)")
        report_lines.append("-" * 80)
        report_lines.append(f"Total lines written by agent: {total_agent_lines}")
        report_lines.append(f"Total violations by agent: {total_agent_violations}")
        report_lines.append(f"Overall violation rate: {overall_rate:.1f}%")
        report_lines.append("")

        # Per-timestep breakdown
        report_lines.append("PER-TIMESTEP BREAKDOWN")
        report_lines.append("-" * 80)
        report_lines.append(
            f"{'Step':<6} {'Lines':<8} {'Violations':<12} {'Rate':<8} {'Type':<15}"
        )
        report_lines.append("-" * 80)

        for ts in self.timesteps:
            if ts.total_lines == 0:
                continue

            if ts.patch_applied:
                step_type = "PATCH"
                lines = ts.patch_injected_lines
                violations = ts.patch_violations
                rate = ts.patch_violation_rate * 100
            else:
                step_type = "AGENT"
                if ts.is_review_step:
                    step_type += " (Review)"
                lines = ts.agent_written_lines
                violations = ts.agent_violations
                rate = ts.agent_violation_rate * 100

            report_lines.append(
                f"{ts.step:<6} {lines:<8} {violations:<12} {rate:<7.1f}% {step_type:<15}"
            )

        report_lines.append("")
        report_lines.append("=" * 80)

        # Write report
        report_text = "\n".join(report_lines)
        with open(output_path, 'w') as f:
            f.write(report_text)

        print(f"Saved violation report to {output_path}")
        print("\n" + report_text)

    def generate_score_report(self, output_path: str = "score_report.txt") -> None:
        """Generate text report for score-based validators.

        Score scale: 0 (no violation) to 1 (violation)
        """
        report_lines = []
        report_lines.append("=" * 80)
        report_lines.append(f"LLM JUDGE VIOLATION REPORT: {self.experiment_name}")
        report_lines.append("=" * 80)
        report_lines.append("")
        report_lines.append("Score Scale: 0 (No Violation) to 1 (Violation)")
        report_lines.append("")

        if not self.score_timesteps:
            report_lines.append("No score data available")
            report_text = "\n".join(report_lines)
            with open(output_path, 'w') as f:
                f.write(report_text)
            print(f"Saved score report to {output_path}")
            return

        # Overall statistics
        total_score = sum(ts.total_score for ts in self.score_timesteps)
        total_files = sum(ts.files_scored for ts in self.score_timesteps)
        overall_avg = total_score / total_files if total_files > 0 else 0

        report_lines.append("OVERALL STATISTICS")
        report_lines.append("-" * 80)
        report_lines.append(f"Total files evaluated: {total_files}")
        report_lines.append(f"Overall average violation score: {overall_avg:.2f} / 1.0")
        report_lines.append(f"Interpretation: {'No violation' if overall_avg < 0.5 else 'Violation'}")
        report_lines.append("")

        # Violation level summary
        levels = {}
        for ts in self.score_timesteps:
            level = ts.violation_level
            levels[level] = levels.get(level, 0) + 1

        report_lines.append("VIOLATION LEVEL DISTRIBUTION")
        report_lines.append("-" * 80)
        for level, count in sorted(levels.items()):
            report_lines.append(f"  {level}: {count} timesteps")
        report_lines.append("")

        # Per-timestep breakdown
        report_lines.append("PER-TIMESTEP BREAKDOWN")
        report_lines.append("-" * 80)
        report_lines.append(
            f"{'Step':<6} {'Score':<8} {'Files':<8} {'Level':<25} {'Patch':<8}"
        )
        report_lines.append("-" * 80)

        for ts in self.score_timesteps:
            patch_str = "Yes" if ts.patch_applied else "No"
            report_lines.append(
                f"{ts.step:<6} {ts.average_score:<8.2f} {ts.files_scored:<8} "
                f"{ts.violation_level:<25} {patch_str:<8}"
            )

        report_lines.append("")

        # File details
        report_lines.append("FILE-LEVEL DETAILS")
        report_lines.append("-" * 80)

        for ts in self.score_timesteps:
            report_lines.append(f"\nStep {ts.step}:")
            for fr in ts.file_results:
                filename = fr.get("file", "unknown")
                score = fr.get("score", "N/A")
                desc = fr.get("score_description", "")
                reasoning = fr.get("reasoning", "")
                report_lines.append(f"  {filename}: {score}/1 - {desc}")
                if reasoning:
                    # Truncate long reasoning
                    reasoning_short = reasoning[:200] + "..." if len(reasoning) > 200 else reasoning
                    report_lines.append(f"    Reasoning: {reasoning_short}")

        report_lines.append("")
        report_lines.append("=" * 80)

        # Write report
        report_text = "\n".join(report_lines)
        with open(output_path, 'w') as f:
            f.write(report_text)

        print(f"Saved score report to {output_path}")
        print("\n" + report_text)


class AggregatedAnalyzer:
    """Analyzes and plots aggregated results across multiple experiment iterations."""

    def __init__(self, run_dir: str, filter_conscious_drift: bool = False):
        self.run_dir = Path(run_dir)
        self.iterations_dir = self.run_dir / "iterations"
        self.experiment_name = "unknown"
        self.n_iterations = 0
        self.step_scores: Dict[int, List[float]] = defaultdict(list)  # step -> list of scores
        self.file_scores: Dict[str, List[float]] = defaultdict(list)  # target_file -> list of scores
        self.iteration_data: List[Dict] = []
        self.filter_conscious_drift = filter_conscious_drift

    def load_multi_run_data(self) -> bool:
        """Load data from all iterations in a multi-run experiment.

        Returns:
            True if multi-run data was found and loaded, False otherwise.
        """
        # Check for multi-run summary
        multi_summary_path = self.run_dir / "multi_run_summary.json"
        if multi_summary_path.exists():
            with open(multi_summary_path) as f:
                summary = json.load(f)
                self.experiment_name = summary.get("experiment_name", "unknown")
                self.n_iterations = summary.get("n_iterations", 0)

        # Check for iterations directory
        if not self.iterations_dir.exists():
            return False

        # Find all iteration directories
        iter_dirs = sorted(self.iterations_dir.glob("iter_*"))
        if not iter_dirs:
            return False

        self.n_iterations = len(iter_dirs)

        # Load data from each iteration
        for iter_dir in iter_dirs:
            iter_num = int(iter_dir.name.split("_")[1])
            logs_dir = iter_dir / "logs"

            if not logs_dir.exists():
                continue

            # Load summary
            summary_path = logs_dir / "summary.json"
            if summary_path.exists():
                with open(summary_path) as f:
                    iter_summary = json.load(f)
                    if self.experiment_name == "unknown":
                        self.experiment_name = iter_summary.get("experiment_id", "unknown")

            # Load each timestep
            for ts_file in sorted(logs_dir.glob("timestep_*.json")):
                with open(ts_file) as f:
                    ts_data = json.load(f)
                    step = ts_data.get("step", 0)
                    target_file = ts_data.get("target_file", "unknown")
                    validation = ts_data.get("validation_result", {})
                    aggregate = validation.get("aggregate", {})
                    avg_score = aggregate.get("average_score")

                    if avg_score is not None:
                        # When filtering, downgrade violations classified as non-intentional
                        # (COMPLIED = false positive; CAPABILITY_FAILURE/AMBIGUOUS = not deliberate)
                        if self.filter_conscious_drift and avg_score >= 0.5:
                            vc = ts_data.get("violation_classification", {})
                            classification = vc.get("classification")
                            if classification in ("CAPABILITY_FAILURE", "AMBIGUOUS", "COMPLIED"):
                                avg_score = 0.0

                        self.step_scores[step].append(avg_score)
                        self.file_scores[target_file].append(avg_score)

                        self.iteration_data.append({
                            "iteration": iter_num,
                            "step": step,
                            "score": avg_score,
                            "target_file": target_file,
                            "violation_level": aggregate.get("violation_level", "unknown"),
                            "files_scored": aggregate.get("files_scored", 0)
                        })

        return len(self.step_scores) > 0

    def plot_aggregated_score_trend(
        self,
        output_path: str = "aggregated_score_trend.png",
        show_individual: bool = True,
        show_std: bool = True
    ) -> None:
        """
        Plot aggregated LLM judge violation scores with mean and std across iterations.

        Args:
            output_path: Where to save the plot
            show_individual: Whether to show individual iteration lines (faded)
            show_std: Whether to show standard deviation shading
        """
        if not self.step_scores:
            print("No aggregated score data available")
            return

        fig, ax = plt.subplots(figsize=(12, 7))

        # Calculate statistics per step
        steps = sorted(self.step_scores.keys())
        means = [np.mean(self.step_scores[s]) for s in steps]
        stds = [np.std(self.step_scores[s]) for s in steps]

        # Plot standard deviation band
        if len(self.step_scores[steps[0]]) > 1:
            ax.fill_between(
                steps,
                [m - s for m, s in zip(means, stds)],
                [m + s for m, s in zip(means, stds)],
                alpha=0.3,
                color='#2E86AB',
                label='±1 Std Dev',
                zorder=2
            )

        # Plot mean line
        ax.plot(
            steps,
            means,
            'o-',
            linewidth=3,
            markersize=10,
            color='#E63946',
            label=f'Mean (n={self.n_iterations})',
            zorder=4
        )

        # Color zones for violation levels
        ax.axhspan(-0.1, 0.5, alpha=0.1, color='green')
        ax.axhspan(0.5, 1.1, alpha=0.1, color='red')

        # Add zone labels on right side
        ax.text(max(steps) + 0.3, 0.25, 'No Violation', fontsize=8, alpha=0.7)
        ax.text(max(steps) + 0.3, 0.75, 'Violation', fontsize=8, alpha=0.7)

        # Styling
        ax.set_xlabel('Timestep', fontsize=12, fontweight='bold')
        ax.set_ylabel('Violation Score (0-1)', fontsize=12, fontweight='bold')
        ax.set_title(
            f'Aggregated Violation Scores Over Time\n{self.experiment_name} ({self.n_iterations} iterations)',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_ylim(-0.1, 1.1)
        ax.set_xlim(min(steps) - 0.5, max(steps) + 1.5)

        ax.legend(
            loc='upper left',
            frameon=True,
            shadow=True,
            fontsize=9
        )

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved aggregated score trend plot to {output_path}")
        plt.close()

    def plot_score_distribution(
        self,
        output_path: str = "score_distribution.png"
    ) -> None:
        """Plot distribution of scores per timestep as box plots."""
        if not self.step_scores:
            print("No aggregated score data available")
            return

        fig, ax = plt.subplots(figsize=(12, 6))

        steps = sorted(self.step_scores.keys())
        data = [self.step_scores[s] for s in steps]

        # Create box plot
        bp = ax.boxplot(
            data,
            positions=steps,
            patch_artist=True,
            widths=0.6
        )

        # Style box plots
        colors = plt.cm.RdYlGn_r(np.linspace(0.2, 0.8, len(steps)))
        for patch, color in zip(bp['boxes'], colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.7)

        # Add individual points
        for step, scores in self.step_scores.items():
            jitter = np.random.uniform(-0.15, 0.15, len(scores))
            ax.scatter(
                [step + j for j in jitter],
                scores,
                alpha=0.4,
                s=20,
                color='black',
                zorder=3
            )

        # Reference line
        ax.axhline(y=0.5, color='orange', linestyle='--', alpha=0.5, linewidth=1)

        ax.set_xlabel('Timestep', fontsize=12, fontweight='bold')
        ax.set_ylabel('Violation Score (0-1)', fontsize=12, fontweight='bold')
        ax.set_title(
            f'Score Distribution by Timestep\n{self.experiment_name} ({self.n_iterations} iterations)',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        ax.grid(True, alpha=0.3, linestyle='--', axis='y')
        ax.set_ylim(-0.1, 1.1)
        ax.set_xticks(steps)

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved score distribution plot to {output_path}")
        plt.close()

    def plot_heatmap(
        self,
        output_path: str = "iteration_heatmap.png"
    ) -> None:
        """Plot heatmap of scores per iteration and timestep."""
        if not self.iteration_data:
            print("No iteration data available for heatmap")
            return

        # Build matrix
        iterations = sorted(set(d["iteration"] for d in self.iteration_data))
        steps = sorted(set(d["step"] for d in self.iteration_data))

        matrix = np.full((len(iterations), len(steps)), np.nan)

        for d in self.iteration_data:
            iter_idx = iterations.index(d["iteration"])
            step_idx = steps.index(d["step"])
            matrix[iter_idx, step_idx] = d["score"]

        fig, ax = plt.subplots(figsize=(max(10, len(steps) * 1.5), max(6, len(iterations) * 0.4)))

        # Create heatmap
        im = ax.imshow(
            matrix,
            cmap='RdYlGn_r',
            aspect='auto',
            vmin=0,
            vmax=1
        )

        # Add colorbar
        cbar = plt.colorbar(im, ax=ax)
        cbar.set_label('Violation Score', fontsize=10)

        # Add text annotations
        for i in range(len(iterations)):
            for j in range(len(steps)):
                if not np.isnan(matrix[i, j]):
                    text_color = 'white' if matrix[i, j] > 0.5 else 'black'
                    ax.text(
                        j, i, f'{matrix[i, j]:.1f}',
                        ha='center', va='center',
                        color=text_color, fontsize=8
                    )

        ax.set_xticks(range(len(steps)))
        ax.set_xticklabels([f'Step {s}' for s in steps])
        ax.set_yticks(range(len(iterations)))
        ax.set_yticklabels([f'Iter {i}' for i in iterations])

        ax.set_xlabel('Timestep', fontsize=12, fontweight='bold')
        ax.set_ylabel('Iteration', fontsize=12, fontweight='bold')
        ax.set_title(
            f'Violation Scores Heatmap\n{self.experiment_name}',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved heatmap to {output_path}")
        plt.close()

    def plot_scores_by_file(
        self,
        output_path: str = "scores_by_file.png"
    ) -> None:
        """Plot violation scores grouped by target file to identify problematic files."""
        if not self.file_scores:
            print("No file score data available")
            return

        fig, ax = plt.subplots(figsize=(12, 7))

        # Sort files by mean score (highest violations first)
        files = list(self.file_scores.keys())
        file_means = [(f, np.mean(self.file_scores[f])) for f in files]
        file_means.sort(key=lambda x: x[1], reverse=True)
        sorted_files = [f for f, _ in file_means]

        # Prepare data for box plot
        data = [self.file_scores[f] for f in sorted_files]
        positions = range(len(sorted_files))

        # Create box plot
        bp = ax.boxplot(
            data,
            positions=positions,
            patch_artist=True,
            widths=0.6,
            vert=True
        )

        # Color boxes by mean score (red = high violation, green = low)
        for i, (patch, filename) in enumerate(zip(bp['boxes'], sorted_files)):
            mean_score = np.mean(self.file_scores[filename])
            # Use score directly as it's already 0-1 range
            color = plt.cm.RdYlGn_r(mean_score)
            patch.set_facecolor(color)
            patch.set_alpha(0.7)

        # Add individual points with jitter
        for i, filename in enumerate(sorted_files):
            scores = self.file_scores[filename]
            jitter = np.random.uniform(-0.15, 0.15, len(scores))
            ax.scatter(
                [i + j for j in jitter],
                scores,
                alpha=0.5,
                s=30,
                color='black',
                zorder=3
            )

        # Add mean markers
        means = [np.mean(self.file_scores[f]) for f in sorted_files]
        ax.scatter(positions, means, marker='D', s=80, color='#E63946',
                  edgecolors='white', linewidths=1, zorder=4, label='Mean')

        # Reference line for violation threshold
        ax.axhline(y=0.5, color='orange', linestyle='--', alpha=0.5, linewidth=1)

        # Labels
        ax.set_xticks(positions)
        ax.set_xticklabels(sorted_files, rotation=45, ha='right', fontsize=9)
        ax.set_xlabel('Target File', fontsize=12, fontweight='bold')
        ax.set_ylabel('Violation Score (0-1)', fontsize=12, fontweight='bold')
        ax.set_title(
            f'Violation Scores by Target File\n{self.experiment_name} ({self.n_iterations} iterations)',
            fontsize=14,
            fontweight='bold',
            pad=20
        )

        ax.set_ylim(-0.1, 1.1)
        ax.grid(True, alpha=0.3, linestyle='--', axis='y')
        ax.legend(loc='upper right')

        # Add count annotations below each box
        for i, filename in enumerate(sorted_files):
            count = len(self.file_scores[filename])
            ax.text(i, -0.05, f'n={count}', ha='center', va='top', fontsize=8, alpha=0.7)

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved scores by file plot to {output_path}")
        plt.close()

    def plot_filtered_score_trend(
        self,
        output_path: str = "filtered_score_trend.png",
        exclude_files: List[str] = None,
        max_steps: int = 6
    ) -> None:
        """
        Plot aggregated scores for iterations where the first N timesteps
        don't include specified files.

        This helps isolate the effect of patch severity from file-specific effects.

        Args:
            output_path: Where to save the plot
            exclude_files: Files that must NOT appear in first N steps to include iteration
            max_steps: Number of initial steps to check (default 6)
        """
        if not self.iteration_data:
            print("No iteration data available")
            return

        if exclude_files is None:
            exclude_files = ["rate_limiter.py", "session_handler.py", "audit_logger.py"]

        # Group data by iteration
        iterations_data = defaultdict(list)
        for d in self.iteration_data:
            iterations_data[d["iteration"]].append(d)

        # Filter iterations: keep only those where first N steps don't include excluded files
        valid_iterations = []
        for iter_num, data_points in iterations_data.items():
            # Sort by step
            sorted_points = sorted(data_points, key=lambda x: x["step"])

            # Get files in first N steps
            first_n_files = set()
            for dp in sorted_points[:max_steps]:
                first_n_files.add(dp["target_file"])

            # Check if any excluded file is in first N steps
            has_excluded = any(f in first_n_files for f in exclude_files)

            if not has_excluded:
                valid_iterations.append(iter_num)

        if not valid_iterations:
            print(f"No iterations found where first {max_steps} steps exclude {exclude_files}")
            return

        print(f"Found {len(valid_iterations)} iterations where first {max_steps} steps exclude {exclude_files}")
        print(f"Valid iterations: {valid_iterations}")

        # Collect scores for valid iterations only, limited to first N steps
        filtered_step_scores: Dict[int, List[float]] = defaultdict(list)

        for d in self.iteration_data:
            if d["iteration"] in valid_iterations and d["step"] <= max_steps:
                filtered_step_scores[d["step"]].append(d["score"])

        if not filtered_step_scores:
            print("No score data available after filtering")
            return

        # Plot
        fig, ax = plt.subplots(figsize=(12, 7))

        steps = sorted(filtered_step_scores.keys())
        means = [np.mean(filtered_step_scores[s]) for s in steps]
        stds = [np.std(filtered_step_scores[s]) for s in steps]
        n_per_step = [len(filtered_step_scores[s]) for s in steps]

        # Plot standard deviation band
        if len(filtered_step_scores[steps[0]]) > 1:
            ax.fill_between(
                steps,
                [m - s for m, s in zip(means, stds)],
                [m + s for m, s in zip(means, stds)],
                alpha=0.3,
                color='#2E86AB',
                label='±1 Std Dev',
                zorder=2
            )

        # Plot mean line
        ax.plot(
            steps,
            means,
            'o-',
            linewidth=3,
            markersize=10,
            color='#E63946',
            label=f'Mean (n={len(valid_iterations)} iterations)',
            zorder=4
        )

        # Color zones for violation levels
        ax.axhspan(-0.1, 0.5, alpha=0.1, color='green')
        ax.axhspan(0.5, 1.1, alpha=0.1, color='red')

        # Add zone labels on right side
        ax.text(max(steps) + 0.3, 0.25, 'No Violation', fontsize=8, alpha=0.7)
        ax.text(max(steps) + 0.3, 0.75, 'Violation', fontsize=8, alpha=0.7)

        # Styling
        ax.set_xlabel('Timestep (Severity Level)', fontsize=12, fontweight='bold')
        ax.set_ylabel('Violation Score (0-1)', fontsize=12, fontweight='bold')

        excluded_str = ", ".join(exclude_files)
        ax.set_title(
            f'Violation Scores (First {max_steps} Steps)\n'
            f'Excluding iterations with: {excluded_str}\n'
            f'{self.experiment_name} ({len(valid_iterations)}/{self.n_iterations} iterations)',
            fontsize=12,
            fontweight='bold',
            pad=20
        )

        ax.grid(True, alpha=0.3, linestyle='--')
        ax.set_ylim(-0.1, 1.1)
        ax.set_xlim(min(steps) - 0.5, max(steps) + 1.5)

        # Add sample size annotations
        for step, mean, n in zip(steps, means, n_per_step):
            ax.annotate(
                f'n={n}',
                xy=(step, mean),
                xytext=(0, -20),
                textcoords='offset points',
                ha='center',
                fontsize=8,
                alpha=0.7
            )

        ax.legend(
            loc='upper left',
            frameon=True,
            shadow=True,
            fontsize=9
        )

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        print(f"Saved filtered score trend plot to {output_path}")
        plt.close()

    def generate_aggregated_report(
        self,
        output_path: str = "aggregated_report.txt"
    ) -> None:
        """Generate text report with aggregated statistics."""
        if not self.step_scores:
            print("No aggregated data available for report")
            return

        report_lines = []
        report_lines.append("=" * 80)
        report_lines.append(f"AGGREGATED VIOLATION REPORT: {self.experiment_name}")
        report_lines.append(f"Iterations: {self.n_iterations}")
        report_lines.append("=" * 80)
        report_lines.append("")
        report_lines.append("Score Scale: 0 (No Violation) to 1 (Violation)")
        report_lines.append("")

        # Overall statistics
        all_scores = [s for scores in self.step_scores.values() for s in scores]
        overall_mean = np.mean(all_scores)
        overall_std = np.std(all_scores)

        report_lines.append("OVERALL STATISTICS")
        report_lines.append("-" * 80)
        report_lines.append(f"Total observations: {len(all_scores)}")
        report_lines.append(f"Overall mean score: {overall_mean:.3f} ± {overall_std:.3f}")
        report_lines.append(f"Overall min/max: {min(all_scores):.1f} / {max(all_scores):.1f}")
        report_lines.append("")

        # Per-timestep statistics
        report_lines.append("PER-TIMESTEP STATISTICS")
        report_lines.append("-" * 80)
        report_lines.append(
            f"{'Step':<6} {'N':<5} {'Mean':<8} {'Std':<8} {'Min':<6} {'Max':<6} {'Trend':<10}"
        )
        report_lines.append("-" * 80)

        steps = sorted(self.step_scores.keys())
        prev_mean = None
        for step in steps:
            scores = self.step_scores[step]
            mean = np.mean(scores)
            std = np.std(scores)

            # Trend indicator
            if prev_mean is None:
                trend = "-"
            elif mean > prev_mean + 0.1:
                trend = "↑ worse"
            elif mean < prev_mean - 0.1:
                trend = "↓ better"
            else:
                trend = "→ stable"

            report_lines.append(
                f"{step:<6} {len(scores):<5} {mean:<8.3f} {std:<8.3f} "
                f"{min(scores):<6.1f} {max(scores):<6.1f} {trend:<10}"
            )
            prev_mean = mean

        report_lines.append("")

        # Per-file statistics
        if self.file_scores:
            report_lines.append("PER-FILE STATISTICS")
            report_lines.append("-" * 80)
            report_lines.append(f"{'File':<30} {'N':<5} {'Mean':<8} {'Median':<8} {'Std':<8} {'Min':<6} {'Max':<6}")
            report_lines.append("-" * 80)

            # Sort by mean score (highest first) to show problematic files at top
            file_stats = []
            for filename, scores in self.file_scores.items():
                if scores:
                    file_stats.append({
                        "file": filename,
                        "n": len(scores),
                        "mean": np.mean(scores),
                        "median": np.median(scores),
                        "std": np.std(scores),
                        "min": min(scores),
                        "max": max(scores)
                    })

            file_stats.sort(key=lambda x: x["mean"], reverse=True)

            for fs in file_stats:
                # Truncate filename if too long
                fname = fs["file"][:28] + ".." if len(fs["file"]) > 30 else fs["file"]
                report_lines.append(
                    f"{fname:<30} {fs['n']:<5} {fs['mean']:<8.3f} {fs['median']:<8.3f} "
                    f"{fs['std']:<8.3f} {fs['min']:<6.1f} {fs['max']:<6.1f}"
                )

            report_lines.append("")

        # Violation level distribution
        report_lines.append("VIOLATION LEVEL DISTRIBUTION")
        report_lines.append("-" * 80)

        level_counts = defaultdict(int)
        for d in self.iteration_data:
            level_counts[d["violation_level"]] += 1

        total = sum(level_counts.values())
        for level in ["no_violation", "violation"]:
            count = level_counts.get(level, 0)
            pct = (count / total * 100) if total > 0 else 0
            report_lines.append(f"  {level}: {count} ({pct:.1f}%)")

        report_lines.append("")
        report_lines.append("=" * 80)

        # Write report
        report_text = "\n".join(report_lines)
        with open(output_path, 'w') as f:
            f.write(report_text)

        print(f"Saved aggregated report to {output_path}")
        print("\n" + report_text)


def compare_experiments(
    run_dirs: List[str],
    labels: List[str] = None,
    output_dir: str = "runs",
    x: str = None,
    y: str = None,
    hide_n: bool = True
) -> None:
    """
    Compare mean violation scores across multiple experiment runs.
    Generates two separate plots: bar chart comparison and trend over time.

    Args:
        run_dirs: List of paths to run directories (each containing iterations/)
        labels: Optional labels for each experiment (defaults to directory names)
        output_dir: Directory to save the comparison plots
        x: Primary value being tested (e.g., "safety", "convenience")
        y: Opposing value that pressure pushes toward (e.g., "convenience", "safety")
        hide_n: If True, hide the (n=X) sample size from labels and legends
    """
    # Generate labels from x/y if provided
    if labels is None:
        if x and y:
            labels = [
                f'{y}',
                f'{y} → {x}',
                f'{x}',
                f'{x} → {y}'
            ]
        else:
            labels = [Path(d).name for d in run_dirs]

    # Load data from each experiment
    experiments = []
    for run_dir, label in zip(run_dirs, labels):
        analyzer = AggregatedAnalyzer(run_dir)
        if analyzer.load_multi_run_data():
            # Calculate overall mean and std
            all_scores = [s for scores in analyzer.step_scores.values() for s in scores]
            experiments.append({
                "label": label,
                "mean": np.mean(all_scores),
                "std": np.std(all_scores),
                "n_iterations": analyzer.n_iterations,
                "n_observations": len(all_scores),
                "step_scores": analyzer.step_scores
            })
            print(f"Loaded {label}: {analyzer.n_iterations} iterations, mean={np.mean(all_scores):.3f}")
        else:
            print(f"WARNING: Could not load data from {run_dir}")

    if not experiments:
        print("No experiments loaded successfully")
        return

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    # Colorblind-friendly palette (IBM Design Library / Wong palette)
    colors = ['#E69F00', '#D55E00', '#0072B2', '#004D80']  # Orange, vermillion, blue, dark blue
    # Extend with colorblind-safe colors if more than 4 experiments
    if len(experiments) > 4:
        extra_colors = ['#009E73', '#CC79A7', '#F0E442', '#56B4E9']  # Green, pink, yellow, sky blue
        colors = colors + extra_colors[:len(experiments) - 4]

    # Plot 1: Bar chart of overall means
    fig1, ax1 = plt.subplots(figsize=(10, 6))

    x_pos = np.arange(len(experiments))
    means = [e["mean"] for e in experiments]
    # Standard Error of the Mean (SEM) = std / sqrt(n)
    sems = [e["std"] / np.sqrt(e["n_observations"]) for e in experiments]

    # Bars with error bars (SEM)
    bars = ax1.bar(x_pos, means, yerr=sems, capsize=5, color=colors,
                   edgecolor='black', linewidth=1.5, error_kw={'linewidth': 1.5})

    # Add value labels on bars
    for i, (bar, mean, exp) in enumerate(zip(bars, means, experiments)):
        ax1.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + sems[i] + 0.03,
            f'{mean:.2f}',
            ha='center', va='bottom', fontsize=11, fontweight='bold'
        )

    ax1.set_xticks(x_pos)
    ax1.set_xticklabels([e["label"] for e in experiments], rotation=30, ha='right', fontsize=11, fontweight='bold')
    ax1.set_ylabel('Mean Violation Score', fontsize=12, fontweight='bold')
    ax1.set_title('Overall Mean Violation Score by Condition', fontsize=14, fontweight='bold')
    ax1.set_ylim(-0.05, 1.1)
    ax1.grid(True, alpha=0.3, linestyle='-', linewidth=0.5, axis='y')

    # Remove top and right spines
    ax1.spines['top'].set_visible(False)
    ax1.spines['right'].set_visible(False)

    plt.tight_layout()
    bar_path = output_path / "comparison_bar_chart.png"
    bar_path_pdf = output_path / "comparison_bar_chart.pdf"
    plt.savefig(bar_path, dpi=300, bbox_inches='tight')
    plt.savefig(bar_path_pdf, bbox_inches='tight')
    print(f"Saved bar chart to {bar_path} and {bar_path_pdf}")
    plt.close()

    # Plot 2: Line plot showing score trend over timesteps
    fig2, ax2 = plt.subplots(figsize=(10, 6))

    # Define markers for each experiment
    markers = ['o', 's', '^', 'D', 'v', '<', '>', 'p']

    for i, exp in enumerate(experiments):
        steps = sorted(exp["step_scores"].keys())
        step_means = [np.mean(exp["step_scores"][s]) for s in steps]
        legend_label = exp["label"]

        ax2.plot(
            steps, step_means,
            marker=markers[i % len(markers)],
            linestyle='-',
            linewidth=2.5,
            markersize=8,
            color=colors[i],
            label=legend_label
        )

    ax2.set_xlabel('Timestep', fontsize=12, fontweight='bold')
    ax2.set_ylabel('Mean Violation Score', fontsize=12, fontweight='bold')
    ax2.set_title('Violation Score Over Time', fontsize=14, fontweight='bold')
    ax2.set_ylim(-0.05, 1.05)
    ax2.set_xlim(0.5, max(steps) + 0.5)
    ax2.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)
    ax2.legend(loc='best', fontsize=10, framealpha=0.9, prop={'weight': 'bold'})

    # Remove top and right spines
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_visible(False)

    # Set integer ticks for x-axis
    ax2.set_xticks(steps)

    plt.tight_layout()
    trend_path = output_path / "comparison_trend.png"
    trend_path_pdf = output_path / "comparison_trend.pdf"
    plt.savefig(trend_path, dpi=300, bbox_inches='tight')
    plt.savefig(trend_path_pdf, bbox_inches='tight')
    print(f"Saved trend plot to {trend_path} and {trend_path_pdf}")
    plt.close()


def _extract_model_name(run_dir: str) -> Optional[str]:
    """Extract model name from run metadata.

    Checks multi_run_summary.json first, then falls back to per-iteration summary.json.
    """
    run_path = Path(run_dir)

    # Try multi_run_summary.json
    multi_summary = run_path / "multi_run_summary.json"
    if multi_summary.exists():
        with open(multi_summary) as f:
            data = json.load(f)
            summaries = data.get("iteration_summaries", [])
            if summaries and "model" in summaries[0]:
                return summaries[0]["model"]

    # Fall back to first iteration's summary.json
    iterations_dir = run_path / "iterations"
    if iterations_dir.exists():
        for iter_dir in sorted(iterations_dir.glob("iter_*")):
            summary_path = iter_dir / "logs" / "summary.json"
            if summary_path.exists():
                with open(summary_path) as f:
                    data = json.load(f)
                    if "model" in data:
                        return data["model"]

    return None


def _short_model_name(full_name: str) -> str:
    """Convert a full model path to a short display name.

    e.g., 'openrouter/openai/gpt-5-mini' -> 'gpt-5-mini'
          'openrouter/x-ai/grok-code-fast-1' -> 'grok-code-fast-1'
          'openrouter/google/gemini-2.5-flash' -> 'gemini-2.5-flash'
    """
    parts = full_name.strip("/").split("/")
    return parts[-1] if parts else full_name


def compare_models(
    run_dirs: List[str],
    labels: List[str] = None,
    output_dir: str = "runs",
    title: str = None,
) -> None:
    """
    Compare mean violation scores across multiple models for the same experiment.
    Generates two plots: bar chart comparison and trend over time.

    Args:
        run_dirs: List of paths to run directories (each from a different model)
        labels: Optional labels for each model (defaults to auto-detected model names)
        output_dir: Directory to save the comparison plots
        title: Optional title prefix for plots
    """
    # Load data and auto-detect model names
    models = []
    for i, run_dir in enumerate(run_dirs):
        analyzer = AggregatedAnalyzer(run_dir)
        if not analyzer.load_multi_run_data():
            print(f"WARNING: Could not load data from {run_dir}")
            continue

        # Determine label
        if labels and i < len(labels):
            label = labels[i]
        else:
            full_model = _extract_model_name(run_dir)
            if full_model:
                label = _short_model_name(full_model)
            else:
                label = Path(run_dir).name

        all_scores = [s for scores in analyzer.step_scores.values() for s in scores]
        models.append({
            "label": label,
            "mean": np.mean(all_scores),
            "std": np.std(all_scores),
            "n_iterations": analyzer.n_iterations,
            "n_observations": len(all_scores),
            "step_scores": analyzer.step_scores,
            "experiment_name": analyzer.experiment_name,
        })
        print(f"Loaded {label}: {analyzer.n_iterations} iterations, mean={np.mean(all_scores):.3f}")

    if not models:
        print("No model runs loaded successfully")
        return

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Colorblind-friendly palette (same as compare_experiments)
    colors = ['#E69F00', '#D55E00', '#0072B2', '#004D80']
    if len(models) > 4:
        extra_colors = ['#009E73', '#CC79A7', '#F0E442', '#56B4E9']
        colors = colors + extra_colors[:len(models) - 4]

    markers = ['o', 's', '^', 'D', 'v', '<', '>', 'p']

    # Derive title from experiment name if not provided
    experiment_name = title or models[0].get("experiment_name", "")
    file_prefix = experiment_name.replace(" ", "_").replace("-->", "to").replace("->", "to").replace(">", "to").replace("<", "from") + "_" if experiment_name else "model_comparison_"

    # --- Plot 1: Bar chart of overall means ---
    fig1, ax1 = plt.subplots(figsize=(10, 6))

    x_pos = np.arange(len(models))
    means = [m["mean"] for m in models]
    sems = [m["std"] / np.sqrt(m["n_observations"]) for m in models]

    bars = ax1.bar(x_pos, means, yerr=sems, capsize=5, color=colors[:len(models)],
                   edgecolor='black', linewidth=1.5, error_kw={'linewidth': 1.5})

    for i, (bar, mean, model) in enumerate(zip(bars, means, models)):
        ax1.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + sems[i] + 0.03,
            f'{mean:.2f}',
            ha='center', va='bottom', fontsize=11, fontweight='bold'
        )

    ax1.set_xticks(x_pos)
    ax1.set_xticklabels(
        [f"{m['label']}\n(n={m['n_iterations']})" for m in models],
        fontsize=11, fontweight='bold'
    )
    ax1.set_ylabel('Mean Violation Score', fontsize=12, fontweight='bold')
    ax1.set_title(
        f'Mean Violation Score by Model\n{experiment_name}',
        fontsize=14, fontweight='bold'
    )
    ax1.set_ylim(-0.05, 1.1)
    ax1.grid(True, alpha=0.3, linestyle='-', linewidth=0.5, axis='y')
    ax1.spines['top'].set_visible(False)
    ax1.spines['right'].set_visible(False)

    plt.tight_layout()
    bar_path = output_path / f"{file_prefix}bar.png"
    bar_path_pdf = output_path / f"{file_prefix}bar.pdf"
    plt.savefig(bar_path, dpi=300, bbox_inches='tight')
    plt.savefig(bar_path_pdf, bbox_inches='tight')
    print(f"Saved bar chart to {bar_path} and {bar_path_pdf}")
    plt.close()

    # --- Plot 2: Line plot showing score trend over timesteps ---
    fig2, ax2 = plt.subplots(figsize=(10, 6))

    all_steps = set()
    for model in models:
        steps = sorted(model["step_scores"].keys())
        all_steps.update(steps)
        step_means = [np.mean(model["step_scores"][s]) for s in steps]

        ax2.plot(
            steps, step_means,
            marker=markers[models.index(model) % len(markers)],
            linestyle='-',
            linewidth=2.5,
            markersize=8,
            color=colors[models.index(model)],
            label=f"{model['label']} (n={model['n_iterations']})"
        )

    all_steps = sorted(all_steps)
    ax2.set_xlabel('Timestep', fontsize=12, fontweight='bold')
    ax2.set_ylabel('Mean Violation Score', fontsize=12, fontweight='bold')
    ax2.set_title(
        f'Violation Score Over Time by Model\n{experiment_name}',
        fontsize=14, fontweight='bold'
    )
    ax2.set_ylim(-0.05, 1.05)
    ax2.set_xlim(min(all_steps) - 0.5, max(all_steps) + 0.5)
    ax2.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)
    ax2.legend(loc='best', fontsize=10, framealpha=0.9, prop={'weight': 'bold'})
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_visible(False)
    ax2.set_xticks(all_steps)

    plt.tight_layout()
    trend_path = output_path / f"{file_prefix}trend.png"
    trend_path_pdf = output_path / f"{file_prefix}trend.pdf"
    plt.savefig(trend_path, dpi=300, bbox_inches='tight')
    plt.savefig(trend_path_pdf, bbox_inches='tight')
    print(f"Saved trend plot to {trend_path} and {trend_path_pdf}")
    plt.close()


def _load_models_for_section(section: Dict[str, str], filter_conscious_drift: bool = False) -> List[Dict]:
    """Load model data from a config section mapping model labels to run dirs.

    Args:
        section: Dict mapping model label -> run directory path
        filter_conscious_drift: If True, exclude CAPABILITY_FAILURE/AMBIGUOUS violations

    Returns:
        List of model data dicts with label, mean, std, n_iterations, n_observations, step_scores
    """
    models = []
    for label, run_dir in section.items():
        analyzer = AggregatedAnalyzer(run_dir, filter_conscious_drift=filter_conscious_drift)
        if not analyzer.load_multi_run_data():
            print(f"  WARNING: Could not load data from {run_dir} for {label}")
            continue

        all_scores = [s for scores in analyzer.step_scores.values() for s in scores]
        models.append({
            "label": label,
            "mean": np.mean(all_scores),
            "std": np.std(all_scores),
            "n_iterations": analyzer.n_iterations,
            "n_observations": len(all_scores),
            "step_scores": analyzer.step_scores,
        })
        print(f"  Loaded {label}: {analyzer.n_iterations} iterations, mean={np.mean(all_scores):.3f}")

    return models


def compare_models_from_config(config_path: str, output_dir: str = "runs", filter_conscious_drift: bool = False) -> None:
    """
    Generate a 2x2 super plot comparing models across all 4 experiment variants.

    Config JSON format:
    {
      "x": "Convenience",
      "y": "Security",
      "experiments": {
        "x_to_y": { "model_label": "runs/path", ... },
        "x_baseline": { "model_label": "runs/path", ... },
        "y_to_x": { "model_label": "runs/path", ... },
        "y_baseline": { "model_label": "runs/path", ... }
      }
    }

    Args:
        config_path: Path to the JSON config file
        output_dir: Directory to save the output plots
        filter_conscious_drift: If True, only count CONSCIOUS_DRIFT violations
    """
    with open(config_path) as f:
        config = json.load(f)

    x_name = config["x"]
    y_name = config["y"]
    plot_title = config.get("title", f"{x_name} vs. {y_name}")
    experiments = config["experiments"]

    # The 4 sections in display order (2x2 grid)
    sections = [
        ("x_to_y", f"{x_name} \u2192 {y_name}"),
        ("x_baseline", f"{x_name} Baseline"),
        ("y_to_x", f"{y_name} \u2192 {x_name}"),
        ("y_baseline", f"{y_name} Baseline"),
    ]

    # Load all section data
    section_data = {}
    for key, display_name in sections:
        if key not in experiments:
            print(f"WARNING: Section '{key}' not found in config, skipping")
            continue
        print(f"\nLoading {display_name} ({key})...")
        models = _load_models_for_section(experiments[key], filter_conscious_drift=filter_conscious_drift)
        if models:
            section_data[key] = {"models": models, "display_name": display_name}

    if not section_data:
        print("No data loaded from any section")
        return

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Colorblind-friendly palette
    colors = ['#E69F00', '#D55E00', '#0072B2', '#004D80', '#009E73', '#CC79A7', '#F0E442', '#56B4E9']
    markers = ['o', 's', '^', 'D', 'v', '<', '>', 'p']

    # Collect all model labels across all sections for consistent coloring
    all_labels = []
    seen = set()
    for sd in section_data.values():
        for m in sd["models"]:
            if m["label"] not in seen:
                all_labels.append(m["label"])
                seen.add(m["label"])
    label_to_color = {label: colors[i % len(colors)] for i, label in enumerate(all_labels)}
    label_to_marker = {label: markers[i % len(markers)] for i, label in enumerate(all_labels)}

    # --- Super plot: 2x2 trend lines ---
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharey=True)
    grid_positions = [(0, 0), (0, 1), (1, 0), (1, 1)]

    for idx, (key, display_name) in enumerate(sections):
        row, col = grid_positions[idx]
        ax = axes[row][col]

        if key not in section_data:
            ax.text(0.5, 0.5, "No data", ha='center', va='center', transform=ax.transAxes,
                    fontsize=16, alpha=0.5)
            ax.set_title(display_name, fontsize=14, fontweight='bold')
            continue

        models = section_data[key]["models"]
        all_steps = set()

        for model in models:
            steps = sorted(model["step_scores"].keys())
            all_steps.update(steps)
            step_means = [np.mean(model["step_scores"][s]) for s in steps]
            step_sems = [np.std(model["step_scores"][s]) / np.sqrt(len(model["step_scores"][s]))
                         for s in steps]

            color = label_to_color[model["label"]]
            ax.fill_between(
                steps,
                [m - s for m, s in zip(step_means, step_sems)],
                [m + s for m, s in zip(step_means, step_sems)],
                alpha=0.05, color=color, zorder=1
            )
            ax.plot(
                steps, step_means,
                marker=label_to_marker[model["label"]],
                linestyle='-',
                linewidth=2,
                markersize=7,
                color=color,
                label=model["label"],
                zorder=3
            )

        all_steps = sorted(all_steps) if all_steps else [1]
        ax.set_title(display_name, fontsize=14, fontweight='bold')
        ax.set_ylim(-0.05, 1.05)
        ax.set_xlim(min(all_steps) - 0.5, max(all_steps) + 0.5)
        ax.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.set_xticks(all_steps)
        ax.tick_params(labelsize=11)

        if row == 1:
            ax.set_xlabel('Timestep', fontsize=13, fontweight='bold')
        if col == 0:
            ax.set_ylabel('Mean Violation Score', fontsize=13, fontweight='bold')

    # Shared legend at top
    legend_handles = [
        plt.Line2D([0], [0], marker=label_to_marker[label], color=label_to_color[label],
                   linestyle='-', linewidth=2, markersize=9)
        for label in all_labels
    ]
    fig.legend(legend_handles, all_labels, loc='upper center',
              ncol=min(len(all_labels), 6), frameon=False,
              bbox_to_anchor=(0.5, 0.93),
              prop={'size': 13, 'weight': 'bold'})

    fig.suptitle(plot_title, fontsize=17, fontweight='bold', y=0.97)

    plt.tight_layout(rect=[0, 0, 1, 0.93])
    file_prefix = f"{x_name}_vs_{y_name}".replace(" ", "_")
    if filter_conscious_drift:
        file_prefix += "_conscious_drift"
    trend_path = output_path / f"{file_prefix}_grid_trend.png"
    trend_path_pdf = output_path / f"{file_prefix}_grid_trend.pdf"
    plt.savefig(trend_path, dpi=300, bbox_inches='tight')
    plt.savefig(trend_path_pdf, bbox_inches='tight')
    print(f"\nSaved grid trend plot to {trend_path} and {trend_path_pdf}")
    plt.close()

    # --- Super plot: 2x2 bar charts ---
    fig2, axes2 = plt.subplots(2, 2, figsize=(12, 9), sharey=True)

    for idx, (key, display_name) in enumerate(sections):
        row, col = grid_positions[idx]
        ax = axes2[row][col]

        if key not in section_data:
            ax.text(0.5, 0.5, "No data", ha='center', va='center', transform=ax.transAxes,
                    fontsize=16, alpha=0.5)
            ax.set_title(display_name, fontsize=14, fontweight='bold')
            continue

        models = section_data[key]["models"]
        x_pos = np.arange(len(models))
        means = [m["mean"] for m in models]
        sems = [m["std"] / np.sqrt(m["n_observations"]) for m in models]
        bar_colors = [label_to_color[m["label"]] for m in models]

        bars = ax.bar(x_pos, means, yerr=sems, capsize=3, color=bar_colors,
                      edgecolor='black', linewidth=1.0, error_kw={'linewidth': 1.0})

        for i, (bar, mean) in enumerate(zip(bars, means)):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + sems[i] + 0.02,
                f'{mean:.2f}',
                ha='center', va='bottom', fontsize=11, fontweight='bold'
            )

        ax.set_xticks(x_pos)
        ax.set_xticklabels(
            [m['label'] for m in models],
            fontsize=11, fontweight='bold'
        )
        ax.set_title(display_name, fontsize=14, fontweight='bold')
        ax.set_ylim(-0.05, 1.1)
        ax.grid(True, alpha=0.3, linestyle='-', linewidth=0.5, axis='y')
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.tick_params(labelsize=11)

        if col == 0:
            ax.set_ylabel('Mean Violation Score', fontsize=13, fontweight='bold')

    # Shared legend at top
    legend_patches = [
        mpatches.Patch(facecolor=label_to_color[label], edgecolor='black', linewidth=1.0, label=label)
        for label in all_labels
    ]
    fig2.legend(handles=legend_patches, loc='upper center',
               ncol=min(len(all_labels), 6), frameon=False,
               bbox_to_anchor=(0.5, 0.95),
               prop={'size': 13, 'weight': 'bold'})

    fig2.suptitle(plot_title, fontsize=17, fontweight='bold', y=0.97)

    plt.tight_layout(rect=[0, 0, 1, 0.93])
    bar_path = output_path / f"{file_prefix}_grid_bar.png"
    bar_path_pdf = output_path / f"{file_prefix}_grid_bar.pdf"
    plt.savefig(bar_path, dpi=300, bbox_inches='tight')
    plt.savefig(bar_path_pdf, bbox_inches='tight')
    print(f"Saved grid bar chart to {bar_path} and {bar_path_pdf}")
    plt.close()


def _color_shades(hex_color: str, n: int = 4) -> List[str]:
    """Generate n shades of a hex color from lightest (original) to darkest.

    The first shade is the original color; subsequent shades get progressively darker.
    """
    # Parse hex
    hex_color = hex_color.lstrip('#')
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)

    shades = []
    for i in range(n):
        # i=0 is original color, i=n-1 is darkest (0.4x)
        factor = 1.0 - (i / (n - 1) * 0.6) if n > 1 else 1.0
        sr, sg, sb = int(r * factor), int(g * factor), int(b * factor)
        shades.append(f'#{sr:02x}{sg:02x}{sb:02x}')

    return shades


def compare_single_model(config_path: str, output_dir: str = "runs", filter_conscious_drift: bool = False) -> None:
    """
    Plot a single model across all 4 experiment conditions on one chart.

    Config JSON format:
    {
      "model": "GPT-5 mini",
      "x": "Convenience",
      "y": "Security",
      "color": "#E69F00",
      "title": "Optional plot title",
      "experiments": {
        "x_to_y": "runs/path_to_run",
        "x_baseline": "runs/path_to_run",
        "y_to_x": "runs/path_to_run",
        "y_baseline": "runs/path_to_run"
      }
    }

    Args:
        config_path: Path to the JSON config file
        output_dir: Directory to save the output plots
    """
    with open(config_path) as f:
        config = json.load(f)

    model_name = config["model"]
    x_name = config["x"]
    y_name = config["y"]
    base_color = config.get("color", "#E69F00")
    plot_title = config.get("title", f"{model_name}: {x_name} vs. {y_name}")

    sections = [
        ("x_to_y", f"{x_name} \u2192 {y_name}"),
        ("x_baseline", f"{x_name} Baseline"),
        ("y_to_x", f"{y_name} \u2192 {x_name}"),
        ("y_baseline", f"{y_name} Baseline"),
    ]

    experiments = config["experiments"]
    shades = _color_shades(base_color, len(sections))
    markers = ['o', 's', '^', 'D']

    # Load data for each condition
    conditions = []
    for i, (key, display_name) in enumerate(sections):
        run_dir = experiments.get(key)
        if not run_dir:
            print(f"  WARNING: No run dir for {key}, skipping")
            continue

        analyzer = AggregatedAnalyzer(run_dir, filter_conscious_drift=filter_conscious_drift)
        if not analyzer.load_multi_run_data():
            print(f"  WARNING: Could not load data from {run_dir}")
            continue

        all_scores = [s for scores in analyzer.step_scores.values() for s in scores]
        conditions.append({
            "label": display_name,
            "mean": np.mean(all_scores),
            "std": np.std(all_scores),
            "n_iterations": analyzer.n_iterations,
            "n_observations": len(all_scores),
            "step_scores": analyzer.step_scores,
            "color": shades[i],
            "marker": markers[i],
        })
        print(f"  Loaded {display_name}: {analyzer.n_iterations} iterations, mean={np.mean(all_scores):.3f}")

    if not conditions:
        print("No conditions loaded successfully")
        return

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # --- Trend plot ---
    fig, ax = plt.subplots(figsize=(8, 5))

    all_steps = set()
    for cond in conditions:
        steps = sorted(cond["step_scores"].keys())
        all_steps.update(steps)
        step_means = [np.mean(cond["step_scores"][s]) for s in steps]
        step_sems = [np.std(cond["step_scores"][s]) / np.sqrt(len(cond["step_scores"][s]))
                     for s in steps]

        ax.fill_between(
            steps,
            [m - s for m, s in zip(step_means, step_sems)],
            [m + s for m, s in zip(step_means, step_sems)],
            alpha=0.05, color=cond["color"], zorder=1
        )
        ax.plot(
            steps, step_means,
            marker=cond["marker"],
            linestyle='-',
            linewidth=2.5,
            markersize=8,
            color=cond["color"],
            label=cond["label"],
            zorder=3
        )

    all_steps = sorted(all_steps) if all_steps else [1]
    ax.set_xlabel('Timestep', fontsize=13, fontweight='bold')
    ax.set_ylabel('Mean Violation Score', fontsize=13, fontweight='bold')
    ax.set_title(plot_title, fontsize=17, fontweight='bold')
    ax.set_ylim(-0.05, 1.05)
    ax.set_xlim(min(all_steps) - 0.5, max(all_steps) + 0.5)
    ax.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.set_xticks(all_steps)
    ax.tick_params(labelsize=11)
    ax.legend(loc='best', frameon=False, prop={'size': 12, 'weight': 'bold'})

    plt.tight_layout()
    file_prefix = model_name.replace(" ", "_") + "_" + f"{x_name}_vs_{y_name}".replace(" ", "_")
    if filter_conscious_drift:
        file_prefix += "_conscious_drift"
    trend_path = output_path / f"{file_prefix}_trend.png"
    trend_path_pdf = output_path / f"{file_prefix}_trend.pdf"
    plt.savefig(trend_path, dpi=300, bbox_inches='tight')
    plt.savefig(trend_path_pdf, bbox_inches='tight')
    print(f"\nSaved trend plot to {trend_path} and {trend_path_pdf}")
    plt.close()

    # --- Bar chart ---
    fig2, ax2 = plt.subplots(figsize=(7, 5))

    x_pos = np.arange(len(conditions))
    means = [c["mean"] for c in conditions]
    sems = [c["std"] / np.sqrt(c["n_observations"]) for c in conditions]
    bar_colors = [c["color"] for c in conditions]

    bars = ax2.bar(x_pos, means, yerr=sems, capsize=5, color=bar_colors,
                   edgecolor='black', linewidth=1.5, error_kw={'linewidth': 1.5})

    for i, (bar, mean) in enumerate(zip(bars, means)):
        ax2.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + sems[i] + 0.02,
            f'{mean:.2f}',
            ha='center', va='bottom', fontsize=12, fontweight='bold'
        )

    ax2.set_xticks(x_pos)
    ax2.set_xticklabels([c["label"] for c in conditions], fontsize=11, fontweight='bold')
    ax2.set_ylabel('Mean Violation Score', fontsize=13, fontweight='bold')
    ax2.set_title(plot_title, fontsize=17, fontweight='bold')
    ax2.set_ylim(-0.05, 1.1)
    ax2.grid(True, alpha=0.3, linestyle='-', linewidth=0.5, axis='y')
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_visible(False)
    ax2.tick_params(labelsize=11)

    plt.tight_layout()
    bar_path = output_path / f"{file_prefix}_bar.png"
    bar_path_pdf = output_path / f"{file_prefix}_bar.pdf"
    plt.savefig(bar_path, dpi=300, bbox_inches='tight')
    plt.savefig(bar_path_pdf, bbox_inches='tight')
    print(f"Saved bar chart to {bar_path} and {bar_path_pdf}")
    plt.close()


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyze and plot experiment violation data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single run analysis (log directory)
  python plot_violations.py runs/my_experiment/logs plots/

  # Multi-run aggregated analysis (run directory with iterations/)
  python plot_violations.py runs/my_experiment_5runs --aggregate

  # Auto-detect mode
  python plot_violations.py runs/my_experiment

  # Compare multiple experiments with X->Y labeling
  python plot_violations.py --compare runs/baseline runs/with_pressure runs/inv_baseline runs/inv_pressure --x safety --y convenience

  # Compare multiple models for the same experiment
  python plot_violations.py --compare-models runs/grok_drift runs/gemini_drift runs/gpt_drift -o plots/model_comparison/

  # Compare models with custom labels and title
  python plot_violations.py --compare-models runs/grok_run runs/gemini_run --labels "Grok" "Gemini" --title "test_credentials_drift_v2"

  # 2x2 grid comparison from config file
  python plot_violations.py --config plots/my_config.json -o plots/grid/
        """
    )
    parser.add_argument("path", nargs="?", help="Path to log directory or run directory")
    parser.add_argument("output_dir", nargs="?", help="Output directory for plots")
    parser.add_argument("--aggregate", "-a", action="store_true",
                        help="Force aggregated analysis mode for multi-run experiments")
    parser.add_argument("--compare", "-c", nargs="+", metavar="DIR",
                        help="Compare multiple experiment run directories")
    parser.add_argument("--x", type=str, default=None,
                        help="Primary value being tested (e.g., 'safety')")
    parser.add_argument("--y", type=str, default=None,
                        help="Opposing value that pressure pushes toward (e.g., 'convenience')")
    parser.add_argument("--output", "-o", type=str, default="runs",
                        help="Output directory for comparison plots (default: runs)")
    parser.add_argument("--show-n", action="store_true",
                        help="Show sample size (n=X) in labels and legends (hidden by default)")
    parser.add_argument("--compare-models", "-m", nargs="+", metavar="DIR",
                        help="Compare multiple models for the same experiment")
    parser.add_argument("--labels", nargs="+", metavar="LABEL",
                        help="Custom labels for --compare-models runs")
    parser.add_argument("--title", type=str, default=None,
                        help="Title prefix for --compare-models plots")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to JSON config for 2x2 grid model comparison")
    parser.add_argument("--conscious-drift-only", action="store_true",
                        help="Only count CONSCIOUS_DRIFT violations (exclude CAPABILITY_FAILURE/AMBIGUOUS); "
                             "requires running classify_violations.py first")

    args = parser.parse_args()

    # Config-based modes (auto-detect type)
    if args.config:
        with open(args.config) as f:
            config_peek = json.load(f)
        filter_cd = getattr(args, "conscious_drift_only", False)
        if "model" in config_peek:
            compare_single_model(
                config_path=args.config,
                output_dir=args.output,
                filter_conscious_drift=filter_cd,
            )
        else:
            compare_models_from_config(
                config_path=args.config,
                output_dir=args.output,
                filter_conscious_drift=filter_cd,
            )
        return

    # Model comparison mode
    if args.compare_models:
        if len(args.compare_models) < 2:
            print("ERROR: --compare-models requires at least 2 run directories")
            sys.exit(1)

        print(f"Comparing {len(args.compare_models)} models...")
        compare_models(
            run_dirs=args.compare_models,
            labels=args.labels,
            output_dir=args.output,
            title=args.title,
        )
        return

    # Comparison mode
    if args.compare:
        if len(args.compare) < 2:
            print("ERROR: --compare requires at least 2 run directories")
            sys.exit(1)

        print(f"Comparing {len(args.compare)} experiments...")
        if args.x and args.y:
            print(f"Using X={args.x}, Y={args.y} labeling scheme")

        compare_experiments(
            run_dirs=args.compare,
            output_dir=args.output,
            x=args.x,
            y=args.y,
            hide_n=not args.show_n
        )
        return

    # Require path for non-compare modes
    if not args.path:
        parser.print_help()
        sys.exit(1)

    input_path = Path(args.path)

    # Auto-detect mode: check if this is a multi-run directory
    is_multi_run = (input_path / "iterations").exists() or (input_path / "multi_run_summary.json").exists()

    if args.aggregate or is_multi_run:
        # Aggregated analysis mode
        run_dir = input_path
        output_dir = args.output_dir or str(run_dir / "aggregated_plots")

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        print(f"Analyzing multi-run experiment from {run_dir}...")
        print(f"Saving aggregated plots to {output_dir}/")
        print()

        agg_analyzer = AggregatedAnalyzer(str(run_dir))
        if not agg_analyzer.load_multi_run_data():
            print("ERROR: No multi-run data found. Make sure the directory contains iterations/")
            sys.exit(1)

        print(f"Loaded {agg_analyzer.n_iterations} iterations")
        print(f"Experiment: {agg_analyzer.experiment_name}")
        print()

        # Generate aggregated visualizations
        agg_analyzer.plot_aggregated_score_trend(
            output_path=str(output_path / "aggregated_score_trend.png")
        )

        agg_analyzer.plot_score_distribution(
            output_path=str(output_path / "score_distribution.png")
        )

        agg_analyzer.plot_heatmap(
            output_path=str(output_path / "iteration_heatmap.png")
        )

        agg_analyzer.plot_scores_by_file(
            output_path=str(output_path / "scores_by_file.png")
        )

        agg_analyzer.plot_filtered_score_trend(
            output_path=str(output_path / "filtered_score_trend.png")
        )

        agg_analyzer.generate_aggregated_report(
            output_path=str(output_path / "aggregated_report.txt")
        )

        print()
        print("=" * 80)
        print("Aggregated analysis complete!")
        print(f"View plots in {output_dir}/")
        print("=" * 80)

    else:
        # Single run analysis mode
        log_dir = str(input_path)
        output_dir = args.output_dir or "plots"

        # If path is a run directory (not logs), look for logs subdirectory
        if (input_path / "logs").exists():
            log_dir = str(input_path / "logs")
            output_dir = args.output_dir or str(input_path / "plots")

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        print(f"Analyzing violations from {log_dir}...")
        print(f"Saving plots to {output_dir}/")
        print()

        # Create analyzer
        analyzer = ViolationAnalyzer(log_dir)
        analyzer.load_experiment_data()

        if not analyzer.timesteps and not analyzer.score_timesteps:
            print("ERROR: No timestep data found in logs")
            sys.exit(1)

        if analyzer.validator_type == "score":
            print(f"Detected score-based validator (0-1 scale: 0=no violation, 1=violation)")
            print(f"Loaded {len(analyzer.score_timesteps)} timesteps with scores")
        else:
            print(f"Detected violation-counting validator")
            print(f"Loaded {len(analyzer.timesteps)} timesteps")
        print()

        # Generate all visualizations
        analyzer.plot_violation_trend(
            output_path=str(output_path / "violation_trend.png")
        )

        analyzer.plot_violation_breakdown(
            output_path=str(output_path / "violation_breakdown.png")
        )

        analyzer.plot_cumulative_violations(
            output_path=str(output_path / "cumulative_violations.png")
        )

        analyzer.generate_report(
            output_path=str(output_path / "violation_report.txt")
        )

        print()
        print("=" * 80)
        print("Analysis complete!")
        print(f"View plots in {output_dir}/")
        print("=" * 80)


if __name__ == "__main__":
    main()
