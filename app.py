"""
Bookly Support Agent — orchestration layer.

Architecture (AOP-driven, Decagon-style):
  aops/*.md          Agent Operating Policies (behavior — edit these, not Python)
  skills.py          Thin skills referenced by AOPs (verify_phone, read_aop)
  bookly_client.py   Bookly MCP manifest → HTTP
  app.py             Flask + Claude tool-use loop + TTS

Claude reads policies, calls Bookly tools for facts, and decides — business rules are not hard-coded.
"""

import os
import uuid

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
import anthropic
from elevenlabs import ElevenLabs, VoiceSettings

from aop_loader import build_system_prompt
from tts_utils import prepare_text_for_speech
from tools import BOOKLY_AGENT_INSTRUCTIONS, TOOLS_SCHEMA, execute_tool, ping_bookly, tool_result_content

load_dotenv()

app = Flask(__name__)

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
ELEVEN_MODEL_ID = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
ELEVEN_VOICE_SETTINGS = VoiceSettings(
    stability=0.72,
    similarity_boost=0.8,
    style=0.0,
    use_speaker_boost=False,
)

anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
eleven_client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))

CONVERSATIONS: dict[str, list] = {}

# System prompt built from AOP markdown + Bookly API overview
SYSTEM_PROMPT = build_system_prompt(BOOKLY_AGENT_INSTRUCTIONS)


def run_agent_turn(history: list, session_id: str) -> str:
    """Run one user turn through Claude's tool-use loop."""
    response = anthropic_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=TOOLS_SCHEMA,
        messages=history,
    )

    while response.stop_reason == "tool_use":
        history.append({"role": "assistant", "content": response.content})

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

        history.append({"role": "user", "content": tool_results})

        response = anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=TOOLS_SCHEMA,
            messages=history,
        )

    final_text = "".join(block.text for block in response.content if block.type == "text")
    history.append({"role": "assistant", "content": response.content})
    return final_text


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    bookly = ping_bookly()
    return jsonify(
        {
            "status": "ok",
            "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "elevenlabs_configured": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "model": ANTHROPIC_MODEL,
            "bookly_api": {"ok": bookly.get("ok", False), "status": bookly.get("status")},
            "tools_loaded": len(TOOLS_SCHEMA),
            "architecture": "aop-driven",
        }
    )


@app.route("/api/session", methods=["POST"])
def new_session():
    session_id = str(uuid.uuid4())
    CONVERSATIONS[session_id] = []
    return jsonify({"session_id": session_id})


@app.route("/api/chat", methods=["POST"])
def chat():
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
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty text."}), 400
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return jsonify({"error": "ELEVENLABS_API_KEY is not set."}), 500

    try:
        spoken_text = prepare_text_for_speech(text)
        audio_stream = eleven_client.text_to_speech.convert(
            voice_id=ELEVEN_VOICE_ID,
            text=spoken_text,
            model_id=ELEVEN_MODEL_ID,
            voice_settings=ELEVEN_VOICE_SETTINGS,
            output_format="mp3_44100_128",
            seed=42,
        )
        audio_bytes = b"".join(audio_stream)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return Response(audio_bytes, mimetype="audio/mpeg")


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
