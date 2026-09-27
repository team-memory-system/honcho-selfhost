FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
# The jev extra is installed but unused unless HONCHO_JEV_GATE is set, so the gate
# can be switched on with an environment variable instead of a rebuild.
RUN uv sync --frozen --no-group dev --extra jev
COPY server.py audit.py jev_gate.py ./

RUN addgroup --system app && adduser --system --group app \
    && mkdir -p /data && chown -R app:app /app /data
USER app
EXPOSE 8765
CMD ["/app/.venv/bin/python", "server.py"]
