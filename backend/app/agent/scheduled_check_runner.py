"""
"Check This Tomorrow" background execution (decisions.md §14 — backend
scheduling/re-check infrastructure; the proactive WhatsApp SEND itself
remains blocked, see app/agent/notifications.py).

**Reuses the existing text-claim investigation pipeline directly** —
`app.agent.claim_pipeline_shared.investigate_claim`, the exact function
TextPipeline already calls for a brand-new claim — rather than building a
second evidence-retrieval/classification system. A re-check IS a fresh
investigation of `claim_text_snapshot`; it is not a replay of the original
analysis (decisions.md §14: "Evidence is re-checked at execution time — the
scheduled job re-runs evidence retrieval, it does not just replay the
original result").

Respects the same cost/abuse controls every other pipeline already has:
the daily circuit breaker (`is_circuit_breaker_tripped`) is checked before
each batch — if tripped, the whole batch is skipped for this tick (rows
stay `pending`, picked up on a later tick), never silently discarded. Each
individual check still gets its own `AnalysisBudgetGuard`, identical to a
live TextPipeline run.

**Provider/timeout failures are NOT what `status='failed'` means.**
`investigate_claim`/`run_investigation_loop` already absorb
`LLMProviderError`/`LLMProviderTimeout`/`BudgetExceededError` internally and
turn them into a normal `insufficient_evidence` `ClaimVerdict`
(decisions.md §1A — a provider hiccup is a first-class non-definitive
RESULT, never a pipeline-level failure). A scheduled check whose re-check
hits a provider timeout therefore completes normally with
`new_result_snapshot.result == 'insufficient_evidence'`, same as any other
pipeline. `status='failed'` (with retry up to `max_attempts`, see
`_handle_failure` below) is reserved for a genuinely unexpected
infrastructure crash — a bug, a DB error — that reaches the generic
exception handler in `_execute_one`.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget_guard import AnalysisBudgetGuard
from app.agent.claim_pipeline_shared import investigate_claim
from app.agent.notifications import notify_scheduled_check_failed, notify_scheduled_check_result
from app.agent.tools.evidence_search import EvidenceSearchTool
from app.agent.tools.evidence_synthesis import EvidenceSynthesisTool
from app.agent.tools.source_evaluation import SourceEvaluationTool
from app.agent.usage_tracking import is_circuit_breaker_tripped
from app.core.config import Settings
from app.core.logging import log_event
from app.integrations.claude_client import LLMClient
from app.models.scheduled_check import ScheduledCheck

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10


async def _execute_one(
    session: AsyncSession,
    settings: Settings,
    llm: LLMClient,
    search_provider: object,
    check: ScheduledCheck,
) -> None:
    check.status = "running"
    check.attempts += 1
    await session.flush()

    budget_guard = AnalysisBudgetGuard(
        settings.max_evidence_searches_per_analysis, settings.max_llm_tool_calls_per_analysis
    )
    source_evaluator = SourceEvaluationTool(session)
    evidence_search_tool = EvidenceSearchTool(search_provider, source_evaluator)
    synthesis_tool = EvidenceSynthesisTool(llm)

    try:
        verdict = await investigate_claim(
            llm,
            settings,
            check.claim_text_snapshot,
            check.category or "other",
            budget_guard,
            evidence_search_tool,
            synthesis_tool,
            check.language,
        )
    except Exception as exc:  # noqa: BLE001 - one bad check must never crash the whole batch/task
        # NOTE: investigate_claim/run_investigation_loop already absorb
        # LLMProviderError/LLMProviderTimeout/BudgetExceededError internally
        # and turn them into a normal `insufficient_evidence` ClaimVerdict
        # (decisions.md §1A — a provider hiccup is never a pipeline-level
        # failure, it's a first-class non-definitive RESULT). Anything that
        # reaches this except block is therefore a genuine, unexpected
        # infrastructure crash (a bug, a DB error), not a routine provider
        # failure — `scheduled_checks.status='failed'` means THAT, not "the
        # evidence search timed out" (which instead completes normally with
        # new_result_snapshot.result == 'insufficient_evidence').
        log_event(logger, logging.ERROR, "scheduled check crashed unexpectedly", check_id=str(check.id))
        await _handle_failure(session, check, exc)
        return

    previous_result = None
    if check.previous_result_snapshot:
        previous_result = check.previous_result_snapshot.get("result")

    new_result_snapshot = {
        "result": verdict.result,
        "reasoning_text": verdict.reasoning_text,
        "claim_confidence": verdict.claim_confidence,
        "evidence_strength": verdict.evidence_strength,
        "evidence_tier_met": verdict.evidence_tier_met,
        "investigation_complete": verdict.investigation_complete,
        "incomplete_reason": verdict.incomplete_reason,
    }
    credibility_changed = previous_result is not None and previous_result != verdict.result

    check.new_result_snapshot = new_result_snapshot
    check.credibility_changed = credibility_changed
    check.status = "completed"
    check.executed_at = datetime.now(timezone.utc)
    check.error_message = None
    await session.flush()

    notifications = await notify_scheduled_check_result(
        session,
        user_id=check.user_id,
        scheduled_check_id=check.id,
        claim_text=check.claim_text_snapshot,
        language=check.language,
        credibility_changed=credibility_changed,
        old_result=previous_result,
        new_result=verdict.result,
    )
    check.notification_status = "generated" if notifications else "not_sent"
    await session.flush()


async def _handle_failure(session: AsyncSession, check: ScheduledCheck, exc: Exception) -> None:
    check.error_message = str(exc)[:500]
    if check.attempts >= check.max_attempts:
        check.status = "failed"
        await session.flush()
        notification = await notify_scheduled_check_failed(
            session,
            user_id=check.user_id,
            scheduled_check_id=check.id,
            claim_text=check.claim_text_snapshot,
            language=check.language,
        )
        check.notification_status = "generated" if notification else "not_sent"
    else:
        # Leave it 'pending' — picked up again on a later tick, up to
        # max_attempts total. Never silently drops a retriable failure.
        check.status = "pending"
    await session.flush()


async def run_due_scheduled_checks(
    session: AsyncSession, settings: Settings, llm: LLMClient, search_provider: object, now: datetime | None = None
) -> int:
    """Called by the periodic background task (main.py's lifespan, same
    in-process asyncio pattern as the retention purge — NOT Celery, per the
    user's explicit instruction to reuse existing background-job
    infrastructure rather than introduce another scheduler)."""
    now = now or datetime.now(timezone.utc)

    if await is_circuit_breaker_tripped(session, settings.daily_spend_circuit_breaker_usd):
        log_event(logger, logging.WARNING, "scheduled-check batch skipped: daily circuit breaker tripped")
        return 0

    result = await session.execute(
        select(ScheduledCheck)
        .where(ScheduledCheck.status == "pending", ScheduledCheck.scheduled_for <= now)
        .order_by(ScheduledCheck.scheduled_for)
        .limit(_BATCH_SIZE)
    )
    due_checks = result.scalars().all()

    for check in due_checks:
        await _execute_one(session, settings, llm, search_provider, check)

    await session.commit()
    return len(due_checks)
