from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.agent import ERROR_REPLY, log_exception, run_agent
from app.catalog import load_catalog


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_catalog()
    yield


app = FastAPI(title="EKT — ИИ-консультант", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CHAT_PATH = Path(__file__).resolve().parent.parent / "static" / "chat.html"


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = Field(default_factory=list)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"reply": "Некорректный запрос: message должен быть текстом, history — списком реплик."},
    )


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(CHAT_PATH, media_type="text/html")


@app.post("/chat")
def chat(payload: ChatRequest) -> dict[str, str]:
    if not payload.message.strip():
        return {"reply": "Пожалуйста, сформулируйте вопрос: какой товар вы ищете?"}
    if len(payload.message) > 1000:
        return {"reply": "Пожалуйста, сократите сообщение до 1000 символов, чтобы я мог помочь."}
    # FastAPI выполняет синхронный агент в пуле потоков, не блокируя веб-сервер.
    try:
        reply = run_agent(payload.message, payload.history)
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("Агент вернул пустой ответ.")
        return {"reply": reply}
    except Exception:
        log_exception("Необработанная ошибка /chat")
        return {"reply": ERROR_REPLY}
