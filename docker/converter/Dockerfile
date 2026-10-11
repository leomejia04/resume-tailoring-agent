# DOCX -> PDF converter: headless LibreOffice with metric-compatible fonts.
# Build (from repo root): docker build -t resume-converter:dev docker/converter
# Base pinned by digest; rebuild to pick up Debian security updates.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

# Liberation = Arial/Times/Courier metrics, Carlito = Calibri, Caladea = Cambria.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui \
        fonts-liberation \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f \
    && dpkg-query -W -f='${Package} ${Version}\n' \
        libreoffice-writer-nogui fonts-liberation fonts-crosextra-carlito \
        fonts-crosextra-caladea fonts-dejavu-core > /etc/converter-versions.txt

RUN useradd --system --uid 10001 --home-dir /tmp --shell /usr/sbin/nologin converter
COPY --chmod=0755 convert-docx.sh /usr/local/bin/convert-docx

ENV HOME=/tmp LANG=C.UTF-8
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/convert-docx"]
