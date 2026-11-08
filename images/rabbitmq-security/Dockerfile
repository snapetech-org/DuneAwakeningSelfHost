ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG EXPECTED_RABBITMQ_VERSION
RUN test -f /etc/alpine-release \
 && apk upgrade --no-cache --update-cache \
 && test "$(rabbitmq-diagnostics --version)" = "$EXPECTED_RABBITMQ_VERSION" \
 && python3 --version \
 && openssl version
