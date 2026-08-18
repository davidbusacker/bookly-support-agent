"""Bookly admin trace create/append — runs on a background thread pool."""

from __future__ import annotations

import logging
from typing import Any

from guardrails import IntentResult
from trace_client import extract_trace_id, extract_trace_number, trace_message

from agent.config import _TRACE_POOL, trace_client
from agent.session import Session


def ensure_trace(
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


def sync_trace(session: Session, messages: list[dict[str, Any]], **update_fields: Any) -> None:
    if not session.trace_id or not messages:
        return
    _TRACE_POOL.submit(_sync_trace_worker, session.trace_id, list(messages), dict(update_fields))
