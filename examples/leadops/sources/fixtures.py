"""Open-data lead fixtures — offline-first input for the LeadOps pipeline.

Deliberately messy: duplicate companies with case/spacing variants, missing
fields, inconsistent phone formats, and near-identical names. This is what
exercises DEDUPE and ENRICH honestly. Derived from public startup-directory
style data; no real personal data.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES_PATH = Path(__file__).parent / "leads.json"


def load_leads() -> list[dict[str, object]]:
    """Load raw fixture leads."""
    leads: list[dict[str, object]] = json.loads(
        FIXTURES_PATH.read_text(encoding="utf-8")
    )
    return leads
