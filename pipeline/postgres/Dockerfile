FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY migrate.py /usr/local/bin/apdl-postgres-migrate
COPY migrations /migrations

# Build contexts can contain files created with a restrictive umask. The image
# runs as postgres, so normalize copied payload permissions before dropping
# privileges instead of relying on host filesystem modes.
RUN chmod 0555 /usr/local/bin/apdl-postgres-migrate \
    && chmod -R a=rX /migrations

USER postgres

ENTRYPOINT ["python3", "/usr/local/bin/apdl-postgres-migrate"]
