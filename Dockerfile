FROM python:3.11.16-slim-bookworm
ARG MONEYPRINTERTURBO_COMMIT=4204d0f39afa8ada6f3edc31604141f959fe254e
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPT_ROOT=/opt/mpt \
    MPT_STARTER_DIR=/opt/worker/starter \
    MPT_DATA_DIR=/data \
    PATH=/opt/mpt/.venv/bin:/usr/local/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates ffmpeg imagemagick fonts-dejavu-core curl gosu \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.12.19

WORKDIR /opt/mpt
# Pin upstream to a reviewed commit rather than installing arbitrary future main.
RUN git init -q && git remote add origin https://github.com/harry0703/MoneyPrinterTurbo.git \
    && git fetch --depth=1 origin "${MONEYPRINTERTURBO_COMMIT}" \
    && git checkout --detach -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${MONEYPRINTERTURBO_COMMIT}" \
    && uv sync --frozen --no-dev --python /usr/local/bin/python

WORKDIR /opt/worker
COPY requirements-api.txt /opt/worker/requirements-api.txt
RUN uv pip install --python /opt/mpt/.venv/bin/python -r requirements-api.txt
COPY workerapp /opt/worker/workerapp
COPY starter /opt/worker/starter
COPY generate_starter_images.py /opt/worker/generate_starter_images.py
RUN /opt/mpt/.venv/bin/python /opt/worker/generate_starter_images.py
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# All queued jobs, uploaded media, completed MP4s and transient render files
# live on the Render persistent disk, outside the read-only Docker image.
RUN rm -rf /opt/mpt/storage && ln -s /data/scratch /opt/mpt/storage \
    && groupadd -g 10001 mpt \
    && useradd -u 10001 -g 10001 -s /usr/sbin/nologin mpt \
    && mkdir -p /data && chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && chown -R mpt:mpt /data /opt/worker /opt/mpt
EXPOSE 10000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["sh","-c","exec uvicorn workerapp.app:create_app --factory --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --no-access-log"]
