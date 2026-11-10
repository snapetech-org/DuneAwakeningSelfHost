ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY setuptools.whl /tmp/dune-setuptools.whl
COPY upgrade-embedded-setuptools.py /tmp/upgrade-embedded-setuptools.py
COPY prune-build-packages.sh /tmp/prune-build-packages.sh

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confold upgrade \
 && python3 /tmp/upgrade-embedded-setuptools.py /tmp/dune-setuptools.whl \
 && apt-get clean \
 && sh /tmp/prune-build-packages.sh \
 && rm -rf /var/lib/apt/lists/* /tmp/dune-setuptools.whl /tmp/upgrade-embedded-setuptools.py /tmp/prune-build-packages.sh
