# Build from the REPO ROOT (so engine/ shared/ usecases/ are in the build context):
#   docker build --platform linux/amd64 -f engine/platform_snowflake/Dockerfile -t <image> .
# --platform linux/amd64 matters if you build on an Apple-Silicon/ARM machine; SPCS is amd64.
# Pin the base image by digest for a reproducible, auditable build once you have chosen one:
#   FROM python:3.11-slim@sha256:<digest>      (docker pull python:3.11-slim && docker inspect --format='{{index .RepoDigests 0}}' ...)
FROM python:3.11-slim
WORKDIR /app

# Install the FULLY RESOLVED, HASHED lock (every transitive pinned; generated from requirements.txt next
# to it -- see that file for the regenerate command). --require-hashes: pip refuses any file whose hash
# is not in the lock, so a tampered index or a "newest allowed" transitive can never reach the image.
COPY engine/platform_snowflake/requirements.lock.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt

# Ship the portable code (all three tiers). No secrets, no data.
COPY engine/ engine/
COPY shared/ shared/
COPY usecases/ usecases/

# Run as a NON-ROOT user (least privilege): the app only needs to read /app and write its optional SQLite
# memory file there. Port 8080 needs no privilege.
RUN useradd --system --uid 10001 --no-create-home app && chown -R app:app /app
USER app

ENV USE_CASE=dq_qals \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["uvicorn", "engine.platform_snowflake.agent:app", "--host", "0.0.0.0", "--port", "8080"]
