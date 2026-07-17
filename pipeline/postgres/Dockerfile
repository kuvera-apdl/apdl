FROM python:3.14-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS python-runtime

FROM postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

# The PostgreSQL image already supplies psql. Copy the matching immutable
# Bookworm Python runtime instead of resolving apt packages from a live mirror.
COPY --from=python-runtime /usr/local /usr/local

COPY migrate.py /usr/local/bin/apdl-postgres-migrate
COPY migrations /migrations

# Build contexts can contain files created with a restrictive umask. The image
# runs as postgres, so normalize copied payload permissions before dropping
# privileges instead of relying on host filesystem modes.
RUN chmod 0555 /usr/local/bin/apdl-postgres-migrate \
    && chmod -R a=rX /migrations

USER postgres

ENTRYPOINT ["/usr/local/bin/python3", "/usr/local/bin/apdl-postgres-migrate"]
