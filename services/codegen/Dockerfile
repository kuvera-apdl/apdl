FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY app/ app/
# Run the API as a non-root user. (The untrusted sandbox where customer/AI code
# executes is the self-hosted EnvironmentWorker container, hardened separately.)
RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser
EXPOSE 8084
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8084"]
