# Stage 1: install dependencies into an isolated virtualenv
FROM python:3.13-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /venv && \
    /venv/bin/pip install --no-cache-dir --upgrade pip && \
    /venv/bin/pip install --no-cache-dir -r requirements.txt

# Stage 2: lean runtime image — no test files, docs, or dev tools
FROM python:3.13-slim AS runtime

WORKDIR /app

COPY --from=builder /venv /venv
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini .

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
