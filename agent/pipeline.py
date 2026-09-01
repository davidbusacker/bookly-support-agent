"""
One customer message: intent → trace → (clarify | Riley loop) → resolution/restock.
This is the orchestrator; chat_events() is the function to read top-to-bottom.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from guardrails import (
    CONFIRM_QUESTION,
    ConfirmResult,
    IntentResult,
    build_clarification_reply,
    classify_intent,
    classify_resolution_confirm,
    fetch_inventory,
    find_restock_offer,
    looks_like_availability_interest,
    looks_like_customer_done,
    looks_like_restock_accept,
    looks_like_restock_decline,
    score_resolution,
    should_score_resolution,
    traces_search_blob,
)
from tools import execute_tool, get_bookly_client
from trace_client import trace_message

from agent.config import (
    CLASSIFIER_MODEL,
    INTENT_CONFIDENCE_THRESHOLD,
    RESOLUTION_THRESHOLD,
    openai_client,
    trace_client,
)
from agent.loop import run_agent_turn
from agent.session import Session, attach_email_from_order, conversation_text
from agent.traces import ensure_trace, sync_trace


def ndjson(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str) + "\n"


def peek(text: str) -> str:
    return ndjson({"type": "status", "text": text})


def _intent_payload(intent: IntentResult) -> dict[str, Any]:
    data = intent.as_dict()
    data["needs_clarification"] = intent.confidence < INTENT_CONFIDENCE_THRESHOLD
    return data


def maybe_restock_offer(session: Session) -> dict[str, Any] | None:
    if session.restock_offered:
        return None

    attach_email_from_order(session)

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

    convo = conversation_text(session.messages)
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
        openai_client,
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
    session.awaiting_restock_confirm = True
    session.pending_restock = offer.as_dict()
    logging.info(
        "Restock offer: %s by %s format=%s isbn=%s stock=%s (email=%s)",
        offer.title,
        offer.author,
        offer.format,
        offer.isbn,
        offer.stock,
        session.customer_email,
    )
    return session.pending_restock


def chat_events(session: Session, session_id: str, user_message: str):
    yield peek("Classifying intent")

    intent_result = classify_intent(
        openai_client,
        model=CLASSIFIER_MODEL,
        user_message=user_message,
        history=session.messages,
    )
    session.last_intent = intent_result.intent
    session.last_confidence = intent_result.confidence
    yield peek(
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
        yield peek("Starting Bookly agent trace")
        ensure_trace(session, session_id, subject=subject, intent=intent_result, first_messages=turn_messages)
    else:
        yield peek("Logging turn to Bookly")
        sync_trace(session, turn_messages)

    if session.awaiting_restock_confirm and session.pending_restock:
        if looks_like_restock_decline(user_message):
            yield from restock_decline_events(session, user_message, intent_result)
            return
        if looks_like_restock_accept(user_message):
            yield from restock_accept_events(session, user_message, intent_result)
            return

    had_prior_reply = any(m.get("role") == "assistant" for m in session.messages)
    try_closeout = session.awaiting_resolution_confirm or (
        had_prior_reply
        and not session.restock_offered
        and looks_like_customer_done(user_message)
    )
    if try_closeout:
        session.awaiting_resolution_confirm = False
        yield peek("Checking if they're done")
        confirm = classify_resolution_confirm(
            openai_client,
            model=CLASSIFIER_MODEL,
            user_message=user_message,
        )
        if confirm.ready_for_offer:
            yield from confirmed_resolution_events(session, user_message, intent_result, confirm)
            return

    # Sole intent cutoff — classifiers report 0–1; they do not apply this number.
    if intent_result.confidence < INTENT_CONFIDENCE_THRESHOLD:
        yield peek("Intent unclear — asking a clarifying question")
        reply_text = build_clarification_reply(
            openai_client,
            model=CLASSIFIER_MODEL,
            user_message=user_message,
            intent=intent_result,
            history=session.messages,
        )
        session.messages.append({"role": "user", "content": user_message})
        session.messages.append({"role": "assistant", "content": reply_text})
        sync_trace(
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
        yield ndjson({"type": "delta", "text": reply_text})
        yield ndjson(
            {
                "type": "done",
                "reply": reply_text,
                "intent": _intent_payload(intent_result),
                "guardrail": "intent_clarification",
                "trace_id": session.trace_id,
                "trace_number": session.trace_number,
            }
        )
        return

    session.messages.append({"role": "user", "content": user_message})
    yield peek("Riley is reasoning")

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
                yield ndjson(event)
    except Exception as exc:
        sync_trace(
            session,
            [trace_message(role="system", speaker="System", content=f"Error: {exc}")],
            status="failed",
        )
        yield ndjson({"type": "error", "error": str(exc)})
        return

    restock_payload = None
    if looks_like_customer_done(user_message) and not session.restock_offered:
        yield peek("Customer said they're done — checking restock")
        restock_payload = maybe_restock_offer(session)
        if restock_payload:
            yield peek(f"Restock match: {restock_payload['title']}")
            extra = f" {restock_payload['solicitation']}"
            reply_text = f"{reply_text.rstrip()}{extra}"
            yield ndjson({"type": "delta", "text": extra})
            session.resolution_confirmed = True
        else:
            yield peek("No restock match")

    resolution_payload = None
    if restock_payload is None and should_score_resolution(
        reply_text=reply_text, tools_this_turn=tools_used
    ):
        yield peek("Scoring resolution")
        resolution = score_resolution(
            openai_client,
            model=CLASSIFIER_MODEL,
            history=session.messages,
            latest_reply=reply_text,
        )
        session.last_resolution = resolution.score
        resolution_payload = resolution.as_dict()
        resolution_payload["is_resolved"] = resolution.score >= RESOLUTION_THRESHOLD
        yield peek(f"Resolution {resolution.score:.0%}")
        # Sole resolution cutoff — classifiers report 0–1; they do not apply this number.
        if (
            resolution.score >= RESOLUTION_THRESHOLD
            and not session.restock_offered
            and not session.resolution_confirmed
            and not session.awaiting_resolution_confirm
            and not looks_like_customer_done(user_message)
        ):
            yield peek("Asking if everything is resolved")
            extra = f" {CONFIRM_QUESTION}"
            reply_text = f"{reply_text.rstrip()}{extra}"
            yield ndjson({"type": "delta", "text": extra})
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
    sync_trace(
        session,
        end_notes,
        metadata={
            "last_intent": intent_result.intent,
            "last_confidence": intent_result.confidence,
            "resolution_score": session.last_resolution,
            "restock_offer": restock_payload,
        },
    )

    yield ndjson(
        {
            "type": "done",
            "reply": reply_text,
            "intent": _intent_payload(intent_result),
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


def confirmed_resolution_events(
    session: Session,
    user_message: str,
    intent_result: IntentResult,
    confirm: ConfirmResult,
):
    session.resolution_confirmed = True
    session.messages.append({"role": "user", "content": user_message})
    yield peek("Customer confirmed — checking prior traces & inventory")
    restock = maybe_restock_offer(session)
    if restock:
        yield peek(f"Restock match: {restock['title']}")
        reply_text = restock["solicitation"]
        note = f"Customer confirmed resolved — restock offer: {restock['title']} by {restock['author']} ({restock.get('format') or 'in-stock SKU'})"
    else:
        yield peek("No restock match")
        reply_text = "Glad we got that sorted."
        note = "Customer confirmed resolved — no restock match in traces/inventory"
        session.restock_offered = True
    session.messages.append({"role": "assistant", "content": reply_text})
    sync_trace(
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
    yield ndjson({"type": "delta", "text": reply_text})
    yield ndjson(
        {
            "type": "done",
            "reply": reply_text,
            "intent": _intent_payload(intent_result),
            "resolution": {"resolution_score": session.last_resolution, "is_resolved": True},
            "restock_offer": restock,
            "guardrail": "restock_offer" if restock else "resolution_confirmed",
            "trace_id": session.trace_id,
            "trace_number": session.trace_number,
            "customer_email": session.customer_email,
        }
    )


def _order_number(result: dict[str, Any]) -> str | None:
    data = result.get("data")
    if isinstance(data, dict):
        return str(data.get("order_number") or data.get("id") or "") or None
    meta = result.get("meta") or {}
    return str(meta.get("order_number") or "") or None


def place_restock_order(session: Session) -> dict[str, Any]:
    offer = session.pending_restock or {}
    isbn = offer.get("isbn")
    email = session.customer_email
    if not isbn or not email:
        return {"ok": False, "error": "missing_isbn_or_email"}
    return get_bookly_client().execute_tool(
        "place_order",
        {
            "customer": email,
            "items": [{"isbn": isbn, "quantity": 1}],
            "notes": f"Restock offer after support: {offer.get('title')}",
        },
    )


def restock_accept_events(session: Session, user_message: str, intent_result: IntentResult):
    session.awaiting_restock_confirm = False
    session.messages.append({"role": "user", "content": user_message})
    offer = session.pending_restock or {}
    yield peek(f"Placing restock order: {offer.get('title')} ({offer.get('format') or 'in-stock'})")
    result = place_restock_order(session)
    session.pending_restock = None
    if result.get("ok"):
        number = _order_number(result)
        reply_text = (
            f"Great — that's order {number}. Thanks so much, have a nice day."
            if number
            else "Great, order received. Thanks so much, have a nice day."
        )
        note = f"Restock accepted — placed order {number or '(no number)'} for {offer.get('isbn')}"
    else:
        reply_text = "I couldn't complete that order just now. I can try again, or we can leave it."
        note = f"Restock accept failed: {result.get('error') or result}"
        session.awaiting_restock_confirm = True
        session.pending_restock = offer
    session.messages.append({"role": "assistant", "content": reply_text})
    extras: dict[str, Any] = {}
    if result.get("ok"):
        extras["status"] = "completed"
    sync_trace(
        session,
        [
            trace_message(role="agent", speaker="Riley", content=reply_text),
            trace_message(
                role="note",
                speaker="System",
                content=note,
                metadata={"restock_order": result, "restock_offer": offer},
            ),
        ],
        metadata={"restock_order": result.get("ok"), "restock_offer": offer},
        **extras,
    )
    yield ndjson({"type": "delta", "text": reply_text})
    yield ndjson(
        {
            "type": "done",
            "reply": reply_text,
            "intent": _intent_payload(intent_result),
            "restock_offer": offer,
            "guardrail": "restock_ordered" if result.get("ok") else "restock_order_failed",
            "trace_id": session.trace_id,
            "trace_number": session.trace_number,
            "customer_email": session.customer_email,
        }
    )


def restock_decline_events(session: Session, user_message: str, intent_result: IntentResult):
    session.awaiting_restock_confirm = False
    offer = session.pending_restock
    session.pending_restock = None
    session.messages.append({"role": "user", "content": user_message})
    reply_text = "No problem — thanks for chatting with Bookly. Have a nice day."
    session.messages.append({"role": "assistant", "content": reply_text})
    sync_trace(
        session,
        [
            trace_message(role="agent", speaker="Riley", content=reply_text),
            trace_message(
                role="note",
                speaker="System",
                content="Customer declined restock offer",
                metadata={"restock_offer": offer},
            ),
        ],
        status="completed",
        metadata={"restock_declined": True, "restock_offer": offer},
    )
    yield ndjson({"type": "delta", "text": reply_text})
    yield ndjson(
        {
            "type": "done",
            "reply": reply_text,
            "intent": _intent_payload(intent_result),
            "restock_offer": offer,
            "guardrail": "restock_declined",
            "trace_id": session.trace_id,
            "trace_number": session.trace_number,
            "customer_email": session.customer_email,
        }
    )
