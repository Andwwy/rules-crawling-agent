FROM python:3.12-slim

WORKDIR /app

# System deps minimal — DuckDB is a self-contained wheel; we just need git for
# any source that wants it and curl for debugging.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml /app/
RUN uv pip install --system --no-cache -r pyproject.toml

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

CMD ["sleep", "infinity"]
