FROM python:3.12-slim
# Docker CLI — only used when CODEGEN_SANDBOX=docker (Option B), so the service can
# launch the per-changeset sandbox container via a mounted Docker socket. The
# default in-process editor does not need it. (docker.io is the full client; it
# can be slimmed to docker-ce-cli from Docker's apt repo if image size matters.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY app/ app/
# Run the API as a non-root user. Untrusted customer/AI code does NOT execute
# here: with CODEGEN_SANDBOX=docker it runs in the ephemeral sandbox container
# (Dockerfile.worker); the default in-process editor runs it in a temp workdir.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser
EXPOSE 8084
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8084"]
