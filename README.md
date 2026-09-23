# hack-c471837d-jiger
Hackathon team repository for JIGER

## Запуск

Все команды выполняются из корня репозитория `hack-c471837d-jiger`.

### Вариант 1 — Docker

Требуются Docker и Docker Compose.

1. При первом запуске создайте файл окружения:

   ```sh
   cp .env.example .env
   ```

2. Заполните `.env`: `OPENAI_API_KEY`, `EKT_API_USER`, `EKT_API_PASSWORD` и `MODEL`.

3. Соберите и запустите приложение:

   ```sh
   docker compose up --build
   ```

4. Откройте [http://localhost:8000](http://localhost:8000).

Папка `data/` подключается в контейнер как `/app/data`; кэш каталога —
`data/products.json`. Если кэша нет, загрузите его командой из локального варианта
ниже перед запуском приложения. Healthcheck проверяет `/docs`.

### Вариант 2 — локально через venv

Требуется Python 3.11 или новее.

1. Создайте виртуальное окружение:

   ```sh
   python -m venv .venv
   ```

2. Активируйте его в PowerShell:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```

   В Linux/macOS:

   ```sh
   source .venv/bin/activate
   ```

3. Установите зависимости:

   ```sh
   python -m pip install -r requirements.txt
   ```

4. Если `.env` ещё не создан, выполните `cp .env.example .env` и заполните
   `OPENAI_API_KEY`, `EKT_API_USER`, `EKT_API_PASSWORD` и `MODEL`.

5. Если отсутствует кэш `data/products.json`, загрузите каталог:

   ```sh
   python fetch_catalog.py
   ```

6. Запустите сервер:

   ```sh
   python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

7. Откройте [http://localhost:8000](http://localhost:8000).

### Запуск тестов

Тесты запускаются локально в активированном `.venv` с установленными зависимостями
из варианта 2. Установите инструменты тестирования:

```sh
python -m pip install pytest pytest-asyncio
```

Обычные тесты — метки `live` и `llm` исключаются настройкой `pyproject.toml`:

```sh
python -m pytest
```

Тесты с меткой `live`:

```sh
python -m pytest -o "addopts=" -m live
```

Тесты с меткой `llm`:

```sh
python -m pytest -o "addopts=" -m llm
```

Опция `-o "addopts="` снимает исключение этих меток по умолчанию. Для `live`
заполните `EKT_API_USER` и `EKT_API_PASSWORD`, для `llm` — `OPENAI_API_KEY` и `MODEL`
в `.env`.

Сейчас в `tests/` подготовлены фикстуры; тестовые функции ещё не добавлены.
До их добавления pytest сообщает `no tests ran`.
