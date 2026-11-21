# Preserve the vendor utility payload and CPython 3.12 ABI. Qualification is
# required separately; this build does not authorize a production rollout.
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS supported
USER root
# The vendor utility images use Alpine 3.21. Keep PostgreSQL 17 and the
# vendor payload while moving distribution libraries to a supported release.
RUN case "$(cat /etc/alpine-release)" in \
        3.21.*) sed -i 's@/v3.21/@/v3.22/@g' /etc/apk/repositories ;; \
        3.22.*) ;; *) exit 1 ;; esac \
 && apk upgrade --no-cache --available \
 && grep -q '^3\.22\.' /etc/alpine-release \
 && pg_dump --version | grep -E ' 17\.' \
 && python3 -c 'import psycopg2, ssl'

FROM supported AS builder
RUN test -f /etc/alpine-release \
 && apk upgrade --no-cache --update-cache \
 && apk add --no-cache build-base linux-headers openssl-dev zlib-dev bzip2-dev \
      xz-dev libffi-dev readline-dev sqlite-dev ncurses-dev gdbm-dev \
      util-linux-dev curl tar
WORKDIR /build
RUN curl --fail --show-error --location --retry 3 \
      https://www.python.org/ftp/python/3.12.16/Python-3.12.16.tar.xz -o python.tar.xz \
 && echo 'b5796915dc45ff745cfb4e990a7e7c93d6f546328a102ce31448d69ebc37575c  python.tar.xz' | sha256sum -c - \
 && tar -xJf python.tar.xz \
 && cd Python-3.12.16 \
 && ./configure --prefix=/usr/local --enable-shared \
      --enable-loadable-sqlite-extensions --with-ensurepip=no \
 && make -j2 \
 && make altinstall DESTDIR=/runtime

FROM supported
# COPY merges the standard library without removing vendor site-packages.
COPY --from=builder /runtime/usr/local/ /usr/local/
COPY revendor-pip.py /opt/utility-security/revendor-pip.py
RUN python3 -m pip install --no-cache-dir --upgrade 'pip==26.3' 'setuptools==80.10.2' \
 && python3 /opt/utility-security/revendor-pip.py \
 && python3 -m pip install --no-cache-dir --upgrade 'setuptools==84.2.1' \
 && python3 -m pip check \
 && python3 -c 'import sys, ssl, sqlite3, bz2, lzma, ctypes, readline, psycopg2, debugpy; assert sys.version_info[:3] == (3, 12, 16); print(ssl.OPENSSL_VERSION, sqlite3.sqlite_version, psycopg2.__version__)'
# Keep the server and client ABI on major 17; accept newer distro security patches.
RUN apk add --no-cache --upgrade 'postgresql17>=17.12-r0' 'postgresql17-client>=17.12-r0' 'libpq>=17.12-r0' \
 && pg_dump --version | grep -E ' 17\.' && python3 -c 'import psycopg2'
