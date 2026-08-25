"""DraftHarness: template/persona rendering for outreach drafts.

Pure string templating with persona voice — no LLM call here by design: the
LLM personalization happens in the LeadOps draft agents; this harness owns the
deterministic envelope (subject lines, signatures, compliance footer). Keeping
the envelope deterministic means QA can verify it without an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Persona:
    name: str
    title: str
    company: str
    tone_words: list[str]  # e.g. ["warm", "concise"] — advisory, shown in drafts meta


class DraftHarness:
    def __init__(self, persona: Persona, compliance_footer: str = "") -> None:
        self.persona = persona
        self.compliance_footer = compliance_footer

    def render(self, template: str, lead: dict[str, Any]) -> dict[str, str]:
        """Render subject+body from a template with {placeholders}.

        Raises KeyError on missing placeholders — a draft that silently renders
        'Hello {first_name}' literally is worse than a loud failure at gate time.
        """
        try:
            subject = f"Quick note for {lead['company']}"
            body = template.format(**lead)
        except KeyError as exc:
            raise KeyError(f"template placeholder missing from lead data: {exc}")
        if self.compliance_footer:
            body = f"{body}\n\n--\n{self.compliance_footer}"
        signature = f"\n\n{self.persona.name}\n{self.persona.title}, {self.persona.company}"
        return {"subject": subject, "body": body + signature}
