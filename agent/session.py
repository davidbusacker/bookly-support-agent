"""
In-memory Session for one browser tab: messages, auth, order/email, restock flags.
Builds the per-turn system prompt (AOP briefing + caller history + authenticated yes/no).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from guardrails import message_plain_text
from tools import get_bookly_client
from trace_client import extract_email, extract_order_id

from agent.config import BASE_SYSTEM_PROMPT, _TRACE_POOL, trace_client


@dataclass
class Session:
    messages: list = field(default_factory=list)
    trace_id: str | None = None
    trace_number: str | None = None
    customer_email: str | None = None
    order_id: str | None = None
    last_intent: str | None = None
    last_confidence: float | None = None
    last_resolution: float | None = None
    caller_history: str = ""
    history_key: str | None = None
    awaiting_resolution_confirm: bool = False
    resolution_confirmed: bool = False
    restock_offered: bool = False
    awaiting_restock_confirm: bool = False
    pending_restock: dict[str, Any] | None = None
    authenticated: bool = False
    auth_subject: str | None = None  # order id or email that passed last-4


SESSIONS: dict[str, Session] = {}


def system_prompt(session: Session) -> str:
    """Base AOPs + prior traces + current caller/auth context."""
    parts = [BASE_SYSTEM_PROMPT]
    if session.caller_history:
        parts.append(session.caller_history)
    auth_line = (
        f"Authenticated: **yes** (subject `{session.auth_subject}`)"
        if session.authenticated
        else "Authenticated: **no** — account tools are blocked until verify_phone_last_four succeeds"
    )
    caller_bits = ["\n## Current caller", auth_line]
    if session.customer_email:
        caller_bits.insert(1, f"Email: `{session.customer_email}`")
    if session.order_id:
        caller_bits.insert(1 if not session.customer_email else 2, f"Order in context: `{session.order_id}`")
    caller_bits.append(
        "Public tools (policies, FAQs, catalog) work without auth. "
        "Order/customer/refund tools hard-fail until authenticated."
    )
    parts.append("\n".join(caller_bits))
    return "\n".join(parts)


def update_caller_identity(session: Session, user_message: str) -> bool:
    """Return True if email or order identity changed this turn."""
    changed = False
    email = extract_email(user_message)
    if email and email != session.customer_email:
        session.customer_email = email
        changed = True
    order_id = extract_order_id(user_message)
    if order_id and order_id != session.order_id:
        session.order_id = order_id
        changed = True
    if session.authenticated and session.auth_subject:
        subject = session.auth_subject.lower()
        order_match = session.order_id and session.order_id.lower() == subject
        email_match = session.customer_email and session.customer_email.lower() == subject
        if not order_match and not email_match:
            session.authenticated = False
            session.auth_subject = None
    return changed


def conversation_text(history: list, limit: int = 12) -> str:
    """Plain-text excerpt of recent turns for classifiers."""
    lines: list[str] = []
    for msg in history[-limit:]:
        role = msg.get("role", "")
        text = message_plain_text(msg.get("content")).strip()
        if text and role in ("user", "assistant"):
            lines.append(f"{role}: {text[:400]}")
    return "\n".join(lines)


def refresh_caller_history(session: Session, *, force: bool = False) -> None:
    """Load prior Bookly traces into session.caller_history (episodic memory)."""
    if not session.customer_email and not session.order_id:
        return
    key = f"{session.customer_email}|{session.order_id}"
    if not force and key == session.history_key:
        return
    traces = trace_client.list_traces_for_caller(
        email=session.customer_email,
        order_id=session.order_id,
        limit=5,
        exclude_trace_id=session.trace_id,
    )
    session.caller_history = trace_client.format_caller_history(traces)
    session.history_key = key


def maybe_update_identity_from_tool(session: Session, tool_name: str, result: dict[str, Any]) -> None:
    """Sync email/order/auth from a tool result back into session + trace."""
    patch = result.pop("_session", None) if isinstance(result, dict) else None
    changed = False
    if isinstance(patch, dict):
        if patch.get("authenticated"):
            session.authenticated = True
            if patch.get("auth_subject"):
                session.auth_subject = str(patch["auth_subject"])
        email = patch.get("customer_email")
        if email and str(email).lower() != session.customer_email:
            session.customer_email = str(email).lower()
            changed = True
        order_id = patch.get("order_id")
        if order_id and order_id != session.order_id:
            session.order_id = str(order_id)
            changed = True

    if not result.get("ok"):
        if changed:
            refresh_caller_history(session, force=True)
        return

    data = result.get("data")
    if isinstance(data, dict):
        customer = data.get("customer") or {}
        if customer.get("email") and customer["email"].lower() != session.customer_email:
            session.customer_email = customer["email"].lower()
            changed = True
        if data.get("order_number") and data["order_number"] != session.order_id:
            session.order_id = data["order_number"]
            changed = True
        elif data.get("email") and str(data["email"]).lower() != session.customer_email:
            session.customer_email = str(data["email"]).lower()
            changed = True
    if isinstance(data, list) and data and tool_name == "list_customers":
        if data[0].get("email") and data[0]["email"].lower() != session.customer_email:
            session.customer_email = data[0]["email"].lower()
            changed = True
    if changed:
        refresh_caller_history(session, force=True)
        if session.trace_id:
            fields: dict[str, Any] = {}
            if session.customer_email:
                fields["customer_email"] = session.customer_email
            if session.order_id:
                fields["order_id"] = session.order_id
            if fields:
                _TRACE_POOL.submit(trace_client.update_trace, session.trace_id, **fields)


def attach_email_from_order(session: Session) -> None:
    """After phone verify, derive customer_email from order for trace lookup."""
    if session.customer_email or not session.order_id:
        return
    result = get_bookly_client().execute_tool("get_order", {"id": session.order_id})
    if not result.get("ok"):
        return
    email = ((result.get("data") or {}).get("customer") or {}).get("email")
    if not email:
        return
    session.customer_email = str(email).lower()
    refresh_caller_history(session, force=True)
    if session.trace_id:
        _TRACE_POOL.submit(
            trace_client.update_trace,
            session.trace_id,
            customer_email=session.customer_email,
        )
