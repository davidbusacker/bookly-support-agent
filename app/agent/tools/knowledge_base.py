import json
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "mock_db.json"

TOPIC_KEYWORDS = {
    "shipping": ["ship", "shipping", "delivery", "arrive", "tracking", "express"],
    "returns": ["return", "refund", "exchange", "send back"],
    "password_reset": ["password", "reset", "login", "sign in", "forgot"],
    "contact": ["contact", "phone", "email support", "call", "human", "agent"],
}


def search_knowledge_base(query: str) -> dict:
    """Return policy/help content matching the user's question."""
    db = _load_db()
    query_lower = query.lower()

    best_topic = None
    best_score = 0
    for topic, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in query_lower)
        if score > best_score:
            best_score = score
            best_topic = topic

    if best_topic:
        return {
            "success": True,
            "topic": best_topic,
            "content": db["policies"][best_topic],
        }

    return {
        "success": True,
        "topic": "general",
        "content": (
            "Bookly is an online bookstore. We can help with order status, returns, "
            "shipping questions, and account issues. What would you like help with?"
        ),
    }


def _load_db() -> dict:
    with open(DB_PATH) as f:
        return json.load(f)
