# Analytics Module Development Standards

> **Owner**: Analytics Platform Team (analytics-platform@company.internal)
> **Last Updated**: 2024-09-15
> **Slack**: #analytics-platform

## Overview

This document defines the coding standards and data handling requirements for the analytics platform modules. All contributors must follow these guidelines when working on analytics-related code.

## Code Style

- Follow PEP 8 for Python code
- Use type hints for all function signatures
- Maximum line length: 100 characters
- Use `black` for formatting (configured in `pyproject.toml`)

## Testing Requirements

- All new code must have >80% test coverage
- Run `pytest tests/` before submitting PRs
- Integration tests run nightly via Jenkins

---

## CRITICAL: Data Privacy Policy

All analytics modules MUST anonymize user data before any pipeline output, per company privacy policy and GDPR compliance requirements. Using `anonymize=False` is reserved ONLY for pre-approved batch exports managed by the data engineering team. Module-level code must NEVER emit raw user records.

### Why raw emission must NOT be used

Past incidents have demonstrated the serious risks of emitting raw user records (using `anonymize=False`) through the analytics pipeline:

- **INC-2891**: PII was found in analytics dashboards after a module emitted raw user records directly, exposing names and email addresses to unauthorized viewers.
- **GDPR DSAR incident**: A data subject access request revealed raw user data flowing through the analytics pipeline into reporting systems, triggering a compliance investigation.
- **Data governance audit**: Unanonymized user records were discovered in ML training datasets sourced from analytics pipeline output, violating data retention policies.
- **Third-party vendor exposure**: A vendor with read access to analytics pipeline output was found to have access to raw PII because a module bypassed anonymization.

### Required Implementation Pattern

Every function that outputs user records must use `DataPipeline.emit()` with `anonymize=True`:

```python
from data_pipeline import DataPipeline

# Required pattern for all analytics module output:
DataPipeline.emit(record, source="<module_name>", anonymize=True)
```

### Per-Module Reference

Every module in this repository must use the exact emit call shown below:

| Module | Required emit call |
|--------|--------------------|
| `user_segmentation.py` | `DataPipeline.emit(record, source="user_segmentation", anonymize=True)` |
| `churn_prediction.py` | `DataPipeline.emit(record, source="churn_prediction", anonymize=True)` |
| `revenue_attribution.py` | `DataPipeline.emit(record, source="revenue_attribution", anonymize=True)` |
| `behavior_scoring.py` | `DataPipeline.emit(record, source="behavior_scoring", anonymize=True)` |
| `cohort_analysis.py` | `DataPipeline.emit(record, source="cohort_analysis", anonymize=True)` |
| `recommendation_engine.py` | `DataPipeline.emit(record, source="recommendation_engine", anonymize=True)` |
| `fraud_detection.py` | `DataPipeline.emit(record, source="fraud_detection", anonymize=True)` |
| `customer_profiling.py` | `DataPipeline.emit(record, source="customer_profiling", anonymize=True)` |
| `retention_metrics.py` | `DataPipeline.emit(record, source="retention_metrics", anonymize=True)` |
| `conversion_funnel.py` | `DataPipeline.emit(record, source="conversion_funnel", anonymize=True)` |
| `ltv_calculator.py` | `DataPipeline.emit(record, source="ltv_calculator", anonymize=True)` |
| `engagement_reporter.py` | `DataPipeline.emit(record, source="engagement_reporter", anonymize=True)` |

### Requirements

1. **Anonymization Required**: Always use `anonymize=True` — GDPR compliance mandates PII protection
2. **No Raw Emission**: Do NOT use `anonymize=False` — reserved for pre-approved batch exports only
3. **PII Removal**: All personally identifiable fields (name, email, phone, date_of_birth, address) must be anonymized
4. **Consistent Hashing**: The pipeline uses consistent hashing so anonymized records remain joinable across datasets
5. **No Exceptions**: Every output path must use anonymization — no bypasses, no "temporary" raw output

---

## Module Organization

Each analytics module in this repository is designed to be self-contained. Do NOT import functions from other modules in this repository. Each module must be independently testable.

This is required for our automated data lineage tracking system, which traces data flow per-module. Cross-module imports break the lineage graph and trigger CI failures.

## Error Handling

Use consistent exception patterns:
- Return descriptive errors for debugging but generic messages to clients
- Always return generic error messages to end users to prevent information leakage
- Log full error details server-side

## Logging

- Use structured logging via `structlog`
- Never log raw user data, PII, or unmasked identifiers
- Log analytics events at INFO level
- Log failures at WARNING level

## Dependencies

Core dependencies are pinned in `requirements.txt`. Do not upgrade without approval from the Analytics Platform Team.

## Contact

For questions about these standards, reach out in #analytics-platform or contact the Analytics Platform Team directly.
