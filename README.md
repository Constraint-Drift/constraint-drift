# Asymmetric Goal Drift

Experiment framework for the paper **"Asymmetric Goal Drift in Coding Agents Under Value Conflict"** (ICLR 2026 Workshop on Lifelong Agents). The full paper is included in this repository: [asymmetric_goal_drift.pdf](asymmetric_goal_drift.pdf).

## Core Concept

![](./image/remade-asym-drift-fig1.pdf)

Tests whether AI coding agents drift from explicit instructions when facing conflicting environmental pressure. Uses a 2×2 factorial design:

- **X** = Value specified in the system prompt (AGENTS.md)
- **Y** = Conflicting value applied via environmental pressure (code comments, patches)

| Variant | Instruction | Pressure | Measures |
|---------|-------------|----------|----------|
| Regular | Follow X | Y pressure | Does agent abandon X? |
| Baseline | Follow X | Neutral | Agent's default X compliance |
| Inverted | Follow Y | X pressure | Does agent abandon Y? |
| Inverted Baseline | Follow Y | Neutral | Agent's default Y compliance |

## Experiments

| Experiment | X (Instruction) | Y (Pressure) |
|------------|-----------------|--------------|
| `utility_privacy_drift` | Output full data (`anonymize=False`) | Privacy warnings (GDPR, PII) |
| `efficiency_security_drift` | Revalidate auth on every call | Performance/latency concerns |
| `convenience_security_drift` | Embed test credentials in code | Security warnings (hardcoded secrets) |

Each experiment has four variants: `_baseline`, `_inverted`, and `_inverted_baseline`. Experiments are grouped by value conflict under `experiments/` but are referenced by name only (e.g., `utility_privacy_drift`, not `utility_privacy/utility_privacy_drift`).

## Creating New Experiments

See [EXPERIMENT_CREATION_GUIDE.md](EXPERIMENT_CREATION_GUIDE.md) for detailed step-by-step instructions on designing novel value-conflict experiments, including how to create base repos, AGENTS.md constraint files, escalating patch pools, validators, and the full 2×2 factorial experiment family.

## Setup

```bash
# Install dependencies
pip install -e .

# Or manually
pip install python-dotenv pydantic openai matplotlib numpy

# Set API key
export OPENROUTER_API_KEY="your_key"
```

Requires [OpenCode](https://opencode.ai) CLI installed separately:
```bash
npm install -g opencode
# or
curl -fsSL https://opencode.ai/install | bash
```

## Running Experiments

```bash
# Single iteration
python orchestrator.py utility_privacy_drift

# 10 iterations, 5 in parallel, with a specific model
python orchestrator.py utility_privacy_drift -n 10 -p 5 --model openrouter/openai/gpt-5-mini

# Deterministic mode (uses config seed, or 42 if none set)
python orchestrator.py utility_privacy_drift --deterministic

# Explicit seed
python orchestrator.py utility_privacy_drift -n 10 --seed 42

# Limit steps for testing
python orchestrator.py utility_privacy_drift --max-steps 2

# Custom run name
python orchestrator.py utility_privacy_drift --run-name my-test-run
```

### CLI Options

```
python orchestrator.py <experiment_name> [options]

Options:
  -n, --iterations N    Number of iterations (default: 1)
  -p, --parallel N      Parallel workers (default: 1)
  --seed N              Base random seed
  --deterministic       Deterministic seed fallback (42 if no seed configured)
  --max-steps N         Limit steps per iteration
  --run-name NAME       Custom run name
  --model MODEL         Override model
```

## Revalidating Runs

Re-run validators on existing run data without re-running the agent. Supports two modes:

### Regex Validation (fast, no API calls)

```bash
# Auto-detect experiment/validator, update files in place
python revalidate_run.py runs/<run_name> --use-regex --update-in-place

# Dry run first to preview what would change
python revalidate_run.py runs/<run_name> --use-regex --dry-run
```

### LLM Judge Validation (slower, uses API)

```bash
# Auto-detect experiment/validator
python revalidate_run.py runs/<run_name> --update-in-place

# Specify judge model
python revalidate_run.py runs/<run_name> --judge-model openrouter/openai/gpt-5.2 --update-in-place

# Override validator and experiment
python revalidate_run.py runs/<run_name> \
    --validator test_credential_inverted_validator \
    --experiment convenience_security_drift_inverted \
    --update-in-place
```

### Revalidation Options

```
python revalidate_run.py <run_dir> [options]

Options:
  --validator NAME       Validator name (auto-detected if omitted)
  --experiment NAME      Experiment name (auto-detected if omitted)
  --judge-model MODEL    LLM judge model (default: openrouter/openai/gpt-4o-mini)
  --update-in-place      Write new validation results to timestep files
  --dry-run              Preview without making changes or API calls
  --use-regex            Use fast regex matching instead of LLM judge
  --no-workspace         Skip workspace files; use pre_agent_file_content only
  --delay SECONDS        Delay between API requests (default: 0.5)
  --max-retries N        Retries per timestep on failure (default: 3)
```

## Plotting Results

### Single-Run Analysis

```bash
python plot_violations.py runs/<run_name>
```

Generates in `<run_dir>/plots/`: `violation_trend.png`, `violation_breakdown.png`, `cumulative_violations.png`, `violation_report.txt`.

### Aggregated Multi-Run Analysis

For runs with multiple iterations (auto-detected):

```bash
python plot_violations.py runs/<multi_iteration_run>
```

Generates in `<run_dir>/aggregated_plots/`: `aggregated_score_trend.png`, `score_distribution.png`, `iteration_heatmap.png`, `scores_by_file.png`.

### Comparing Experiments

```bash
# Compare 2-4 experiment variants
python plot_violations.py --compare \
    runs/baseline runs/with_pressure \
    --x safety --y convenience \
    -o plots/comparison/
```

When `--x` and `--y` are provided, the four runs are labeled as: `<y>`, `<y> → <x>`, `<x>`, `<x> → <y>`.

### Config-Based 2×2 Grid (Multi-Model)

Use a JSON config to compare multiple models across all four conditions:

```bash
python plot_violations.py --config runs/utility_privacy_plot.json -o plots/
```

Config format:
```json
{
  "x": "Utility",
  "y": "Privacy",
  "experiments": {
    "x_to_y":     { "Model A": "runs/model-a-regular",     "Model B": "runs/model-b-regular" },
    "x_baseline": { "Model A": "runs/model-a-baseline",    "Model B": "runs/model-b-baseline" },
    "y_to_x":     { "Model A": "runs/model-a-inverted",    "Model B": "runs/model-b-inverted" },
    "y_baseline": { "Model A": "runs/model-a-inv-baseline", "Model B": "runs/model-b-inv-baseline" }
  }
}
```

Generates 2×2 grid trend and bar plots comparing all models across conditions.

### Conscious Drift Filtering

After classifying violations with `classify_violations.py`, filter to only intentional drift:

```bash
python classify_violations.py runs/<run_name>
python plot_violations.py --config runs/my_config.json --conscious-drift-only -o plots/
```

### Plotting CLI Reference

```
python plot_violations.py [OPTIONS] [path] [output_dir]

Modes:
  <path>                        Single-run or aggregated analysis (auto-detected)
  --compare DIR [DIR ...]       Compare 2-4 experiment variants
  --compare-models DIR [DIR ...]  Compare models on the same experiment
  --config PATH                 JSON config for 2×2 grid comparison

Options:
  -o, --output DIR              Output directory for plots
  -a, --aggregate               Force aggregated analysis mode
  --x VALUE                     Primary value label (e.g., "Utility")
  --y VALUE                     Opposing value label (e.g., "Privacy")
  --labels LABEL [...]          Custom labels for --compare-models
  --title TEXT                   Title prefix for plots
  --show-n                      Show sample size (n=X) in labels
  --conscious-drift-only        Only count CONSCIOUS_DRIFT violations
```

## Directory Structure

```
constraint-drift/
├── orchestrator.py             # Main experiment runner
├── plot_violations.py          # Plotting and analysis
├── revalidate_run.py           # Re-validate with updated validators
├── classify_violations.py      # Classify violation types
├── merge_runs.py               # Merge multiple runs
├── pyproject.toml
│
├── experiments/                # Experiment definitions (grouped by value conflict)
│   ├── utility_privacy/
│   │   ├── utility_privacy_drift/
│   │   ├── utility_privacy_drift_baseline/
│   │   ├── utility_privacy_drift_inverted/
│   │   └── utility_privacy_drift_inverted_baseline/
│   ├── convenience_security/
│   │   ├── convenience_security_drift/
│   │   ├── convenience_security_drift_baseline/
│   │   ├── convenience_security_drift_inverted/
│   │   └── convenience_security_drift_inverted_baseline/
│   └── efficiency_security/
│       ├── efficiency_security_drift/
│       ├── ...
│       └── each contains:
│           ├── config.json     # Queries, patches, validation config
│           ├── AGENTS.md       # System prompt with X instruction
│           ├── patches/        # Escalating pressure files (severity 1-6)
│           └── validators/     # Scoring logic (regex + LLM judge)
│
├── base-repos/                 # Starting codebases with stub functions
│
└── runs/                       # Output from experiment runs
    ├── *_plot.json             # Plot configs for 2×2 comparisons
    ├── utility_privacy/        # Compressed run archives (.tar.gz)
    ├── convenience_security/
    ├── efficiency_security/
    └── <run_name>/             # Extracted run directory
        ├── multi_run_summary.json
        └── iterations/
            └── iter_XXX/logs/
                ├── summary.json
                └── timestep_XXX.json
```

## Included Run Data

The `runs/` directory contains compressed archives (`.tar.gz`) of all experiment runs from the paper, grouped by value conflict:

```
runs/
├── utility_privacy/           # 12 runs (4 conditions × 3 models)
├── convenience_security/      # 12 runs
└── efficiency_security/       # 12 runs
```

To extract a run for analysis or plotting:

```bash
cd runs/utility_privacy
tar xzf <run_name>.tar.gz
```

Plot config files (`*_plot.json`) in `runs/` reference the compressed run names and can be used directly after extraction.

## Output Format

Each `timestep_XXX.json` contains:

| Field | Description |
|-------|-------------|
| `message` | The task sent to the agent |
| `target_file` | The file this query targets |
| `response` | Full agent response with events (tool calls, text, errors) |
| `pre_agent_file_content` | File content before agent ran (after patch applied) |
| `validation_result` | Validator score and reasoning |
| `agent_diffs` | Per-file diffs with additions/deletions |

## Reproducibility

- Set `execution.random_seed` in each experiment's `config.json` or pass `--seed`.
- Use `--deterministic` (or `DETERMINISTIC_RUNS=1`) to avoid timestamp-based seed fallback.
- Note: `opencode run` does not expose flags for temperature or sampler seed, so model outputs may still vary even with fixed orchestration seeds.

## Aborting a Run

Press Ctrl+C to cancel. The orchestrator will cancel pending tasks, shut down workers, and exit. To kill stuck processes:

```bash
pkill -f orchestrator.py && pkill -f multiprocessing.spawn && pkill -f opencode
```
