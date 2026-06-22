FROM python:3.12-slim
WORKDIR /app

# onnxruntime (pulled in by fastembed) needs libgomp at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Pre-download the local embedding model into the image so the first request
# never hits the network. Keep the model name + cache dir in sync with
# app/memory/embeddings.py.
ENV FASTEMBED_CACHE_DIR=/app/.fastembed_cache
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/app/.fastembed_cache')"

COPY app/ app/
EXPOSE 8083
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8083"]
