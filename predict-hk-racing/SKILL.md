---
name: predict-hk-racing
description: >-
  香港賽馬（HKJC）數據驅動預測、馬房部署／去馬意圖評估、臨場概率更新、賽後結算、回測校準及版本化學習。用於用戶要求自動搜集排位、往績、段速、試閘操練、配備、傷患、騎練或賠率資料；分析沙田／跑馬地賽事、揀馬或比較馬匹；判斷練馬師、騎師、馬房「今場會否去馬」；建立勝出／位置概率；或要求複盤、記錄、回測及修正預測。亦適用於「今日點買」「邊隻做膽」「幫我睇形勢」「賽後檢討模型」。將意圖視為有證據的不確定推論，唔當內幕或保證。
---

# 專業香港賽馬預測與學習

執行「自動搜資料 → 建賽前快照 → 分析能力／形勢／意圖 → 輸出概率 → 凍結版本 → 賽後評估 → 向前回測 → 有門檻修正」嘅完整閉環。

## 不可妥協

1. 先核實再分析。馬號、馬名、場次、退出、賠率同時間以當時可靠來源為準；唔好憑記憶補即時資料。
2. 分開「觀察事實」「模型推論」「未知」。每項去馬判斷附逐字 `quote`、來源、反證、置信度；冇逐字原文就當「證據不足」，唔好捏造內幕或指控不盡力。
3. 唔好用賽果反推真實意圖。只可用時間外數據檢驗意圖特徵有冇預測價值。
4. 保存每次預測。臨場變化用新快照、新 `run_id`；賽後唔好改舊概率、舊理由或舊模型。
5. 未夠樣本就退回市場去水基準加定性分析，明講未有已驗證優勢；唔好自創精準權重。
6. 唔好講「穩贏」「必中」。冇優勢或資料不足就建議觀望；用戶要求投注分析時先講注項、成本及上限，唔好鼓勵追輸。

## 按需讀取資源

- 搜集、核實及保存資料前，讀 [data-collection.md](references/data-collection.md)。
- 評估「去馬／保留／備戰」前，讀 [intent-model.md](references/intent-model.md)。
- 建概率、訓練、回測或修正模型前，讀 [modeling-and-backtesting.md](references/modeling-and-backtesting.md)。
- 建立 JSON、執行腳本或解讀資料庫前，讀 [schemas-and-cli.md](references/schemas-and-cli.md)。
- 用 `assets/race-card-template.json`、`assets/race-result-template.json` 複製輸入模板；唔好直接改模板當用戶紀錄。

將本目錄記為 `<SKILL>`；將用戶數據放當前工作區 `racing-data/`，唔好寫入 Skill 安裝目錄。

## 賽前標準流程

### 1. 鎖定任務與截止時間

確認賽馬日、馬場、場次、開跑時間、分析時點同用戶需要。資料足以唯一確定時自動搜；有歧義先問。分清「賽前初版」「臨場更新」「歷史回測」，唔好混為一談。

### 2. 自動搜集與交叉核實

優先搵馬會官方排位、往績、評語、段速、試閘／操練、配備、退出／獸醫、場地、騎練及賠率；再搵直接訪問同可靠補充來源。記錄 URL、發布／擷取時間、原文摘錄及來源層級。

至少完成：
- 全場馬號、馬名、騎練、檔位、負磅、評分、配備、退出狀態；
- 同場同程、近績、班次／路程／場地轉變、休息日數、可用段速；
- 跑法對碰、預計步速、檔位及場地偏差；
- 意圖正面證據、相反證據同資料缺口；
- 臨場任務的全場同時點賠率。

網站受限或資料缺失時如實降級，請用戶補 PDF／截圖；唔好繞過限制或猜數。

### 3. 建立不可覆寫快照

按模板建立 race-card JSON，設 `collection_mode: live`；歷史重建設 `historical`。確保 `data_cutoff <= off_time`，每項意圖證據有有效 `source_id`。

```bash
python "<SKILL>/scripts/racing_lab.py" init --db "racing-data/racing.db"
python "<SKILL>/scripts/racing_lab.py" import-card --db "racing-data/racing.db" --file "racing-data/cards/<race_id>.json"
```

每次更新另存新 JSON 再 import。原始網頁／PDF 可存 `racing-data/sources/<race_id>/`。

### 4. 分開四個判斷層

1. **能力與狀態**：往績、班數、速度、體能、健康、適性；比率附樣本數。
2. **比賽形勢**：步速、跑位、檔位、場地、對手及可能劇本；至少想一個相反劇本。
3. **人馬去馬意圖**：按意圖參考文件評分，輸出支持度而唔係 `P(去馬)`；冇證據就「證據不足」。
4. **市場**：用同時點全場賠率計去水基準；落飛只屬市場支持，唔好重複當獨立意圖證據。

唔好將名騎、配備、落飛或熱廄任何單一訊號當定局。搵到相反證據要列出，唔好只砌支持自己心水嘅材料。

### 5. 計概率並凍結

```bash
python "<SKILL>/scripts/racing_lab.py" predict \
  --db "racing-data/racing.db" --race-id "<race_id>" \
  --model-version auto --output "racing-data/predictions/<race_id>.json"
```

`auto` 用最新已通過門檻嘅 promoted 模型；未有任何 promoted 就用市場基準。需要指定版本時用確實版本名。檢查全場勝出概率合計約 1、退出馬已排除、資料截止時間正確、警告已呈現。

### 6. 輸出可審計結論

先講資料時點、模型版本同限制，再輸出：

| 馬號／馬名 | 勝出概率 | 位置概率 | 公平倍數 | 當時賠率 | 去馬支持度／置信 | 核心理據 |
|---|---:|---:|---:|---:|---|---|

- 概率用一位小數百分比，公平倍數用合理小數；唔好製造多餘精度。
- 跟住列主要馬匹嘅「支持去馬／反對去馬／未知」三欄證據，並附實際來源引用。
- 補充步速主劇本、替代劇本、退出／場地突變點及最大資料缺口。
- 市場基準模式下講明公平倍數只係去水換算，唔代表有值博率或模型優勢。
- 用戶冇要求注項時唔好硬塞投注組合；有要求時先列全部成本、預設上限及「可唔買」選項。

## 臨場更新

重新核對退出、場地、騎師／配備變更及全場賠率時間戳。新增 card snapshot 與 prediction run，列出「相對上一版邊匹升、邊匹跌、由邊項新資料驅動」。唔好刪舊版，唔好將中途賠率噪音當確定部署。

用戶要求持續監察、指定時間更新或賽後自動複盤時，先另外建立合適自動化；一般單次分析唔好自行排程。

## 賽後記錄與評估

1. 用官方賽果新增 result snapshot，執行 `import-result`。
2. 執行 `evaluate`，保存 log loss、multiclass Brier、校準、Top-1／Top-3、位置 Brier 及每場明細。
3. 對照當時預測，分開檢討：資料錯漏、特徵定義、形勢假設、意圖證據、概率校準、不可預測事件。`evaluate` 預設只計賽前預測；歷史重建要另外用 `--kinds historical-reconstruction`，唔好撈埋一齊講 KPI。
4. 唔好只記「中／唔中」，亦唔好因一場輸贏改規則。評估輸出預設每場每模型取最新凍結預測；比較不同時點時分開報告，避免重複當獨立樣本。
5. 若有用戶預先記錄嘅注項，另計全部成本及回報；唔好賽後揀中過嘅注項砌 ROI。

## 回測與修正

1. 累積有官方結果嘅賽前快照；檢查歷史資料能否證明當時已存在。
2. 用新 `model_version` 執行 `fit`，訓練截止日之後嘅賽事唔准進入訓練。
3. 執行 `backtest`，以 expanding-window walk-forward 對照市場及現任 champion；保留 bootstrap 不確定區間。
4. 對 `intent_centered` 做同窗口 ablation，判斷意圖特徵是否真正增加時間外預測力。
5. 執行 `promote`。只有測試場數、log loss、Brier、bootstrap 區間及 champion 比較全部通過先會 promoted；否則自動記 rejected，繼續舊版。
6. 將新規則先標「候選假設」，記低適用範圍、樣本數、反例及下次檢查點。唔好為單場爆冷改門檻或製造事後規則。

完整命令及欄位遵循 [schemas-and-cli.md](references/schemas-and-cli.md)，指標與升級門檻遵循 [modeling-and-backtesting.md](references/modeling-and-backtesting.md)。

## 校準與特徵塊

有足夠 out-of-fold 樣本就跑 `calibrate`（用新版本名，例如 `-cal`）；`predict` 會自動套用並在輸出報 `calibration`。特徵塊分 required／optional：只有 required 要 ablation 通過；optional 未通過就由最終特徵集剔除，唔好因此否決整個模型。

## 價值與選擇性使用

有官方派彩就一定要跑 `value-backtest`：報 ROI、turnover、max drawdown 同注碼政策，唔好只報 log loss。Kelly 會放大概率估計誤差；ROI 要由時間外派彩結算證明，唔好預設正回報，亦唔好將虧損修飾成「接近打和」。
只買部分場次就用 `evaluate --coverage` 睇 selective-use：高分場次的表現在往同全場不同，甚至會反轉。

## 最後自檢

交付前核對：場次與馬號無錯、來源真係讀過、賠率同資料截止時間一致、概率無重複或遺漏、意圖無冒充事實、預測已凍結、回測無未來資料、修正無繞過門檻、價值報告有報成本同 drawdown。最後附一句：賽馬高方差，概率唔係保證；只以可承受損失作娛樂預算，唔追輸。
