"""
Reads aops/*.md, builds the system-prompt catalog (title + when to load), inlines startup policies.
Adding a policy is a new markdown file with frontmatter; format_aop_catalog() is what Claude sees.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

AOPS_DIR = Path(__file__).resolve().parent / "aops"


@dataclass(frozen=True)
class AopMeta:
    policy_id: str
    title: str
    description: str
    startup: bool
    body: str
    path: Path


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split optional YAML-ish --- frontmatter from markdown body."""
    if not text.startswith("---"):
        return {}, text
    rest = text[3:].lstrip("\n")
    end = rest.find("\n---")
    if end < 0:
        return {}, text
    raw = rest[:end]
    body = rest[end + 4 :].lstrip("\n")
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().lower()] = value.strip().strip('"').strip("'")
    return fields, body


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in {"1", "true", "yes"}


def load_aops() -> list[AopMeta]:
    """Discover every policy markdown file in aops/."""
    found: list[AopMeta] = []
    for path in sorted(AOPS_DIR.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        fields, body = _parse_frontmatter(path.read_text())
        policy_id = path.stem
        title = fields.get("title") or policy_id.replace("-", " ").title()
        description = fields.get("description") or f"Call read_aop with policy_id `{policy_id}` when relevant."
        found.append(
            AopMeta(
                policy_id=policy_id,
                title=title,
                description=description,
                startup=_truthy(fields.get("startup")),
                body=body,
                path=path,
            )
        )
    return found


def _catalog() -> list[AopMeta]:
    return load_aops()


def all_aop_ids() -> tuple[str, ...]:
    return tuple(a.policy_id for a in _catalog())


def startup_aop_ids() -> tuple[str, ...]:
    ids = [a.policy_id for a in _catalog() if a.startup]
    if "core" in ids:
        ids.remove("core")
        return ("core", *sorted(ids))
    return tuple(sorted(ids))


# Back-compat names used in comments / older call sites.
ALL_AOP_IDS = all_aop_ids()
STARTUP_AOPS = startup_aop_ids()


def get_aop(policy_id: str) -> AopMeta | None:
    for aop in _catalog():
        if aop.policy_id == policy_id:
            return aop
    return None


def read_aop(policy_id: str) -> dict:
    """Return one AOP by id, or an error dict. Body has frontmatter stripped."""
    aop = get_aop(policy_id)
    if not aop:
        return {
            "ok": False,
            "error": f"Unknown policy_id '{policy_id}'. Valid: {list(all_aop_ids())}",
        }
    return {
        "ok": True,
        "policy_id": aop.policy_id,
        "title": aop.title,
        "description": aop.description,
        "content": aop.body,
    }


def format_aop_catalog() -> str:
    """Title + description for every AOP — this is how Riley knows what exists."""
    catalog = {a.policy_id: a for a in _catalog()}
    ordered = [catalog[i] for i in startup_aop_ids() if i in catalog]
    ordered.extend(sorted((a for a in catalog.values() if not a.startup), key=lambda a: a.policy_id))
    lines = [
        "## AOP catalog",
        "",
        "These are all Bookly Agent Operating Policies. "
        "If a situation matches an on-demand policy, call `read_aop` with that `policy_id` "
        "before acting. Do not invent a policy that is not listed.",
        "",
    ]
    for aop in ordered:
        how = "already loaded below" if aop.startup else "call `read_aop` to load full text"
        lines.append(f"- `{aop.policy_id}` — **{aop.title}** ({how})")
        lines.append(f"  {aop.description}")
    return "\n".join(lines)


def build_system_prompt(bookly_manifest_instructions: str) -> str:
    """Assemble Riley's system prompt from the AOP catalog + startup full text."""
    catalog = {a.policy_id: a for a in _catalog()}
    sections = [
        "# Riley — Bookly Support Agent\n",
        "Behavior is defined by **Agent Operating Policies (AOPs)** in `aops/`. "
        "The catalog below is the index of every policy. "
        "Use `read_aop` to load full text for any on-demand AOP before you act on it.\n",
        format_aop_catalog(),
        "",
    ]

    for policy_id in startup_aop_ids():
        aop = catalog.get(policy_id)
        if aop:
            sections.append(f"## AOP: {aop.policy_id} — {aop.title}\n\n{aop.body}\n")

    sections.append(f"\n## Bookly API tools\n\n{bookly_manifest_instructions}")
    return "\n".join(sections)
