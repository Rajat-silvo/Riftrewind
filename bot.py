# bot.py  — Rift Rewind Telegram Bot (single-file, advanced)
# Features:
# - Inline keyboard menu (/start)
# - Roast latest match (Claude via Bedrock, with local fallback)
# - Analyze last N matches (charts + teammate compatibility)
# - Riot region auto-detect + SEA/ASIA/EU/AM fallback
# - Sends profile icon (Data Dragon)
# - Single-file, no .env

import os
import io
import json
import random
import requests
import math
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

# ===================== CONFIG (REPLACE THESE) =====================
RIOT_API_KEY    = "RGAPI-89836dfe-9219-4add-a3bb-df3dc460396b"
TELEGRAM_TOKEN  = "8031999930:AAEicHa9swrd-HOuqAQZd8YqSror8EfbgKQ"

# Optional Bedrock (for 3k-word roast) — leave blank to use local roast fallback
AWS_ACCESS_KEY_ID     = ""   # e.g., "AKIA..."
AWS_SECRET_ACCESS_KEY = ""   # e.g., "abcd..."
AWS_REGION            = "us-east-1"
BEDROCK_MODEL_ID      = "anthropic.claude-3-haiku-20240307-v1:0"

# Data Dragon version (update if icons 404). You can bump to latest patch.
DDRAGON_VERSION = "14.20.1"

# ===================== SAFETY GUARDS =====================
if not RIOT_API_KEY.startswith("RGAPI-"):
    print("❌ Please set a valid RIOT_API_KEY at the top.")
if ":" not in TELEGRAM_TOKEN:
    print("❌ Please set a valid TELEGRAM_TOKEN at the top.")

# ===================== GLOBALS =====================
headers = {"X-Riot-Token": RIOT_API_KEY}

REGION_ROUTING = {
    "americas": "https://americas.api.riotgames.com",
    "europe":   "https://europe.api.riotgames.com",
    "asia":     "https://asia.api.riotgames.com",
    "sea":      "https://sea.api.riotgames.com",
}

# Conversation states
MODE, SUMMONER_NAME, SUMMONER_TAG, ANALYZE_COUNT = range(4)

# ===================== AWS BEDROCK (optional) =====================
def bedrock_generate(prompt: str, max_tokens: int = 3000) -> str:
    """
    Calls AWS Bedrock (Claude 3 Haiku). If not configured or errors occur,
    returns a friendly fallback string.
    """
    if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and AWS_REGION):
        return "⚠️ Bedrock not configured. Add AWS keys at top to enable 3,000-word roast."

    try:
        import boto3
        bedrock = boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        )
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [{"role":"user","content": prompt}],
        })
        resp = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=body,
            contentType="application/json",
        )
        payload = json.loads(resp["body"].read())
        return payload["content"][0]["text"]
    except Exception as e:
        return f"💀 Bedrock error or not approved: {e}\n(Using local roast fallback instead.)"

# ===================== RIOT HELPERS =====================
def detect_region(name, tag):
    for region, base in REGION_ROUTING.items():
        try:
            r = requests.get(f"{base}/riot/account/v1/accounts/by-riot-id/{name}/{tag}", headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                return region, data.get("puuid")
        except:
            pass
    return None, None

def get_match_ids_anywhere(puuid, count=10):
    for reg, base in REGION_ROUTING.items():
        try:
            r = requests.get(f"{base}/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}", headers=headers, timeout=10)
            if r.status_code == 200 and r.json():
                return reg, r.json()
        except:
            pass
    return None, []

def get_match_data(region, match_id):
    try:
        r = requests.get(f"{REGION_ROUTING[region]}/lol/match/v5/matches/{match_id}", headers=headers, timeout=12)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

def get_summoner_by_puuid(platform_region_base, puuid):
    """Summoner-v4 for profile icon + level"""
    try:
        r = requests.get(f"{platform_region_base}/lol/summoner/v4/summoners/by-puuid/{puuid}",
                         headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

def get_platform_base_from_match_region(match_region: str) -> str:
    """Map match regional routing to platform region base for summoner-v4 calls.
       Approx mapping (not perfect, but works for public recap bots):
    """
    mapping = {
        "americas": "https://na1.api.riotgames.com",
        "europe":   "https://euw1.api.riotgames.com",
        "asia":     "https://kr.api.riotgames.com",
        "sea":      "https://sg2.api.riotgames.com",
    }
    return mapping.get(match_region, "https://na1.api.riotgames.com")

def profile_icon_url(icon_id: int) -> str:
    return f"https://ddragon.leagueoflegends.com/cdn/{DDRAGON_VERSION}/img/profileicon/{icon_id}.png"

# ===================== ANALYSIS + CHARTS =====================
def extract_player_from_match(match, name, tag):
    try:
        for p in match["info"]["participants"]:
            if (
                p.get("riotIdGameName","").lower() == name.lower()
                and p.get("riotIdTagline","").lower() == tag.lower()
            ):
                return p
    except:
        pass
    return None

def compute_teammate_compat(matches, name, tag):
    """
    From multiple matches, compute teammate -> (Games, Wins) + Win Rate.
    """
    from collections import defaultdict
    counts = defaultdict(lambda: [0,0])  # games, wins
    for m in matches:
        if not m or "info" not in m: 
            continue
        my = extract_player_from_match(m, name, tag)
        if not my: 
            continue
        my_team = my.get("teamId")
        win = 1 if my.get("win") else 0
        for p in m["info"]["participants"]:
            if p.get("teamId") == my_team and p is not my:
                teammate = f"{p.get('riotIdGameName','?')}#{p.get('riotIdTagline','?')}"
                counts[teammate][0] += 1
                counts[teammate][1] += win
    # produce sorted list
    rows = []
    for tm, vals in counts.items():
        g, w = vals
        wr = (w/g*100.0) if g else 0.0
        rows.append((tm, g, w, round(wr,1)))
    rows.sort(key=lambda x: (-x[3], -x[1], x[0]))
    return rows

def make_dual_chart_image(kda_series, match_ids, team_summary):
    """
    Build a single PNG image with 2 panels:
      Left: KDA vs match index
      Right: Top teammate compatibility bar chart
    Returns bytes (PNG).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Prepare teammate top 8
    top = team_summary[:8]
    names = [r[0] for r in top]
    wrs   = [r[3] for r in top]

    fig, axes = plt.subplots(1, 2, figsize=(13, 5), dpi=140)

    # Left: KDA Trend
    ax = axes[0]
    x = list(range(1, len(kda_series)+1))
    ax.plot(x, kda_series, marker="o", linewidth=2)
    ax.set_title("KDA Trend (Last Matches)")
    ax.set_xlabel("Match index (1=oldest)")
    ax.set_ylabel("KDA")
    ax.grid(True, alpha=0.3)

    # Right: Teammate Compatibility
    ax2 = axes[1]
    ax2.barh(names[::-1], wrs[::-1])
    ax2.set_xlabel("Win Rate %")
    ax2.set_title("Top Teammates (by Win Rate)")
    ax2.set_xlim(0, 100)
    for i, v in enumerate(wrs[::-1]):
        ax2.text(v + 1, i, f"{v:.1f}%", va="center")
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf

def build_long_roast_prompt(player, duration_min, team_rows):
    """
    Creates a detailed prompt for Claude to write a very long roast.
    """
    kp = round(player.get("challenges",{}).get("killParticipation",0)*100,2)
    dpm = 0.0
    try:
        dpm = player.get("totalDamageDealtToChampions",0) / max(1e-9, duration_min)
    except:
        dpm = 0.0

    top_lines = "\n".join([f"- {t[0]}: {t[3]}% over {t[1]} games" for t in team_rows[:5]]) or "- No notable teammates."

    prompt = f"""
You are a savage esports analyst. Write a **3000-word**, statistically-detailed roast about this single player's latest match performance and overall synergy with teammates. The tone is brutal, witty, and sarcastic but focused on gameplay (no IRL insults).

PLAYER MATCH SNAPSHOT
- Champion: {player.get('championName')}
- Role: {player.get('teamPosition','UNKNOWN')}
- K/D/A: {player['kills']}/{player['deaths']}/{player['assists']}
- KDA Ratio: {round((player['kills'] + player['assists'])/max(1, player['deaths']), 2)}
- CS: {player.get('totalMinionsKilled',0)}
- Damage to Champs: {player.get('totalDamageDealtToChampions',0)}
- Damage Taken: {player.get('totalDamageTaken',0)}
- Gold: {player.get('goldEarned',0)}
- Vision Score: {player.get('visionScore',0)} (≈{round(player.get('visionScore',0)/max(1e-9,duration_min),2)}/min)
- Kill Participation: {kp}%
- DPM: {round(dpm,1)}
- Result: {"WIN" if player.get("win") else "LOSS"}

TEAM CHEMISTRY (Recent sample)
{top_lines}

STRUCTURE:
1) OPENING: Savage high-level critique (3–5 paragraphs).
2) OBJECTIVES & MACRO: Dragons, heralds, towers — roast objective control assumptions based on the stats.
3) MECHANICS: Punish K/D/A, deaths, wrong fights; call out missed timings implied by numbers.
4) DAMAGE & IMPACT: Compare damage & DPM to expectations for the role/champion.
5) FARMING & GOLD: Evaluate CS and gold — were they behind tempo?
6) VISION & AWARENESS: Vision score & vision/min; ridicule map awareness failures.
7) TEAMPLAY: Mock synergy and micro combos; tie in win-rate with common teammates from the list above.
8) CLOSING: Final burn; 3 meme captions; 1-sentence "verdict".

STYLE:
- Brutal but gameplay-focused.
- Use exact numbers from above.
- Creative metaphors; esports-caster flair.

Now write the roast.
"""
    return prompt

# ===================== UI HELPERS =====================
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Roast Latest Match", callback_data="menu_roast")],
        [InlineKeyboardButton("📊 Analyze Last N Matches", callback_data="menu_analyze")],
        [InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")]
    ])

def ask_name_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")]])

def ask_tag_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")]])

def ask_count_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("10", callback_data="count_10"),
         InlineKeyboardButton("20", callback_data="count_20"),
         InlineKeyboardButton("30", callback_data="count_30")],
        [InlineKeyboardButton("❌ Cancel", callback_data="menu_cancel")]
    ])

# ===================== HANDLERS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎮 Welcome to **Rift Rewind Bot**!\nChoose an option:",
        reply_markup=main_menu_kb()
    )

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu_roast":
        context.user_data["mode"] = "roast"
        await q.edit_message_text("Enter **Summoner Name**:", reply_markup=ask_name_kb())
        return SUMMONER_NAME

    if data == "menu_analyze":
        context.user_data["mode"] = "analyze"
        await q.edit_message_text("Enter **Summoner Name**:", reply_markup=ask_name_kb())
        return SUMMONER_NAME

    if data == "menu_cancel":
        await q.edit_message_text("✅ Cancelled. Use /start to open menu.")
        return ConversationHandler.END

    if data.startswith("count_"):
        n = int(data.split("_")[1])
        context.user_data["count"] = n
        # proceed as if user pressed enter on count
        return await perform_analysis(q.message, context)

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("Enter **Tag** (after #):", reply_markup=ask_tag_kb())
    return SUMMONER_TAG

async def get_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tag"] = update.message.text.strip()
    mode = context.user_data.get("mode")

    if mode == "roast":
        await update.message.reply_text("⏳ Fetching latest match…")
        return await perform_roast(update.message, context)

    if mode == "analyze":
        await update.message.reply_text("How many recent matches to analyze?", reply_markup=ask_count_kb())
        return ANALYZE_COUNT

async def perform_roast(message, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data["name"]
    tag  = context.user_data["tag"]

    region, puuid = detect_region(name, tag)
    if not puuid:
        await message.reply_text("❌ Player not found. Check name + tag.")
        return ConversationHandler.END

    region, match_ids = get_match_ids_anywhere(puuid, count=1)
    if not match_ids:
        await message.reply_text("❌ No recent matches found.")
        return ConversationHandler.END

    match = get_match_data(region, match_ids[0])
    if not match or "info" not in match:
        await message.reply_text("❌ Could not fetch match details.")
        return ConversationHandler.END

    player = extract_player_from_match(match, name, tag)
    if not player:
        await message.reply_text("❌ Player not present in match.")
        return ConversationHandler.END

    duration_min = match["info"]["gameDuration"] / 60.0

    # pull small teammate context from few matches for spice
    region2, last10 = get_match_ids_anywhere(puuid, count=10)
    matches = [get_match_data(region2, mid) for mid in last10]
    team_rows = compute_teammate_compat(matches, name, tag)

    # Profile icon
    platform_base = get_platform_base_from_match_region(region)
    summ = get_summoner_by_puuid(platform_base, puuid)
    if summ:
        icon_id = summ.get("profileIconId", 0)
        icon_url = profile_icon_url(icon_id)
        try:
            await message.reply_photo(icon_url, caption=f"🧑‍🎮 {name}#{tag} — Profile Icon")
        except:
            pass

    # Bedrock long roast (or fallback)
    prompt = build_long_roast_prompt(player, duration_min, team_rows)
    roast_text = bedrock_generate(prompt)
    if roast_text.startswith("💀 Bedrock error") or roast_text.startswith("⚠️ Bedrock not configured"):
        # short local roast fallback
        k, d, a = player["kills"], player["deaths"], player["assists"]
        kda = round((k+a)/max(1,d),2)
        roast_text = (
            f"💀 Local Roast Fallback:\n"
            f"You went {k}/{d}/{a} on {player.get('championName')} ({player.get('teamPosition','UNKNOWN')}). "
            f"KDA {kda}. This was a TED talk on how to boost the enemy's confidence. "
            f"Damage: {player.get('totalDamageDealtToChampions',0)} — which is basically a love tap. Better luck next queue."
        )

    # Telegram messages have length limits; split if needed
    CHUNK = 3500
    for i in range(0, len(roast_text), CHUNK):
        await message.reply_text(roast_text[i:i+CHUNK])

    await message.reply_text("🔥 Roast complete. Use /start to open menu.")
    return ConversationHandler.END

async def perform_analysis(message, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data["name"]
    tag  = context.user_data["tag"]
    count = context.user_data.get("count", 10)

    await message.reply_text(f"⏳ Analyzing last {count} matches…")

    region, puuid = detect_region(name, tag)
    if not puuid:
        await message.reply_text("❌ Player not found.")
        return ConversationHandler.END

    region, match_ids = get_match_ids_anywhere(puuid, count=count)
    if not match_ids:
        await message.reply_text("❌ No matches found.")
        return ConversationHandler.END

    matches = []
    for mid in match_ids:
        m = get_match_data(region, mid)
        if m: matches.append(m)

    # Profile icon
    platform_base = get_platform_base_from_match_region(region)
    summ = get_summoner_by_puuid(platform_base, puuid)
    if summ:
        try:
            icon_id = summ.get("profileIconId", 0)
            icon_url = profile_icon_url(icon_id)
            await message.reply_photo(icon_url, caption=f"🧑‍🎮 {name}#{tag} — Level {summ.get('summonerLevel','?')}")
        except:
            pass

    # Build per-match KDA list (oldest->newest)
    kdas = []
    mids_sorted = []
    for m in matches[::-1]:  # make oldest first
        p = extract_player_from_match(m, name, tag)
        if not p: 
            continue
        k, d, a = p["kills"], p["deaths"], p["assists"]
        kdas.append(round((k+a)/max(1,d), 2))
        mids_sorted.append(m["metadata"]["matchId"])

    # Teammate summary
    team_rows = compute_teammate_compat(matches, name, tag)

    # Chart: dual panel image
    buf = make_dual_chart_image(kdas, mids_sorted, team_rows)
    await message.reply_photo(buf, caption="📊 Performance & Compatibility Overview")

    # Text summary
    if len(kdas) > 0:
        avg_kda = round(sum(kdas)/len(kdas), 2)
    else:
        avg_kda = 0.0

    # Compute win rate
    wins = 0
    total = 0
    dmg_sum = 0
    for m in matches:
        p = extract_player_from_match(m, name, tag)
        if p:
            total += 1
            wins += 1 if p.get("win") else 0
            dmg_sum += p.get("totalDamageDealtToChampions", 0)
    wr = round((wins/max(1,total))*100,1)
    avg_dmg = round(dmg_sum/max(1,total))

    text = [
        f"🏆 **{name}#{tag} — {total} matches**",
        f"- Average KDA: {avg_kda}",
        f"- Win Rate: {wr}%",
        f"- Avg Damage: {avg_dmg}",
        "",
        "🤝 **Top teammates** (by win rate):"
    ]
    for tm, g, w, wrp in team_rows[:8]:
        text.append(f"• {tm}: {wrp}% (W {w}/{g})")

    await message.reply_text("\n".join(text))
    await message.reply_text("✅ Analysis complete. Use /start to open menu.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Cancelled. Use /start to open menu.")
    return ConversationHandler.END

# ===================== BOOTSTRAP =====================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(menu_callback, pattern="^menu_"),
        ],
        states={
            SUMMONER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            SUMMONER_TAG:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_tag)],
            ANALYZE_COUNT: [CallbackQueryHandler(menu_callback, pattern="^count_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    print("✅ Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
