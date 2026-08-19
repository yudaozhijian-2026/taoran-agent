FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV DSM_TAORAN_ENVIRONMENT=production
ENV DSM_TAORAN_DATABASE_PATH=/data/taoran_agent.db
EXPOSE 8030
CMD ["uvicorn", "taoran_agent.api:app", "--host", "0.0.0.0", "--port", "8030"]

