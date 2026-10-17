# NAS / always-on alternative to GitHub Actions: runs `python -m monitor serve`,
# which does the same plan+run cycle every 15 minutes.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 monitor

USER monitor
WORKDIR /app
ENV PATH="/home/monitor/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MONITOR_CONFIG_PATH=/app/config.yaml \
    MONITOR_STATE_PATH=/app/state/state.json

# Claude Code CLI; authenticates with CLAUDE_CODE_OAUTH_TOKEN from .env.
RUN curl -fsSL https://claude.ai/install.sh | bash

COPY --chown=monitor requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=monitor monitor/ monitor/
COPY --chown=monitor prompts/ prompts/

CMD ["python", "-m", "monitor", "serve"]
