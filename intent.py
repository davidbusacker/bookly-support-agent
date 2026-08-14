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

Be generous with confidence — most messages are clear enough to act on:
- "Where is order BK-10001?" → order_status, 0.95
- "I want to return a damaged book" → return_refund, 0.92
- "Hi" or "Hello" → greeting, 0.90
- "What's your return policy?" → policy_question, 0.90

Reserve confidence BELOW 0.50 only for truly ambiguous messages with no topic, e.g.:
- "help", "something is wrong", "idk", "???"
- Random unrelated text with no support context

When prior turns already established context, factor that in — confidence should rise."""


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


def classify_intent(
    client: anthropic.Anthropic,
    *,
    model: str,
    user_message: str,
    history: list | None = None,
) -> IntentResult:
    """Classify the latest customer message."""
    context_lines: list[str] = []
    if history:
        for msg in history[-6:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                text = str(content)
            if text.strip():
                context_lines.append(f"{role}: {text[:200]}")

    prompt = user_message
    if context_lines:
        prompt = "Recent conversation:\n" + "\n".join(context_lines) + f"\n\nLatest message: {user_message}"

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
Ask whether they need help with an order, return/refund, shipping, account, or something else."""


def build_clarification_reply(
    client: anthropic.Anthropic,
    *,
    model: str,
    user_message: str,
    intent: IntentResult,
) -> str:
    """Generate a natural clarification question for low-confidence turns."""
    response = client.messages.create(
        model=model,
        max_tokens=120,
        system=CLARIFY_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Customer said: {user_message}\nClassifier note: {intent.reasoning}",
            }
        ],
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or (
        "I want to make sure I help with the right thing — is this about an order, "
        "a return, shipping, or your account?"
    )
