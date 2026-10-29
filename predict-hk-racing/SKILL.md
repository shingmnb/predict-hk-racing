---
name: predict-hk-racing
description: >-
  香港賽馬（HKJC）情報分析、馬房部署／去馬意圖評估、10 倍以上半冷馬篩選、非熱門核心連贏（Q）／位置連贏（PQ）組合、臨場更新、賽後核查及版本化學習。用於自動搜集排位、往績、段速、試閘操練、配備、傷患、騎練或直接訪問；分析沙田／跑馬地賽事、揀馬、冷腳、Q／位置Q 配腳或比較馬匹；以賽前 2–3 小時全場賠率界定半冷資格，每場 0 或 1 匹核心並容許全日棄選；需要兩名子 Agent 分頭研究及獨立核查、凍結實際馬對、逐組官方派彩結算；判斷練馬師、騎師、馬房「今場會否去馬」；保存支持證據、反證、未知項及可審計結論；或要求複盤、記錄、回測及修正。原始賠率只屬市場消息及價位門檻，唔會換算成概率或估算派彩。將意圖視為有證據的不確定推論，唔當內幕或保證。
---

# 專業香港賽馬情報分析與驗證

執行「自動搜原件 → 建賽前快照 → 能力／形勢／部署分析 → 保存支持、反證及未知 → 可選原始賠率參考 → 凍結情報版本 → 賽後核查 → 時間外驗證 → 有門檻修正」嘅閉環。核心產物係可審計情報，唔係硬砌勝出率。

## 不可妥協

1. 先核實再分析。馬號、馬名、場次、退出、消息同時間以當時可靠來源為準；唔好憑記憶補即時資料。
2. **情報先行，賠率只作參考。**先形成基本面／消息短名單，再睇市場；唔好用賠率排名代替選馬分析，亦唔好將熱門或落飛當成馬房意圖。
3. 分開「觀察事實」「分析推論」「未知」。每項去馬判斷附逐字 `quote`、來源、反證、置信度；匿名傳聞標「未核實」，唔納入評分，唔好捏造內幕或指控不盡力。
4. 唔好用賽果反推真實意圖。只可核對事實、替代解釋及事先寫低嘅可觀察預期；意圖特徵有冇預測價值由時間外數據決定。
5. 保存每次分析。臨場變化用新快照、新 `run_id`；賽後唔好改舊理由、舊短名單、舊分數或舊模型。
6. **冇已驗證模型就只出情報分析。**唔好用賠率、均勻值或自創權重冒充勝出概率；資料不足就明講「證據不足」。
7. 唔好講「穩贏」「必中」。冇優勢或資料不足就建議觀望；用戶要求投注分析時先講注項、成本及上限，唔好鼓勵追輸。

## 按需讀取資源

- 搜集、核實及保存資料前，讀 [data-collection.md](references/data-collection.md)。
- 由零開始取得原件、臨場賠率、歷史樣本或截圖轉錄時，讀 [data-acquisition-guide.md](references/data-acquisition-guide.md)。
- 搜集消息、判斷邊條消息有用前，讀 [intel-sources.md](references/intel-sources.md)。
- 用真實歷史數據驗證模型、或想知 861 場實測結論前，讀 [real-data-validation.md](references/real-data-validation.md)。
- 評估「去馬／保留／備戰」前，讀 [intent-model.md](references/intent-model.md)。
- 篩選 10 倍以上半冷馬、記錄棄選或計半冷回測前，讀 [longshot.md](references/longshot.md)。
- 凍結非熱門核心 Q／位置 Q、執行三名子 Agent 隔離或逐組結算前，讀 [exotic.md](references/exotic.md)。
- 建概率、訓練、回測或修正模型前，讀 [modeling-and-backtesting.md](references/modeling-and-backtesting.md)。
- 建立 JSON、執行腳本或解讀資料庫前，讀 [schemas-and-cli.md](references/schemas-and-cli.md)。
- 想用 Obsidian 管理歷史數據、或由舊 notes 遷移，讀 [obsidian-history.md](references/obsidian-history.md)。
- 用 `assets/race-card-template.json`、`assets/race-result-template.json`、`assets/longshot-decision-template.json`、`assets/exotic-ticket-template.json` 複製輸入模板；唔好直接改模板當用戶紀錄。

將本目錄記為 `<SKILL>`；將用戶數據放當前工作區 `racing-data/`，唔好寫入 Skill 安裝目錄。

## 賽前標準流程

### 1. 鎖定任務與截止時間

確認賽馬日、馬場、場次、開跑時間、分析時點同用戶需要。資料足以唯一確定時自動搜；有歧義先問。分清「賽前初版」「臨場更新」「歷史回測」。

### 2. 自動搜集與交叉核實

先搵官方排位、逐馬往績、評語、段速、試閘／操練、配備、退出／獸醫、場地及騎練資料，再搵直接訪問、馬房公布同可靠補充來源；賠率放喺基本面記錄之後。每項記錄 URL、發布／擷取時間、原文摘錄及來源層級。

至少完成：
- 全場馬號、馬名、騎練、檔位、負磅、評分、配備、退出狀態；
- 同場同程、近績、班次／路程／場地轉變、休息日數、可用段速；
- 上仗／今仗配備及騎師對比、同倉選鞍、直接言論、試閘／操練同健康線索；
- 跑法對碰、預計步速、檔位及場地偏差；
- 每項重要 claim 嘅支持證據、相反解釋、未知及撤回條件；
- 有賠率時保存全場同一時點原始快照；冇賠率仍可完整交付情報分析。

網站受限或資料缺失時如實降級，請用戶補 PDF／截圖；唔好繞過限制或猜數。

### 3. 建立不可覆寫快照

按模板建立 race-card JSON，設 `collection_mode: live`；歷史重建設 `historical`。確保 `data_cutoff <= off_time`，每項意圖證據有有效 `source_id` 同逐字 `quote`。

```bash
python "<SKILL>/scripts/racing_lab.py" init --db "racing-data/racing.db"
python "<SKILL>/scripts/racing_lab.py" import-card --db "racing-data/racing.db" --file "racing-data/cards/<race_id>.json"
```

每次更新另存新 JSON 再 import。原始網頁／PDF 可存 `racing-data/sources/<race_id>/`。基本面分析另存短名單、支持／反證、`fundamental_created_at`、`market_checked_at` 及 `market_blind`；只有首次接觸賠率前已完成短名單先可記 `market_blind: true`。

### 4. 做三層情報分析

1. **能力與狀態**：往績、班數、速度、體能、健康、適性；比率附樣本數。
2. **比賽形勢**：步速、跑位、檔位、場地、對手及可能劇本；至少想一個相反劇本。
3. **人馬去馬意圖**：按意圖參考文件整理支持度，輸出支持／反對／未知，唔係 `P(去馬)`；冇證據就「證據不足」。

原始賠率只另列 `market_move` 觀察：記錄時間窗同變化，唔換算概率、唔入模型特徵、唔當獨立意圖證據。冇新非市場證據，唔好因熱門、冷門或落飛改寫基本面理由。

### 5. 凍結情報分析

```bash
python "<SKILL>/scripts/racing_lab.py" analyze \
  --db "racing-data/racing.db" --race-id "<race_id>" \
  --output "racing-data/analyses/<race_id>.json"
```

輸出逐匹保存能力欄位、部署支持度、支持證據、反證、中性觀察、未知項、原文與來源，以及可選原始 WIN／PLA 賠率。輸出唔包含勝出率、位置概率或公平倍數；部署分數係未校準嘅證據整理，唔係實戰勝率。

### 6. 輸出可審計結論

先講資料時點、`market_blind` 同限制，然後**先消息／基本面，後原始賠率參考**：

| 馬號／馬名 | 核心消息／能力證據 | 反證／風險 | 未知／待核 | 去馬支持度／置信 | 基本面取態 |
|---|---|---|---|---|---|

- 主要馬匹列「支持去馬／反對去馬／未知」三欄，附實際來源引用；再補步速主劇本、替代劇本及突變點。
- 原始賠率只另列快照與變化，唔加市場概率或公平倍數欄。
- 補充最大資料缺口。用戶冇要求注項時唔好硬塞投注組合；有要求時先列全部成本、預設上限及「可唔買」選項。

## 10 倍以上半冷馬專用分支

用戶要搵半冷馬時，先完成並凍結未見賠率嘅基本面分析，再攞最接近 T-2h（限 T-3h 至 T-2h）嘅全場 WIN 快照。逐匹審查全部合資格馬，每場只選 0 或 1 匹；冇兩類獨立證據（至少一類非市場）、反證、未知及撤回條件就棄選，唔好為中冷而硬揀。

複製 `assets/longshot-decision-template.json` 建立決策，執行 `longshot.py validate`；官方賽果後用 `longshot.py backtest` 保留棄選場次、對照合資格馬基率並計不確定區間。規則改動要開新 `rule_version`，只用新賽事驗證。詳細門檻、目錄及命令見 [longshot.md](references/longshot.md)，格式見 [schemas-and-cli.md](references/schemas-and-cli.md)。

用戶要 Q／位置 Q 時，A 負責能力與形勢、B 負責馬房與消息，C 喺完成前唔睇推薦，獨立核對原件及反證；共用來源只算一條證據鏈。只凍結兩馬各有非市場證據、共同入位機制、反證及撤回條件嘅實際馬對，列出全部成本；零組合即棄選。用 `exotic.py freeze` 綁定單馬決策及市場快照，賽後用 `exotic.py settle` 逐組讀官方派彩。非核心派彩照入現金帳但唔算目標命中；冇官方派彩紀錄唔准用賠率補算。見 [exotic.md](references/exotic.md)。

## 臨場更新

重新核對退出、場地、騎師／配備變更及新消息，新增 card snapshot 與 analysis run。分清「非市場新證據」同「只有賠率變化」：前者附原文同對短名單嘅影響；後者只更新市場觀察，唔好無新證據就改寫基本面取態。列出相對上一版邊項判斷改變及由邊項資料驅動；唔好刪舊版。

用戶要求持續監察、指定時間更新或賽後自動複盤時，先另外建立合適自動化；一般單次分析唔好自行排程。

## 賽後記錄與核查

1. 用官方賽果新增 result snapshot，執行 `import-result`。
2. 對照凍結情報，逐項核查：事實有冇錯、原文有冇斷章、反證有冇漏、形勢假設有冇出現、邊啲事件屬不可預測。
3. 唔好只記「中／唔中」，亦唔好將贏輸直接標成「意圖證實／推翻」。保存陰性結果同未解決分歧。
4. 只有真正概率預測先執行 `evaluate` 計 log loss、Brier、校準及命中率；`analyze` 紀錄唔會被當概率預測評分。
5. 若有用戶預先記錄嘅注項，另計全部成本及回報；唔好賽後揀中過嘅注項砌 ROI。

## 可選概率模型、回測與修正

情報分析唔需要先建模。只有累積足夠合格賽前快照、特徵同官方結果，而用戶明確需要概率時，先進入呢一步：

1. 用新 `model_version` 執行 `fit`；訓練截止日之後嘅賽事唔准進入訓練，原始賠率唔係特徵。
2. 執行 `backtest`，以 expanding-window walk-forward 對照**均勻無資訊基準**及現任 champion；呢個基準只作內部驗收，唔係賽日預測。
3. 對 `intent_centered` 做同窗口 ablation，判斷意圖特徵是否真正增加時間外預測力。
4. 執行 `promote`。測試場數、log loss、Brier、bootstrap 區間及 champion 比較全部通過先會 promoted；否則記 rejected。
5. `predict --model-version auto` 只使用最新 promoted；冇 promoted 會報錯並提示返回 `analyze`，唔會自動補一組概率。
6. 將新規則先標「候選假設」，記低適用範圍、樣本數、反例及下次檢查點。唔好為單場爆冷改門檻或製造事後規則。

完整命令及欄位遵循 [schemas-and-cli.md](references/schemas-and-cli.md)，指標與升級門檻遵循 [modeling-and-backtesting.md](references/modeling-and-backtesting.md)。

## 最後自檢

交付前核對：場次與馬號無錯、來源真係讀過、原文逐字相符、支持同反證並列、未知冇被當事實、意圖無冒充內幕、快照已凍結、賠率只作原始參考、冇模型時唔出概率、回測無未來資料、修正無繞過門檻。最後附一句：賽馬高方差，情報判斷同概率都唔係保證；只以可承受損失作娛樂預算，唔追輸。
