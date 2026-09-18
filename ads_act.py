# -*- coding: utf-8 -*-
"""
Meta 廣告變更執行器 — 所有花錢相關的動作都必須經過這裡

設計原則：授權規則寫在 code 裡強制執行，不依賴操作者自律。

自動許可（AUTO）：
  - pause_ad / pause_adset / pause_campaign     關閉 = 止血，不會增加支出
  - decrease_budget                             只准往下調
  - 測試預算池內的 create_adset                 總日預算不超過 TEST_POOL_DAILY_CAP

需要人工核可（--approved）：
  - increase_budget                             任何提高預算
  - resume（重新啟用已暫停項目）                 等同增加支出
  - 超出測試預算池的任何新建

永遠拒絕（無論有沒有 --approved）：
  - 使帳戶投放中總日預算超過 ACCOUNT_DAILY_CAP
  - 單次預算調幅超過 MAX_BUDGET_CHANGE_PCT

Usage:
  python ads_act.py --plan plan.json                 # dry-run，只印出會做什麼
  python ads_act.py --plan plan.json --execute       # 執行 AUTO 類動作
  python ads_act.py --plan plan.json --execute --approved   # 連需核可的一起執行

plan.json 格式：
  [
    {"op": "pause_adset",      "id": "1234", "reason": "30天零轉換，燒掉 NT$11,749"},
    {"op": "decrease_budget",  "id": "5678", "level": "adset", "daily_budget": 800,
     "reason": "ROAS 0.8 低於門檻，先砍半觀察"},
    {"op": "increase_budget",  "id": "9012", "level": "adset", "daily_budget": 3000,
     "reason": "ROAS 5.4 穩定 7 天"}
  ]
  daily_budget 一律用「帳戶幣別的整數金額」（例如 NT$800 就寫 800），程式自己換算 offset。
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

import ads_pull as P   # 共用 api_get / CURRENCY_OFFSET / 路徑

API_BASE   = P.API_BASE
TOKEN      = P.TOKEN
AD_ACCOUNT = P.AD_ACCOUNT
ADS_DIR    = P.ADS_DIR

# ── 護欄參數（必須明確設定，沒設就不准跑）────────────────────────────────────
def _envint(name):
    v = os.environ.get(name, '').strip()
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None

ACCOUNT_DAILY_CAP     = _envint('ADS_ACCOUNT_DAILY_CAP')   # 帳戶投放中總日預算上限
TEST_POOL_DAILY_CAP   = _envint('ADS_TEST_POOL_DAILY_CAP')  # 測試池每日上限
MAX_BUDGET_CHANGE_PCT = _envint('ADS_MAX_BUDGET_CHANGE_PCT') or 30   # 單次調幅上限 %
TEST_POOL_TAG         = os.environ.get('ADS_TEST_POOL_TAG', '[TEST]')  # 測試池以名稱前綴辨識

LOG_PATH = os.path.join(ADS_DIR, 'action_log.jsonl')

AUTO_OPS     = {'pause_ad', 'pause_adset', 'pause_campaign', 'decrease_budget'}
APPROVAL_OPS = {'increase_budget', 'resume_ad', 'resume_adset', 'resume_campaign',
                'create_adset', 'create_ad'}
LEVEL_EDGE   = {'adset': 'adsets', 'campaign': 'campaigns', 'ad': 'ads'}


class Rejected(Exception):
    pass


# ── API 寫入 ──────────────────────────────────────────────────────────────────
def api_post(node, params):
    url  = '{}/{}'.format(API_BASE, node)
    body = dict(params)
    body['access_token'] = TOKEN
    resp = requests.post(url, data=body, timeout=60)
    data = resp.json()
    if 'error' in data:
        err = data['error']
        raise RuntimeError('{} (code {})'.format(err.get('message', ''), err.get('code')))
    return data


# ── 帳戶現況（護欄要用真實數字，不用快取猜）──────────────────────────────────
def load_live_state():
    """回傳 (currency, offset, {adset_id: adset}, {campaign_id: campaign})"""
    account = P.api_get(AD_ACCOUNT, {'fields': 'currency,account_status'})
    cur     = account.get('currency', 'TWD')
    offset  = P.CURRENCY_OFFSET.get(cur, 100)

    adsets = P.api_get_all('{}/adsets'.format(AD_ACCOUNT), {
        'fields': 'id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget',
        'limit': 200})
    camps = P.api_get_all('{}/campaigns'.format(AD_ACCOUNT), {
        'fields': 'id,name,status,effective_status,daily_budget,lifetime_budget',
        'limit': 200})
    return cur, offset, {a['id']: a for a in adsets}, {c['id']: c for c in camps}


def active_daily_total(adsets, camps, offset):
    """帳戶目前投放中的總日預算（CBO 算 campaign，ABO 算 adset，避免重複計算）"""
    total = 0
    cbo_ids = set()
    for c in camps.values():
        if c.get('effective_status') != 'ACTIVE':
            continue
        if c.get('daily_budget'):
            total += int(c['daily_budget'])
            cbo_ids.add(c['id'])
    for a in adsets.values():
        if a.get('effective_status') != 'ACTIVE':
            continue
        if a.get('campaign_id') in cbo_ids:
            continue
        if a.get('daily_budget'):
            total += int(a['daily_budget'])
    return total / offset


def test_pool_daily_total(adsets, offset):
    """測試池目前佔用的日預算（以名稱前綴辨識）"""
    total = 0
    for a in adsets.values():
        if a.get('effective_status') != 'ACTIVE':
            continue
        if TEST_POOL_TAG in (a.get('name') or '') and a.get('daily_budget'):
            total += int(a['daily_budget'])
    return total / offset


# ── 護欄檢查 ──────────────────────────────────────────────────────────────────
def classify(action, adsets, camps, offset, state):
    """回傳 ('auto'|'approval', 說明字串)，違反硬上限直接 raise Rejected"""
    op = action.get('op')
    if op not in AUTO_OPS and op not in APPROVAL_OPS:
        raise Rejected('未知動作 `{}`'.format(op))

    level_of = op.split('_', 1)[1]
    if op.startswith('pause_') or op.startswith('resume_'):
        pool = {'adset': adsets, 'campaign': camps}.get(level_of)
        if pool is not None and str(action.get('id')) not in pool:
            raise Rejected('找不到 {} `{}`'.format(level_of, action.get('id')))

    if op.startswith('pause_'):
        return 'auto', '關閉，不增加支出'

    if op in ('decrease_budget', 'increase_budget'):
        level = action.get('level', 'adset')
        obj   = (adsets if level == 'adset' else camps).get(str(action.get('id')))
        if obj is None:
            raise Rejected('找不到 {} `{}`'.format(level, action.get('id')))
        cur_minor = obj.get('daily_budget')
        if not cur_minor:
            raise Rejected('`{}` 沒有日預算（可能是總預算或 CBO 上層控預算），不可用此動作'
                           .format(action.get('id')))
        cur_amt = int(cur_minor) / offset
        new_amt = float(action.get('daily_budget'))
        if new_amt <= 0:
            raise Rejected('新預算必須大於 0')

        change_pct = abs(new_amt - cur_amt) / cur_amt * 100
        if change_pct > MAX_BUDGET_CHANGE_PCT:
            raise Rejected('調幅 {:.0f}% 超過上限 {}%（{:,.0f} → {:,.0f}）'
                           .format(change_pct, MAX_BUDGET_CHANGE_PCT, cur_amt, new_amt))

        if op == 'decrease_budget':
            if new_amt >= cur_amt:
                raise Rejected('decrease_budget 的新預算 {:,.0f} 沒有低於現值 {:,.0f}'
                               .format(new_amt, cur_amt))
            state['projected_total'] -= (cur_amt - new_amt)
            return 'auto', '{:,.0f} → {:,.0f}（-{:.0f}%）'.format(cur_amt, new_amt, change_pct)

        # increase_budget
        delta = new_amt - cur_amt
        if delta <= 0:
            raise Rejected('increase_budget 的新預算沒有高於現值')
        projected = state['projected_total'] + delta
        if ACCOUNT_DAILY_CAP and projected > ACCOUNT_DAILY_CAP:
            raise Rejected('執行後帳戶總日預算 {:,.0f} 會超過上限 {:,.0f}'
                           .format(projected, ACCOUNT_DAILY_CAP))
        state['projected_total'] = projected
        return 'approval', '{:,.0f} → {:,.0f}（+{:.0f}%）'.format(cur_amt, new_amt, change_pct)

    if op.startswith('resume_'):
        return 'approval', '重新啟用，會增加支出'

    if op == 'create_adset':
        budget = float(action.get('daily_budget') or 0)
        if budget <= 0:
            raise Rejected('create_adset 必須指定 daily_budget')
        is_test   = TEST_POOL_TAG in (action.get('name') or '')
        projected = state['projected_total'] + budget
        if ACCOUNT_DAILY_CAP and projected > ACCOUNT_DAILY_CAP:
            raise Rejected('執行後帳戶總日預算 {:,.0f} 會超過上限 {:,.0f}'
                           .format(projected, ACCOUNT_DAILY_CAP))
        if is_test:
            if not TEST_POOL_DAILY_CAP:
                raise Rejected('測試池未設定 ADS_TEST_POOL_DAILY_CAP，不准自動建案')
            projected_test = state['projected_test'] + budget
            if projected_test > TEST_POOL_DAILY_CAP:
                raise Rejected('測試池會達 {:,.0f}，超過每日上限 {:,.0f}'
                               .format(projected_test, TEST_POOL_DAILY_CAP))
            state['projected_total'] = projected
            state['projected_test']  = projected_test
            return 'auto', '測試池內新建，池用量 {:,.0f}/{:,.0f}'.format(
                projected_test, TEST_POOL_DAILY_CAP)
        state['projected_total'] = projected
        return 'approval', '池外新建 ad set，日預算 {:,.0f}'.format(budget)

    if op == 'create_ad':
        return 'approval', '新增素材'

    raise Rejected('未處理的動作 `{}`'.format(op))


# ── 執行 ──────────────────────────────────────────────────────────────────────
def execute(action, offset):
    op  = action['op']
    oid = str(action['id']) if action.get('id') else None

    if op.startswith('pause_'):
        return api_post(oid, {'status': 'PAUSED'})
    if op.startswith('resume_'):
        return api_post(oid, {'status': 'ACTIVE'})
    if op in ('decrease_budget', 'increase_budget'):
        minor = int(round(float(action['daily_budget']) * offset))
        return api_post(oid, {'daily_budget': minor})
    if op == 'create_adset':
        params = dict(action.get('payload') or {})
        params['name']         = action['name']
        params['daily_budget'] = int(round(float(action['daily_budget']) * offset))
        params['status']       = 'PAUSED'   # 新建一律 PAUSED，開啟是另一個需核可動作
        return api_post('{}/adsets'.format(AD_ACCOUNT), params)
    if op == 'create_ad':
        params = dict(action.get('payload') or {})
        params['name']   = action['name']
        params['status'] = 'PAUSED'
        return api_post('{}/ads'.format(AD_ACCOUNT), params)
    raise Rejected('execute 不支援 `{}`'.format(op))


def log(entry):
    if not os.path.isdir(ADS_DIR):
        os.makedirs(ADS_DIR)
    entry['at'] = P.tw_now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_PATH, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + '\n')


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    args     = sys.argv[1:]
    execute_ = '--execute' in args
    approved = '--approved' in args

    if '--plan' not in args:
        print('ERROR: 需要 --plan <file.json>')
        sys.exit(1)
    plan_path = args[args.index('--plan') + 1]

    if os.path.exists(plan_path):
        with open(plan_path, 'r', encoding='utf-8') as fh:
            plan = json.load(fh)
    else:
        plan = json.loads(plan_path)   # 允許直接傳 JSON 字串（workflow_dispatch 用）
    if isinstance(plan, dict):
        plan = plan.get('actions', [])

    if not TOKEN or not AD_ACCOUNT:
        print('ERROR: 缺 META_ADS_TOKEN 或 AD_ACCOUNT_ID')
        sys.exit(1)
    if ACCOUNT_DAILY_CAP is None:
        print('ERROR: 未設定 ADS_ACCOUNT_DAILY_CAP（帳戶總日預算上限）。'
              '沒有天花板就不准動任何預算。')
        sys.exit(1)

    cur, offset, adsets, camps = load_live_state()
    state = {
        'projected_total': active_daily_total(adsets, camps, offset),
        'projected_test':  test_pool_daily_total(adsets, offset),
    }

    print('帳戶 {}　幣別 {}'.format(AD_ACCOUNT, cur))
    print('目前投放中總日預算：{} {:,.0f}　上限 {:,.0f}'.format(
        cur, state['projected_total'], ACCOUNT_DAILY_CAP))
    if TEST_POOL_DAILY_CAP:
        print('測試池用量：{} {:,.0f} / {:,.0f}'.format(
            cur, state['projected_test'], TEST_POOL_DAILY_CAP))
    print('模式：{}{}'.format('EXECUTE' if execute_ else 'DRY-RUN',
                              '（含核可動作）' if approved else ''))
    print('-' * 66)

    done = skipped = failed = 0
    for i, action in enumerate(plan, 1):
        label = '{}. {} `{}`'.format(i, action.get('op'), action.get('id') or action.get('name', ''))
        try:
            kind, note = classify(action, adsets, camps, offset, state)
        except Rejected as e:
            print('{}\n   ✗ 拒絕：{}'.format(label, e))
            log({'action': action, 'result': 'rejected', 'detail': str(e)})
            failed += 1
            continue

        need_ok = (kind == 'approval')
        mark    = '需核可' if need_ok else '自動'
        print('{}\n   [{}] {}'.format(label, mark, note))
        if action.get('reason'):
            print('   理由：{}'.format(action['reason']))

        if need_ok and not approved:
            print('   → 略過（等你放行）')
            log({'action': action, 'result': 'awaiting_approval', 'detail': note})
            skipped += 1
            continue
        if not execute_:
            print('   → dry-run，未實際執行')
            continue

        try:
            resp = execute(action, offset)
            print('   ✓ 已執行 {}'.format(json.dumps(resp, ensure_ascii=False)[:100]))
            log({'action': action, 'result': 'executed', 'detail': note, 'response': resp})
            done += 1
            time.sleep(0.4)
        except Exception as e:
            print('   ✗ 執行失敗：{}'.format(e))
            log({'action': action, 'result': 'failed', 'detail': str(e)})
            failed += 1

    print('-' * 66)
    print('完成 {}　等核可 {}　失敗/拒絕 {}'.format(done, skipped, failed))
    print('執行後預估總日預算：{} {:,.0f}'.format(cur, state['projected_total']))
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
