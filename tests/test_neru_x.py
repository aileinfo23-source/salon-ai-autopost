"""ネル（neru）とXの投稿を、偽のAPIで確かめる。本物のAPIは呼ばない。

  python3 tests/test_neru_x.py
"""
import io, json, os, shutil, sys, tempfile, urllib.error
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import post  # noqa: E402

post.time.sleep = lambda s: None

NERU_ENV = {
    "IG_TOKEN_NERU": "igt", "IG_USER_ID_NERU": "900", "THREADS_TOKEN_NERU": "tht",
    "X_API_KEY_NERU": "ck", "X_API_SECRET_NERU": "cs", "X_ACCESS_TOKEN_NERU": "tk", "X_ACCESS_SECRET_NERU": "ts",
}
ALL_KEYS = list(NERU_ENV) + ["IG_TOKEN", "IG_USER_ID", "THREADS_TOKEN", "IG_TOKEN_BALLET", "IG_USER_ID_BALLET",
                             "THREADS_TOKEN_BALLET", "TARGET_DATE", "DRY_RUN"]


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


class FakeAPI:
    def __init__(self, fail_tweet_no=None):
        self.calls = []
        self.tweets = []
        self.uploads = 0
        self.fail_tweet_no = fail_tweet_no

    def __call__(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        self.calls.append((method, url))
        if "api.x.com" in url:
            assert req.headers.get("Authorization", "").startswith("OAuth "), "Xに署名が付いていない"
            if url.endswith("/users/me"):
                return self.ok({"data": {"id": "77", "username": "neru_ninja"}})
            if url.endswith("/media/upload"):
                assert b'name="media_category"' in req.data and b"tweet_image" in req.data
                self.uploads += 1
                return self.ok({"data": {"id": f"m{self.uploads}"}})
            if url.endswith("/tweets"):
                body = json.loads(req.data)
                n = len(self.tweets) + 1
                if self.fail_tweet_no == n:
                    raise urllib.error.HTTPError(url, 500, "err", {}, io.BytesIO(b'{"title":"Internal"}'))
                self.tweets.append(body)
                return self.ok({"data": {"id": f"tw{n}"}})
            raise AssertionError("知らないXのURL " + url)
        if "graph.instagram.com" in url:
            if "/me?" in url:
                return self.ok({"user_id": "900", "username": "neru_ninja"})
            if "media_publish" in url:
                return self.ok({"id": "ig_pub"})
            if method == "POST":
                return self.ok({"id": "igc"})
            return self.ok({"status_code": "FINISHED"})
        if "graph.threads.net" in url:
            if "/me?" in url:
                return self.ok({"id": "th_user", "username": "neru_ninja"})
            if "threads_publish" in url:
                return self.ok({"id": "th_pub"})
            if method == "POST":
                return self.ok({"id": "thc"})
            return self.ok({"status": "FINISHED"})
        raise AssertionError("知らないURL " + url)

    @staticmethod
    def ok(obj):
        return Resp(json.dumps(obj).encode())

    def count(self, host):
        return sum(1 for _, u in self.calls if host in u)


def neru_entry(**over):
    e = {"id": "neru01", "account": "neru", "date": "2026-10-05", "title": "自己紹介",
         "images": ["neru01/1.jpg", "neru01/2.jpg"], "caption": "この投稿も、ボクが寝てる間に出てます。\n#自動投稿",
         "threads_text": "この投稿も、ボクが寝てる間に出てます。", "threads_topic": "SNS運用",
         "x_posts": [{"text": "この投稿も、ボクが寝てる間に出てます。ニン", "images": ["neru01/1.jpg", "neru01/2.jpg"]},
                     {"text": "続きはプロフィールのリンクから。", "images": []}]}
    e.update(over)
    return e


def run(schedule, env, posted=None, fake=None, now=None):
    """一時フォルダで post.main() を動かす。戻り値：(終了コード, posted, fake)"""
    fake = fake or FakeAPI()
    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    saved = {k: os.environ.get(k) for k in ALL_KEYS}
    try:
        os.chdir(d)
        os.makedirs("docs/neru01")
        for n in (1, 2):
            open(f"docs/neru01/{n}.jpg", "wb").write(b"\xff\xd8fakejpg")
        json.dump(schedule, open("schedule.json", "w"), ensure_ascii=False)
        json.dump(posted or [], open("posted.json", "w"))
        for k in ALL_KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        os.environ["BASE_URL"] = "https://example.github.io/repo"
        post.urllib.request.urlopen = fake
        if now:
            class FakeDT(datetime):
                @classmethod
                def now(cls, tz=None):
                    return now
            post.datetime = FakeDT
        code = 0
        try:
            post.main()
        except SystemExit as e:
            code = e.code
        finally:
            post.datetime = datetime
        return code, json.load(open("posted.json")), fake
    finally:
        os.chdir(cwd)
        shutil.rmtree(d)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


results = []


def check(name, cond):
    results.append((name, cond))
    print(("✅ " if cond else "❌ ") + name)


# 1. OAuth 1.0a の署名が、Xの説明書の見本と一致する
h = post.oauth1_header(
    "POST", "https://api.twitter.com/1.1/statuses/update.json",
    {"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
    ("xvz1evFS4wEEPTGEFPHBog", "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
     "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE"),
    nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", stamp=1318622958)
check("署名が見本と一致", 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in h)

# 2. 文字数とURLの見分け
check("日本語は2で数える", post.x_length("あいう") == 6 and post.x_length("abc") == 3)
check("URLを見つける", bool(post.URL_RE.search("LINEは https://lin.ee/abc")) and bool(post.URL_RE.search("lin.ee/abc")))
check("普通の文はURL扱いしない", not post.URL_RE.search("3つのSNSに、決めた時間で自動投稿。ニン"))

# 3. 本番：インスタ・スレッズ・Xの3つに出る。Xはツリー
env = dict(NERU_ENV, TARGET_DATE="2026-10-05")
code, posted, f = run([neru_entry()], env)
plats = sorted(p["platform"] for p in posted)
check("3つとも記録", code == 0 and plats == ["instagram", "threads", "x"])
check("Xはツリーで2つ（2つ目は1つ目への返信）",
      len(f.tweets) == 2 and "reply" not in f.tweets[0] and f.tweets[1]["reply"]["in_reply_to_tweet_id"] == "tw1")
check("Xの画像は2枚アップして1つ目に付く", f.uploads == 2 and f.tweets[0]["media"]["media_ids"] == ["m1", "m2"])
check("Xの記録は1つ目のID", [p["media_id"] for p in posted if p["platform"] == "x"] == ["tw1"])

# 4. 同じ日にもう一度動いても、二重に出さない
code, posted2, f2 = run([neru_entry()], env, posted=posted)
check("2回目は何もしない", code == 0 and len(posted2) == 3 and not f2.tweets and f2.count("media_publish") == 0)

# 5. Xのツリーの途中で失敗：1つ目で記録して、失敗を知らせる。インスタ・スレッズは出る
code, posted, f = run([neru_entry()], env, fake=FakeAPI(fail_tweet_no=2))
x_rec = [p for p in posted if p["platform"] == "x"]
check("途中失敗でも1つ目を記録・失敗で終わる", code == 1 and len(x_rec) == 1 and x_rec[0]["media_id"] == "tw1"
      and len(posted) == 3)

# 6. Xの1つ目で失敗：記録しない（次に出し直せる）
code, posted, f = run([neru_entry()], env, fake=FakeAPI(fail_tweet_no=1))
check("1つ目で失敗なら記録しない", code == 1 and not [p for p in posted if p["platform"] == "x"] and len(posted) == 2)

# 7. XにURLがあったら出さない（お金がかかるため）。ほかは出る
bad = neru_entry(x_posts=[{"text": "LINEはこちら https://lin.ee/QQ", "images": []}])
code, posted, f = run([bad], env)
check("URL入りはXに出さない", code == 1 and f.count("api.x.com") == 0 and len(posted) == 2)

# 8. テスト（DRY_RUN）：Xは自分のアカウントを読むだけ。アップも投稿もしない
code, posted, f = run([neru_entry()], dict(env, DRY_RUN="1"))
check("テストはXに出さない", code == 0 and not posted and not f.tweets and f.uploads == 0
      and any(u.endswith("/users/me") for _, u in f.calls))

# 9. x_posts が無い予約は、Xには出さない
code, posted, f = run([neru_entry(x_posts=None)], env)
check("x_postsなしならXは飛ばす", code == 0 and len(posted) == 2 and f.count("api.x.com") == 0)

# 10. 22:00より前は出さない、過ぎたら出す
from post import JST  # noqa: E402
code, posted, f = run([neru_entry()], NERU_ENV, now=datetime(2026, 10, 5, 21, 0, tzinfo=JST))
check("21:00の起動では出さない", code == 0 and not posted)
code, posted, f = run([neru_entry()], NERU_ENV, now=datetime(2026, 10, 5, 22, 3, tzinfo=JST))
check("22:03の起動で出る", code == 0 and len(posted) == 3)

# 11. 執事の日：ネルの鍵があってもXは呼ばない（接続確認もしない）
shitsuji = {"id": "n99", "date": "2026-10-05", "title": "t", "images": ["neru01/1.jpg", "neru01/2.jpg"],
            "caption": "c", "threads_text": "c"}
code, posted, f = run([shitsuji], dict(NERU_ENV, IG_TOKEN="a", IG_USER_ID="900", THREADS_TOKEN="b",
                                       TARGET_DATE="2026-10-05"))
check("執事の日はXを呼ばない", code == 0 and f.count("api.x.com") == 0 and len(posted) == 2
      and all(p["account"] == "shitsuji" for p in posted))

# 12. ネルの鍵が無ければ、ネルの予約は飛ばす（失敗にしない）
code, posted, f = run([neru_entry()], {"TARGET_DATE": "2026-10-05"})
check("鍵なしは飛ばす", code == 0 and not posted and not f.calls)

print(f"\n{sum(ok for _, ok in results)}/{len(results)} 合格")
sys.exit(0 if all(ok for _, ok in results) else 1)
