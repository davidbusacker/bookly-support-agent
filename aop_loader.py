"""
Load Agent Operating Policies (AOPs) from markdown files.

Business rules live in aops/*.md — this module only reads files and builds the system prompt.
"""

from pathlib import Path

AOPS_DIR = Path(__file__).resolve().parent / "aops"

# Loaded into every session (keep small)
STARTUP_AOPS = ("core", "conversation-style")

# Available via read_aop skill
ALL_AOP_IDS = (
    "core",
    "conversation-style",
    "identity-verification",
    "returns-and-refunds",
    "loyalty-early-refund",
)

_AOP_FILES = {
    "core": "core.md",
    "conversation-style": "conversation-style.md",
    "identity-verification": "identity-verification.md",
    "returns-and-refunds": "returns-and-refunds.md",
    "loyalty-early-refund": "loyalty-early-refund.md",
}


def read_aop(policy_id: str) -> dict:
    """Return one AOP by id, or an error dict."""
    filename = _AOP_FILES.get(policy_id)
    if not filename:
        return {"ok": False, "error": f"Unknown policy_id '{policy_id}'. Valid: {list(_AOP_FILES)}"}
    path = AOPS_DIR / filename
    if not path.exists():
        return {"ok": False, "error": f"Policy file missing: {filename}"}
    return {"ok": True, "policy_id": policy_id, "content": path.read_text()}


def build_system_prompt(bookly_manifest_instructions: str) -> str:
    """Assemble Riley's system prompt from startup AOPs + Bookly API overview."""
    sections = [
        "# Riley — Bookly Support Agent\n",
        "Behavior is defined by **Agent Operating Policies (AOPs)** in `aops/`. "
        "Use `read_aop` to load full policy text before identity, return, or loyalty decisions.\n",
    ]

    for policy_id in STARTUP_AOPS:
        doc = read_aop(policy_id)
        if doc.get("ok"):
            sections.append(f"## AOP: {policy_id}\n\n{doc['content']}\n")

    sections.append("## Available AOPs (call read_aop to load)\n")
    for pid in ALL_AOP_IDS:
        if pid not in STARTUP_AOPS:
            sections.append(f"- `{pid}`")

    sections.append(f"\n## Bookly API tools\n\n{bookly_manifest_instructions}")

    return "\n".join(sections)
