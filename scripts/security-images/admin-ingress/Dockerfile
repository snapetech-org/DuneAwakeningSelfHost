ARG CADDY_IMAGE=caddy:2.11.9-alpine@sha256:fc9cbc100585871d18d4e451cca7021785c90a7006087427ae5ceb3e4efe6424
ARG GO_BUILDER_IMAGE=golang:1.27.4-alpine3.24@sha256:ea30e25680a5470bac02b08bb0e0a735fb75bd0ab7d431b9b95ee0abef362586

FROM ${GO_BUILDER_IMAGE} AS builder

ARG CADDY_VERSION=v2.11.9
ARG CADDY_GEOLOCATION_VERSION=v1.0.3

RUN apk add --no-cache git
WORKDIR /src
COPY caddy-main.go /src/main.go
RUN go mod init caddy \
    && go get "github.com/caddyserver/caddy/v2@${CADDY_VERSION}" \
        "github.com/porech/caddy-maxmind-geolocation@${CADDY_GEOLOCATION_VERSION}" \
    && go get golang.org/x/crypto@latest \
        golang.org/x/net@v0.62.0 \
        golang.org/x/text@latest \
        google.golang.org/grpc@latest \
    && go mod edit -replace=github.com/go-jose/go-jose/v3=github.com/snapetech-org/go-jose-v3/v3@v3.0.6-snape.1 \
    && go get github.com/go-jose/go-jose/v4@v4.1.6 \
    && go get -u golang.org/x/net \
    && go mod tidy \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /usr/bin/caddy .

FROM ${CADDY_IMAGE}

RUN apk update && apk upgrade --no-cache
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
