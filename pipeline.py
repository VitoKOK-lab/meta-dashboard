# -*- coding: utf-8 -*-
"""
泰熙爾札娜 IP 儀表板 - 資料抓取與 HTML 生成
Usage:
  python pipeline.py          自動判斷（首次=全量，之後=增量）
  python pipeline.py --full   強制全量掃描

Token 讀取順序：
  1. 環境變數 META_TOKEN（GitHub Actions 用）
  2. 本機 config.py（本地測試用）
"""
from __future__ import print_function
import json
import os
import re
import sys
import io
import time
import datetime

# Windows 終端機強制 UTF-8
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

# ── Token（環境變數優先，本機 fallback 到 config.py）────────────────────────
TOKEN = os.environ.get('META_TOKEN', '')
FB_PAGE    = os.environ.get('FB_PAGE_ID', '1627804834169159')
IG_ACCOUNT = os.environ.get('IG_ACCOUNT_ID', '17841456817621335')

if not TOKEN:
    try:
        import config
        TOKEN     = config.TOKEN
        FB_PAGE   = getattr(config, 'FB_PAGE',    FB_PAGE)
        IG_ACCOUNT = getattr(config, 'IG_ACCOUNT', IG_ACCOUNT)
    except ImportError:
        print("ERROR: 找不到 META_TOKEN 環境變數，也找不到 config.py")
        sys.exit(1)

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_PATH     = os.path.join(BASE_DIR, 'data', 'videos.json')
TEMPLATE_PATH = os.path.join(BASE_DIR, 'template.html')
OUTPUT_PATH   = os.path.join(BASE_DIR, 'index.html')
API_BASE      = 'https://graph.facebook.com/v19.0'

INSIGHTS_REFRESH_DAYS = 28   # 每天：更新 28 天內的 insights
INSIGHTS_WEEKLY_DAYS  = 90   # 每週一：更新 90 天內的 insights
HTML_EMBED_DAYS       = 90

# ── 時區（台灣 UTC+8）────────────────────────────────────────────────────────
TW_HOURS = 8

def tw_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=TW_HOURS)

def utc_now():
    return datetime.datetime.utcnow()

def to_tw_date(iso_str):
    if not iso_str:
        return ''
    s = iso_str.strip()
    for suffix in ('+00:00', '+0000', 'Z'):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.datetime.strptime(s[:19], fmt)
            return (dt + datetime.timedelta(hours=TW_HOURS)).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return s[:10]

def days_ago_from(iso_str):
    if not iso_str:
        return 9999
    s = iso_str.strip().replace('Z', '').split('+')[0][:19]
    try:
        dt = datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        try:
            dt = datetime.datetime.strptime(s[:10], '%Y-%m-%d')
        except ValueError:
            return 9999
    return max((utc_now() - dt).days, 0)

def tw_yesterday():
    return (tw_now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

# ── JSON 資料庫 ───────────────────────────────────────────────────────────────
def load_db():
    """讀取 data/videos.json，回傳 {id: video_dict}"""
    if not os.path.exists(DATA_PATH):
        return {}
    try:
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('videos', {})
    except Exception:
        return {}

def save_db(videos_dict):
    """把 {id: video_dict} 存回 data/videos.json"""
    data_dir = os.path.dirname(DATA_PATH)
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    payload = {
        'updated_at': utc_now().isoformat(),
        'count': len(videos_dict),
        'videos': videos_dict,
    }
    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
    size_kb = os.path.getsize(DATA_PATH) // 1024
    print('  data/videos.json 已儲存 {} 支 ({} KB)'.format(len(videos_dict), size_kb))

def get_stale_ids(videos_dict):
    """回傳需要更新 insights 的 (id, platform) 清單"""
    today = utc_now().strftime('%Y-%m-%d')
    stale = []
    for vid_id, v in videos_dict.items():
        age = days_ago_from(v.get('created_time', ''))
        if age <= INSIGHTS_REFRESH_DAYS:
            last = v.get('insights_at', '')[:10]
            if last < today:
                stale.append((vid_id, v.get('platform', 'fb')))
    return stale

def get_recent(videos_dict, days=HTML_EMBED_DAYS):
    """回傳最近 N 天的影片 list，按時間降冪"""
    result = []
    for v in videos_dict.values():
        if days_ago_from(v.get('created_time', '')) <= days:
            result.append(v)
    result.sort(key=lambda x: x.get('created_time', ''), reverse=True)
    return result

# ── API ───────────────────────────────────────────────────────────────────────
def api_get(path, params=None):
    if params is None:
        params = {}
    params['access_token'] = TOKEN
    url = path if path.startswith('http') else '{}/{}'.format(API_BASE, path)
    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()
    if 'error' in data:
        raise RuntimeError('{} (code {})'.format(
            data['error'].get('message', ''), data['error'].get('code', '')))
    return data

def batch_api(req_list):
    results = []
    for i in range(0, len(req_list), 50):
        chunk = req_list[i:i+50]
        batch = json.dumps([{'method': 'GET', 'relative_url': r} for r in chunk])
        resp = requests.post(
            '{}/'.format(API_BASE),
            data={'batch': batch, 'access_token': TOKEN, 'include_headers': 'false'},
            timeout=60
        )
        for item in resp.json():
            try:
                results.append(json.loads(item['body']) if item.get('code') == 200 else None)
            except Exception:
                results.append(None)
        if i + 50 < len(req_list):
            time.sleep(0.5)
    return results

# ── 內容類型 ──────────────────────────────────────────────────────────────────
COMMERCE_RE = re.compile(
    u'留言[「\u300e\u300a\u300c\\[]|[*\uff0a]|優惠|寵粉|限時|折扣|團購|秒殺|限量|搶購|下單|特賣|特惠'
)

def detect_type(caption):
    if not caption:
        return 'traffic'
    body = re.split(r'(?:^|\n)\s*#', caption, maxsplit=1)[0]
    return 'commerce' if COMMERCE_RE.search(body) else 'traffic'

# ── Insights 解析 ─────────────────────────────────────────────────────────────
def parse_fb_insights(data):
    m = {}
    for d in (data.get('data') or []):
        vals = d.get('values') or [{}]
        m[d['name']] = vals[0].get('value') if vals else None
    social    = m.get('post_video_social_actions') or {}
    likes_map = m.get('post_video_likes_by_reaction_type') or {}
    retention = m.get('post_video_retention_graph') or {}
    ret_vals  = list(retention.values()) if isinstance(retention, dict) else []
    return {
        'plays':           m.get('blue_reels_play_count') or 0,
        'reach':           m.get('post_impressions_unique') or 0,
        'avg_watch_ms':    m.get('post_video_avg_time_watched') or 0,
        'total_view_ms':   m.get('post_video_view_time') or 0,
        'shares':          social.get('SHARE') or 0,
        'comments':        social.get('COMMENT') or 0,
        'likes':           sum(likes_map.values()) if likes_map else 0,
        'new_followers':   m.get('post_video_followers') or 0,
        'retention':       ret_vals,
        'completion_rate': ret_vals[-1] if ret_vals else None,
    }

def parse_ig_insights(data):
    m = {}
    for d in (data.get('data') or []):
        vals = d.get('values') or [{}]
        m[d['name']] = vals[0].get('value') if vals else None
    avg_watch  = m.get('ig_reels_avg_watch_time') or 0
    total_view = m.get('ig_reels_video_view_total_time') or 0
    plays = int(round(total_view / avg_watch)) if avg_watch > 0 else 0
    return {
        'plays':           plays,
        'reach':           m.get('reach') or 0,
        'shares':          m.get('shares') or 0,
        'comments':        m.get('comments') or 0,
        'likes':           m.get('likes') or 0,
        'saved':           m.get('saved') or 0,
        'avg_watch_ms':    avg_watch,
        'total_view_ms':   total_view,
        'completion_rate': None,
        'retention':       [],
    }

# ── 評分 ──────────────────────────────────────────────────────────────────────
def score_video(v, avg_fb=5000, avg_ig=3000):
    plays = v.get('plays') or 0
    if plays == 0:
        return 0
    share_rate   = (v.get('shares') or 0) / plays
    comment_rate = (v.get('comments') or 0) / plays
    avg   = avg_fb if v.get('platform') == 'fb' else avg_ig
    reach = v.get('reach') or 0
    dev   = (reach - avg) / avg if avg > 0 else 0
    dev_score = min(max((dev + 0.5) / 1.5, 0), 1)
    if v.get('type') == 'traffic':
        raw = (min(share_rate/0.03, 1)*0.40
             + dev_score*0.35
             + min(comment_rate/0.015, 1)*0.25)
    else:
        raw = (min(comment_rate/0.02, 1)*0.45
             + dev_score*0.40
             + min(share_rate/0.02, 1)*0.15)
    cr = v.get('completion_rate')
    if cr is not None:
        raw += cr * 0.05
    return min(int(round(raw * 100)), 100)

def compute_averages(videos_list):
    fb_r = [v.get('reach') or 0 for v in videos_list if v.get('platform') == 'fb' and (v.get('reach') or 0) > 0]
    ig_r = [v.get('reach') or 0 for v in videos_list if v.get('platform') == 'ig' and (v.get('reach') or 0) > 0]
    return (
        sum(fb_r)/len(fb_r) if fb_r else 5000.0,
        sum(ig_r)/len(ig_r) if ig_r else 3000.0,
    )

# ── 抓影片列表 ────────────────────────────────────────────────────────────────
def fetch_video_list(platform, since_days=7):
    now_ts = int(time.time())
    videos = []
    cursor = None
    page   = 0
    while True:
        try:
            if cursor:
                data = api_get(cursor)
            elif platform == 'fb':
                data = api_get('{}/videos'.format(FB_PAGE), {
                    'fields': 'id,description,created_time,length',
                    'since': now_ts - since_days*86400,
                    'until': now_ts + 86400, 'limit': 100,
                })
            else:
                data = api_get('{}/media'.format(IG_ACCOUNT), {
                    'fields': 'id,caption,media_type,timestamp',
                    'since': now_ts - since_days*86400,
                    'until': now_ts + 86400, 'limit': 100,
                })
        except RuntimeError as e:
            print('    {} 列表錯誤: {}'.format(platform.upper(), e))
            break
        items = data.get('data') or []
        if platform == 'ig':
            # IG：只要影片，且必須有說明文字
            items = [v for v in items
                     if v.get('media_type') == 'VIDEO'
                     and (v.get('caption') or '').strip()]
        for item in items:
            ts  = item.get('created_time') or item.get('timestamp', '')
            cap = item.get('description') or item.get('caption') or ''
            cap = cap.strip()
            # FB：跳過沒有說明文字的影片（直播、純影片等）
            if platform == 'fb' and not cap:
                continue
            # FB：只要短影音（120 秒以下 = 2 分鐘），過濾直播或長影片
            length = item.get('length')
            if platform == 'fb' and length and length > 120:
                continue
            videos.append({
                'id':           item['id'],
                'platform':     platform,
                'title':        cap[:300],
                'created_time': ts,
                'created_date': to_tw_date(ts),
                'type':         detect_type(cap),
                'length_sec':   length,
            })
        cursor = (data.get('paging') or {}).get('next')
        page += 1
        print('    {} 頁{} → {}支'.format(platform.upper(), page, len(items)))
        if not cursor or not items:
            break
        time.sleep(0.3)
    return videos

# ── 抓 Insights ───────────────────────────────────────────────────────────────
def fetch_insights_for(stale_list):
    """stale_list: [(id, platform), ...]，回傳 {id: parsed_insights}"""
    fb_ids = [r[0] for r in stale_list if r[1] == 'fb']
    ig_ids = [r[0] for r in stale_list if r[1] == 'ig']
    results = {}
    if fb_ids:
        print('  [FB] 更新 insights ({} 支)...'.format(len(fb_ids)))
        raw = batch_api(['{}/video_insights'.format(vid) for vid in fb_ids])
        for i, vid in enumerate(fb_ids):
            results[vid] = parse_fb_insights(raw[i]) if raw[i] else {}
    if ig_ids:
        print('  [IG] 更新 insights ({} 支)...'.format(len(ig_ids)))
        time.sleep(1.5)
        raw = batch_api([
            '{}/insights?metric=reach,shares,comments,likes,saved,'
            'ig_reels_avg_watch_time,ig_reels_video_view_total_time'.format(vid)
            for vid in ig_ids
        ])
        for i, vid in enumerate(ig_ids):
            results[vid] = parse_ig_insights(raw[i]) if raw[i] else {}
    return results

# ── HTML 生成 ─────────────────────────────────────────────────────────────────
def to_js_video(v):
    title = v.get('title') or ''
    return {
        'id':             v['id'],
        'platform':       v['platform'],
        'type':           v.get('type', 'traffic'),
        'description':    title,
        'caption':        title,
        'created_time':   v.get('created_time', ''),
        'created_date':   v.get('created_date', ''),
        'length':         v.get('length_sec'),
        'plays':          v.get('plays', 0),
        'reach':          v.get('reach', 0),
        'shares':         v.get('shares', 0),
        'comments':       v.get('comments', 0),
        'likes':          v.get('likes', 0),
        'saved':          v.get('saved', 0),
        'avgWatchMs':     v.get('avg_watch_ms', 0),
        'totalViewMs':    v.get('total_view_ms', 0),
        'newFollowers':   v.get('new_followers', 0),
        'completionRate': v.get('completion_rate'),
        'retention':      v.get('retention') or [],
        'score':          v.get('score', 0),
    }

def generate_html(recent_videos, avg_fb, avg_ig):
    if not os.path.exists(TEMPLATE_PATH):
        print('ERROR: 找不到 template.html')
        sys.exit(1)
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        template = f.read()
    snapshot_date = tw_yesterday()
    payload = {
        'generated_at':  tw_now().strftime('%Y-%m-%d %H:%M'),
        'snapshot_date': snapshot_date,
        'avg_fb':        round(avg_fb, 1),
        'avg_ig':        round(avg_ig, 1),
        'videos':        [to_js_video(v) for v in recent_videos],
    }
    html = template.replace('__STATIC_DATA__', json.dumps(payload, ensure_ascii=False))
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(html)
    size_kb = os.path.getsize(OUTPUT_PATH) // 1024
    print('  index.html → {} KB（嵌入 {} 支）'.format(size_kb, len(recent_videos)))

# ── 主程式 ────────────────────────────────────────────────────────────────────
def main():
    force_full   = '--full'   in sys.argv
    force_weekly = '--weekly' in sys.argv
    refresh_days = INSIGHTS_WEEKLY_DAYS if force_weekly else INSIGHTS_REFRESH_DAYS
    print('=' * 50)
    print('  泰熙爾札娜 IP 儀表板 pipeline')
    print('  {}'.format(tw_now().strftime('%Y-%m-%d %H:%M')))
    print('=' * 50)

    videos_dict = load_db()
    is_first = len(videos_dict) == 0 or force_full
    fetch_days = 90 if is_first else 7
    if force_full:
        videos_dict = {}  # 清空，從頭重建

    if is_first:
        print('\n[全量模式] 抓取近 90 天...')
    else:
        print('\n[增量模式] DB {} 支 → 抓新影片（7天）+ 更新近{}天 insights'.format(
            len(videos_dict), INSIGHTS_REFRESH_DAYS))

    # 步驟 1：抓新影片
    print('\n[1] 影片列表（{}天）...'.format(fetch_days))
    new_count = 0
    fb_list = fetch_video_list('fb', since_days=fetch_days)
    time.sleep(1.5)
    ig_list = fetch_video_list('ig', since_days=fetch_days)
    for v in fb_list + ig_list:
        if v['id'] not in videos_dict:
            videos_dict[v['id']] = v
            new_count += 1
        else:
            # 更新基本資料（標題可能改過）
            videos_dict[v['id']].update({
                'title': v['title'],
                'type':  v['type'],
                'length_sec': v['length_sec'],
            })
    print('  新影片: {} 支'.format(new_count))

    # 步驟 2：更新 insights
    print('\n[2] 更新 Insights...')
    stale = get_stale_ids(videos_dict, refresh_days=refresh_days)
    print('  需更新: {} 支（{}天內）'.format(len(stale), refresh_days))
    if stale:
        insights_map = fetch_insights_for(stale)
        now_iso = utc_now().isoformat()
        for vid_id, ins in insights_map.items():
            if ins and vid_id in videos_dict:
                videos_dict[vid_id].update(ins)
                videos_dict[vid_id]['insights_at'] = now_iso

    # 步驟 3：計算評分
    print('\n[3] 計算評分...')
    recent = get_recent(videos_dict, days=HTML_EMBED_DAYS)
    avg_fb, avg_ig = compute_averages(recent)
    print('  平均觸及 FB={:.0f}  IG={:.0f}'.format(avg_fb, avg_ig))
    stale_ids = set(r[0] for r in stale)
    for v in videos_dict.values():
        if v['id'] in stale_ids or is_first:
            v['score'] = score_video(v, avg_fb, avg_ig)

    # 儲存 JSON
    save_db(videos_dict)

    # 步驟 4：生成 HTML
    print('\n[4] 生成 index.html...')
    recent = get_recent(videos_dict, days=HTML_EMBED_DAYS)
    generate_html(recent, avg_fb, avg_ig)

    print('\n完成！DB:{} 支 / HTML:{} 支'.format(len(videos_dict), len(recent)))

if __name__ == '__main__':
    main()
