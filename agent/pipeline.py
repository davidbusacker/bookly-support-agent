"""
Turn orchestration — read chat_events() top-to-bottom for the full pipeline.

  classify intent → log trace → closeout | clarify | agent loop → resolution/restock
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
    anthropic_client,
    trace_client,
)
from agent.loop import run_agent_turn
from agent.session import Session, attach_email_from_order, conversation_text
from agent.traces import ensure_trace, sync_trace


def ndjson(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str) + "\n"


def peek(text: str) -> str:
    return ndjson({"type": "status", "text": text})


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


def chat_events(session: Session, session_id: str, user_message: str):
    yield peek("Classifying intent")

    intent_result = classify_intent(
        anthropic_client,
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
            anthropic_client,
            model=CLASSIFIER_MODEL,
            user_message=user_message,
        )
        if confirm.ready_for_offer:
            yield from confirmed_resolution_events(session, user_message, intent_result, confirm)
            return

    if intent_result.needs_clarification:
        yield peek("Intent unclear — asking a clarifying question")
        reply_text = build_clarification_reply(
            anthropic_client,
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
                "intent": intent_result.as_dict(),
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
            anthropic_client,
            model=CLASSIFIER_MODEL,
            history=session.messages,
            latest_reply=reply_text,
        )
        session.last_resolution = resolution.score
        resolution_payload = resolution.as_dict()
        yield peek(f"Resolution {resolution.score:.0%}")
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
        note = f"Customer confirmed resolved — restock offer: {restock['title']} by {restock['author']}"
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
            "intent": intent_result.as_dict(),
            "resolution": {"resolution_score": session.last_resolution, "is_resolved": True},
            "restock_offer": restock,
            "guardrail": "restock_offer" if restock else "resolution_confirmed",
            "trace_id": session.trace_id,
            "trace_number": session.trace_number,
            "customer_email": session.customer_email,
        }
    )
