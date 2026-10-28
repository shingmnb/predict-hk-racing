#!/usr/bin/env python3
# cards_fixed.json + odds → predict-hk-racing race-card schema（S1-8/S1-9 盲跑）
import json, os
from datetime import datetime

WS = '/tmp/predict-hk-racing/racing-data'
os.makedirs(f'{WS}/cards', exist_ok=True)
os.makedirs(f'{WS}/predictions', exist_ok=True)
cards = json.load(open('/root/racing/20261024/cards_fixed.json'))

TOP_J = ('麥道朗','韋紀力','薛恩','布浩榮','柏威廉','易威廉','戴文高','連達文','史卓豐','畢鐵辰')
META = {
 '8': {'race_id':'2026-10-24-S1-R8','race_name':'覺士盾 G1','off':'2026-10-24T14:10:00+08:00','dist':2040,'cls':'G1'},
 '9': {'race_id':'2026-10-24-S1-R9','race_name':'泰斯奧錦標 G3','off':'2026-10-24T14:50:00+08:00','dist':1200,'cls':'G3'},
}

def intent_ev(h):
    ev = []
    jk = h.get('jockey','')
    if any(t in jk for t in TOP_J):
        ev.append({'signal':'jockey_booking','direction':'positive','strength':0.4,'confidence':0.65,
                   'source_id':'PDF','note':f'大牌騎師 {jk} 主轡（候選部署訊號）'})
    fins = [f.get('pos') for f in (h.get('forms') or [])[:3] if isinstance(f.get('pos'), int)]
    if fins and fins[0] and fins[0] <= 3:
        ev.append({'signal':'recent_form','direction':'positive','strength':0.35,'confidence':0.55,
                   'source_id':'PDF','note':f'上仗第{fins[0]}名（近況訊號，非部署）'})
    return ev

now = '2026-10-24T16:20:00+08:00'  # 盲跑時間戳（賽後記錄但只用賽前數據 — collection_mode 標 historical 誠實處理）
for rno in ('8','9'):
    m = META[rno]
    od = {int(k): v for k, v in json.load(open(f'/root/racing/20261024/odds_s1_{rno}.json')).items()}
    runners = []
    for h in cards[rno]:
        if h.get('emergency'): continue
        c = h['card']
        if c not in od: continue  # NR/live 檢查
        fins = [f.get('pos') for f in (h.get('forms') or [])[:5] if isinstance(f.get('pos'), int)]
        st = h.get('stats') or {}
        td = st.get('同場同程', ['0','0-0-0'])
        try:
            tdn = int(td[0]); t3 = sum(int(x) for x in td[1].split('-')[:3])
            cd_rate = round(t3/(tdn*3) if tdn else 0, 3); cd_n = tdn
        except: cd_rate, cd_n = 0, 0
        runners.append({
            'horse_no': c, 'horse_name': h['name'], 'jockey': h.get('jockey',''), 'trainer': h.get('trainer_cn') or h.get('trainer',''),
            'draw': h.get('draw'), 'weight_lb': h.get('weight'), 'rating': int(float(h.get('rating') or 70)),
            'scratched': False, 'win_odds': od[c],
            'features': {
                'career_place_rate': round(sum(1 for f in fins if f and f<=3)/max(len(fins),1),3), 'cd_place_rate': cd_rate, 'cd_runs': cd_n,
                'recent_finish_mean': round(sum(fins)/len(fins),1) if fins else None,
                'days_since_run': 21,
            },
            'intent_evidence': intent_ev(h),
        })
    rc = {
        'schema_version':'1.0',
        'collection_mode':'historical',  # 誠實標記：賽後補錄，只用賽前存在數據
        'captured_at': now, 'data_cutoff': m['off'],
        'race': {'race_id': m['race_id'], 'meeting_date':'2026-10-24','venue':'S1','race_no':int(rno),
                 'race_name': m['race_name'],'off_time': m['off'],'distance_m': m['dist'],
                 'surface':'turf','going':'good_to_dead','class_name': m['cls'],'place_slots':3},
        'sources': [{'source_id':'PDF','title':'HKJC 越洋轉播排位表 PDF（osc20261024）','url':'local','retrieved_at': now,'official':True},
                    {'source_id':'ODDS','title':'HKJC GraphQL 同時點賠率快照','url':'local','retrieved_at': now,'official':True}],
        'runners': runners,
    }
    p = f"{WS}/cards/{m['race_id']}.json"
    json.dump(rc, open(p,'w'), ensure_ascii=False, indent=1)
    print(f"{m['race_id']}: {len(runners)} runners written")
print('done')
