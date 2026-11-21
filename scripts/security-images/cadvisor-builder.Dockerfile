ARG GO_BUILDER_IMAGE
FROM ${GO_BUILDER_IMAGE} AS compiler
FROM ghcr.io/google/cadvisor@sha256:b8e7d1093144fd088f425ff003d75a4aa405de075db78dae3bc563730b1bd07a
USER root
RUN apk --no-cache add build-base linux-headers git curl
COPY --from=compiler /usr/local/go /usr/local/go
ENV PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN curl --connect-timeout 15 --max-time 180 -fL https://sourceforge.net/projects/perfmon2/files/libpfm4/libpfm-4.11.0.tar.gz -o /tmp/libpfm.tar.gz && echo '112bced9a67d565ff0ce6c2bb90452516d1183e5  /tmp/libpfm.tar.gz' | sha1sum -c - && tar -xzf /tmp/libpfm.tar.gz -C /tmp && mkdir -p /usr/local/include && cp -a /tmp/libpfm-4.11.0/include/perfmon /usr/local/include/ && rm -rf /tmp/libpfm-4.11.0 /tmp/libpfm.tar.gz
ENTRYPOINT []
