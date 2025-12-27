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

# ================== LOAD ENV ==================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

USERS_FILE = "users.json"
WEEKLY_FILE = "weekly_stats.json"
CHECK_INTERVAL = 120
STATS_RETRY_DELAY = 120  # 2 minutter

HEADERS = {
    "Authorization": f"Bearer {FACEIT_API_KEY}"
}

BOT_START_TIME = datetime.now(timezone.utc)

# ================== BOT ==================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ================== SAFE FACEIT API ==================

def faceit_get(url, retries=3, delay=2):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()
        except RequestException as e:
            print(f"[Faceit API] Error ({attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(delay)
    return None

# ================== FILE HELPERS ==================

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Failed saving {path}:", e)

# ================== FACEIT HELPERS ==================

def get_player_id(nickname):
    data = faceit_get(f"https://open.faceit.com/data/v4/players?nickname={nickname}")
    return data["player_id"] if data else None

def get_player_elo(player_id):
    data = faceit_get(f"https://open.faceit.com/data/v4/players/{player_id}")
    if not data:
        return None
    return data["games"]["cs2"]["faceit_elo"]

def get_last_match(player_id):
    data = faceit_get(
        f"https://open.faceit.com/data/v4/players/{player_id}/history?game=cs2&limit=1"
    )
    if not data or not data.get("items"):
        return None
    return data["items"][0]

def get_team_players(team):
    return team.get("players") or team.get("roster") or []

def did_player_win(details, nickname):
    winner = details.get("results", {}).get("winner")
    for team_key in ["faction1", "faction2"]:
        team = details.get("teams", {}).get(team_key, {})
        for p in get_team_players(team):
            if p.get("nickname", "").lower() == nickname.lower():
                return team_key == winner
    return False

def get_player_stats(details, nickname):
    for p in details.get("players", []):
        if p.get("nickname", "").lower() == nickname.lower():
            return p.get("player_stats", {})
    for rnd in details.get("rounds", []):
        for team in rnd.get("teams", []):
            for p in team.get("players", []):
                if p.get("nickname", "").lower() == nickname.lower():
                    return p.get("player_stats", {})
    return {}

def get_map_and_score(details):
    map_name = "Unknown"
    score = "N/A"
    voting = details.get("voting", {}).get("map", {})
    if voting.get("pick"):
        map_name = voting["pick"][0]
    score_data = details.get("results", {}).get("score", {})
    if "faction1" in score_data and "faction2" in score_data:
        score = f"{score_data['faction1']}-{score_data['faction2']}"
    return map_name, score

def update_streak(previous, won):
    if won:
        return 1 if previous <= 0 else previous + 1
    else:
        return -1 if previous >= 0 else previous - 1

# ================== WEEKLY ==================

def update_weekly(weekly, nickname, won, elo_diff):
    if nickname not in weekly:
        weekly[nickname] = {"games": 0, "wins": 0, "losses": 0, "elo": 0}
    weekly[nickname]["games"] += 1
    weekly[nickname]["elo"] += elo_diff
    weekly[nickname]["wins" if won else "losses"] += 1

def is_recap_time():
    now = datetime.now(timezone.utc)
    return now.weekday() == 6 and now.hour == 20

async def send_weekly_recap(channel, weekly):
    if not weekly:
        return
    embed = discord.Embed(title="📊 Weekly Faceit Recap", color=discord.Color.gold())
    for nick, s in weekly.items():
        embed.add_field(
            name=nick,
            value=f"Kampe: {s['games']}\nW/L: {s['wins']} / {s['losses']}\nELO: {s['elo']:+}",
            inline=False
        )
    await channel.send(embed=embed)

# ================== STATS RETRY ==================

async def retry_stats(message, match_id, nickname):
    await asyncio.sleep(STATS_RETRY_DELAY)

    details = faceit_get(f"https://open.faceit.com/data/v4/matches/{match_id}")
    if not details:
        return

    stats = get_player_stats(details, nickname)
    kills = stats.get("Kills")
    deaths = stats.get("Deaths")

    if kills is None or deaths is None:
        return  # stadig ikke klar

    kills = int(kills)
    deaths = int(deaths)
    kd = round(kills / max(deaths, 1), 2)

    embed = message.embeds[0]
    embed.set_field_at(
        index=3,
        name="Stats",
        value=f"🔫 K/D: {kills}/{deaths} ({kd})",
        inline=False
    )
    await message.edit(embed=embed)

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
        try:
            users = load_json(USERS_FILE)
            weekly = load_json(WEEKLY_FILE)

            for user in users.values():
                nickname = user["nickname"]
                pid = get_player_id(nickname)
                if not pid:
                    continue

                match = get_last_match(pid)
                if not match:
                    continue

                finished_at = datetime.fromtimestamp(match["finished_at"], timezone.utc)

                if finished_at < BOT_START_TIME:
                    user["last_match"] = match["match_id"]
                    user["last_elo"] = get_player_elo(pid)
                    save_json(USERS_FILE, users)
                    continue

                if user.get("last_match") == match["match_id"]:
                    continue

                current_elo = get_player_elo(pid)
                if current_elo is None:
                    continue

                if "last_elo" not in user:
                    user["last_elo"] = current_elo
                    user["last_match"] = match["match_id"]
                    save_json(USERS_FILE, users)
                    continue

                elo_before = user["last_elo"]
                elo_change = current_elo - elo_before

                details = faceit_get(
                    f"https://open.faceit.com/data/v4/matches/{match['match_id']}"
                )
                if not details:
                    continue

                won = did_player_win(details, nickname)
                map_name, score = get_map_and_score(details)

                stats = get_player_stats(details, nickname)
                kills = stats.get("Kills")
                deaths = stats.get("Deaths")

                if kills is None or deaths is None:
                    stats_text = "Stats pending…"
                    needs_retry = True
                else:
                    kills = int(kills)
                    deaths = int(deaths)
                    kd = round(kills / max(deaths, 1), 2)
                    stats_text = f"🔫 K/D: {kills}/{deaths} ({kd})"
                    needs_retry = False

                streak = update_streak(user.get("streak", 0), won)

                embed = discord.Embed(
                    title=f"🏁 Match finished – {nickname}",
                    color=discord.Color.green() if won else discord.Color.red()
                )
                embed.add_field(name="Result", value="Win ✅" if won else "Loss ❌", inline=True)
                embed.add_field(name="Score", value=score, inline=True)
                embed.add_field(name="Map", value=map_name, inline=True)
                embed.add_field(name="Stats", value=stats_text, inline=False)
                embed.add_field(
                    name="ELO",
                    value=f"{elo_before} → {current_elo} ({elo_change:+})",
                    inline=False
                )
                embed.add_field(
                    name="Streak",
                    value=f"{'🔥' if streak > 0 else '❄️'} {streak}",
                    inline=False
                )

                msg = await channel.send(embed=embed)

                if needs_retry:
                    asyncio.create_task(
                        retry_stats(msg, match["match_id"], nickname)
                    )

                user["last_match"] = match["match_id"]
                user["last_elo"] = current_elo
                user["streak"] = streak
                update_weekly(weekly, nickname, won, elo_change)

                save_json(USERS_FILE, users)
                save_json(WEEKLY_FILE, weekly)

            if is_recap_time():
                await send_weekly_recap(channel, weekly)
                save_json(WEEKLY_FILE, {})

        except Exception as e:
            print("[MATCH LOOP ERROR – recovered]:", e)

        await asyncio.sleep(CHECK_INTERVAL)

# ================== START ==================

bot.run(DISCORD_TOKEN)
