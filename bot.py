import discord
from discord.ext import commands
import requests
import asyncio
import json
import os
import time
from dotenv import load_dotenv
from requests.exceptions import RequestException

# ================== LOAD ENV ==================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

USERS_FILE = "users.json"
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
            print(f"[Faceit API] Error (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(delay)
    return None

# ================== DATA ==================

def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(data):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print("Failed saving users.json:", e)

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
    if not details:
        return False

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
    return {}

def get_map_and_score(details):
    map_name = "Unknown"
    score = "N/A"

    if not details:
        return map_name, score

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

# ================== EVENTS ==================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    asyncio.create_task(match_loop())

# ================== COMMANDS ==================

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="📊 Faceit Bot", color=discord.Color.blurple())
    embed.add_field(name="!elo <nick>", value="Vis nuværende ELO", inline=False)
    embed.add_field(name="!last5 <nick>", value="Sidste 5 kampe", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def elo(ctx, nickname: str):
    pid = get_player_id(nickname)
    if not pid:
        return await ctx.send("Kunne ikke finde spilleren.")

    elo = get_player_elo(pid)
    if elo is None:
        return await ctx.send("Faceit API fejl – prøv igen senere.")

    embed = discord.Embed(
        title=f"🎯 {nickname}",
        description=f"**ELO: {elo}**",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command(name="last5")
async def last5(ctx, nickname: str):
    pid = get_player_id(nickname)
    if not pid:
        return await ctx.send("Kunne ikke finde spilleren.")

    matches = get_last_matches(pid, 5)
    embed = discord.Embed(title=f"📊 Last 5 – {nickname}", color=discord.Color.orange())

    for m in matches:
        details = faceit_get(f"https://open.faceit.com/data/v4/matches/{m['match_id']}")
        won = did_player_win(details, nickname)
        map_name, score = get_map_and_score(details)

        stats = get_player_stats(details, nickname)
        kills = int(stats.get("Kills", 0))
        deaths = int(stats.get("Deaths", 0))
        kd = round(kills / max(deaths, 1), 2)

        embed.add_field(
            name=f"{'W' if won else 'L'} | {score} | {map_name}",
            value=f"K/D {kills}/{deaths} ({kd})",
            inline=False
        )

    await ctx.send(embed=embed)

# ================== MATCH LOOP ==================

async def match_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)

    while True:
        try:
            users = load_users()

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
                kills = int(stats.get("Kills", 0))
                deaths = int(stats.get("Deaths", 0))
                kd = round(kills / max(deaths, 1), 2)

                current_elo = get_player_elo(pid)
                if current_elo is None:
                    continue

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
                embed.add_field(
                    name="Stats",
                    value=f"🔫 K/D: {kills}/{deaths} ({kd})",
                    inline=False
                )
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

                save_users(users)

        except Exception as e:
            print("[MATCH LOOP ERROR – recovered]:", e)

        await asyncio.sleep(CHECK_INTERVAL)

# ================== START ==================

bot.run(DISCORD_TOKEN)
