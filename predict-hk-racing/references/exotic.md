# 非熱門核心 Q／位置 Q：組合凍結與結算

只處理已通過 `longshot.py validate` 嘅核心馬：每個實際馬對都要有證據，唔用組合概率或賠率估算派彩。Q／PQ 分帳，容許零組合。

## 1. 三名子 Agent 點樣隔離

- A（`form_and_race_shape`）：原片、分段、走位、對手質素及步速劇本。
- B（`stable_and_intelligence`）：晨操、試閘、配備、獸醫、騎師調動及具名消息。
- C（獨立核查）：A／B 完成後開始；完成前唔睇兩者推薦或評分，只由原始來源核查身份、時點、引文、反證及遺漏候選，並自行搜反證。核心錯誤未解決就唔准凍結。

用戶指定三方審查時，實際啟動兩個互唔共享 context 嘅子 Agent；C 另開新子 Agent，唔好 resume A／B。平台唔支援或 C 未完成時，`audit.status` 唔准填 `passed`，主 Agent 唔可以假扮獨立核查。

A／B 可以共用原件，但共用來源只算一條證據鏈。程式檢查嘅係**程序隔離聲明、時間及來源歸屬**，唔可以證明 Agent 真係思想獨立；三人同意亦唔係賽果證明。

## 2. 凍結實際組合

複製 `assets/exotic-ticket-template.json`。填入：

- 與單馬決策相同嘅 `core_horse_no`；棄選時係 `null`，唔准生成組合。
- `longshot_decision_sha256`、`market_card_sha256`：由 `exotic.py` 對實際 JSON canonical content 計算，唔好填隨機值。
- A／B 角色、完成時間、原始 `source_ids` 及未睇對方輸出聲明；C 嘅盲核聲明、檢查及時間。
- 每個 `combinations` 項目：`pool`（Q／PQ）、遞增馬號 `horses`、`stake_hkd`、`scenario_id`、共同 `mechanism`、最強反證、未知及撤回條件。
- 每匹馬至少一項非市場 `horse_evidence`，附研究員、來源及逐字 quote；同一彩池同一馬對唔准重複，Q 與 PQ 可以各自有同一馬對。
- 全部注額總和不得超過 `budget_limit_hkd`；零組合要填 `abstain_reason`。

可用呢段取得兩個 content hash（只讀實際輸入，唔改舊檔）：

```bash
python -c "import json,sys; sys.path.insert(0,'<SKILL>/scripts'); from exotic import canonical_sha256; print(canonical_sha256(json.load(open(sys.argv[1],encoding='utf-8'))))" "<actual.json>"
```

建議目錄：

```text
racing-data/longshot/
├── cards/ decisions/ results/ tickets/
└── evaluations/
```

凍結命令：

```bash
python "<SKILL>/scripts/exotic.py" freeze \
  --card "racing-data/longshot/cards/<race_id>.json" \
  --longshot-decision "racing-data/longshot/decisions/<race_id>.json" \
  --ticket "racing-data/longshot/tickets/<race_id>.json" \
  --output "racing-data/longshot/evaluations/<race_id>-freeze.json"
```

`freeze_status: ready` 只代表結構、時序、證據及預算合格；`forward_kpi_eligible` 仍要求正式 live 時點資料。原始來源 SHA-256 係輸入完整性聲明，程式另核對引文，唔會假裝已驗證外部原件真偽。

## 3. 官方結果格式

`meeting_chain.py` 會由官方賽果 HTML 保存 Q／PQ 逐組派彩。其他輸入方式須將以下區塊加入同一 result JSON；`dividend_hkd` 係官方每 HK$10 派彩，唔係十进制賠率：

```json
"exotic_dividends": {
  "Q": {
    "status": "final",
    "combinations": [{"horses": [3, 4], "dividend_hkd": 194.5}],
    "refunds": []
  },
  "PQ": {
    "status": "final",
    "combinations": [
      {"horses": [3, 4], "dividend_hkd": 74.0},
      {"horses": [1, 3], "dividend_hkd": 112.5},
      {"horses": [1, 4], "dividend_hkd": 65.0}
    ],
    "refunds": []
  }
}
```

只保存官方實際中獎組合，唔好自行補足其他馬對。官方退款用 `refunds: [{"horses": [1, 3], "amount_hkd": 10}]` 明確記錄；彩池關閉用 `status: "closed"`、`refund_all: true` 及空組合。未能裁定嘅非完賽馬會阻止結算，唔會估算退款。官方更正須新增 result snapshot。

## 4. 逐組結算

```bash
python "<SKILL>/scripts/exotic.py" settle \
  --card "racing-data/longshot/cards/<race_id>.json" \
  --longshot-decision "racing-data/longshot/decisions/<race_id>.json" \
  --ticket "racing-data/longshot/tickets/<race_id>.json" \
  --result "racing-data/longshot/results/<race_id>.json" \
  --freeze "racing-data/longshot/evaluations/<race_id>-freeze.json" \
  --output "racing-data/longshot/evaluations/<race_id>-settlement.json"
```

正式 CLI 必須提供凍結收據；票單內容 hash 改變就拒絕結算。每組分開保存：

- `sporting_hit`：按名次係咪符合 Q／PQ 條件；
- `target_hit`：`sporting_hit` 並包含事前核心馬；
- `official_payout_hit`：官方派彩表實際列出該組；
- `stake_hkd`、退款、官方派彩、實收及盈虧。

因此特殊派彩可以「官方中咗、賽事名次目標未中」；一張 PQ 亦可以有多組派彩。非核心或兩馬 WIN 均低於 10 倍嘅派彩會另列，**仍計現金，但唔計冷馬目標命中**。缺官方 Q／PQ 紀錄就拒絕結算，唔用賠率或概率補數。

呢個命令只結算已凍結票單，唔輸出 EV 或長期優勢；多賽日比較仍須同場、同時點、同預算對照及未來賽事驗證。
