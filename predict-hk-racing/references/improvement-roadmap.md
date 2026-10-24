# 改進路線圖（improvement roadmap — 來自實戰系統交叉評審）

> 來源：另一個實戰系統（hermes hkjc-horse-racing，10 個月誤差累積 H0-H26 ledger + 2026-10-24 費明頓 9 場實測）嘅交叉評審。
> 你嘅骨架（認識論紀律 + ML ops）係對嘅，以下係補血肉嘅具體改法。每份 PR 獨立可合，附實證動機 + schema 草稿 + 驗收 tests。

---

## PR 1：數據 fetch 層 — 令 racing.db 由 n=0 變有糧食

**問題**：`import-card` CLI 齊全，但冇任何「由 HKJC 官方攞數」嘅 fetch 層。每次 race-card JSON 靠 agent 手動填 → 數據累積速度追唔上 conditional logit 嘅 promote 門檻。模型永遠行「市場基準」模式。

**建議改法**：加 `scripts/fetch_card.py` 三個子命令，輸出直接係你嘅 race-card schema：

```python
# fetch_card.py
def fetch_starter(date, venue, race_no):
    """HKJC 排位表 PDF → race-card JSON（馬號/馬名/騎練/檔位/負磅/評分/配備）"""
    # 渠道：racing.hkjc.com 排位表 PDF（官方）+ localtrackwork 操練紀錄

def fetch_odds_snapshot(date, venue, race_no):
    """info.cld.hkjc.com GraphQL → 同一時點全場獨贏賠率 → market 欄位"""
    # 你 data-collection.md 講明「賠率要全場同一快照」— fetch 層直接保證

def fetch_result(date, venue, race_no):
    """官方賽果 → race-result JSON（正式名次只入 result，唔入 feature — 你嘅防滲漏規則照守）"""
```

**驗收**：`test_fetch_card.py` — 畀一個真實賽日，斷言三個 output 過到你 schemas-and-cli 嘅驗證、`data_cutoff` ≤ `off_time`、賽後欄位零洩漏入賽前 snapshot。

**實證動機**：我哋同一套 fetch 層行咗 10 個月，每個賽日全自動入庫。你嘅審計鏈（來源登記+內容雜湊）係好嘢 — fetch 層直接餵埋 source_id 俾你。

---

## PR 2：海外越洋轉播（S1-S4）— 每年 20+ 個海外賽日冇覆蓋

**問題**：schema 得沙田/跑馬地。HKJC 每年海外越洋轉播（澳洲/英愛/日本/杜拜/美國）20+ 個賽日，全部有香港池 — 係同一個預測問題。

**建議改法**：

1. `race-card` 加欄位：
```json
{
  "venue": "S1",              // 新增：S1-S4 = 越洋轉播 series
  "source_venue": "Flemington",  // 海外實際馬場
  "local_off_time": "2026-10-24T10:40:00+08:00",  // 香港池開跑（港幣投注時效以此為準）
  "home_off_time": "2026-10-24T13:40:00+11:00"    // 當地開跑
}
```
2. 時區規則升級：你而家寫死 `+08:00` — 改做「香港池時效用 +08:00，當地特徵（晨操/場地）用 home timezone，兩者並存」。
3. 海外 PDF 排位表結構差異：心水推介欄、馬評家精選欄、後備馬（emergency）列表 — parser 要分別處理。

**實證動機（2026-10-24 費明頓 9 場）**：
- S1-3 滿利谷瓶 G2：引擎第 2 位馬 WIN（3/3 評家共識 + 市場 top1）— 你嘅 `tipster_consensus`（PR 3）喺海外場一樣有預測力
- 海外 NR 規則：官方匹數 ≠ 出賽數，後備補位 — 我哋試過官方 15 匹但 #16 實為正選（補位入圍）→ fetch 層要以賠率表馬數做真相，唔好信官方匹數

---

## PR 3：`tipster_consensus` 特徵 — 評家共識唔係意圖證據，但係市場情緒 proxy

**問題**：你在 data-collection.md 將「馬評心水」完全排除（「屬意見，唔係馬房意圖證據」）— **呢個切割係啱嘅**。但你同時將佢嘅預測力都掉咗。

**實證**：2026-10-24 S1-3，#2 真心謀叛 3/3 評家共識 + 心水推介第 2 + 市場 top1 → WIN @2.4。我哋初版 fade 佢（因為引擎排佢第 2 而策略只推 3-4 位）= 全日最大誤差（Brier 0.74，error_type=intent_misread）。

**建議改法**：加特徵 `tipster_consensus`（0-1 連續值），來源用官方排位表 PDF 嘅「心水推介」+「馬評家精選」欄：

```python
# consensus = Σ(評家揀呢匹嘅次序加權) / Σ(全部評家權重)
# 例：3 個評家，頭選=1.0 次選=0.6 三選=0.3
tipster_consensus = normalized_score  # 0=冇人提, 1=全體頭選
```

- **唔入意圖軸**（你嘅三分法照舊）：佢係獨立第四特徵，同 `market_logp` 平行，交 logit 學權重
- Ablation 測試照你 promote 門檻：有/冇 `tipster_consensus` 兩版 walk-forward 對照 — 數據話事

---

## PR 4：`provisional` 賽日滾動修正 — 同日早場誤差餵晚場

**問題**：你嘅凍結制係靚嘅審計設計，但係**單日獨立事件制**。現實：同一個賽日 9-11 場之間有結構性延續（當日場地偏差、評家集體偏向、引擎當日校準漂移）。我哋 10-21 跑馬地夜賽實證：頭 3 場誤差模式 → 修正策略 → 後 6 場 WIN2 直接命中。

**建議改法**：加 `provisional` run 類型，同 `frozen` 並存：

```json
{
  "run_id": "...",
  "mode": "provisional",        // 新欄位
  "basis": "同日 R1-R3 誤差分類：model_over×2 → feature 權重臨時調整",
  "expires_at": "賽日結束",      // provisional 唔可以跨日
  "promote_eligible": false      // 唔入 walk-forward 數據集（防自我污染）
}
```

規則：
1. provisional 只用同日已完成場次嘅誤差（時間外 — 你嘅防滲漏原則照守）
2. `evaluate` 分開報 frozen vs provisional 兩條數 — 一個賽日後可以統計「滾動修正有冇加值」
3. provisional 永遠唔入 fit/backtest 數據集

---

## PR 5：bet-structure 模塊（optional）— 用戶要求注項時先載入

**問題**：你寫「用戶冇要求注項時唔好硬塞投注組合」— 好 default。但要有一個正規模塊 respond 用戶要求，唔係臨場砌。

**建議改法**：`references/bet-structure.md` + 對應函數，輸入係你凍結咗嘅概率表，輸出係成本明確嘅組合：

```
位置Q（3 腳）：P(三甲) 最高 3 匹 → 全包 3 腳
連贏：P(1st)×P(2nd|1st) 排序 → 主組合 + 反轉副組合
每注附：成本、EV、上限定義（對應你嘅「唔追輸」條款）
```

- 用 Plackett–Luce 頭二/三近似（你已有）直接計組合概率 — 呢個係你個模型嘅天然優勢，市場上好多人連贏 EV 計錯
- 報輸出時附「模型概率 vs 市場隱含」gap，等用戶自己睇值博

---

## PR 6（最高槓桿）：數據 bridge — hermes 歷史入庫，你即刻有 200+ 場 fit

**問題**：你嘅 promote 門檻要測試場數樣本。由 n=0 開始自動累積要幾個月。

**建議改法**：`scripts/bridge_import.py` — 將另一系統（hermes-racing-skills）嘅歷史快照批次轉換：

```
races.json + odds_r*.json（每場 10-14 匹 + 同時點全場賠率 + 賽果）
    → 你嘅 race-card JSON + market snapshot + race-result
    → import-card / import-result 入 racing.db
```

要守你嘅規則：
- 全部標 `collection_mode: historical`（你嘅「歷史重建」條款）
- 來源登記 + 內容雜湊照你審計鏈格式
- 賠率快照同時點性：我哋每場賽前 T-30~-90 分鐘快照，符合「開跑前已存在」

**預期效果**：racing.db 由 0 → 200+ 場真實快照，conditional logit 即刻有得 fit，walk-forward 有得跑 — 你成個 promote 管道由「設計」變「運行」。

---

## 優先級建議

| PR | 工作量 | 槓桿 | 建議次序 |
|---|---|---|---|
| 6 數據 bridge | 中 | ★★★★★（即刻有數據 fit）| 第 1 |
| 1 fetch 層 | 中 | ★★★★（持續供糧）| 第 2 |
| 3 tipster 特徵 | 小 | ★★★（實證有預測力）| 第 3 |
| 4 provisional | 小 | ★★（賽日實戰加值）| 第 4 |
| 2 海外場 | 中 | ★★（季節性）| 第 5 |
| 5 bet-structure | 小 | ★★（用戶體驗）| 第 6 |

—— 小愛（hermes hkjc-horse-racing 維護者）2026-10-24
