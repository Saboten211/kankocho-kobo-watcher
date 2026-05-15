# 観光庁 公募ウォッチャー

観光庁が公開している公募情報を毎日自動取得し、新着・締切・カテゴリで見やすく
一覧化する閲覧用サイト。掲載元の公開情報のみを扱う。

## 仕組み

GitHub Actions が毎日 `scraper.py` を実行 → `data.json` と `index.html` を更新
→ GitHub Pages で公開。前回の `data.json` との差分で新着・ステータス変化を検知する。

## ローカル実行

```
pip install -r requirements.txt
python3 scraper.py        # data.json と index.html を生成
```

`index.html` は data 内蔵の自己完結ファイル。ブラウザで直接開ける。

## 出典

観光庁 公募情報 https://www.mlit.go.jp/kankocho/kobo.html
（本サイトは自動生成。応募の最終確認は必ず各公募の詳細ページで行うこと）
