# metal_rookie_bot.py
import os
import sys
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv
import discord
from discord.ext import commands

# ---------------------
# .env 読み込み
# ---------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")

# ---------------------
# ログ
# ---------------------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("metal-rookie-bot")

# ---------------------
# import パスの安定化（cogs/ を確実に見せる）
# ---------------------
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ---------------------
# Intents / Bot
# ---------------------
def make_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True  # これが無いと「!」コマンドを拾えません
    intents.messages = True
    intents.guilds = True
    return intents

bot = commands.Bot(command_prefix=PREFIX, intents=make_intents(), help_command=None)

@bot.event
async def on_ready():
    logger.info(f"ログイン成功: {bot.user} (ID: {bot.user.id})")

# ---------------------
# 非同期エントリポイント
# ---------------------
async def main():
    if not DISCORD_TOKEN:
        raise SystemExit("環境変数 DISCORD_TOKEN を設定してください（.env を参照）")

    try:
        # ★ ここが重要：await する
        await bot.load_extension("cogs.notice")
        logger.info("拡張をロードしました: cogs.notice")

        await bot.load_extension("cogs.route")
        logger.info("拡張をロードしました: cogs.route")

    except Exception as e:
        logger.exception("拡張のロードに失敗しました（cogs.notice）: %s", e)
        raise

    # Bot 起動
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
