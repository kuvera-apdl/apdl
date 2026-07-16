FROM ubuntu:24.04

ARG CODEGEN_EGRESS_POLICY_SHA256

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl socat squid tini \
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
