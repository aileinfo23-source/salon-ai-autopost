"""インスタの予約投稿（カルーセル）を、Instagram公式API（Instagramログイン）で出す。

毎日 21:00（日本時間）に GitHub Actions から起動する。
schedule.json の中で「今日の日付」かつ posted.json に無いものだけを投稿する。
日付がずれて取りこぼしたものは、二重投稿を避けるため自動では出さず、警告だけ出す。

環境変数
  IG_TOKEN     アクセストークン（GitHubの秘密の設定に置く。コードやファイルに書かない）
  IG_USER_ID   InstagramのユーザーID
  BASE_URL     画像を置いているGitHub PagesのURL（末尾の / なし）
  DRY_RUN      "1" なら投稿の直前（コンテナ作成と処理完了の確認）までで止める
  TARGET_DATE  テスト用。YYYY-MM-DD を入れると、その日の投稿として扱う
"""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

GRAPH = "https://graph.instagram.com/" + os.environ.get("GRAPH_VERSION", "v23.0")
JST = timezone(timedelta(hours=9))


def call(method, path, **params):
    params["access_token"] = os.environ["IG_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{GRAPH}/{path}?{data.decode()}")
    else:
        req = urllib.request.Request(f"{GRAPH}/{path}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        # トークンがログに出ないよう、エラー本文だけを出す
        raise SystemExit(f"APIエラー {e.code} {method} {path}: {body}")


def wait_finished(container_id, label):
    for _ in range(30):
        st = call("GET", container_id, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise SystemExit(f"{label} の処理に失敗しました（status={st}）")
        time.sleep(5)
    raise SystemExit(f"{label} の処理が2分半たっても終わりませんでした")


def main():
    user = os.environ["IG_USER_ID"]
    base = os.environ["BASE_URL"].rstrip("/")
    dry = os.environ.get("DRY_RUN") == "1"
    today = os.environ.get("TARGET_DATE") or datetime.now(JST).strftime("%Y-%m-%d")

    # 毎回まずトークンが生きているか確かめる（切れていたらここで失敗→メールで気づける）
    me = call("GET", "me", fields="username")
    print(f"接続OK：@{me.get('username')}　対象日：{today}　{'【テスト】投稿はしません' if dry else ''}")

    schedule = json.load(open("schedule.json"))
    posted = json.load(open("posted.json"))
    done = {p["id"] for p in posted}

    missed = [s["id"] for s in schedule if s["date"] < today and s["id"] not in done]
    if missed and not os.environ.get("TARGET_DATE"):
        print(f"⚠ 過去の日付で未投稿のものがあります（自動では出しません）：{missed}")

    todays = [s for s in schedule if s["date"] == today and s["id"] not in done]
    if not todays:
        print("今日の投稿はありません")
        return

    for s in todays:
        print(f"▶ {s['id']}｜{s['title']}")
        children = []
        for img in s["images"]:
            c = call("POST", f"{user}/media", image_url=f"{base}/{img}", is_carousel_item="true")
            wait_finished(c["id"], img)
            children.append(c["id"])
            print(f"  画像OK {img}")
        carousel = call("POST", f"{user}/media", media_type="CAROUSEL",
                        children=",".join(children), caption=s["caption"])
        wait_finished(carousel["id"], "カルーセル")
        print("  カルーセルOK（投稿の直前まで確認できました）")
        if dry:
            continue
        pub = call("POST", f"{user}/media_publish", creation_id=carousel["id"])
        posted.append({"id": s["id"], "media_id": pub["id"],
                       "posted_at": datetime.now(JST).isoformat(timespec="seconds")})
        json.dump(posted, open("posted.json", "w"), ensure_ascii=False, indent=2)
        print(f"  ✅ 投稿しました media_id={pub['id']}")


if __name__ == "__main__":
    main()
