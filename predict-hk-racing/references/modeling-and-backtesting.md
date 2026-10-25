# 概率模型、回測與修正

## 目錄
- 兩層概率
- 特徵與防滲漏
- 評估指標
- 向前滾動回測
- 模型修正門檻
- 解讀限制

## 兩層概率

1. **市場基準**：全場獨贏賠率齊全時，先取 `1 / odds`，再除以全場合計去除簡單水位。呢個係比較基準，唔係模型優勢。
2. **候選模型**：用 conditional logit（每場 softmax）學習馬匹相對效用，全場勝出概率合計為 1。L2 正則化、中位數填補、標準化參數全部由訓練集計算並存入模型版本。

位置概率用同一組勝出效用作 Plackett–Luce 頭二／頭三近似。佢唔係獨立位置模型；有需要時另建位置模型並單獨校準。

未夠歷史樣本時，輸出市場基準、意圖證據及定性形勢，明確標示「未有已驗證模型優勢」。唔好用自創權重假裝精準 AI 概率。

## 資訊增益 Δf²（Benter 1994）

`logloss 好過市場` 只證明校準較好，唔證明有 edge。Benter 用 **McFadden pseudo-R²** 量度「解釋力」，再用差值量度模型比市場多知道幾多：

```python
from racing_model import pseudo_r2, information_gain_vs_market

f2 = pseudo_r2(rows)                     # 1 - mean_logloss / mean_log(runners)
gain = information_gain_vs_market(candidate_rows, market_rows, samples=1000)
gain["delta"], gain["ci95"]              # Δf² 同 95% bootstrap 區間
```

- `f² = 0`：同隨機均勻猜測一樣；`f² = 1`：完美。
- Benter 的實戰數字：公開賠率 0.1218、基本面模型 0.1245、兩者組合 0.1396 → **Δf² = 0.0178** 係額外解釋力指標，唔係保證可下注或正 ROI。
- `delta` 定義係 `candidate_pseudo_r2 - market_pseudo_r2`；輸入必須按同一 `race_id` 順序逐場對齊，bootstrap 亦以場次為單位配對重抽。
- 每個賽事 row 必須有 `runner_count`（`evaluate_race` 已加），因為 null model 的 log loss 係 `log(出賽馬數)`。
- 推廣 gate 預設只**報告** Δf²；加 `--require-information-gain` 才會把 `Δf² > 0` 變成一條硬性條件（保持舊行為）。

> 注意：Δf² 係相對市場的資訊優勢，唔等於 ROI。要賺錢仍然要過抽水 — 各池抽水唔同：WIN 池 17.5%（PR #13 拍板）；place/QIN/QPL 待 T-60 實測核實官方 takeout 表（issue #22），唔可以套用 WIN 數字。

> **Δf² 唔係獨立證據**：`pseudo_r2 = 1 − logloss / log(field)`，所以對同一批場次，
> `Δf² = (LL_market − LL_candidate) / mean(log field)`，分母恆正——每一個 bootstrap
> resample 內，Δf² 與 log loss 差**同符號**。即係話 `--require-information-gain` 同
> `logloss_improvement_adjusted_ci_excludes_zero` 實際上係同一個判斷（換了單位）。
> 要第二個獨立 gate，應該用正交指標：CLV、校準誤差、或 value-backtest 的 ROI CI。

## 機率校準層（isotonic / Platt）

**去水 ≠ 校準。** Shin 只係移除書商水位，模型輸出仍然可能系統性過度自信或不足。校準層負責把 raw probability 映射到實際命中率。

```python
from racing_model import isotonic_fit, isotonic_apply, platt_fit, platt_apply, expected_calibration_error

model = isotonic_fit(post_fit_probs, post_fit_labels)   # PAVA，單調非遞減
p_cal = isotonic_apply(model, raw_prob)

platt = platt_fit(post_fit_probs, post_fit_labels)      # sigmoid(a·logit(p)+b)
p_cal = platt_apply(platt, raw_prob)

ece = expected_calibration_error(list(zip(later_holdout_probs, later_holdout_labels)))
```

**最重要的紀律：校準分數必須對部署 source model 屬時間外樣本。** `calibrate` 固定未校準嘅 source model，只用 `meeting_date >= train_before` 嘅已完成場次；唔會 walk-forward refit 另一個 fold model，亦唔會退回訓練窗。窗後較早場次用嚟擬合，最後 25%（至少一場）留作較後 holdout，`ece_before`／`ece_after` 共用呢批獨立評估點，唔重用 fit points。套用校準時必須逐場處理全場概率再 normalise，唔可以逐匹單點套用。

**不變量：評估對象必須等於預測對象。** `backtest`／`value-backtest` 對有 `calibration` 的版本，會逐窗把校準層套到 refit 模型（報告 `calibration_applied: true`），並用 `calibration.through` 擋住與校準資料重疊的測試窗。否則 gate 批准的是未校準模型，實際輸出卻是校準版，兩者唔一致。

- `isotonic`：非參數、樣本多時較準；小樣本（<500 點）容易階梯化，建議配 `n ≥ 300`。
- `platt`：兩個參數、小樣本穩定，但只能修正單調壓縮，修正不了非單調偏差。
- `ece`：10 等寬 bin 的加權 |confidence − accuracy|；< 0.05 可接受。

## Early stopping（`fit_model`）

```python
model = fit_model(
    records, features,
    epochs=300,
    validation_fraction=0.25,      # 最後 25% 賽事做 hold-out（時間順序）
    early_stopping_patience=15,    # 15 個 epoch 無改善就停
    min_epochs=20,
)
model["training"]["best_epoch"], model["training"]["validation_logloss"]
```

- 預設 `validation_fraction=0.0`，行為同舊版完全一致（可重現）。
- hold-out 一定係**時間上最後**的賽事，唔可以隨機抽，否則泄漏未來。
- 停止後會還原最佳 epoch 的係數，唔係最後一個 epoch。
- `training` 新增：`validation_races`、`validation_logloss`、`best_epoch`、`stopped_early`、`validation_note`。

## De-vig 方法與 Q/PQ 聯合機率

`market_probabilities(runners, method=...)` 支援兩種去水方法：

- `normalized`（預設，向後兼容）：`p_i = (1/o_i) / Σ(1/o_j)`，將水位按比例攤分。
- `shin`（Shin 1993）：假設書商面對部分內幕交易者，解出令全場機率和為 1 的內幕比例 `z`。比例攤分會**高估冷門、低估大熱**（favourite–longshot bias）；Shin 會把機率**移向大熱**。實測（odds 1.8 / 3.5 / 7.0 / 10.0）大熱由 51.2% 升至 52.7%，最冷由 9.2% 降至 8.6%。

> 兩者都只係「去水」而唔係「校準」；輸出概率仍然要經賽後 Brier／校準檢查。

`pair_probabilities(win_probs, slots=2|3)` 用同一組 Plackett–Luce 效用計**成對聯合機率**，供連贏（`slots=2`）同位置Q（`slots=3`）使用：

- 連贏：`P(i,j) = p_i p_j/(1-p_i) + p_j p_i/(1-p_j)`
- 位置Q：對所有第三匹 `k`，加總 `{i,j,k}` 六個排列的 PL 機率
- 不變量（已寫入測試）：連贏全對機率和 = 1；位置Q `Σ_j P(i,j) = 2 × P(i 入三甲)`

留意：Q/PQ 聯合機率仍然係 PL 近似，未計步速互動、檔位碰撞等相關性；落注前要對實時彩池賠率。

## 特徵與防滲漏

可用例子：`market_logp`、`intent_centered`、`rating`、`draw`、`weight_lb`、`career_place_rate`、`cd_place_rate`、`recent_finish_mean`、`days_since_run`。原始數值先交模型學方向；唔好因常識預設「輕磅一定好」或「外檔一定差」。

- 每個特徵必須喺該次 `data_cutoff` 前已存在，並可按快照重現。
- 速度分要先校正場地、路程、班次、當日跑道偏差；唔好直接比較唔同路程 raw time。
- 比率要保留分子、分母或最少樣本數。小樣本要收縮或降權，唔好將 1 戰 1 勝當穩定 100%。
- 退出馬唔入該次有效場次概率；重新正常化其餘馬匹。
- 嚴禁正式名次、派彩、賽後評語、賽後段速或開跑後賠率進入賽前特徵。
- 訓練、校準、選特徵、調參只用當時可用嘅過去資料。唔好抽亂場次做普通 train/test split。
- 歷史重建快照要標記；未能證明資料當時存在，就唔算嚴格實時回測。

## 評估指標

- **Log loss**：每場 `−log(頭馬賽前概率)`，越低越好；對過度自信懲罰大，係主要指標。
- **Multiclass Brier**：每場對全馬 `Σ(predicted − outcome)²` 取平均，越低越好。唔好用「預測名次差距」冒充 Brier。
- **校準**：按預測概率分組，比較平均預測與實際勝率；同時報樣本數。
- **Top-1／Top-3**：只作易懂輔助，唔可以取代概率質量。
- **位置 Brier**：評估位置概率，但要注意 Plackett–Luce 近似限制。
- **投注回報**：只有預先記錄注項、賠率、注碼同全部成本先計；唔好賽後只揀中過嘅注項。ROI 波動大，唔作唯一升級標準。

## 向前滾動回測

`backtest` 用 expanding-window walk-forward：

1. 按開跑時間排序。
2. 第 N 場只用第 1 至 N−1 場訓練，預測第 N 場。
3. 每 `refit_every` 場重訓；重用期間模型仍然只見過較早賽事。
4. 同場同時比較候選模型、市場基準；有現任 champion 時，用同樣向前方式重訓 champion 配置作比較。
5. 對每場 log loss 差做 race-level bootstrap，報 95% 區間。負數代表候選較市場好；跨 0 即改善未明朗。
6. 保存每場指標、訓練窗口、特徵、參數、模型版本同報告，唔好只留一句「回測好好」。

並聯 dead heat 或無法確定單一頭馬嘅場次會喺訓練／主要指標排除並記錄，唔好偷偷改規則。

## 模型修正門檻

每次賽後先評估，唔係每輸一場就改模型。累積新資料後：

1. 用新版本名稱 `fit` 候選模型，舊版本永不覆寫。
2. 跑意圖 ablation：同一特徵集合加入／移除 `intent_centered`，各自 walk-forward。
3. 檢查數據錯、特徵錯、校準錯同隨機波動；唔好由單場爆冷創新規則。
4. `promote` 只接受同時符合以下條件嘅版本：
   - 測試場數達到 `min_test_races`（預設 50；只係最低門檻，可按数据量提高）；
   - log loss 優於市場基準；
   - Brier 唔差過市場基準；
   - 對市場 log loss 改善嘅原始 bootstrap 95% 區間上端低於 0，且 Bonferroni 調整後（`effective_alpha = 0.05 / candidates_in_family`）區間上端亦低於 0；
   - 有現任 champion 時，亦要優於 champion，且對 champion 改善嘅原始同調整後區間上端都低於 0；
   - 要求 information gain 時，`information_gain_ci95_excludes_zero` 成立，且調整後區間下端亦高於 0；
   - 每個 required block 都要有完整配對 ablation，原始同調整後區間上端都低於 0；`promote` 只會累積 source model hash、回測方法／窗口、champion、candidate／baseline／逐場數據、候選家族數同 effective alpha 全部一致嘅證據，並記錄每個 block 對應嘅 evaluation id。呢個係 per-block necessity，唔代表 joint necessity；optional block 只記錄未證明價值，要剔除就另 fit 新版本。舊模型冇 `required_blocks` key 時，保留「latest backtest 要有顯著 ablation」嘅舊閘門；
   - 用嚟入閘嘅 comparison（市場、champion、按需 Δf² 同 required ablation）bootstrap 解析度不足時，adjusted CI 必須留空並記錄 warning，候選版本不得晉升；optional ablation 唔會否決模型。要增加 `--bootstrap`，或喺睇結果前按登記準則收窄候選家族再重跑；
   - 閘門證據必須 `multiple_comparison_method = "bonferroni"`、`familywise_alpha = 0.05`、`effective_alpha` 同 0.05/n 一致；候選家族改變 → `multiple_comparison_stale` → 拒絕並要求重跑 backtest；舊版報告缺呢啲欄位一律拒絕，冇人手覆寫。
5. 未通過就記錄 `rejected`，繼續用舊版或市場基準。將新想法列為候選假設，唔好即場改投注規則。
6. 通過後先記錄 `promoted`；`predict --model-version auto` 會用最新 promoted，冇 promoted 就用市場基準。

## 解讀限制

- 歷史績效唔保證未來；賽制、馬房、騎師、場地同市場效率會漂移。
- 概率高唔等於值博，概率低亦唔等於冇機會。值博需要比較同一時點可成交賠率、水位、風險同模型優勢。
- 只輸出市場基準時，唔好同時聲稱跑贏市場或有獨家預測。
- 輸出整體不確定性同資料缺口；唔好講「穩贏」「必中」「內幕」。

## 注碼層（staking）

預測概率唔等於投注。`scripts/staking.py` 係純函數模組（無 I/O、無新依賴）：

```python
from staking import edge, kelly_fraction, stake_size, plan_race

edge(0.20, 6.0)                 # 0.20  → EV +20%
kelly_fraction(0.20, 6.0)       # 0.04  → full Kelly 4% bankroll

outcome = stake_size(0.20, 6.0, bankroll=10_000, kelly_multiplier=0.25)
outcome["stake"], outcome["status"]

plan = plan_race(bets, bankroll=10_000, max_race_fraction=0.05)
```

規則（全部可調，預設值偏保守）：

| 參數 | 預設 | 作用 |
|---|---|---|
| `kelly_multiplier` | 0.25 | fractional Kelly；Benter 建議 1/2–1/3，因為 edge 常被高估約 2 倍 |
| `min_edge` | 0.02 | edge 低於 2% 直接跳過 |
| `max_bankroll_fraction` | 0.02 | 單注最多 2% bankroll |
| `max_pool_fraction` | 0.001 | 注碼 ≤ 池的 0.1%，避免自己壓低派彩 |
| `unit` / `min_stake` | 10 / 10 | 向下取整至 HK$10；不足一注就跳過 |
| `max_race_fraction` | 0.05 | 同一場總注碼上限（同場注項相關） |

- `edge = p × odds − 1`；`f* = (p × odds − 1) / (odds − 1)`。
- 池上限用 `pool_size × max_pool_fraction`；彩池賠率已含抽水，唔需要再乘 0.825。
- `plan_race` 會先逐注計 Kelly，總額超過 race cap 就按比例縮細再取整，並將低於最小注的注項標記 `scaled_below_minimum`；總額亦唔會超過當時 bankroll。
- `value-backtest --stake kelly` 直接用呢個共用注碼層：`--kelly-multiplier` 預設 0.25，`--max-race-fraction` 預設 0.05；同場有多個正 edge 注項時用 `plan_race`，唔再另設 full Kelly 公式。
- `--pool-size` 只係明確輸入嘅回測假設，會套用於每場並記入報告；佢唔係由歷史資料自動觀測嘅彩池，唔好當成實績證據。

