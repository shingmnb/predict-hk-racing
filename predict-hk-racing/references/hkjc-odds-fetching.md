# HKJC 賠率抓取實測（WIN/PLA/QIN/QPL）— Agent A 側證據

> 回答 build-spec-v1 §6.1 開放問題 1：「Q/PQ 真實 pool 賠率有無穩定公開來源？」
> 實測：2026-10-25 沙田（ST）賽日，賽後 PAYOUT 狀態抓 R1 + R5 兩場。
>
> **證據邊界（2026-10-25 14:38Z 審查後收窄）**：本文全部證據屬**賽後（`PAYOUT`）狀態**；
> live `SELLING` 窗口（T-60 tick、開賣前 pool 出現時間）未驗，由 issue #31 追蹤。

## 1. 來源與 query 契約（已實證）

- Endpoint：`POST https://info.cld.hkjc.com/graphql/base/`，headers 必帶
  `Origin: https://bet.hkjc.com`、`Referer: https://bet.hkjc.com/`、`Content-Type: application/json`，curl 要 `--compressed`。
- **Query body 係白名單制**。可重現嘅完整 body 全文如下（`operationName: "racing"`，2026-10-25 實測通過）：

```graphql
query racing($date: String, $venueCode: String, $oddsTypes: [OddsType], $raceNo: Int) {
  raceMeetings(date: $date, venueCode: $venueCode) {
    pmPools(oddsTypes: $oddsTypes, raceNo: $raceNo) {
      id
      status
      sellStatus
      oddsType
      lastUpdateTime
      guarantee
      minTicketCost
      name_en
      name_ch
      leg { number races }
      cWinSelections { composite name_ch name_en starters }
      oddsNodes { combString oddsValue hotFavourite oddsDropValue bankerOdds { combString oddsValue } }
    }
  }
}
```

  variables 例：`{"date":"2026-10-25","venueCode":"ST","oddsTypes":["WIN","PLA","QIN","QPL"],"raceNo":1}`。
- **刪欄位行為（按實測範圍寫）**：2026-10-25 實測刪除以下**四個** field，每個獨立觸發
  `Internal server error - WHITELIST_ERROR`：`oddsDropValue`、`bankerOdds{…}`（`oddsNodes` 內）；
  `guarantee`、`cWinSelections{…}`（`pmPools` 內）。其餘 field **未逐個刪測**——「唔可以為慳 payload
  自行裁剪 query」係由呢四個橫跨兩層嘅樣本推斷，唔係逐欄位證明。
- **QIN/QPL 唔使新 query**：同一條 body，`variables.oddsTypes` 加 `"QIN"`、`"QPL"` 就得。

## 2. QIN/QPL 實測結果（2026-10-25 ST，賽後 PAYOUT 狀態）

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
  開賣前 pool 出現時間——下個賽日補實測（issue #31 追蹤）。
- `bankerOdds` 喺 QIN/QPL pool 有無值：兩場樣本都冇非空樣本，未證實有定冇。
- QIN/QPL 抽水率估算：要有跨多個賽日嘅 closing odds 先可以估，今次只確立「來源可得」。

## 4. 對 `tickets.py` gating 嘅含意（等 B 拍板）

§6.1 原文 gating 前提係「QIN/QPL pool query 與 closing odds 抓取未實測，唔可以假設可得」——
**賽後層面已被推翻**（query 可得 + closing odds 可抓，兩場實證）；**live 層面未驗**（#31）。
`ev: None + estimated: true` 係咪即時解除、定係等 T-60 live tick 都驗埋先解除，由 B 拍板；Kimi 照拍板結果落 code。
