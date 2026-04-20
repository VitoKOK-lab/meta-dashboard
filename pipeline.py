# -*- coding: utf-8 -*-
"""
泰熙爾札娜 IP 儀表板 - 資料抓取與 HTML 生成
Usage:
  python pipeline.py          ← 自動判斷（首次=全量，之後=增量）
  python pipeline.py --full   ← 強制全量掃描（重建資料庫）

增量模式邏輯：
  - 只抓 7 天內新影片
  - 只更新 28 天內影片的 insights（數字還在變動）
  - 28 天以上：直接沿用 SQLite 舊資料
  - HTML 只嵌入最近 90 天（大小固定）
"""
from __future__ import print_function
import json
import os
import re
import sqlite3
import sys
import io
import time
import datetime

# Windows 終端機強制使用 UTF-8 輸出
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

try:
    import requests
except ImportError:
    print("ERROR: 請先安裝 requests: pip install requests")
    sys.exit(1)

import config

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_PATH       = os.path.join(BASE_DIR, 'data', 'videos.db')
TEMPLATE_PATH = os.path.join(BASE_DIR, 'template.html')
OUTPUT_PATH   = os.path.join(BASE_DIR, 'index.html')
API_BASE      = 'https://graph.facebook.com/v19.0'

# 28 天內的影片 insights 每天更新；超過則沿用快取
INSIGHTS_REFRESH_DAYS = 28
# HTML 只嵌入最近幾天的資料（排行按鈕最大值）
HTML_EMBED_DAYS = 90

# ── 時區（台灣 UTC+8）────────────────────────────────────────────────────────
TW_HOURS = 8

def utc_now():
    return datetime.datetime.utcnow()

def tw_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=TW_HOURS)

def to_tw_date(iso_str):
    """ISO timestamp → 台灣日期字串 YYYY-MM-DD"""
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
            tw = dt + datetime.timedelta(hours=TW_HOURS)
            return tw.strftime('%Y-%m-%d')
        except ValueError:
            continue
    return s[:10]

def days_ago(iso_str):
    """影片距今幾天（UTC）"""
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
    return (utc_now() - dt).days

def tw_yesterday():
    return (tw_now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

# ── 資料庫 ────────────────────────────────────────────────────────────────────
def init_db(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            id              TEXT PRIMARY KEY,
            platform        TEXT,
            type            TEXT,
            title           TEXT,
            created_time    TEXT,
            created_date    TEXT,
            length_sec      REAL,
            plays           INTEGER DEFAULT 0,
            reach           INTEGER DEFAULT 0,
            shares          INTEGER DEFAULT 0,
            comments        INTEGER DEFAULT 0,
            likes           INTEGER DEFAULT 0,
            saved           INTEGER DEFAULT 0,
            avg_watch_ms    INTEGER DEFAULT 0,
            total_view_ms   INTEGER DEFAULT 0,
            new_followers   INTEGER DEFAULT 0,
            completion_rate REAL,
            retention       TEXT,
            score           INTEGER DEFAULT 0,
            insights_at     TEXT,
            updated_at      TEXT
        )
    ''')
    # 補欄位（舊版 DB 升級用）
    try:
        conn.execute('ALTER TABLE videos ADD COLUMN insights_at TEXT')
    except Exception:
        pass
    conn.commit()

def upsert_video(conn, v, update_insights=True):
    """新影片 insert；已有影片視 update_insights 決定是否更新數字欄位"""
    now_iso = utc_now().isoformat()
    if update_insights:
        conn.execute('''
            INSERT OR REPLACE INTO videos
                (id, platform, type, title, created_time, created_date, length_sec,
                 plays, reach, shares, comments, likes, saved,
                 avg_watch_ms, total_view_ms, new_followers,
                 completion_rate, retention, score, insights_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            v['id'], v['platform'], v.get('type','traffic'), v.get('title',''),
            v.get('created_time',''), v.get('created_date',''), v.get('length_sec'),
            v.get('plays',0), v.get('reach',0), v.get('shares',0),
            v.get('comments',0), v.get('likes',0), v.get('saved',0),
            v.get('avg_watch_ms',0), v.get('total_view_ms',0), v.get('new_followers',0),
            v.get('completion_rate'),
            json.dumps(v['retention']) if v.get('retention') else None,
            v.get('score',0), now_iso, now_iso
        ))
    else:
        # 只補基本資料（標題、日期），不動 insights
        conn.execute('''
            INSERT OR IGNORE INTO videos
                (id, platform, type, title, created_time, created_date, length_sec, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (
            v['id'], v['platform'], v.get('type','traffic'), v.get('title',''),
            v.get('created_time',''), v.get('created_date',''), v.get('length_sec'), now_iso
        ))
    conn.commit()

def get_existing_ids(conn):
    cur = conn.execute('SELECT id FROM videos')
    return set(row[0] for row in cur.fetchall())

def get_stale_ids(conn, refresh_days=INSIGHTS_REFRESH_DAYS):
    """回傳需要更新 insights 的影片 id（在更新期限內且今天尚未更新）"""
    cutoff_created = (utc_now() - datetime.timedelta(days=refresh_days)).isoformat()
    today = utc_now().strftime('%Y-%m-%d')
    cur = conn.execute('''
        SELECT id, platform FROM videos
        WHERE created_time >= ?
          AND (insights_at IS NULL OR insights_at < ?)
        ORDER BY created_time DESC
    ''', (cutoff_created, today))
    return cur.fetchall()

def load_recent_videos(conn, days=HTML_EMBED_DAYS):
    """讀取最近 N 天的影片供 HTML 嵌入"""
    cutoff = (utc_now() - datetime.timedelta(days=days)).isoformat()
    cur = conn.execute(
        'SELECT * FROM videos WHERE created_time >= ? ORDER BY created_time DESC',
        (cutoff,)
    )
    rows = cur.fetchall()
    result = []
    for row in rows:
        v = dict(row)
        if v.get('retention'):
            try:
                v['retention'] = json.loads(v['retention'])
            except Exception:
                v['retention'] = []
        else:
            v['retention'] = []
        result.append(v)
    return result

def db_video_count(conn):
    return conn.execute('SELECT COUNT(*) FROM videos').fetchone()[0]

# ── API ───────────────────────────────────────────────────────────────────────
def api_get(path, params=None):
    if params is None:
        params = {}
    params['access_token'] = config.TOKEN
    if path.startswith('http'):
        resp = requests.get(path, params=params, timeout=30)
    else:
        resp = requests.get('{}/{}'.format(API_BASE, path), params=params, timeout=30)
    data = resp.json()
    if 'error' in data:
        raise RuntimeError('{} (code {})'.format(
            data['error'].get('message',''), data['error'].get('code','')))
    return data

def batch_api(req_list):
    results = []
    for i in range(0, len(req_list), 50):
        chunk = req_list[i:i+50]
        batch = json.dumps([{'method':'GET','relative_url':r} for r in chunk])
        resp = requests.post(
            '{}/'.format(API_BASE),
            data={'batch':batch,'access_token':config.TOKEN,'include_headers':'false'},
            timeout=60
        )
        for item in resp.json():
            try:
                results.append(json.loads(item['body']) if item.get('code')==200 else None)
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

def compute_averages(videos):
    fb_r = [v.get('reach') or 0 for v in videos if v.get('platform')=='fb' and (v.get('reach') or 0)>0]
    ig_r = [v.get('reach') or 0 for v in videos if v.get('platform')=='ig' and (v.get('reach') or 0)>0]
    return (
        sum(fb_r)/len(fb_r) if fb_r else 5000.0,
        sum(ig_r)/len(ig_r) if ig_r else 3000.0
    )

# ── 抓影片列表 ────────────────────────────────────────────────────────────────
def fetch_video_list(platform, since_days=7):
    now_ts = int(time.time())
    since  = now_ts - since_days * 86400
    until  = now_ts + 86400
    videos = []
    cursor = None
    page   = 0

    while True:
        try:
            if cursor:
                data = api_get(cursor)
            elif platform == 'fb':
                data = api_get('{}/videos'.format(config.FB_PAGE), {
                    'fields': 'id,description,created_time,length',
                    'since': since, 'until': until, 'limit': 100
                })
            else:
                data = api_get('{}/media'.format(config.IG_ACCOUNT), {
                    'fields': 'id,caption,media_type,timestamp',
                    'since': since, 'until': until, 'limit': 100
                })
        except RuntimeError as e:
            print('    {} 列表錯誤: {}'.format(platform.upper(), e))
            break

        items = data.get('data') or []
        if platform == 'ig':
            items = [v for v in items if v.get('media_type') == 'VIDEO']

        for item in items:
            ts = item.get('created_time') or item.get('timestamp', '')
            cap = item.get('description') or item.get('caption') or ''
            videos.append({
                'id':           item['id'],
                'platform':     platform,
                'title':        cap[:300],
                'created_time': ts,
                'created_date': to_tw_date(ts),
                'type':         detect_type(cap),
                'length_sec':   item.get('length'),
            })

        cursor = (data.get('paging') or {}).get('next')
        page += 1
        print('    {} 頁{} → {}支'.format(platform.upper(), page, len(items)))
        if not cursor or not items:
            break
        time.sleep(0.3)

    return videos

# ── 抓 Insights ───────────────────────────────────────────────────────────────
def fetch_insights_for(video_rows):
    """video_rows: list of (id, platform) from DB stale query"""
    fb_ids = [r[0] for r in video_rows if r[1] == 'fb']
    ig_ids = [r[0] for r in video_rows if r[1] == 'ig']
    results = {}

    if fb_ids:
        print('  [FB] 更新 insights ({} 支)...'.format(len(fb_ids)))
        reqs = ['{}/video_insights'.format(vid) for vid in fb_ids]
        raw  = batch_api(reqs)
        for i, vid in enumerate(fb_ids):
            results[vid] = ('fb', parse_fb_insights(raw[i]) if raw[i] else {})

    if ig_ids:
        print('  [IG] 更新 insights ({} 支)...'.format(len(ig_ids)))
        time.sleep(1.5)
        reqs = [
            '{}/insights?metric=reach,shares,comments,likes,saved,'
            'ig_reels_avg_watch_time,ig_reels_video_view_total_time'.format(vid)
            for vid in ig_ids
        ]
        raw = batch_api(reqs)
        for i, vid in enumerate(ig_ids):
            results[vid] = ('ig', parse_ig_insights(raw[i]) if raw[i] else {})

    return results

# ── HTML 生成 ─────────────────────────────────────────────────────────────────
def to_js_video(v):
    title = v.get('title') or ''
    return {
        'id':             v['id'],
        'platform':       v['platform'],
        'type':           v.get('type','traffic'),
        'description':    title,
        'caption':        title,
        'created_time':   v.get('created_time',''),
        'created_date':   v.get('created_date',''),
        'length':         v.get('length_sec'),
        'plays':          v.get('plays',0),
        'reach':          v.get('reach',0),
        'shares':         v.get('shares',0),
        'comments':       v.get('comments',0),
        'likes':          v.get('likes',0),
        'saved':          v.get('saved',0),
        'avgWatchMs':     v.get('avg_watch_ms',0),
        'totalViewMs':    v.get('total_view_ms',0),
        'newFollowers':   v.get('new_followers',0),
        'completionRate': v.get('completion_rate'),
        'retention':      v.get('retention') or [],
        'score':          v.get('score',0),
    }

def generate_html(videos, avg_fb, avg_ig):
    if not os.path.exists(TEMPLATE_PATH):
        print('ERROR: 找不到 template.html: {}'.format(TEMPLATE_PATH))
        sys.exit(1)
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        template = f.read()

    dates = [v.get('created_date','') for v in videos if v.get('created_date')]
    snapshot_date = max(dates) if dates else tw_yesterday()
    generated_at  = tw_now().strftime('%Y-%m-%d %H:%M')

    payload = {
        'generated_at':  generated_at,
        'snapshot_date': snapshot_date,
        'avg_fb':        round(avg_fb, 1),
        'avg_ig':        round(avg_ig, 1),
        'videos':        [to_js_video(v) for v in videos],
    }
    json_str = json.dumps(payload, ensure_ascii=False)
    html = template.replace('__STATIC_DATA__', json_str)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(html)
    size_kb = os.path.getsize(OUTPUT_PATH) // 1024
    print('  index.html → {} KB（嵌入 {} 支影片）'.format(size_kb, len(videos)))

# ── 主程式 ────────────────────────────────────────────────────────────────────
def main():
    force_full = '--full' in sys.argv
    print('=' * 50)
    print('  泰熙爾札娜 IP 儀表板 pipeline')
    print('  {}'.format(tw_now().strftime('%Y-%m-%d %H:%M')))
    print('=' * 50)

    data_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    total_in_db = db_video_count(conn)
    is_first_run = (total_in_db == 0) or force_full

    if is_first_run:
        # ── 全量模式（首次或 --full）────────────────────────────────────────
        print('\n[全量模式] 抓取近 90 天所有影片...')
        fetch_days = 90
    else:
        # ── 增量模式 ─────────────────────────────────────────────────────────
        print('\n[增量模式] DB 已有 {} 支，只抓新影片（7天）+ 更新近 {} 天 insights'.format(
            total_in_db, INSIGHTS_REFRESH_DAYS))
        fetch_days = 7

    # 步驟 1：抓影片列表
    print('\n[1] 抓取影片列表（{}天）...'.format(fetch_days))
    existing_ids = get_existing_ids(conn)
    new_videos   = []

    fb_list = fetch_video_list('fb', since_days=fetch_days)
    time.sleep(1.5)
    ig_list = fetch_video_list('ig', since_days=fetch_days)

    for v in fb_list + ig_list:
        if v['id'] not in existing_ids:
            new_videos.append(v)
            upsert_video(conn, v, update_insights=False)  # 先存基本資料
    print('  新影片: {} 支'.format(len(new_videos)))

    # 步驟 2：決定哪些影片需要更新 insights
    print('\n[2] 更新 Insights...')
    stale = get_stale_ids(conn, refresh_days=INSIGHTS_REFRESH_DAYS)
    print('  需更新: {} 支（{}天內且今天尚未更新）'.format(len(stale), INSIGHTS_REFRESH_DAYS))

    if stale:
        insights_map = fetch_insights_for(stale)
        # 重新讀 DB 取得這些影片的完整資料
        ids_str = ','.join('?' for _ in stale)
        cur = conn.execute(
            'SELECT * FROM videos WHERE id IN ({})'.format(ids_str),
            [r[0] for r in stale]
        )
        video_rows = {row['id']: dict(row) for row in cur.fetchall()}

        for vid_id, (platform, ins_data) in insights_map.items():
            if not ins_data:
                continue
            v = video_rows.get(vid_id, {'id': vid_id, 'platform': platform})
            v.update(ins_data)
            upsert_video(conn, v, update_insights=True)

    # 步驟 3：計算評分
    print('\n[3] 計算評分...')
    recent = load_recent_videos(conn, days=HTML_EMBED_DAYS)
    avg_fb, avg_ig = compute_averages(recent)
    print('  平均觸及 FB={:.0f}  IG={:.0f}'.format(avg_fb, avg_ig))

    # 更新有 insights 的影片評分
    stale_ids = set(r[0] for r in stale)
    for v in recent:
        if v['id'] in stale_ids or is_first_run:
            new_score = score_video(v, avg_fb, avg_ig)
            if new_score != v.get('score', 0):
                conn.execute('UPDATE videos SET score=? WHERE id=?', (new_score, v['id']))
    conn.commit()

    # 重新讀取（含更新後的評分）
    recent = load_recent_videos(conn, days=HTML_EMBED_DAYS)
    conn.close()

    # 步驟 4：生成 HTML
    print('\n[4] 生成 index.html（最近 {} 天）...'.format(HTML_EMBED_DAYS))
    generate_html(recent, avg_fb, avg_ig)

    print('\n完成！')
    print('  DB 總計: {} 支  /  HTML 嵌入: {} 支'.format(
        total_in_db + len(new_videos), len(recent)))
    print('\n下一步:')
    print('  git add index.html')
    print('  git commit -m "update {}"'.format(tw_now().strftime('%Y-%m-%d')))
    print('  git push')

if __name__ == '__main__':
    main()
