# 改進路線圖（improvement roadmap — 來自實戰系統交叉評審）

> 來源：另一個實戰系統（10 個月誤差累積 hypothesis ledger + 2026-10-24 費明頓 9 場實測）嘅交叉評審。
> 狀態：以下係候選藍圖。外部實戰結果、引用數同文獻摘要未喺本 repo 獨立重現或核實，唔好當成已驗證優勢。藍圖編號只係規劃 ID，唔對應 GitHub PR 編號。
> 每份藍圖都要獨立評審同驗收；實作時附 schema、時間順序測試同反證，唔代表可以全部直接合入。

---

## 藍圖 1：數據 fetch 層 — 令 racing.db 由 n=0 變有糧食

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

## 藍圖 2：海外越洋轉播（S1-S4）— 每年 20+ 個海外賽日冇覆蓋

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
- S1-3 滿利谷瓶 G2：引擎第 2 位馬 WIN（3/3 評家共識 + 市場 top1）— 你嘅 `tipster_consensus`（藍圖 3）喺海外場一樣有預測力
- 海外 NR 規則：官方匹數 ≠ 出賽數，後備補位 — 我哋試過官方 15 匹但 #16 實為正選（補位入圍）→ fetch 層要以賠率表馬數做真相，唔好信官方匹數

---

## 藍圖 3：`tipster_consensus` 特徵 — 評家共識唔係意圖證據，但係市場情緒 proxy

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

## 藍圖 4：`provisional` 賽日滾動修正 — 同日早場誤差餵晚場

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

## 藍圖 5：bet-structure 模塊（optional）— 用戶要求注項時先載入

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

## 藍圖 6（最高槓桿）：數據 bridge — 歷史快照批次入庫，即刻有 200+ 場 fit

**問題**：你嘅 promote 門檻要測試場數樣本。由 n=0 開始自動累積要幾個月。

**建議改法**：`scripts/bridge_import.py` — 將另一系統（racing-data-system）嘅歷史快照批次轉換：

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

| 藍圖 | 工作量 | 槓桿 | 建議次序 |
|---|---|---|---|
| 6 數據 bridge | 中 | ★★★★★（即刻有數據 fit）| 第 1 |
| 1 fetch 層 | 中 | ★★★★（持續供糧）| 第 2 |
| 3 tipster 特徵 | 小 | ★★★（實證有預測力）| 第 3 |
| 4 provisional | 小 | ★★（賽日實戰加值）| 第 4 |
| 2 海外場 | 中 | ★★（季節性）| 第 5 |
| 5 bet-structure | 小 | ★★（用戶體驗）| 第 6 |

—— 外部實戰系統維護者 2026-10-24

---

# 文獻參考與候選增量（2026-10-24 第二輪）

> 來源：外部評審提供嘅 OpenAlex 檢索摘要（market efficiency 經典系列 + 香港 ML 前例 + 2026 SOTA）。
> 呢啲只係研究線索，唔係「論文證明本 repo 啱」；藍圖 7-9 要核實原文，再用本 repo 數據做時間外驗證。

## 文獻線索（仍需核實原文及本地驗證）

| 你嘅設計 | 文獻線索 |
|---|---|
| 「落飛唔好當獨立意圖證據」 | **Camerer 1998**（JCR，cites 134）：$500/$1000 落注再取消實驗 — 賠率郁但快速回復，操縱效果 shallow 而短命 → 落飛訊號本身好弱 |
| 「市場基準做比較線」 | **Asch, Malkiel & Quandt 1981**（cites 248）：win pool 接近有效 — beat 市場唔係應有假設 |
| 「樣本不足退回市場基準」 | **Thaler & Ziemba 1988**（cites 621）：parimutuel 市場異象系統性存在但細 — 未有驗證優勢前市場基準係理性起點 |
| 凍結版本 + 時間外回測 | walk-forward 係量化共識；1984 Ziemba 模擬已用嚴格時間外樣本 |

## 藍圖 7：Place/Show pool 低效掃描器（候選假設）

**候選假設**：據外部摘要，**Asch, Malkiel & Quandt 1982「Racetrack betting and informed behavior」**（cites 150）+ **Ziemba 1984「Dr. Z」模擬**曾報告 place/show pool 低效。呢種低效喺香港是否存在、邊一類馬有正 EV，必須由本地時間外數據驗證。

**建議改法**：你已有 Plackett–Luce place 概率 — 加一個掃描函數：

```python
def place_value_scan(place_probs, place_odds_snapshot):
    """對每匹：P(place|PL) vs 1/place_odds 去水 → gap 排序
    觸發條件：gap > 歷史去水偏差 + 交易成本
    （takeout：以 HKJC 官方 place 池抽水率為準 — ≠ WIN 池 17.5%；
    T-60 實測時核實官方 takeout 表先寫入 constants，唔寫死喺掃描器。見 issue #22）"""
    # 重點掃描對象：市場 top1-3 大熱（Dr. Z 經典位）
```

- 需要 fetch 層加 place odds（同 WIN 快照同一時點 — 你嘅規則直接延伸）
- 掃描器可以同時檢驗「大熱 place 被擠壓」同「半冷 place 被低估」兩類假設，唔預設任何一類有效。

## 藍圖 8：Ensemble committee（香港前例 ★★★）

**證據**：**「A SVM-Based committee machine for prediction of Hong Kong horse racing」2017**（cites 11）— 3 年 HKJC 數據訓練 committee machine，香港場直接前例：**ensemble 優於單模型**。

**建議改法**：conditional logit（你已有）+ gradient boosting（LightGBM，處理非線性互動：檔位×路程×場地）兩路並行 → 簡單加權融合。你嘅 promote 門檻機制照守 — champion 對象由「市場基準」升級做「ensemble vs 單 logit vs 市場」三方。

## 藍圖 9：Favourite-longshot bias 校準層（經典異象 ★★★）

**證據**：**Thaler & Ziemba 1988** + **Sobel & Raines 1997**（cites 120）— parimutuel 市場 favourite-longshot bias 穩定存在：大熱真實勝率高過市場隱含、大冷低過隱含。

**建議改法**：模型概率輸出之後加一層 bias correction（唔係改特徵）：

```python
def flb_correction(p_market_normalized, flb_params):
    """p' = p * (1 + alpha * log(p/q_uniform))，alpha 由自己 walk-forward 數據學
    大熱微升、大冷微降 — 學到 alpha≈0 即無 bias（誠實輸出）"""
```

- 校準層參數 `flb_params` 入 model_version 凍結 — 同你現有審計制無縫
- 香港場 bias 強度同美英唔同（池大、職業賭客多）— 由自己數據學而唔抄文獻數值（你嘅「唔好人手加權重」原則照守）

## 時間線建議

| 階段 | 內容 | 觸發條件 |
|---|---|---|
| 而家 | 藍圖 6 bridge + 藍圖 1 fetch（數據地基）| 即刻 |
| n ≥ 100 場 | 藍圖 3 tipster + 藍圖 7 place pool 掃描 | 有特徵數據 |
| n ≥ 300 場 | 藍圖 8 ensemble + 藍圖 9 FLB 校準 | 樣本支持非線性 |

## 未採用嘅 SOTA（記錄在案）

- **2026 Transformer 賽果預測**：sequence model 馬匹狀態時間序列 — 需 500+ 場先唔會過拟合；而家採用會違反你自己嘅「未夠樣本唔好自創精準」原則
- **2018 graph-based features**：對手相遇圖 — 同場同程 stats 已覆蓋核心；n 大後可試
- **2001 going prediction**：場地預測 — 屬天氣模型，scope 之外

## 參考文獻（OpenAlex 檢索 2026-10-24）

1. Asch, Malkiel & Quandt (1981) Efficiency of the Market for Racetrack Betting — c:248
2. Asch, Malkiel & Quandt (1982) Racetrack betting and informed behavior — c:150
3. Ziemba (1984) Market Efficiency in Racetrack Betting (Dr. Z) — c:119
4. Thaler & Ziemba (1988) Anomalies: Parimutuel Betting Markets — c:621
5. Camerer (1998) Can Asset Markets Be Manipulated? — c:134
6. Sobel & Raines (1997) Why is There a Favourite-Longshot Bias — c:120
7. Lam et al. (2017) SVM-Based committee machine, HK racing — c:11
8.Transformer-Based Prediction (2026) — 新 SOTA，樣本門檻未到
