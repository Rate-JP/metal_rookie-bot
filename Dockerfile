# ---- Base image ----
FROM python:3.12-slim

# ---- Runtime settings ----
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 起動スクリプトを環境変数で差し替え可能に
# 既定は metal_rookie_bot.py
ENV PY_ENTRYPOINT=metal_rookie_bot.py

WORKDIR /app

# ---- (任意) ベース整備 ----
# ca-certificates は HTTPS アクセスや一部ライブラリであると安心
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
# 既存の requirements.txt を利用（ファイルがある前提）
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---- App ----
COPY . .

# ---- Webヘルスチェック用ポート（コードのデフォルトは 8080）----
EXPOSE 8080

# ---- コンテナのヘルスチェック ----
# Northflank のヘルスチェックにも使えるよう、ローカルHTTPを叩く
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python - <<'PY' || exit 1
import os, urllib.request
port = os.getenv("PORT", "8080")
urllib.request.urlopen(f"http://127.0.0.1:{port}/").read()
PY

# ---- Start ----
# 環境変数の PY_ENTRYPOINT（既定: metal_rookie_bot.py）を起動
SHELL ["/bin/sh", "-lc"]
CMD python -u "${PY_ENTRYPOINT}"
