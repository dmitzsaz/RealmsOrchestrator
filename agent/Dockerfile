# Runtime image for worlds on every provider: itzg/minecraft-server + the node agent.
#   docker build -t ghcr.io/<you>/realms-runtime:dev agent/
# BASE_IMAGE must be a Debian/Ubuntu based itzg tag (latest, java21, java17, ...),
# not an -alpine one.
ARG BASE_IMAGE=itzg/minecraft-server:latest
FROM ${BASE_IMAGE}

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-aiohttp tini \
 && rm -rf /var/lib/apt/lists/*

COPY agent.py /opt/realms/agent.py

# tini reaps zombies and forwards SIGTERM to the agent, which stops Minecraft
# and uploads a snapshot before the provider's kill timeout.
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/opt/realms/agent.py"]
