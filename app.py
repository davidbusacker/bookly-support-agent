"""
Bookly Support Agent — orchestration layer.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
import anthropic
from elevenlabs import ElevenLabs, VoiceSettings

from aop_loader import build_system_prompt
from intent import IntentResult, build_clarification_reply, classify_intent, message_plain_text
from resolution import (
    CONFIRM_QUESTION,
    ConfirmResult,
    classify_resolution_confirm,
    looks_like_customer_done,
    score_resolution,
    should_score_resolution,
)
from restock import (
    fetch_inventory,
    find_restock_offer,
    looks_like_availability_interest,
    traces_search_blob,
)
from tts_utils import prepare_text_for_speech
from tools import (
    BOOKLY_AGENT_INSTRUCTIONS,
    TOOLS_SCHEMA,
    execute_tool,
    get_bookly_client,
    ping_bookly,
    reload_bookly_client,
    tool_result_content,
)
from trace_client import (
    TraceClient,
    extract_email,
    extract_order_id,
    extract_trace_id,
    extract_trace_number,
    trace_message,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-haiku-4-5")
INTENT_CONFIDENCE_THRESHOLD = float(os.environ.get("INTENT_CONFIDENCE_THRESHOLD", "0.50"))
RESOLUTION_THRESHOLD = float(os.environ.get("RESOLUTION_THRESHOLD", "0.90"))
TOOL_RESULT_KEEP = 2
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
ELEVEN_MODEL_ID = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
ELEVEN_VOICE_SETTINGS = VoiceSettings(
    stability=0.72,
    similarity_boost=0.8,
    style=0.0,
    use_speaker_boost=False,
)

anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
eleven_client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))
_TRACE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bookly-trace")

reload_bookly_client()
trace_client = TraceClient(get_bookly_client(), model=ANTHROPIC_MODEL)

BASE_SYSTEM_PROMPT = build_system_prompt(BOOKLY_AGENT_INSTRUCTIONS)


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
    authenticated: bool = False
    auth_subject: str | None = None  # order id or email that passed last-4


SESSIONS: dict[str, Session] = {}


def _ndjson(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str) + "\n"


# Short titles for the live "Peek into the agent" demo panel.
TOOL_PEEK_TITLES: dict[str, str] = {
    "read_aop": "Reading a policy",
    "verify_phone_last_four": "Verifying phone (last 4)",
    "get_order": "Looking up the order",
    "list_orders": "Listing past orders",
    "list_customers": "Looking up the customer",
    "get_customer": "Looking up the customer",
    "list_books": "Checking inventory",
    "get_book": "Looking up a title",
    "create_return": "Opening a return",
    "create_refund": "Issuing a refund",
    "update_refund": "Updating a refund",
    "cancel_order": "Cancelling the order",
    "cancel_return": "Cancelling a return",
    "create_ticket": "Escalating to a ticket",
    "list_agent_traces": "Reading prior conversations",
    "get_agent_trace": "Reading a prior conversation",
    "list_policies": "Checking store policy",
    "get_policy": "Checking store policy",
    "list_faqs": "Checking FAQs",
    "list_tickets": "Checking open tickets",
    "reship_order": "Arranging a reship",
    "address_change": "Updating the address",
    "password_reset": "Starting a password reset",
    "receive_return": "Marking a return received",
}


def _peek(text: str) -> str:
    """NDJSON status line for the live agent peek panel."""
    return _ndjson({"type": "status", "text": text})


def _tool_peek_title(name: str) -> str:
    return TOOL_PEEK_TITLES.get(name, f"Using {name.replace('_', ' ')}")


def _system_prompt(session: Session) -> str:
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


def _update_caller_identity(session: Session, user_message: str) -> bool:
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
    # Switching lookup subject invalidates prior phone verification.
    if session.authenticated and session.auth_subject:
        subject = session.auth_subject.lower()
        order_match = session.order_id and session.order_id.lower() == subject
        email_match = session.customer_email and session.customer_email.lower() == subject
        if not order_match and not email_match:
            session.authenticated = False
            session.auth_subject = None
    return changed


def _conversation_text(history: list, limit: int = 12) -> str:
    lines: list[str] = []
    for msg in history[-limit:]:
        role = msg.get("role", "")
        text = message_plain_text(msg.get("content")).strip()
        if text and role in ("user", "assistant"):
            lines.append(f"{role}: {text[:400]}")
    return "\n".join(lines)


def compact_history(messages: list, keep_recent: int = TOOL_RESULT_KEEP) -> None:
    """Shrink old tool_result payloads so later Claude calls stay small."""
    tool_idxs = []
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            continue
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            tool_idxs.append(i)
    drop = set(tool_idxs[:-keep_recent])
    if not drop:
        return
    for i in drop:
        compacted = []
        for block in messages[i]["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                compacted.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "content": '{"ok": true, "omitted": "prior tool result trimmed"}',
                    }
                )
            else:
                compacted.append(block)
        messages[i] = {"role": "user", "content": compacted}


def _attach_email_from_order(session: Session) -> None:
    """Phone verify never returns account data; pull email from the order for trace lookup."""
    if session.customer_email or not session.order_id:
        return
    result = get_bookly_client().execute_tool("get_order", {"id": session.order_id})
    if not result.get("ok"):
        return
    email = ((result.get("data") or {}).get("customer") or {}).get("email")
    if not email:
        return
    session.customer_email = str(email).lower()
    _refresh_caller_history(session, force=True)
    if session.trace_id:
        _TRACE_POOL.submit(
            trace_client.update_trace,
            session.trace_id,
            customer_email=session.customer_email,
        )


def _maybe_restock_offer(session: Session) -> dict[str, Any] | None:
    if session.restock_offered:
        return None

    _attach_email_from_order(session)

    traces: list[dict[str, Any]] = []
    if session.customer_email or session.order_id:
        traces = trace_client.list_traces_for_caller(
            email=session.customer_email,
            order_id=session.order_id,
            limit=8,
            exclude_trace_id=session.trace_id,
        )
        if session.trace_id:
            current = get_bookly_client().execute_tool("get_agent_trace", {"id": session.trace_id})
            if current.get("ok") and current.get("data"):
                traces = [current["data"]] + traces

    convo = _conversation_text(session.messages)
    trace_blob = traces_search_blob(traces)
    if not looks_like_availability_interest(convo, trace_blob, session.caller_history):
        logging.info(
            "Restock skipped: no availability language (email=%s order=%s traces=%s)",
            session.customer_email,
            session.order_id,
            len(traces),
        )
        return None

    inventory = fetch_inventory(execute_tool)
    offer = find_restock_offer(
        anthropic_client,
        model=CLASSIFIER_MODEL,
        traces=traces,
        current_convo=convo,
        inventory=inventory,
    )
    if not offer:
        logging.info(
            "Restock: no inventory match (email=%s order=%s traces=%s books=%s)",
            session.customer_email,
            session.order_id,
            len(traces),
            len(inventory),
        )
        return None
    session.restock_offered = True
    logging.info(
        "Restock offer: %s by %s (email=%s)",
        offer.title,
        offer.author,
        session.customer_email,
    )
    return offer.as_dict()


def _refresh_caller_history(session: Session, *, force: bool = False) -> None:
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


def _maybe_update_identity_from_tool(session: Session, tool_name: str, result: dict[str, Any]) -> None:
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
            _refresh_caller_history(session, force=True)
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
        _refresh_caller_history(session, force=True)
        if session.trace_id:
            fields: dict[str, Any] = {}
            if session.customer_email:
                fields["customer_email"] = session.customer_email
            if session.order_id:
                fields["order_id"] = session.order_id
            if fields:
                _TRACE_POOL.submit(trace_client.update_trace, session.trace_id, **fields)


def _ensure_trace(
    session: Session,
    session_id: str,
    *,
    subject: str,
    intent: IntentResult,
    first_messages: list[dict[str, Any]],
) -> None:
    if session.trace_id:
        return
    result = trace_client.start_trace(
        subject=subject,
        customer_email=session.customer_email,
        order_id=session.order_id,
        intent=intent.intent,
        messages=first_messages,
        metadata={
            "external_session_id": session_id,
            "intent_confidence": intent.confidence,
            "intent_reasoning": intent.reasoning,
        },
    )
    session.trace_id = extract_trace_id(result)
    session.trace_number = extract_trace_number(result)
    if not session.trace_id:
        logging.warning("Could not create Bookly agent trace: %s", result)


def _sync_trace_worker(trace_id: str, messages: list[dict[str, Any]], update_fields: dict[str, Any]) -> None:
    try:
        if messages:
            trace_client.append_messages(trace_id, messages)
        if update_fields:
            trace_client.update_trace(trace_id, **update_fields)
    except Exception:
        logging.exception("Background trace write failed")


def _sync_trace(session: Session, messages: list[dict[str, Any]], **update_fields: Any) -> None:
    if not session.trace_id or not messages:
        return
    _TRACE_POOL.submit(_sync_trace_worker, session.trace_id, list(messages), dict(update_fields))


def _claude_message(history: list, system: str):
    """Yield text deltas live, then a final _message event."""
    tool_started = False
    with anthropic_client.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        system=system,
        tools=TOOLS_SCHEMA,
        messages=history,
    ) as stream:
        for event in stream:
            event_type = getattr(event, "type", None)
            if event_type == "content_block_start":
                block = getattr(event, "content_block", None)
                if block is not None and getattr(block, "type", None) == "tool_use":
                    tool_started = True
            elif event_type == "content_block_delta" and not tool_started:
                delta = getattr(event, "delta", None)
                if delta is not None and getattr(delta, "type", None) == "text_delta":
                    chunk = getattr(delta, "text", "") or ""
                    if chunk:
                        yield {"type": "delta", "text": chunk}
        yield {"type": "_message", "message": stream.get_final_message()}


def run_agent_turn(
    history: list,
    session_id: str,
    *,
    session: Session,
):
    """Yield status/delta events, then a final _result dict."""
    compact_history(history)
    tools_used: list[str] = []
    tool_traces: list[dict[str, Any]] = []
    streamed_text = False

    while True:
        response = None
        for event in _claude_message(history, _system_prompt(session)):
            if event.get("type") == "_message":
                response = event["message"]
            else:
                streamed_text = True
                yield event
        if response is None:
            raise RuntimeError("Claude stream ended without a message.")

        if response.stop_reason == "tool_use":
            history.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                yield {"type": "status", "text": _tool_peek_title(block.name)}
                result = execute_tool(
                    block.name,
                    block.input,
                    session_id=session_id,
                    authenticated=session.authenticated,
                )
                if result.get("error") == "authentication_required":
                    yield {"type": "status", "text": f"Blocked {block.name} — not authenticated"}
                _maybe_update_identity_from_tool(session, block.name, result)
                if block.name == "verify_phone_last_four" and result.get("verified"):
                    yield {"type": "status", "text": "Authenticated"}
                tools_used.append(block.name)
                tool_traces.append(
                    trace_message(
                        role="tool",
                        speaker="Riley",
                        content=f"Called `{block.name}`",
                        tool_name=block.name,
                        tool_input=block.input,
                        tool_output=result,
                    )
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_result_content(result),
                    }
                )
            history.append({"role": "user", "content": tool_results})
            continue

        final_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not streamed_text and final_text:
            yield {"type": "delta", "text": final_text}
        history.append({"role": "assistant", "content": response.content})
        yield {
            "type": "_result",
            "reply": final_text,
            "tools": tools_used,
            "traces": tool_traces,
        }
        return


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    bookly = ping_bookly()
    return jsonify(
        {
            "status": "ok",
            "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "elevenlabs_configured": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "model": ANTHROPIC_MODEL,
            "classifier_model": CLASSIFIER_MODEL,
            "intent_threshold": INTENT_CONFIDENCE_THRESHOLD,
            "resolution_threshold": RESOLUTION_THRESHOLD,
            "bookly_api": {"ok": bookly.get("ok", False), "status": bookly.get("status")},
            "tools_loaded": len(TOOLS_SCHEMA),
            "trace_tools": "log_agent_trace" in get_bookly_client()._tools_by_name,
            "architecture": "aop-driven",
        }
    )


@app.route("/api/session", methods=["POST"])
def new_session():
    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = Session()
    return jsonify({"session_id": session_id})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    user_message = data.get("message", "").strip()

    if not session_id or session_id not in SESSIONS:
        return jsonify({"error": "Unknown or missing session_id. Call /api/session first."}), 400
    if not user_message:
        return jsonify({"error": "Empty message."}), 400
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set."}), 500

    session = SESSIONS[session_id]
    identity_changed = _update_caller_identity(session, user_message)
    _refresh_caller_history(session, force=identity_changed)

    def generate():
        try:
            yield from _chat_events(session, session_id, user_message)
        except Exception as exc:
            logging.exception("Chat turn failed")
            yield _ndjson({"type": "error", "error": str(exc)})

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _chat_events(session: Session, session_id: str, user_message: str):
    yield _peek("Classifying intent")

    intent_result = classify_intent(
        anthropic_client,
        model=CLASSIFIER_MODEL,
        user_message=user_message,
        history=session.messages,
    )
    session.last_intent = intent_result.intent
    session.last_confidence = intent_result.confidence
    yield _peek(
        f"Intent: {intent_result.intent.replace('_', ' ')} "
        f"({intent_result.confidence:.0%})"
    )

    subject = user_message[:80] + ("…" if len(user_message) > 80 else "")
    turn_messages = [
        trace_message(role="customer", speaker="Customer", content=user_message),
        trace_message(
            role="note",
            speaker="System",
            content=f"Intent: {intent_result.intent} (confidence {intent_result.confidence:.0%})",
            metadata={
                "intent": intent_result.intent,
                "confidence": intent_result.confidence,
                "reasoning": intent_result.reasoning,
                "guardrail_threshold": INTENT_CONFIDENCE_THRESHOLD,
            },
        ),
    ]

    if not session.trace_id:
        yield _peek("Starting Bookly agent trace")
        _ensure_trace(session, session_id, subject=subject, intent=intent_result, first_messages=turn_messages)
    else:
        yield _peek("Logging turn to Bookly")
        _sync_trace(session, turn_messages)

    had_prior_reply = any(m.get("role") == "assistant" for m in session.messages)
    try_closeout = session.awaiting_resolution_confirm or (
        had_prior_reply
        and not session.restock_offered
        and looks_like_customer_done(user_message)
    )
    if try_closeout:
        session.awaiting_resolution_confirm = False
        yield _peek("Checking if they're done")
        confirm = classify_resolution_confirm(
            anthropic_client,
            model=CLASSIFIER_MODEL,
            user_message=user_message,
        )
        if confirm.ready_for_offer:
            yield from _confirmed_resolution_events(session, user_message, intent_result, confirm)
            return

    if intent_result.needs_clarification:
        yield _peek("Intent unclear — asking a clarifying question")
        reply_text = build_clarification_reply(
            anthropic_client,
            model=CLASSIFIER_MODEL,
            user_message=user_message,
            intent=intent_result,
            history=session.messages,
        )
        session.messages.append({"role": "user", "content": user_message})
        session.messages.append({"role": "assistant", "content": reply_text})
        _sync_trace(
            session,
            [
                trace_message(
                    role="agent",
                    speaker="Riley",
                    content=reply_text,
                    metadata={"guardrail": "intent_clarification"},
                )
            ],
            summary=f"Clarifying unclear intent (confidence {intent_result.confidence:.0%}).",
        )
        yield _ndjson({"type": "delta", "text": reply_text})
        yield _ndjson(
            {
                "type": "done",
                "reply": reply_text,
                "intent": intent_result.as_dict(),
                "guardrail": "intent_clarification",
                "trace_id": session.trace_id,
                "trace_number": session.trace_number,
            }
        )
        return

    session.messages.append({"role": "user", "content": user_message})
    yield _peek("Riley is reasoning")

    reply_text = ""
    tools_used: list[str] = []
    tool_traces: list[dict[str, Any]] = []
    try:
        for event in run_agent_turn(session.messages, session_id, session=session):
            if event.get("type") == "_result":
                reply_text = event.get("reply") or ""
                tools_used = event.get("tools") or []
                tool_traces = event.get("traces") or []
            else:
                yield _ndjson(event)
    except Exception as exc:
        _sync_trace(
            session,
            [trace_message(role="system", speaker="System", content=f"Error: {exc}")],
            status="failed",
        )
        yield _ndjson({"type": "error", "error": str(exc)})
        return

    restock_payload = None
    if looks_like_customer_done(user_message) and not session.restock_offered:
        # They already wrapped up this turn — don't wait for a second "I'm good."
        yield _peek("Customer said they're done — checking restock")
        restock_payload = _maybe_restock_offer(session)
        if restock_payload:
            yield _peek(f"Restock match: {restock_payload['title']}")
            extra = f" {restock_payload['solicitation']}"
            reply_text = f"{reply_text.rstrip()}{extra}"
            yield _ndjson({"type": "delta", "text": extra})
            session.resolution_confirmed = True
        else:
            yield _peek("No restock match")

    resolution_payload = None
    if restock_payload is None and should_score_resolution(
        reply_text=reply_text, tools_this_turn=tools_used
    ):
        yield _peek("Scoring resolution")
        resolution = score_resolution(
            anthropic_client,
            model=CLASSIFIER_MODEL,
            history=session.messages,
            latest_reply=reply_text,
        )
        session.last_resolution = resolution.score
        resolution_payload = resolution.as_dict()
        yield _peek(f"Resolution {resolution.score:.0%}")
        if (
            resolution.score >= RESOLUTION_THRESHOLD
            and not session.restock_offered
            and not session.resolution_confirmed
            and not session.awaiting_resolution_confirm
            and not looks_like_customer_done(user_message)
        ):
            yield _peek("Asking if everything is resolved")
            extra = f" {CONFIRM_QUESTION}"
            reply_text = f"{reply_text.rstrip()}{extra}"
            yield _ndjson({"type": "delta", "text": extra})
            session.awaiting_resolution_confirm = True
    elif restock_payload is None:
        session.last_resolution = None

    end_notes = list(tool_traces)
    end_notes.append(trace_message(role="agent", speaker="Riley", content=reply_text))
    if restock_payload:
        end_notes.append(
            trace_message(
                role="note",
                speaker="System",
                content=(
                    f"Customer already said they're done — restock offer: "
                    f"{restock_payload['title']} by {restock_payload['author']}"
                ),
                metadata={"restock_offer": restock_payload, "resolution_confirmed": True},
            )
        )
    if resolution_payload:
        end_notes.append(
            trace_message(
                role="note",
                speaker="System",
                content=(
                    f"Resolution: {resolution_payload['resolution_score']:.0%}"
                    + (
                        " — asked customer to confirm before any restock offer"
                        if session.awaiting_resolution_confirm
                        else ""
                    )
                ),
                metadata={
                    "resolution_score": resolution_payload["resolution_score"],
                    "awaiting_resolution_confirm": session.awaiting_resolution_confirm,
                },
            )
        )
    _sync_trace(
        session,
        end_notes,
        metadata={
            "last_intent": intent_result.intent,
            "last_confidence": intent_result.confidence,
            "resolution_score": session.last_resolution,
            "restock_offer": restock_payload,
        },
    )

    yield _ndjson(
        {
            "type": "done",
            "reply": reply_text,
            "intent": intent_result.as_dict(),
            "resolution": resolution_payload,
            "restock_offer": restock_payload,
            "guardrail": (
                "restock_offer"
                if restock_payload
                else "resolution_confirm" if session.awaiting_resolution_confirm else None
            ),
            "trace_id": session.trace_id,
            "trace_number": session.trace_number,
            "customer_email": session.customer_email,
        }
    )


def _confirmed_resolution_events(
    session: Session,
    user_message: str,
    intent_result: IntentResult,
    confirm: ConfirmResult,
):
    session.resolution_confirmed = True
    session.messages.append({"role": "user", "content": user_message})
    yield _peek("Customer confirmed — checking prior traces & inventory")
    restock = _maybe_restock_offer(session)
    if restock:
        yield _peek(f"Restock match: {restock['title']}")
        reply_text = restock["solicitation"]
        note = f"Customer confirmed resolved — restock offer: {restock['title']} by {restock['author']}"
    else:
        yield _peek("No restock match")
        reply_text = "Glad we got that sorted."
        note = "Customer confirmed resolved — no restock match in traces/inventory"
        session.restock_offered = True
    session.messages.append({"role": "assistant", "content": reply_text})
    _sync_trace(
        session,
        [
            trace_message(role="agent", speaker="Riley", content=reply_text),
            trace_message(
                role="note",
                speaker="System",
                content=note,
                metadata={
                    "resolution_confirmed": True,
                    "confirm": confirm.as_dict(),
                    "restock_offer": restock,
                },
            ),
        ],
        metadata={"resolution_confirmed": True, "restock_offer": restock},
    )
    yield _ndjson({"type": "delta", "text": reply_text})
    yield _ndjson(
        {
            "type": "done",
            "reply": reply_text,
            "intent": intent_result.as_dict(),
            "resolution": {"resolution_score": session.last_resolution, "is_resolved": True},
            "restock_offer": restock,
            "guardrail": "restock_offer" if restock else "resolution_confirmed",
            "trace_id": session.trace_id,
            "trace_number": session.trace_number,
            "customer_email": session.customer_email,
        }
    )


@app.route("/api/tts", methods=["POST"])
def tts():
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text."}), 400
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return jsonify({"error": "ELEVENLABS_API_KEY is not set."}), 500

    try:
        spoken_text = prepare_text_for_speech(text)
        audio_stream = eleven_client.text_to_speech.convert(
            voice_id=ELEVEN_VOICE_ID,
            text=spoken_text,
            model_id=ELEVEN_MODEL_ID,
            voice_settings=ELEVEN_VOICE_SETTINGS,
            output_format="mp3_44100_128",
            seed=42,
        )
        audio_bytes = b"".join(audio_stream)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return Response(audio_bytes, mimetype="audio/mpeg")


if __name__ == "__main__":
    # Avoid macOS AirPlay Receiver, which binds *:5000 and returns 403 in Chrome.
    app.run(debug=True, host="127.0.0.1", port=5050, threaded=True)
