# HKJC 馬會賠率即時攞取方法（GraphQL 實戰指南）

> 來自實戰系統（10 個月每日使用驗證）。目的：令 `race-card` 嘅 `win_odds`／`place_odds` 欄位全自動化 — 同一時點全場快照，符合 data-collection.md「賠率要全場同一快照」規則。
> 呢個方法唔需要 API key（公開 GraphQL endpoint），但要正確 headers。

## 一、Endpoint 同 Headers

```bash
ENDPOINT="https://info.cld.hkjc.com/graphql/base/"
curl -s "$ENDPOINT" \
  -H "content-type: application/json" \
  -H "Origin: https://bet.hkjc.com" \
  -H "Referer: https://bet.hkjc.com/" \
  --compressed \
  -d '{"query":"<QUERY_BODY>"}'
```

**三個必守位**：
1. `Origin`/`Referer` 一定要係 `bet.hkjc.com`（唔係 racing.hkjc.com）— 缺咗即刻被拒
2. `--compressed` 必須（回應係 gzip）
3. Query body 要 exact match（下面有可複製版本）— 唔好自己改 field 名

## 二、Query Body（WIN/PLA 池即時賠率）

```json
{
  "query": "query MeetingWinPlaceOdds($meetingDate: String!, $venue: MeetingVenue!, $raceNo: Int!) { meeting(raceDate: $meetingDate, venue: $venue) { race(no: $raceNo) { pmPools { oddsType pool(mergeLeg: false) { oddsNodes { combString odds } } } } } }",
  "variables": {
    "meetingDate": "2026-10-24",
    "venue": "S1",
    "raceNo": 8
  }
}
```

`venue` 值：`ST`（沙田）／`HV`（跑馬地）／`S1`-`S4`（越洋轉播 series）。`meetingDate` 用香港日期（YYYY-MM-DD）。

## 三、回應結構同三個陷阱

```json
{ "data": { "meeting": { "race": { "pmPools": [
  { "oddsType": "WIN", "pool": { "oddsNodes": [ { "combString": 1, "odds": 4.6 }, ... ] } },
  { "oddsType": "PLA", "pool": { "oddsNodes": [...] } }
] } } } }
```

| 陷阱 | 說明 | 對策 |
|---|---|---|
| **pmPools 順序唔保證** | 實測試過 `pool[0]` 係 PLA 唔係 WIN | **必須 filter `oddsType == 'WIN'`**，唔可以靠索引 |
| **combString 係 int** | `combString: 1` = 1 號馬（唔係字串） | 轉換時 `int(combString)` |
| **NR 偵測** | 退出馬嘅 cloth 唔會出現喺 `oddsNodes` | 賠率馬數 < 排位表馬數 → 差額就係 NR；以賠率表做「live 真相」唔好信官方匹數（海外場後備補位常態，試過官方 15 匹但 #16 實為正選） |

**數據完整性 check**（經驗值）：
- WIN max 應該 20-60 倍（超過 100 = 數據壞）
- 全場最熱 1.5-1.8 = 呢個係 PLA 池特徵（WIN 最熱通常 1.8+）— 用嚟驗證自己啱唔啱池

## 四、Python 封裝（OddsTracker 模式）

```python
import requests, json

class OddsTracker:
    ENDPOINT = "https://info.cld.hkjc.com/graphql/base/"
    HEADERS = {"content-type": "application/json",
               "Origin": "https://bet.hkjc.com",
               "Referer": "https://bet.hkjc.com/"}
    QUERY = """query MeetingWinPlaceOdds($d: String!, $v: MeetingVenue!, $r: Int!) {
      meeting(raceDate: $d, venue: $v) { race(no: $r) {
        pmPools { oddsType pool(mergeLeg: false) { oddsNodes { combString odds } } } } } }"""

    def fetch(self, date_hk, venue, race_no):
        r = requests.post(self.ENDPOINT, json={"query": self.QUERY,
            "variables": {"d": date_hk, "v": venue, "r": race_no}},
            headers=self.HEADERS, timeout=20)
        pools = r.json()["data"]["meeting"]["race"]["pmPools"]
        out = {}
        for p in pools:
            key = "win" if p["oddsType"] == "WIN" else "place" if p["oddsType"] == "PLA" else None
            if not key: continue
            out[key] = {str(n["combString"]): {"odds": n["odds"]} for n in p["pool"]["oddsNodes"]}
        return out
```

用法：`ot.fetch("2026/10/24", "S1", 8)` → `{"win": {"1": {"odds": 7.9}, ...}, "place": {...}}`

## 五、餵入 race-card（同 data_cutoff 規則配合）

```python
snap = ot.fetch(date, venue, race_no)
for runner in card["runners"]:
    cloth = str(runner["horse_no"])
    if cloth in snap["win"]:
        runner["win_odds"] = snap["win"][cloth]["odds"]
        runner["place_odds"] = snap["place"].get(cloth, {}).get("odds")
    else:
        runner["scratched"] = True   # 賠率冇 = NR，雙源核實（CONFLICT 規則：以賠率為準）
```

快照時間戳 = `captured_at` — 一次 fetch 全場同一時點，天然符合「賠率要全場同一快照」。

## 六、快照節奏建議（實戰經驗）

| 時點 | 用途 |
|---|---|
| 賽前一日晚（T-16h） | 早盤快照（市場成形） |
| T-60min | 主快照（推薦凍結用） |
| T-15 起 | 滾動快照（捕捉 late money） |

**時區鐵則**：香港開跑時間用 `TZ=Asia/Hong_Kong` 計 — 伺服器 UTC 時間直接標 HKT 會錯 8 小時（實戰事故：全日時間判斷錯，賠率裁決時窗全錯）。所有 `captured_at`/`data_cutoff`/`off_time` 帶 timezone ISO-8601（你嘅 schema 已要求，呢度係執行提醒）。

**落飛訊號（餵 intent_evidence 用）**：兩個時點快照對比，`oddsDrop = (odds_early - odds_late) / odds_early ≥ 40%` 記 `signal: "market_support"`（記住：只係市場支持，唔係意圖證據 — intent-model.md 已有呢條規則）。

## 七、已知限制

- GraphQL 偶發 502/timeout：重試 2 次唔得就用上一個快照 + 記 warning
- 海外場（S1-S4）開跑前 ~2 小時先有穩定池
- 呢個 endpoint 係無文檔嘅公開接口 — field 名有機會變（10 個月未變過，但 schema 加保護：解析失敗即刻報 warning 唔好靜靜哋空數據）
