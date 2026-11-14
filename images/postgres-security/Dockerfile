ARG BASE_IMAGE
ARG GO_BUILDER_IMAGE=golang:1.27.4-alpine3.24@sha256:ea30e25680a5470bac02b08bb0e0a735fb75bd0ab7d431b9b95ee0abef362586
FROM ${GO_BUILDER_IMAGE} AS gosu-build
ARG GOSU_COMMIT=6456aaa0f3c854d199d0f037f068eb97515b7513
RUN apk add --no-cache git \
 && git init /src \
 && git -C /src remote add origin https://github.com/tianon/gosu.git \
 && git -C /src fetch --depth=1 origin "$GOSU_COMMIT" \
 && git -C /src checkout --detach FETCH_HEAD \
 && test "$(git -C /src rev-parse HEAD)" = "$GOSU_COMMIT" \
 && cd /src \
 && go mod download \
 && go mod verify \
 && mkdir -p /out \
 && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -mod=readonly -trimpath -ldflags='-s -w' -o /out/gosu .

FROM ${BASE_IMAGE}
ARG EXPECTED_POSTGRES_VERSION=17.12
COPY --from=gosu-build /out/gosu /usr/local/bin/gosu
RUN test -f /etc/alpine-release \
 && grep -q '^3\.24\.' /etc/alpine-release \
 && apk add --no-cache python3 \
 && test "$(postgres --version | awk '{print $3}')" = "$EXPECTED_POSTGRES_VERSION" \
 && test -f "$(pg_config --pkglibdir)/pg_trgm.so" \
 && test -f "$(pg_config --pkglibdir)/pgcrypto.so" \
 && pg_restore --version \
 && python3 --version \
 && gosu --version | grep -F 'go1.27.4'
