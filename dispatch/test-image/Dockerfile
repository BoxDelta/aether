# Test image for the dispatcher's local path (ADR-030 step 4b). Built by
# dispatch/local.py from main's pyproject.toml, uv.lock and .python-version only;
# the model's diff never reaches the build. Tests run with --network none.
# Pinned by digest (python:3.14-slim, 2026-11-27); bump on purpose.
FROM python:3.14-slim@sha256:f57c4c23b985d95deea5bc5249e2f9c8a512b4cc9d5d3fd31900cbd2f5f0a604
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.8
ENV UV_PROJECT_ENVIRONMENT=/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
WORKDIR /deps
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-install-project && rm -rf /root/.cache
ENV PATH=/venv/bin:$PATH
