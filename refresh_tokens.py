"""Instagram と Threads のアクセストークンを延長する（毎週、GitHub Actions から起動）。

どちらも「作ってから24時間以上たった、まだ切れていないトークン」なら、新しい60日のトークンに交換できる。
新しいトークンは GitHub の秘密の設定に書き戻す（ワークフロー側で gh secret set）。
トークンはログに出さない。ここでは交換した値をファイルに書くだけ。

**複数アカウント対応**（2026-09-25〜）。設定されているものだけ延長する。

環境変数
  IG_TOKEN / THREADS_TOKEN                いまのトークン（AIの執事）
  IG_TOKEN_BALLET / THREADS_TOKEN_BALLET  いまのトークン（バレエ教室）
  IG_TOKEN_NERU / THREADS_TOKEN_NERU      いまのトークン（ネル）※Xの鍵は期限が無いので延長しない
  ※未設定のものは飛ばす
  OUT_DIR                   新しいトークンを書き出すフォルダ
"""
import json, os, sys, urllib.parse, urllib.request, urllib.error

IG = ("https://graph.instagram.com/refresh_access_token", "ig_refresh_token")
TH = ("https://graph.threads.net/refresh_access_token", "th_refresh_token")
TARGETS = {
    "IG_TOKEN": IG,
    "THREADS_TOKEN": TH,
    "IG_TOKEN_BALLET": IG,
    "THREADS_TOKEN_BALLET": TH,
    "IG_TOKEN_NERU": IG,
    "THREADS_TOKEN_NERU": TH,
}


def main():
    out = os.environ["OUT_DIR"]
    os.makedirs(out, exist_ok=True)
    failures = []
    for name, (url, grant) in TARGETS.items():
        token = os.environ.get(name)
        if not token:
            print(f"{name}：未設定のため飛ばします")
            continue
        q = urllib.parse.urlencode({"grant_type": grant, "access_token": token})
        try:
            with urllib.request.urlopen(f"{url}?{q}", timeout=60) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            failures.append(f"{name}：延長できませんでした {e.code} {e.read().decode(errors='replace')}")
            continue
        new = data.get("access_token")
        if not new:
            failures.append(f"{name}：応答にトークンがありません")
            continue
        # GitHubのログで万一出ても伏せ字になるよう登録してから、ファイルに書く
        print(f"::add-mask::{new}")
        with open(os.path.join(out, name), "w") as f:
            f.write(new)
        days = int(data.get("expires_in", 0)) // 86400
        print(f"{name}：延長しました（あと約{days}日有効）")
    if failures:
        print("\n".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
