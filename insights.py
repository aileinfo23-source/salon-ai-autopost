"""投稿の反応（表示・リーチ・保存など）を集めて、次の内容に活かすまとめを作る（毎週、GitHub Actions から起動）。

- インスタ・Threads の「最近の投稿」をアカウントから取ってくる（自動投稿の分も、手で出した分も入る）
- 投稿から24時間以上たったものだけ集計（直後は数字が動き続けるため）
- 表を insights/ に残し、Claude が「お店の設定.md」を踏まえて、効いたこと・次に作るものをまとめる
- 「【今週の反応】」の Issue を立てる → GitHub からメールで届く

環境変数
  IG_TOKEN / THREADS_TOKEN  アクセストークン（未設定のSNSは飛ばす）
  ANTHROPIC_API_KEY         まとめを作るため（未設定なら表だけ）
  INSIGHTS_TEST             "1" なら Issue を立てず、ファイルも残さず、表示するだけ
"""
import json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

import post  # API呼び出し（やり直し・トークンを伏せる処理）を共通で使う

JST = timezone(timedelta(hours=9))
IG_METRICS = ["views", "reach", "saved", "likes", "comments", "shares"]
TH_METRICS = ["views", "likes", "replies", "reposts", "quotes", "shares"]
LABELS = {"views": "表示", "reach": "リーチ", "saved": "保存", "likes": "いいね", "comments": "コメント",
          "shares": "シェア", "replies": "返信", "reposts": "再投稿", "quotes": "引用"}


def metric_values(platform, media_id, metrics):
    """まとめて取れなければ1つずつ取る（投稿の種類によって取れない数字があるため）。"""
    def parse(res):
        out = {}
        for m in res.get("data", []):
            vals = m.get("values") or [{}]
            out[m["name"]] = m.get("total_value", {}).get("value", vals[0].get("value"))
        return out
    try:
        return parse(post.call(platform, "GET", f"{media_id}/insights", metric=",".join(metrics)))
    except RuntimeError:
        got = {}
        for m in metrics:
            try:
                got.update(parse(post.call(platform, "GET", f"{media_id}/insights", metric=m)))
            except RuntimeError:
                pass
        return got


def first_line(text):
    return (text or "").strip().splitlines()[0][:40] if (text or "").strip() else "（本文なし）"


def collect(platform, now, min_age):
    if platform == "instagram":
        items = post.call("instagram", "GET", "me/media", limit="30",
                          fields="id,caption,media_type,timestamp,permalink").get("data", [])
        text_key, metrics = "caption", IG_METRICS
    else:
        items = post.call("threads", "GET", "me/threads", limit="30",
                          fields="id,text,media_type,timestamp,permalink").get("data", [])
        text_key, metrics = "text", TH_METRICS
    rows = []
    for it in items:
        ts = datetime.fromisoformat(it["timestamp"].replace("+0000", "+00:00")).astimezone(JST)
        if now - ts < min_age:
            continue
        vals = metric_values(platform, it["id"], metrics)
        if not vals:
            continue
        row = {"platform": platform, "id": it["id"], "date": ts.strftime("%Y-%m-%d"),
               "days": (now - ts).days, "type": it.get("media_type"), "title": first_line(it.get(text_key)),
               "permalink": it.get("permalink"), **vals}
        if platform == "instagram" and row.get("reach"):
            row["save_rate"] = round(100 * (row.get("saved") or 0) / row["reach"], 1)
        rows.append(row)
    return rows


def table(rows, platform):
    if platform == "instagram":
        cols = ["views", "reach", "saved", "save_rate", "likes", "comments", "shares"]
        head = ["表示", "リーチ", "保存", "保存率", "いいね", "コメント", "シェア"]
    else:
        cols = ["views", "likes", "replies", "reposts", "quotes", "shares"]
        head = [LABELS[c] for c in cols]
    lines = ["| 投稿日 | 経過 | 投稿 | " + " | ".join(head) + " |", "|" + "---|" * (len(cols) + 3)]
    for r in sorted(rows, key=lambda r: r["date"], reverse=True):
        vals = [(f"{r[c]}%" if c == "save_rate" else str(r[c])) if r.get(c) is not None else "-" for c in cols]
        lines.append(f"| {r['date']} | {r['days']}日 | [{r['title']}]({r['permalink']}) | " + " | ".join(vals) + " |")
    return "\n".join(lines)


def summarize(report, settings):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model="claude-opus-5",
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},  # 安全のための判定で断られたら、別のモデルで自動でやり直す
        output_config={"effort": "medium"},
        system=("あなたはSNS運用の相談相手です。次の「お店の設定」を前提に、投稿の反応の表を読んで、"
                "お店の人が次の投稿に活かせる短いまとめを書きます。\n"
                "- 見出しは「効いていること」「伸びていないこと」「次に作るとよさそうなもの（3つ）」「次に1つだけ試すこと」\n"
                "- 数字を根拠に書く。投稿が少ない・日数が浅いうちは言い切らず「まだ判断できない」と書く\n"
                "- インスタは保存と保存率を最も重く見る（お店の目的が保存・フォローのため）\n"
                "- 表にない数字や事実は作らない。専門用語は使わない。全体で400字程度\n\n" + settings),
        messages=[{"role": "user", "content": report}],
    )
    if resp.stop_reason == "refusal":
        return "（AIのまとめは作れませんでした）"
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


def main():
    test = os.environ.get("INSIGHTS_TEST") == "1"
    now = datetime.now(JST)
    rows, notes = [], []
    for platform, env in (("instagram", "IG_TOKEN"), ("threads", "THREADS_TOKEN")):
        if not os.environ.get(env):
            continue
        try:
            rows += collect(platform, now, timedelta(0) if test else timedelta(hours=24))  # テストでは今日の投稿も見る
        except RuntimeError as e:
            if platform == "threads" and ("permission" in str(e).lower() or '"code":10' in str(e)):
                notes.append("Threadsの反応は取れませんでした（アプリに `threads_manage_insights` の権限が必要です）")
            else:
                notes.append(f"{platform}の反応を取れませんでした：{e}")

    ig = [r for r in rows if r["platform"] == "instagram"]
    th = [r for r in rows if r["platform"] == "threads"]
    parts = [f"# 投稿の反応（{now:%Y-%m-%d} 時点・投稿から24時間以上たったもの）", ""]
    if ig:
        parts += ["## インスタ", table(ig, "instagram"), ""]
    if th:
        parts += ["## Threads", table(th, "threads"), ""]
    if not rows:
        parts += ["まだ集計できる投稿がありません。", ""]
    parts += [f"※ {n}" for n in notes]
    report = "\n".join(parts)

    summary = ""
    if rows and os.environ.get("ANTHROPIC_API_KEY"):
        summary = summarize(report, open("お店の設定.md").read())
    full = report + ("\n\n## まとめ（AI）\n" + summary if summary else "")
    print(full)
    if test:
        print("\n【テスト】Issue もファイルも作っていません")
        return

    os.makedirs("insights", exist_ok=True)
    json.dump(rows, open(f"insights/{now:%Y-%m-%d}.json", "w"), ensure_ascii=False, indent=2)
    open("insights/最新の反応.md", "w").write(full + "\n")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and rows:
        subprocess.run(["gh", "issue", "create", "-R", repo, "--title", f"【今週の反応】{now.month}/{now.day}",
                        "--body", full], check=True)


if __name__ == "__main__":
    main()
