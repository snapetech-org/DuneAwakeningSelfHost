ARG BASE_IMAGE
ARG ASPNET_IMAGE
FROM ${BASE_IMAGE} AS vendor
FROM ${ASPNET_IMAGE} AS runtime
FROM runtime AS prepare
USER root
RUN apk add --no-cache python3
ARG APP_DIR
ARG APP
COPY --from=vendor ${APP_DIR}/${APP} /work/bundle
COPY extract.py prepare.py /work/
RUN python3 /work/extract.py /work/bundle /work/app \
 && python3 /work/prepare.py /work/app "$APP" /usr/share/dotnet

FROM vendor
ARG APP_DIR
ARG APP
ARG DOTNET_PATCH
ENV DOTNET_ROOT=/usr/share/dotnet DOTNET_VERSION=${DOTNET_PATCH} DUNE_DOTNET_APP_DLL=${APP}.dll
RUN apk upgrade --no-cache && rm -rf /usr/share/dotnet
COPY --from=runtime /usr/share/dotnet/ /usr/share/dotnet/
COPY --from=prepare /work/app/ ${APP_DIR}/
COPY --chmod=755 launcher.sh ${APP_DIR}/${APP}
