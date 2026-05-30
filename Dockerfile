FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl ca-certificates zstd && rm -rf /var/lib/apt/lists/*

# Install ollama binary
RUN curl -fsSL https://ollama.ai/install.sh | sh

# Create a non-root user with specific UID/GID to match common NAS setups
RUN groupadd -g 1000 monitor && \
    useradd -u 1000 -g 1000 -m -s /bin/bash monitor && \
    mkdir -p /app/data && \
    chown -R monitor:monitor /app

WORKDIR /app

COPY --chown=monitor:monitor requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=monitor:monitor poller/ poller/
COPY --chown=monitor:monitor processor/ processor/
COPY --chown=monitor:monitor entrypoint.sh .
RUN chmod +x entrypoint.sh

# Ensure the data directory is writable by the non-root user
RUN mkdir -p /app/data/ollama && chown -R monitor:monitor /app/data

USER monitor

# poller modules importable; processor dir added automatically as script dir
ENV PYTHONPATH=/app/poller

CMD ["/app/entrypoint.sh"]
