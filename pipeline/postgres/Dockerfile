FROM python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b AS python-runtime

FROM postgres:16-bookworm@sha256:da788743d2060767375896de4d646f7576f5911461444b372616f19ea61db2ec

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
