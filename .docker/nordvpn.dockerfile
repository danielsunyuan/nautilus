FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        iproute2 \
        iptables \
        procps \
        wget && \
    rm -rf /var/lib/apt/lists/*

RUN bash -lc 'wget -qO- https://downloads.nordcdn.com/apps/linux/install.sh | bash -s -- -n -p nordvpn'

COPY .docker/nordvpn_entrypoint.sh /usr/local/bin/nordvpn-entrypoint
COPY .docker/nordvpn_healthcheck.sh /usr/local/bin/nordvpn-healthcheck

RUN chmod +x /usr/local/bin/nordvpn-entrypoint /usr/local/bin/nordvpn-healthcheck

ENTRYPOINT ["/usr/local/bin/nordvpn-entrypoint"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 CMD ["/usr/local/bin/nordvpn-healthcheck"]
CMD ["sleep", "infinity"]
