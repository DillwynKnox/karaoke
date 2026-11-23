# The public guest page. It never touches audio: the base install of the project, without
# the hub extras, carries no torch, no demucs and no ffmpeg.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PATH=/app/.venv/bin:$PATH \
    HTTP_HOST=0.0.0.0 \
    GUEST_PORT=8082

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-editable

RUN useradd --create-home --uid 10001 karaoke
USER karaoke

EXPOSE 8082
CMD ["karaoke", "guest"]
