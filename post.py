"""予約投稿（写真1枚・カルーセル・リール／動画）を、Instagram と Threads の公式APIで出す。

本番の起動は外の時報サービス（cron-job.org）から21:00にかける。GitHub側の定時起動は当てにならないので予備（20:13〜翌2:13の1時間おき）。
21:00より前に起動したら21:00まで待って出す。21:00より後に起動したら、まだ出ていなければすぐ出す。
0時〜3時の起動は「前日」として扱う（遅れて日付をまたいでも、前日の分を出せる）。
schedule.json の中で「その日の日付」かつ posted.json に無いものだけを投稿する（SNSごとに記録）。一度出たら、あとの起動は何もしない。
それより前の日付で取りこぼしたものは、二重投稿を避けるため自動では出さず、警告だけ出す。
公開のAPIがエラーを返しても実際には出ていることがあるので、最近の投稿を見て確かめてから記録する。
片方のSNSで失敗しても、もう片方は出す。最後に失敗があれば Actions を失敗にしてメールで知らせる。

**複数アカウント対応**（2026-09-25〜）。schedule.json の各予約に "account" を書く（省略時は shitsuji）。
アカウントごとにトークンの秘密の設定が別。トークンが無いアカウントは、その日の予約があっても飛ばす。

環境変数（アカウントごと）
  shitsuji … IG_TOKEN / IG_USER_ID / THREADS_TOKEN
  ballet   … IG_TOKEN_BALLET / IG_USER_ID_BALLET / THREADS_TOKEN_BALLET
  BASE_URL        画像を置いているGitHub PagesのURL（末尾の / なし）
  DRY_RUN         "1" なら投稿の直前（コンテナ作成と処理完了の確認）までで止める
  TARGET_DATE     テスト用。YYYY-MM-DD を入れると、その日の投稿として扱う（待たずにすぐ動く）
  WAIT_UNTIL      定時起動のときだけ "21:00" が入る。その時刻より前なら、その時刻まで待つ
"""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
GRAPH = {
    "instagram": "https://graph.instagram.com/" + os.environ.get("GRAPH_VERSION", "v23.0"),
    "threads":   "https://graph.threads.net/v1.0",
}
ACCOUNTS = {
    "shitsuji": {"label": "AIの執事（@ai.shitsuji）",
                 "instagram": {"token": "IG_TOKEN", "user": "IG_USER_ID"},
                 "threads":   {"token": "THREADS_TOKEN"}},
    "ballet":   {"label": "バレエ教室（@kasuthijomion_ballet）",
                 "instagram": {"token": "IG_TOKEN_BALLET", "user": "IG_USER_ID_BALLET"},
                 "threads":   {"token": "THREADS_TOKEN_BALLET"}},
}


def has_token(acct, platform):
    return bool(os.environ.get(ACCOUNTS[acct][platform]["token"]))


def call(acct, platform, method, path, **params):
    params["access_token"] = os.environ[ACCOUNTS[acct][platform]["token"]]
    data = urllib.parse.urlencode(params).encode()
    url = f"{GRAPH[platform]}/{path}"
    for attempt in range(4):
        req = (urllib.request.Request(f"{url}?{data.decode()}") if method == "GET"
               else urllib.request.Request(url, data=data, method="POST"))
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            # 一時的な制限（回数制限など）は、待ってからやり直す
            transient = '"is_transient":true' in body or '"code":4,' in body or e.code >= 500
            if transient and attempt < 3:
                wait = 60 * (attempt + 1)
                print(f"  一時的なエラーのため {wait} 秒待ってやり直します（{e.code}）")
                time.sleep(wait)
                continue
            # トークンがログに出ないよう、エラー本文だけを出す
            raise RuntimeError(f"APIエラー {e.code} {method} {path}: {body}")


def wait_finished(acct, platform, container_id, label, video=False):
    field = "status_code" if platform == "instagram" else "status"
    time.sleep(3)  # 作った直後は処理中なので、少し待ってから確認する（問い合わせ回数を減らす）
    tries, interval = (60, 10) if video else (30, 5)  # 動画は処理に時間がかかる（最大10分待つ）
    for _ in range(tries):
        st = call(acct, platform, "GET", container_id, fields=field).get(field)
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{label} の処理に失敗しました（status={st}）")
        time.sleep(interval)
    raise RuntimeError(f"{label} の処理が{tries * interval // 60}分たっても終わりませんでした")


def parse_time(t):
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%S%z")


def find_published(acct, platform, user, text):
    """最近の投稿の中に、同じ文章で30分以内に出たものがあれば、そのIDを返す。"""
    if platform == "instagram":
        items = call(acct, "instagram", "GET", f"{user}/media", fields="id,caption,timestamp", limit=5).get("data", [])
        key = "caption"
    else:
        items = call(acct, "threads", "GET", f"{user}/threads", fields="id,text,timestamp", limit=5).get("data", [])
        key = "text"
    head = text.strip()[:40]
    now = datetime.now(timezone.utc)
    for it in items:
        if (it.get(key) or "").strip()[:40] == head and it.get("timestamp") \
                and now - parse_time(it["timestamp"]) < timedelta(minutes=30):
            return it["id"]
    return None


def publish(acct, platform, user, creation_id, text):
    """公開する。エラーが返っても実際には出ていることがあるので、確かめてから1回だけやり直す。"""
    path = f"{user}/media_publish" if platform == "instagram" else f"{user}/threads_publish"
    for attempt in range(2):
        try:
            return call(acct, platform, "POST", path, creation_id=creation_id)["id"]
        except RuntimeError as e:
            print(f"  [{platform}] 公開でエラーが返りました。本当に出ていないか確かめます：{e}")
            time.sleep(30)
            found = find_published(acct, platform, user, text)
            if found:
                print(f"  [{platform}] 実際には公開されていました（media_id={found}）")
                return found
            if attempt == 1:
                raise
            print(f"  [{platform}] 出ていなかったので、もう一度公開します")


def post_instagram(acct, s, base, dry):
    me = call(acct, "instagram", "GET", "me", fields="user_id,username")
    want = os.environ.get(ACCOUNTS[acct]["instagram"]["user"])
    user = want or me.get("user_id")
    if me.get("user_id") and want and me["user_id"] != want:
        raise RuntimeError(f"トークンのアカウント（@{me.get('username')}）とユーザーIDが一致しません")
    print(f"  [Instagram] 接続OK @{me.get('username')}")
    if s.get("video"):  # リール
        c = call(acct, "instagram", "POST", f"{user}/media", media_type="REELS",
                 video_url=f"{base}/{s['video']}", caption=s["caption"], share_to_feed="true")
        wait_finished(acct, "instagram", c["id"], "リール", video=True)
        label = "リール"
    elif len(s["images"]) == 1:  # 写真1枚
        c = call(acct, "instagram", "POST", f"{user}/media", image_url=f"{base}/{s['images'][0]}", caption=s["caption"])
        wait_finished(acct, "instagram", c["id"], s["images"][0])
        label = "写真"
    else:  # カルーセル
        children = []
        for img in s["images"]:
            ch = call(acct, "instagram", "POST", f"{user}/media", image_url=f"{base}/{img}", is_carousel_item="true")
            wait_finished(acct, "instagram", ch["id"], img)
            children.append(ch["id"])
            print(f"  [Instagram] 画像OK {img}")
        c = call(acct, "instagram", "POST", f"{user}/media", media_type="CAROUSEL",
                 children=",".join(children), caption=s["caption"])
        wait_finished(acct, "instagram", c["id"], "カルーセル")
        label = "カルーセル"
    print(f"  [Instagram] {label}OK（投稿の直前まで確認できました）")
    if dry:
        return None
    return publish(acct, "instagram", user, c["id"], s["caption"])


def post_threads(acct, s, base, dry):
    me = call(acct, "threads", "GET", "me", fields="id,username")
    user = me["id"]
    print(f"  [Threads] 接続OK @{me.get('username')}")
    extra = {"topic_tag": s["threads_topic"]} if s.get("threads_topic") else {}
    if s.get("video"):
        c = call(acct, "threads", "POST", f"{user}/threads", media_type="VIDEO",
                 video_url=f"{base}/{s['video']}", text=s["threads_text"], **extra)
        wait_finished(acct, "threads", c["id"], "動画", video=True)
        label = "動画"
    elif len(s["images"]) == 1:
        c = call(acct, "threads", "POST", f"{user}/threads", media_type="IMAGE",
                 image_url=f"{base}/{s['images'][0]}", text=s["threads_text"], **extra)
        wait_finished(acct, "threads", c["id"], s["images"][0])
        label = "写真"
    else:
        children = []
        for img in s["images"]:
            ch = call(acct, "threads", "POST", f"{user}/threads", media_type="IMAGE",
                      image_url=f"{base}/{img}", is_carousel_item="true")
            # 画像の準備が終わる前にカルーセルを組むと「Invalid Carousel Children」で失敗する
            wait_finished(acct, "threads", ch["id"], img)
            children.append(ch["id"])
            print(f"  [Threads] 画像OK {img}")
        c = call(acct, "threads", "POST", f"{user}/threads", media_type="CAROUSEL",
                 children=",".join(children), text=s["threads_text"], **extra)
        wait_finished(acct, "threads", c["id"], "カルーセル")
        label = "カルーセル"
    print(f"  [Threads] {label}OK（投稿の直前まで確認できました）")
    if dry:
        return None
    time.sleep(10)  # Threadsは作成直後に公開すると失敗することがあるので少し待つ
    return publish(acct, "threads", user, c["id"], s["threads_text"])


def main():
    base = os.environ["BASE_URL"].rstrip("/")
    dry = os.environ.get("DRY_RUN") == "1"
    # 0時〜3時の起動は前日として扱う（定時起動が遅れて日付をまたいでも、前日の分を出せる）
    today = os.environ.get("TARGET_DATE") or (datetime.now(JST) - timedelta(hours=3)).strftime("%Y-%m-%d")
    print(f"対象日：{today}　{'【テスト】投稿はしません' if dry else ''}")

    schedule = json.load(open("schedule.json"))
    posted = json.load(open("posted.json"))
    done = {(p["id"], p.get("platform", "instagram")) for p in posted}
    failures = []

    def acct_of(s):
        return s.get("account", "shitsuji")

    def platforms(acct):
        return [p for p in ("instagram", "threads") if has_token(acct, p)]

    # 今日の予約に出てくるアカウント（無ければトークンのある全アカウント）の接続を確かめる
    todays = [s for s in schedule if s["date"] == today]
    accounts = sorted({acct_of(s) for s in todays}) or [a for a in ACCOUNTS if has_token(a, "instagram")]
    for acct in accounts:
        if acct not in ACCOUNTS:
            failures.append(f"{acct}：知らないアカウント名です（post.py の ACCOUNTS に足してください）")
            continue
        if not platforms(acct):
            print(f"⚠ {ACCOUNTS[acct]['label']}：トークンが設定されていないので飛ばします")
            continue
        for plat in platforms(acct):
            # 毎回トークンが生きているか確かめる（投稿の無い日も。切れていたらメールで気づける）
            try:
                me = call(acct, plat, "GET", "me", fields="username")
                print(f"  ✅ {ACCOUNTS[acct]['label']} {plat} 接続OK @{me.get('username')}")
            except RuntimeError as e:
                failures.append(f"{ACCOUNTS[acct]['label']} {plat}：接続できません {e}")

    missed = [(s["id"], p) for s in schedule if s["date"] < today and acct_of(s) in ACCOUNTS
              for p in platforms(acct_of(s)) if (s["id"], p) not in done]
    if missed and not os.environ.get("TARGET_DATE"):
        print(f"⚠ 過去の日付で未投稿のものがあります（自動では出しません）：{missed}")

    if not todays:
        print("今日の投稿はありません")
    pending = [s for s in todays for p in platforms(acct_of(s)) if (s["id"], p) not in done]
    if not pending and todays:
        print("今日の分はもう出ています")
    wait_until = os.environ.get("WAIT_UNTIL")
    if pending and wait_until and not os.environ.get("TARGET_DATE"):
        h, m = map(int, wait_until.split(":"))
        at = datetime.strptime(today, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=JST)
        sec = (at - datetime.now(JST)).total_seconds()
        if sec > 0:
            print(f"{wait_until} まで {int(sec // 60)} 分待ちます")
            time.sleep(sec)

    for s in todays:
        acct = acct_of(s)
        if acct not in ACCOUNTS or not platforms(acct):
            continue
        print(f"▶ {s['id']}｜{s['title']}　［{ACCOUNTS[acct]['label']}］")
        for plat, fn in (("instagram", post_instagram), ("threads", post_threads)):
            if plat not in platforms(acct) or (s["id"], plat) in done:
                continue
            if plat == "threads" and not s.get("threads_text"):
                continue
            try:
                media_id = fn(acct, s, base, dry)
            except RuntimeError as e:
                failures.append(f"{s['id']} {plat}：{e}")
                print(f"  ❌ {plat} 失敗：{e}")
                continue
            if media_id:
                posted.append({"id": s["id"], "account": acct, "platform": plat, "media_id": media_id,
                               "posted_at": datetime.now(JST).isoformat(timespec="seconds")})
                json.dump(posted, open("posted.json", "w"), ensure_ascii=False, indent=2)
                print(f"  ✅ {plat} に投稿しました media_id={media_id}")

    if failures:
        print("\n失敗があります：\n" + "\n".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
