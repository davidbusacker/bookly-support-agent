import json
import os
import re
from typing import Any

from dotenv import load_dotenv

load_dotenv()

INTENT_SLOTS = {
    "order_status": ["order_id", "email"],
    "return_refund": ["order_id", "email", "reason"],
    "general_question": [],
}


class LLMClient:
    """OpenAI wrapper with deterministic mock fallback for demo without API key."""

    def __init__(self) -> None:
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self._client = None
        if self.api_key and not self.api_key.startswith("sk-your"):
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key)

    @property
    def is_live(self) -> bool:
        return self._client is not None

    def classify_and_extract(self, user_message: str, history: list[dict]) -> dict:
        if self._client:
            return self._llm_classify(user_message, history)
        return self._mock_classify(user_message)

    def extract_slots(self, user_message: str, intent: str, missing: list[str]) -> dict:
        if self._client:
            return self._llm_extract_slots(user_message, intent, missing)
        return self._mock_extract_slots(user_message, missing)

    def generate_response(
        self,
        intent: str | None,
        slots: dict,
        tool_result: dict | None,
        fallback: str,
    ) -> str:
        if self._client:
            try:
                return self._llm_generate(intent, slots, tool_result)
            except Exception:
                return fallback
        return fallback

    def _llm_classify(self, user_message: str, history: list[dict]) -> dict:
        from app.agent.prompts import INTENT_CLASSIFICATION_PROMPT, SYSTEM_PROMPT

        messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + INTENT_CLASSIFICATION_PROMPT}]
        messages.extend(history[-6:])
        messages.append({"role": "user", "content": user_message})

        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content or "{}")

    def _llm_extract_slots(self, user_message: str, intent: str, missing: list[str]) -> dict:
        from app.agent.prompts import SLOT_EXTRACTION_PROMPT

        prompt = SLOT_EXTRACTION_PROMPT.format(intent=intent, missing_slots=", ".join(missing))
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content or "{}")

    def _llm_generate(self, intent: str | None, slots: dict, tool_result: dict | None) -> str:
        from app.agent.prompts import RESPONSE_PROMPT

        prompt = RESPONSE_PROMPT.format(
            intent=intent,
            slots=json.dumps(slots),
            tool_result=json.dumps(tool_result) if tool_result else "none",
        )
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": "Write the reply."}],
            temperature=0.4,
        )
        return (resp.choices[0].message.content or "").strip()

    def _mock_classify(self, text: str) -> dict[str, Any]:
        text_lower = text.lower()
        slots: dict[str, str] = {}

        order_match = re.search(r"BK-\d{4,6}", text, re.I)
        if order_match:
            slots["order_id"] = order_match.group().upper()

        email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        if email_match:
            slots["email"] = email_match.group()

        if any(w in text_lower for w in ["return", "refund", "send back", "money back"]):
            intent = "return_refund"
            if "damaged" in text_lower:
                slots["reason"] = "damaged item"
            elif "wrong" in text_lower:
                slots["reason"] = "wrong item received"
            elif "no longer need" in text_lower or "changed my mind" in text_lower:
                slots["reason"] = "changed mind"
        elif any(w in text_lower for w in ["policy", "password", "reset", "contact", "how long", "how do i"]):
            intent = "general_question"
        elif any(
            w in text_lower
            for w in ["where is my order", "order status", "track", "tracking", "delivery status"]
        ) or (
            "order" in text_lower
            and any(w in text_lower for w in ["where", "status", "when", "arrive"])
        ):
            intent = "order_status"
        elif any(w in text_lower for w in ["ship", "delivery"]) and "policy" not in text_lower:
            intent = "order_status"
        else:
            intent = "unclear"

        return {"intent": intent, "slots": slots}

    def _mock_extract_slots(self, text: str, missing: list[str]) -> dict:
        extracted: dict[str, str] = {}
        if "order_id" in missing:
            m = re.search(r"BK-\d{4,6}", text, re.I)
            if m:
                extracted["order_id"] = m.group().upper()
        if "email" in missing:
            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
            if m:
                extracted["email"] = m.group()
        if "reason" in missing and len(text.strip()) > 3:
            if not re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.-]+", text.strip()):
                if not re.search(r"BK-\d{4,6}", text, re.I):
                    extracted["reason"] = text.strip()
        return {"slots": extracted}
