ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG EXPECTED_POSTGRES_VERSION
RUN test -f /etc/alpine-release \
 && apk upgrade --no-cache --update-cache \
 && test "$(postgres --version | awk '{print $3}')" = "$EXPECTED_POSTGRES_VERSION" \
 && pg_restore --version \
 && python3 --version
