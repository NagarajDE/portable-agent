# Build from the REPO ROOT:  docker build -f engine/platform_snowflake/Dockerfile .
FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt fastapi uvicorn[standard]

COPY engine/ engine/
COPY shared/ shared/
COPY usecases/ usecases/

ENV USE_CASE=dq_qals
EXPOSE 8080
CMD ["uvicorn", "engine.platform_snowflake.agent:app", "--host", "0.0.0.0", "--port", "8080"]
