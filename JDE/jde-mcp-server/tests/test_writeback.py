"""Write-back validation and staging tests.

The critical property under test: nothing reaches JD Edwards without a human.
``stage`` may only ever produce a draft; ``commit_draft`` is not an MCP tool and
is exercised here only through the service, as the approval CLI would.
"""

from pathlib import Path

import pytest

from jde_mcp.audit import AuditLog
from jde_mcp.semantic import SemanticModel
from jde_mcp.writeback import (
    ValidationError,
    WritebackService,
    validate_ap_voucher,
    validate_journal_entry,
)

OBJECTS = Path(__file__).resolve().parent.parent / "config" / "objects.yaml"


@pytest.fixture(scope="module")
def model():
    return SemanticModel.load(OBJECTS)


@pytest.fixture
def audit(tmp_path):
    log = AuditLog(tmp_path / "state.db")
    yield log
    log.close()


BALANCED = [
    {"account": "1.1110.BEAR", "amount": 1500.00},
    {"account": "1.5010.BEAR", "amount": -1500.00},
]


class TestJournalValidation:
    def test_balanced_entry_passes(self):
        assert validate_journal_entry("00100", "2026-07-27", BALANCED) == []

    def test_unbalanced_entry_rejected(self):
        lines = [{"account": "1.1110", "amount": 1500.00},
                 {"account": "1.5010", "amount": -1000.00}]
        errors = validate_journal_entry("00100", "2026-07-27", lines)
        assert any("does not balance" in e for e in errors)

    def test_rounding_tolerance(self):
        # Sub-cent float noise must not be reported as an imbalance.
        lines = [{"account": "a", "amount": 0.1 + 0.2},
                 {"account": "b", "amount": -0.3}]
        assert validate_journal_entry("00100", "2026-07-27", lines) == []

    def test_missing_company(self):
        errors = validate_journal_entry("", "2026-07-27", BALANCED)
        assert any("company" in e for e in errors)

    def test_bad_date_format(self):
        errors = validate_journal_entry("00100", "27/07/2026", BALANCED)
        assert any("gl_date" in e for e in errors)

    def test_no_lines(self):
        errors = validate_journal_entry("00100", "2026-07-27", [])
        assert any("at least one line" in e for e in errors)

    def test_line_missing_account(self):
        errors = validate_journal_entry(
            "00100", "2026-07-27",
            [{"amount": 100.0}, {"account": "b", "amount": -100.0}])
        assert any("'account' is required" in e for e in errors)

    def test_non_numeric_amount(self):
        errors = validate_journal_entry(
            "00100", "2026-07-27",
            [{"account": "a", "amount": "lots"}, {"account": "b", "amount": -100.0}])
        assert any("must be numeric" in e for e in errors)


class TestVoucherValidation:
    def test_valid(self):
        assert validate_ap_voucher(4242, "00100", "2026-07-27", 1234.56) == []

    def test_zero_amount_rejected(self):
        errors = validate_ap_voucher(4242, "00100", "2026-07-27", 0)
        assert any("gross_amount" in e for e in errors)

    def test_missing_supplier(self):
        errors = validate_ap_voucher(0, "00100", "2026-07-27", 100)
        assert any("supplier_number" in e for e in errors)


class TestStaging:
    def test_read_only_refuses_to_stage(self, model, audit):
        svc = WritebackService(None, model, audit, read_only=True)
        result = svc.stage("journal_entry", {"a": 1}, "test", errors=[])
        assert result["status"] == "refused"
        assert audit.list_drafts() == []

    def test_validation_errors_block_staging(self, model, audit):
        svc = WritebackService(None, model, audit, read_only=False)
        result = svc.stage("journal_entry", {"a": 1}, "test",
                           errors=["does not balance"])
        assert result["status"] == "rejected"
        assert audit.list_drafts() == []

    def test_valid_write_is_staged_not_posted(self, model, audit):
        svc = WritebackService(None, model, audit, read_only=False)
        payload = {"company": "00100", "gl_date": "2026-07-27", "lines": BALANCED}
        result = svc.stage("journal_entry", payload, "July accrual", errors=[])

        assert result["status"] == "pending_approval"
        assert result["draft_id"].startswith("draft-")
        # The orchestration name is reported but has not been invoked.
        assert result["will_post_via"] == "JDE_CreateJournalEntry"

        drafts = audit.list_drafts()
        assert len(drafts) == 1
        assert drafts[0]["status"] == "pending_approval"

    def test_unconfigured_target_refuses(self, model, audit):
        svc = WritebackService(None, model, audit, read_only=False)
        with pytest.raises(Exception):
            svc.stage("wire_transfer", {"amount": 1_000_000}, "nope", errors=[])

    def test_commit_unknown_draft_raises(self, model, audit):
        svc = WritebackService(None, model, audit, read_only=False)
        with pytest.raises(ValidationError):
            svc.commit_draft("draft-doesnotexist", "Sarah K.")

    def test_reject_marks_draft(self, model, audit):
        svc = WritebackService(None, model, audit, read_only=False)
        staged = svc.stage("journal_entry",
                           {"company": "00100", "gl_date": "2026-07-27",
                            "lines": BALANCED},
                           "test", errors=[])
        svc.reject_draft(staged["draft_id"], "Sarah K.", "wrong cost centre")
        assert audit.list_drafts(status="pending_approval") == []
        assert audit.get_draft(staged["draft_id"])["status"] == "rejected"

    def test_double_commit_blocked(self, model, audit):
        svc = WritebackService(None, model, audit, read_only=False)
        staged = svc.stage("journal_entry",
                           {"company": "00100", "gl_date": "2026-07-27",
                            "lines": BALANCED},
                           "test", errors=[])
        audit.decide_draft(staged["draft_id"], "posted", "Sarah K.", "ok")
        with pytest.raises(ValidationError):
            svc.commit_draft(staged["draft_id"], "Sarah K.")


class TestAudit:
    def test_reads_are_logged(self, audit):
        audit.record("get_open_ap_vouchers", "read",
                     {"company": "00100"}, row_count=12)
        entries = audit.recent()
        assert entries[0]["tool"] == "get_open_ap_vouchers"
        assert entries[0]["row_count"] == 12

    def test_credentials_are_redacted(self, audit):
        audit.record("login", "auth", {"username": "SVC", "password": "hunter2"})
        # Confirm the secret never reached durable storage.
        raw = audit._conn.execute(
            "SELECT arguments FROM audit_log ORDER BY ts DESC LIMIT 1"
        ).fetchone()[0]
        assert "hunter2" not in raw
        assert "redacted" in raw

    def test_logging_failure_does_not_raise(self, audit):
        audit.close()
        # A broken audit sink must not take down a tool call.
        assert audit.record("tool", "action") is not None
