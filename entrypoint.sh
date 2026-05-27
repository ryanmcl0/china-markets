#!/bin/bash
set -e

export OLLAMA_MODELS="${OLLAMA_MODELS:-/app/data/ollama}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-0s}"
mkdir -p "$OLLAMA_MODELS"

ollama serve &

echo "Waiting for ollama..."
for i in $(seq 1 60); do
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "Ollama ready"
        break
    fi
    sleep 2
done

MODEL="${OLLAMA_MODEL:-qwen2.5:3b-instruct-q4_K_M}"
if ! ollama list 2>/dev/null | grep -qF "$MODEL"; then
    echo "Pulling $MODEL — this takes a few minutes on first start..."
    ollama pull "$MODEL"
fi

exec python /app/processor/worker.py
