import discord
from discord.ext import commands
import requests
import asyncio
import json
import os
from dotenv import load_dotenv

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

# ================== HELPERS ==================

def faceit_get(url):
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(data):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def get_player_id(nickname):
    data = faceit_get(f"https://open.faceit.com/data/v4/players?nickname={nickname}")
    return data["player_id"]

def get_player_elo(player_id):
    data = faceit_get(f"https://open.faceit.com/data/v4/players/{player_id}")
    return data["games"]["cs2"]["faceit_elo"]

def get_last_matches(player_id, limit=5):
    data = faceit_get(
        f"https://open.faceit.com/data/v4/players/{player_id}/history?game=cs2&limit={limit}"
    )
    return data.get("items", [])

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
    return {}

def get_map_and_score(details):
    map_name = "Unknown"
    voting = details.get("voting", {}).get("map", {})
    if voting.get("pick"):
        map_name = voting["pick"][0]

    score_data = details.get("results", {}).get("score", {})
    if "faction1" in score_data and "faction2" in score_data:
        score = f"{score_data['faction1']}-{score_data['faction2']}"
    else:
        score = "N/A"

    return map_name, score

def update_streak(previous_streak, won):
    if won:
        return 1 if previous_streak <= 0 else previous_streak + 1
    else:
        return -1 if previous_streak >= 0 else previous_streak - 1

# ================== EVENTS ==================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    bot.loop.create_task(match_loop())

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
    elo = get_player_elo(pid)
    embed = discord.Embed(
        title=f"🎯 {nickname}",
        description=f"**ELO: {elo}**",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command(name="last5")
async def last5(ctx, nickname: str):
    pid = get_player_id(nickname)
    matches = get_last_matches(pid, 5)

    embed = discord.Embed(
        title=f"📊 Last 5 – {nickname}",
        color=discord.Color.orange()
    )

    for m in matches:
        details = faceit_get(
            f"https://open.faceit.com/data/v4/matches/{m['match_id']}"
        )

        won = did_player_win(details, nickname)
        result = "W" if won else "L"
        map_name, score = get_map_and_score(details)

        stats = get_player_stats(details, nickname)
        kills = stats.get("Kills", "0")
        deaths = stats.get("Deaths", "0")
        kd = round(int(kills) / max(int(deaths), 1), 2)

        embed.add_field(
            name=f"{result} | {score} | {map_name}",
            value=f"K/D {kills}/{deaths} ({kd})",
            inline=False
        )

    await ctx.send(embed=embed)

# ================== MATCH LOOP ==================

async def match_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)

    while True:
        users = load_users()

        for user in users.values():
            nickname = user["nickname"]
            pid = get_player_id(nickname)

            matches = get_last_matches(pid, 1)
            if not matches:
                continue

            match_id = matches[0]["match_id"]
            if user.get("last_match") == match_id:
                continue

            details = faceit_get(
                f"https://open.faceit.com/data/v4/matches/{match_id}"
            )

            won = did_player_win(details, nickname)
            map_name, score = get_map_and_score(details)

            current_elo = get_player_elo(pid)
            prev_elo = user.get("last_elo", current_elo)
            elo_diff = current_elo - prev_elo

            previous_streak = user.get("streak", 0)
            streak = update_streak(previous_streak, won)

            embed = discord.Embed(
                title=f"🏁 Match finished – {nickname}",
                color=discord.Color.green() if won else discord.Color.red()
            )
            embed.add_field(name="Result", value="Win ✅" if won else "Loss ❌", inline=True)
            embed.add_field(name="Score", value=score, inline=True)
            embed.add_field(name="Map", value=map_name, inline=True)
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

        await asyncio.sleep(CHECK_INTERVAL)

# ================== START ==================

bot.run(DISCORD_TOKEN)