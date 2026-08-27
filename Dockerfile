FROM python:3.12.14-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
       libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
       libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
       libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
       libcairo2 libasound2t64 fonts-liberation fonts-noto-color-emoji \
       libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt
RUN python -m playwright install chromium

COPY backend /app/backend
COPY frontend /app/frontend
COPY shared /app/shared
COPY voice_agent /app/voice_agent
COPY data /app/data
COPY scripts /app/scripts

ENV PYTHONPATH=/app/backend:/app
ENV PORT=8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
