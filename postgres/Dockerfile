# PostgreSQL for the governance layer. Build: docker build -t genai-gateway-db postgres/
# On the first start (empty data directory) the official entrypoint creates the role POSTGRES_USER with
# POSTGRES_PASSWORD (or POSTGRES_PASSWORD_FILE) and the database POSTGRES_DB; network logins use scram-sha-256.
# The app creates its own tables. The password is never baked into the image: pass it at run time.
# Data lives in /var/lib/postgresql/data: mount a volume there or it is lost with the container.
FROM postgres:17-bookworm

ENV POSTGRES_USER=gateway \
    POSTGRES_DB=gateway

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 \
  CMD pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" || exit 1
