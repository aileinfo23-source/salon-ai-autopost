"""予約投稿（写真1枚・カルーセル・リール／動画）を、Instagram と Threads の公式APIで出す。

本番の起動は外の時報サービス（cron-job.org）から21:00にかける。GitHub側の定時起動は当てにならないので予備（20:13〜翌2:13の1時間おき）。
投稿の時刻は**アカウントごと**（執事＝21:00、バレエ＝12:00）。その時刻より前の起動では出さず、次の起動に回す。
時刻を過ぎていて、まだ出ていなければ出す。
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
  neru     … IG_TOKEN_NERU / IG_USER_ID_NERU / THREADS_TOKEN_NERU
             ＋ X：X_API_KEY_NERU / X_API_SECRET_NERU / X_ACCESS_TOKEN_NERU / X_ACCESS_SECRET_NERU（OAuth 1.0a・期限なし）

**X（2026-09-26〜・ネルだけ）**。予約に "x_posts"（ツリー。1つ目がポスト、2つ目からは返信）を書くと出す。
  "x_posts": [{"text": "…", "images": ["neru01/1.jpg", …]}, …]   画像は1つに4枚まで。画像は docs/ から直接アップする
  Xは**有料**（投稿1回 $0.015、本文にURLがあると $0.20）。なので本文にURLがあったら出さずに失敗にする。
  毎回の接続確認はしない（お金がかかるため）。テスト（DRY_RUN）のときだけ、鍵が効くか自分のアカウントを読んで確かめる。
  二重投稿を防ぐため、Xは公開のやり直しをしない。ツリーの途中で失敗したら、出せた1つ目のIDで記録して失敗を知らせる。
  BASE_URL        画像を置いているGitHub PagesのURL（末尾の / なし）
  DRY_RUN         "1" なら投稿の直前（コンテナ作成と処理完了の確認）までで止める
  TARGET_DATE     テスト用。YYYY-MM-DD を入れると、その日の投稿として扱う（待たずにすぐ動く）
  ※ 時刻の判定は**どの起動でも働く**（2026-09-26〜）。TARGET_DATE を入れたときだけ、時刻を見ずにすぐ出す
     （以前は手で起動すると時刻を無視したため、試運転で朝に投稿が出てしまった）
"""
import base64, hashlib, hmac, json, os, re, secrets, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
GRAPH = {
    "instagram": "https://graph.instagram.com/" + os.environ.get("GRAPH_VERSION", "v23.0"),
    "threads":   "https://graph.threads.net/v1.0",
}
ACCOUNTS = {
    "shitsuji": {"label": "AIの執事（@ai.shitsuji）", "time": "21:00",
                 "instagram": {"token": "IG_TOKEN", "user": "IG_USER_ID"},
                 "threads":   {"token": "THREADS_TOKEN"}},
    "ballet":   {"label": "バレエ教室（@kasuthijomion_ballet）", "time": "12:00",
                 "instagram": {"token": "IG_TOKEN_BALLET", "user": "IG_USER_ID_BALLET"},
                 "threads":   {"token": "THREADS_TOKEN_BALLET"}},
    "neru":     {"label": "ネル（@neru_ninja）", "time": "22:00",
                 "instagram": {"token": "IG_TOKEN_NERU", "user": "IG_USER_ID_NERU"},
                 "threads":   {"token": "THREADS_TOKEN_NERU"},
                 "x":         {"keys": ("X_API_KEY_NERU", "X_API_SECRET_NERU",
                                        "X_ACCESS_TOKEN_NERU", "X_ACCESS_SECRET_NERU")}},
}
PLATFORMS = ("instagram", "threads", "x")
X_API = "https://api.x.com/2"


def has_token(acct, platform):
    conf = ACCOUNTS[acct].get(platform)
    if not conf:
        return False
    if platform == "x":
        return all(os.environ.get(k) for k in conf["keys"])
    return bool(os.environ.get(conf["token"]))


# ---- X（OAuth 1.0a で署名して呼ぶ） ----

def _pct(v):
    return urllib.parse.quote(str(v), safe="~-._")


def oauth1_header(method, url, query, keys, nonce=None, stamp=None):
    """OAuth 1.0a の Authorization ヘッダーを作る。署名に入れるのは URL のクエリとOAuthの値だけ（JSON・multipartの本文は入れない）。"""
    ck, cs, tk, ts = keys
    oauth = {"oauth_consumer_key": ck, "oauth_nonce": nonce or secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(stamp or int(time.time())),
             "oauth_token": tk, "oauth_version": "1.0"}
    pairs = sorted((_pct(k), _pct(v)) for k, v in list(query.items()) + list(oauth.items()))
    param_str = "&".join(f"{k}={v}" for k, v in pairs)
    base_str = "&".join([method.upper(), _pct(url), _pct(param_str)])
    key = f"{_pct(cs)}&{_pct(ts)}".encode()
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key, base_str.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def x_call(acct, method, path, json_body=None, multipart=None, query=None):
    """Xを呼ぶ。回数制限（429）だけ待ってやり直す。ポストの作成は二重にならないよう、それ以外ではやり直さない。"""
    keys = tuple(os.environ[k] for k in ACCOUNTS[acct]["x"]["keys"])
    url = f"{X_API}/{path}"
    query = query or {}
    full = url + ("?" + urllib.parse.urlencode(query) if query else "")
    for attempt in range(3):
        headers = {"Authorization": oauth1_header(method, url, query, keys)}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif multipart is not None:
            boundary = "----neru" + secrets.token_hex(8)
            parts = []
            for name, value in multipart.items():
                if isinstance(value, bytes):
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="image"\r\n'
                                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + value + b"\r\n")
                else:
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = urllib.request.Request(full, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429 and attempt < 2:
                print(f"  [X] 回数制限のため {60 * (attempt + 1)} 秒待ってやり直します")
                time.sleep(60 * (attempt + 1))
                continue
            raise RuntimeError(f"APIエラー {e.code} {method} {path}: {body}")


URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(com|jp|net|ee|me|co|io|ly)\b", re.I)


def x_length(text):
    """Xの文字数（日本語などは2、英数字は1。上限280）"""
    n = 0
    for ch in text:
        o = ord(ch)
        light = (o <= 0x10FF or 0x2000 <= o <= 0x200D or 0x2010 <= o <= 0x201F or 0x2032 <= o <= 0x2037)
        n += 1 if light else 2
    return n


class PartialPost(RuntimeError):
    """ツリーの途中で失敗した（1つ目は出ている）"""
    def __init__(self, first_id, msg):
        super().__init__(msg)
        self.first_id = first_id


def check_x_posts(s):
    parts = s.get("x_posts") or []
    problems = []
    for i, p in enumerate(parts, 1):
        if URL_RE.search(p["text"]):
            problems.append(f"{i}つ目にURLがあります（Xでは1回$0.20かかるので入れない）")
        if x_length(p["text"]) > 280:
            problems.append(f"{i}つ目が長すぎます（{x_length(p['text'])}/280）")
        if len(p.get("images", [])) > 4:
            problems.append(f"{i}つ目の画像が{len(p['images'])}枚（Xは4枚まで）")
        for img in p.get("images", []):
            if not os.path.exists(os.path.join("docs", img)):
                problems.append(f"画像がありません docs/{img}")
    return problems


def post_x(acct, s, base, dry):
    problems = check_x_posts(s)
    if problems:
        raise RuntimeError("Xの予約に問題があります：" + "／".join(problems))
    parts = s["x_posts"]
    if dry:
        me = x_call(acct, "GET", "users/me").get("data", {})
        print(f"  [X] 接続OK @{me.get('username')}（{len(parts)}つのツリー・文字数と画像OK。テストなのでアップも投稿もしません）")
        return None
    first_id = prev_id = None
    for i, p in enumerate(parts, 1):
        try:
            media_ids = []
            for img in p.get("images", []):
                with open(os.path.join("docs", img), "rb") as f:
                    up = x_call(acct, "POST", "media/upload",
                                multipart={"media": f.read(), "media_category": "tweet_image"})
                media_ids.append(up["data"]["id"])
            body = {"text": p["text"]}
            if media_ids:
                body["media"] = {"media_ids": media_ids}
            if prev_id:
                body["reply"] = {"in_reply_to_tweet_id": prev_id}
            prev_id = x_call(acct, "POST", "tweets", json_body=body)["data"]["id"]
            print(f"  [X] {i}/{len(parts)} 出しました id={prev_id}")
            first_id = first_id or prev_id
        except RuntimeError as e:
            if first_id:
                raise PartialPost(first_id, f"ツリーの{i}つ目で失敗（1つ目は出ています）：{e}")
            raise
    return first_id


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
        return [p for p in PLATFORMS if has_token(acct, p)]

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
            if plat == "x":
                print(f"  ✅ {ACCOUNTS[acct]['label']} x 鍵あり（Xは有料なので毎回の接続確認はしない）")
                continue
            # 毎回トークンが生きているか確かめる（投稿の無い日も。切れていたらメールで気づける）
            try:
                me = call(acct, plat, "GET", "me", fields="username")
                print(f"  ✅ {ACCOUNTS[acct]['label']} {plat} 接続OK @{me.get('username')}")
            except RuntimeError as e:
                failures.append(f"{ACCOUNTS[acct]['label']} {plat}：接続できません {e}")

    missed = [(s["id"], p) for s in schedule if s["date"] < today and acct_of(s) in ACCOUNTS
              for p in platforms(acct_of(s)) if (s["id"], p) not in done
              and (p != "x" or s.get("x_posts")) and (p != "threads" or s.get("threads_text"))]
    if missed and not os.environ.get("TARGET_DATE"):
        print(f"⚠ 過去の日付で未投稿のものがあります（自動では出しません）：{missed}")

    if not todays:
        print("今日の投稿はありません")
    def wants(s, p):
        return {"instagram": True, "threads": bool(s.get("threads_text")), "x": bool(s.get("x_posts"))}[p]

    pending = [s for s in todays for p in platforms(acct_of(s)) if wants(s, p) and (s["id"], p) not in done]
    if not pending and todays:
        print("今日の分はもう出ています")
    # 日付を指定したとき（テスト・取りこぼしを手で出すとき）だけ、時刻の判定をしない
    check_time = not os.environ.get("TARGET_DATE")

    def due(acct):
        """そのアカウントの投稿時刻を過ぎているか"""
        if not check_time:
            return True
        h, m = map(int, ACCOUNTS[acct].get("time", "21:00").split(":"))
        at = datetime.strptime(today, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=JST)
        return datetime.now(JST) >= at - timedelta(minutes=2)

    for s in todays:
        acct = acct_of(s)
        if acct not in ACCOUNTS or not platforms(acct):
            continue
        if not due(acct):
            print(f"⏳ {s['id']}｜{ACCOUNTS[acct]['label']} は {ACCOUNTS[acct].get('time','21:00')} から。この回では出しません")
            continue
        print(f"▶ {s['id']}｜{s['title']}　［{ACCOUNTS[acct]['label']}］")
        for plat, fn in (("instagram", post_instagram), ("threads", post_threads), ("x", post_x)):
            if plat not in platforms(acct) or (s["id"], plat) in done:
                continue
            if not wants(s, plat):
                continue
            try:
                media_id = fn(acct, s, base, dry)
            except PartialPost as e:
                media_id = e.first_id   # 出せた分は記録する（二重投稿を防ぐ）
                failures.append(f"{s['id']} {plat}：{e}")
                print(f"  ❌ {plat} 途中で失敗：{e}")
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
