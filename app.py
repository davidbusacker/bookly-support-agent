"""
Bookly Support Agent — orchestration layer.

Architecture (prompt-guided, truly agentic):
  Browser (text or mic via Web Speech API)
    -> POST /api/chat   Claude Messages API + hand-rolled tool-use loop
    -> POST /api/tts    ElevenLabs text-to-speech passthrough

Claude decides when to call tools based on the conversation — no hard-coded
slot-filling state machine. Guardrails live in the system prompt + tool schemas.

Voice is I/O at the edges only:
  - STT: browser Web Speech API (Chrome; works on localhost)
  - TTS: ElevenLabs via /api/tts
"""

import os
import uuid

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
import anthropic
from elevenlabs import ElevenLabs, VoiceSettings

from tts_utils import prepare_text_for_speech

from tools import BOOKLY_AGENT_INSTRUCTIONS, TOOLS_SCHEMA, execute_tool, init_session, ping_bookly, tool_result_content

# Load secrets from .env before reading os.environ
load_dotenv()

app = Flask(__name__)

# --- Configuration -----------------------------------------------------------

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
# ElevenLabs TTS — tuned for stable, natural support-agent speech (not turbo/low-latency)
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # Sarah
ELEVEN_MODEL_ID = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
# High stability = fewer weird inflections and hallucinated audio at clip ends
ELEVEN_VOICE_SETTINGS = VoiceSettings(
    stability=0.72,
    similarity_boost=0.8,
    style=0.0,
    use_speaker_boost=False,
)

anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
eleven_client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))

# In-memory session store: session_id -> list of Claude message dicts
CONVERSATIONS: dict[str, list] = {}

# --- System prompt: scope + guardrails for flexible NLU ----------------------

SYSTEM_PROMPT = f"""You are Riley, a friendly Bookly support agent. You talk like a real person on live chat or the phone — casual, warm, and brief.

You have tools that call the live Bookly order-management API on the customer's behalf.
{BOOKLY_AGENT_INSTRUCTIONS}

Recommended flow (internal — do not recite this to the customer):
1. meta for sample IDs/rules if needed
2. Identify customer via email or order number
3. Order status: get_order, get_order_shipments, get_shipment
4. Returns: return_eligibility → get_order_items → create_return
5. Policies: list_policies / get_policy / list_faqs
6. password_reset, address_change, create_ticket when needed

Identity verification (required — enforced by tools):
- Before revealing ANY order or customer info (status, items, address, tracking, refunds, etc.),
  you MUST verify the last 4 digits of the primary phone on file via verify_phone_last_four.
- If they ask about an order, get the order number or email first, then ask for the last 4 digits.
  Do not share order details until verify_phone_last_four returns verified:true.
- If verification fails, ask them to try again — never reveal what the correct digits are.
- Policy/FAQ answers (list_policies, list_faqs) do not require verification.

Loyalty early refund (3+ purchases in last year):
- Normally refunds happen after Bookly receives the return (receive_return / auto_refund).
- Loyal customers with 3+ orders in the last 365 days may get refunded BEFORE the book is received.
- Use check_loyalty_early_refund to confirm eligibility before promising this.
- Flow: create_return → create_refund with return_id (only if loyalty check passes).
- If not eligible, explain they'll get refunded once Bookly gets the book back — keep it casual.

Progressive disclosure (critical — this is a live conversation):
- Say the MINIMUM needed to move the conversation forward. One fact, one question, or one action per turn.
- Answer what they asked — nothing extra. Do not dump order details, item lists, addresses, or totals unprompted.
  • "Where's my order?" → after verify: "Still processing — hasn't shipped yet." STOP. Wait for follow-up.
  • Only add tracking, ETA, or items if they ask for that specifically.
  • "Return policy?" → one sentence. Offer to go deeper only if they ask.
- Never recap tool results they didn't ask for. You have the data; they don't need it all at once.
- Hard target: ~15–30 words per reply. Two short sentences max. Three only if confirming a refund/RMA number.
- One question per turn. Never stack asks ("what's your order number and last four digits?" → ask one, then the other).

Tone:
- Casual, warm, contractions. Like texting a helpful friend who works at Bookly.
- No markdown, no bullets, no sign-offs, no menus, no "I'd be happy to help!"
- If a tool fails, one plain sentence — no error codes.

Rules:
- Never invent order details, tracking, refunds, or policy text — always use tools first.
- Ask for order number or email before lookups if you don't have it.
- Check return_eligibility before create_return or promising a refund.
- Money in API responses is cents — say dollars to the customer.
- If a tool fails, say what happened in plain English, no error codes.
- Book recs are out of scope unless they need a replacement (list_books).
"""


def run_agent_turn(history: list, session_id: str) -> str:
    """
    Run one user turn through Claude's tool-use loop.

    Flow:
      1. Send conversation + tool schemas to Claude
      2. If Claude returns tool_use blocks, execute them and loop
      3. When Claude returns text, that's the reply

    Mutates `history` in place so the session persists across turns.
    """
    response = anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=TOOLS_SCHEMA,
        messages=history,
    )

    # Agentic loop: keep resolving tool calls until Claude responds in text
    while response.stop_reason == "tool_use":
        # Append assistant message that contains tool_use blocks
        history.append({"role": "assistant", "content": response.content})

        # Execute each tool Claude requested and collect results
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input, session_id=session_id)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_result_content(result),
                    }
                )

        # Feed tool results back to Claude as a user message (Anthropic API convention)
        history.append({"role": "user", "content": tool_results})

        response = anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=TOOLS_SCHEMA,
            messages=history,
        )

    # Extract final natural-language reply from text blocks
    final_text = "".join(block.text for block in response.content if block.type == "text")
    history.append({"role": "assistant", "content": response.content})
    return final_text


# --- HTTP routes -------------------------------------------------------------

@app.route("/")
def index():
    """Serve the chat + voice UI."""
    return render_template("index.html")


@app.route("/api/health")
def health():
    """Quick status check for the UI and for debugging."""
    bookly = ping_bookly()
    return jsonify(
        {
            "status": "ok",
            "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "elevenlabs_configured": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "model": ANTHROPIC_MODEL,
            "bookly_api": {
                "ok": bookly.get("ok", False),
                "status": bookly.get("status"),
            },
            "tools_loaded": len(TOOLS_SCHEMA),
        }
    )


@app.route("/api/session", methods=["POST"])
def new_session():
    """Create a fresh conversation session."""
    session_id = str(uuid.uuid4())
    CONVERSATIONS[session_id] = []
    init_session(session_id)
    return jsonify({"session_id": session_id})


@app.route("/api/chat", methods=["POST"])
def chat():
    """
    Handle one user message: append to history, run agent loop, return reply.
    """
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    user_message = data.get("message", "").strip()

    if not session_id or session_id not in CONVERSATIONS:
        return jsonify({"error": "Unknown or missing session_id. Call /api/session first."}), 400
    if not user_message:
        return jsonify({"error": "Empty message."}), 400
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set."}), 500

    history = CONVERSATIONS[session_id]
    history.append({"role": "user", "content": user_message})

    try:
        reply_text = run_agent_turn(history, session_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"reply": reply_text})


@app.route("/api/tts", methods=["POST"])
def tts():
    """
    ElevenLabs TTS passthrough — converts agent reply text to MP3 audio.
    Called by the browser when 'Speak responses aloud' is enabled.
    """
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text."}), 400
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return jsonify({"error": "ELEVENLABS_API_KEY is not set."}), 500

    try:
        # Strip markdown before TTS — raw Claude output causes glitches and misreads
        spoken_text = prepare_text_for_speech(text)
        audio_stream = eleven_client.text_to_speech.convert(
            voice_id=ELEVEN_VOICE_ID,
            text=spoken_text,
            model_id=ELEVEN_MODEL_ID,
            voice_settings=ELEVEN_VOICE_SETTINGS,
            output_format="mp3_44100_128",
            seed=42,  # more consistent tone across turns
        )
        audio_bytes = b"".join(audio_stream)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return Response(audio_bytes, mimetype="audio/mpeg")


if __name__ == "__main__":
    # debug=True for local demo; use localhost so Chrome Web Speech API works
    app.run(debug=True, host="127.0.0.1", port=5000)
