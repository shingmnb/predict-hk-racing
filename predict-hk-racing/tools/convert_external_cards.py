#!/usr/bin/env python3
"""外部排位數據 → predict-hk-racing race-card schema 轉換器（bridge）

輸入格式（本 converter 示範嘅外部 schema）：
  cards: {race_no_str: [ {card, name, jockey, trainer_cn, draw, weight, rating,
                          forms: [{pos}], stats: {同場同程: [n, "a-b-c"]}, emergency}, ... ]}
  odds:  odds_r{no}.json — {cloth_str: win_odds_float}

輸出：racing-data/cards/<race_id>.json（符合 race-card-template schema）

用法：
  python3 convert_external_cards.py <cards.json> <odds_dir> <out_dir> [--venue S1] [--date 2026-10-24]

實測：2026-10-24 費明頓 S1-8/S1-9（27 匹）轉換 + import-card + predict 全通。
依賴 hkjc-odds-fetching.md 嘅 OddsTracker 攞同時點賠率。
"""
import json, os, sys
from datetime import datetime

def career_place_rate(forms):
    fins = [f.get('pos') for f in (forms or []) if isinstance(f.get('pos'), int)]
    return round(sum(1 for f in fins if f and f <= 3) / max(len(fins), 1), 3) if fins else 0.0

def cd_rate(stats):
    td = (stats or {}).get('同場同程', ['0', '0-0-0'])
    try:
        n = int(td[0]); t3 = sum(int(x) for x in td[1].split('-')[:3])
        return round(t3 / (n * 3) if n else 0, 3), n
    except Exception:
        return 0.0, 0

def convert(cards_path, odds_dir, out_dir, venue='S1', date='2026-10-24'):
    cards = json.load(open(cards_path))
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for rno, horses in cards.items():
        od_path = os.path.join(odds_dir, f'odds_r{rno}.json')
        if not os.path.exists(od_path):
            od_path = os.path.join(odds_dir, f'odds_s1_{rno}.json')
        od = {int(k): v for k, v in json.load(open(od_path)).items()} if os.path.exists(od_path) else {}
        runners = []
        for h in horses:
            if h.get('emergency') or h.get('card') not in od:
                continue  # emergency 或 NR（賠率缺 cloth = 退出，雙源核實）
            fins = [f.get('pos') for f in (h.get('forms') or [])[:5] if isinstance(f.get('pos'), int)]
            cdp, cdn = cd_rate(h.get('stats'))
            runners.append({
                'horse_no': h['card'], 'horse_name': h['name'],
                'jockey': h.get('jockey', ''), 'trainer': h.get('trainer_cn') or h.get('trainer', ''),
                'draw': h.get('draw'), 'weight_lb': h.get('weight'),
                'rating': int(float(h.get('rating') or 70)),
                'scratched': False, 'win_odds': od[h['card']],
                'features': {
                    'career_place_rate': career_place_rate(h.get('forms')),
                    'cd_place_rate': cdp, 'cd_runs': cdn,
                    'recent_finish_mean': round(sum(fins) / len(fins), 1) if fins else 20.0,
                    'days_since_run': 21,  # 外部數據冇日期 — 中性值（訓練中位數填補原則）
                },
            })
        rc = {
            'schema_version': '1.0', 'collection_mode': 'live',
            'captured_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'data_cutoff': datetime.now().astimezone().isoformat(timespec='seconds'),
            'race': {'race_id': f'{date}-{venue}-R{rno}', 'meeting_date': date,
                     'venue': venue, 'race_no': int(rno), 'place_slots': 3},
            'sources': [], 'runners': runners,
        }
        p = os.path.join(out_dir, f"{rc['race']['race_id']}.json")
        json.dump(rc, open(p, 'w'), ensure_ascii=False, indent=1)
        written.append(p)
    return written

if __name__ == '__main__':
    a = sys.argv
    venue = a[a.index('--venue') + 1] if '--venue' in a else 'S1'
    date = a[a.index('--date') + 1] if '--date' in a else '2026-10-24'
    for p in convert(a[1], a[2], a[3], venue, date):
        print('written:', p)
