import os
import json
import random
import requests
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
import boto3

# ============================================================
# ✅ CONFIG
# ============================================================
load_dotenv()
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
MATCH_FOLDER = "matches"
headers = {"X-Riot-Token": RIOT_API_KEY}

# AWS Bedrock client
try:
    bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
except:
    bedrock = None

REGION_ROUTING = {
    "americas": "https://americas.api.riotgames.com",
    "europe": "https://europe.api.riotgames.com",
    "asia": "https://asia.api.riotgames.com",
    "sea": "https://sea.api.riotgames.com"
}

# ============================================================
# ✅ RIOT HELPERS
# ============================================================
def detect_region(gameName, tagLine):
    for region, base in REGION_ROUTING.items():
        try:
            res = requests.get(f"{base}/riot/account/v1/accounts/by-riot-id/{gameName}/{tagLine}", headers=headers)
            if res.status_code == 200:
                return region, res.json()["puuid"]
        except:
            pass
    return None, None

def get_match_ids(region, puuid, count=10):
    for reg, base in REGION_ROUTING.items():
        try:
            res = requests.get(f"{base}/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}", headers=headers)
            if res.status_code == 200 and len(res.json()) > 0:
                return reg, res.json()
        except:
            pass
    return None, []

def get_match_data(region, match_id):
    try:
        res = requests.get(f"{REGION_ROUTING[region]}/lol/match/v5/matches/{match_id}", headers=headers)
        if res.status_code == 200:
            return res.json()
    except:
        pass
    return None

# ============================================================
# ✅ PERFORMANCE / SYNERGY ANALYSIS HELPERS
# ============================================================
def extract_player_stats(match_data, name, tag):
    if not match_data or "info" not in match_data:
        return None
    for p in match_data["info"]["participants"]:
        if (
            p.get("riotIdGameName", "").lower() == name.lower()
            and p.get("riotIdTagline", "").lower() == tag.lower()
        ):
            return {
                "Match ID": match_data["metadata"]["matchId"],
                "Champion": p["championName"],
                "Kills": p["kills"],
                "Deaths": p["deaths"],
                "Assists": p["assists"],
                "KDA": round((p["kills"] + p["assists"]) / max(1, p["deaths"]), 2),
                "Damage": p["totalDamageDealtToChampions"],
                "Win": "Win" if p["win"] else "Loss"
            }
    return None

def extract_teammates(match_data, name, tag):
    participants = match_data["info"]["participants"]
    my = None
    for p in participants:
        if (
            p.get("riotIdGameName", "").lower() == name.lower()
            and p.get("riotIdTagline", "").lower() == tag.lower()
        ):
            my = p
            break
    if not my:
        return []
    teammates=[]
    my_team=my["teamId"]
    win=my["win"]
    for p in participants:
        if p["teamId"]==my_team and p!=my:
            teammates.append({
                "Teammate": f"{p['riotIdGameName']}#{p['riotIdTagline']}",
                "Win": win
            })
    return teammates

def analyze_all_matches(name, tag):
    stats=[]
    pairs=[]
    for f in os.listdir(MATCH_FOLDER):
        if f.endswith(".json"):
            match = json.load(open(f"{MATCH_FOLDER}/{f}", "r"))
            s = extract_player_stats(match, name, tag)
            if s:
                stats.append(s)
            pairs.extend(extract_teammates(match, name, tag))
    if not stats:
        return None, None
    df = pd.DataFrame(stats)
    df_team = pd.DataFrame(pairs)
    summary = df_team.groupby("Teammate").agg(Games=("Win","count"), Wins=("Win","sum")).reset_index()
    summary["Win Rate %"]=round(summary["Wins"]/summary["Games"]*100,1)
    return df, summary

# ============================================================
# ✅ ROAST BUILDING + BEDROCK
# ============================================================
def build_roast_prompt(stats):
    style = random.choice([
        "a furious Challenger analyst losing faith in humanity",
        "a washed-up ex-pro who regrets solo queue",
        "a Diamond OTP filled with unfiltered rage",
    ])
    personality = random.choice([
        "unhinged and sarcastic", 
        "cold and venomously analytical",
    ])
    return f"""Roast a League of Legends player …
… shortened for brevity …
Champion: {stats['champion']}
Role: {stats['role']}
KDA: {stats['kills']}/{stats['deaths']}/{stats['assists']}
Damage: {stats['damage']}
Gold: {stats['gold']}
"""

def call_bedrock(prompt):
    if not bedrock:
        return "⚠️ Bedrock not configured."
    try:
        response = bedrock.invoke_model(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            body=json.dumps({"anthropic_version":"bedrock-2023-05-31","max_tokens":900,"messages":[{"role":"user","content":prompt}]}),
            contentType="application/json",
        )
        return json.loads(response["body"].read())["content"][0]["text"]
    except Exception as e:
        return f"💀 Bedrock error: {e}"

# ============================================================
# ✅ STREAMLIT UI – BIG REWORK
# ============================================================
def run_dashboard():

    st.set_page_config(page_title="Rift Rewind AI", layout="wide")

    # 🔥 Neon-sidebar styling
    st.sidebar.markdown("## ⚡ Rift Rewind Menu")
    st.sidebar.markdown("---")

    mode = st.sidebar.radio(
        "Choose Mode:",
        ["📊 Analyze Player Stats", "🔥 Roast Player Match"],
        index=0
    )

    st.sidebar.markdown("### 🎮 Player Info")
    name = st.sidebar.text_input("Summoner Name:", "WPE Devoured")
    tag = st.sidebar.text_input("Tag:", "Carry")

    st.sidebar.markdown("---")
    st.sidebar.caption("Powered by Riot API + AWS Bedrock")

    # ============================================================
    # ✅ MODE 1 — PERFORMANCE ANALYSIS
    # ============================================================
    if mode == "📊 Analyze Player Stats":
        st.title("📊 Performance & Compatibility Dashboard")

        if st.button("Analyze Player"):
            st.info("Fetching matches...")

            region, puuid = detect_region(name, tag)
            if not puuid:
                st.error("❌ Player not found.")
                return

            region, match_ids = get_match_ids(region, puuid, count=10)
            if not match_ids:
                st.error("❌ No matches found.")
                return

            os.makedirs(MATCH_FOLDER, exist_ok=True)
            for mid in match_ids:
                md = get_match_data(region, mid)
                if md:
                    with open(f"{MATCH_FOLDER}/{mid}.json","w") as f:
                        json.dump(md, f)

            df, summary = analyze_all_matches(name, tag)
            if df is None:
                st.error("❌ Invalid match data.")
                return

            avg_kda=df["KDA"].mean()
            win_rate=(df["Win"]=="Win").mean()*100
            avg_dmg=df["Damage"].mean()

            st.markdown(f"### 🧠 Summary for {name}#{tag}")
            st.success(f"**KDA:** {avg_kda:.2f} | **Win Rate:** {win_rate:.1f}% | **Avg Damage:** {avg_dmg:.0f}")

            # ⚡ 2 Chart Layout Side-by-Side
            colA, colB = st.columns(2)

            with colA:
                fig1 = go.Figure()
                fig1.add_trace(go.Scatter(
                    x=df["Match ID"], y=df["KDA"], mode="lines+markers",
                    name="KDA Trend", line=dict(color="#00FFFF", width=3)
                ))
                fig1.update_layout(
                    title="📈 KDA Progression",
                    template="plotly_dark",
                    height=400
                )
                st.plotly_chart(fig1,use_container_width=True)

            with colB:
                fig2 = go.Figure()
                fig2.add_trace(go.Bar(
                    x=summary["Teammate"],
                    y=summary["Win Rate %"],
                    marker_color="#FF4C4C"
                ))
                fig2.update_layout(
                    title="🤝 Win Rate With Teammates",
                    template="plotly_dark",
                    height=400
                )
                st.plotly_chart(fig2,use_container_width=True)

            st.subheader("📘 Raw Teammate Stats")
            st.dataframe(summary)

    # ============================================================
    # ✅ MODE 2 — ROAST MATCH
    # ============================================================
    if mode == "🔥 Roast Player Match":
        st.title("🔥 AI Deep Roast — Powered by Claude 3")

        if st.button("Roast My Latest Match"):
            st.info("Fetching match...")

            region, puuid = detect_region(name, tag)
            if not puuid:
                st.error("❌ Player not found.")
                return

            region, match_ids = get_match_ids(region, puuid, count=1)
            if not match_ids:
                st.error("❌ No matches found.")
                return

            match = get_match_data(region, match_ids[0])
            if not match or "info" not in match:
                st.error("❌ Invalid match data returned.")
                return

            # extract stats
            target = None
            for p in match["info"]["participants"]:
                if (
                    p.get("riotIdGameName","").lower()==name.lower()
                    and p.get("riotIdTagline","").lower()==tag.lower()
                ):
                    target=p
                    break
            
            if not target:
                st.error("❌ Player not in match participants.")
                return

            duration = match["info"]["gameDuration"]/60

            stats={
                "champion":target["championName"],
                "role":target.get("teamPosition","UNKNOWN"),
                "kills":target["kills"],
                "deaths":target["deaths"],
                "assists":target["assists"],
                "kda":round((target["kills"]+target["assists"])/max(1,target["deaths"]),2),
                "cs":target.get("totalMinionsKilled",0),
                "damage":target["totalDamageDealtToChampions"],
                "damage_taken":target["totalDamageTaken"],
                "gold":target["goldEarned"],
                "vision":target["visionScore"],
                "vision_per_min":round(target["visionScore"]/duration,2),
                "kill_participation":round(target.get("challenges",{}).get("killParticipation",0)*100,2),
                "damage_per_min":round(target["totalDamageDealtToChampions"]/duration,1),
                "win":target["win"]
            }

            # Generate roast
            prompt = build_roast_prompt(stats)
            roast = call_bedrock(prompt)

            st.markdown("## 💀 AI Roast Generated")
            st.write(roast)


# ============================================================
if __name__ == "__main__":
    run_dashboard()
