# predict-hk-racing

[![test](https://github.com/shingmnb/predict-hk-racing/actions/workflows/test.yml/badge.svg)](https://github.com/shingmnb/predict-hk-racing/actions/workflows/test.yml)

香港賽馬數據驅動預測 Skill：自動搜集及核實資料、分析能力／形勢／馬房去馬意圖、輸出勝出與位置概率，再用不可覆寫紀錄、時間順序回測及模型升級門檻持續校準。

> 呢個項目處理嘅「去馬」係基於公開證據嘅不確定推論，唔係內幕消息。概率唔係保證；賽馬高方差，請只用可承受損失作娛樂預算。

## 功能

- **資料快照**：官方來源優先；保存來源、時間戳、退出及臨場更新。
- **意圖證據模型**：分開能力、形勢、人馬部署同市場；每項判斷附反證及置信度。
- **概率模型**：市場去水基準、conditional logit、Plackett–Luce 位置概率近似。
- **審計紀錄**：SQLite 保存賽前快照、預測、賽果、模型版本及評估；主要紀錄 append-only。
- **專業回測**：expanding-window walk-forward、log loss、Brier、校準及 race-level bootstrap。
- **有門檻學習**：候選模型要跑贏市場及現任 champion 先可 promoted；否則記錄 rejected。
- **意圖 ablation**：驗證 `intent_centered` 有冇時間外預測價值，唔好用賽果反推動機。

## 安裝

- 從 [Releases](https://github.com/shingmnb/predict-hk-racing/releases) 下載 `predict-hk-racing.skill`，匯入支援 `.skill` 套件嘅 agent。
- 或直接使用 repo 內嘅 [`predict-hk-racing/`](predict-hk-racing/) 源文件。
- 完整操作指引見 [`predict-hk-racing/SKILL.md`](predict-hk-racing/SKILL.md)。

## 快速開始

所有用戶資料預設放 `racing-data/`，已列入 `.gitignore`。腳本只用 Python 標準庫，建議 Python 3.11 或以上。

```bash
# 初始化本地紀錄庫
python predict-hk-racing/scripts/racing_lab.py init --db racing-data/racing.db

# 複製並填寫賽前資料模板
cp predict-hk-racing/assets/race-card-template.json racing-data/cards/race.json
python predict-hk-racing/scripts/racing_lab.py import-card \
  --db racing-data/racing.db --file racing-data/cards/race.json

# 凍結預測；未有任何 promoted 模型時會用市場基準
python predict-hk-racing/scripts/racing_lab.py predict \
  --db racing-data/racing.db --race-id "<race_id>" \
  --model-version auto --output racing-data/predictions/<race_id>.json
```

賽後結算、訓練、回測及升級命令見 [`schemas-and-cli.md`](predict-hk-racing/references/schemas-and-cli.md)。

## Repo 結構

```text
predict-hk-racing/       # 可分發 Skill（SKILL.md、scripts、references、assets）
tests/                   # 標準庫 unittest；全部使用虛構資料
tools/package_skill.py   # Skill 驗證、打包及 SHA-256
.github/workflows/       # CI 測試及 tag 觸發 Release
.github/ISSUE_TEMPLATE/  # Bug 與功能建議
CONTRIBUTING.md          # 維護及 PR 流程
CHANGELOG.md             # 版本紀錄
```

## 維護

```bash
# 執行全部測試
python -m unittest discover -s tests -v

# 只驗證 Skill 結構
python tools/package_skill.py --check-only

# 打包到 dist/
python tools/package_skill.py --output dist
```

採用 Semantic Versioning。推送 `v*.*.*` tag 會觸發測試、打包 `.skill`、產生 SHA-256 並建立 GitHub Release。詳見 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 數據與限制

- 唔好提交真實投注紀錄、個人資料、登入資料、付費內容或 `racing-data/`。
- 歷史重建唔可以冒充實時預測；任何特徵都要證明喺資料截止時間前已存在。
- 未夠樣本時使用市場基準，唔好自創權重假裝有已驗證優勢。
- 模型表現會漂移；歷史績效唔保證未來，更唔保證投注回報。

## License

[MIT](LICENSE) © 2026 shingmnb
