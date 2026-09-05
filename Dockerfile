# The API and the approval screen. The static showcase in `site/` is not in here — it is
# built by `scripts/build_site.py` and served by Cloudflare Pages, with no server at all.
#
# Two things about this image are deliberate and load-bearing:
#
# 1. **The project is not installed, only its dependencies are.** `api/ui.py` finds the
#    committed eval results with `Path(__file__).parents[3] / "evals" / "results"`, which
#    resolves correctly only while the code sits at `/app/src/precedent/...`. Installing the
#    wheel would move it into site-packages and that path would silently point at the Python
#    standard library instead. So: deps into a venv, source on `PYTHONPATH`.
# 2. **One process, one writer.** SQLite takes a single writer, so this runs one uvicorn
#    worker and the deployment pins max-instances to 1. Scaling out is not a config change
#    here; it is a database change.

FROM python:3.11-slim-bookworm AS deps

# uv as a pinned binary copied onto a pinned Python, rather than one of Astral's combined
# images: those are tagged by uv version *or* by Python version, never both, so pinning one
# would have left the other floating.
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Only the lock and manifest, so the dependency layer survives every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.11-slim-bookworm

# Continuous replication of the SQLite file to object storage. Optional at runtime — see
# deploy/entrypoint.sh — but always present, so one image serves every environment.
COPY --from=litestream/litestream:0.3.13 /usr/local/bin/litestream /usr/local/bin/litestream

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PRECEDENT_DB_PATH=/data/precedent.db \
    PORT=8080

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv

# `evals/` is here for its results and dataset, not to run the harness: the approval screen
# reports measured figures by reading the same JSON the eval wrote, and the demo seed builds
# its rows from the committed dataset rather than inventing them.
COPY src/ ./src/
COPY evals/ ./evals/
COPY prompts/ ./prompts/
COPY scripts/ ./scripts/
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# The database lives outside the image. Nothing writes into /app at runtime.
RUN useradd --create-home --uid 10001 precedent \
 && mkdir -p /data \
 && chown -R precedent:precedent /data
USER precedent
VOLUME ["/data"]

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
