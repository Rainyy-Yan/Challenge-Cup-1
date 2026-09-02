FROM node:24.7.0-bookworm-slim@sha256:0104d9447ea3ddf7373643be7f9915fc7b7c896e41d0d33229338e457217cd78 AS node_runtime

FROM python:3.11.13-slim-bookworm@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1

RUN printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20251020T000000Z bookworm main' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20251020T000000Z bookworm-security main' \
      > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.39.5-0+deb12u2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY tools/qykw/verify_smoke.py /opt/qykw/verify_smoke.py

RUN chmod 0555 /opt/qykw/verify_smoke.py \
    && mkdir -p /workspace \
    && chmod 0755 /workspace

WORKDIR /workspace
USER 65532:65532

ENTRYPOINT ["/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/tmp", "TMPDIR=/tmp", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "LANG=C.UTF-8", "LC_ALL=C.UTF-8"]
CMD ["python", "-c", "import signal; signal.pause()"]
