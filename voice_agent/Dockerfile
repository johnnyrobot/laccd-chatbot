FROM python:3.12.14-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

WORKDIR /app

# System dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt

# Application code (voice agent imports backend services and shared prompts)
COPY backend /app/backend
COPY shared /app/shared
COPY voice_agent /app/voice_agent

ENV PYTHONPATH=/app/backend:/app

# Download model files (Silero VAD, turn detector, noise cancellation)
RUN python voice_agent/agent.py download-files

CMD ["python", "voice_agent/agent.py", "start"]
