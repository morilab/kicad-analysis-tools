# revhist — CHANGELOG.md → 回路図の改版履歴

正本は案件の `CHANGELOG.md`。回路図 1 枚目 (ルートシート) の改版履歴テキストと title_block の rev/date を CHANGELOG から書き写す / 一致を検査する。
規則は `skills/board-hotl/SKILL.md` §3.3。標準ライブラリだけ。KiCad 10.0.7 で確認 (書き込み後に kicad-cli の PDF 出力・ERC 0 を確認)。

```sh
R=tools/revhist/revhist.py
python3 $R CHANGELOG.md --print [--include-unreleased]      # 回路図に書く本文
python3 $R CHANGELOG.md board.kicad_sch [--at 300,20]        # 書き込み (元は *.revhist-bak)。既存テキストは位置を保って本文だけ更新
python3 $R CHANGELOG.md board.kicad_sch --check              # 0 = 一致、1 = 不一致、2 = CHANGELOG 書式エラー (G1/G3 の検査項目)
python3 tools/revhist/tests/test_revhist.py                             # 自己テスト
```

- CHANGELOG の見出し: `## revX.Y | YYYY-MM-DD | 作成者`、次の行 `要約: …`。日付が `未発行` (旧 `unreleased`) の版は `--include-unreleased` のときだけ書く
  (作業中の回路図に載せる場合。--check にも同じオプションを付ける)。
- 回路図側の目印: 先頭行が `改版履歴` の `(text …)` が 1 個 (旧版の `REVISION HISTORY` は見つけたら日本語に置き換える)。2 個以上はエラー。
- テキストは見出し「改版履歴」「版 / 日付 / 作成者 / 変更内容」と 1 版 1 行。人が読む文は日本語 (2026-10-09 規則)。既定ストロークフォントは等幅でないので桁は揃わない。カナは崩れるので要約・作成者は漢字・英数字で。
- KiCad で開いている回路図に書かない (KiCad を閉じてから。開いたままだと保存で上書きされる)。
