# -*- coding: utf-8 -*-
"""
Meta 廣告成效拉取（唯讀）— 帳戶健檢與每日診斷的資料來源

Usage:
  python ads_pull.py                 # 預設：拉 90 天，輸出 data/ads/*.json + 健檢報告
  python ads_pull.py --days 30       # 指定回溯天數
  python ads_pull.py --report-only   # 不打 API，用既有 json 重算報告

Token 讀取順序：
  1. 環境變數 META_ADS_TOKEN（GitHub Actions 用，System User Token）
  2. 本機 config.py 的 ADS_TOKEN

這支程式「只讀不寫」：不會建立、修改、暫停任何廣告物件。
所有變更動作一律在 ads_act.py，並受預算護欄限制。
"""
from __future__ import print_function
import json
import os
import sys
import time
import datetime

# 終端機強制 UTF-8（reconfigure 可重入，避免被 import 兩次時關掉 buffer）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

# ── 設定 ──────────────────────────────────────────────────────────────────────
API_BASE   = 'https://graph.facebook.com/v19.0'
TOKEN      = os.environ.get('META_ADS_TOKEN', '').strip()
AD_ACCOUNT = os.environ.get('AD_ACCOUNT_ID', '').strip()

if not TOKEN or not AD_ACCOUNT:
    try:
        import config
        TOKEN      = TOKEN or getattr(config, 'ADS_TOKEN', '')
        AD_ACCOUNT = AD_ACCOUNT or getattr(config, 'AD_ACCOUNT_ID', '')
    except ImportError:
        pass

if AD_ACCOUNT and not AD_ACCOUNT.startswith('act_'):
    AD_ACCOUNT = 'act_' + AD_ACCOUNT

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADS_DIR  = os.path.join(BASE_DIR, 'data', 'ads')

TW_HOURS = 8

# Meta 金額以「幣別最小單位」回傳。多數幣別 offset=100，少數為 1。
# 這張表只影響顯示，raw 值一律原樣存進 json，健檢報告會要求人工對帳一次。
CURRENCY_OFFSET = {
    'TWD': 100, 'USD': 100, 'EUR': 100, 'GBP': 100, 'HKD': 100, 'SGD': 100,
    'CNY': 100, 'AUD': 100, 'CAD': 100, 'THB': 100, 'MYR': 100, 'PHP': 100,
    'JPY': 1, 'KRW': 1, 'VND': 1, 'CLP': 1, 'ISK': 1, 'IDR': 1,
}

# 電商轉換口徑：purchase 事件在不同帳戶會出現不同 action_type，全部視為購買
PURCHASE_ACTIONS = (
    'purchase',
    'omni_purchase',
    'offsite_conversion.fb_pixel_purchase',
    'onsite_web_purchase',
    'onsite_web_app_purchase',
)

INSIGHT_FIELDS_COMMON = [
    'date_start', 'date_stop', 'spend', 'impressions', 'reach', 'frequency',
    'clicks', 'inline_link_clicks', 'ctr', 'inline_link_click_ctr', 'cpc', 'cpm',
    'actions', 'action_values', 'purchase_roas', 'cost_per_action_type',
]
INSIGHT_FIELDS_AD = INSIGHT_FIELDS_COMMON + [
    'quality_ranking', 'engagement_rate_ranking', 'conversion_rate_ranking',
    'video_p75_watched_actions', 'video_thruplay_watched_actions',
]

LEVEL_ID_FIELDS = {
    'account':  [],
    'campaign': ['campaign_id', 'campaign_name'],
    'adset':    ['campaign_id', 'campaign_name', 'adset_id', 'adset_name'],
    'ad':       ['campaign_id', 'campaign_name', 'adset_id', 'adset_name', 'ad_id', 'ad_name'],
}


# ── 時間 ──────────────────────────────────────────────────────────────────────
def tw_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=TW_HOURS)


def date_range(days):
    """回傳 (since, until)，until = 昨天（今天資料未結算，會誤導判斷）"""
    until = tw_now().date() - datetime.timedelta(days=1)
    since = until - datetime.timedelta(days=days - 1)
    return since.strftime('%Y-%m-%d'), until.strftime('%Y-%m-%d')


# ── API ───────────────────────────────────────────────────────────────────────
def api_get(path, params=None):
    if params is None:
        params = {}
    params = dict(params)
    params['access_token'] = TOKEN
    url = path if path.startswith('http') else '{}/{}'.format(API_BASE, path)
    for attempt in range(4):
        try:
            resp = requests.get(url, params=params, timeout=60)
            data = resp.json()
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        if 'error' in data:
            err  = data['error']
            code = err.get('code')
            # 17 / 613 = rate limit，等一下再試
            if code in (17, 613, 80004) and attempt < 3:
                wait = 30 * (attempt + 1)
                print('  [rate limit] 等 {} 秒後重試...'.format(wait))
                time.sleep(wait)
                continue
            raise RuntimeError('{} (code {}, subcode {})'.format(
                err.get('message', ''), code, err.get('error_subcode', '')))
        return data
    raise RuntimeError('API 連續失敗: {}'.format(path))


def api_get_all(path, params=None, max_pages=60):
    """跟著 paging.next 把所有頁拉完"""
    out  = []
    data = api_get(path, params)
    out.extend(data.get('data', []))
    pages = 1
    while pages < max_pages:
        nxt = (data.get('paging') or {}).get('next')
        if not nxt:
            break
        data = api_get(nxt)
        out.extend(data.get('data', []))
        pages += 1
        time.sleep(0.3)
    return out


# ── 抓結構 ────────────────────────────────────────────────────────────────────
def fetch_account():
    print('[1/6] 帳戶基本資料...')
    return api_get(AD_ACCOUNT, {'fields': ','.join([
        'id', 'name', 'account_status', 'currency', 'timezone_name',
        'amount_spent', 'balance', 'spend_cap', 'disable_reason',
        'business_country_code', 'funding_source_details',
    ])})


def fetch_campaigns():
    print('[2/6] Campaign 結構...')
    return api_get_all('{}/campaigns'.format(AD_ACCOUNT), {
        'fields': ','.join([
            'id', 'name', 'status', 'effective_status', 'objective',
            'buying_type', 'bid_strategy', 'daily_budget', 'lifetime_budget',
            'budget_remaining', 'special_ad_categories',
            'created_time', 'start_time', 'stop_time',
        ]),
        'limit': 200,
    })


def fetch_adsets():
    print('[3/6] Ad Set 結構...')
    return api_get_all('{}/adsets'.format(AD_ACCOUNT), {
        'fields': ','.join([
            'id', 'name', 'campaign_id', 'status', 'effective_status',
            'daily_budget', 'lifetime_budget', 'budget_remaining',
            'optimization_goal', 'billing_event', 'bid_amount', 'bid_strategy',
            'attribution_spec', 'promoted_object', 'targeting',
            'learning_stage_info', 'start_time', 'end_time', 'created_time',
        ]),
        'limit': 200,
    })


def fetch_ads():
    print('[4/6] Ad 與素材...')
    return api_get_all('{}/ads'.format(AD_ACCOUNT), {
        'fields': ','.join([
            'id', 'name', 'adset_id', 'campaign_id', 'status', 'effective_status',
            'created_time', 'updated_time',
            'creative{id,name,thumbnail_url,object_type,effective_object_story_id,'
            'video_id,image_url,body,title,call_to_action_type}',
        ]),
        'limit': 200,
    })


def fetch_insights(level, days, time_increment=1):
    """time_increment=1 逐日；'all_days' 則為區間合計"""
    since, until = date_range(days)
    fields = (INSIGHT_FIELDS_AD if level == 'ad' else INSIGHT_FIELDS_COMMON)
    fields = LEVEL_ID_FIELDS[level] + fields
    params = {
        'level': level,
        'fields': ','.join(fields),
        'time_range': json.dumps({'since': since, 'until': until}),
        'time_increment': time_increment,
        'limit': 500,
        'action_attribution_windows': json.dumps(['7d_click', '1d_view']),
    }
    return api_get_all('{}/insights'.format(AD_ACCOUNT), params)


# ── 指標解析（全部 deterministic，不做語意判斷）────────────────────────────────
def _action_sum(action_list, wanted):
    """從 actions / action_values 取指定 action_type 的總和（同義事件取最大值避免重複計算）"""
    if not action_list:
        return 0.0
    best = 0.0
    for a in action_list:
        if a.get('action_type') in wanted:
            try:
                best = max(best, float(a.get('value') or 0))
            except (TypeError, ValueError):
                continue
    return best


def normalize_row(row):
    """把一列 insights 轉成統一指標，所有除法都在這裡做完，下游不再算"""
    def f(key):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    spend      = f('spend')
    purchases  = _action_sum(row.get('actions'), PURCHASE_ACTIONS)
    revenue    = _action_sum(row.get('action_values'), PURCHASE_ACTIONS)
    atc        = _action_sum(row.get('actions'), ('add_to_cart', 'omni_add_to_cart',
                                                 'offsite_conversion.fb_pixel_add_to_cart'))
    ic         = _action_sum(row.get('actions'), ('initiate_checkout', 'omni_initiated_checkout',
                                                 'offsite_conversion.fb_pixel_initiate_checkout'))
    lpv        = _action_sum(row.get('actions'), ('landing_page_view',))
    link_click = f('inline_link_clicks') or _action_sum(row.get('actions'), ('link_click',))

    out = {
        'date':        row.get('date_start', ''),
        'spend':       round(spend, 2),
        'impressions': int(f('impressions')),
        'reach':       int(f('reach')),
        'frequency':   round(f('frequency'), 2),
        'link_clicks': int(link_click),
        'lpv':         int(lpv),
        'atc':         int(atc),
        'checkout':    int(ic),
        'purchases':   int(purchases),
        'revenue':     round(revenue, 2),
        'cpm':         round(f('cpm'), 2),
        'cpc':         round(spend / link_click, 2) if link_click else None,
        'ctr_link':    round(link_click / f('impressions') * 100, 3) if f('impressions') else None,
        'roas':        round(revenue / spend, 3) if spend else None,
        'cpa':         round(spend / purchases, 2) if purchases else None,
        'aov':         round(revenue / purchases, 2) if purchases else None,
        # 漏斗轉換率，用來定位破口在哪一段
        'lpv_rate':    round(lpv / link_click * 100, 2) if link_click else None,
        'atc_rate':    round(atc / lpv * 100, 2) if lpv else None,
        'buy_rate':    round(purchases / atc * 100, 2) if atc else None,
    }
    for key in ('campaign_id', 'campaign_name', 'adset_id', 'adset_name', 'ad_id', 'ad_name',
                'quality_ranking', 'engagement_rate_ranking', 'conversion_rate_ranking'):
        if row.get(key):
            out[key] = row[key]
    return out


def aggregate(rows, key_field):
    """把逐日列按 key 合併，重算比率（先加總分子分母再除，不平均比率）"""
    buckets = {}
    for r in rows:
        k = r.get(key_field)
        if not k:
            continue
        b = buckets.setdefault(k, {
            'key': k, 'name': r.get(key_field.replace('_id', '_name'), ''),
            'spend': 0.0, 'impressions': 0, 'reach': 0, 'link_clicks': 0,
            'lpv': 0, 'atc': 0, 'checkout': 0, 'purchases': 0, 'revenue': 0.0,
            'days': 0,
        })
        for m in ('spend', 'impressions', 'link_clicks', 'lpv', 'atc',
                  'checkout', 'purchases', 'revenue'):
            b[m] += r.get(m) or 0
        b['reach'] = max(b['reach'], r.get('reach') or 0)  # reach 不可加總
        b['days'] += 1

    for b in buckets.values():
        b['spend']   = round(b['spend'], 2)
        b['revenue'] = round(b['revenue'], 2)
        b['roas']    = round(b['revenue'] / b['spend'], 3) if b['spend'] else None
        b['cpa']     = round(b['spend'] / b['purchases'], 2) if b['purchases'] else None
        b['aov']     = round(b['revenue'] / b['purchases'], 2) if b['purchases'] else None
        b['cpm']     = round(b['spend'] / b['impressions'] * 1000, 2) if b['impressions'] else None
        b['cpc']     = round(b['spend'] / b['link_clicks'], 2) if b['link_clicks'] else None
        b['ctr_link'] = round(b['link_clicks'] / b['impressions'] * 100, 3) if b['impressions'] else None
    return sorted(buckets.values(), key=lambda x: -x['spend'])


# ── 存檔 ──────────────────────────────────────────────────────────────────────
def save(name, obj):
    if not os.path.isdir(ADS_DIR):
        os.makedirs(ADS_DIR)
    path = os.path.join(ADS_DIR, name)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print('  → {} ({:,} bytes)'.format(name, os.path.getsize(path)))


def load(name, default=None):
    path = os.path.join(ADS_DIR, name)
    if not os.path.exists(path):
        return default
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


# ── 健檢報告（純規則判斷，不做主觀評語）──────────────────────────────────────
def money(v, offset, cur):
    if v is None or v == '':
        return '—'
    try:
        return '{} {:,.0f}'.format(cur, float(v) / offset)
    except (TypeError, ValueError):
        return str(v)


def pct(v, digits=1):
    return '—' if v is None else '{:.{d}f}%'.format(v, d=digits)


def num(v, digits=2):
    return '—' if v is None else '{:.{d}f}'.format(v, d=digits)


def build_health_report(account, campaigns, adsets, ads, daily_acct,
                        camp_rows, adset_rows, ad_rows, days):
    cur    = account.get('currency', '')
    offset = CURRENCY_OFFSET.get(cur, 100)
    since, until = date_range(days)
    L = []
    W = []   # 需要處理的問題

    L.append('# 廣告帳戶健檢')
    L.append('')
    L.append('- 帳戶：{}（`{}`）'.format(account.get('name', ''), account.get('id', '')))
    L.append('- 幣別：{}　時區：{}'.format(cur, account.get('timezone_name', '')))
    L.append('- 資料區間：{} ~ {}（{} 天，不含今日未結算資料）'.format(since, until, days))
    L.append('- 歸因視窗：7 天點擊 + 1 天瀏覽')
    L.append('- 產生時間：{} (UTC+8)'.format(tw_now().strftime('%Y-%m-%d %H:%M')))
    L.append('')

    # ── 帳戶狀態 ──
    status = account.get('account_status')
    if status != 1:
        W.append('帳戶狀態異常：account_status={}，disable_reason={}'.format(
            status, account.get('disable_reason')))
    if account.get('spend_cap') and float(account['spend_cap']) > 0:
        remain = float(account['spend_cap']) - float(account.get('amount_spent') or 0)
        L.append('- 帳戶花費上限：{}（已花 {}，剩 {}）'.format(
            money(account['spend_cap'], offset, cur),
            money(account.get('amount_spent'), offset, cur),
            money(remain, offset, cur)))
        if remain <= 0:
            W.append('帳戶已達花費上限，廣告會停止投遞')
        L.append('')

    # ── 期間總表 ──
    tot = aggregate([dict(r, _k='all') for r in daily_acct], '_k')
    t = tot[0] if tot else None
    L.append('## 一、期間總成效')
    L.append('')
    if not t or not t['spend']:
        L.append('這個區間沒有花費紀錄。')
        W.append('區間內無花費資料，確認帳戶 ID 與投放時間是否正確')
    else:
        daily_spend = t['spend'] / max(len([d for d in daily_acct if d['spend'] > 0]), 1)
        L.append('| 指標 | 數值 |')
        L.append('|---|---|')
        L.append('| 總花費 | {} {:,.0f} |'.format(cur, t['spend']))
        L.append('| 平均日花費 | {} {:,.0f} |'.format(cur, daily_spend))
        L.append('| 總營收 | {} {:,.0f} |'.format(cur, t['revenue']))
        L.append('| **ROAS** | **{}** |'.format(num(t['roas'])))
        L.append('| 購買次數 | {:,} |'.format(t['purchases']))
        L.append('| CPA | {} |'.format(money(t['cpa'] * offset if t['cpa'] else None, offset, cur)))
        L.append('| AOV | {} |'.format(money(t['aov'] * offset if t['aov'] else None, offset, cur)))
        L.append('| CPM | {} |'.format(num(t['cpm'])))
        L.append('| 連結 CTR | {} |'.format(pct(t['ctr_link'], 2)))
        L.append('| 連結 CPC | {} |'.format(num(t['cpc'])))
        L.append('')

        # ── 漏斗 ──
        L.append('## 二、轉換漏斗（定位破口在哪一段）')
        L.append('')
        L.append('| 階段 | 次數 | 對上一階轉換率 |')
        L.append('|---|---|---|')
        steps = [('連結點擊', t['link_clicks'], None),
                 ('到達頁瀏覽', t['lpv'], t['link_clicks']),
                 ('加入購物車', t['atc'], t['lpv']),
                 ('開始結帳', t['checkout'], t['atc']),
                 ('完成購買', t['purchases'], t['checkout'])]
        for label, val, prev in steps:
            rate = '{:.1f}%'.format(val / prev * 100) if prev else '—'
            L.append('| {} | {:,} | {} |'.format(label, val, rate))
        L.append('')

        # Pixel 事件品質檢查
        if t['purchases'] and not t['revenue']:
            W.append('Pixel 有回傳 purchase 事件但沒有金額（action_values 為空）→ ROAS 算不出來，'
                     '需要修 Pixel 的 value / currency 參數')
        if t['link_clicks'] and not t['lpv']:
            W.append('有連結點擊但完全沒有 landing_page_view → 到達頁 Pixel base code 可能沒觸發，'
                     '或網站載入太慢')
        if t['lpv'] and t['link_clicks'] and t['lpv'] / t['link_clicks'] < 0.6:
            W.append('到達頁瀏覽只有連結點擊的 {:.0f}% → 到達頁載入流失嚴重，'
                     '先修速度比調廣告有效'.format(t['lpv'] / t['link_clicks'] * 100))
        if not t['atc'] and t['purchases']:
            W.append('有購買但沒有 AddToCart 事件 → 中段事件缺失，演算法學習訊號不足')

    # ── 結構 ──
    L.append('## 三、帳戶結構')
    L.append('')
    act_camp  = [c for c in campaigns if c.get('effective_status') == 'ACTIVE']
    act_adset = [a for a in adsets    if a.get('effective_status') == 'ACTIVE']
    act_ad    = [a for a in ads       if a.get('effective_status') == 'ACTIVE']
    L.append('- Campaign：{} 個（投放中 {}）'.format(len(campaigns), len(act_camp)))
    L.append('- Ad Set：{} 個（投放中 {}）'.format(len(adsets), len(act_adset)))
    L.append('- Ad：{} 個（投放中 {}）'.format(len(ads), len(act_ad)))
    L.append('')

    cbo = [c for c in act_camp if c.get('daily_budget') or c.get('lifetime_budget')]
    abo = [c for c in act_camp if c not in cbo]
    if cbo and abo:
        W.append('CBO（{} 個）與 ABO（{} 個）混用中 → 預算調整邏輯不一致，'
                 '自動化前要先統一'.format(len(cbo), len(abo)))

    objectives = {}
    for c in act_camp:
        objectives[c.get('objective', '?')] = objectives.get(c.get('objective', '?'), 0) + 1
    if objectives:
        L.append('投放中的 campaign 目標分佈：')
        for k, v in sorted(objectives.items(), key=lambda x: -x[1]):
            L.append('- `{}` × {}'.format(k, v))
        L.append('')
        non_sales = [k for k in objectives if k not in
                     ('OUTCOME_SALES', 'CONVERSIONS', 'PRODUCT_CATALOG_SALES')]
        if non_sales:
            W.append('有 {} 類非轉換目標的 campaign 在跑（{}）→ 以 ROAS 為目標時，'
                     '這些不該計入同一口徑'.format(len(non_sales), '、'.join(non_sales)))

    # 學習期
    learning = [a for a in act_adset
                if (a.get('learning_stage_info') or {}).get('status') == 'LEARNING']
    limited  = [a for a in act_adset
                if (a.get('learning_stage_info') or {}).get('status') == 'LEARNING_LIMITED']
    if learning:
        L.append('- 學習期中：{} 個 ad set（這期間不要動預算）'.format(len(learning)))
    if limited:
        L.append('- **學習受限**：{} 個 ad set → 週轉換數不足 50，需要合併或放寬受眾'.format(len(limited)))
        W.append('{} 個 ad set 卡在 LEARNING_LIMITED → 這是結構問題，'
                 '調預算無法解決'.format(len(limited)))
        for a in limited[:5]:
            L.append('  - `{}` {}'.format(a['id'], a.get('name', '')))
    L.append('')

    # ── 花費集中度 ──
    if adset_rows:
        L.append('## 四、Ad Set 成效（依花費排序，前 15）')
        L.append('')
        L.append('| Ad Set | 花費 | ROAS | 購買 | CPA | CPM | CTR |')
        L.append('|---|---|---|---|---|---|---|')
        for r in adset_rows[:15]:
            L.append('| {} | {:,.0f} | {} | {} | {} | {} | {} |'.format(
                (r['name'] or r['key'])[:36], r['spend'], num(r['roas']),
                r['purchases'], num(r['cpa'], 0), num(r['cpm'], 0), pct(r['ctr_link'], 2)))
        L.append('')

        total_spend = sum(r['spend'] for r in adset_rows) or 1
        top3 = sum(r['spend'] for r in adset_rows[:3]) / total_spend * 100
        L.append('- 前 3 個 ad set 佔總花費 {:.0f}%'.format(top3))
        if top3 > 80 and len(adset_rows) > 3:
            W.append('花費高度集中（前 3 個佔 {:.0f}%）→ 單點失效風險高，'
                     '需要備援組合'.format(top3))

        zero = [r for r in adset_rows if r['spend'] > 0 and not r['purchases']]
        if zero:
            wasted = sum(r['spend'] for r in zero)
            L.append('- **零轉換 ad set：{} 個，累計燒掉 {} {:,.0f}（佔 {:.0f}%）**'.format(
                len(zero), cur, wasted, wasted / total_spend * 100))
            W.append('{} 個 ad set 有花費零購買，共 {} {:,.0f} → 第一波止血對象'.format(
                len(zero), cur, wasted))
            for r in sorted(zero, key=lambda x: -x['spend'])[:8]:
                L.append('  - `{}` {} — 花費 {:,.0f}'.format(
                    r['key'], (r['name'] or '')[:36], r['spend']))
        L.append('')

    # ── 素材層 ──
    if ad_rows:
        L.append('## 五、素材成效（前 15）')
        L.append('')
        L.append('| Ad | 花費 | ROAS | 購買 | CTR | CPM |')
        L.append('|---|---|---|---|---|---|')
        for r in ad_rows[:15]:
            L.append('| {} | {:,.0f} | {} | {} | {} | {} |'.format(
                (r['name'] or r['key'])[:36], r['spend'], num(r['roas']),
                r['purchases'], pct(r['ctr_link'], 2), num(r['cpm'], 0)))
        L.append('')

        # 素材疲勞：頻率高 + CTR 低於中位
        ctrs = sorted([r['ctr_link'] for r in ad_rows if r['ctr_link'] is not None])
        med  = ctrs[len(ctrs) // 2] if ctrs else None
        if med is not None:
            tired = [r for r in ad_rows
                     if r['spend'] > 0 and r['ctr_link'] is not None and r['ctr_link'] < med * 0.6]
            if tired:
                L.append('- CTR 低於中位數 60%（中位 {:.2f}%）的素材：{} 個'.format(med, len(tired)))
                W.append('{} 個素材 CTR 明顯低於帳戶中位 → 換素材優先於調預算'.format(len(tired)))
        L.append('')

    # ── 近期趨勢 ──
    if daily_acct and len(daily_acct) >= 14:
        recent = daily_acct[-7:]
        prev   = daily_acct[-14:-7]

        def blk(rows):
            s = sum(r['spend'] for r in rows)
            v = sum(r['revenue'] for r in rows)
            p = sum(r['purchases'] for r in rows)
            return s, v, (v / s if s else None), p

        s1, v1, r1, p1 = blk(recent)
        s0, v0, r0, p0 = blk(prev)
        L.append('## 六、近 7 天 vs 前 7 天')
        L.append('')
        L.append('| | 前 7 天 | 近 7 天 | 變化 |')
        L.append('|---|---|---|---|')

        def row(label, a, b, fmt='{:,.0f}'):
            d = '—' if not a else '{:+.0f}%'.format((b - a) / a * 100)
            return '| {} | {} | {} | {} |'.format(label, fmt.format(a), fmt.format(b), d)

        L.append(row('花費', s0, s1))
        L.append(row('營收', v0, v1))
        L.append(row('購買', p0, p1))
        L.append('| ROAS | {} | {} | {} |'.format(
            num(r0), num(r1),
            '—' if not r0 else '{:+.0f}%'.format((r1 - r0) / r0 * 100) if r1 else '—'))
        L.append('')
        if r0 and r1 and r1 < r0 * 0.8:
            W.append('近 7 天 ROAS 較前 7 天下滑 {:.0f}% → 需要判斷是素材疲勞還是受眾耗盡'.format(
                (r0 - r1) / r0 * 100))

    # ── 問題清單放最前面的摘要 ──
    head = ['# 廣告帳戶健檢', '']
    if W:
        head.append('## ⚠ 需要處理（{} 項）'.format(len(W)))
        head.append('')
        for i, w in enumerate(W, 1):
            head.append('{}. {}'.format(i, w))
        head.append('')
    else:
        head.append('健檢未發現結構性問題。')
        head.append('')
    head.append('> 金額對帳：本報告以 {} offset={} 換算。'
                '請對照 Ads Manager 的總花費確認一次，數字對不上要先修 offset。'.format(cur, offset))
    head.append('')
    head.append('---')
    head.append('')

    return '\n'.join(head + L[1:])


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    days = 90
    if '--days' in args:
        days = int(args[args.index('--days') + 1])
    report_only = '--report-only' in args

    if report_only:
        account   = load('account.json', {})
        campaigns = load('campaigns.json', [])
        adsets    = load('adsets.json', [])
        ads       = load('ads.json', [])
        daily     = load('daily_account.json', [])
        camp_d    = load('daily_campaign.json', [])
        adset_d   = load('daily_adset.json', [])
        ad_d      = load('daily_ad.json', [])
    else:
        if not TOKEN:
            print('ERROR: 找不到 META_ADS_TOKEN')
            sys.exit(1)
        if not AD_ACCOUNT:
            print('ERROR: 找不到 AD_ACCOUNT_ID（格式 act_1234567890）')
            sys.exit(1)

        print('拉取 {} 最近 {} 天資料'.format(AD_ACCOUNT, days))
        account   = fetch_account()
        campaigns = fetch_campaigns()
        adsets    = fetch_adsets()
        ads       = fetch_ads()

        print('[5/6] 逐日 insights（account / campaign / adset / ad）...')
        daily   = [normalize_row(r) for r in fetch_insights('account',  days)]
        camp_d  = [normalize_row(r) for r in fetch_insights('campaign', days)]
        adset_d = [normalize_row(r) for r in fetch_insights('adset',    min(days, 30))]
        ad_d    = [normalize_row(r) for r in fetch_insights('ad',       min(days, 30))]

        print('[6/6] 存檔...')
        save('account.json',        account)
        save('campaigns.json',      campaigns)
        save('adsets.json',         adsets)
        save('ads.json',            ads)
        save('daily_account.json',  daily)
        save('daily_campaign.json', camp_d)
        save('daily_adset.json',    adset_d)
        save('daily_ad.json',       ad_d)

    daily.sort(key=lambda r: r.get('date', ''))
    camp_rows  = aggregate(camp_d,  'campaign_id')
    adset_rows = aggregate(adset_d, 'adset_id')
    ad_rows    = aggregate(ad_d,    'ad_id')

    save('summary.json', {
        'generated_at': tw_now().strftime('%Y-%m-%d %H:%M'),
        'days':         days,
        'currency':     account.get('currency', ''),
        'campaign':     camp_rows,
        'adset':        adset_rows,
        'ad':           ad_rows,
    })

    report = build_health_report(account, campaigns, adsets, ads,
                                 daily, camp_rows, adset_rows, ad_rows, days)
    path = os.path.join(ADS_DIR, 'health_report.md')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(report)
    print('\n' + report)
    print('\n完成 → data/ads/health_report.md')


if __name__ == '__main__':
    main()
