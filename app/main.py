from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.agent import run_agent

app = FastAPI(title="EKT — ИИ-консультант")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CHAT_PATH = Path(__file__).resolve().parent.parent / "static" / "chat.html"
ERROR_REPLY = "Извините, произошла ошибка. Попробуйте ещё раз."


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
    # FastAPI выполняет синхронный агент в пуле потоков, не блокируя веб-сервер.
    try:
        reply = run_agent(payload.message, payload.history)
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("Агент вернул пустой ответ.")
        return {"reply": reply}
    except Exception as error:
        print(f"Ошибка /chat: {type(error).__name__}", flush=True)
        return {"reply": ERROR_REPLY}
