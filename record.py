"""この回で増えた投稿の記録を、GitHub上の最新の posted.json に足す（post.yml の「投稿の記録を残す」から呼ぶ）。

  python3 record.py <この回の posted.json の写し>

以前は git pull --rebase だけだったので、同じ日に別の回が先に記録を足していると衝突して、
この回の記録が消えていた（2026-11-04 hw02）。記録が消えると、次の回がもう一度出してしまう。
同じ (id, platform, media_id) は1つにまとめる。
"""
import json, sys

mine = json.load(open(sys.argv[1]))
latest = json.load(open("posted.json"))
have = {(p["id"], p.get("platform", "instagram"), p.get("media_id")) for p in latest}
added = [p for p in mine if (p["id"], p.get("platform", "instagram"), p.get("media_id")) not in have]
if added:
    json.dump(latest + added, open("posted.json", "w"), ensure_ascii=False, indent=2)
print(f"記録を {len(added)} 件足しました")
