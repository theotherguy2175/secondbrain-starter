FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRAIN_DB=/data/brain.db

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Non-root; /data is the persistent volume.
RUN useradd --uid 10001 --create-home brain \
 && mkdir -p /data/inbox /data/backups && chown 10001:10001 /data
USER 10001

EXPOSE 8080
CMD ["brain", "serve", "--host", "0.0.0.0", "--port", "8080"]
