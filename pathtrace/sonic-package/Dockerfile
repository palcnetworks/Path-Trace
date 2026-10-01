# PathTrace — SONiC Application Extension image.
#
# Produces a sonic-package-manager-installable Docker image:
#   - carries the SAE manifest as the required com.azure.sonic.manifest label
#   - installs the pathtrace tool and its CLI plugins
#   - runs a lightweight idle service so the SAE service lifecycle is satisfied
#     (PathTrace does its work on demand via `show pathtrace ...`, not as a daemon)
#
# Build:
#   docker build -f sonic-package/Dockerfile -t pathtrace:0.2.0 .
# Save for offline install on a switch:
#   docker save pathtrace:0.2.0 | gzip > pathtrace.gz
# Install on SONiC:
#   sudo sonic-package-manager install --from-tarball pathtrace.gz --enable

FROM debian:bookworm-slim

# --- install the tool ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip python3-redis python3-yaml && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/pathtrace
COPY pathtrace/ ./pathtrace/
COPY pyproject.toml README.md ./
RUN pip install --break-system-packages --no-cache-dir . || pip install --no-cache-dir .
# --- install CLI plugins into the locations the manifest points at ---
COPY sonic-package/cli/ /cli/

# --- SAE manifest (REQUIRED): sonic-package-manager reads this label ---
ARG manifest
LABEL com.azure.sonic.manifest="$manifest"

# Version labels inherited/declared per SAE convention
LABEL com.azure.sonic.versions.libswsscommon="1.0.0"

# --- lightweight idle service so the container stays up for on-demand exec ---
# PathTrace performs no background work; it acts when invoked via CLI.
CMD ["tail", "-f", "/dev/null"]
