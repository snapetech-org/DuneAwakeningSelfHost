ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN . /etc/os-release && test "$ID:$VERSION_CODENAME" = debian:trixie \
 && test -x /home/steam/steamcmd/steamcmd.sh \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
 && DEBIAN_FRONTEND=noninteractive apt-get purge -y curl libcurl4t64 \
 && DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y \
 && printf 'deb https://deb.debian.org/debian sid main\n' >/etc/apt/sources.list.d/dune-security.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends -t sid \
      bsdutils util-linux libacl1 libsqlite3-0 libsystemd0 libncursesw6 libtinfo6 ncurses-base ncurses-bin perl-base \
 && rm /etc/apt/sources.list.d/dune-security.list \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
