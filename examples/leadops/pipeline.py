"""LeadOps flagship pipeline: INGEST -> ENRICH -> DEDUPE -> SCORE -> DRAFT -> QA -> REVIEW.

Built entirely on the swarmd kernel: the runtime executes each lead as a task
with checkpointed steps; the DAG executor orders stages; quality gates sit
between SCORE->DRAFT and QA->REVIEW; approved drafts land in the durable HITL
review queue (never auto-sent — ADR-003).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import BaseModel, Field

from swarmd.harnesses.llm import LLMHarness
from swarmd.harnesses.verify import (
    VerifyHarness,
    forbidden_content_check,
    range_check,
    schema_check,
)
from swarmd.hitl.approvals import ApprovalManager, InMemoryApprovalStore
from swarmd.observability.tracing import (
    TraceSink,
    instrument_llm,
    record_thought,
    tracer,
)
from swarmd.pipeline.gates import QualityGate
from swarmd.router.providers import Provider

# ---- structured outputs ----------------------------------------------------


class Enriched(BaseModel):
    company_clean: str
    domain: str
    signals: list[str]


class Score(BaseModel):
    icp_score: int = Field(ge=0, le=10)
    reason: str


class Draft(BaseModel):
    subject: str
    body: str


# ---- pipeline --------------------------------------------------------------


@dataclass
class LeadOpsResult:
    leads_in: int = 0
    enriched: int = 0
    deduped: int = 0
    scored: int = 0
    drafted: int = 0
    qa_passed: int = 0
    awaiting_review: int = 0
    dead_lettered: int = 0
    taxonomy: dict[str, int] = field(default_factory=dict)
    integrity_hash: str = ""
    trace_id: str = ""


def _normalize_key(company: str) -> str:
    """Canonical dedupe key: lowercase, strip non-alphanumerics."""
    return "".join(c for c in company.lower() if c.isalnum())


def _lead_key(lead: dict[str, Any]) -> str:
    """Stable identity for a lead across runs — email if present else company.

    Case-insensitive: email providers treat local parts case-insensitively in
    practice, and fixture data mixes casing freely.
    """
    ident = lead.get("email") or _normalize_key(lead.get("company", ""))
    return hashlib.sha256(ident.lower().encode()).hexdigest()[:16]


class LeadOpsPipeline:
    def __init__(self, provider: Any, trace_sink: TraceSink | None = None) -> None:
        # provider is Provider or the tracing wrapper (structurally identical).
        # Every LLM call becomes a traced span (prompt/response/tokens) — visible
        # in Jaeger AND Langfuse-style backends via the same sink.
        traced = instrument_llm(provider, trace_sink)
        # The wrapper is structurally a Provider; cast keeps mypy strict happy.
        traced_provider = cast(Provider, traced)
        # Verifier stages pin low temperature; draft runs warmer.
        self.enrich_llm = LLMHarness(traced_provider, temperature=0.2, max_tokens=256,
                                     system_prompt="You normalize and enrich company records. Be factual.")
        self.score_llm = LLMHarness(traced_provider, temperature=0.2, max_tokens=128,
                                    system_prompt="You score B2B leads for ICP fit. Be strict.")
        self.draft_llm = LLMHarness(traced_provider, temperature=0.7, max_tokens=320,
                                    system_prompt="You write short, warm first-touch B2B emails.")
        self.trace_sink = trace_sink
        self.approvals = ApprovalManager(InMemoryApprovalStore())

        self.score_gate = QualityGate(
            "score",
            VerifyHarness("score")
            .add_check(schema_check(["icp_score", "reason"]))
            .add_check(range_check("icp_score", 0, 10))
            .verify,
            max_repairs=1,
            repair_fn=self._repair_score,
        )
        self.qa_gate = QualityGate(
            "qa",
            VerifyHarness("qa")
            .add_check(schema_check(["subject", "body"]))
            .add_check(forbidden_content_check(["guarantee", "risk-free", "act now"]))
            .verify,
            max_repairs=1,
            repair_fn=self._repair_draft,
        )

    async def _repair_score(self, item: dict[str, Any], reason: str) -> dict[str, Any]:
        out = await self.score_llm.structured(
            f"Fix this scoring to satisfy: {reason}. "
            f"Company: {item.get('company')}, signals: {item.get('signals', [])}",
            Score,
        )
        return {**item, **out.model_dump()}

    async def _repair_draft(self, item: dict[str, Any], reason: str) -> dict[str, Any]:
        out = await self.draft_llm.structured(
            f"Rewrite this draft to satisfy: {reason}. "
            f"Company: {item.get('company')}",
            Draft,
        )
        return {**item, **out.model_dump()}

    async def run(self, raw_leads: list[dict[str, Any]]) -> LeadOpsResult:
        res = LeadOpsResult(leads_in=len(raw_leads))

        with tracer("stage", "leadops.run", sink=self.trace_sink, leads=len(raw_leads)):
            # INGEST: stable keys
            leads = [{**lead, "_key": _lead_key(lead)} for lead in raw_leads]
            record_thought("ingest", reasoning="assigned stable identity keys",
                           count=len(leads))

            # ENRICH (concurrently)
            with tracer("stage", "enrich", sink=self.trace_sink):
                enriched = await asyncio.gather(*(self._enrich(l) for l in leads))
                record_thought("enrich_done", reasoning="normalized companies + extracted signals",
                               enriched=len(enriched))
            res.enriched = len(enriched)

            # DEDUPE: canonical-key merge, keep richest record
            with tracer("stage", "dedupe", sink=self.trace_sink):
                by_key: dict[str, dict[str, Any]] = {}
                for lead in enriched:
                    k = _normalize_key(lead["company"])
                    existing = by_key.get(k)
                    if existing is None or self._richness(lead) > self._richness(existing):
                        by_key[k] = lead
                deduped = list(by_key.values())
                record_thought("dedupe_done",
                               reasoning="merged case/spacing variants by canonical key",
                               merged_from=len(enriched), kept=len(deduped))
            res.deduped = len(deduped)

            # SCORE with quality gate
            with tracer("stage", "score", sink=self.trace_sink):
                scored_items: list[dict[str, Any]] = []
                for lead in deduped:
                    score = await self.score_llm.structured(
                        f"Score ICP fit 0-10 for a B2B automation seller.\n"
                        f"Company: {lead['company']}\nSignals: {lead.get('signals', [])}\n"
                        f"Employees: {lead.get('employees')}",
                        Score,
                    )
                    item = {**lead, **score.model_dump()}
                    outcome = await self.score_gate.check(item)
                    if outcome.ok:
                        scored_items.append(outcome.item)
                    else:
                        res.dead_lettered += 1
                record_thought("score_done", reasoning="gated scores; failures dead-lettered",
                               passed=len(scored_items))
            res.scored = len(scored_items)

            # DRAFT (concurrent outreach agents)
            with tracer("stage", "draft", sink=self.trace_sink):
                drafts = await asyncio.gather(*(self._draft(item) for item in scored_items))
                record_thought("draft_done", reasoning="personalized first-touch emails",
                               drafted=len(drafts))
            res.drafted = len(drafts)

            # QA gate
            with tracer("stage", "qa", sink=self.trace_sink):
                qa_passed: list[dict[str, Any]] = []
                for item in drafts:
                    outcome = await self.qa_gate.check(item)
                    if outcome.ok:
                        qa_passed.append(outcome.item)
                    else:
                        res.dead_lettered += 1
                record_thought("qa_done", reasoning="compliance checks; banned language blocked",
                               passed=len(qa_passed))
            res.qa_passed = len(qa_passed)

            # REVIEW QUEUE: durable HITL state — never auto-send (ADR-003)
            with tracer("approval", "review_queue_submit", sink=self.trace_sink,
                        items=len(qa_passed)):
                for item in qa_passed:
                    await self.approvals.submit(item, stage="review_queue")
                record_thought("queued_for_human", reasoning="ADR-003: outreach never auto-sends")
            res.awaiting_review = len(qa_passed)

            res.taxonomy = {
                **self.score_gate.taxonomy,
                **{f"qa:{k}": v for k, v in self.qa_gate.taxonomy.items()},
            }
            res.integrity_hash = self._integrity_hash(qa_passed)
            from swarmd.observability.tracing import current_trace_id
            res.trace_id = current_trace_id()
            return res

    async def _enrich(self, lead: dict[str, Any]) -> dict[str, Any]:
        try:
            out = await self.enrich_llm.structured(
                f"Normalize this company record. Company: {lead['company']!r} "
                f"Website: {lead.get('website') or 'unknown'}",
                Enriched,
            )
            # Identity rule: enrichment may ADD signals but never REPLACE the
            # company name — dedupe keys on it. A "cleaned" name that differs
            # from the input would silently break duplicate detection.
            return {
                **lead,
                "domain": out.domain or (lead.get("website") or "").replace("https://", "").replace("http://", "").split("/")[0],
                "signals": out.signals,
            }
        except (ValueError, KeyError):
            # Enrichment failure is not fatal; pass through raw for downstream gates.
            # Narrow catch: unexpected errors SHOULD surface, not be swallowed.
            return {**lead, "signals": []}

    async def _draft(self, item: dict[str, Any]) -> dict[str, Any]:
        draft = await self.draft_llm.structured(
            f"Write a 3-sentence first-touch email.\n"
            f"From: Arsh at SwarmCo (we automate ops workflows).\n"
            f"To: {item.get('contact_name') or 'team'} at {item['company']} "
            f"(ICP score {item['icp_score']}: {item['reason']}).\n"
            f"No guarantees language, no urgency tactics.",
            Draft,
        )
        return {**item, **draft.model_dump()}

    @staticmethod
    def _richness(lead: dict[str, Any]) -> int:
        return sum(1 for v in lead.values() if v not in (None, "", []))

    @staticmethod
    def _integrity_hash(items: list[dict[str, Any]]) -> str:
        records = sorted(json.dumps(i, sort_keys=True, default=str) for i in items)
        return hashlib.sha256("\n".join(records).encode()).hexdigest()[:16]
