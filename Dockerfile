# ---- Base image ----
FROM python:3.12-slim

# ---- Runtime settings ----
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ---- (任意) OSレイヤを軽く整備：証明書/基本ツール ----
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
# discord.py は "discord" モジュール名でimportされます
# aiohttp はウェブサーバ用に明示的に入れておきます
RUN pip install --upgrade pip && \
    pip install "discord.py>=2.4,<3.0" "aiohttp>=3.9,<4.0"

# ---- App ----
# ※ このDockerfileと同じ階層に app.py を置いてください
COPY . .

# ---- Webヘルスチェック用のポート（コード側のデフォルトは 8080）----
EXPOSE 8080

# ---- コンテナのヘルスチェック（Northflankの外形監視にも有益）----
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8080\")}').read()" || exit 1

# ---- Start ----
# 例: スクリプト名が main.py なら ["python","-u","main.py"] に変更
CMD ["python", "-u", "app.py"]
