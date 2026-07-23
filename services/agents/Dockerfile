FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf
WORKDIR /app

# onnxruntime (pulled in by fastembed) needs libgomp at runtime.
ARG DEBIAN_SNAPSHOT=20260623T000000Z
ARG LIBGOMP_VERSION=14.2.0-19
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && echo "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} trixie main" \
        > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends "libgomp1=${LIBGOMP_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Bake one immutable model snapshot into the image. fastembed's model catalog
# points BAAI/bge-small-en-v1.5 at this quantized ONNX repository, but its
# default downloader follows the mutable repository head.
ENV FASTEMBED_CACHE_DIR=/app/.fastembed_cache \
    EMBEDDING_MODEL_PATH=/app/models/bge-small-en-v1.5 \
    EMBEDDING_MODEL_REPOSITORY=qdrant/bge-small-en-v1.5-onnx-q \
    EMBEDDING_MODEL_REVISION=52398278842ec682c6f32300af41344b1c0b0bb2
RUN python -c "import os; from huggingface_hub import snapshot_download; snapshot_download(repo_id=os.environ['EMBEDDING_MODEL_REPOSITORY'], revision=os.environ['EMBEDDING_MODEL_REVISION'], local_dir=os.environ['EMBEDDING_MODEL_PATH'])" \
    && HF_HUB_OFFLINE=1 python -c "import os; from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir=os.environ['FASTEMBED_CACHE_DIR'], specific_model_path=os.environ['EMBEDDING_MODEL_PATH'], local_files_only=True)"

# Runtime embedding loads must fail closed if the baked snapshot is absent.
ENV HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

COPY app/ app/
COPY scripts/provision_llm_policy.py scripts/provision_llm_policy.py
EXPOSE 8083
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8083"]
