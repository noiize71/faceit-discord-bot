import discord
from discord.ext import commands
import requests
import asyncio
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# ================== ENV ==================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

USERS_FILE = "users.json"
WEEKLY_FILE = "weekly_stats.json"
CHECK_INTERVAL = 120

HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"}
BOT_START_TIME = datetime.now(timezone.utc)
DK_TZ = ZoneInfo("Europe/Copenhagen")

# ================== BOT ==================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ================== API ==================
def faceit_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

# ================== FILE HELPERS ==================
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
    d = faceit_get(
        f"https://open.faceit.com/data/v4/players/{pid}/history?game=cs2&limit=1"
    )
    return d["items"][0] if d and d.get("items") else None

def get_match_details(match_id):
    return faceit_get(f"https://open.faceit.com/data/v4/matches/{match_id}")

def get_match_stats(match_id):
    return faceit_get(f"https://open.faceit.com/data/v4/matches/{match_id}/stats")

def get_player_faction(details, nick):
    for faction in ["faction1", "faction2"]:
        for p in details["teams"][faction]["players"]:
            if p["nickname"].lower() == nick.lower():
                return faction
    return None

def did_player_win(details, nick):
    faction = get_player_faction(details, nick)
    if not faction:
        return False

    score = details["results"]["score"]
    my_score = score[faction]
    other = "faction1" if faction == "faction2" else "faction2"
    other_score = score[other]

    return my_score > other_score

def get_map_and_score(details):
    map_name = details.get("voting", {}).get("map", {}).get("pick", ["Unknown"])[0]
    s = details["results"]["score"]
    return map_name, f"{s['faction1']}-{s['faction2']}"

def get_player_stats_from_match(stats_data, nick):
    for team in stats_data.get("rounds", []):
        for p in team.get("players", []):
            if p["nickname"].lower() == nick.lower():
                return p["player_stats"]
    return {}

def update_streak(prev, won):
    if won:
        return 1 if prev <= 0 else prev + 1
    return -1 if prev >= 0 else prev - 1

# ================== WEEKLY ==================
def update_weekly(weekly, nick, won, elo_diff):
    if nick not in weekly:
        weekly[nick] = {"games": 0, "wins": 0, "losses": 0, "elo": 0}
    weekly[nick]["games"] += 1
    weekly[nick]["elo"] += elo_diff
    if won:
        weekly[nick]["wins"] += 1
    else:
        weekly[nick]["losses"] += 1

def is_weekly_recap_time(last_sent):
    now = datetime.now(DK_TZ)
    return (
        now.weekday() == 6 and
        now.hour == 22 and
        (last_sent is None or last_sent.date() != now.date())
    )

async def send_weekly_recap(channel, weekly):
    if not weekly:
        return
    embed = discord.Embed(
        title="📊 Weekly Faceit Recap",
        color=discord.Color.gold()
    )
    for nick, s in weekly.items():
        embed.add_field(
            name=nick,
            value=f"Kampe: {s['games']}\nW/L: {s['wins']} / {s['losses']}\nELO: {s['elo']:+}",
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
    last_weekly_sent = None

    while True:
        users = load_json(USERS_FILE)
        weekly = load_json(WEEKLY_FILE)

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
            prev_elo = user.get("last_elo")
            if prev_elo is None:
                user["last_elo"] = current_elo
                user["last_match"] = match["match_id"]
                user["streak"] = 0
                save_json(USERS_FILE, users)
                continue

            elo_diff = current_elo - prev_elo

            details = get_match_details(match["match_id"])
            stats_data = get_match_stats(match["match_id"])
            if not details or not stats_data:
                continue

            won = did_player_win(details, nick)
            streak = update_streak(user.get("streak", 0), won)
            map_name, score = get_map_and_score(details)

            stats = get_player_stats_from_match(stats_data, nick)
            kills = stats.get("Kills")
            deaths = stats.get("Deaths")

            if kills and deaths:
                k, d = int(kills), int(deaths)
                stats_text = f"🔫 K/D: {k}/{d} ({round(k/max(d,1),2)})"
            else:
                stats_text = "Stats unavailable"

            embed = discord.Embed(
                title=f"🏁 Match finished – {nick}",
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
            embed.add_field(name="Streak", value=streak, inline=False)

            await channel.send(embed=embed)

            user["last_match"] = match["match_id"]
            user["last_elo"] = current_elo
            user["streak"] = streak
            update_weekly(weekly, nick, won, elo_diff)

            save_json(USERS_FILE, users)
            save_json(WEEKLY_FILE, weekly)

        if is_weekly_recap_time(last_weekly_sent):
            await send_weekly_recap(channel, weekly)
            save_json(WEEKLY_FILE, {})
            last_weekly_sent = datetime.now(DK_TZ)

        await asyncio.sleep(CHECK_INTERVAL)

# ================== START ==================
bot.run(DISCORD_TOKEN)
