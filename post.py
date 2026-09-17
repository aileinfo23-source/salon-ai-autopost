"""予約投稿（カルーセル）を、Instagram と Threads の公式APIで出す。

毎日 21:00（日本時間）に GitHub Actions から起動する。
schedule.json の中で「今日の日付」かつ posted.json に無いものだけを投稿する（SNSごとに記録）。
日付がずれて取りこぼしたものは、二重投稿を避けるため自動では出さず、警告だけ出す。
片方のSNSで失敗しても、もう片方は出す。最後に失敗があれば Actions を失敗にしてメールで知らせる。

環境変数
  IG_TOKEN        Instagram のアクセストークン（GitHubの秘密の設定。コードやファイルに書かない）
  IG_USER_ID      Instagram のユーザーID
  THREADS_TOKEN   Threads のアクセストークン（未設定なら Threads は飛ばす）
  BASE_URL        画像を置いているGitHub PagesのURL（末尾の / なし）
  DRY_RUN         "1" なら投稿の直前（コンテナ作成と処理完了の確認）までで止める
  TARGET_DATE     テスト用。YYYY-MM-DD を入れると、その日の投稿として扱う
"""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
PLATFORMS = {
    "instagram": {"graph": "https://graph.instagram.com/" + os.environ.get("GRAPH_VERSION", "v23.0"),
                  "token": "IG_TOKEN"},
    "threads":   {"graph": "https://graph.threads.net/v1.0", "token": "THREADS_TOKEN"},
}


def call(platform, method, path, **params):
    conf = PLATFORMS[platform]
    params["access_token"] = os.environ[conf["token"]]
    data = urllib.parse.urlencode(params).encode()
    url = f"{conf['graph']}/{path}"
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


def wait_finished(platform, container_id, label):
    field = "status_code" if platform == "instagram" else "status"
    time.sleep(3)  # 作った直後は処理中なので、少し待ってから確認する（問い合わせ回数を減らす）
    for _ in range(30):
        st = call(platform, "GET", container_id, fields=field).get(field)
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{label} の処理に失敗しました（status={st}）")
        time.sleep(5)
    raise RuntimeError(f"{label} の処理が2分半たっても終わりませんでした")


def post_instagram(s, base, dry):
    me = call("instagram", "GET", "me", fields="user_id,username")
    user = os.environ.get("IG_USER_ID") or me.get("user_id")
    if me.get("user_id") and os.environ.get("IG_USER_ID") and me["user_id"] != os.environ["IG_USER_ID"]:
        raise RuntimeError(f"トークンのアカウント（@{me.get('username')}）と IG_USER_ID が一致しません")
    print(f"  [Instagram] 接続OK @{me.get('username')}")
    children = []
    for img in s["images"]:
        c = call("instagram", "POST", f"{user}/media", image_url=f"{base}/{img}", is_carousel_item="true")
        wait_finished("instagram", c["id"], img)
        children.append(c["id"])
        print(f"  [Instagram] 画像OK {img}")
    carousel = call("instagram", "POST", f"{user}/media", media_type="CAROUSEL",
                    children=",".join(children), caption=s["caption"])
    wait_finished("instagram", carousel["id"], "カルーセル")
    print("  [Instagram] カルーセルOK（投稿の直前まで確認できました）")
    if dry:
        return None
    return call("instagram", "POST", f"{user}/media_publish", creation_id=carousel["id"])["id"]


def post_threads(s, base, dry):
    me = call("threads", "GET", "me", fields="id,username")
    user = me["id"]
    print(f"  [Threads] 接続OK @{me.get('username')}")
    children = []
    for img in s["images"]:
        c = call("threads", "POST", f"{user}/threads", media_type="IMAGE",
                 image_url=f"{base}/{img}", is_carousel_item="true")
        children.append(c["id"])
        print(f"  [Threads] 画像OK {img}")
    params = dict(media_type="CAROUSEL", children=",".join(children), text=s["threads_text"])
    if s.get("threads_topic"):
        params["topic_tag"] = s["threads_topic"]
    carousel = call("threads", "POST", f"{user}/threads", **params)
    wait_finished("threads", carousel["id"], "カルーセル")
    print("  [Threads] カルーセルOK（投稿の直前まで確認できました）")
    if dry:
        return None
    time.sleep(10)  # Threadsは作成直後に公開すると失敗することがあるので少し待つ
    return call("threads", "POST", f"{user}/threads_publish", creation_id=carousel["id"])["id"]


def main():
    base = os.environ["BASE_URL"].rstrip("/")
    dry = os.environ.get("DRY_RUN") == "1"
    today = os.environ.get("TARGET_DATE") or datetime.now(JST).strftime("%Y-%m-%d")
    print(f"対象日：{today}　{'【テスト】投稿はしません' if dry else ''}")

    active = ["instagram"] + (["threads"] if os.environ.get("THREADS_TOKEN") else [])
    schedule = json.load(open("schedule.json"))
    posted = json.load(open("posted.json"))
    done = {(p["id"], p.get("platform", "instagram")) for p in posted}
    failures = []

    for plat in active:
        # 毎回トークンが生きているか確かめる（投稿の無い日も。切れていたらメールで気づける）
        try:
            call(plat, "GET", "me", fields="username")
        except RuntimeError as e:
            failures.append(f"{plat}：接続できません {e}")

    missed = [(s["id"], p) for s in schedule for p in active
              if s["date"] < today and (s["id"], p) not in done]
    if missed and not os.environ.get("TARGET_DATE"):
        print(f"⚠ 過去の日付で未投稿のものがあります（自動では出しません）：{missed}")

    todays = [s for s in schedule if s["date"] == today]
    if not todays:
        print("今日の投稿はありません")
    for s in todays:
        print(f"▶ {s['id']}｜{s['title']}")
        for plat, fn in (("instagram", post_instagram), ("threads", post_threads)):
            if plat not in active or (s["id"], plat) in done:
                continue
            if plat == "threads" and not s.get("threads_text"):
                continue
            try:
                media_id = fn(s, base, dry)
            except RuntimeError as e:
                failures.append(f"{s['id']} {plat}：{e}")
                print(f"  ❌ {plat} 失敗：{e}")
                continue
            if media_id:
                posted.append({"id": s["id"], "platform": plat, "media_id": media_id,
                               "posted_at": datetime.now(JST).isoformat(timespec="seconds")})
                json.dump(posted, open("posted.json", "w"), ensure_ascii=False, indent=2)
                print(f"  ✅ {plat} に投稿しました media_id={media_id}")

    if failures:
        print("\n失敗があります：\n" + "\n".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
