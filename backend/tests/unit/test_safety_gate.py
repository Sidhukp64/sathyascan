"""
Safety gate tests (app/agent/safety_gate.py) — non-bypassable, fails closed
on any provider error, never persists/returns the checked content itself.
"""

import pytest

from app.agent.safety_gate import NullSafetyProvider, SafetyCheckResult, SafetyGate, SafetyOutcome


class _RaisingProvider:
    provider_name = "raising_test_provider"

    async def check(self, media_bytes: bytes, mime_type: str) -> SafetyCheckResult:
        raise RuntimeError("simulated provider crash")


class _BlockingProvider:
    provider_name = "blocking_test_provider"

    async def check(self, media_bytes: bytes, mime_type: str) -> SafetyCheckResult:
        return SafetyCheckResult(
            outcome=SafetyOutcome.BLOCKED, check_type="content_classifier", provider_name=self.provider_name
        )


class TestSafetyGateSafeInput:
    async def test_passes_on_safe_content(self):
        gate = SafetyGate(NullSafetyProvider())
        result = await gate.check(b"harmless bytes", "image/jpeg")
        assert result.outcome == SafetyOutcome.PASSED
        assert SafetyGate.may_proceed(result) is True


class TestSafetyGateRejectedInput:
    async def test_blocked_outcome_prevents_proceeding(self):
        gate = SafetyGate(_BlockingProvider())
        result = await gate.check(b"some bytes", "image/jpeg")
        assert result.outcome == SafetyOutcome.BLOCKED
        assert SafetyGate.may_proceed(result) is False


class TestSafetyGateProviderFailure:
    async def test_provider_exception_is_caught_not_propagated(self):
        gate = SafetyGate(_RaisingProvider())
        # Must not raise — the gate itself never lets a provider crash
        # propagate up into the pipeline.
        result = await gate.check(b"some bytes", "image/jpeg")
        assert result.outcome == SafetyOutcome.ERROR

    async def test_error_outcome_prevents_proceeding_fail_closed(self):
        gate = SafetyGate(_RaisingProvider())
        result = await gate.check(b"some bytes", "image/jpeg")
        # This is the core fail-closed requirement: ERROR is treated
        # identically to BLOCKED, never as "proceed anyway".
        assert SafetyGate.may_proceed(result) is False
        assert SafetyGate.may_proceed(SafetyCheckResult(outcome=SafetyOutcome.BLOCKED, check_type="other", provider_name=None)) is False


class TestSafetyGateFailClosedBehavior:
    @pytest.mark.parametrize("outcome", [SafetyOutcome.BLOCKED, SafetyOutcome.ERROR])
    def test_only_passed_allows_proceeding(self, outcome):
        result = SafetyCheckResult(outcome=outcome, check_type="other", provider_name="x")
        assert SafetyGate.may_proceed(result) is False

    def test_passed_is_the_only_proceed_outcome(self):
        result = SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="x")
        assert SafetyGate.may_proceed(result) is True


class TestSafetyGateNoContentPersistence:
    async def test_result_never_contains_the_checked_bytes(self):
        """The result object structurally cannot carry the media content —
        SafetyCheckResult has no bytes-typed field at all, only
        outcome/check_type/provider_name/reference_id (a vendor case ID at
        most, never content)."""
        gate = SafetyGate(NullSafetyProvider())
        secret_bytes = b"THIS_MUST_NEVER_APPEAR_ANYWHERE_IN_THE_RESULT"
        result = await gate.check(secret_bytes, "image/jpeg")

        result_fields = vars(result)
        for value in result_fields.values():
            assert value != secret_bytes
            if isinstance(value, str):
                assert b"THIS_MUST_NEVER_APPEAR" not in value.encode()

    async def test_null_provider_reference_id_is_never_set_to_content(self):
        gate = SafetyGate(NullSafetyProvider())
        result = await gate.check(b"any content", "image/png")
        assert result.reference_id is None
