FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY api/app ./app
COPY api/verify ./verify
COPY api/tests ./tests
COPY api/pytest.ini ./pytest.ini
COPY web ./web
COPY compose.yaml ./compose.yaml

RUN mkdir -p /data
ENV DATABASE_PATH=/data/calibration.db

EXPOSE 8000
# 单 worker：写事务串行化是竞争不变量的核心保证（见 README）。
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
