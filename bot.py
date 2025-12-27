import discord
from discord.ext import commands
import requests
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from requests.exceptions import RequestException

# ================== ENV ==================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

USERS_FILE = "users.json"
CHECK_INTERVAL = 120
STATS_RETRY_DELAY = 120  # sekunder

HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"}
BOT_START_TIME = datetime.now(timezone.utc)

# ================== BOT ==================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ================== API ==================
def faceit_get(url, retries=3, delay=2):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()
        except RequestException:
            if i < retries - 1:
                time.sleep(delay)
    return None

# ================== FILES ==================
def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

# ================== FACEIT HELPERS ==================
def get_player_id(nick):
    d = faceit_get(f"https://open.faceit.com/data/v4/players?nickname={nick}")
    return d["player_id"] if d else None

def get_player_elo(pid):
    d = faceit_get(f"https://open.faceit.com/data/v4/players/{pid}")
    return d["games"]["cs2"]["faceit_elo"] if d else None

def get_last_match(pid):
    d = faceit_get(f"https://open.faceit.com/data/v4/players/{pid}/history?game=cs2&limit=1")
    return d["items"][0] if d and d.get("items") else None

def did_player_win(details, nick):
    winner = details["results"]["winner"]
    for team in details["teams"].values():
        for p in team.get("players", []):
            if p["nickname"].lower() == nick.lower():
                return team["team_id"] == winner
    return False

def get_player_stats(details, nick):
    for rnd in details.get("rounds", []):
        for team in rnd.get("teams", []):
            for p in team.get("players", []):
                if p["nickname"].lower() == nick.lower():
                    return p.get("player_stats", {})
    return {}

def get_map_and_score(details):
    map_name = details.get("voting", {}).get("map", {}).get("pick", ["Unknown"])[0]
    s = details.get("results", {}).get("score", {})
    score = f"{s.get('faction1','?')}-{s.get('faction2','?')}"
    return map_name, score

def update_streak(prev, won):
    if won:
        return 1 if prev <= 0 else prev + 1
    return -1 if prev >= 0 else prev - 1

# ================== LOCKED STATS RETRY ==================
async def retry_stats_edit_only(message_id, channel_id, match_id, nick):
    await asyncio.sleep(STATS_RETRY_DELAY)

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        message = await channel.fetch_message(message_id)
    except Exception:
        return  # message findes ikke → gør intet

    details = faceit_get(f"https://open.faceit.com/data/v4/matches/{match_id}")
    if not details:
        return

    stats = get_player_stats(details, nick)
    if not stats.get("Kills"):
        return  # stadig ikke klar → stop, ingen ekstra handling

    k, d = int(stats["Kills"]), int(stats["Deaths"])
    kd = round(k / max(d, 1), 2)

    old = message.embeds[0]
    new = discord.Embed(title=old.title, color=old.color)

    for f in old.fields:
        if f.name == "Stats":
            new.add_field(
                name="Stats",
                value=f"🔫 K/D: {k}/{d} ({kd})",
                inline=False
            )
        else:
            new.add_field(name=f.name, value=f.value, inline=f.inline)

    await message.edit(embed=new)

# ================== EVENTS ==================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    asyncio.create_task(match_loop())

# ================== MATCH LOOP ==================
async def match_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)

    while True:
        users = load_json(USERS_FILE)

        for user in users.values():
            nick = user["nickname"]
            pid = get_player_id(nick)
            if not pid:
                continue

            match = get_last_match(pid)
            if not match:
                continue

            finished = datetime.fromtimestamp(match["finished_at"], timezone.utc)

            if finished < BOT_START_TIME:
                user["last_match"] = match["match_id"]
                user["last_elo"] = get_player_elo(pid)
                user["streak"] = 0
                save_json(USERS_FILE, users)
                continue

            if user.get("last_match") == match["match_id"]:
                continue

            current_elo = get_player_elo(pid)
            if "last_elo" not in user:
                user["last_elo"] = current_elo
                user["last_match"] = match["match_id"]
                user["streak"] = 0
                save_json(USERS_FILE, users)
                continue

            elo_before = user["last_elo"]
            elo_diff = current_elo - elo_before

            details = faceit_get(f"https://open.faceit.com/data/v4/matches/{match['match_id']}")
            won = did_player_win(details, nick)
            streak = update_streak(user.get("streak", 0), won)
            map_name, score = get_map_and_score(details)

            stats = get_player_stats(details, nick)
            if stats.get("Kills"):
                k, d = int(stats["Kills"]), int(stats["Deaths"])
                stats_text = f"🔫 K/D: {k}/{d} ({round(k/max(d,1),2)})"
                retry = False
            else:
                stats_text = "Stats pending…"
                retry = True

            embed = discord.Embed(
                title=f"🏁 Match finished – {nick}",
                color=discord.Color.green() if won else discord.Color.red()
            )
            embed.add_field(name="Result", value="Win ✅" if won else "Loss ❌", inline=True)
            embed.add_field(name="Score", value=score, inline=True)
            embed.add_field(name="Map", value=map_name, inline=True)
            embed.add_field(name="Stats", value=stats_text, inline=False)
            embed.add_field(name="ELO", value=f"{elo_before} → {current_elo} ({elo_diff:+})", inline=False)
            embed.add_field(name="Streak", value=streak, inline=False)

            msg = await channel.send(embed=embed)

            if retry:
                asyncio.create_task(
                    retry_stats_edit_only(
                        msg.id,
                        CHANNEL_ID,
                        match["match_id"],
                        nick
                    )
                )

            user["last_match"] = match["match_id"]
            user["last_elo"] = current_elo
            user["streak"] = streak
            save_json(USERS_FILE, users)

        await asyncio.sleep(CHECK_INTERVAL)

# ================== START ==================
bot.run(DISCORD_TOKEN)
