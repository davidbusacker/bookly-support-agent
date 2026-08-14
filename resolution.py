"""
Resolution score — did Riley finish the customer's request this turn?

Logged to Bookly traces (metadata.resolution_score until the dedicated API field lands).
Triggers a "did we resolve everything?" check when score >= 0.90.
A restock offer only runs after the customer confirms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anthropic

RESOLVE_TOOL: dict[str, Any] = {
    "name": "report_resolution",
    "description": "Score whether the customer's original request is fully taken care of.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resolution_score": {
                "type": "number",
                "description": "0.0–1.0. Use 0.90+ only when nothing remains for this request.",
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence.",
            },
        },
        "required": ["resolution_score", "reasoning"],
        "additionalProperties": False,
    },
}

RESOLVE_SYSTEM = """You score whether Bookly support has fully handled the customer's request.

High score (0.90–1.0) only when:
- The asked-for action is done (status given, return opened, refund issued, policy answered), AND
- You are not waiting on the customer (no pending last-4, order number, or clarification).

Keep the score LOW when:
- Still verifying identity
- Still collecting info
- Greeting / small talk only
- You asked a question and are waiting
- The customer just said "thanks" but you never actually resolved anything this chat

Be conservative. 0.90+ means "we took care of everything they asked." """


WRITE_TOOLS = frozenset(
    {
        "create_return",
        "create_refund",
        "update_refund",
        "cancel_order",
        "cancel_return",
        "create_ticket",
        "receive_return",
        "reship_order",
        "address_change",
        "password_reset",
    }
)


def should_score_resolution(*, reply_text: str, tools_this_turn: list[str]) -> bool:
    """Skip the extra Claude call while Riley is still asking a question."""
    if any(name in WRITE_TOOLS for name in tools_this_turn):
        return True
    text = (reply_text or "").strip()
    if not text:
        return False
    if text.endswith("?"):
        return False
    lowered = text.lower()
    if any(p in lowered for p in ("last 4", "last four", "order number", "email address")):
        return False
    return True


@dataclass
class ResolutionResult:
    score: float
    reasoning: str

    @property
    def is_resolved(self) -> bool:
        return self.score >= 0.90

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolution_score": round(self.score, 3),
            "reasoning": self.reasoning,
            "is_resolved": self.is_resolved,
        }


def _history_text(history: list | None, limit: int = 10) -> str:
    if not history:
        return ""
    lines: list[str] = []
    for msg in history[-limit:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            text = " ".join(
                block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                for block in content
                if (isinstance(block, dict) and block.get("type") == "text")
                or getattr(block, "type", None) == "text"
            )
        else:
            text = str(content)
        if text.strip() and role in ("user", "assistant"):
            lines.append(f"{role}: {text[:400]}")
    return "\n".join(lines)


def score_resolution(
    client: anthropic.Anthropic,
    *,
    model: str,
    history: list,
    latest_reply: str,
) -> ResolutionResult:
    convo = _history_text(history)
    prompt = f"Conversation so far:\n{convo}\n\nLatest agent reply:\n{latest_reply}"

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=RESOLVE_SYSTEM,
        tools=[RESOLVE_TOOL],
        tool_choice={"type": "tool", "name": "report_resolution"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "report_resolution":
            data = block.input
            score = max(0.0, min(1.0, float(data.get("resolution_score", 0.0))))
            return ResolutionResult(score=score, reasoning=str(data.get("reasoning", "")))

    return ResolutionResult(score=0.0, reasoning="Resolution scoring failed.")


CONFIRM_QUESTION = "Did we resolve everything today?"

CONFIRM_TOOL: dict[str, Any] = {
    "name": "report_resolution_confirm",
    "description": "Did the customer confirm the support issue is fully resolved?",
    "input_schema": {
        "type": "object",
        "properties": {
            "confirmed": {
                "type": "boolean",
                "description": "True if they said the issue is taken care of / they're good.",
            },
            "has_new_issue": {
                "type": "boolean",
                "description": "True if they also raised a new question or problem.",
            },
            "reasoning": {"type": "string"},
        },
        "required": ["confirmed", "has_new_issue", "reasoning"],
        "additionalProperties": False,
    },
}

CONFIRM_SYSTEM = """The agent just asked whether everything was resolved today.
Classify the customer's reply.

confirmed=true: yes, all good, thanks, that's all, we're done, yup, etc.
has_new_issue=true: they still need help (another order, a return, a complaint).

If they confirm AND raise something new, confirmed=true AND has_new_issue=true.
Unclear / off-topic: confirmed=false, has_new_issue=true so support can continue."""


@dataclass
class ConfirmResult:
    confirmed: bool
    has_new_issue: bool
    reasoning: str

    @property
    def ready_for_offer(self) -> bool:
        return self.confirmed and not self.has_new_issue

    def as_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "has_new_issue": self.has_new_issue,
            "reasoning": self.reasoning,
        }


DONE_PHRASES = (
    "that's everything",
    "thats everything",
    "that's all",
    "thats all",
    "that is all",
    "that's it",
    "thats it",
    "i'm good",
    "im good",
    "i'm all set",
    "im all set",
    "all set",
    "nothing else",
    "we're done",
    "we are done",
    "nope i'm good",
    "nope im good",
    "no thanks",
    "no thank you",
)


def looks_like_customer_done(text: str) -> bool:
    """Unsolicited close-out — treat as confirm even if we never asked."""
    lowered = " ".join((text or "").lower().split())
    return any(phrase in lowered for phrase in DONE_PHRASES)


def classify_resolution_confirm(
    client: anthropic.Anthropic,
    *,
    model: str,
    user_message: str,
) -> ConfirmResult:
    response = client.messages.create(
        model=model,
        max_tokens=200,
        system=CONFIRM_SYSTEM,
        tools=[CONFIRM_TOOL],
        tool_choice={"type": "tool", "name": "report_resolution_confirm"},
        messages=[{"role": "user", "content": user_message}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_resolution_confirm":
            data = block.input
            return ConfirmResult(
                confirmed=bool(data.get("confirmed")),
                has_new_issue=bool(data.get("has_new_issue")),
                reasoning=str(data.get("reasoning", "")),
            )
    return ConfirmResult(confirmed=False, has_new_issue=True, reasoning="Confirm classify failed.")
