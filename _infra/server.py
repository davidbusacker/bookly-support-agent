"""Flask HTTP layer — routes only; agent logic lives in agent/."""

from __future__ import annotations

import logging
import os
import uuid

from elevenlabs import ElevenLabs, VoiceSettings
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from agent.config import (
    ANTHROPIC_MODEL,
    CLASSIFIER_MODEL,
    INTENT_CONFIDENCE_THRESHOLD,
    RESOLUTION_THRESHOLD,
)
from agent.pipeline import chat_events, ndjson
from agent.session import SESSIONS, Session, refresh_caller_history, update_caller_identity
from tools import TOOLS_SCHEMA, get_bookly_client, ping_bookly
from tts_utils import prepare_text_for_speech

app = Flask(__name__)

ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
ELEVEN_MODEL_ID = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
ELEVEN_VOICE_SETTINGS = VoiceSettings(
    stability=0.72,
    similarity_boost=0.8,
    style=0.0,
    use_speaker_boost=False,
)
eleven_client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))


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
            "classifier_model": CLASSIFIER_MODEL,
            "intent_threshold": INTENT_CONFIDENCE_THRESHOLD,
            "resolution_threshold": RESOLUTION_THRESHOLD,
            "bookly_api": {"ok": bookly.get("ok", False), "status": bookly.get("status")},
            "tools_loaded": len(TOOLS_SCHEMA),
            "trace_tools": "log_agent_trace" in get_bookly_client()._tools_by_name,
            "architecture": "aop-driven",
        }
    )


@app.route("/api/session", methods=["POST"])
def new_session():
    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = Session()
    return jsonify({"session_id": session_id})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    user_message = data.get("message", "").strip()

    if not session_id or session_id not in SESSIONS:
        return jsonify({"error": "Unknown or missing session_id. Call /api/session first."}), 400
    if not user_message:
        return jsonify({"error": "Empty message."}), 400
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set."}), 500

    session = SESSIONS[session_id]
    identity_changed = update_caller_identity(session, user_message)
    refresh_caller_history(session, force=identity_changed)

    def generate():
        try:
            yield from chat_events(session, session_id, user_message)
        except Exception as exc:
            logging.exception("Chat turn failed")
            yield ndjson({"type": "error", "error": str(exc)})

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
