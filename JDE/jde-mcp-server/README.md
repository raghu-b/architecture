# JDE MCP Server

An MCP server that exposes JD Edwards EnterpriseOne finance data to Claude,
through the AIS REST API. Plugs into VS Code, Claude Desktop, or any MCP client.

This is the same architecture commercial products like ChatFin use: a semantic
layer over JDE's tables, session-aware AIS authentication, security inherited
from a service account, and write-back through business functions with human
approval — not direct table writes.

**Status:** working reference implementation. The column mappings in
`config/objects.yaml` are standard for a vanilla install but **must be verified
against your own environment** before you trust the output.

---

## What you get

| Layer | File | What it solves |
|---|---|---|
| AIS session | `src/jde_mcp/ais.py` | Token lifetime, stable device name, one-shot re-auth retry |
| Conversions | `src/jde_mcp/convert.py` | Julian dates (CYYDDD), implied decimals |
| Semantic model | `config/objects.yaml` | F0411 → "AP voucher", column → business field |
| Query builder | `src/jde_mcp/query.py` | Business filters → AIS conditions; response normalization |
| MCP tools | `src/jde_mcp/server.py` | 14 business-shaped tools |
| Write-back | `src/jde_mcp/writeback.py` | Validation, draft staging, orchestration commit |
| Audit | `src/jde_mcp/audit.py` | Independent log of every read and write |
| Approval CLI | `src/jde_mcp/approve.py` | Human commit path, deliberately not an MCP tool |

### Tools exposed

**Discovery** — `list_business_objects`, `describe_business_object`, `check_connection`

**Read** — `get_open_ap_vouchers`, `get_ap_aging_summary`, `get_open_ar_invoices`,
`get_gl_transactions`, `get_account_balance`, `get_purchase_orders`,
`lookup_address_book`

**Write (staged only)** — `draft_journal_entry`, `draft_ap_voucher`,
`list_pending_drafts`

**Governance** — `get_audit_trail`

---

## Setup

### 1. Provision a JDE service account

Create a dedicated E1 user whose row and column security profile is exactly what
you want agents to have. **This account is the hard ceiling** — no prompt can
make an agent see past it. Start read-only against a narrow set of tables.

### 2. Install

```bash
git clone <your-repo> jde-mcp-server && cd jde-mcp-server
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```bash
JDE_AIS_URL=https://ais.yourcompany.com:9302
JDE_USERNAME=SVC_CLAUDE
JDE_PASSWORD=...
JDE_READ_ONLY=true      # leave true until writeback is reviewed
JDE_MAX_ROWS=500
```

### 4. Verify

```bash
pytest -q                                    # 86 tests, no JDE needed
python -c "from jde_mcp.server import mcp; print('ok')"
```

---

## Connecting to VS Code

`.vscode/mcp.json` ships ready to use. Open the folder in VS Code, then run
**MCP: List Servers** from the Command Palette and start `jde-finance`. It will
prompt for the AIS URL, username and password on first run.

To store credentials in `.env` instead of prompting, delete the three
`${input:...}` lines from `.vscode/mcp.json` — `config.py` loads `.env`
automatically.

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jde-finance": {
      "command": "/absolute/path/to/jde-mcp-server/.venv/bin/python",
      "args": ["-m", "jde_mcp.server"],
      "cwd": "/absolute/path/to/jde-mcp-server",
      "env": {
        "PYTHONPATH": "/absolute/path/to/jde-mcp-server/src"
      }
    }
  }
}
```

Use absolute paths — Claude Desktop does not expand `${workspaceFolder}`.

---

## Adapting it to your environment

### Verify the column mappings first

`config/objects.yaml` maps business fields to JDE columns. These are standard
aliases, but custom fields and localisations vary by install. Check anything
you're unsure of in Data Dictionary (**P92001**) or Table Design Aid before
trusting a number that came out of this server.

### Add a business object

```yaml
objects:
  work_order:
    table: F4801
    description: >
      Work Order Master. One row per work order header.
    fields:
      order_number:  { column: WADOCO, type: numeric,     label: Work order number }
      description:   { column: WADL01, type: string,      label: Description }
      status:        { column: WASRST, type: string,      label: Status code }
      start_date:    { column: WASTRT, type: julian_date, label: Planned start }
      estimated_cost:{ column: WAAMTO, type: currency,    label: Estimated cost }
```

Field types drive conversion automatically: `julian_date` → ISO string,
`currency` → implied decimals applied, `string` → padding stripped. No code
change needed for reads via `_run_read`.

### Add a tool

```python
@mcp.tool()
def get_open_work_orders(business_unit: str | None = None,
                         limit: int = 100) -> dict:
    """Open work orders from F4801.

    The docstring is what Claude reads to decide when to call this and how to
    fill the arguments — write it for that audience, not for a developer.
    """
    filters = {"status": {"ne": "99"}}
    if business_unit:
        filters["business_unit"] = {"eq": business_unit}
    return _run_read("get_open_work_orders", "work_order", filters, limit)
```

Prefer narrow, business-shaped tools over a generic `run_query`. A SQL
passthrough gives the model more rope than it needs and gives your auditors
nothing reviewable.

---

## Write-back

Writes are two-phase and cannot be completed by the model alone.

```
Claude calls draft_journal_entry(...)
  → validated (balance check, date format, required fields)
  → staged as draft-a1b2c3, nothing sent to JDE
  → human runs: python -m jde_mcp.approve approve draft-a1b2c3 --by "Sarah K."
  → posts via the orchestration named in objects.yaml
  → orchestration calls the JDE business function
  → JDE's own validations, posting rules and audit triggers all fire
```

`commit_draft` is **not** an MCP tool. It lives in `approve.py` behind a CLI so
no sequence of prompts can cause a posting.

Before enabling writes:

1. Publish the orchestrations named under `writeback:` in `objects.yaml`
   (`JDE_CreateJournalEntry`, `JDE_CreateAPVoucher`) in Orchestrator Studio,
   each calling the appropriate business function.
2. Set `JDE_READ_ONLY=false`.
3. Replace the CLI with your real approval workflow for anything beyond a pilot.

```bash
python -m jde_mcp.approve --list
python -m jde_mcp.approve show    draft-a1b2c3d4e5f6
python -m jde_mcp.approve approve draft-a1b2c3d4e5f6 --by "Sarah K."
python -m jde_mcp.approve reject  draft-a1b2c3d4e5f6 --by "Sarah K." --reason "wrong cost centre"
```

---

## Security notes

**Inherited permissions.** Everything runs under the JDE service account and
honours its row/column security. Scope that account, not this code.

**No direct table writes.** Bypassing business functions skips posting rules and
validations, producing data that looks fine until close.

**Row caps.** `JDE_MAX_ROWS` is enforced server-side regardless of what the model
requests, protecting both the context window and the AIS server.

**Independent audit trail.** JDE logs writes but not reads. When an auditor asks
what the AI had access to, only your own log can answer. Credentials are redacted
before anything is persisted.

**Per-user identity.** This reference uses one shared service account. For a team
deployment, map each human user to their own JDE credentials — retrofitting that
later is painful.

**Never commit `.env`.** It's in `.gitignore`; keep it that way.

---

## Deliberate limitations

- **Read-only by default.** `JDE_READ_ONLY=true` until you decide otherwise.
- **Five business objects.** AP, AR, GL, PO, Address Book — enough to be useful,
  small enough to verify. Extend via `objects.yaml`.
- **No JDBC path.** Direct database reads are faster but bypass row/column
  security, which is what kills these projects at audit review. If you add one,
  replicate the security filter yourself.
- **Column aliases are unverified against your install.** See above.
- **SQLite state.** Fine for single-user. Move to Postgres for a shared server.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `AIS rejected the service account credentials` | Wrong username/password, or the account is locked/expired |
| `cannot reach AIS server` | Wrong `JDE_AIS_URL`, firewall, or TLS. Confirm the URL has no `/jderest` suffix |
| Empty results, no error | Filters too narrow, or the column alias in `objects.yaml` doesn't exist in your install — verify in P92001 |
| Amounts off by 100× | `decimals` in `objects.yaml` doesn't match the currency's decimal places |
| Dates wildly wrong | Field typed `numeric` instead of `julian_date` |
| Server won't start in VS Code | Check `PYTHONPATH` points at `src/` and the venv path in `mcp.json` is correct |
| `Semantic model not loaded` | Set `JDE_OBJECTS_FILE` or launch from the package root |

Server logs go to stderr (stdout is the MCP transport). In VS Code: **Output →
MCP**.

---

## Testing

```bash
pytest -q                      # 86 tests, all offline
pytest --cov=jde_mcp           # with coverage
```

Tests use a canned AIS response fixture, so the suite runs with no JDE
connection. The conversion tests are the highest-value ones: a Julian date or
implied decimal handled wrong produces output that is confidently, plausibly
incorrect — far more dangerous in finance than an outright error.

---

## License

Provided as a reference implementation. Review and adapt before production use.
