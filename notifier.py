import os, requests

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

def send_discord(msg: str):
    if not DISCORD_WEBHOOK:
        print("DISCORD_WEBHOOK not set")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)
    except Exception as e:
        print("Discord send failed:", e)
