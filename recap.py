from storage import get_alerts_for_slug

def grade_alerts(slug: str, winner: str):
    alerts = get_alerts_for_slug(slug)
    if not alerts:
        return None

    wins = 0
    total = 0
    lines = []
    for ts, team, side, entry_price, score, period, reason in alerts:
        total += 1
        hit = (team == winner)
        if hit:
            wins += 1
        lines.append(f"- {team} @ {entry_price:.2f} ({period} {score}) {'✅' if hit else '❌'} | {reason}")

    hit_rate = (wins / total) * 100.0
    return hit_rate, wins, total, lines
