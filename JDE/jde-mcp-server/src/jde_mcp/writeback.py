"""Write-back: validate, stage, and (separately) commit.

Two rules make this safe enough to put in front of a real ledger:

1. Nothing is written directly to an F-table. Commits go through a published
   orchestration, which calls the JDE business function, so posting rules,
   validations and audit triggers all still fire.

2. The agent drafts; a human commits. ``stage_*`` is exposed as an MCP tool.
   ``commit_draft`` deliberately is NOT — it is called by your approval UI or
   the bundled CLI, so no sequence of prompts can cause a posting.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .ais import AISSession
from .audit import AuditLog
from .semantic import SemanticModel


class ValidationError(ValueError):
    """The proposed write is not internally consistent; nothing was staged."""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_journal_entry(company: str, gl_date: str,
                           lines: list[dict[str, Any]]) -> list[str]:
    """Catch the errors that JDE would reject anyway, but cheaply and with a
    message the model can act on instead of a business-function error code."""
    errors: list[str] = []

    if not company or not str(company).strip():
        errors.append("company is required")

    try:
        date.fromisoformat(gl_date)
    except (TypeError, ValueError):
        errors.append(f"gl_date must be an ISO date (YYYY-MM-DD), got {gl_date!r}")

    if not lines:
        errors.append("at least one line is required")
        return errors

    total = 0.0
    for i, line in enumerate(lines, start=1):
        if "account" not in line or not str(line.get("account", "")).strip():
            errors.append(f"line {i}: 'account' is required")
        amount = line.get("amount")
        if amount is None:
            errors.append(f"line {i}: 'amount' is required")
            continue
        try:
            total += float(amount)
        except (TypeError, ValueError):
            errors.append(f"line {i}: 'amount' must be numeric, got {amount!r}")

    # Debits positive, credits negative — a balanced JE sums to zero.
    if abs(total) > 0.005:
        errors.append(
            f"entry does not balance: debits minus credits = {total:.2f}. "
            "Debits are positive, credits negative."
        )
    return errors


def validate_ap_voucher(supplier_number: int, company: str, gl_date: str,
                        gross_amount: float) -> list[str]:
    errors: list[str] = []
    if not supplier_number:
        errors.append("supplier_number is required")
    if not company or not str(company).strip():
        errors.append("company is required")
    try:
        date.fromisoformat(gl_date)
    except (TypeError, ValueError):
        errors.append(f"gl_date must be an ISO date (YYYY-MM-DD), got {gl_date!r}")
    if gross_amount is None or float(gross_amount) == 0:
        errors.append("gross_amount must be non-zero")
    return errors


# --------------------------------------------------------------------------
# Staging and commit
# --------------------------------------------------------------------------

class WritebackService:
    def __init__(self, ais: AISSession, model: SemanticModel, audit: AuditLog,
                 read_only: bool = True):
        self.ais = ais
        self.model = model
        self.audit = audit
        self.read_only = read_only

    def stage(self, target: str, payload: dict[str, Any], explanation: str,
              errors: list[str]) -> dict[str, Any]:
        """Common staging path. Returns a result dict the tool can hand back."""
        if self.read_only:
            return {
                "status": "refused",
                "reason": "Server is running in read-only mode "
                          "(JDE_READ_ONLY=true). No draft was created.",
            }

        if errors:
            self.audit.record("writeback", f"stage_{target}", payload,
                              status="rejected", detail="; ".join(errors))
            return {"status": "rejected", "errors": errors}

        # Confirms an orchestration is actually configured for this target
        # before we promise the user something can be posted.
        route = self.model.write_target(target)

        draft_id = self.audit.save_draft(target, payload, explanation)
        self.audit.record("writeback", f"stage_{target}", payload,
                          status="staged", detail=draft_id)
        return {
            "status": "pending_approval",
            "draft_id": draft_id,
            "target": target,
            "will_post_via": route.orchestration,
            "note": "Nothing has been written to JD Edwards. A human must "
                    "approve this draft before it posts.",
            "payload": payload,
        }

    # NOT exposed as an MCP tool — called by the approval CLI / UI only.
    def commit_draft(self, draft_id: str, approved_by: str) -> dict[str, Any]:
        draft = self.audit.get_draft(draft_id)
        if not draft:
            raise ValidationError(f"no such draft: {draft_id}")
        if draft["status"] != "pending_approval":
            raise ValidationError(
                f"draft {draft_id} is '{draft['status']}', not pending approval"
            )

        route = self.model.write_target(draft["target"])
        try:
            response = self.ais.run_orchestration(route.orchestration,
                                                  draft["payload"])
        except Exception as exc:
            self.audit.decide_draft(draft_id, "failed", approved_by, str(exc))
            self.audit.record("writeback", "commit", {"draft_id": draft_id},
                              status="error", detail=str(exc), actor=approved_by)
            raise

        self.audit.decide_draft(draft_id, "posted", approved_by,
                                str(response)[:2000])
        self.audit.record("writeback", "commit", {"draft_id": draft_id},
                          status="posted", actor=approved_by)
        return {"status": "posted", "draft_id": draft_id,
                "orchestration": route.orchestration, "response": response}

    def reject_draft(self, draft_id: str, rejected_by: str,
                     reason: str = "") -> dict[str, Any]:
        draft = self.audit.get_draft(draft_id)
        if not draft:
            raise ValidationError(f"no such draft: {draft_id}")
        self.audit.decide_draft(draft_id, "rejected", rejected_by, reason)
        return {"status": "rejected", "draft_id": draft_id, "reason": reason}
