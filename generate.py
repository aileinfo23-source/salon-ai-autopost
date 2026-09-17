"""「写真を入れる」フォルダに入った写真・動画から、投稿文を作って次の空き枠に予約する。

- 写真（1枚でも複数枚でも）→ 1つの投稿（2枚以上ならカルーセル）
- 動画 → 1本ずつ、インスタはリール、Threadsは動画の投稿
- 文章は「お店の設定.md」に沿って Claude が作る（写真・動画のコマを見て書く）
- 予約日は「お店の設定.md」の曜日のうち、明日以降で空いている日（時間は毎日21:00の起動に合わせる）
- 作ったら「【予約しました】」の Issue を立てる → GitHub からメールで届く。直したい時は schedule.json を編集、やめたい時はその予定を消す

環境変数
  ANTHROPIC_API_KEY  Claude の API キー（GitHubの秘密の設定）
  INBOX              写真を入れるフォルダ（既定：写真を入れる）
"""
import base64, io, json, os, re, shutil, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

import anthropic
from PIL import Image, ImageOps

try:  # iPhone の HEIC 写真も読めるようにする
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

JST = timezone(timedelta(hours=9))
INBOX = os.environ.get("INBOX", "写真を入れる")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
WEEKDAYS = "月火水木金土日"
MODEL = "claude-opus-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "管理用の短い題名（20字以内）"},
        "instagram_caption": {"type": "string", "description": "インスタのキャプション本文（ハッシュタグは含めない）"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "#付きのハッシュタグ"},
        "threads_text": {"type": "string", "description": "Threadsの本文（ハッシュタグなし・500字以内）"},
        "needs_check": {"type": "array", "items": {"type": "string"},
                        "description": "写真から読み取れず、人に確認してほしい点（無ければ空）"},
    },
    "required": ["title", "instagram_caption", "hashtags", "threads_text", "needs_check"],
    "additionalProperties": False,
}


def to_post_jpeg(src, dst):
    """インスタの縦長（4:5・1080×1350）に中央で切り抜いてJPEGにする（APIはJPEGしか受け付けない）。"""
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    im = ImageOps.fit(im, (1080, 1350), Image.LANCZOS, centering=(0.5, 0.5))
    im.save(dst, "JPEG", quality=90, optimize=True)


def to_reel_mp4(src, dst):
    """リール向けの形式（H.264／AAC／縦1080×1920に収める）に変換する。"""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst], check=True)


def video_frames(src, n=4):
    """動画から等間隔でn枚のコマを切り出す（AIに見せる用）。"""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", src], capture_output=True, text=True, check=True).stdout)
    tmp = tempfile.mkdtemp()
    paths = []
    for i in range(n):
        p = os.path.join(tmp, f"{i}.jpg")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(dur * (i + 0.5) / n), "-i", src,
                        "-frames:v", "1", p], check=True)
        paths.append(p)
    return paths


def image_block(path):
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im.thumbnail((1024, 1024))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def write_text(settings, view_paths, kind):
    client = anthropic.Anthropic()
    content = [image_block(p) for p in view_paths]
    what = "動画から切り出したコマ" if kind == "reel" else ("写真" if len(view_paths) == 1 else "カルーセルの写真（この順番で並びます）")
    content.append({"type": "text", "text":
        f"上の{what}で、インスタとThreadsの投稿文を作ってください。"
        "写真に写っていること以外の事実・数字・実績は足さないでください。"
        "分からない点は needs_check に書いてください。"})
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},  # 安全のための判定で断られたら、別のモデルで自動でやり直す
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system="あなたはSNS投稿の文章を書く担当です。次の「お店の設定」を必ず守って書きます。\n\n" + settings,
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("AIが文章の作成を断りました。写真の内容を確認してください")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def post_days(settings):
    m = re.search(r"## 投稿する曜日\s*\n-\s*(.+)", settings)
    days = {WEEKDAYS.index(c) for c in (m.group(1) if m else "月水金") if c in WEEKDAYS}
    return days or {0, 2, 4}


def next_free_date(schedule, days):
    used = {s["date"] for s in schedule}
    d = datetime.now(JST).date() + timedelta(days=1)  # 最低1日は、確認して直す時間をあける
    while d.weekday() not in days or d.isoformat() in used:
        d += timedelta(days=1)
    return d.isoformat()


def notify(entry, text, repo):
    d = datetime.fromisoformat(entry["date"])
    body = [f"**{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）21:00** に、インスタとThreadsへ自動で投稿します。",
            "", "直したいとき：`schedule.json` の該当の文章を編集してください。",
            "やめたいとき：`schedule.json` からこの予定（id: `" + entry["id"] + "`）を消してください。", ""]
    if text["needs_check"]:
        body += ["### ⚠ 確認してほしいこと"] + [f"- {x}" for x in text["needs_check"]] + [""]
    body += ["### インスタ", "```", entry["caption"], "```", "### Threads", "```", entry["threads_text"], "```"]
    media = entry.get("video") or entry["images"][0]
    body += ["", f"素材：https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/{media}"]
    subprocess.run(["gh", "issue", "create", "-R", repo,
                    "--title", f"【予約しました】{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）{entry['title']}",
                    "--body", "\n".join(body)], check=True)


def main():
    files = sorted(f for f in os.listdir(INBOX) if not f.startswith("."))
    images = [f for f in files if os.path.splitext(f)[1].lower() in IMAGE_EXT]
    videos = [f for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXT]
    if not images and not videos:
        print("新しい写真・動画はありません")
        return
    settings = open("お店の設定.md").read()
    schedule = json.load(open("schedule.json"))
    days = post_days(settings)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    stamp = datetime.now(JST).strftime("%Y%m%d%H%M")

    batches = ([("post", images)] if images else []) + [("reel", [v]) for v in videos]
    for n, (kind, group) in enumerate(batches, 1):
        pid = f"a{stamp}{n:02d}"
        os.makedirs(f"docs/{pid}", exist_ok=True)
        src = [os.path.join(INBOX, f) for f in group]
        entry = {"id": pid, "date": next_free_date(schedule, days)}
        if kind == "reel":
            entry["video"] = f"{pid}/video.mp4"
            to_reel_mp4(src[0], f"docs/{entry['video']}")
            view = video_frames(f"docs/{entry['video']}")
        else:
            entry["images"] = []
            for i, s in enumerate(src[:10], 1):  # カルーセルは10枚まで
                to_post_jpeg(s, f"docs/{pid}/{i}.jpg")
                entry["images"].append(f"{pid}/{i}.jpg")
            view = [f"docs/{p}" for p in entry["images"]]
        text = write_text(settings, view, kind)
        entry.update({
            "title": text["title"],
            "caption": text["instagram_caption"].rstrip() + "\n\n" + " ".join(text["hashtags"]),
            "threads_text": text["threads_text"][:500],
            "threads_topic": "サロン経営",
            "made_by": "AI（" + MODEL + "）",
        })
        schedule.append(entry)
        schedule.sort(key=lambda s: s["date"])
        json.dump(schedule, open("schedule.json", "w"), ensure_ascii=False, indent=2)
        print(f"予約しました：{entry['date']} {entry['title']}（{kind}）")
        if repo:
            notify(entry, text, repo)

    for f in images + videos:  # 使い終わった元の写真・動画はフォルダから片づける（変換したものは docs/ に残る）
        os.remove(os.path.join(INBOX, f))


if __name__ == "__main__":
    main()
