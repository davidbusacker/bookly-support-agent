from app.agent.memory import SessionMemory
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import initiate_return, lookup_order, search_knowledge_base
from app.llm.client import INTENT_SLOTS, LLMClient

SLOT_LABELS = {
    "order_id": "order number (e.g. BK-10042)",
    "email": "email address on the order",
    "reason": "reason for the return",
}


class AgentOrchestrator:
    """
    Explicit orchestration pipeline:
    classify intent → collect required slots → execute tool → generate response.

    This keeps tool calls gated behind verified inputs instead of letting the LLM
    freely invoke tools on incomplete or ambiguous requests.
    """

    def __init__(self) -> None:
        self.llm = LLMClient()

    def handle_message(self, session: SessionMemory, user_message: str) -> dict:
        session.add_message("user", user_message)

        # Continue an in-progress flow (slot filling)
        if session.intent and session.pending_slots:
            return self._continue_slot_filling(session, user_message)

        # Fresh turn: classify intent
        parsed = self.llm.classify_and_extract(user_message, session.messages[:-1])
        intent = parsed.get("intent", "unclear")
        slots = parsed.get("slots") or {}

        if intent == "unclear":
            reply = self._clarify_intent()
            session.add_message("assistant", reply)
            return self._build_response(session, reply)

        if intent == "general_question":
            return self._handle_general(session, user_message)

        # Transactional flows need slot collection
        required = INTENT_SLOTS.get(intent, [])
        session.set_intent(intent, required)
        for key, value in slots.items():
            if value and key in required:
                session.fill_slot(key, value)

        if session.pending_slots:
            reply = self._ask_for_slot(session.pending_slots[0])
            session.add_message("assistant", reply)
            return self._build_response(session, reply, awaiting=session.pending_slots[0])

        return self._execute_and_respond(session)

    def _continue_slot_filling(self, session: SessionMemory, user_message: str) -> dict:
        missing = list(session.pending_slots)
        extracted = self.llm.extract_slots(user_message, session.intent or "", missing)
        for key, value in (extracted.get("slots") or {}).items():
            if value and key in session.pending_slots:
                session.fill_slot(key, value)

        if session.pending_slots:
            reply = self._ask_for_slot(session.pending_slots[0])
            session.add_message("assistant", reply)
            return self._build_response(session, reply, awaiting=session.pending_slots[0])

        return self._execute_and_respond(session)

    def _handle_general(self, session: SessionMemory, user_message: str) -> dict:
        result = search_knowledge_base(user_message)
        session.last_tool_result = result
        reply = self.llm.generate_response(
            "general_question",
            {},
            result,
            fallback=result["content"],
        )
        session.add_message("assistant", reply)
        return self._build_response(session, reply, tool_used="search_knowledge_base")

    def _execute_and_respond(self, session: SessionMemory) -> dict:
        intent = session.intent
        slots = dict(session.slots)
        tool_name = None
        result = None

        if intent == "order_status":
            tool_name = "lookup_order"
            result = lookup_order(slots["order_id"], slots.get("email"))
        elif intent == "return_refund":
            tool_name = "initiate_return"
            result = initiate_return(slots["order_id"], slots["email"], slots["reason"])

        session.last_tool_result = result
        fallback = self._fallback_response(intent, result)
        reply = self.llm.generate_response(intent, slots, result, fallback=fallback)
        session.add_message("assistant", reply)
        session.reset_flow()

        return self._build_response(session, reply, tool_used=tool_name, tool_result=result)

    def _fallback_response(self, intent: str | None, result: dict | None) -> str:
        if not result:
            return "I'm sorry, something went wrong. Could you try again?"

        if not result.get("success"):
            return result.get("message", "I couldn't complete that request.")

        if intent == "order_status":
            order = result["order"]
            lines = [
                f"Order **{order['order_id']}** is **{order['status']}**.",
            ]
            if order.get("tracking_number"):
                lines.append(f"Tracking: {order['tracking_number']}.")
            if order.get("estimated_delivery"):
                lines.append(f"Estimated delivery: {order['estimated_delivery']}.")
            items = ", ".join(i["title"] for i in order["items"])
            lines.append(f"Items: {items}.")
            return " ".join(lines)

        if intent == "return_refund":
            ret = result["return"]
            return (
                f"Your return **{ret['return_id']}** is confirmed for order {ret['order_id']}. "
                f"{result['message']}"
            )

        return result.get("content") or result.get("message", "Done.")

    def _clarify_intent(self) -> str:
        return (
            "I'd be happy to help! Are you looking to **check an order status**, "
            "**start a return or refund**, or ask a **general question** about shipping or policies?"
        )

    def _ask_for_slot(self, slot: str) -> str:
        label = SLOT_LABELS.get(slot, slot.replace("_", " "))
        prompts = {
            "order_id": f"Sure — what's your {label}?",
            "email": f"Thanks! What's the {label}?",
            "reason": "Got it. What's the reason for your return?",
        }
        return prompts.get(slot, f"Could you share your {label}?")

    def _build_response(
        self,
        session: SessionMemory,
        reply: str,
        *,
        awaiting: str | None = None,
        tool_used: str | None = None,
        tool_result: dict | None = None,
    ) -> dict:
        return {
            "reply": reply,
            "session_id": session.session_id,
            "intent": session.intent,
            "slots": session.slots,
            "awaiting_slot": awaiting,
            "tool_used": tool_used,
            "tool_result": tool_result,
            "llm_mode": "openai" if self.llm.is_live else "mock",
            "system_prompt": SYSTEM_PROMPT,
        }
