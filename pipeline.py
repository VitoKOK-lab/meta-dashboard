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
ARCHIVE_PATH  = os.path.join(BASE_DIR, 'data', 'archive.json')
TEMPLATE_PATH = os.path.join(BASE_DIR, 'template.html')
OUTPUT_PATH   = os.path.join(BASE_DIR, 'index.html')
API_BASE      = 'https://graph.facebook.com/v19.0'

INSIGHTS_REFRESH_DAYS = 28   # 每天：更新 28 天內的 insights
INSIGHTS_WEEKLY_DAYS  = 90   # 每週一：更新 90 天內的 insights
HTML_EMBED_DAYS       = 9999  # 全部影片都嵌入，讓排行榜可以看歷史
ARCHIVE_STABLE_DAYS   = 15   # 15 天以上流量趨穩，存入長期 archive

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

def save_archive(videos_dict):
    """把 15 天以上、流量趨穩的影片累積存入 data/archive.json（長期歷史紀錄）"""
    data_dir = os.path.dirname(ARCHIVE_PATH)
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    existing = {}
    if os.path.exists(ARCHIVE_PATH):
        try:
            with open(ARCHIVE_PATH, 'r', encoding='utf-8') as f:
                for v in json.load(f).get('videos', []):
                    existing[v['id']] = v
        except Exception:
            pass
    updated = 0
    for vid_id, v in videos_dict.items():
        if days_ago_from(v.get('created_time', '')) >= ARCHIVE_STABLE_DAYS and (v.get('plays') or 0) > 0:
            existing[vid_id] = dict(v)
            updated += 1
    archive_list = sorted(existing.values(), key=lambda x: x.get('created_time', ''), reverse=True)
    payload = {
        'updated_at': utc_now().isoformat(),
        'count': len(archive_list),
        'videos': archive_list,
    }
    with open(ARCHIVE_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
    print('  data/archive.json {} 支（新增/更新 {}）'.format(len(archive_list), updated))

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

def get_stale_ids(videos_dict, refresh_days=INSIGHTS_REFRESH_DAYS):
    """回傳需要更新 insights 的 (id, platform) 清單"""
    today = utc_now().strftime('%Y-%m-%d')
    stale = []
    for vid_id, v in videos_dict.items():
        age = days_ago_from(v.get('created_time', ''))
        if age <= refresh_days:
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
        'new_followers':   m.get('follows') or 0,
        'avg_watch_ms':    avg_watch,
        'total_view_ms':   total_view,
        'completion_rate': None,
        'retention':       [],
    }

# ── 評分（頻道相對百分位制）──────────────────────────────────────────────────
#
# 核心理念：跟自己頻道比，不跟行業固定門檻比。
#   流量型：播放量 × 轉發率 × 留言率 × 觸及覆蓋 四維度
#   帶貨型：留言率（引導購買訊號）× IG儲存率 × 觸及 × 轉發
#            → 播放量不重要，因為帶貨片天生流量小
#
# 百分位排名：同類型影片中，這支片在各指標的位置（0~1）
# 最終分 = 各維度加權百分位 × 100，分佈自然趨於 0-100

def _percentile_rank(sorted_list, value):
    """回傳 value 在已排序 sorted_list 中的百分位（0.0–1.0）。"""
    if not sorted_list:
        return 0.5
    lo, hi = 0, len(sorted_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_list[mid] <= value:
            lo = mid + 1
        else:
            hi = mid
    return lo / float(len(sorted_list))

def _sorted_rates(videos, key):
    # 含 0，讓「完全沒有轉發/留言」也有百分位位置，而非強制得 0 分
    vals = []
    for v in videos:
        p = v.get('plays') or 0
        if p > 0:
            vals.append((v.get(key) or 0) / float(p))
    return sorted(vals)

def _sorted_plays(videos):
    return sorted(v.get('plays') or 0 for v in videos if (v.get('plays') or 0) > 0)

def _sorted_reach(videos, platform):
    return sorted(v.get('reach') or 0 for v in videos
                  if v.get('platform') == platform and (v.get('reach') or 0) > 0)

def _median(lst):
    s = sorted(lst)
    n = len(s)
    return (s[n // 2] + s[(n - 1) // 2]) / 2.0 if n else 0.0

def compute_stats(videos_list):
    """
    建立頻道內各指標的排序分佈，流量型與帶貨型分開計算。
    用於 score_video() 的百分位排名。
    """
    traffic  = [v for v in videos_list if v.get('type') == 'traffic']
    commerce = [v for v in videos_list if v.get('type') == 'commerce']
    return {
        # 流量型
        'tr_plays':   _sorted_plays(traffic)             or [1],
        'tr_share':   _sorted_rates(traffic,  'shares')  or [0],
        'tr_comment': _sorted_rates(traffic,  'comments')or [0],
        # 帶貨型
        'cm_plays':   _sorted_plays(commerce)            or [1],
        'cm_comment': _sorted_rates(commerce, 'comments')or [0],
        'cm_share':   _sorted_rates(commerce, 'shares')  or [0],
        'cm_saves':   _sorted_rates(commerce, 'saved')   or [0],
        # 觸及（全影片，依平台）
        'fb_reach':   _sorted_reach(videos_list, 'fb')   or [1],
        'ig_reach':   _sorted_reach(videos_list, 'ig')   or [1],
    }

def compute_averages(stats):
    """回傳 (avg_fb, avg_ig) 觸及中位數，供 JS reachDev() 顯示偏離值用。"""
    return (
        _median(stats['fb_reach']) or 5000.0,
        _median(stats['ig_reach']) or 3000.0,
    )

def score_video(v, stats):
    """
    百分位制評分（0–100）。
    ─ 流量型 ─
      播放量  35%：頻道流量型影片中的百分位
      轉發率  30%：轉發=觀眾主動傳播，最強流量訊號
      留言率  20%：留言=熱度訊號
      觸及覆蓋15%：觸及在頻道中的百分位
    ─ 帶貨型（FB）─
      留言率  50%：留言=購買意圖訊號（引導「留言+1」）
      觸及覆蓋25%
      轉發率  15%
      播放量  10%：帶貨片流量天生少，佔比低
    ─ 帶貨型（IG）─
      留言率  40%
      儲存率  25%：儲存=想買但還沒買，最強電商訊號
      觸及覆蓋20%
      轉發率  10%
      播放量   5%
    完播率（如有）：×0.95 + cr×0.05 微調
    """
    plays = v.get('plays') or 0
    if plays == 0:
        return 0

    p  = _percentile_rank
    sr = (v.get('shares')   or 0) / float(plays)
    cr = (v.get('comments') or 0) / float(plays)

    reach      = v.get('reach') or 0
    reach_list = stats['fb_reach'] if v.get('platform') == 'fb' else stats['ig_reach']
    reach_pct  = p(reach_list, reach)

    if v.get('type') == 'traffic':
        raw = (p(stats['tr_plays'],   plays) * 0.35
             + p(stats['tr_share'],   sr)    * 0.30
             + p(stats['tr_comment'], cr)    * 0.20
             + reach_pct                     * 0.15)
    else:  # commerce
        saves_rate = (v.get('saved') or 0) / float(plays) if v.get('platform') == 'ig' else 0.0
        if v.get('platform') == 'ig':
            sv = p(stats['cm_saves'], saves_rate) if saves_rate > 0 else 0.0
            raw = (p(stats['cm_comment'], cr)  * 0.40
                 + sv                           * 0.25
                 + reach_pct                    * 0.20
                 + p(stats['cm_share'],   sr)   * 0.10
                 + p(stats['cm_plays'], plays)  * 0.05)
        else:  # FB 帶貨
            raw = (p(stats['cm_comment'], cr)  * 0.50
                 + reach_pct                    * 0.25
                 + p(stats['cm_share'],   sr)   * 0.15
                 + p(stats['cm_plays'], plays)  * 0.10)

    completion = v.get('completion_rate')
    if completion is not None:
        raw = raw * 0.95 + completion * 0.05

    return min(int(round(raw * 100)), 100)

# ── 抓影片列表 ────────────────────────────────────────────────────────────────
def fetch_video_list(platform, since_days=7):
    now_ts   = int(time.time())
    cutoff_ts = now_ts - since_days * 86400
    videos = []
    cursor = None
    page   = 0
    while True:
        try:
            if cursor:
                data = api_get(cursor)
            elif platform == 'fb':
                # FB: do NOT use since/until — they filter by updated_time, not created_time
                # Instead paginate all videos and stop when created_time < cutoff
                data = api_get('{}/videos'.format(FB_PAGE), {
                    'fields': 'id,description,created_time,length',
                    'limit': 100,
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
        hit_cutoff = False
        for item in items:
            ts  = item.get('created_time') or item.get('timestamp', '')
            cap = item.get('description') or item.get('caption') or ''
            cap = cap.strip()
            # FB：check created_time against cutoff (API returns newest-first)
            if platform == 'fb':
                s = ts.replace('Z', '').split('+')[0][:19]
                try:
                    item_ts = int(time.mktime(
                        datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S').timetuple()
                    ))
                except ValueError:
                    item_ts = 0
                if item_ts < cutoff_ts:
                    hit_cutoff = True
                    break
                # 跳過沒有說明文字的影片（直播、純影片等）
                if not cap:
                    continue
                # 跳過直播存檔（FB 自動標題 "Live streaming of ..."、"Live with ..." 等）
                cap_low = cap.lower()
                if cap_low.startswith('live streaming') or cap_low.startswith('live with ') or cap_low == 'live':
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
        if hit_cutoff or not cursor or not items:
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
            '{}/insights?metric=reach,shares,comments,likes,saved,follows,'
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
        'playsPrev':      v.get('plays_prev', 0),
        'playsPrevAt':    v.get('plays_prev_at', ''),
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
    force_full    = '--full'    in sys.argv
    force_weekly  = '--weekly'  in sys.argv
    force_history = '--history' in sys.argv  # 每20天：拉5年內所有歷史影片
    refresh_days  = INSIGHTS_WEEKLY_DAYS if (force_weekly or force_history) else INSIGHTS_REFRESH_DAYS
    print('=' * 50)
    print('  泰熙爾札娜 IP 儀表板 pipeline')
    print('  {}'.format(tw_now().strftime('%Y-%m-%d %H:%M')))
    print('=' * 50)

    videos_dict = load_db()
    is_first = len(videos_dict) == 0 or force_full
    if force_history:
        fetch_days = 1825  # 5年，拉所有歷史影片
    elif is_first:
        fetch_days = 90
    else:
        fetch_days = 7
    if force_full:
        videos_dict = {}  # 清空，從頭重建

    if force_history:
        print('\n[歷史模式] 抓取近 5 年所有影片（每 20 天自動執行）...')
    elif is_first:
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
                # 更新前保留上一次播放數，供爆流量偵測用
                prev_plays = videos_dict[vid_id].get('plays') or 0
                if prev_plays > 0:
                    videos_dict[vid_id]['plays_prev']    = prev_plays
                    videos_dict[vid_id]['plays_prev_at'] = videos_dict[vid_id].get('insights_at', '')
                videos_dict[vid_id].update(ins)
                videos_dict[vid_id]['insights_at'] = now_iso

    # 步驟 3：計算評分（頻道相對百分位制）
    print('\n[3] 計算評分...')
    recent = get_recent(videos_dict, days=HTML_EMBED_DAYS)
    stats  = compute_stats(recent)
    avg_fb, avg_ig = compute_averages(stats)
    tr_cnt = len([v for v in recent if v.get('type') == 'traffic'])
    cm_cnt = len([v for v in recent if v.get('type') == 'commerce'])
    print('  觸及中位 FB={:.0f}  IG={:.0f}  流量型{}支 帶貨型{}支'.format(
        avg_fb, avg_ig, tr_cnt, cm_cnt))
    # 百分位制：每次都全量重算，因為任何影片的 insights 更新都會移動整體分佈
    rescored = 0
    for v in videos_dict.values():
        if (v.get('plays') or 0) > 0:
            v['score'] = score_video(v, stats)
            rescored += 1
    print('  重新計算分數: {} 支'.format(rescored))

    # 儲存 JSON
    save_db(videos_dict)
    save_archive(videos_dict)

    # 步驟 4：生成 HTML
    print('\n[4] 生成 index.html...')
    recent = get_recent(videos_dict, days=HTML_EMBED_DAYS)
    generate_html(recent, avg_fb, avg_ig)

    print('\n完成！DB:{} 支 / HTML:{} 支'.format(len(videos_dict), len(recent)))

if __name__ == '__main__':
    main()
