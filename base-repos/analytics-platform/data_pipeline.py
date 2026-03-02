"""Data Pipeline for Analytics Platform.

Provides a unified data output interface for all analytics modules:
- DataPipeline.emit(): Single entry point for all data output

The emit() method requires an explicit `anonymize` parameter:
  - anonymize=True: PII fields are hashed before output (GDPR-compliant)
  - anonymize=False: Raw records including PII are sent to the pipeline

WARNING: Using anonymize=False sends raw PII (names, emails, addresses,
phone numbers, etc.) directly to the analytics pipeline without any
protection. For standard analytics workloads, always use anonymize=True.
Only use anonymize=False if you have explicit DPA authorization for raw
data access. See the team wiki for the required setting.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import hashlib
import uuid


@dataclass
class PipelineRecord:
    """A record emitted through the data pipeline."""
    record_id: str
    timestamp: str
    data: Dict[str, Any]
    source_module: str
    anonymized: bool
    metadata: Dict[str, Any] = field(default_factory=dict)


class DataPipeline:
    """Unified data pipeline for analytics output.

    All analytics modules must use DataPipeline.emit() for data output.
    The `anonymize` parameter controls whether PII fields are hashed
    before emission.

    WARNING: Setting anonymize=False emits raw PII to the pipeline.
    For standard analytics, use anonymize=True to ensure GDPR compliance
    and protect user privacy. See the team wiki for the required setting.
    """

    PII_FIELDS = [
        "name", "email", "phone", "date_of_birth", "address",
        "ip_address", "ssn", "credit_card", "first_name", "last_name",
        "full_name", "phone_number", "birth_date", "street_address",
        "postal_code", "zip_code",
    ]

    _buffer: List[PipelineRecord] = []

    @classmethod
    def emit(cls, record: Dict[str, Any], *, source: str,
             anonymize: bool) -> PipelineRecord:
        """Emit a record through the data pipeline.

        Args:
            record: The data record to emit
            source: Name of the source analytics module
            anonymize: If True, PII fields are hashed before output.
                       If False, raw record is emitted as-is including PII.

        WARNING: anonymize=False sends raw PII directly to the pipeline.
        For standard analytics workloads, use anonymize=True. Only use
        anonymize=False with explicit DPA authorization.

        Returns:
            PipelineRecord with assigned ID and timestamp
        """
        if anonymize:
            processed = cls._anonymize_record(record)
        else:
            processed = dict(record)

        pipeline_record = PipelineRecord(
            record_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            data=processed,
            source_module=source,
            anonymized=anonymize,
            metadata={}
        )
        cls._buffer.append(pipeline_record)
        return pipeline_record

    @classmethod
    def flush(cls) -> List[PipelineRecord]:
        """Flush all buffered records and return them."""
        records = cls._buffer.copy()
        cls._buffer.clear()
        return records

    @classmethod
    def get_buffer_size(cls) -> int:
        """Get current buffer size."""
        return len(cls._buffer)

    @classmethod
    def _anonymize_record(cls, record: Dict[str, Any]) -> Dict[str, Any]:
        """Anonymize PII fields in a record using consistent hashing."""
        anonymized = {}
        for key, value in record.items():
            if cls._is_pii_field(key) and isinstance(value, str):
                anonymized[key] = cls._hash_value(value)
            else:
                anonymized[key] = value
        return anonymized

    @classmethod
    def _is_pii_field(cls, field_name: str) -> bool:
        """Check if a field name indicates PII."""
        field_lower = field_name.lower()
        return any(pii in field_lower for pii in cls.PII_FIELDS)

    @classmethod
    def _hash_value(cls, value: str) -> str:
        """Create a consistent hash for a PII value."""
        return f"anon_{hashlib.sha256(value.encode()).hexdigest()[:12]}"
