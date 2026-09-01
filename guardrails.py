"""
Orchestrator classifiers (gpt-4o-mini): intent before Riley, resolution after, restock only after confirm.
Not on TOOLS_SCHEMA — pipeline.py calls these; Riley cannot skip or invoke them as tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import re

from openai import OpenAI

from aop_loader import read_aop
from llm import complete_text, complete_tool

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def message_plain_text(content: Any) -> str:
    """Flatten chat message content (string, or leftover list/text blocks) to text."""
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
# Intent — classify before each turn. The loop (pipeline.py) owns the cutoff.
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
                "description": "0.0–1.0 how sure you are of this intent. Do not apply a pass/fail cutoff.",
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

Report a 0–1 confidence for how sure you are. The orchestrator—not you—decides whether that is high enough to proceed. Do not treat any number as a pass/fail cutoff.

Examples:
- "Where is order BK-10001?" → order_status, high confidence
- Agent asked for last 4 of phone; customer says "9876" → keep prior intent (often order_status / return_refund), high confidence
- Agent asked for order number; customer says "BK-10005" → keep prior intent, high confidence
- "I want to return a damaged book" → return_refund, high confidence
- "Hi" or "Hello" → greeting, high confidence
- "What's your return policy?" → policy_question, high confidence
- First message is only "help", "something is wrong", "idk", "???" → unclear, low confidence
- Random unrelated text with no support context and no prior thread → unclear, low confidence

Never treat identity answers (last 4, order id, email) as unclear if the agent just asked for them."""


@dataclass
class IntentResult:
    intent: str
    confidence: float
    reasoning: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "reasoning": self.reasoning,
        }


def classify_intent(
    client: OpenAI,
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

    data = complete_tool(
        client,
        model=model,
        system=CLASSIFY_SYSTEM,
        user=prompt,
        tool=CLASSIFY_TOOL,
        max_tokens=256,
    )
    if not data:
        return IntentResult(intent="unclear", confidence=0.0, reasoning="Classification failed.")
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


CLARIFY_PROMPT = """The orchestrator flagged this turn as too unclear to send to the support agent.
Write ONE short clarifying question (~15–25 words, voice-friendly, no markdown).
Ask whether they need help with an order, return/refund, shipping, account, or something else.
If recent conversation already shows a clear topic, do NOT ignore it — ask a question that fits that thread."""


def build_clarification_reply(
    client: OpenAI,
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
    text = complete_text(
        client,
        model=model,
        system=CLARIFY_PROMPT,
        user=user_content,
        max_tokens=120,
    )
    return text or (
        "I want to make sure I help with the right thing — is this about an order, "
        "a return, shipping, or your account?"
    )


# ---------------------------------------------------------------------------
# Resolution — score after each reply. The loop (pipeline.py) owns the cutoff.
# ---------------------------------------------------------------------------

RESOLVE_TOOL: dict[str, Any] = {
    "name": "report_resolution",
    "description": "Score whether the customer's original request is fully taken care of.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resolution_score": {
                "type": "number",
                "description": "0.0–1.0 how fully the original request is handled. Do not apply a pass/fail cutoff.",
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

Report a 0–1 score. The orchestrator—not you—decides whether that is high enough to close out. Do not treat any number as a pass/fail cutoff.

High only when:
- The asked-for action is done (status given, return opened, refund issued, policy answered), AND
- You are not waiting on the customer (no pending last-4, order number, or clarification).

Keep the score LOW when:
- Still verifying identity
- Still collecting info
- Greeting / small talk only
- You asked a question and are waiting
- The customer just said "thanks" but you never actually resolved anything this chat

Be conservative: high means we took care of everything they asked."""


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
    """Skip the extra classifier call while Riley is still asking a question."""
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolution_score": round(self.score, 3),
            "reasoning": self.reasoning,
        }


def score_resolution(
    client: OpenAI,
    *,
    model: str,
    history: list,
    latest_reply: str,
) -> ResolutionResult:
    convo = "\n".join(_history_lines(history, limit=10, max_chars=400))
    prompt = f"Conversation so far:\n{convo}\n\nLatest agent reply:\n{latest_reply}"
    data = complete_tool(
        client,
        model=model,
        system=RESOLVE_SYSTEM,
        user=prompt,
        tool=RESOLVE_TOOL,
        max_tokens=256,
    )
    if not data:
        return ResolutionResult(score=0.0, reasoning="Resolution scoring failed.")
    score = max(0.0, min(1.0, float(data.get("resolution_score", 0.0))))
    return ResolutionResult(score=score, reasoning=str(data.get("reasoning", "")))


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
    client: OpenAI,
    *,
    model: str,
    user_message: str,
) -> ConfirmResult:
    data = complete_tool(
        client,
        model=model,
        system=CONFIRM_SYSTEM,
        user=user_message,
        tool=CONFIRM_TOOL,
        max_tokens=200,
    )
    if not data:
        return ConfirmResult(confirmed=False, has_new_issue=True, reasoning="Confirm classify failed.")
    return ConfirmResult(
        confirmed=bool(data.get("confirmed")),
        has_new_issue=bool(data.get("has_new_issue")),
        reasoning=str(data.get("reasoning", "")),
    )


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


MATCH_TOOL: dict[str, Any] = {
    "name": "report_restock_match",
    "description": "Whether to offer a restocked title after resolving the support issue.",
    "input_schema": {
        "type": "object",
        "properties": {
            "should_offer": {"type": "boolean"},
            "title": {"type": "string", "description": "In-stock title to mention."},
            "author": {"type": "string"},
            "isbn": {"type": "string", "description": "ISBN of an in-stock format/SKU only."},
            "format": {
                "type": "string",
                "description": "Format of that SKU (hardcover, paperback, ebook, audiobook).",
            },
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

FORMAT_PREFERENCE = ("paperback", "hardcover", "ebook", "audiobook")

RESTOCK_ACCEPT_HINTS = (
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "please",
    "go ahead",
    "love to",
    "i'd like",
    "id like",
    "sounds good",
    "do it",
    "order it",
    "order one",
    "make an order",
    "place the order",
    "buy it",
    "i want it",
    "i'd love",
    "id love",
)

RESTOCK_DECLINE_HINTS = (
    "no thanks",
    "no thank you",
    "not today",
    "maybe later",
    "nope",
    "nah",
    "skip it",
    "don't want",
    "dont want",
    "not interested",
    "not sure",
    "not really",
)


def _blob_has_hint(text: str, hints: tuple[str, ...]) -> bool:
    lowered = " ".join((text or "").lower().split())
    for hint in hints:
        if " " in hint:
            if hint in lowered:
                return True
        elif re.search(rf"\b{re.escape(hint)}\b", lowered):
            return True
    return False


def looks_like_restock_accept(text: str) -> bool:
    if looks_like_restock_decline(text):
        return False
    return _blob_has_hint(text, RESTOCK_ACCEPT_HINTS)


def looks_like_restock_decline(text: str) -> bool:
    return _blob_has_hint(text, RESTOCK_DECLINE_HINTS)


@dataclass
class RestockOffer:
    title: str
    author: str
    isbn: str | None
    format: str | None
    stock: int | None
    reason: str
    solicitation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "isbn": self.isbn,
            "format": self.format,
            "stock": self.stock,
            "reason": self.reason,
            "solicitation": self.solicitation,
        }


def fetch_inventory(execute_tool: Callable) -> list[dict[str, Any]]:
    """Full catalog with per-format stock via get_inventory (not list_books)."""
    result = execute_tool("get_inventory", {})
    if not result.get("ok"):
        return []
    data = result.get("data")
    if isinstance(data, dict):
        books = data.get("books") or []
    elif isinstance(data, list):
        books = data
    else:
        books = []
    return books if isinstance(books, list) else []


def compact_inventory(books: list[dict[str, Any]]) -> str:
    lines = []
    for book in books:
        stock = book.get("stock", 0)
        lines.append(
            f"{book.get('title', '?')} | {book.get('author', '?')} | "
            f"format={book.get('format', '?')} | stock={stock} | isbn={book.get('isbn', '')}"
        )
    return "\n".join(lines)


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def pick_in_stock_sku(
    inventory: list[dict[str, Any]],
    *,
    title: str,
    author: str,
    preferred_isbn: str | None = None,
    preferred_format: str | None = None,
) -> dict[str, Any] | None:
    """Choose a real in-stock SKU. Title can be in stock as paperback while hardcover does not exist."""
    want_title = _norm_text(title)
    want_author = _norm_text(author)

    def in_stock(book: dict[str, Any]) -> bool:
        try:
            return int(book.get("stock") or 0) > 0
        except (TypeError, ValueError):
            return False

    same_title = [
        book
        for book in inventory
        if in_stock(book) and _norm_text(book.get("title")) == want_title
    ]
    if want_author:
        same_title = [
            book for book in same_title if want_author in _norm_text(book.get("author"))
        ]
    same_author = [
        book
        for book in inventory
        if in_stock(book) and want_author and want_author in _norm_text(book.get("author"))
    ]
    pool = same_title or same_author
    if not pool:
        return None

    if preferred_isbn:
        for book in pool:
            if str(book.get("isbn") or "") == str(preferred_isbn):
                return book
    if preferred_format:
        want_fmt = _norm_text(preferred_format)
        for book in pool:
            if _norm_text(book.get("format")) == want_fmt:
                return book
    for fmt in FORMAT_PREFERENCE:
        for book in pool:
            if _norm_text(book.get("format")) == fmt:
                return book
    return pool[0]


def restock_solicitation(title: str, author: str) -> str:
    return (
        f"Hey, looks like {title} by {author} is back in stock — "
        "want me to put in an order for you?"
    )


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
    client: OpenAI,
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
- Each inventory row is one format/SKU. Offer only a row with stock > 0. Never pick a format that is missing or stock=0.
- If that title is now in stock in any format, offer that title (the runtime will pick an in-stock ISBN).
- If that exact title is still out in every format, offer another in-stock title by the SAME author.
- If nothing matches, should_offer=false. Do not invent interest.
- Do not offer just because they placed an order — it must be an availability / out-of-stock ask.
"""

    prompt = (
        f"## Current conversation\n{current_convo or '(empty)'}\n\n"
        f"## Prior agent traces for this caller\n{compact_traces(traces)}\n\n"
        f"## Live inventory snapshot (title | author | format | stock | isbn)\n{compact_inventory(inventory)}"
    )

    match = complete_tool(
        client,
        model=model,
        system=match_system,
        user=prompt,
        tool=MATCH_TOOL,
        max_tokens=300,
    )
    if not match or not match.get("should_offer"):
        return None

    title = (match.get("title") or "").strip()
    author = (match.get("author") or "").strip()
    if not title or not author:
        return None

    sku = pick_in_stock_sku(
        inventory,
        title=title,
        author=author,
        preferred_isbn=(match.get("isbn") or None),
        preferred_format=(match.get("format") or None),
    )
    if not sku:
        return None

    picked_title = str(sku.get("title") or title)
    picked_author = str(sku.get("author") or author)
    return RestockOffer(
        title=picked_title,
        author=picked_author,
        isbn=str(sku.get("isbn") or "") or None,
        format=str(sku.get("format") or "") or None,
        stock=sku.get("stock"),
        reason=str(match.get("reason", "")),
        solicitation=restock_solicitation(picked_title, picked_author),
    )
