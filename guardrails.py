"""
Orchestrator guardrails — not agent skills.

These run around Riley's turn in app.py. They are never on TOOLS_SCHEMA.
Riley cannot skip them or call them as tools.

  Intent      — before the turn; <50% confidence → clarify, do not guess
  Resolution  — after the reply; ≥90% → ask the customer to confirm
  Restock     — only after confirm; prior OOS ask + live inventory match

Classifiers use CLASSIFIER_MODEL (Haiku) with forced tool_choice.
Skills (verify_phone, read_aop) live in skills.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import anthropic

from aop_loader import read_aop

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


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


def _history_lines(history: list | None, limit: int = 8, *, max_chars: int = 300) -> list[str]:
    if not history:
        return []
    lines: list[str] = []
    for msg in history[-limit:]:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        text = message_plain_text(msg.get("content")).strip()
        if text:
            lines.append(f"{role}: {text[:max_chars]}")
    return lines


# ---------------------------------------------------------------------------
# Intent — classify before each turn; clarify when confidence < 50%
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Resolution — score after each reply; ≥90% asks the customer to confirm
# ---------------------------------------------------------------------------

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


def score_resolution(
    client: anthropic.Anthropic,
    *,
    model: str,
    history: list,
    latest_reply: str,
) -> ResolutionResult:
    convo = "\n".join(_history_lines(history, limit=10, max_chars=400))
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


# ---------------------------------------------------------------------------
# Restock — after customer confirms, match prior OOS interest to live inventory
# ---------------------------------------------------------------------------

OOS_HINTS = (
    "out of stock",
    "out-of-stock",
    "sold out",
    "waitlist",
    "wait list",
    "back in stock",
    "restock",
    "unavailable",
    "when is it back",
    "when will it be",
    "no stock",
    "not in stock",
    "0 units",
    "stock 0",
    "stock=0",
    "no copies",
)


def looks_like_availability_interest(*texts: str) -> bool:
    """Cheap skip: don't snapshot inventory unless someone talked about stock."""
    blob = " ".join(t or "" for t in texts).lower()
    return any(hint in blob for hint in OOS_HINTS)


INVENTORY_PAGE_SIZE = 100

MATCH_TOOL: dict[str, Any] = {
    "name": "report_restock_match",
    "description": "Whether to offer a restocked title after resolving the support issue.",
    "input_schema": {
        "type": "object",
        "properties": {
            "should_offer": {"type": "boolean"},
            "title": {"type": "string", "description": "In-stock title to mention."},
            "author": {"type": "string"},
            "isbn": {"type": "string"},
            "stock": {"type": "integer"},
            "reason": {
                "type": "string",
                "description": "Why this title — prior OOS ask from traces.",
            },
        },
        "required": ["should_offer"],
        "additionalProperties": False,
    },
}


@dataclass
class RestockOffer:
    title: str
    author: str
    isbn: str | None
    stock: int | None
    reason: str
    solicitation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "isbn": self.isbn,
            "stock": self.stock,
            "reason": self.reason,
            "solicitation": self.solicitation,
        }


def fetch_inventory(execute_tool: Callable) -> list[dict[str, Any]]:
    """Snapshot the full Bookly catalog via list_books."""
    books: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = execute_tool("list_books", {"limit": INVENTORY_PAGE_SIZE, "offset": offset})
        if not result.get("ok"):
            break
        page = result.get("data") or []
        if isinstance(page, dict):
            page = page.get("data") or []
        books.extend(page)
        meta = result.get("meta") or {}
        if not meta.get("has_more"):
            break
        offset = meta.get("next_offset")
        if offset is None:
            offset = len(books)
        if not page:
            break
    return books


def compact_inventory(books: list[dict[str, Any]]) -> str:
    lines = []
    for book in books:
        stock = book.get("stock", 0)
        lines.append(
            f"{book.get('title', '?')} | {book.get('author', '?')} | "
            f"stock={stock} | isbn={book.get('isbn', '')}"
        )
    return "\n".join(lines)


def traces_search_blob(traces: list[dict[str, Any]]) -> str:
    """Flatten trace fields the list API actually returns (summary often has the OOS note)."""
    parts: list[str] = []
    for trace in traces:
        parts.append(str(trace.get("transcript_text") or ""))
        parts.append(str(trace.get("subject") or ""))
        parts.append(str(trace.get("summary") or ""))
        parts.append(str(trace.get("intent") or ""))
        tags = trace.get("tags") or []
        if isinstance(tags, list):
            parts.extend(str(tag) for tag in tags)
        for msg in trace.get("messages") or []:
            parts.append(str(msg.get("content") or ""))
    return " ".join(parts)


def compact_traces(traces: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for trace in traces[:8]:
        num = trace.get("trace_number", trace.get("id", "?"))
        subject = trace.get("subject", "")
        summary = (trace.get("summary") or "").strip()
        transcript = (trace.get("transcript_text") or "")[:2500]
        if not transcript:
            msgs = []
            for msg in (trace.get("messages") or [])[:20]:
                role = msg.get("role", "")
                content = (msg.get("content") or "")[:300]
                if content and role in ("customer", "agent"):
                    msgs.append(f"{role}: {content}")
            transcript = "\n".join(msgs)
        header = f"### {num} — {subject}"
        if summary:
            header += f"\nSummary: {summary}"
        chunks.append(f"{header}\n{transcript}")
    return "\n\n".join(chunks) if chunks else "(no prior traces)"


def _policy_text() -> str:
    doc = read_aop("restock-offer")
    return doc.get("content", "") if doc.get("ok") else ""


def find_restock_offer(
    client: anthropic.Anthropic,
    *,
    model: str,
    traces: list[dict[str, Any]],
    current_convo: str,
    inventory: list[dict[str, Any]],
) -> RestockOffer | None:
    if not inventory:
        return None

    match_system = f"""You look for a restock upsell after Bookly support resolved a ticket.

{_policy_text()}

Rules:
- Offer ONLY if prior traces or this chat show the customer asked about a book or author that was out of stock (waitlist, "when is it back", "sold out", agent said out of stock).
- Then check the inventory snapshot. If that title is now in stock, offer it.
- If that exact title is still out, offer another in-stock title by the SAME author.
- If nothing matches, should_offer=false. Do not invent interest.
- Do not offer just because they placed an order — it must be an availability / out-of-stock ask.
"""

    prompt = (
        f"## Current conversation\n{current_convo or '(empty)'}\n\n"
        f"## Prior agent traces for this caller\n{compact_traces(traces)}\n\n"
        f"## Live inventory snapshot (title | author | stock | isbn)\n{compact_inventory(inventory)}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=300,
        system=match_system,
        tools=[MATCH_TOOL],
        tool_choice={"type": "tool", "name": "report_restock_match"},
        messages=[{"role": "user", "content": prompt}],
    )

    match: dict[str, Any] | None = None
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_restock_match":
            match = block.input
            break
    if not match or not match.get("should_offer"):
        return None

    title = (match.get("title") or "").strip()
    author = (match.get("author") or "").strip()
    if not title or not author:
        return None

    solicitation = _write_solicitation(client, model=model, title=title, author=author)
    return RestockOffer(
        title=title,
        author=author,
        isbn=(match.get("isbn") or None),
        stock=match.get("stock"),
        reason=str(match.get("reason", "")),
        solicitation=solicitation,
    )


def _write_solicitation(
    client: anthropic.Anthropic,
    *,
    model: str,
    title: str,
    author: str,
) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=80,
        system=(
            "Write ONE voice-friendly sentence (no markdown) offering a restocked Bookly title. "
            "Direct, not pushy. Name the title and author. Ask if they want to buy. "
            "Do not apologize or add filler."
        ),
        messages=[{"role": "user", "content": f"Title: {title}\nAuthor: {author}"}],
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or f"{title} by {author} is back in stock — want me to put a copy on an order for you?"
