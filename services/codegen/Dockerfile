FROM python:3.12-slim
# git: the in-process editor clones/commits with it.
# docker.io (CLI): only used when CODEGEN_SANDBOX=docker (Option B) to launch the
#   per-changeset sandbox container via a mounted Docker socket; unused otherwise.
# nodejs (Node 20 LTS via NodeSource, bundles npm): the default in-process editor
#   runs the customer repo's build/test command here, so JS/TS repos need a Node
#   toolchain to satisfy detected commands like `npm install && npm run build`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git docker.io curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
# Install the service deps + the coding agent (aider) so the DEFAULT in-process
# editor works out of the box — `make dev-all` needs no extra setup. With
# CODEGEN_SANDBOX=docker the edit runs in the sandbox image instead and this
# bundled aider is simply unused.
RUN pip install --no-cache-dir ".[agent]"
COPY app/ app/
# Run the API as a non-root user. With CODEGEN_SANDBOX=docker, untrusted code runs
# in the ephemeral sandbox container (Dockerfile.worker); by default the in-process
# editor runs it here in a throwaway workdir (fine for trusted/dev repos).
RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser
EXPOSE 8084
# Pin a single worker: the job-concurrency limit (jobs/runner.py) is a per-process
# asyncio.Semaphore, so N workers would multiply effective concurrency to N×limit
# and defeat the host-protection intent. Keep this 1 unless concurrency is moved
# out-of-process (e.g. a Postgres advisory lock / DB running-count).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8084", "--workers", "1"]
