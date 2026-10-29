# 10 倍以上半冷馬：精簡情報與回測流程

只回答一條問題：**賽前 2–3 小時已標價 10 倍以上嘅馬入面，有冇一匹具備可審計非賠率情報支持入位？**唔擴大成多因子評分或強制每日有選擇。

## 硬性門檻

1. 以 T-2h 為目標，接受 T-3h 至 T-2h（包含邊界）嘅完整全場 WIN 快照；多張快照只用最接近 T-2h 嘅一張，唔准事後揀價。
2. 快照必須涵蓋全部 active 馬嘅正數 `win_odds`，並保存原件、實際 `captured_at`、URL 及 SHA-256。最終賠率不得倒填。
3. 基本面短名單、`fundamental_created_at` 及 `fundamental_run_id` 必須先於市場快照；只有確實未見賠率先記 `market_blind: true`。
4. `reviews` 必須逐匹覆蓋快照內全部 10 倍以上 active 馬，唔准只寫最後中咗嘅馬。
5. 每場最多一匹 `selected`；其餘全部 `rejected` 並附理由。可以單場或全日棄選，零選擇唔係失敗。
6. 入選至少兩個不同 `family` 嘅證據，其中至少一個唔係 `market`；每項 quote 必須逐字出現喺對應 source excerpt。
7. 入選馬必須有最強反證、至少一項未知及撤回條件。冇選擇時必填 `abstain_reason`。
8. 降班、配備、名騎、見習減磅或單次落飛唔可以單獨構成入選理由，亦唔准虛構馬房意圖。

## 建立一場決策

先按主 SKILL 完成基本面 `analyze`，再建立 T-2h race card。複製 `assets/longshot-decision-template.json`，但唔好直接改模板當用戶紀錄。建議目錄：

```text
racing-data/longshot/
├── cards/       # <race_id>.json；T-2h 全場 WIN 快照
├── decisions/   # <race_id>.json；凍結選擇、拒絕與棄選
├── results/     # <race_id>.json；官方結果及派彩
└── evaluations/
```

驗證並凍結：

```bash
python "<SKILL>/scripts/longshot.py" validate \
  --card "racing-data/longshot/cards/<race_id>.json" \
  --decision "racing-data/longshot/decisions/<race_id>.json" \
  --output "racing-data/longshot/evaluations/<race_id>-validation.json"
```

`validation_status: passed` 只代表規則同證據結構合格；`forward_kpi_eligible: true` 仲要求 `collection_mode: live`、冇事後補採標記及全部時點合格。歷史重建可以保存，但正式回測會排除。

## 回測

```bash
python "<SKILL>/scripts/longshot.py" backtest \
  --input-dir "racing-data/longshot" --bootstrap 1000 \
  --output "racing-data/longshot/evaluations/backtest.json"
```

一份報告只接受一個 `rule_version`，避免將新舊規則混合報成績。主指標係**每個正式選擇入位置名額嘅比例**；本地賽 7 匹或以上通常係前三，4–6 匹則按 card `place_slots`。棄選場次保留喺場次分母，唔會假裝有選擇。

同時報告：

- 頭馬率、實際 10 倍以上入位馬捕捉率、棄選率及最長未入位連敗；
- 同場全部合資格馬入位基率，以及相同選擇數量嘅隨機期望；
- 有官方派彩先計固定一單位 WIN／PLACE ROI 及最大回撤；缺派彩就留空；
- 以賽日為區塊嘅 bootstrap 95% 區間；歷史價位帶基準缺外部獨立樣本時標 `requires_external_history`。

未滿 30 場及 100 個正式選擇，只描述表現，唔宣稱穩定優勢。達標亦只係可以開始判斷，唔係盈利保證。每次改規則必開新 `rule_version`，只用下一批未見過賽事驗證。

## 獨立核查

官方賽果及派彩負責結算；原始快照負責證明賽前時點；第三方重跑負責核對程式；盲審負責抽查影片標記及引文。任何一項未完成都如實標記，唔好將歷史重建包裝成真正賽前預測。
