import os
import discord
from discord.ext import commands
import asyncio
from datetime import datetime, timedelta, timezone
from aiohttp import web  # Northflank用：Webサーバーを追加して稼働維持

# ===== 環境変数 =====
TOKEN = os.getenv("DISCORD_TOKEN")  # Northflank環境変数から取得
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # チャンネルIDも環境変数化

if not TOKEN or CHANNEL_ID == 0:
    raise ValueError("❌ 環境変数 DISCORD_TOKEN または CHANNEL_ID が設定されていません。")

# ===== 時間設定 =====
JST = timezone(timedelta(hours=9))
START_ANCHOR = datetime(2025, 10, 16, 12, 0, 0, tzinfo=JST)
INTERVAL = timedelta(hours=2, minutes=30)

# ===== Discord Bot 初期化 =====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== 状態変数 =====
last_notified_time = None


def next_window_start_from_anchor(after: datetime) -> datetime:
    """指定時刻以降の次のメタルーキー発生時刻を返す（JST基準）"""
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc).astimezone(JST)
    else:
        after = after.astimezone(JST)

    anchor = START_ANCHOR.astimezone(JST)
    if after < anchor:
        return anchor

    elapsed_seconds = (after - anchor).total_seconds()
    interval_seconds = INTERVAL.total_seconds()
    cycles = int(elapsed_seconds // interval_seconds)
    last = anchor + INTERVAL * cycles

    if last <= after:
        return last + INTERVAL
    return last


@bot.event
async def on_ready():
    print(f"✅ ログインしました: {bot.user}")
    bot.loop.create_task(schedule_metal_notify())


async def schedule_metal_notify():
    """メタルーキー時間を定期通知"""
    global last_notified_time
    while True:
        now = datetime.now(tz=JST)
        next_start = next_window_start_from_anchor(now)
        wait = max((next_start - now).total_seconds(), 0)

        print("----------")
        print(f"🕒 現在時刻 (JST): {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"⏰ 次の通知時刻 (JST): {next_start.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"💤 待機時間: {wait:.1f} 秒")
        print("----------")

        # 長時間スリープ中でもNorthflankがCPUを止めないように5分ごとに軽い動作
        while wait > 300:
            await asyncio.sleep(300)
            wait -= 300
            print(f"[KeepAlive] 稼働中 ({datetime.now(tz=JST).strftime('%H:%M:%S')})")

        await asyncio.sleep(wait)

        # 同時刻の重複通知を防止
        if last_notified_time == next_start:
            print("⚠️ 重複通知スキップ")
            await asyncio.sleep(5)
            continue

        # 通知送信
        try:
            channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
            await channel.send("🪙 メタルーキーの時間です！")
            last_notified_time = next_start
            print(f"📢 通知送信完了: {next_start.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            print(f"送信エラー: {e}")
            await asyncio.sleep(60)

        await asyncio.sleep(60)


# ===== Northflank 用: 軽いWebサーバーを起動（常時稼働維持） =====
async def handle_health(request):
    return web.Response(text="✅ Bot is running!")

async def start_web_server():
    """Northflankのヘルスチェック/常時稼働用HTTPサーバー"""
    app = web.Application()
    app.router.add_get("/", handle_health)
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Health check server started on port {port}")


async def main():
    # 並行実行: Discord Bot + Webサーバー
    await asyncio.gather(
        start_web_server(),
        bot.start(TOKEN)
    )


if __name__ == "__main__":
    asyncio.run(main())