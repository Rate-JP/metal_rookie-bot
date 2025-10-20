# cogs/update.py
import os
import logging
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

logger = logging.getLogger("metal-rookie-bot")

# ---------------------
# 環境変数
# ---------------------
load_dotenv()
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# ---------------------
# 送信ユーティリティ
# ---------------------
async def ensure_channel(client: discord.Client, channel_id: int) -> discord.abc.Messageable:
    ch = client.get_channel(channel_id)
    if ch is None:
        ch = await client.fetch_channel(channel_id)
    return ch

def build_update_text() -> str:
    # notice.py の @silent 仕様（先頭に付ける or Discordの通知抑制フラグ）に合わせた案内
    return "\n".join(
        [
            "## === Metal Rookie Bot V1.1 === ##",
            "**📣 新機能: サイレント返信 (@silent) 対応**",
            "• メッセージを**サイレントで送受信**したいときは、**先頭に `@silent`** を付けて送信してください。",
            "  例: `@silent !next`, `@silent !route プクレットの村`",
            "",
        ]
    )

# ---------------------
# Cog 本体
# ---------------------
class OneShotUpdateCog(commands.Cog):
    """起動時に一度だけアップデート告知を送る Cog"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id: Optional[int] = CHANNEL_ID
        self._ready_once: bool = False

    @commands.Cog.listener()
    async def on_ready(self):
        # 多重 on_ready 対策
        if self._ready_once:
            return
        self._ready_once = True

        if not self.channel_id:
            logger.error("環境変数 CHANNEL_ID が未設定のため、アップデート告知を送信できません。")
            return

        try:
            ch = await ensure_channel(self.bot, self.channel_id)
            await ch.send(build_update_text())
            logger.info("アップデート告知を送信しました。")
        except Exception as e:
            logger.exception(f"アップデート告知の送信に失敗しました: {e}")

# ---------------------
# 拡張エントリ（discord.py v2.x では async 必須）
# ---------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(OneShotUpdateCog(bot))
