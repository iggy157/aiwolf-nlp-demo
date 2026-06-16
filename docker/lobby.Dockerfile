# ロビーbackend（FastAPI）＋ agent-llm 同梱。build context = 作業ルート(aiwolf-nlp-demo/)
# HANDOFF §6: AIエージェントはサービス化せず、ロビーイメージに同梱して subprocess 起動する。
FROM python:3.11-slim

WORKDIR /app

# --- agent-llm を同梱しインストール（langchain 等の依存も入る）---
COPY repos/aiwolf-nlp-agent-llm/ /app/agent-llm/
RUN pip install --no-cache-dir /app/agent-llm

# --- lobby 依存 ---
COPY lobby/requirements.txt /app/lobby/requirements.txt
RUN pip install --no-cache-dir -r /app/lobby/requirements.txt

# --- アプリ本体・設定テンプレ ---
COPY lobby/ /app/lobby/
COPY configs/ /app/configs/

ENV AGENT_LLM_DIR=/app/agent-llm \
    AGENTS_DIR=/app/configs/agents \
    DEFAULT_LANGUAGE=ja \
    AGENT_LLM_PYTHON=python \
    GENERATED_CONFIG_DIR=/app/lobby/.generated

WORKDIR /app/lobby
EXPOSE 8002
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
