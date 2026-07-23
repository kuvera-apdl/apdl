FROM debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd

ARG CODEGEN_EGRESS_POLICY_SHA256
ARG DEBIAN_SNAPSHOT=20260623T000000Z
ARG CA_CERTIFICATES_VERSION=20250419
ARG CURL_VERSION=8.14.1-2+deb13u3
ARG SOCAT_VERSION=1.8.0.3-1
ARG SQUID_VERSION=6.13-2+deb13u1
ARG TINI_VERSION=0.19.0-3+b7

RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} trixie main" \
        > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
        "curl=${CURL_VERSION}" \
        "socat=${SOCAT_VERSION}" \
        "squid=${SQUID_VERSION}" \
        "tini=${TINI_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

COPY squid.conf /etc/squid/squid.conf
COPY allowed-domains.txt /etc/squid/allowed-domains.txt
COPY policy.json /etc/squid/codegen-egress-policy.json
COPY entrypoint.sh /usr/local/bin/codegen-egress-proxy
COPY healthcheck.sh /usr/local/bin/codegen-egress-healthcheck

RUN test -n "$CODEGEN_EGRESS_POLICY_SHA256" \
    && test "$CODEGEN_EGRESS_POLICY_SHA256" != "development-unattested" \
    && /usr/sbin/squid -k parse -f /etc/squid/squid.conf \
    && mkdir -p /run/apdl-codegen-egress \
    && chmod 0555 \
        /usr/local/bin/codegen-egress-healthcheck \
        /usr/local/bin/codegen-egress-proxy \
    && chown -R proxy:proxy \
        /etc/squid \
        /run/apdl-codegen-egress \
        /var/log/squid \
        /var/spool/squid

LABEL dev.apdl.codegen.egress.role="proxy" \
      dev.apdl.codegen.egress.policy-sha256="$CODEGEN_EGRESS_POLICY_SHA256"

USER proxy
EXPOSE 3128
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/codegen-egress-proxy"]
CMD []
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=20 \
  CMD ["/usr/local/bin/codegen-egress-healthcheck"]
