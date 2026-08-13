import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.memory import SessionStore
from app.agent.orchestrator import AgentOrchestrator

app = FastAPI(title="Bookly Support Agent", version="1.0.0")
orchestrator = AgentOrchestrator()
sessions = SessionStore()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    intent: str | None = None
    slots: dict = Field(default_factory=dict)
    awaiting_slot: str | None = None
    tool_used: str | None = None
    llm_mode: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "llm_mode": "openai" if orchestrator.llm.is_live else "mock"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    session_id = req.session_id or str(uuid.uuid4())
    session = sessions.get_or_create(session_id)

    try:
        result = orchestrator.handle_message(session, req.message.strip())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ChatResponse(
        reply=result["reply"],
        session_id=result["session_id"],
        intent=result.get("intent"),
        slots=result.get("slots", {}),
        awaiting_slot=result.get("awaiting_slot"),
        tool_used=result.get("tool_used"),
        llm_mode=result["llm_mode"],
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
