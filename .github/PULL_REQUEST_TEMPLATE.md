## 變更摘要

<!-- 講清楚改咗乜、點解改、涉及邊個版本。 -->

## 變更類型

- [ ] Bug fix
- [ ] 新功能
- [ ] 模型／特徵
- [ ] 資料 schema
- [ ] 文件
- [ ] CI／維護

## 驗證

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python tools/package_skill.py --check-only`
- [ ] 已核對 README、SKILL 參考文件、模板及命令一致
- [ ] 冇加入秘密、真實用戶數據、資料庫或臨時檔

## 模型改動專用（冇涉及可刪）

- [ ] 證明全部特徵喺 `data_cutoff` 前已存在
- [ ] 附時間順序 walk-forward，而非隨機拆分
- [ ] 附市場基準、champion、樣本數及 bootstrap 區間
- [ ] 意圖改動附 `intent_centered` ablation
- [ ] 冇改寫歷史預測，亦冇用賽果反推意圖

## 風險與回退

<!-- 兼容性、數據漂移、部署風險，以及點樣保留最後可用版本。 -->
