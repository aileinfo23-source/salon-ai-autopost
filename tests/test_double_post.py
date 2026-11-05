"""二重投稿の防ぎ（2026-11-04 hw02 の再発防止）を、偽のAPIで確かめる。本物のAPIは呼ばない。

  python3 tests/test_double_post.py
"""
import json, os, shutil, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import post  # noqa: E402

post.time.sleep = lambda s: None
ENV = {"IG_TOKEN_BALLET": "igt", "IG_USER_ID_BALLET": "900", "THREADS_TOKEN_BALLET": "tht",
       "BASE_URL": "https://example.github.io/repo", "TARGET_DATE": "2026-11-04"}
CAPTION = "ハロウィンのレッスン、こんな日でした🎃\n\nこの日だけは、レッスン着を衣装に着替えて。"
ENTRY = {"id": "hw02", "account": "ballet", "date": "2026-11-04", "title": "t", "images": ["hw02/1.jpg"],
         "video": "hw02/reel.mp4", "caption": CAPTION, "threads_text": CAPTION}


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class Fake:
    """post.call の代わり。recent に「もう出ている投稿」を入れておける。"""
    def __init__(self, recent=None):
        self.recent = recent or []
        self.publishes = []

    def __call__(self, acct, platform, method, path, **params):
        if path == "me":
            return {"user_id": "900", "id": "th_user", "username": "kasuthijomion_ballet"}
        if path.endswith("/media") and method == "GET" or path.endswith("/threads") and method == "GET":
            key = "caption" if platform == "instagram" else "text"
            return {"data": [{"id": r["id"], key: r["text"], "timestamp": r["ts"]}
                             for r in self.recent if r["platform"] == platform]}
        if path.endswith("_publish"):
            self.publishes.append(platform)
            return {"id": f"{platform}_new"}
        if method == "POST":
            return {"id": "container"}
        return {"status_code": "FINISHED", "status": "FINISHED"}


def run(fake, posted=None, dry=False):
    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    saved = dict(os.environ)
    try:
        os.chdir(d)
        json.dump([ENTRY], open("schedule.json", "w"), ensure_ascii=False)
        json.dump(posted or [], open("posted.json", "w"))
        os.environ.update(ENV, DRY_RUN="1" if dry else "0")
        post.call = fake
        code = 0
        try:
            post.main()
        except SystemExit as e:
            code = e.code
        return code, json.load(open("posted.json"))
    finally:
        os.chdir(cwd)
        shutil.rmtree(d)
        os.environ.clear()
        os.environ.update(saved)


results = []


def check(name, cond):
    results.append((name, cond))
    print(("✅ " if cond else "❌ ") + name)


# 1. 何も出ていなければ、両方に出す
f = Fake()
code, posted = run(f)
check("まだなら両方に出す", code == 0 and f.publishes == ["instagram", "threads"] and len(posted) == 2)

# 2. 11/4 と同じ：記録は古いが、4分前に同じ投稿がもう出ている → 出さずに、出ているIDで記録する
f = Fake([{"platform": "instagram", "id": "17950899498345834", "text": CAPTION, "ts": ago(4)},
          {"platform": "threads", "id": "18089706527284797", "text": CAPTION, "ts": ago(3)}])
code, posted = run(f)
check("もう出ていたら出さない", code == 0 and f.publishes == [])
check("出ていたIDで記録する", sorted(p["media_id"] for p in posted) == ["17950899498345834", "18089706527284797"])

# 3. 片方だけ出ていたら、もう片方だけ出す
f = Fake([{"platform": "instagram", "id": "ig_old", "text": CAPTION, "ts": ago(10)}])
code, posted = run(f)
check("インスタだけ出ていたらThreadsだけ出す", f.publishes == ["threads"] and len(posted) == 2)

# 4. 同じ書き出しでも、数日前のもの（別の日の予約）は別物として扱い、出す
f = Fake([{"platform": "instagram", "id": "old", "text": CAPTION, "ts": ago(60 * 24 * 5)}])
code, posted = run(f)
check("5日前の同じ書き出しは別物", f.publishes == ["instagram", "threads"])

# 5. テストのときは、もう出ていても記録を書き換えない
f = Fake([{"platform": "instagram", "id": "x", "text": CAPTION, "ts": ago(4)}])
code, posted = run(f, dry=True)
check("テストでは記録しない", code == 0 and posted == [] and f.publishes == [])

# 6. record.py：先に別の回が記録を足していても、この回の記録を足す（衝突で消さない）
d = tempfile.mkdtemp()
try:
    base = [{"id": "hw01", "platform": "instagram", "media_id": "1"}]
    latest = base + [{"id": "hw02", "platform": "instagram", "media_id": "A"}]
    mine = base + [{"id": "hw02", "platform": "instagram", "media_id": "B"},
                   {"id": "hw02", "platform": "threads", "media_id": "C"}]
    json.dump(latest, open(os.path.join(d, "posted.json"), "w"))
    json.dump(mine, open(os.path.join(d, "mine.json"), "w"))
    subprocess.run([sys.executable, os.path.join(REPO, "record.py"), "mine.json"], cwd=d, check=True,
                   capture_output=True)
    merged = json.load(open(os.path.join(d, "posted.json")))
    check("record.py は両方の記録を残す", [p["media_id"] for p in merged] == ["1", "A", "B", "C"])
    subprocess.run([sys.executable, os.path.join(REPO, "record.py"), "mine.json"], cwd=d, check=True,
                   capture_output=True)
    check("record.py を2回呼んでも増えない", len(json.load(open(os.path.join(d, "posted.json")))) == 4)
finally:
    shutil.rmtree(d)

print(f"\n{sum(ok for _, ok in results)}/{len(results)} 合格")
sys.exit(0 if all(ok for _, ok in results) else 1)
