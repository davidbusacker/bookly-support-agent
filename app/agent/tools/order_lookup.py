import json
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "mock_db.json"


def _load_db() -> dict:
    with open(DB_PATH) as f:
        return json.load(f)


def lookup_order(order_id: str, email: str | None = None) -> dict:
    """Look up order status. Returns structured result or error."""
    db = _load_db()
    order = db["orders"].get(order_id.upper())

    if not order:
        return {
            "success": False,
            "error": "order_not_found",
            "message": f"No order found with ID {order_id.upper()}.",
        }

    if email and order["customer_email"].lower() != email.lower():
        return {
            "success": False,
            "error": "email_mismatch",
            "message": "The email provided does not match this order.",
        }

    return {"success": True, "order": order}
