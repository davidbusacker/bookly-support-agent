"""
Demo restock offer — after a resolved turn, check traces + live inventory.

If this caller previously asked about a title/author that was out of stock,
and that book (or another by the same author) is now in stock, solicit a purchase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import anthropic

from aop_loader import read_aop

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
