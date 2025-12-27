import discord
import os
import json
import asyncio
import requests
from discord import Embed
from dotenv import load_dotenv

# ---------------- CONFIG ----------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

USERS_FILE = "users.json"
HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# ---------------- UTIL ----------------

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=4)

def get_player(nickname):
    r = requests.get(
        f"https://open.faceit.com/data/v4/players?nickname={nickname}",
        headers=HEADERS
    )
    return r.json()

def get_last_matches(player_id, limit=5):
    r = requests.get(
        f"https://open.faceit.com/data/v4/players/{player_id}/history?game=cs2&limit={limit}",
        headers=HEADERS
    )
    return r.json().get("items", [])

def get_match_stats(match_id):
    r = requests.get(
        f"https://open.faceit.com/data/v4/matches/{match_id}/stats",
        headers=HEADERS
    )
    return r.json()

def update_streak(user, won):
    streak = user.get("streak", 0)
    if won:
        streak = streak + 1 if streak >= 0 else 1
    else:
        streak = streak - 1 if streak <= 0 else -1
    user["streak"] = streak
    return streak

def extract_player_and_score(match_stats, nickname):
    rounds = match_stats.get("rounds", [])
    if not rounds:
        return None

    my_score = None
    enemy_score = None
    kills = deaths = 0

    for team in rounds[0]["teams"]:
        score = int(team["team_stats"]["Final Score"])
        for p in team["players"]:
            if p["nickname"].lower() == nickname.lower():
                my_score = score
                kills = int(p["player_stats"]["Kills"])
                deaths = int(p["player_stats"]["Deaths"])
            else:
                enemy_score = score

    if my_score is None or enemy_score is None:
        return None

    kd = round(kills / max(1, deaths), 2)
    return my_score, enemy_score, kills, deaths, kd

# ---------------- COMMANDS ----------------

@client.event
async def on_message(message):
    if message.author.bot:
        return

    # HELP
    if message.content.startswith("!help"):
        embed = Embed(title="🤖 Faceit Bot Commands", color=0x95a5a6)
        embed.add_field(name="!faceit <navn>", value="Registrer Faceit-bruger", inline=False)
        embed.add_field(name="!elo <navn>", value="Vis nuværende ELO", inline=False)
        embed.add_field(name="!last5 <navn>", value="Vis sidste 5 kampe", inline=False)
        await message.channel.send(embed=embed)

    # REGISTER USER
    if message.content.startswith("!faceit"):
        parts = message.content.split()
        if len(parts) != 2:
            await message.channel.send("Brug: `!faceit FaceitNavn`")
            return

        nickname = parts[1]
        users = load_users()

        users[nickname.lower()] = {
            "nickname": nickname,
            "last_match": None,
            "last_elo": None,
            "streak": 0
        }

        save_users(users)
        await message.channel.send(f"✅ **{nickname}** er nu registreret!")

    # ELO
    if message.content.startswith("!elo"):
        parts = message.content.split()
        if len(parts) != 2:
            return

        nickname = parts[1]
        player = get_player(nickname)
        elo = player["games"]["cs2"]["faceit_elo"]

        embed = Embed(title="📊 Faceit ELO", description=f"**{nickname}**", color=0x3498db)
        embed.add_field(name="Total ELO", value=str(elo))
        await message.channel.send(embed=embed)

    # LAST 5 MATCHES (CORRECT W/L)
    if message.content.startswith("!last5"):
        parts = message.content.split()
        if len(parts) != 2:
            await message.channel.send("Brug: `!last5 FaceitNavn`")
            return

        nickname = parts[1]
        player = get_player(nickname)
        matches = get_last_matches(player["player_id"], 5)

        embed = Embed(
            title="🕘 Sidste 5 Faceit-kampe",
            description=f"**{nickname}**",
            color=0x9b59b6
        )

        for m in matches:
            stats = get_match_stats(m["match_id"])
            data = extract_player_and_score(stats, nickname)

            if not data:
                continue

            my_score, enemy_score, _, _, _ = data
            result = "✅ Win" if my_score > enemy_score else "❌ Loss"

            embed.add_field(
                name=result,
                value=f"Score: **{my_score}–{enemy_score}**",
                inline=False
            )

        await message.channel.send(embed=embed)

# ---------------- AUTO MATCH TRACKER ----------------

async def match_loop():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    while True:
        users = load_users()

        for key, user in users.items():
            nickname = user["nickname"]
            player = get_player(nickname)
            matches = get_last_matches(player["player_id"], 1)

            if not matches:
                continue

            match = matches[0]
            current_match_id = match["match_id"]

            # FIRST RUN: store but don't post
            if user["last_match"] is None:
                user["last_match"] = current_match_id
                user["last_elo"] = player["games"]["cs2"]["faceit_elo"]
                save_users(users)
                continue

            if current_match_id == user["last_match"]:
                continue

            # ELO CALC
            current_elo = player["games"]["cs2"]["faceit_elo"]
            previous_elo = user["last_elo"]
            elo_change = current_elo - previous_elo
            won = elo_change > 0

            streak = update_streak(user, won)
            user["last_match"] = current_match_id
            user["last_elo"] = current_elo
            save_users(users)

            stats = get_match_stats(current_match_id)
            data = extract_player_and_score(stats, nickname)
            if not data:
                continue

            my_score, enemy_score, kills, deaths, kd = data

            embed = Embed(
                title="🎮 Faceit Kamp Afsluttet",
                description=f"**{nickname}** {'✅ Vundet' if won else '❌ Tabt'}",
                color=0x2ecc71 if won else 0xe74c3c
            )

            embed.add_field(name="Resultat", value=f"{my_score}–{enemy_score}", inline=False)
            embed.add_field(name="Total ELO", value=str(current_elo))
            embed.add_field(name="ELO ændring", value=f"{elo_change:+}")
            embed.add_field(name="K/D", value=str(kd))
            embed.add_field(name="Kills / Deaths", value=f"{kills} / {deaths}")
            embed.add_field(
                name="Streak",
                value=f"🔥 Win streak: {streak}" if streak > 0 else f"❄ Loss streak: {abs(streak)}",
                inline=False
            )

            await channel.send(embed=embed)

        await asyncio.sleep(120)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    client.loop.create_task(match_loop())

client.run(TOKEN)
