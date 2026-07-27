"""Human approval CLI.

Deliberately not an MCP tool. Committing a draft is the one action that must be
impossible to reach through the model, so it lives behind a separate entry point
that a person runs. In a team deployment, replace this with your approval UI —
the important property is that ``commit_draft`` is never reachable from a prompt.

    python -m jde_mcp.approve --list
    python -m jde_mcp.approve show draft-a1b2c3d4e5f6
    python -m jde_mcp.approve approve draft-a1b2c3d4e5f6 --by "Sarah K."
    python -m jde_mcp.approve reject  draft-a1b2c3d4e5f6 --by "Sarah K." --reason "wrong cost centre"
"""

from __future__ import annotations

import argparse
import json
import sys

from .ais import AISSession
from .audit import AuditLog
from .config import Settings
from .semantic import SemanticModel
from .writeback import WritebackService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jde_mcp.approve",
                                     description="Review and commit JDE write drafts")
    parser.add_argument("--list", action="store_true", help="list pending drafts")
    sub = parser.add_subparsers(dest="command")

    p_show = sub.add_parser("show", help="show a draft in full")
    p_show.add_argument("draft_id")

    p_ok = sub.add_parser("approve", help="approve and post a draft")
    p_ok.add_argument("draft_id")
    p_ok.add_argument("--by", required=True, help="name of the approver")

    p_no = sub.add_parser("reject", help="reject a draft")
    p_no.add_argument("draft_id")
    p_no.add_argument("--by", required=True, help="name of the reviewer")
    p_no.add_argument("--reason", default="")

    args = parser.parse_args(argv)

    settings = Settings.load()
    audit = AuditLog(settings.state_db)

    if args.list or not args.command:
        drafts = audit.list_drafts()
        if not drafts:
            print("No drafts pending approval.")
            return 0
        print(f"{len(drafts)} draft(s) pending approval:\n")
        for d in drafts:
            print(f"  {d['draft_id']}  {d['target']:<15} {d['ts']}")
            print(f"      {d['explanation']}")
        return 0

    if args.command == "show":
        draft = audit.get_draft(args.draft_id)
        if not draft:
            print(f"No such draft: {args.draft_id}", file=sys.stderr)
            return 1
        print(json.dumps(draft, indent=2, default=str))
        return 0

    model = SemanticModel.load(settings.objects_file)
    ais = AISSession(settings)
    # read_only is irrelevant here: this path is a human action, not an agent one
    service = WritebackService(ais, model, audit, read_only=False)

    try:
        if args.command == "approve":
            draft = audit.get_draft(args.draft_id)
            if not draft:
                print(f"No such draft: {args.draft_id}", file=sys.stderr)
                return 1
            print(json.dumps(draft["payload"], indent=2, default=str))
            confirm = input(f"\nPost this to JD Edwards as {args.by}? [y/N] ")
            if confirm.strip().lower() != "y":
                print("Aborted. Nothing was posted.")
                return 1
            result = service.commit_draft(args.draft_id, args.by)
            print(json.dumps(result, indent=2, default=str))
            return 0

        if args.command == "reject":
            result = service.reject_draft(args.draft_id, args.by, args.reason)
            print(json.dumps(result, indent=2, default=str))
            return 0
    finally:
        ais.close()
        audit.close()

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
