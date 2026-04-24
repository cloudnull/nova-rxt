ARG NOVA_TAG=2025.1
FROM ghcr.io/rackerlabs/genestack-images/nova:${NOVA_TAG}-latest AS build
ARG NOVA_TAG
USER 0
RUN apt update && apt install -y git
RUN /var/lib/openstack/bin/pip install --upgrade --force-reinstall pip
WORKDIR /opt/nova-rxt
COPY . /opt/nova-rxt
RUN if [ "${NOVA_TAG}" = "2024.1" ]; then \
      RELEASE="unmaintained/2024.1"; \
    else \
      RELEASE="stable/${NOVA_TAG}"; \
    fi; \
    /var/lib/openstack/bin/pip install --no-cache-dir \
                                       --constraint https://opendev.org/openstack/requirements/raw/branch/${RELEASE}/upper-constraints.txt \
                                       /opt/nova-rxt

RUN find /var/lib/openstack -regex '^.*\(__pycache__\|\.py[co]\)$' -delete


FROM ghcr.io/rackerlabs/genestack-images/nova:${NOVA_TAG}-latest
COPY --from=build /var/lib/openstack/. /var/lib/openstack/
