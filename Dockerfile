# Imagen de despliegue del proveedor de pagos sintético. Su SQLite vive en el volumen montado.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
CMD ["sh", "-c", "uvicorn tesis_payment_provider.api:app_factory --factory --host 0.0.0.0 --port ${PORT:-8080}"]
