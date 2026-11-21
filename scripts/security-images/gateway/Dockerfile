ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN grep -q '^3\.24\.' /etc/alpine-release \
 && apk upgrade --no-cache \
 && apk add --no-cache --upgrade 'krb5-libs>=1.22.3-r0' 'libpq>=18.7-r0' \
 && python -c 'import psycopg2'
