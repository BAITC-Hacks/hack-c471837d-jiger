FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Keep dependency installation cached when application files change.
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown app:app /app/data

COPY --chown=app:app app/ ./app/
COPY --chown=app:app static/chat.html ./static/chat.html

# Mount the catalog cache at /app/data/products.json at runtime.

# Supply credentials through environment variables when starting the container.
USER 10001:10001

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
