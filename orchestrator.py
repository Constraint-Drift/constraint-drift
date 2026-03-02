#!/usr/bin/env python3
"""
Constraint Drift Experiment Orchestrator

Orchestrator supporting flexible experiment designs with:
- Independent, shufflable queries
- Pooled patches with severity-based assignment
- Patch prepending to target files
- Seed-based reproducibility
"""

import json
import os
import random
import signal
import subprocess
import sys
import shutil
import importlib.util
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

import dotenv

# Load environment variables
dotenv.load_dotenv()


def _parse_bool_env(var_name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    value = os.getenv(var_name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _find_experiment_dir(base_dir: Path, experiment_name: str) -> Path:
    """Find experiment directory, searching subdirectories if needed.

    Supports both flat layout (experiments/<name>/) and grouped layout
    (experiments/<group>/<name>/).
    """
    # Try direct path first
    direct = base_dir / "experiments" / experiment_name
    if direct.is_dir():
        return direct

    # Search one level of subdirectories
    experiments_dir = base_dir / "experiments"
    if experiments_dir.is_dir():
        for group_dir in sorted(experiments_dir.iterdir()):
            candidate = group_dir / experiment_name
            if candidate.is_dir() and (candidate / "config.json").exists():
                return candidate

    raise FileNotFoundError(
        f"Experiment '{experiment_name}' not found under {experiments_dir}"
    )


def _load_config_seed(base_dir: Path, experiment_name: str) -> Optional[int]:
    """Load execution.random_seed from experiment config if present."""
    try:
        experiment_dir = _find_experiment_dir(base_dir, experiment_name)
    except FileNotFoundError:
        return None
    config_path = experiment_dir / "config.json"
    if not config_path.exists():
        return None

    with open(config_path) as f:
        config = json.load(f)

    return config.get("execution", {}).get("random_seed")


def _resolve_seed(
    explicit_seed: Optional[int],
    config_seed: Optional[int],
    deterministic: bool
) -> Tuple[int, str]:
    """Resolve effective seed and record where it came from."""
    if explicit_seed is not None:
        return explicit_seed, "explicit"

    if config_seed is not None:
        return config_seed, "experiment_config"

    if deterministic:
        return 42, "deterministic_default"

    return int(datetime.now().timestamp()), "timestamp"


def get_tool_versions() -> Dict[str, str]:
    """Get versions of key tools for reproducibility logging."""
    versions = {
        "python": sys.version.split()[0],
    }

    # Get opencode version
    try:
        result = subprocess.run(
            ["opencode", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            versions["opencode"] = result.stdout.strip()
        else:
            versions["opencode"] = "unknown"
    except Exception:
        versions["opencode"] = "not found"

    return versions


@dataclass
class Query:
    """Represents an independent query in the experiment."""
    id: str
    message: str
    target_file: str


@dataclass
class Patch:
    """Represents a patch in the pool."""
    id: str
    file: str
    severity: int


@dataclass
class TimestepLog:
    """Log entry for a single timestep."""
    step: int
    timestamp: str
    query_id: str
    message: str
    target_file: str
    patch_applied: Optional[Dict] = None
    response: Dict = None
    files_modified: List[str] = None
    agent_diffs: Optional[List[Dict]] = None
    validation_result: Optional[Dict] = None
    pre_agent_file_content: Optional[str] = None  # File content after patch but before agent


def cleanup_stale_run_directories(base_dir: Path, run_name: str) -> List[str]:
    """Remove stale directories that could catch misplaced agent writes.

    When agents construct relative paths incorrectly, they may write to directories
    with partial run names (e.g., "experiment_10runs" instead of "experiment_10runs_p10_timestamp").
    This function identifies and removes such directories to prevent cross-contamination.

    Args:
        base_dir: Base directory of the project
        run_name: The full run name being used for current execution

    Returns:
        List of directory names that were removed
    """
    runs_dir = base_dir / "runs"
    if not runs_dir.exists():
        return []

    removed = []

    # Find potential partial names that could catch misplaced writes
    # Example: run_name = "test_credentials_drift_v2_10runs_p10_20260204_205404"
    # Stale partial: "test_credentials_drift_v2_10runs" (without _p10_timestamp)
    # Look for directories that are prefixes of our run_name but not the exact name
    for item in runs_dir.iterdir():
        if not item.is_dir():
            continue

        item_name = item.name

        # Skip if it's our exact run name
        if item_name == run_name:
            continue

        # Check if this directory name is a prefix of our run name
        # AND if our run name starts with this directory name followed by more characters
        if run_name.startswith(item_name + "_"):
            # This is a potential partial-name directory that could catch misplaced writes
            # Only remove if it was created recently (within last day) to avoid removing old data
            # OR if it contains nested run directories
            try:
                # Check if this directory contains nested run-like directories
                nested_runs = list(item.glob("*_20*"))  # Matches timestamp patterns
                if nested_runs:
                    print(f"[CLEANUP] Removing stale directory with nested runs: {item_name}")
                    shutil.rmtree(item)
                    removed.append(item_name)
            except Exception as e:
                print(f"[CLEANUP] Warning: Could not check/remove {item_name}: {e}")

    return removed


def init_workspace(
    workspace_dir: Path,
    base_dir: Path,
    base_repo: str,
    experiment_dir: Path,
    constraint_file: str,
    quiet: bool = False
) -> None:
    """Initialize a workspace with base repo and constraint file.

    This is a standalone function that can be used for parallel worker initialization.

    Args:
        workspace_dir: Directory to initialize as workspace
        base_dir: Base directory of the project (where base-repos/ lives)
        base_repo: Name of the base repo to copy
        experiment_dir: Directory containing the experiment
        constraint_file: Name of the constraint file (e.g., AGENTS.md)
        quiet: If True, suppress print statements
    """
    # Clean up existing workspace to ensure fresh state
    if workspace_dir.exists():
        if not quiet:
            print(f"Cleaning existing workspace at {workspace_dir}...")
        shutil.rmtree(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Copy base repo to workspace if specified
    if base_repo:
        base_repo_path = base_dir / "base-repos" / base_repo
        if base_repo_path.exists():
            if not quiet:
                print(f"Copying base repo '{base_repo}' to workspace...")
            for item in base_repo_path.iterdir():
                if item.is_file():
                    shutil.copy2(item, workspace_dir / item.name)
                elif item.is_dir() and item.name != '.git':
                    shutil.copytree(item, workspace_dir / item.name, dirs_exist_ok=True)

    # Copy constraint file to workspace
    if constraint_file:
        constraint_source = experiment_dir / constraint_file
        if constraint_source.exists():
            if not quiet:
                print(f"Copying {constraint_file} to workspace...")
            shutil.copy2(constraint_source, workspace_dir / constraint_file)

    # Initialize git if not already a repo
    git_dir = workspace_dir / ".git"
    if not git_dir.exists():
        subprocess.run(
            ["git", "init"],
            cwd=workspace_dir,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "experiment@opencode.local"],
            cwd=workspace_dir,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Experiment Bot"],
            cwd=workspace_dir,
            check=True,
            capture_output=True
        )
        # Initial commit
        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace_dir,
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial workspace setup"],
            cwd=workspace_dir,
            capture_output=True
        )


# =============================================================================
# Parallel Execution Infrastructure
# =============================================================================

@dataclass
class WorkerContext:
    """Context for a parallel worker."""
    worker_id: int
    workspace_dir: Path
    base_dir: Path
    experiment_name: str
    experiment_dir: Path
    run_name: str
    model: str
    max_steps: Optional[int]
    base_seed: int
    base_repo: str
    constraint_file: str
    deterministic: bool

    def get_iteration_seed(self, iteration: int) -> int:
        """Compute deterministic seed for an iteration."""
        return self.base_seed + iteration - 1


def create_parallel_workspaces(
    run_dir: Path,
    n_workers: int,
    base_dir: Path,
    base_repo: str,
    experiment_dir: Path,
    constraint_file: str
) -> List[Path]:
    """Create isolated workspaces for parallel workers.

    DEPRECATED: Use create_iteration_workspaces instead for proper isolation.

    Args:
        run_dir: The run directory (e.g., runs/experiment_name_timestamp/)
        n_workers: Number of worker workspaces to create
        base_dir: Base directory of the project
        base_repo: Name of the base repo to copy
        experiment_dir: Directory containing the experiment
        constraint_file: Name of the constraint file

    Returns:
        List of workspace paths, one per worker
    """
    workspaces_dir = run_dir / "workspaces"
    workspaces_dir.mkdir(parents=True, exist_ok=True)

    workspace_paths = []
    for i in range(1, n_workers + 1):
        worker_dir = workspaces_dir / f"worker_{i:03d}"
        print(f"[Parallel] Initializing workspace for worker {i}/{n_workers}...")
        init_workspace(
            workspace_dir=worker_dir,
            base_dir=base_dir,
            base_repo=base_repo,
            experiment_dir=experiment_dir,
            constraint_file=constraint_file,
            quiet=True
        )
        workspace_paths.append(worker_dir)

    return workspace_paths


def create_iteration_workspaces(
    run_dir: Path,
    n_iterations: int,
    base_dir: Path,
    base_repo: str,
    experiment_dir: Path,
    constraint_file: str
) -> List[Path]:
    """Create one isolated workspace per iteration.

    This provides proper isolation by giving each iteration its own workspace,
    avoiding race conditions when multiple iterations run in parallel.

    Args:
        run_dir: The run directory (e.g., runs/experiment_name_timestamp/)
        n_iterations: Number of iterations to create workspaces for
        base_dir: Base directory of the project
        base_repo: Name of the base repo to copy
        experiment_dir: Directory containing the experiment
        constraint_file: Name of the constraint file

    Returns:
        List of workspace paths, one per iteration
    """
    workspaces_dir = run_dir / "workspaces"
    workspaces_dir.mkdir(parents=True, exist_ok=True)

    workspace_paths = []
    for i in range(1, n_iterations + 1):
        iter_dir = workspaces_dir / f"iter_{i:03d}"
        print(f"[Parallel] Initializing workspace for iteration {i}/{n_iterations}...")
        init_workspace(
            workspace_dir=iter_dir,
            base_dir=base_dir,
            base_repo=base_repo,
            experiment_dir=experiment_dir,
            constraint_file=constraint_file,
            quiet=True
        )
        workspace_paths.append(iter_dir)

    return workspace_paths


def reset_workspace_for_iteration(
    workspace_dir: Path,
    base_dir: Path,
    base_repo: str,
    experiment_dir: Path,
    constraint_file: str
) -> None:
    """Reset workspace to clean state between iterations.

    This preserves the git directory but resets all files to initial state.

    Args:
        workspace_dir: Workspace directory to reset
        base_dir: Base directory of the project
        base_repo: Name of the base repo
        experiment_dir: Directory containing the experiment
        constraint_file: Name of the constraint file
    """
    # Remove all files except .git
    for item in workspace_dir.iterdir():
        if item.name != '.git':
            if item.is_file():
                item.unlink()
            else:
                shutil.rmtree(item)

    # Re-copy base repo
    if base_repo:
        base_repo_path = base_dir / "base-repos" / base_repo
        if base_repo_path.exists():
            for item in base_repo_path.iterdir():
                if item.is_file():
                    shutil.copy2(item, workspace_dir / item.name)
                elif item.is_dir() and item.name != '.git':
                    shutil.copytree(item, workspace_dir / item.name, dirs_exist_ok=True)

    # Re-copy constraint file
    if constraint_file:
        constraint_source = experiment_dir / constraint_file
        if constraint_source.exists():
            shutil.copy2(constraint_source, workspace_dir / constraint_file)

    # NOTE: We intentionally do NOT run `git checkout .` or `git clean -fd` here.
    # Those commands would restore files to the last committed state, which may include
    # patches from previous iterations. Since we've already copied fresh files from the
    # base repo above, the workspace is now in the correct clean state.


def _worker_run_iteration(args: Tuple) -> Dict:
    """Worker function for parallel execution.

    This is a top-level function (not a method) so it can be pickled by ProcessPoolExecutor.

    Args:
        args: Tuple of (worker_context, iteration_num, total_iterations)

    Returns:
        Dict with iteration results including success status, iteration number, and worker ID
    """
    worker_context, iteration, total_iterations = args

    try:
        # NOTE: No workspace reset needed - each iteration has its own dedicated
        # workspace created by create_iteration_workspaces(), avoiding race conditions

        # Create orchestrator with pre-initialized workspace
        orchestrator = OpenCodeOrchestratorV2(
            experiment_name=worker_context.experiment_name,
            working_dir=str(worker_context.workspace_dir),
            model=worker_context.model,
            run_name=worker_context.run_name,
            max_steps=worker_context.max_steps,
            random_seed=worker_context.get_iteration_seed(iteration),
            base_dir=str(worker_context.base_dir),
            iteration=iteration,
            total_iterations=total_iterations,
            skip_workspace_init=True,  # Workspace already initialized
            deterministic=worker_context.deterministic
        )

        orchestrator.run_experiment()

        return {
            "success": True,
            "iteration": iteration,
            "worker_id": worker_context.worker_id
        }

    except Exception as e:
        return {
            "success": False,
            "iteration": iteration,
            "worker_id": worker_context.worker_id,
            "error": str(e),
            "traceback": traceback.format_exc()
        }


class OpenCodeOrchestratorV2:
    """Orchestrates flexible OpenCode experiments with shufflable queries and patches."""

    def __init__(
        self,
        experiment_name: str,
        working_dir: str = None,
        model: str = None,
        run_name: str = None,
        max_steps: int = None,
        random_seed: int = None,
        base_dir: str = None,
        iteration: int = None,
        total_iterations: int = None,
        skip_workspace_init: bool = False,
        deterministic: bool = False
    ):
        # Determine base directory (where orchestrator.py lives)
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).parent

        self.experiment_name = experiment_name
        self.experiment_dir = _find_experiment_dir(self.base_dir, experiment_name)
        self.config_path = self.experiment_dir / "config.json"

        # Default working dir is workspace/repo relative to base_dir
        if working_dir is None:
            working_dir = self.base_dir / "workspace" / "repo"
        self.working_dir = Path(working_dir)

        self.model = model or os.getenv("OPENCODE_MODEL", "openrouter/anthropic/claude-3.5-sonnet")

        # Multi-iteration support
        self.iteration = iteration
        self.total_iterations = total_iterations
        self.deterministic = deterministic

        # Generate run name if not provided
        if run_name is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_name = f"{experiment_name}_{timestamp}"

        self.run_name = run_name
        self.run_dir = self.base_dir / "runs" / run_name

        # For multi-iteration runs, use iteration subdirectories
        if iteration is not None:
            self.log_dir = self.run_dir / "iterations" / f"iter_{iteration:03d}" / "logs"
            self.plots_dir = self.run_dir / "iterations" / f"iter_{iteration:03d}" / "plots"
        else:
            self.log_dir = self.run_dir / "logs"
            self.plots_dir = self.run_dir / "plots"

        # Create run directories
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.plots_dir.mkdir(parents=True, exist_ok=True)

        # Load experiment configuration
        with open(self.config_path) as f:
            self.config = json.load(f)

        self.experiment_id = self.config.get("experiment_id", experiment_name)
        self.base_repo = self.config.get("base_repo")
        self.max_steps = max_steps
        self.constraint_file = self.config.get("constraint_file")
        self.validation_config = self.config.get("validation", {})

        # Parse queries and patches
        self.queries = [Query(**q) for q in self.config["queries"]]
        self.patch_pool = [Patch(**p) for p in self.config.get("patch_pool", [])]

        # Execution config
        self.execution_config = self.config.get("execution", {})
        self.shuffle_queries = self.execution_config.get("shuffle_queries", False)
        self.patch_assignment = self.execution_config.get("patch_assignment", "sequential_severity")
        self.persist_session = self.execution_config.get("persist_session", True)
        self.session_id = None  # Will be set on first step if persist_session is True
        configured_max_retries = self.execution_config.get("max_retries")
        self.max_retries = configured_max_retries if configured_max_retries is not None else (0 if self.deterministic else 5)
        configured_retry_delay = self.execution_config.get("retry_delay")
        self.retry_delay = configured_retry_delay if configured_retry_delay is not None else (0 if self.max_retries == 0 else 5)

        # Set random seed for reproducibility
        config_seed = self.execution_config.get("random_seed")
        self.random_seed, self.random_seed_source = _resolve_seed(
            explicit_seed=random_seed,
            config_seed=config_seed,
            deterministic=self.deterministic
        )
        self.rng = random.Random(self.random_seed)

        # Prepare execution order
        self.execution_queries, self.execution_patches = self._prepare_execution_order()

        # Initialize workspace (unless pre-initialized by parallel worker manager)
        if not skip_workspace_init:
            self._init_workspace()

    def _prepare_execution_order(self) -> Tuple[List[Query], List[Patch]]:
        """Prepare the execution order for queries and patches."""
        queries = self.queries.copy()
        patches = self.patch_pool.copy()

        # Shuffle queries if configured
        if self.shuffle_queries:
            self.rng.shuffle(queries)

        # Sort patches by severity for sequential assignment
        if self.patch_assignment == "sequential_severity":
            patches.sort(key=lambda p: p.severity)
        elif self.patch_assignment == "tiered_severity":
            # Group patches by severity tier
            severity_groups = {}
            for p in patches:
                severity_groups.setdefault(p.severity, []).append(p)

            # Calculate how many queries per tier based on query count and tier count
            num_tiers = len(severity_groups)
            num_queries = len(queries)
            queries_per_tier = num_queries // num_tiers if num_tiers > 0 else 0
            remainder = num_queries % num_tiers if num_tiers > 0 else 0

            selected_patches = []
            for i, severity in enumerate(sorted(severity_groups.keys())):
                group = sorted(severity_groups[severity], key=lambda p: p.id)
                # How many patches needed for this tier
                count = queries_per_tier + (1 if i < remainder else 0)
                # Randomly sample (with replacement if needed) from this tier
                for _ in range(count):
                    selected_patches.append(self.rng.choice(group))

            patches = selected_patches

        return queries, patches

    def _init_workspace(self):
        """Initialize the workspace repository from base repo."""
        init_workspace(
            workspace_dir=self.working_dir,
            base_dir=self.base_dir,
            base_repo=self.base_repo,
            experiment_dir=self.experiment_dir,
            constraint_file=self.constraint_file
        )

    def apply_patch_to_file(self, patch: Patch, target_file: str, step_num: int) -> Dict:
        """
        Prepend patch content to the target file.

        Args:
            patch: The patch to apply
            target_file: File to prepend patch to
            step_num: Current step number

        Returns:
            Dictionary with patch application results
        """
        patch_path = self.experiment_dir / "patches" / patch.file
        target_path = self.working_dir / target_file

        if not patch_path.exists():
            return {
                "success": False,
                "error": f"Patch file not found: {patch_path}"
            }

        if not target_path.exists():
            return {
                "success": False,
                "error": f"Target file not found: {target_path}"
            }

        print(f"[Step {step_num}] Applying patch '{patch.id}' (severity {patch.severity}) to {target_file}")

        try:
            # Read patch content and original file
            patch_content = patch_path.read_text()
            original_content = target_path.read_text()

            # Prepend patch content to file
            new_content = patch_content + original_content
            target_path.write_text(new_content)

            # NOTE: We intentionally do NOT commit the patch to git.
            # The patch modifies the file content, but committing it would pollute
            # the git history and cause issues with workspace resets between iterations.
            # The pre_agent_file_content captures the state after patching for validation.

            return {
                "success": True,
                "patch_id": patch.id,
                "patch_file": patch.file,
                "severity": patch.severity,
                "target_file": target_file,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Error applying patch: {str(e)}",
                "patch_id": patch.id
            }

    def _execute_opencode_once(self, cmd: List[str], step_num: int) -> Dict:
        """Execute OpenCode once and return results."""
        try:
            env = os.environ.copy()

            result = subprocess.run(
                cmd,
                cwd=str(self.working_dir),
                capture_output=True,
                text=True,
                timeout=240,  # 4 minutes
                env=env
            )

            if result.returncode == 0:
                json_events = []
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        try:
                            event = json.loads(line)
                            json_events.append(event)
                            # Capture session ID from first response if persist_session enabled
                            # Session ID is in the 'sessionID' field of events (e.g., step_start, tool_use)
                            if self.persist_session and not self.session_id:
                                session_id = event.get("sessionID")
                                if session_id:
                                    self.session_id = session_id
                                    print(f"[Step {step_num}] Captured session ID: {self.session_id}")
                        except json.JSONDecodeError:
                            pass

                return {
                    "success": True,
                    "data": {
                        "events": json_events,
                        "message": cmd[-1],  # message is last arg
                        "raw_output": result.stdout,
                        "session_id": self.session_id
                    },
                    "stdout": result.stdout,
                    "stderr": result.stderr
                }
            else:
                return {
                    "success": False,
                    "error": f"OpenCode returned non-zero exit code: {result.returncode}",
                    "stdout": result.stdout,
                    "stderr": result.stderr
                }

        except subprocess.TimeoutExpired as e:
            return {
                "success": False,
                "error": "OpenCode execution timed out after 4 minutes",
                "stdout": e.stdout.decode() if e.stdout else "",
                "stderr": e.stderr.decode() if e.stderr else ""
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }

    def run_opencode_step(self, message: str, step_num: int) -> Dict:
        """
        Execute a single OpenCode interaction with retry logic.

        Args:
            message: The user message to send to OpenCode
            step_num: The current step number

        Returns:
            Parsed response dictionary
        """
        print(f"[Step {step_num}] Executing OpenCode with message: {message[:100]}...")

        # Find opencode binary - check env var, then common locations
        opencode_bin = os.getenv("OPENCODE_PATH")
        if not opencode_bin:
            # Check common installation locations
            common_paths = [
                os.path.expanduser("~/.local/bin/opencode"),
                "/usr/local/bin/opencode",
                "/opt/homebrew/bin/opencode",
            ]
            for path in common_paths:
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    opencode_bin = path
                    break
            else:
                opencode_bin = "opencode"  # Fall back to PATH lookup

        cmd = [
            opencode_bin,
            "run",
            "-m", self.model,
            "--format", "json",
        ]

        # Add session persistence if enabled
        if self.persist_session:
            if self.session_id:
                # Continue existing session
                cmd.extend(["--session", self.session_id])
                print(f"[Step {step_num}] Using existing session: {self.session_id}")
            else:
                print(f"[Step {step_num}] No session ID yet, will capture from response")

        cmd.append(message)

        # Execute with retries
        last_result = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                print(f"[Step {step_num}] Retry {attempt}/{self.max_retries} after {self.retry_delay}s delay...")
                time.sleep(self.retry_delay)

            result = self._execute_opencode_once(cmd, step_num)

            if result.get("success"):
                if attempt > 0:
                    print(f"[Step {step_num}] Succeeded on retry {attempt}")
                return result

            last_result = result
            print(f"[Step {step_num}] Attempt {attempt + 1} failed: {result.get('error', 'Unknown error')}")

        # All retries exhausted
        print(f"[Step {step_num}] All {self.max_retries + 1} attempts failed")
        return last_result

    def get_modified_files(self, response: Dict) -> Tuple[List[str], List[Dict]]:
        """Get list of files modified and their diffs by parsing OpenCode tool events."""
        modified_files = set()
        diffs = []

        if not response.get("success"):
            return [], []

        events = response.get("data", {}).get("events", [])

        for event in events:
            if event.get("type") == "tool_use":
                part = event.get("part", {})
                tool = part.get("tool", "")
                state = part.get("state", {})

                if state.get("status") == "completed" and tool in ["edit", "write", "patch", "apply_patch", "multiedit"]:
                    input_data = state.get("input", {})
                    metadata = state.get("metadata", {})

                    if tool == "apply_patch":
                        # apply_patch stores file info in metadata["files"]
                        for file_info in metadata.get("files", []):
                            file_path = file_info.get("filePath", "")
                            if file_path:
                                relative_path = file_info.get("relativePath", file_path)
                                # Still strip workspace prefix if relativePath isn't available
                                if relative_path == file_path:
                                    workspace_prefix = str(self.working_dir)
                                    if not workspace_prefix.endswith("/"):
                                        workspace_prefix += "/"
                                    if file_path.startswith(workspace_prefix):
                                        relative_path = file_path[len(workspace_prefix):]
                                    elif file_path.startswith("/workspace/repo/"):
                                        relative_path = file_path[len("/workspace/repo/"):]
                                modified_files.add(relative_path)

                                file_diff = file_info.get("diff", "")
                                if file_diff:
                                    # Count additions/deletions from the diff
                                    additions = sum(1 for l in file_diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
                                    deletions = sum(1 for l in file_diff.splitlines() if l.startswith("-") and not l.startswith("---"))
                                    diffs.append({
                                        "file": relative_path,
                                        "tool": tool,
                                        "additions": additions,
                                        "deletions": deletions,
                                        "diff": file_diff
                                    })
                    else:
                        file_path = input_data.get("filePath", "")

                        if file_path:
                            relative_path = file_path
                            # Dynamic path stripping based on actual working_dir
                            workspace_prefix = str(self.working_dir)
                            if not workspace_prefix.endswith("/"):
                                workspace_prefix += "/"
                            if file_path.startswith(workspace_prefix):
                                relative_path = file_path[len(workspace_prefix):]
                            elif file_path.startswith("/workspace/repo/"):
                                # Fallback for legacy path format
                                relative_path = file_path[len("/workspace/repo/"):]
                            modified_files.add(relative_path)

                            if "filediff" in metadata:
                                filediff = metadata["filediff"]
                                diff_entry = {
                                    "file": relative_path,
                                    "tool": tool,
                                    "additions": filediff.get("additions", 0),
                                    "deletions": filediff.get("deletions", 0),
                                    "diff": metadata.get("diff", "")
                                }
                                diffs.append(diff_entry)

        return sorted(list(modified_files)), diffs

    def detect_misplaced_writes(self, response: Dict) -> List[Dict]:
        """Detect files written outside the expected workspace directory.

        Returns list of misplaced writes with source (wrong) and target (correct) paths.
        """
        misplaced = []
        expected_workspace = str(self.working_dir)

        if not response.get("success"):
            return misplaced

        events = response.get("data", {}).get("events", [])

        for event in events:
            if event.get("type") == "tool_use":
                part = event.get("part", {})
                tool = part.get("tool", "")
                state = part.get("state", {})

                if state.get("status") == "completed" and tool in ["edit", "write", "patch", "apply_patch", "multiedit"]:
                    input_data = state.get("input", {})
                    metadata = state.get("metadata", {})

                    if tool == "apply_patch":
                        # apply_patch stores file info in metadata["files"]
                        for file_info in metadata.get("files", []):
                            file_path = file_info.get("filePath", "")
                            if file_path and not file_path.startswith(expected_workspace):
                                filename = Path(file_path).name
                                correct_path = self.working_dir / filename
                                misplaced.append({
                                    "wrong_path": file_path,
                                    "correct_path": str(correct_path),
                                    "filename": filename,
                                    "tool": tool
                                })
                    else:
                        file_path = input_data.get("filePath", "")

                        if file_path and not file_path.startswith(expected_workspace):
                            filename = Path(file_path).name
                            correct_path = self.working_dir / filename
                            misplaced.append({
                                "wrong_path": file_path,
                                "correct_path": str(correct_path),
                                "filename": filename,
                                "tool": tool
                            })

        return misplaced

    def repair_misplaced_writes(self, misplaced: List[Dict]) -> List[str]:
        """Copy files from wrong locations to correct workspace locations.

        Returns list of successfully repaired files.
        """
        repaired = []

        for item in misplaced:
            wrong_path = Path(item["wrong_path"])
            correct_path = Path(item["correct_path"])

            if wrong_path.exists():
                try:
                    # Copy the file to the correct location
                    shutil.copy2(wrong_path, correct_path)
                    repaired.append(item["filename"])
                    print(f"[REPAIR] Copied misplaced file: {item['filename']}")
                    print(f"         From: {wrong_path}")
                    print(f"         To:   {correct_path}")
                except Exception as e:
                    print(f"[REPAIR] Failed to copy {item['filename']}: {e}")
            else:
                print(f"[REPAIR] Misplaced file no longer exists: {wrong_path}")

        return repaired

    def validate_timestep(self, timestep_log: TimestepLog) -> Optional[Dict]:
        """Run validation on a timestep."""
        config_section = self.validation_config.get("config", {})
        enabled = self.validation_config.get("enabled", config_section.get("enabled", False))

        if not enabled:
            return None

        validation_type = self.validation_config.get("type")

        validator_files = {
            "test_credential_validator": "test_credential_validator.py",
            "test_credential_inverted_validator": "test_credential_inverted_validator.py",
            "auth_revalidation_validator": "auth_revalidation_validator.py",
            "auth_revalidation_inverted_validator": "auth_revalidation_inverted_validator.py",
            "accuracy_privacy_validator": "accuracy_privacy_validator.py",
            "accuracy_privacy_inverted_validator": "accuracy_privacy_inverted_validator.py",
        }

        validator_filename = validator_files.get(validation_type)
        if not validator_filename:
            print(f"[WARNING] Unknown validation type: {validation_type}")
            return None

        validator_path = self.experiment_dir / "validators" / validator_filename
        if not validator_path.exists():
            print(f"[WARNING] Validator not found: {validator_path}")
            return None

        try:
            spec = importlib.util.spec_from_file_location("validator", validator_path)
            validator_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(validator_module)

            validator_config = self.validation_config.get("config", {}).copy()
            validator_config["logs_dir"] = str(self.log_dir)
            validator_config["current_step"] = timestep_log.step
            validator_config["base_repo"] = self.base_repo
            validator_config["base_repo_path"] = str(self.base_dir / "base-repos" / self.base_repo) if self.base_repo else None
            validator_config["workspace_dir"] = str(self.working_dir)

            timestep_data = asdict(timestep_log)
            result = validator_module.validate(
                validator_config,
                timestep_data
            )
            return result
        except Exception as e:
            print(f"[ERROR] Validation failed: {e}")
            return None

    def log_timestep(self, log_entry: TimestepLog):
        """Save timestep log to JSON file."""
        log_file = self.log_dir / f"timestep_{log_entry.step:03d}.json"

        with open(log_file, 'w') as f:
            json.dump(asdict(log_entry), f, indent=2)

        print(f"[Step {log_entry.step}] Log saved to {log_file}")

    def run_experiment(self):
        """Execute the full experiment sequence."""
        total_queries = len(self.execution_queries)
        steps_to_run = (
            min(self.max_steps, total_queries)
            if self.max_steps is not None
            else total_queries
        )

        # Log tool versions for reproducibility
        versions = get_tool_versions()
        print(f"Tool versions: Python {versions['python']}, OpenCode {versions['opencode']}")
        print(f"Starting experiment: {self.experiment_id}")
        print(f"Model: {self.model}")
        print(f"Deterministic mode: {self.deterministic}")
        print(f"Random seed: {self.random_seed}")
        print(f"Random seed source: {self.random_seed_source}")
        print(f"Total queries: {total_queries}")
        print(f"Steps to run: {steps_to_run}" + (
            f" (limited by MAX_STEPS={self.max_steps})" if self.max_steps else ""
        ))
        print(f"Working directory: {self.working_dir}")
        print(f"Constraint file: {self.constraint_file}")
        print(f"Shuffle queries: {self.shuffle_queries}")
        print(f"Patch assignment: {self.patch_assignment}")
        print(f"Persist session: {self.persist_session}" + (
            " (agent sees full chat history)" if self.persist_session else " (each step is independent)"
        ))
        print("-" * 80)

        # Print execution order
        print("\nExecution order:")
        for i, query in enumerate(self.execution_queries[:steps_to_run]):
            patch = self.execution_patches[i] if i < len(self.execution_patches) else None
            patch_info = f" + patch '{patch.id}' (severity {patch.severity})" if patch else ""
            print(f"  Step {i+1}: {query.id} -> {query.target_file}{patch_info}")
        print("-" * 80)

        patches_applied = 0

        for step_idx in range(steps_to_run):
            step_num = step_idx + 1
            query = self.execution_queries[step_idx]
            patch = self.execution_patches[step_idx] if step_idx < len(self.execution_patches) else None

            print(f"\n{'='*80}")
            print(f"STEP {step_num}/{steps_to_run}")
            print(f"Query: {query.id}")
            print(f"Target: {query.target_file}")
            print(f"{'='*80}")

            # Apply patch to target file before agent sees it
            patch_result = None
            if patch:
                patch_result = self.apply_patch_to_file(patch, query.target_file, step_num)
                if patch_result.get("success"):
                    patches_applied += 1

            # Capture pre-agent file content (after patch, before agent runs)
            # This is needed for single_step validation to compute what the agent actually added
            pre_agent_content = None
            target_path = self.working_dir / query.target_file
            if target_path.exists():
                try:
                    pre_agent_content = target_path.read_text()
                except Exception as e:
                    print(f"[Step {step_num}] Warning: Could not read pre-agent file content: {e}")

            # Execute OpenCode step
            response = self.run_opencode_step(query.message, step_num)

            # Get modified files and diffs
            files_modified, agent_diffs = self.get_modified_files(response)

            # Detect and repair misplaced writes (files written outside workspace)
            misplaced_writes = self.detect_misplaced_writes(response)
            if misplaced_writes:
                print(f"[Step {step_num}] WARNING: Detected {len(misplaced_writes)} misplaced write(s)")
                repaired = self.repair_misplaced_writes(misplaced_writes)
                if repaired:
                    # Add repaired files to the modified files list
                    files_modified = sorted(set(files_modified) | set(repaired))
                    print(f"[Step {step_num}] Repaired {len(repaired)} misplaced file(s)")

            # Build log entry
            log_entry = TimestepLog(
                step=step_num,
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                query_id=query.id,
                message=query.message,
                target_file=query.target_file,
                response=response,
                files_modified=files_modified,
                agent_diffs=agent_diffs,
                patch_applied=patch_result,
                pre_agent_file_content=pre_agent_content
            )

            # Run validation
            validation_result = self.validate_timestep(log_entry)
            if validation_result:
                log_entry.validation_result = validation_result
                print(f"[Step {step_num}] Validation: {validation_result}")

            # Save log
            self.log_timestep(log_entry)

            # Check for errors
            if not response.get("success"):
                print(f"[WARNING] Step {step_num} encountered errors:")
                print(response.get("error"))

        # Generate summary
        self._generate_summary(patches_applied, steps_to_run)

        print(f"\n{'='*80}")
        print(f"Experiment completed: {self.experiment_id}")
        print(f"Run name: {self.run_name}")
        print(f"Steps completed: {steps_to_run}/{total_queries}")
        print(f"Logs saved to: {self.log_dir}")
        print(f"Plots directory: {self.plots_dir}")
        print(f"\nTo generate plots, run:")
        print(f"  python3 plot_violations.py runs/{self.run_name}/logs runs/{self.run_name}/plots")
        print(f"{'='*80}")

    def _generate_summary(self, patches_applied: int, steps_completed: int):
        """Generate experiment summary."""
        summary = {
            "experiment_id": self.experiment_id,
            "experiment_name": self.experiment_name,
            "experiment_type": "flexible",
            "run_name": self.run_name,
            "tool_versions": get_tool_versions(),
            "model": self.model,
            "deterministic_mode": self.deterministic,
            "random_seed": self.random_seed,
            "random_seed_source": self.random_seed_source,
            "base_repo": self.base_repo,
            "constraint_file": self.constraint_file,
            "total_queries": len(self.queries),
            "steps_completed": steps_completed,
            "max_steps_limit": self.max_steps,
            "patches_applied": patches_applied,
            "shuffle_queries": self.shuffle_queries,
            "patch_assignment": self.patch_assignment,
            "persist_session": self.persist_session,
            "session_id": self.session_id,
            "max_retries": self.max_retries,
            "retry_delay": self.retry_delay,
            "iteration": self.iteration,
            "total_iterations": self.total_iterations,
            "execution_order": [
                {
                    "step": i + 1,
                    "query_id": self.execution_queries[i].id,
                    "target_file": self.execution_queries[i].target_file,
                    "patch_id": self.execution_patches[i].id if i < len(self.execution_patches) else None,
                    "patch_severity": self.execution_patches[i].severity if i < len(self.execution_patches) else None
                }
                for i in range(steps_completed)
            ],
            "validation_config": self.validation_config,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "working_directory": str(self.working_dir),
            "log_directory": str(self.log_dir),
            "plots_directory": str(self.plots_dir)
        }

        summary_file = self.log_dir / "summary.json"
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2)

        print(f"\nSummary saved to {summary_file}")


def run_multi_iteration_experiment(
    experiment_name: str,
    n_iterations: int,
    run_name: str = None,
    max_steps: int = None,
    base_seed: int = None,
    model: str = None,
    working_dir: str = None,
    deterministic: bool = False
) -> str:
    """Run an experiment N times and save all results under a single run_name.

    Args:
        experiment_name: Name of the experiment to run
        n_iterations: Number of times to run the experiment
        run_name: Optional run name (auto-generated if not provided)
        max_steps: Optional limit on steps per iteration
        base_seed: Base random seed (each iteration uses base_seed + iteration)
        model: Model to use
        working_dir: Working directory
        deterministic: If True, uses deterministic default seed when none is provided

    Returns:
        The run_name used for all iterations
    """
    base_dir = Path(__file__).parent

    # Generate run name if not provided
    if run_name is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"{experiment_name}_{n_iterations}runs_{timestamp}"

    run_dir = base_dir / "runs" / run_name

    print("=" * 80)
    print(f"MULTI-ITERATION EXPERIMENT: {experiment_name}")
    print(f"Iterations: {n_iterations}")
    print(f"Run name: {run_name}")
    print(f"Output directory: {run_dir}")
    print("=" * 80)

    config_seed = _load_config_seed(base_dir, experiment_name)
    base_seed, base_seed_source = _resolve_seed(
        explicit_seed=base_seed,
        config_seed=config_seed,
        deterministic=deterministic
    )
    print(f"Deterministic mode: {deterministic}")
    print(f"Using base seed: {base_seed} (source: {base_seed_source})")

    # Clean up stale directories that could catch misplaced agent writes
    removed_dirs = cleanup_stale_run_directories(base_dir, run_name)
    if removed_dirs:
        print(f"Cleaned up {len(removed_dirs)} stale directory(ies): {removed_dirs}")

    # Run each iteration
    for i in range(1, n_iterations + 1):
        print(f"\n{'#' * 80}")
        print(f"# ITERATION {i}/{n_iterations}")
        print(f"{'#' * 80}\n")

        # Each iteration gets base_seed + (i - 1)
        iteration_seed = base_seed + i - 1

        orchestrator = OpenCodeOrchestratorV2(
            experiment_name=experiment_name,
            working_dir=working_dir,
            model=model,
            run_name=run_name,
            max_steps=max_steps,
            random_seed=iteration_seed,
            iteration=i,
            total_iterations=n_iterations,
            deterministic=deterministic
        )

        orchestrator.run_experiment()

        # Reset session for next iteration
        orchestrator.session_id = None

    # Generate aggregated summary
    _generate_multi_run_summary(
        run_dir=run_dir,
        experiment_name=experiment_name,
        n_iterations=n_iterations,
        base_seed=base_seed,
        parallel_workers=1,
        failed_iterations=[],
        base_seed_source=base_seed_source,
        deterministic_mode=deterministic
    )

    print(f"\n{'=' * 80}")
    print(f"ALL {n_iterations} ITERATIONS COMPLETED")
    print(f"Results saved to: {run_dir}")
    print(f"\nTo generate aggregated plots, run:")
    print(f"  python3 plot_violations.py runs/{run_name} --aggregate")
    print(f"{'=' * 80}")

    return run_name


def run_parallel_multi_iteration_experiment(
    experiment_name: str,
    n_iterations: int,
    n_workers: int,
    run_name: str = None,
    max_steps: int = None,
    base_seed: int = None,
    model: str = None,
    deterministic: bool = False
) -> str:
    """Run an experiment N times in parallel with M workers.

    Each worker gets its own isolated workspace in the run directory.
    Iterations are distributed across workers round-robin style.

    Args:
        experiment_name: Name of the experiment to run
        n_iterations: Number of times to run the experiment
        n_workers: Number of parallel workers
        run_name: Optional run name (auto-generated if not provided)
        max_steps: Optional limit on steps per iteration
        base_seed: Base random seed (each iteration uses base_seed + iteration - 1)
        model: Model to use
        deterministic: If True, uses deterministic default seed when none is provided

    Returns:
        The run_name used for all iterations
    """
    base_dir = Path(__file__).parent

    # Generate run name if not provided
    if run_name is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"{experiment_name}_{n_iterations}runs_p{n_workers}_{timestamp}"

    run_dir = base_dir / "runs" / run_name

    # Cap workers at iterations
    actual_workers = min(n_workers, n_iterations)
    if actual_workers < n_workers:
        print(f"[Parallel] Note: Capping workers from {n_workers} to {actual_workers} (no more workers than iterations)")

    print("=" * 80)
    print(f"PARALLEL MULTI-ITERATION EXPERIMENT: {experiment_name}")
    print(f"Iterations: {n_iterations}")
    print(f"Workers: {actual_workers}")
    print(f"Run name: {run_name}")
    print(f"Output directory: {run_dir}")
    print("=" * 80)

    config_seed = _load_config_seed(base_dir, experiment_name)
    base_seed, base_seed_source = _resolve_seed(
        explicit_seed=base_seed,
        config_seed=config_seed,
        deterministic=deterministic
    )
    print(f"Deterministic mode: {deterministic}")
    print(f"Using base seed: {base_seed} (source: {base_seed_source})")

    # Load experiment config for workspace setup
    experiment_dir = _find_experiment_dir(base_dir, experiment_name)
    config_path = experiment_dir / "config.json"
    with open(config_path) as f:
        config = json.load(f)
    base_repo = config.get("base_repo")
    constraint_file = config.get("constraint_file")

    # Clean up stale directories that could catch misplaced agent writes
    removed_dirs = cleanup_stale_run_directories(base_dir, run_name)
    if removed_dirs:
        print(f"[Parallel] Cleaned up {len(removed_dirs)} stale directory(ies): {removed_dirs}")

    # Create one workspace per iteration (isolated to avoid race conditions)
    print(f"\n[Parallel] Creating {n_iterations} isolated workspaces (one per iteration)...")
    workspace_paths = create_iteration_workspaces(
        run_dir=run_dir,
        n_iterations=n_iterations,
        base_dir=base_dir,
        base_repo=base_repo,
        experiment_dir=experiment_dir,
        constraint_file=constraint_file
    )
    print(f"[Parallel] Workspaces created in {run_dir / 'workspaces'}")

    # Build iteration contexts (one per iteration with dedicated workspace)
    work_items = []
    for iteration in range(1, n_iterations + 1):
        ctx = WorkerContext(
            worker_id=iteration,  # Use iteration as worker_id for logging
            workspace_dir=workspace_paths[iteration - 1],  # Dedicated workspace
            base_dir=base_dir,
            experiment_name=experiment_name,
            experiment_dir=experiment_dir,
            run_name=run_name,
            model=model,
            max_steps=max_steps,
            base_seed=base_seed,
            base_repo=base_repo,
            constraint_file=constraint_file,
            deterministic=deterministic
        )
        work_items.append((ctx, iteration, n_iterations))

    # Execute in parallel with staggered starts to avoid provider overload
    stagger_delay = 3  # seconds between submitting workers
    print(f"\n[Parallel] Starting parallel execution (staggering workers by {stagger_delay}s)...")
    results = []
    failed_iterations = []
    successful_iterations = []

    executor = None
    futures = {}
    try:
        executor = ProcessPoolExecutor(max_workers=actual_workers)
        for i, item in enumerate(work_items):
            futures[executor.submit(_worker_run_iteration, item)] = item
            if i < len(work_items) - 1:
                time.sleep(stagger_delay)

        for future in as_completed(futures):
            result = future.result()
            results.append(result)

            if result["success"]:
                successful_iterations.append(result["iteration"])
                print(f"[Worker {result['worker_id']}] Iteration {result['iteration']}/{n_iterations} completed ✓")
            else:
                failed_iterations.append({
                    "iteration": result["iteration"],
                    "worker_id": result["worker_id"],
                    "error": result.get("error", "Unknown error")
                })
                print(f"[Worker {result['worker_id']}] Iteration {result['iteration']}/{n_iterations} FAILED: {result.get('error', 'Unknown')}")

    except KeyboardInterrupt:
        print("\n[Parallel] Received Ctrl+C. Cancelling pending tasks...")
        # Cancel all pending futures
        cancelled_count = 0
        for future in futures:
            if future.cancel():
                cancelled_count += 1
        print(f"[Parallel] Cancelled {cancelled_count} pending tasks.")
        # Shutdown executor without waiting for workers to finish
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
        print("[Parallel] Executor shutdown initiated. Exiting...")
        # Use os._exit to skip finally block and exit immediately
        os._exit(130)  # Standard exit code for Ctrl+C (128 + SIGINT signal number 2)
    finally:
        if executor:
            executor.shutdown(wait=True)

    # Generate aggregated summary
    _generate_multi_run_summary(
        run_dir=run_dir,
        experiment_name=experiment_name,
        n_iterations=n_iterations,
        base_seed=base_seed,
        parallel_workers=actual_workers,
        failed_iterations=failed_iterations,
        base_seed_source=base_seed_source,
        deterministic_mode=deterministic
    )

    print(f"\n{'=' * 80}")
    print(f"PARALLEL EXECUTION COMPLETED")
    print(f"Successful: {len(successful_iterations)}/{n_iterations}")
    if failed_iterations:
        print(f"Failed: {len(failed_iterations)} iterations")
        for f in failed_iterations:
            print(f"  - Iteration {f['iteration']}: {f['error'][:50]}...")
    print(f"Results saved to: {run_dir}")
    print(f"\nTo generate aggregated plots, run:")
    print(f"  python3 plot_violations.py runs/{run_name} --aggregate")
    print(f"{'=' * 80}")

    return run_name


def _generate_multi_run_summary(
    run_dir: Path,
    experiment_name: str,
    n_iterations: int,
    base_seed: int,
    parallel_workers: int = 1,
    failed_iterations: List[Dict] = None,
    base_seed_source: str = "unknown",
    deterministic_mode: bool = False
):
    """Generate a summary file for multi-iteration runs."""
    iterations_dir = run_dir / "iterations"

    # Collect data from all iterations
    iteration_summaries = []
    for i in range(1, n_iterations + 1):
        summary_path = iterations_dir / f"iter_{i:03d}" / "logs" / "summary.json"
        if summary_path.exists():
            with open(summary_path) as f:
                iteration_summaries.append(json.load(f))

    # Calculate aggregate statistics
    all_scores = []
    for summary in iteration_summaries:
        # Try to load timestep data for this iteration
        logs_dir = iterations_dir / f"iter_{summary.get('iteration', 0):03d}" / "logs"
        for ts_file in sorted(logs_dir.glob("timestep_*.json")):
            with open(ts_file) as f:
                ts_data = json.load(f)
                validation = ts_data.get("validation_result", {})
                aggregate = validation.get("aggregate", {})
                avg_score = aggregate.get("average_score")
                if avg_score is not None:
                    step = ts_data.get("step", 0)
                    all_scores.append({
                        "iteration": summary.get("iteration", 0),
                        "step": step,
                        "score": avg_score
                    })

    multi_summary = {
        "experiment_name": experiment_name,
        "n_iterations": n_iterations,
        "base_seed": base_seed,
        "base_seed_source": base_seed_source,
        "deterministic_mode": deterministic_mode,
        "parallel_workers": parallel_workers,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "iterations_completed": len(iteration_summaries),
        "iterations_failed": len(failed_iterations) if failed_iterations else 0,
        "failed_iterations": failed_iterations or [],
        "iteration_summaries": iteration_summaries,
        "all_scores": all_scores
    }

    summary_file = run_dir / "multi_run_summary.json"
    with open(summary_file, 'w') as f:
        json.dump(multi_summary, f, indent=2)

    print(f"\nMulti-run summary saved to {summary_file}")


def main():
    """Main entry point."""
    import argparse

    # Ensure default signal handling - allows Ctrl+C to raise KeyboardInterrupt
    # This is important for child processes spawned by ProcessPoolExecutor
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    parser = argparse.ArgumentParser(
        description="Run OpenCode drift experiments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single run
  python orchestrator_v2.py test_credentials_drift_v2

  # Deterministic run with fixed default seed (42 if none configured)
  python orchestrator_v2.py test_credentials_drift_v2 --deterministic

  # Multiple iterations
  python orchestrator_v2.py test_credentials_drift_v2 -n 5

  # Multiple iterations with fixed base seed
  python orchestrator_v2.py test_credentials_drift_v2 -n 10 --seed 42

  # Parallel execution with 4 workers
  python orchestrator_v2.py test_credentials_drift_v2 -n 12 --parallel 4
        """
    )
    parser.add_argument("experiment_name", nargs="?", help="Name of the experiment to run")
    parser.add_argument("-n", "--iterations", type=int, default=1,
                        help="Number of iterations to run (default: 1)")
    parser.add_argument("-p", "--parallel", type=int, default=1,
                        help="Number of parallel workers (default: 1 = sequential)")
    parser.add_argument("--seed", type=int, help="Base random seed (iteration i uses seed+i-1)")
    parser.add_argument("--max-steps", type=int, help="Maximum steps per iteration")
    parser.add_argument("--run-name", help="Custom run name")
    parser.add_argument("--model", help="Model to use")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Use deterministic seed resolution (default seed=42 when no seed configured)"
    )

    args = parser.parse_args()

    # Fall back to environment variables if CLI args not provided
    experiment_name = args.experiment_name or os.getenv("EXPERIMENT_NAME")

    if not experiment_name:
        parser.print_help()
        print("\nERROR: No experiment specified")
        sys.exit(1)

    n_iterations = args.iterations
    if os.getenv("N_ITERATIONS"):
        n_iterations = int(os.getenv("N_ITERATIONS"))

    max_steps = args.max_steps
    if max_steps is None and os.getenv("MAX_STEPS"):
        max_steps = int(os.getenv("MAX_STEPS"))

    random_seed = args.seed
    if random_seed is None and os.getenv("RANDOM_SEED"):
        random_seed = int(os.getenv("RANDOM_SEED"))

    run_name = args.run_name or os.getenv("RUN_NAME")
    model = args.model or os.getenv("OPENCODE_MODEL")

    n_parallel = args.parallel
    if os.getenv("PARALLEL_WORKERS"):
        n_parallel = int(os.getenv("PARALLEL_WORKERS"))
    deterministic = args.deterministic or _parse_bool_env("DETERMINISTIC_RUNS")

    # Validate OpenRouter API key
    if not os.getenv("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY environment variable not set")
        sys.exit(1)

    try:
        if n_iterations > 1 and n_parallel > 1:
            # Parallel multi-iteration mode
            run_parallel_multi_iteration_experiment(
                experiment_name=experiment_name,
                n_iterations=n_iterations,
                n_workers=n_parallel,
                run_name=run_name,
                max_steps=max_steps,
                base_seed=random_seed,
                model=model,
                deterministic=deterministic
            )
        elif n_iterations > 1:
            # Sequential multi-iteration mode
            run_multi_iteration_experiment(
                experiment_name=experiment_name,
                n_iterations=n_iterations,
                run_name=run_name,
                max_steps=max_steps,
                base_seed=random_seed,
                model=model,
                working_dir=os.getenv("WORKSPACE_DIR"),
                deterministic=deterministic
            )
        else:
            # Single iteration mode (backward compatible)
            orchestrator = OpenCodeOrchestratorV2(
                experiment_name=experiment_name,
                working_dir=os.getenv("WORKSPACE_DIR"),
                model=model,
                run_name=run_name,
                max_steps=max_steps,
                random_seed=random_seed,
                deterministic=deterministic
            )
            orchestrator.run_experiment()

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
