#!/usr/bin/env bash
# Compuerta local: lock, formato, lint, tipos y pruebas. Se corre desde cualquier carpeta.
set -euo pipefail

cd "$(dirname "$0")/.."

uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q

echo "Compuerta local en verde."
