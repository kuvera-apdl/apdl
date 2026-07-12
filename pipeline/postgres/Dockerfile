FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY migrate.py /usr/local/bin/apdl-postgres-migrate
COPY migrations /migrations

USER postgres

ENTRYPOINT ["python3", "/usr/local/bin/apdl-postgres-migrate"]
