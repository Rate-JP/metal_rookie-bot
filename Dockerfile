# ---- Base image ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ENV PY_ENTRYPOINT=metal_rookie_bot.py
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# EXPOSE は任意（HTTPサーバを立てないなら不要）
# EXPOSE 8080

SHELL ["/bin/sh", "-lc"]
CMD python -u "${PY_ENTRYPOINT}"
