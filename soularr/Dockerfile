# Soularr с доработками (soularr_plus.py): обход запрещённых в Soulseek имён, очередь по приоритету,
# повтор с паузой, пропуск синглов, песни которых есть на альбомах. Оригинал остаётся soularr_core.py.
ARG SOULARR_VERSION=1.2.2
FROM ghcr.io/mrusse/soularr:${SOULARR_VERSION}
USER root
RUN mv /app/soularr.py /app/soularr_core.py
COPY soularr_plus.py /app/soularr.py
