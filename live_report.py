# -*- coding: utf-8 -*-
"""
直播成效報告 — 直播結束後執行，自動抓取最近一場直播的完整數據
Usage: python live_report.py
"""
from __future__ import print_function
import json, os, sys, io, datetime
import time

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

TOKEN = os.environ.get('META_TOKEN', '')
FB_PAGE = os.environ.get('FB_PAGE_ID', '1627804834169159')
API_BASE = 'https://graph.facebook.com/v19.0'

if not TOKEN:
    try:
        import config
        TOKEN = config.TOKEN
        FB_PAGE = getattr(config, 'FB_PAGE', FB_PAGE)
    except ImportError:
        print("ERROR: 找不到 META_TOKEN，也找不到 config.py")
        sys.exit(1)

def api_get(path, params=None):
    if params is None: params = {}
    params['access_token'] = TOKEN
    url = path if path.startswith('http') else '{}/{}'.format(API_BASE, path)
    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()
    if 'error' in data:
        raise RuntimeError('{} (code {})'.format(
            data['error'].get('message', ''), data['error'].get('code', '')))
    return data

def fmt_num(n):
    n = n or 0
    if n >= 1000000: return '{:.2f}M'.format(n/1000000)
    if n >= 1000:    return '{:.1f}K'.format(n/1000)
    return str(int(n))

def fmt_dur(seconds):
    if not seconds: return '—'
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h: return '{}h {}m'.format(h, m)
    return '{}m {}s'.format(m, s)

def bar(pct, width=20):
    filled = int(round(pct / 100 * width))
    return '█' * filled + '░' * (width - filled)

print()
print('=' * 50)
print('  📡 泰熙爾 直播成效報告')
print('  {}'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M')))
print('=' * 50)

# ── 步驟 1：抓最近直播列表 ────────────────────────────────
print('\n[1] 抓取直播列表...')
try:
    resp = api_get('{}/live_videos'.format(FB_PAGE), {
        'fields': 'id,title,description,status,live_views,video,broadcast_start_time,broadcast_end_time,permalink_url',
        'limit': 10
    })
except Exception as e:
    print('ERROR: {}'.format(e))
    sys.exit(1)

all_lives = resp.get('data', [])
print('  找到 {} 筆直播記錄'.format(len(all_lives)))

# 找已結束的（VOD）
vod = [l for l in all_lives if l.get('status') == 'VOD']
live_now = [l for l in all_lives if l.get('status') == 'LIVE']

if live_now:
    print('  ⚠️  目前仍有直播進行中，部分數據尚未定案')

if not vod:
    print('\n  找不到已結束的直播，可能還沒結束，請稍後再試。')
    print('  目前狀態:', [l.get('status') for l in all_lives])
    sys.exit(0)

live = vod[0]  # 最新那場
title = live.get('title') or live.get('description', '')[:40] or '（無標題）'

# ── 計算直播時長 ──────────────────────────────────────────
start_str = live.get('broadcast_start_time', '')
end_str   = live.get('broadcast_end_time', '')
duration_sec = None
if start_str and end_str:
    try:
        def parse_ts(s):
            s = s.replace('+0000','+00:00')
            for fmt in ('%Y-%m-%dT%H:%M:%S+00:00','%Y-%m-%dT%H:%M:%S'):
                try: return datetime.datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')
                except: pass
            return None
        s = parse_ts(start_str)
        e = parse_ts(end_str)
        if s and e:
            duration_sec = (e - s).total_seconds()
    except: pass

print('\n' + '─' * 50)
print('  直播名稱: {}'.format(title[:45]))
print('  開始時間: {}'.format(start_str[:16].replace('T',' ') if start_str else '—'))
print('  時長:     {}'.format(fmt_dur(duration_sec)))
print('─' * 50)

# ── 步驟 2：直播即時數字（峰值在線）─────────────────────
peak_viewers = live.get('live_views') or 0
print('\n[2] 直播即時數據')
print('  🔴 峰值同時在線人數: {}'.format(fmt_num(peak_viewers)))

# ── 步驟 3：抓影片 insights ───────────────────────────────
video_obj = live.get('video') or {}
video_id  = video_obj.get('id')

insights = {}
social    = {}

if video_id:
    print('\n[3] 抓取影片 insights (ID: {})...'.format(video_id))
    time.sleep(1)
    try:
        ins_resp = api_get('{}/video_insights'.format(video_id), {
            'metric': ','.join([
                'total_video_views',
                'total_video_views_unique',
                'total_video_avg_time_watched',
                'total_video_complete_views_unique',
                'total_video_10s_views_unique',
                'post_video_social_actions',
            ])
        })
        for d in ins_resp.get('data', []):
            vals = d.get('values') or [{}]
            insights[d['name']] = vals[0].get('value')
        social = insights.get('post_video_social_actions') or {}
    except Exception as e:
        print('  Insights 錯誤: {}'.format(e))
        print('  （可能需要再等一段時間讓平台計算完成）')
else:
    print('\n[3] 找不到對應 video_id，跳過 insights')

# ── 步驟 4：呈現報告 ──────────────────────────────────────
total_views  = insights.get('total_video_views') or 0
unique_views = insights.get('total_video_views_unique') or 0
avg_watch_ms = insights.get('total_video_avg_time_watched') or 0
complete_uniq= insights.get('total_video_complete_views_unique') or 0
views_10s    = insights.get('total_video_10s_views_unique') or 0
comments     = social.get('COMMENT') or 0
shares       = social.get('SHARE') or 0
likes        = (social.get('LIKE') or 0) + (social.get('LOVE') or 0) + (social.get('WOW') or 0)

print()
print('╔' + '═'*48 + '╗')
print('║  📊 直播成效完整報告' + ' '*28 + '║')
print('╠' + '═'*48 + '╣')
print('║  觀看人數                                      ║')
print('║    👁  總觀看次數（含重複）  {:>10}         ║'.format(fmt_num(total_views)))
print('║    👤 獨立觀看人數           {:>10}         ║'.format(fmt_num(unique_views)))
print('║    🔴 直播峰值同時在線       {:>10}         ║'.format(fmt_num(peak_viewers)))
if unique_views > 0 and peak_viewers > 0:
    live_ratio = min(round(peak_viewers / unique_views * 100), 100)
    replay_ratio = 100 - live_ratio
    print('║    📡 直播{}% vs 回放{}%  {}  ║'.format(
        live_ratio, replay_ratio,
        bar(live_ratio, 16)
    ))
print('╠' + '═'*48 + '╣')
print('║  觀看深度                                      ║')
if avg_watch_ms:
    avg_sec = avg_watch_ms / 1000
    print('║    ⏱  平均觀看時長           {:>10}         ║'.format(fmt_dur(avg_sec)))
    if duration_sec and duration_sec > 0:
        depth_pct = min(round(avg_sec / duration_sec * 100), 100)
        print('║    📈 平均看了直播的 {}%    {}           ║'.format(
            str(depth_pct).rjust(3),
            bar(depth_pct, 12)
        ))
if views_10s:
    print('║    👀 看超過10秒人數          {:>10}         ║'.format(fmt_num(views_10s)))
if complete_uniq:
    print('║    ✅ 看完整場人數            {:>10}         ║'.format(fmt_num(complete_uniq)))
print('╠' + '═'*48 + '╣')
print('║  互動                                          ║')
print('║    💬 留言數                 {:>10}         ║'.format(fmt_num(comments)))
print('║    🔁 分享數                 {:>10}         ║'.format(fmt_num(shares)))
print('║    ❤️  反應數                 {:>10}         ║'.format(fmt_num(likes)))
if unique_views > 0:
    if comments: print('║    💬 留言率  {:.2f}%（每百人留言）              ║'.format(comments/unique_views*100))
    if shares:   print('║    🔁 分享率  {:.2f}%                           ║'.format(shares/unique_views*100))
print('╠' + '═'*48 + '╣')

# ── 評語 ──────────────────────────────────────────────────
verdicts = []
if unique_views >= 10000:
    verdicts.append('🔥 破萬觀看，流量強')
elif unique_views >= 5000:
    verdicts.append('✅ 5千以上，正常水準')
elif unique_views >= 1000:
    verdicts.append('📊 千人觀看，持續優化')
if unique_views > 0 and comments / unique_views * 100 >= 2:
    verdicts.append('💬 留言率高，互動熱')
if unique_views > 0 and shares / unique_views * 100 >= 0.5:
    verdicts.append('🔁 分享率不錯，有傳播力')
if peak_viewers > 0 and unique_views > 0:
    if peak_viewers / unique_views * 100 < 20:
        verdicts.append('📡 大多數是回放觀看，考慮更好的直播時段')
    else:
        verdicts.append('📡 即時觀看比例健康')

print('║  結論                                          ║')
for v in verdicts:
    line = '║    {}  '.format(v)
    line = line.ljust(49) + '║'
    print(line)
if not verdicts:
    print('║    資料不足，無法給出結論                      ║')

print('╚' + '═'*48 + '╝')

if live.get('permalink_url'):
    print('\n🔗 直播連結: {}'.format(live['permalink_url']))
print()
