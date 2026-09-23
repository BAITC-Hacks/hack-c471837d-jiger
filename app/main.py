import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import UploadFile

from app import cart
from app.agent import ERROR_REPLY, log_exception, run_agent
from app.attachments import AttachmentError, attachment_form, prepare_attachments
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


@app.post("/chat", openapi_extra={
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": ChatRequest.model_json_schema()},
            "multipart/form-data": {"schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "default": "", "maxLength": 1000},
                    "history": {"type": "string", "default": "[]", "description": "JSON-список реплик"},
                    "product_ids": {"type": "string", "default": "[]", "description": "JSON-список id товаров"},
                    "files": {"type": "array", "maxItems": 3, "items": {"type": "string", "format": "binary"}},
                },
            }},
        },
    },
})
async def chat(request: Request, response: Response):
    attachment_parts = []
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        if content_type == "multipart/form-data":
            async with attachment_form(request) as form:
                payload = ChatRequest.model_validate({
                    "message": form.get("message", ""),
                    "history": json.loads(form.get("history", "[]")),
                    "product_ids": json.loads(form.get("product_ids", "[]")),
                })
                files = form.getlist("files")
                if any(not isinstance(file, UploadFile) for file in files) or any(
                    isinstance(value, UploadFile) and key != "files"
                    for key, value in form.multi_items()
                ):
                    raise AttachmentError("Прикрепляйте файлы в поле files.", 422)
                if len(payload.message) <= 1000:
                    attachment_parts = await run_in_threadpool(prepare_attachments, files)
        elif not content_type or content_type == "application/json":
            payload = ChatRequest.model_validate(await request.json())
        else:
            raise AttachmentError("Отправьте JSON или форму multipart/form-data.", 415)
    except (ValidationError, json.JSONDecodeError, UnicodeError, TypeError) as error:
        return await invalid_request(request, error)
    except AttachmentError as error:
        return JSONResponse(status_code=error.status_code, content={"reply": error.reply, "products": []})
    session_id = ensure_session(request, response)
    if not payload.message.strip() and not attachment_parts:
        return {"reply": "Пожалуйста, сформулируйте вопрос: какой товар вы ищете?", "products": []}
    if len(payload.message) > 1000:
        return {"reply": "Пожалуйста, сократите сообщение до 1000 символов, чтобы я мог помочь.", "products": []}
    # Извлечение документов и синхронный агент не блокируют цикл веб-сервера.
    try:
        kwargs = {"attachment_parts": attachment_parts} if attachment_parts else {}
        result = await run_in_threadpool(
            run_agent, payload.message, payload.history, payload.product_ids, session_id, **kwargs
        )
        reply = result.get("reply") if isinstance(result, dict) else None
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("Агент вернул пустой ответ.")
        products = result.get("products")
        return {"reply": reply, "products": products if isinstance(products, list) else []}
    except Exception:
        log_exception("Необработанная ошибка /chat")
        return {"reply": ERROR_REPLY, "products": []}
