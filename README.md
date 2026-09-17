# salon-ai-autopost

@salon.ai.office（サロンのAI導入屋さん）の投稿を、決まった日の 21:00 に自動で出す仕組み。

- `schedule.json` … いつ・どの画像・どのキャプションで出すか
- `docs/` … 投稿画像（GitHub Pages で公開。Instagram がここから画像を取りにくる）
- `post.py` … Instagram公式API（Instagramログイン）でカルーセルを投稿する
- `.github/workflows/post.yml` … 毎日 21:00（日本時間）に `post.py` を動かす
- `posted.json` … 投稿済みの記録（二重投稿を防ぐ）

## 秘密の設定（Settings → Secrets and variables → Actions）
- `IG_TOKEN` … アクセストークン（約60日で切れる。切れたら作り直して貼り替える）
- `IG_USER_ID` … Instagram のユーザーID

## テストのしかた
Actions → 「インスタ予約投稿」→ Run workflow → 「テスト」にチェックのまま、日付に投稿日を入れて実行。
投稿の直前（Instagram が画像を受け取り、カルーセルができた所）まで動かして止まる。

画像の数字・名前はすべてサンプル。
