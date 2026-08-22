"""Shared evidence envelope for high-level public-information tools."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

EvidenceStatus = Literal[
    "ok", "partial", "not_configured", "no_results", "upstream_error"
]
EvidencePrecision = Literal[
    "dataset", "area", "transaction", "listing", "candidate", "registry"
]
EvidenceConfidence = Literal["high", "medium", "low", "not_applicable"]


def build_evidence_envelope(
    *,
    status: EvidenceStatus,
    data_as_of: str | None,
    precision: EvidencePrecision,
    confidence: EvidenceConfidence,
    sources: list[dict[str, Any]],
    limitations: list[str],
    data: dict[str, Any],
) -> dict[str, Any]:
    """Build the stable, machine-readable Phase 1 evidence envelope."""
    return {
        "status": status,
        "data_as_of": data_as_of,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "precision": precision,
        "confidence": confidence,
        "sources": sources,
        "limitations": limitations,
        "data": data,
    }
