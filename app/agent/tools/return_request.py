import json
import uuid
from datetime import datetime
from pathlib import Path

from app.agent.tools.order_lookup import lookup_order

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "mock_db.json"


def _load_db() -> dict:
    with open(DB_PATH) as f:
        return json.load(f)


def _save_db(db: dict) -> None:
    with open(DB_PATH, "w") as f:
        json.dump(db, f, indent=2)


def initiate_return(order_id: str, email: str, reason: str) -> dict:
    """Start a return/refund request for an eligible order."""
    lookup = lookup_order(order_id, email)
    if not lookup["success"]:
        return lookup

    order = lookup["order"]
    if not order.get("return_eligible"):
        return {
            "success": False,
            "error": "not_eligible",
            "message": "This order is not yet eligible for return (item may not have shipped or been delivered).",
        }

    if order["status"] not in ("delivered", "shipped"):
        return {
            "success": False,
            "error": "not_eligible",
            "message": f"Orders with status '{order['status']}' cannot be returned yet.",
        }

    db = _load_db()
    return_id = f"RET-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "return_id": return_id,
        "order_id": order_id.upper(),
        "email": email,
        "reason": reason,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "refund_estimate_days": "5-7 business days",
    }
    db["returns"][return_id] = record
    _save_db(db)

    return {
        "success": True,
        "return": record,
        "message": f"Return {return_id} initiated. You'll receive a prepaid label by email within 24 hours.",
    }
