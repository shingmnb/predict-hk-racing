# HKJC 賠率抓取實測（WIN/PLA/QIN/QPL）— Agent A 側證據

> 回答 build-spec-v1 §6.1 開放問題 1：「Q/PQ 真實 pool 賠率有無穩定公開來源？」
> 實測：2026-10-25 沙田（ST）賽日，賽後 PAYOUT 狀態抓 R1 + R5 兩場。

## 1. 來源與 query 契約（已實證）

- Endpoint：`POST https://info.cld.hkjc.com/graphql/base/`，headers 必帶
  `Origin: https://bet.hkjc.com`、`Referer: https://bet.hkjc.com/`、`Content-Type: application/json`，curl 要 `--compressed`。
- **Query body 係白名單制**：只可以用已驗證嘅完整 query（`operationName: racing`，fields：
  `pmPools{id status sellStatus oddsType lastUpdateTime guarantee minTicketCost name_en name_ch leg{number races} cWinSelections{...} oddsNodes{combString oddsValue hotFavourite oddsDropValue bankerOdds{combString oddsValue}}}`）。
  刪任何 field 即 `WHITELIST_ERROR`（Internal server error）——唔可以為慳 payload 自行裁剪 query。
- **QIN/QPL 唔使新 query**：同一條 body，`variables.oddsTypes` 加 `"QIN"`、`"QPL"` 就得
  （例：`["WIN","PLA","QIN","QPL"]`）。

## 2. QIN/QPL 實測結果（2026-10-25 ST）

| 場 | 出馬 | QIN nodes | QPL nodes | 完整性檢查 |
|---|---|---|---|---|
| R1 | 11 | 55 | 55 | C(11,2)=55 ✓ |
| R5 | 14 | 91 | 91 | C(14,2)=91 ✓ |

- **combString 語義**：pair = `NN,MM`（零填充、逗號分隔；樣本內 pair 兩位恒升序，如 `01,02`、`09,13`）。
  單式（WIN/PLA）= `NN`。解析時按逗號 split，唔好假設對方陣列順序。
- **組合排序警告**：`oddsNodes` 排序係**賠率升序**（熱組合行先），唔係字典序；QIN 同 QPL
  各自獨立排序（R5 尾段兩個 pool 順序已見分歧）。**pmPools 順序亦唔保證**（R1 回應順序係
  PLA→QIN→QPL→WIN）——同 WIN/PLA 舊教訓一致，必須 `oddsType` filter，唔可以靠陣列位置。
- **賽後仍可抓 closing odds**：status=`PAYOUT`、sellStatus=`STOP_SELL` 之下 final odds 保留
  （R1 樣本：WIN `02`→2.0；QIN `01,02`→5.3；QPL `01,02`→2.3；`lastUpdateTime` 有 final tick 時間）。
- 樣本觀察：QIN/QPL `guarantee=None`、`minTicketCost=0`（R1/R5）；`leg.races=[n]` 單關。

## 3. 未驗證（誠實邊界）

- **T-60 live selling window 未觀察**：status=`SELLING` 時嘅 tick 頻率、`oddsDropValue` 變化、
  開賣前 pool 出現時間——下個賽日補實測。
- `bankerOdds` 喺 QIN/QPL pool 有無值：兩場樣本都冇非空樣本，未證實有定冇。
- QIN/QPL 抽水率估算：要有跨多個賽日嘅 closing odds 先可以估，今次只確立「來源可得」。

## 4. 對 `tickets.py` gating 嘅含意（等 B 拍板）

§6.1 原文 gating 前提係「QIN/QPL pool query 與 closing odds 抓取未實測，唔可以假設可得」——
**此前提已被推翻**（query 可得 + closing odds 可抓，兩場實證）。`ev: None + estimated: true`
係咪可以解除、定係等 T-60 live tick 都驗埋先解除，由 B 拍板；Kimi 照拍板結果落 code。
