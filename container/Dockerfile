FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app


RUN apt-get update \
 && apt-get install -y --no-install-recommends bash tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY container/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY container/agent_runner/ /app/agent_runner/
COPY container/skills/ /app/skills/

CMD ["python", "-m", "agent_runner.main"]
