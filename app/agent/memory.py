from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionMemory:
    """Per-conversation state for multi-turn slot filling."""

    session_id: str
    intent: str | None = None
    slots: dict[str, str] = field(default_factory=dict)
    pending_slots: list[str] = field(default_factory=list)
    last_tool_result: dict[str, Any] | None = None
    messages: list[dict[str, str]] = field(default_factory=list)

    def add_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def set_intent(self, intent: str, required_slots: list[str]) -> None:
        self.intent = intent
        self.pending_slots = [s for s in required_slots if s not in self.slots]

    def fill_slot(self, name: str, value: str) -> None:
        self.slots[name] = value
        if name in self.pending_slots:
            self.pending_slots.remove(name)

    def is_ready_for_tool(self) -> bool:
        return self.intent is not None and len(self.pending_slots) == 0

    def reset_flow(self) -> None:
        self.intent = None
        self.slots = {}
        self.pending_slots = []
        self.last_tool_result = None


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionMemory] = {}

    def get_or_create(self, session_id: str) -> SessionMemory:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionMemory(session_id=session_id)
        return self._sessions[session_id]
