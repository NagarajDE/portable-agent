# Build from the REPO ROOT (so engine/ shared/ usecases/ are in the build context):
#   docker build --platform linux/amd64 -f engine/platform_snowflake/Dockerfile -t <image> .
# --platform linux/amd64 matters if you build on an Apple-Silicon/ARM machine; SPCS is amd64.
FROM python:3.11-slim
WORKDIR /app

# Install the minimal Snowflake-container deps (NOT the repo-root requirements.txt).
COPY engine/platform_snowflake/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ship the portable code (all three tiers). No secrets, no data.
COPY engine/ engine/
COPY shared/ shared/
COPY usecases/ usecases/

ENV USE_CASE=dq_qals
EXPOSE 8080
CMD ["uvicorn", "engine.platform_snowflake.agent:app", "--host", "0.0.0.0", "--port", "8080"]
