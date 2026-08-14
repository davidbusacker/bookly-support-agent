"""
Customer intent classification — runs before each agent turn.

Returns intent label + confidence (0–1). Used for the <50% clarification guardrail
and for admin trace auditing on bookly.davidbusacker.com.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anthropic

INTENT_LABELS = (
    "order_status",
    "return_refund",
    "shipping",
    "account",
    "policy_question",
    "greeting",
    "other",
    "unclear",
)

CLASSIFY_TOOL: dict[str, Any] = {
    "name": "report_customer_intent",
    "description": "Report the customer's support intent and your confidence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": list(INTENT_LABELS),
                "description": "Best-fit intent label for this message.",
            },
            "confidence": {
                "type": "number",
                "description": "0.0–1.0. Use high scores (0.85+) when the ask is clear.",
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence explaining the classification.",
            },
        },
        "required": ["intent", "confidence", "reasoning"],
        "additionalProperties": False,
    },
}

CLASSIFY_SYSTEM = """You classify customer messages for Bookly bookstore support.

Judge intent from the FULL conversation, not the latest message alone.
Short replies that answer the agent's last question inherit that topic with HIGH confidence.

Examples:
- "Where is order BK-10001?" → order_status, 0.95
- Agent asked for last 4 of phone; customer says "9876" → keep prior intent (often order_status / return_refund), 0.90+
- Agent asked for order number; customer says "BK-10005" → keep prior intent, 0.90+
- "I want to return a damaged book" → return_refund, 0.92
- "Hi" or "Hello" → greeting, 0.90
- "What's your return policy?" → policy_question, 0.90

Reserve confidence BELOW 0.50 only when the conversation still has no usable topic, e.g.:
- First message is only "help", "something is wrong", "idk", "???"
- Random unrelated text with no support context and no prior thread

Never treat identity answers (last 4, order id, email) as unclear if the agent just asked for them."""


def message_plain_text(content: Any) -> str:
    """Flatten Anthropic message content (str, dict blocks, or SDK objects) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            else:
                if getattr(block, "type", None) == "text":
                    parts.append(str(getattr(block, "text", "") or ""))
        return " ".join(parts)
    return str(content)


@dataclass
class IntentResult:
    intent: str
    confidence: float
    reasoning: str

    @property
    def needs_clarification(self) -> bool:
        return self.confidence < 0.50

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "reasoning": self.reasoning,
            "needs_clarification": self.needs_clarification,
        }


def _history_lines(history: list | None, limit: int = 8) -> list[str]:
    if not history:
        return []
    lines: list[str] = []
    for msg in history[-limit:]:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        text = message_plain_text(msg.get("content")).strip()
        if text:
            lines.append(f"{role}: {text[:300]}")
    return lines


def classify_intent(
    client: anthropic.Anthropic,
    *,
    model: str,
    user_message: str,
    history: list | None = None,
) -> IntentResult:
    """Classify the latest customer message using prior turns for context."""
    context_lines = _history_lines(history)
    if context_lines:
        prompt = (
            "Conversation so far (oldest → newest):\n"
            + "\n".join(context_lines)
            + f"\n\nLatest customer message: {user_message}\n\n"
            "Classify the customer's overall support intent given this thread."
        )
    else:
        prompt = f"Latest customer message: {user_message}"

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=CLASSIFY_SYSTEM,
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "report_customer_intent"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "report_customer_intent":
            data = block.input
            intent = data.get("intent", "unclear")
            if intent not in INTENT_LABELS:
                intent = "unclear"
            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
            return IntentResult(
                intent=intent,
                confidence=confidence,
                reasoning=str(data.get("reasoning", "")),
            )

    return IntentResult(intent="unclear", confidence=0.0, reasoning="Classification failed.")


CLARIFY_PROMPT = """The customer's intent is unclear (confidence below 50%).
Write ONE short clarifying question (~15–25 words, voice-friendly, no markdown).
Ask whether they need help with an order, return/refund, shipping, account, or something else.
If recent conversation already shows a clear topic, do NOT ignore it — ask a question that fits that thread."""


def build_clarification_reply(
    client: anthropic.Anthropic,
    *,
    model: str,
    user_message: str,
    intent: IntentResult,
    history: list | None = None,
) -> str:
    """Generate a natural clarification question for low-confidence turns."""
    context = "\n".join(_history_lines(history, limit=6))
    user_content = (
        f"Conversation so far:\n{context or '(none)'}\n\n"
        f"Latest customer message: {user_message}\n"
        f"Classifier note: {intent.reasoning}"
    )
    response = client.messages.create(
        model=model,
        max_tokens=120,
        system=CLARIFY_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or (
        "I want to make sure I help with the right thing — is this about an order, "
        "a return, shipping, or your account?"
    )
