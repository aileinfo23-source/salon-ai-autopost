"""予約の残りが少なくなったら知らせる（毎回の投稿のあとに GitHub Actions から起動）。

- アカウントごとに、予約表（schedule.json）のいちばん先の投稿日（ストーリーは数えない）を見る
- 残りが WARN_DAYS 日分以下になったら「【残りわずか】…」の Issue を立てる → GitHub からメールで届く
- 同じ Issue が開いている間は立て直さない（1回だけ届く）。予約を足して残りが増えたら、Issue は自動で閉じる

環境変数
  GITHUB_REPOSITORY / GH_TOKEN  Issue を立てるため（無ければ表示するだけ）
"""
import json, os, subprocess
from datetime import datetime, timedelta, timezone

import post

JST = timezone(timedelta(hours=9))
WARN_DAYS = {"ballet": 3}   # 知らせるアカウントと、何日分を切ったら知らせるか


def remaining(schedule, acct, today):
    """今日より後に予約されている投稿（ストーリー以外）の日数と、最後の日"""
    days = sorted({s["date"] for s in schedule
                   if s.get("account", "shitsuji") == acct and not s.get("story") and s["date"] > today})
    return len(days), (days[-1] if days else None)


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main():
    schedule = json.load(open("schedule.json", encoding="utf-8"))
    today = datetime.now(JST).strftime("%Y-%m-%d")
    repo = os.environ.get("GITHUB_REPOSITORY")
    for acct, warn in WARN_DAYS.items():
        label = post.ACCOUNTS[acct]["label"]
        n, last = remaining(schedule, acct, today)
        title = f"【残りわずか】{label} の投稿"
        print(f"{label}：残り {n} 日分（最後は {last or 'なし'}）")
        if not repo:
            continue
        opened = json.loads(gh("issue", "list", "-R", repo, "--state", "open", "--search", f'"{title}" in:title',
                               "--json", "number,title"))
        opened = [i for i in opened if i["title"] == title]
        if n <= warn and not opened:
            body = (f"{label} の予約が、残り **{n} 日分** です"
                    + (f"（最後の投稿は {last}）。" if last else "（明日から出るものがありません）。") + "\n\n"
                    "次の分を作るときは、執事さんに「バレエのリールの続きを作って」と声をかけてください。\n"
                    "予約が足されると、このお知らせは自動で閉じます。")
            gh("issue", "create", "-R", repo, "--title", title, "--body", body)
            print("  → お知らせを出しました")
        elif n > warn:
            for i in opened:
                gh("issue", "close", str(i["number"]), "-R", repo, "--comment", f"予約が足されました（残り {n} 日分・最後は {last}）。")
                print("  → お知らせを閉じました")


if __name__ == "__main__":
    main()
