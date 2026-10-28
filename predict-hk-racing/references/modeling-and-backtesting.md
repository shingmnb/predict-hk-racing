# 可選概率模型、回測與修正

## 先分清情報與概率

`analyze` 係預設及核心輸出：事實、原文、支持、反證、未知及部署支持度，不含勝出率。只有用戶需要概率、資料足夠而且有明確模型時，先使用本文件。

`predict_card` 必須收到已訓練模型；`predict --model-version auto` 只揀最新 promoted。冇 promoted 會報錯並提示返回 `analyze`，唔會用賠率、均勻值或人手權重補一組概率。原始賠率可以保存作市場消息，但唔係模型特徵。

## 候選模型與內部對照

候選模型用 conditional logit（每場 softmax）學習馬匹相對效用，全場勝出概率合計為 1。L2 正則化、中位數填補、標準化參數全部由訓練集計算並存入模型版本。

位置概率用同一組勝出效用作 Plackett–Luce 頭二／頭三近似，唔係獨立位置模型；有需要時另建位置模型並單獨校準。輸出保留模型概率同當時原始賠率，但唔附公平倍數欄。

回測使用 `uniform-baseline`／`uniform-no-information-v1`：每匹 active 馬同一概率，只係**均勻無資訊內部對照**。佢唔係賽日預測，唔可以用 `predict` 調用，亦唔代表情報結論。

## 資訊增益 Δf²

McFadden pseudo-R² 量度模型相對均勻無資訊對照嘅解釋力：

```python
from racing_model import pseudo_r2, information_gain_vs_uniform

f2 = pseudo_r2(rows)
gain = information_gain_vs_uniform(candidate_rows, baseline_rows, samples=1000)
gain["delta"], gain["ci95"]
```

- `f² = 0`：同均勻猜測一樣；`f² = 1`：完美。
- `delta = candidate_pseudo_r2 - uniform_pseudo_r2`；輸入必須按同一 `race_id` 順序逐場對齊，bootstrap 以場次為單位配對重抽。
- 每個賽事 row 必須有 `runner_count`，因為無資訊對照嘅 log loss 係 `log(出賽馬數)`。
- 推廣 gate 預設只報告 Δf²；加 `--require-information-gain` 先將 `Δf² > 0` 變成硬性條件。
- Δf² 與 log loss 差係同一批數據嘅單調變換，唔係第二項獨立證據，更唔等於 ROI。

## 特徵與防滲漏

可用例子：`intent_centered`、`rating`、`draw`、`weight_lb`、`career_place_rate`、`cd_place_rate`、`recent_finish_mean`、`days_since_run`。原始數值先交模型學方向；唔好因常識預設「輕磅一定好」或「外檔一定差」。

- 每個特徵必須喺該次 `data_cutoff` 前已存在，並可按快照重現。
- 情報 claim 要保存逐字 quote、來源、反證及未知；`intent_centered` 只係候選特徵，唔係已證明優勢。
- 速度分要先校正場地、路程、班次、當日跑道偏差；唔好直接比較唔同路程 raw time。
- 比率要保留分子、分母或最少樣本數。小樣本要收縮或降權，唔好將 1 戰 1 勝當穩定 100%。
- 退出馬唔入該次有效場次概率；重新正常化其餘馬匹。
- 嚴禁正式名次、派彩、賽後評語、賽後段速或開跑後賠率進入賽前特徵。
- 訓練、校準、選特徵、調參只用當時可用嘅過去資料，唔好隨機抽場次做普通 train/test split。
- 歷史重建快照要標記；未能證明資料當時存在，就唔算嚴格實時回測。

## 評估指標

- **Log loss**：每場 `−log(頭馬賽前概率)`，越低越好；對過度自信懲罰大，係主要指標。
- **Multiclass Brier**：每場對全馬 `Σ(predicted − outcome)²` 取平均，越低越好。
- **校準**：按預測概率分組，比較平均預測與實際勝率；同時報樣本數。
- **Top-1／Top-3**：只作易懂輔助，唔可以取代概率質量。
- **位置 Brier**：評估位置概率，但要注明 Plackett–Luce 近似限制。
- **投注回報**：只有預先記錄注項、賠率、注碼同全部成本先計；ROI 唔作唯一升級標準。

`analyze` 冇概率欄，`metric_row`、`evaluate` 同校準漂移會跳過呢啲紀錄；唔好將部署支持度當勝率計 KPI。

## 向前滾動回測

`backtest` 用 expanding-window walk-forward：

1. 按開跑時間排序。
2. 第 N 場只用第 1 至 N−1 場訓練，預測第 N 場。
3. 每 `refit_every` 場重訓；重用期間模型仍然只見過較早賽事。
4. 同場比較候選模型、均勻無資訊基準；有現任 champion 時，用同樣向前方式重訓 champion 配置。
5. 對每場 log loss 差做 race-level bootstrap，報 95% 區間。負數代表候選較基準好；跨 0 即改善未明朗。
6. 保存每場指標、訓練窗口、特徵、參數、模型版本同報告，唔好只留一句「回測好好」。

並聯 dead heat 或無法確定單一頭馬嘅場次會喺訓練／主要指標排除並記錄。

## 模型修正門檻

每次賽後先評估，唔係每輸一場就改模型。累積新資料後：

1. 用新版本名稱 `fit` 候選模型，舊版本永不覆寫。
2. 跑意圖 ablation：同一特徵集合加入／移除 `intent_centered`，各自 walk-forward。
3. 檢查數據錯、特徵錯、校準錯同隨機波動；唔好由單場爆冷創新規則。
4. `promote` 只接受同時符合以下條件嘅版本：
   - 測試場數達到 `min_test_races`；
   - log loss 優於均勻基準，Brier 唔差過均勻基準；
   - 對均勻基準 log loss 改善嘅原始 bootstrap 95% 區間上端低於 0，Bonferroni 調整後區間上端亦低於 0；
   - 有現任 champion 時，亦要優於 champion，且原始同調整後區間上端都低於 0；
   - 要求 information gain 時，原始同調整後區間都要排除 0；
   - 每個 required block 都要有完整配對 ablation，原始同調整後區間上端都低於 0；證據必須對齊 source model hash、窗口、champion、候選家族同 alpha；
   - 入閘 comparison 嘅 bootstrap 解析度必須足夠，否則 adjusted CI 留空並拒絕晉升；
   - 閘門證據必須符合 Bonferroni 家族政策；候選家族改變會標 stale 並要求重跑。
5. 未通過就記錄 `rejected`，繼續舊版；冇舊版就只用 `analyze`。將新想法列為候選假設，唔好即場改投注規則。
6. 通過後先記錄 `promoted`；`predict --model-version auto` 只會用最新 promoted。

## 機率校準層（isotonic / Platt）

校準層負責把 raw model probability 映射到實際命中率，唔會將賠率變成概率。

```python
from racing_model import isotonic_fit, isotonic_apply, platt_fit, platt_apply, expected_calibration_error

model = isotonic_fit(post_fit_probs, post_fit_labels)
p_cal = isotonic_apply(model, raw_prob)
platt = platt_fit(post_fit_probs, post_fit_labels)
p_cal = platt_apply(platt, raw_prob)
ece = expected_calibration_error(list(zip(later_holdout_probs, later_holdout_labels)))
```

**校準分數必須對部署 source model 屬時間外樣本。** `calibrate` 固定未校準 source model，只用 `meeting_date >= train_before` 嘅已完成場次；較早場次擬合，最後 25%（至少一場）留作獨立 holdout。套用時逐場處理全場概率再 normalise。

**評估對象必須等於預測對象。** `backtest`／`value-backtest` 對有 `calibration` 嘅版本逐窗套用校準層，並用 `calibration.through` 擋住重疊測試窗；`promote` 只接受 source model hash 相同、校準已套用且 through 一致嘅證據。

- `isotonic`：非參數、樣本多時較準；小樣本容易過擬合。
- `platt`：兩個參數、小樣本較穩定，但只能修正單調壓縮。
- `ece`：10 等寬 bin 嘅加權 |confidence − accuracy|，必須附樣本數。
- 校準器本身也要驗收：holdout ECE 變差預設拒絕；`--method auto` 少於 500 個 fit points 用 Platt；輸出 clamp 到 `[0.001, 0.99]` 再 normalise。
- `calibration-drift` 按月或逐賽日檢查之後嘅 ECE；樣本不足只作參考。

## Early stopping

```python
model = fit_model(
    records, features,
    epochs=300,
    validation_fraction=0.25,
    early_stopping_patience=15,
    min_epochs=20,
)
```

hold-out 一定係時間上最後嘅賽事；停止後還原最佳 epoch 係數。預設 `validation_fraction=0.0`，唔會自動切 hold-out。

## Q／PQ 聯合機率（可選模型輸出）

`pair_probabilities(win_probs, slots=2|3)` 用同一組 Plackett–Luce 效用計成對聯合機率，供連贏同位置Q研究：

- 連贏：`P(i,j) = p_i p_j/(1-p_i) + p_j p_i/(1-p_j)`。
- 位置Q：對所有第三匹 `k`，加總 `{i,j,k}` 六個排列嘅 PL 機率。
- 呢啲仍係模型近似，未計步速互動、檔位碰撞等相關性；唔係派彩保證。

## 注碼層（只供已驗證概率）

預測概率唔等於投注。`scripts/staking.py` 係純函數模組；只有模型已通過時間外驗證，先可將概率同實際可成交原始賠率用於 `edge = p × odds − 1` 或 fractional Kelly。

- Kelly 會放大概率估計誤差；必須設單注、同場及彩池上限。
- `value-backtest` 只接受實際模型版本，唔接受內部基準名稱。
- 結算優先用官方派彩，報 turnover、ROI、strike rate、max drawdown、期末 bankroll 同不確定區間。
- 冇已驗證模型時，情報支持度唔可以代入注碼公式。

## 解讀限制

歷史績效唔保證未來；賽制、馬房、騎師、場地同市場效率會漂移。概率高唔等於值博，情報支持高亦唔等於必勝。輸出整體不確定性同資料缺口；唔好講「穩贏」「必中」「內幕」。
