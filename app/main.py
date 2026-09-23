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
    # id товаров, уже показанных в чате: контекст для уточняющих вопросов.
    product_ids: list[int] = Field(default_factory=list, max_length=20)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "reply": "Некорректный запрос: message должен быть текстом, history — списком реплик.",
            "products": [],
        },
    )


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(CHAT_PATH, media_type="text/html")


@app.post("/chat")
def chat(payload: ChatRequest) -> dict:
    if not payload.message.strip():
        return {"reply": "Пожалуйста, сформулируйте вопрос: какой товар вы ищете?", "products": []}
    if len(payload.message) > 1000:
        return {"reply": "Пожалуйста, сократите сообщение до 1000 символов, чтобы я мог помочь.", "products": []}
    # FastAPI выполняет синхронный агент в пуле потоков, не блокируя веб-сервер.
    try:
        result = run_agent(payload.message, payload.history, payload.product_ids)
        reply = result.get("reply") if isinstance(result, dict) else None
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("Агент вернул пустой ответ.")
        products = result.get("products")
        return {"reply": reply, "products": products if isinstance(products, list) else []}
    except Exception:
        log_exception("Необработанная ошибка /chat")
        return {"reply": ERROR_REPLY, "products": []}
