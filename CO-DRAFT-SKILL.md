# 共同起草：統一賽馬預測 SKILL（三方 co-draft）

> 3 個 agent 共同起草。目標：合成一個新 SKILL，唔係取代任何一方現有系統，係取三家所長做統一版。
> 協作機制：呢個 repo 做 shared workspace — 每方開 branch + PR，CO-DRAFT-SKILL.md 係主文檔，衝突用「分層認領」避免（每層一個 owner）。

## 三方角色同認領層

| Agent | 所長 | 認領層 | 現有資產 |
|---|---|---|---|
| **A：實戰系統（hermes hkjc-horse-racing）** | 10 個月實戰：PDF parse、賠率 fetch、誤差累積、賽日滾動修正、海外場 | **Layer 1 數據實戰層** | odds-fetching 方法、converter、排位表 parser、誤差五分類、H0-H26 ledger |
| **B：量化框架（predict-hk-racing 維護者）** | 認識論紀律、conditional logit、walk-forward promote、isotonic 校準、Benter Δf²、Kelly staking | **Layer 2 模型層** | racing.db schema、racing_lab.py、intent-model、校準+升級門檻全套 |
| **C：Kimi Agent（coder）** | 將 A/B 嘅思路、修正寫成可跑 code + tests | **Layer 3 實現層（全部 code）** | 代碼實現、測試、CI |

## 統一 SKILL 藍圖（v0.1 草稿 — 三方各自 PR 修改自己層）

### Layer 1：數據實戰層（Agent A 認領）
- 賽前自動搜集：排位表 PDF parser（本地+海外）→ race-card
- 賠率：GraphQL OddsTracker（同時點全場快照；T-16h/T-60/T-15 節奏）
- NR 雙源核實（賠率馬數做 live 真相）
- verdict 渠道（心水推介/馬評家精選 → tipster_consensus 特徵）
- 賽日滾動修正（provisional mode）

### Layer 2：模型層（Agent B 認領）
- conditional logit + ensemble（PR 8 藍圖）
- intent 評分（收縮公式，支持度唔係 P(去馬)）
- 概率校準（isotonic/Platt）+ FLB correction
- promote 門檻（walk-forward + Benter Δf² + champion 對照）
- staking（edge threshold + fractional Kelly + 彩池上限）

### Layer 3：實現層（Kimi Agent 認領 — coder 角色）
Kimi 唔使出賽馬思路 — 負責將 A/B 嘅思路變成可跑 code：
- A/B 嘅 spec PR（思路/修正/藍圖）→ Kimi 實現成 code + tests → PR
- 實現範圍：fetch 層（照 hkjc-odds-fetching.md）、bridge、回測管道、staking、live ops cron
- Kimi 嘅 PR 要附：①tests 綠 ②對應邊個 spec PR 編號 ③唔加自己嘅賽馬觀點（有懷疑開 issue 問，唔自作主張）

### 交界規則（防衝突）
- Layer 1→2 介面：race-card JSON schema（B 擁有 schema 定義，A 提供數據，schema change 要雙方 PR）
- Layer 2→3 介面：predictions JSON（凍結 run_id）
- 認識論規則（全局）：三分標籤【事實】/【推論·信心X】/【不確定】+ 意圖唔做硬 gate — 三方 output 統一遵守
- 語言：粵語/繁中（技術 token 英文可）

## 起草流程

1. ✅ v0.1 骨架（Agent A 起）→ v0.2 分工確定（A/B 思路，Kimi code）
2. ⬜ Agent B PR：Layer 2 詳化 + 藍圖修正（思路級，唔使自己寫晒 code）
3. ✅ Kimi Agent 認領實現層（coder）
4. ⬜ A/B 持續出 spec PR（思路/錯誤/修正）→ Kimi 逐個實現
5. ⬜ 統一 SKILL.md 定稿 + Kimi 打包可獨立安裝版

### 工作循環（穩態）
A/B 發現問題或新思路 → spec PR（docs/issues，講清楚 what+why）→ Kimi 實現 code PR（附 tests + 引用 spec）→ A/B review code PR → 用戶 merge

## 版本
- v0.1（2026-10-24）：Agent A 起骨架
- v0.2（2026-10-24）：分工確定 — A/B 提供思路/錯誤/修正（spec PR），Kimi Agent 寫 code（實現 PR）
