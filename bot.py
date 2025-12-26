import discord
from discord.ext import commands
import requests
import asyncio
import json
import os
import time
from datetime import datetime
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

HEADERS = {
    "Authorization": f"Bearer {FACEIT_API_KEY}"
}

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

def get_last_matches(player_id, limit=5):
    data = faceit_get(
        f"https://open.faceit.com/data/v4/players/{player_id}/history?game=cs2&limit={limit}"
    )
    return data.get("items", []) if data else []

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
    return 1 if won and previous <= 0 else previous + 1 if won else -1 if previous >= 0 else previous - 1

# ================== WEEKLY RECAP ==================

def update_weekly(weekly, nickname, won, elo_diff):
    if nickname not in weekly:
        weekly[nickname] = {"games": 0, "wins": 0, "losses": 0, "elo": 0}

    weekly[nickname]["games"] += 1
    weekly[nickname]["elo"] += elo_diff
    weekly[nickname]["wins" if won else "losses"] += 1

def is_recap_time():
    now = datetime.utcnow()
    return now.weekday() == 6 and now.hour == 20

async def send_weekly_recap(channel, weekly):
    if not weekly:
        return

    embed = discord.Embed(
        title="📊 Weekly Faceit Recap",
        color=discord.Color.gold()
    )

    for nick, stats in weekly.items():
        embed.add_field(
            name=nick,
            value=(
                f"Kampe: {stats['games']}\n"
                f"W / L: {stats['wins']} / {stats['losses']}\n"
                f"ELO: {stats['elo']:+}"
            ),
            inline=False
        )

    await channel.send(embed=embed)

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

                matches = get_last_matches(pid, 1)
                if not matches:
                    continue

                match_id = matches[0]["match_id"]
                if user.get("last_match") == match_id:
                    continue

                details = faceit_get(f"https://open.faceit.com/data/v4/matches/{match_id}")
                if not details:
                    continue

                won = did_player_win(details, nickname)
                map_name, score = get_map_and_score(details)

                stats = get_player_stats(details, nickname)
                kills = stats.get("Kills")
                deaths = stats.get("Deaths")
                stats_text = (
                    f"🔫 K/D: {kills}/{deaths} ({round(int(kills)/max(int(deaths),1),2)})"
                    if kills and deaths else "Stats unavailable"
                )

                current_elo = get_player_elo(pid)
                prev_elo = user.get("last_elo", current_elo)
                elo_diff = current_elo - prev_elo

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
                    value=f"{prev_elo} → {current_elo} ({elo_diff:+})",
                    inline=False
                )
                embed.add_field(
                    name="Streak",
                    value=f"{'🔥' if streak > 0 else '❄️'} {streak}",
                    inline=False
                )

                await channel.send(embed=embed)

                user["last_match"] = match_id
                user["last_elo"] = current_elo
                user["streak"] = streak
                update_weekly(weekly, nickname, won, elo_diff)

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
