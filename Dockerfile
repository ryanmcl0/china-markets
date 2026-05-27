FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl ca-certificates zstd && rm -rf /var/lib/apt/lists/*

# Install ollama binary
RUN curl -fsSL https://ollama.ai/install.sh | sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY poller/ poller/
COPY processor/ processor/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# poller modules importable; processor dir added automatically as script dir
ENV PYTHONPATH=/app/poller

CMD ["/app/entrypoint.sh"]
