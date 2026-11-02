# 部品の SPICE モデル (pdn 用)

このディレクトリには SPICE モデルを同梱していません (メーカーの著作物のため)。
必要なら、各自でメーカーのサイトから取得してここに置いてください。置かない場合、pdn は部品 YAML の ESR + ESL + C で近似します。

| ファイル | 取得元 | 部品 YAML (LCSC) |
|---|---|---|
| GRM155R61A475MEAA.mod | Murata SPICE Model (netlist) GRM シリーズ (https://www.murata.com/en-us/tool/data/spicedata/netlist-mlcc) | C23733 (代用) |
| GRM155R71C104KA88.mod | 同上 | C1525 (代用) |
| GRM21BR61E226ME44.mod | 同上 | C45783 (代用) |
| GRM188R61A106ME69.mod | 同上 | C19702 (代用) |

SPICE models are not bundled (vendor copyright). Download them from the vendor site if needed; otherwise pdn falls back to ESR + ESL + C.
