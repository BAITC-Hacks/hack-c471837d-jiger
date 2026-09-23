from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app import cart
from app.agent import ERROR_REPLY, log_exception, run_agent
from app.cart_page import render_cart_page
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
NO_STORE = {"Cache-Control": "no-store"}


def ensure_session(request: Request, response: Response) -> str:
    """Корзина привязана только к cookie ekt_session, которую выдаёт сервер.

    Неизвестная или подделанная cookie заменяется новой: чужую корзину по ней
    не прочитать и не изменить.
    """
    session_id = request.cookies.get(cart.SESSION_COOKIE)
    if cart.is_known_session(session_id):
        return session_id
    session_id = cart.new_session()
    response.set_cookie(
        cart.SESSION_COOKIE,
        session_id,
        max_age=cart.SESSION_TTL,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cart.public_base_url().startswith("https://"),
    )
    return session_id


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


@app.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request) -> HTMLResponse:
    """Актуальный состав корзины по cookie; без cookie или с чужой — пустая корзина."""
    state = cart.snapshot(request.cookies.get(cart.SESSION_COOKIE))
    return HTMLResponse(render_cart_page(state), headers=NO_STORE)


@app.get("/api/cart")
def cart_api(request: Request) -> JSONResponse:
    state = cart.snapshot(request.cookies.get(cart.SESSION_COOKIE))
    content = {key: state[key] for key in ("items", "count", "total_quantity", "total", "cart_url")}
    return JSONResponse(content, headers=NO_STORE)


@app.post("/chat")
def chat(payload: ChatRequest, request: Request, response: Response) -> dict:
    session_id = ensure_session(request, response)
    if not payload.message.strip():
        return {"reply": "Пожалуйста, сформулируйте вопрос: какой товар вы ищете?", "products": []}
    if len(payload.message) > 1000:
        return {"reply": "Пожалуйста, сократите сообщение до 1000 символов, чтобы я мог помочь.", "products": []}
    # FastAPI выполняет синхронный агент в пуле потоков, не блокируя веб-сервер.
    try:
        result = run_agent(payload.message, payload.history, payload.product_ids, session_id)
        reply = result.get("reply") if isinstance(result, dict) else None
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("Агент вернул пустой ответ.")
        products = result.get("products")
        return {"reply": reply, "products": products if isinstance(products, list) else []}
    except Exception:
        log_exception("Необработанная ошибка /chat")
        return {"reply": ERROR_REPLY, "products": []}
