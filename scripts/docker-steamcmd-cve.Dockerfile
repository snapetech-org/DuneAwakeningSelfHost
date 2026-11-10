# Build only from a locally verified SteamCMD base image ID.
# Requalify the helper when the upstream image changes.
ARG BASE_IMAGE=cm2network/steamcmd:root
FROM ${BASE_IMAGE}
USER root
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/*
