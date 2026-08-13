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
from elevenlabs import ElevenLabs

from tools import TOOLS_SCHEMA, execute_tool, tool_result_content

# Load secrets from .env before reading os.environ
load_dotenv()

app = Flask(__name__)

# --- Configuration -----------------------------------------------------------

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVEN_MODEL_ID = "eleven_turbo_v2_5"  # low-latency TTS for live voice demo

anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
eleven_client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))

# In-memory session store: session_id -> list of Claude message dicts
CONVERSATIONS: dict[str, list] = {}

# --- System prompt: scope + guardrails for flexible NLU ----------------------

SYSTEM_PROMPT = """You are Riley, a customer support agent for Bookly, an online bookstore.

You help with three things only: order status, returns/refunds, and general
questions about shipping, policies, or password resets. If asked about anything
else (e.g. book recommendations), politely say it's outside what you can help with.

Rules:
- Never invent order details, refund confirmations, or policy text. Always use
  the provided tools to look things up — if you don't have a tool result, say so.
- For order status or refunds, if the customer hasn't given you an order ID,
  ASK for it before calling a tool. Do not guess an order ID.
- For refunds specifically, you need BOTH an order ID and a reason before calling
  initiate_refund. If either is missing, ask a clarifying question first.
- Keep responses short and conversational — this is a live chat/voice interface,
  not an email. Aim for 1-3 sentences unless listing order details.
- If a request is ambiguous (e.g. "I have a problem with my order" with no detail),
  ask a clarifying question rather than guessing what they mean.
"""


def run_agent_turn(history: list) -> str:
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
        max_tokens=1024,
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
                result = execute_tool(block.name, block.input)
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
            max_tokens=1024,
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
    return jsonify(
        {
            "status": "ok",
            "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "elevenlabs_configured": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "model": ANTHROPIC_MODEL,
        }
    )


@app.route("/api/session", methods=["POST"])
def new_session():
    """Create a fresh conversation session."""
    session_id = str(uuid.uuid4())
    CONVERSATIONS[session_id] = []
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
        reply_text = run_agent_turn(history)
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
        audio_stream = eleven_client.text_to_speech.convert(
            voice_id=ELEVEN_VOICE_ID,
            text=text,
            model_id=ELEVEN_MODEL_ID,
            output_format="mp3_44100_128",
        )
        audio_bytes = b"".join(audio_stream)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return Response(audio_bytes, mimetype="audio/mpeg")


if __name__ == "__main__":
    # debug=True for local demo; use localhost so Chrome Web Speech API works
    app.run(debug=True, host="127.0.0.1", port=5000)
