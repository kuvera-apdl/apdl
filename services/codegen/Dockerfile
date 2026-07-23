FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS node-runtime

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf
# git: the in-process editor clones/commits with it.
# docker CLI: only used when CODEGEN_SANDBOX=docker (Option B) to launch the
#   per-changeset sandbox container via a mounted Docker socket; unused otherwise.
#   Install the pinned upstream static client instead of relying on Debian's
#   docker.io package: on current slim images docker-cli is only a recommended
#   package, so --no-install-recommends otherwise produces a controller without
#   the executable container_editor invokes.
# nodejs (Node 20 LTS copied from an immutable official image, including npm):
# the default in-process editor
#   runs the customer repo's build/test command here, so JS/TS repos need a Node
#   toolchain to satisfy detected commands like `npm install && npm run build`.
ARG TARGETARCH
ARG DOCKER_CLI_VERSION=27.5.1
ARG DOCKER_CLI_SHA256_AMD64=4f798b3ee1e0140eab5bf30b0edc4e84f4cdb53255a429dc3bbae9524845d640
ARG DOCKER_CLI_SHA256_ARM64=e6b53725a73763ab3f988c73f8772eaed429754c1a579db5ff11f21990fd1817
ARG DEBIAN_SNAPSHOT=20260623T000000Z
ARG GIT_VERSION=1:2.47.3-0+deb13u1
ARG CURL_VERSION=8.14.1-2+deb13u3
ARG CA_CERTIFICATES_VERSION=20250419
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && echo "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} trixie main" \
        > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        "git=${GIT_VERSION}" \
        "curl=${CURL_VERSION}" \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
    && case "$TARGETARCH" in \
        amd64) docker_arch=x86_64; docker_sha256="$DOCKER_CLI_SHA256_AMD64" ;; \
        arm64) docker_arch=aarch64; docker_sha256="$DOCKER_CLI_SHA256_ARM64" ;; \
        *) echo "Unsupported Docker CLI architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo /tmp/docker.tgz \
        "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
    && echo "${docker_sha256}  /tmp/docker.tgz" | sha256sum -c - \
    && tar -xzf /tmp/docker.tgz --strip-components=1 -C /usr/local/bin docker/docker \
    && rm /tmp/docker.tgz \
    && docker --version \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack
WORKDIR /app
# The API orchestrates isolated workers; it deliberately does not carry Aider or
# execute customer repositories in this credential-bearing process.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app/ app/
# Run the API as a non-root user. Evaluation and Compose deliberately replace
# this UID with the invoking host UID so bind-mounted artifacts stay writable.
# Git only records the executable bit, so a checkout created with a restrictive
# umask can otherwise leave copied source readable only by image UID 1000.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app \
    && chmod -R u=rwX,go=rX /app
USER appuser
EXPOSE 8084
# Pin a single worker: the job-concurrency limit (jobs/runner.py) is a per-process
# asyncio.Semaphore, so N workers would multiply effective concurrency to N×limit
# and defeat the host-protection intent. Keep this 1 unless concurrency is moved
# out-of-process (e.g. a Postgres advisory lock / DB running-count).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8084", "--workers", "1"]
