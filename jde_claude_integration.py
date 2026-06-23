"""
jde_claude_integration.py
═══════════════════════════════════════════════════════════════════
Complete integration: Claude AI  ↔  JDE EnterpriseOne via AIS Orchestrations

Architecture:
    User → FastAPI service → Claude (reasoning + tool use) → JDE AIS → JDE E1

Setup:
    pip install anthropic fastapi uvicorn requests python-dotenv

Environment variables (create a .env file):
    ANTHROPIC_API_KEY = sk-ant-...
    JDE_AIS_BASE      = https://jde.yourcompany.com/jderest/v3
    JDE_USERNAME      = JDEUSER
    JDE_PASSWORD      = your_password

Run:
    uvicorn jde_claude_integration:app --reload --port 8000

Endpoints:
    POST /api/chat           – main chat endpoint
    GET  /api/health         – health check
    GET  /api/orchestrations – list published JDE Orchestrations
═══════════════════════════════════════════════════════════════════
"""

import os
import time
import json
import logging
import threading
from typing import Optional

import anthropic
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Optional: load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s – %(message)s",
)
log = logging.getLogger("jde_claude")


# ═══════════════════════════════════════════════════════════════════
# 1. CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

class Config:
    # JDE AIS server
    AIS_BASE      = os.getenv("JDE_AIS_BASE",   "https://jde.yourcompany.com/jderest/v3")
    JDE_USER      = os.getenv("JDE_USERNAME",   "JDEUSER")
    JDE_PASSWORD  = os.getenv("JDE_PASSWORD",   "")

    # Claude model
    CLAUDE_MODEL  = "claude-sonnet-4-6"
    MAX_TOKENS    = 2048
    MAX_TOOL_ITER = 5           # safety cap on tool-call iterations per turn

    # AIS tokens expire after ~30 min; refresh 2 min early
    TOKEN_TTL     = 1680


# ═══════════════════════════════════════════════════════════════════
# 2. JDE AUTH MANAGER  (thread-safe, auto-refresh)
# ═══════════════════════════════════════════════════════════════════

class JDEAuthManager:
    """Maintains a single AIS session token and refreshes it automatically."""

    def __init__(self):
        self._token:      Optional[str] = None
        self._expires_at: float         = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if time.time() >= self._expires_at:
                self._refresh()
            return self._token

    def _refresh(self) -> None:
        resp = requests.post(
            f"{Config.AIS_BASE}/tokenrequest",
            json={
                "username":             Config.JDE_USER,
                "password":             Config.JDE_PASSWORD,
                "deviceName":           "claude_integration",
                "requiredCapabilities": "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "userInfo" not in data:
            raise RuntimeError(f"AIS auth failed: {data}")
        self._token      = data["userInfo"]["token"]
        self._expires_at = time.time() + Config.TOKEN_TTL
        log.info("JDE AIS token refreshed")


auth = JDEAuthManager()


# ═══════════════════════════════════════════════════════════════════
# 3. JDE API CLIENT
# ═══════════════════════════════════════════════════════════════════

def call_orchestration(name: str, inputs: dict) -> dict:
    """
    Call a published JDE Orchestration by name via AIS REST.

    Args:
        name:   Orchestration name (as published in Orchestrator Studio)
        inputs: Dict matching the Orchestration's input aliases

    Returns:
        The 'output' block from the AIS response

    Raises:
        ValueError:  JDE hard error (severity E)
        HTTPError:   AIS server communication error
    """
    token = auth.get_token()
    resp  = requests.post(
        f"{Config.AIS_BASE}/orchestrator/{name}",
        headers={
            "Authorization": token,
            "Content-Type":  "application/json",
        },
        json={"inputs": inputs},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # Hard errors (JDE severity prefix "E") must abort the call
    hard_errors = [
        e for e in data.get("errors", [])
        if str(e.get("CAUSE", "")).startswith("E")
    ]
    if hard_errors:
        raise ValueError(f"JDE error [{name}]: {hard_errors[0]['CAUSE']}")

    # Log warnings but continue
    for w in data.get("warnings", []):
        log.warning("JDE warning [%s]: %s", name, w.get("CAUSE", w))

    return data.get("output", data)


def list_orchestrations() -> list:
    """Return all Orchestrations published on the AIS server."""
    token = auth.get_token()
    resp  = requests.get(
        f"{Config.AIS_BASE}/orchestratorstudio/listorchestrations",
        headers={"Authorization": token},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("orchestrations", [])


def get_orchestration_schema(name: str) -> dict:
    """Fetch the JSON definition of a specific Orchestration."""
    token = auth.get_token()
    resp  = requests.get(
        f"{Config.AIS_BASE}/orchestratorstudio/orchestration/{name}",
        headers={"Authorization": token},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ═══════════════════════════════════════════════════════════════════
# 4. JDE KNOWLEDGE CONTEXT  (system prompt – prompt-cached)
# ═══════════════════════════════════════════════════════════════════

JDE_SYSTEM_PROMPT = """You are a JDE EnterpriseOne operations assistant.
You help finance and operations teams query and update JDE using natural language.
Always confirm before posting or creating records.

ENVIRONMENT:
  AIS Base URL: https://jde.yourcompany.com/jderest/v3
  Auth:         POST /tokenrequest  (returns userInfo.token)
  Orchestrator: POST /orchestrator/{name}

JDE NAMING CONVENTIONS:
  Tables:   F + module  (F4211=Sales Detail, F0911=GL, F0411=AP Ledger, F4311=PO Detail)
  Programs: P + number  (P4210=SO Entry, P0411=AP Voucher)
  Reports:  R + number  (R09801=Trial Balance, R04423=AP Aging)
  BSFNs:    B or N prefix (N0400047=AP Voucher, N4200310=Sales Order)

MODULES IN SCOPE: AP (04xx), AR (03xx), GL (09xx), PO (43xx), SO (42xx)
ERROR SEVERITY:   E = hard error (abort), W = warning (log), I = info (ignore)

When tools return data:
- Summarise clearly in plain English — do not expose JDE field codes (e.g. RPDOC)
- Format amounts with currency symbols and 2 decimal places
- Format dates as DD-MMM-YYYY
- Always confirm transactional actions (create, post, update) before executing"""


# ═══════════════════════════════════════════════════════════════════
# 5. CLAUDE TOOL DEFINITIONS
#    One tool entry per JDE Orchestration you want Claude to call.
#    Add or remove tools to match what you've published in JDE.
# ═══════════════════════════════════════════════════════════════════

TOOLS = [
    # ── AP: Create voucher ────────────────────────────────────────
    {
        "name": "create_ap_voucher",
        "description": (
            "Creates an AP voucher (payable invoice) in JDE. "
            "Use when the user asks to enter, post, or record a supplier invoice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_an8": {
                    "type": "integer",
                    "description": "Supplier JDE Address Book number",
                },
                "invoice_amount": {
                    "type": "number",
                    "description": "Gross invoice amount (positive)",
                },
                "invoice_date": {
                    "type": "string",
                    "description": "Invoice date in YYYY-MM-DD format",
                },
                "company": {
                    "type": "string",
                    "description": "JDE company code, e.g. 00001",
                },
                "remark": {
                    "type": "string",
                    "description": "Optional invoice remark (max 30 chars)",
                },
            },
            "required": ["supplier_an8", "invoice_amount", "invoice_date", "company"],
        },
    },

    # ── AP: Aging report ──────────────────────────────────────────
    {
        "name": "get_ap_aging",
        "description": "Returns AP aging report for a company from JDE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {
                    "type": "string",
                    "description": "JDE company code",
                },
                "aging_days": {
                    "type": "integer",
                    "description": "Aging bucket in days",
                    "enum": [30, 60, 90],
                },
            },
            "required": ["company"],
        },
    },

    # ── PO: Status query ──────────────────────────────────────────
    {
        "name": "get_po_status",
        "description": "Returns purchase order status and receipt details from JDE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "po_number": {
                    "type": "integer",
                    "description": "PO document number",
                },
                "company": {
                    "type": "string",
                    "description": "JDE company code",
                },
            },
            "required": ["po_number"],
        },
    },

    # ── GL: Account balance ───────────────────────────────────────
    {
        "name": "query_gl_account",
        "description": "Queries GL account balance or transaction history from JDE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "GL account identifier, e.g. 1.1110",
                },
                "fiscal_year": {
                    "type": "integer",
                    "description": "Fiscal year, e.g. 2026",
                },
                "fiscal_period": {
                    "type": "integer",
                    "description": "Period 1–14 (0 = year-to-date)",
                },
                "company": {
                    "type": "string",
                    "description": "JDE company code",
                },
            },
            "required": ["account_id", "fiscal_year", "company"],
        },
    },
]


# ═══════════════════════════════════════════════════════════════════
# 6. TOOL DISPATCHER
#    Maps each Claude tool name to a JDE Orchestration call.
#    Update the Orchestration names and parameter mappings to match
#    what you have published in your Orchestrator Studio.
# ═══════════════════════════════════════════════════════════════════

def dispatch_tool(tool_name: str, tool_input: dict) -> str:
    """
    Route a Claude tool_use block to the matching JDE Orchestration.

    Returns:
        JSON string of the JDE response (fed back to Claude as tool_result)
    """

    if tool_name == "create_ap_voucher":
        result = call_orchestration("CreateAPVoucher", {
            "SupplierAN8":   tool_input["supplier_an8"],
            "InvoiceAmount": tool_input["invoice_amount"],
            "InvoiceDate":   tool_input["invoice_date"],
            "Company":       tool_input["company"],
            "Remark":        tool_input.get("remark", ""),
        })

    elif tool_name == "get_ap_aging":
        result = call_orchestration("GetAPAging", {
            "Company":   tool_input["company"],
            "AgingDays": tool_input.get("aging_days", 30),
        })

    elif tool_name == "get_po_status":
        result = call_orchestration("GetPOStatus", {
            "PONumber": tool_input["po_number"],
            "Company":  tool_input.get("company", "00001"),
        })

    elif tool_name == "query_gl_account":
        result = call_orchestration("QueryGLAccount", {
            "AccountID":    tool_input["account_id"],
            "FiscalYear":   tool_input["fiscal_year"],
            "FiscalPeriod": tool_input.get("fiscal_period", 0),
            "Company":      tool_input["company"],
        })

    else:
        raise ValueError(f"No JDE Orchestration mapped for tool: {tool_name}")

    log.info("Tool %s → JDE response: %s", tool_name, result)
    return json.dumps(result)


# ═══════════════════════════════════════════════════════════════════
# 7. CLAUDE AGENT  (core reasoning + tool loop)
# ═══════════════════════════════════════════════════════════════════

claude = anthropic.Anthropic()


def run_agent(
    user_message:   str,
    history:        list,
    schema_context: str = "",
) -> tuple[str, list]:
    """
    Run one agent turn: natural language in → JDE action → natural language out.

    Args:
        user_message:   The user's request in plain English
        history:        Full previous messages list (managed by caller)
        schema_context: Optional JDE schema/spec text to inject for this turn

    Returns:
        (reply_text, updated_history)
    """
    messages = list(history)

    # Inject per-request schema context (F-table defs, Orch schemas, etc.)
    if schema_context:
        messages += [
            {"role": "user",
             "content": f"[JDE CONTEXT]\n{schema_context}\n[/JDE CONTEXT]"},
            {"role": "assistant",
             "content": "Context received. Ready for your request."},
        ]

    messages.append({"role": "user", "content": user_message})

    for iteration in range(Config.MAX_TOOL_ITER):
        response = claude.messages.create(
            model=Config.CLAUDE_MODEL,
            max_tokens=Config.MAX_TOKENS,
            system=[{
                "type":          "text",
                "text":          JDE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},   # cache system prompt
            }],
            tools=TOOLS,
            messages=messages,
        )

        log.info(
            "Iter %d | stop=%s | tokens in=%d out=%d",
            iteration + 1,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        # ── Claude finished: return reply to caller ────────────────
        if response.stop_reason == "end_turn":
            reply = next(
                b.text for b in response.content if hasattr(b, "text")
            )
            messages.append({"role": "assistant", "content": reply})
            return reply, messages

        # ── Claude issued tool call(s): execute and return results ─
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    content = dispatch_tool(block.name, block.input)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     content,
                    })
                except Exception as exc:
                    log.error("Tool %s failed: %s", block.name, exc)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     f"Error calling JDE: {exc}",
                        "is_error":    True,
                    })

            messages.append({"role": "user", "content": tool_results})

    # Reached iteration cap
    fallback = "Too many steps required. Please simplify your request."
    messages.append({"role": "assistant", "content": fallback})
    return fallback, messages


# ═══════════════════════════════════════════════════════════════════
# 8. FASTAPI REST SERVICE
# ═══════════════════════════════════════════════════════════════════

app = FastAPI(
    title="JDE Claude Integration API",
    version="1.0.0",
    description="Natural language interface to JDE EnterpriseOne via Claude + AIS Orchestrations",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # restrict to your domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ──────────────────────────────────────

class ChatRequest(BaseModel):
    message:        str
    history:        list = []   # client maintains and resends history each call
    schema_context: str  = ""   # optional JDE schema docs for this turn


class ChatResponse(BaseModel):
    reply:   str
    history: list               # updated history — client stores for next call


class HealthResponse(BaseModel):
    status:            str
    jde_configured:    bool
    claude_configured: bool


# ── Endpoints ──────────────────────────────────────────────────────

@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    Main chat endpoint.

    POST body:
        { "message": "Get AP aging for company 00001",
          "history": [...],        ← previous exchanges
          "schema_context": "..."  ← optional JDE schemas
        }

    Response:
        { "reply": "Here is the aging...", "history": [...] }
    """
    try:
        reply, updated_history = run_agent(
            req.message,
            req.history,
            req.schema_context,
        )
        return ChatResponse(reply=reply, history=updated_history)

    except requests.HTTPError as exc:
        log.error("JDE AIS HTTP error: %s", exc)
        raise HTTPException(status_code=502, detail=f"JDE AIS: {exc}")

    except anthropic.APIStatusError as exc:
        log.error("Claude API error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Claude: {exc}")

    except Exception as exc:
        log.exception("Unexpected error in /api/chat")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Verify JDE credentials and Claude API key are configured."""
    jde_ok    = bool(Config.JDE_USER and Config.JDE_PASSWORD)
    claude_ok = bool(os.getenv("ANTHROPIC_API_KEY"))
    return HealthResponse(
        status            = "ok" if (jde_ok and claude_ok) else "degraded",
        jde_configured    = jde_ok,
        claude_configured = claude_ok,
    )


@app.get("/api/orchestrations")
async def get_orchestrations():
    """List all JDE Orchestrations published on the AIS server."""
    try:
        return list_orchestrations()
    except requests.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/orchestrations/{name}")
async def get_orchestration(name: str):
    """Get the JSON definition of a specific JDE Orchestration."""
    try:
        return get_orchestration_schema(name)
    except requests.HTTPError as exc:
        raise HTTPException(status_code=404, detail=f"Orchestration '{name}' not found")


# ═══════════════════════════════════════════════════════════════════
# 9. QUICK TEST  (run without a server)
# ═══════════════════════════════════════════════════════════════════

def _quick_test():
    """Smoke-test the agent without starting FastAPI."""
    print("=== JDE Claude Agent Quick Test ===\n")
    history = []

    queries = [
        "Get AP aging report for company 00001, 30 day bucket",
        "What is the status of PO number 112345?",
        "Create an AP voucher: supplier 4500, $1,200, today, company 00001",
    ]

    for q in queries:
        print(f"User: {q}")
        reply, history = run_agent(q, history)
        print(f"Agent: {reply}\n")


# ═══════════════════════════════════════════════════════════════════
# 10. ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        _quick_test()
    else:
        import uvicorn
        uvicorn.run(
            "jde_claude_integration:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
        )
