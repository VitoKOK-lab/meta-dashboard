# -*- coding: utf-8 -*-
"""貼文銷售 × Meta 成效 → 排行榜 PDF 產生器

產出兩份 PDF（reports/ 目錄）：
  1. 貼文銷售排行榜_商品版.pdf — 依商品分組（賣過幾次排前面），列出每一次
     貼文用哪支影片帶貨、該影片的流量（播放/觸及）與互動率，貼文與影片皆可點連結。
  2. 貼文銷售排行榜_影片版.pdf — 依影片排序（流量與互動率綜合高者在前），
     每支影片後面列出它賣過的商品與 Shopline 成效。

比對邏輯與 template.html 的「🛒 貼文銷售」分頁一致：
  - 只取近 12 個月活動（以最新活動日為基準）
  - 商品名 = 活動標題正規化（psNormProduct）
  - 互動 = 讚 + 留言 + 分享 + 收藏；互動率 = 互動 / 播放

用法： python3 postsale_pdf.py
需求： pip install reportlab fonttools（字型自動下載 Noto Sans TC）
"""
import csv
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
OUT_DIR = os.path.join(BASE_DIR, 'reports')
FONT_DIR = os.path.join(BASE_DIR, '.fonts')

NOTO_URL = ('https://raw.githubusercontent.com/google/fonts/main/'
            'ofl/notosanstc/NotoSansTC%5Bwght%5D.ttf')

# ── 色彩（沿用儀表板金黑風格）────────────────────────────────────────────────
GOLD = '#b08d3c'
DARK = '#1a1a1a'
MUTED = '#6e6857'
GOOD = '#0d9488'
BG_HEAD = '#26221a'
BG_ROW = '#f7f4ec'
BG_ROW2 = '#ffffff'
LINE = '#e4ddcd'
LINK = '#8a6d1f'


def tw_now():
    return datetime.now(timezone(timedelta(hours=8)))


# ── 字型 ─────────────────────────────────────────────────────────────────────
def ensure_fonts():
    """回傳 (regular_path, bold_path)。優先用已生成的靜態字重，否則下載＋實例化。"""
    reg = os.path.join(FONT_DIR, 'NotoSansTC-Medium.ttf')
    bold = os.path.join(FONT_DIR, 'NotoSansTC-Bold.ttf')
    if os.path.exists(reg) and os.path.exists(bold):
        return reg, bold
    os.makedirs(FONT_DIR, exist_ok=True)
    var_path = os.path.join(FONT_DIR, 'NotoSansTC[wght].ttf')
    if not os.path.exists(var_path):
        print('  下載 Noto Sans TC …')
        urllib.request.urlretrieve(NOTO_URL, var_path)
    try:
        from fontTools.ttLib import TTFont as FTFont
        from fontTools.varLib.instancer import instantiateVariableFont
        for wght, path in ((500, reg), (700, bold)):
            f = FTFont(var_path)
            instantiateVariableFont(f, {'wght': wght})
            f.save(path)
        return reg, bold
    except ImportError:
        # 沒有 fonttools：直接用可變字型（預設字重）當 Regular 與 Bold
        return var_path, var_path


# ── 資料載入（與 pipeline.py / template.html 邏輯一致）──────────────────────
def load_videos():
    vids = {}
    hist = os.path.join(DATA_DIR, 'history.db')
    if os.path.exists(hist):
        try:
            conn = sqlite3.connect(hist)
            for r in conn.execute('SELECT id, data FROM videos'):
                vids[r[0]] = json.loads(r[1])
            conn.close()
        except Exception as e:
            print('  [WARN] history.db 讀取失敗: {}'.format(e))
    with open(os.path.join(DATA_DIR, 'videos.json'), encoding='utf-8') as f:
        vids.update(json.load(f)['videos'])
    return vids


def load_campaigns():
    camps = []
    path = os.path.join(DATA_DIR, 'postsale_links.csv')
    with open(path, newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            links = [l.strip() for l in (r.get('貼文連結') or '').split('\n') if l.strip()]
            metas = [m.strip() for m in (r.get('Meta post ID') or '').split('\n') if m.strip()]
            plat = r.get('平台', '')
            vids = []
            for l in links:
                m = re.search(r'facebook\.com/reel/(\d+)', l)
                if m:
                    vids.append(m.group(1))
            if plat == 'Instagram':
                vids = metas or vids
            camps.append({
                'id': r.get('活動ID', ''),
                'date': r.get('建立日期', ''),
                'platform': 'ig' if plat == 'Instagram' else 'fb',
                'title': r.get('活動標題', ''),
                'postType': r.get('貼文類型', ''),
                'links': links,
                'videoIds': vids,
                'slComments': int(r.get('留言數') or 0),
                'slAddons': int(r.get('留言加購') or 0),
                'slSales': int(r.get('銷售額NT$') or 0),
            })
    return camps


def norm_product(title):
    t = (title or '').strip()
    t = re.sub(r'\d{8}[a-z]\d+(fb|ig)?$', '', t, flags=re.I)
    t = re.sub(r'(fb|ig)$', '', t, flags=re.I)
    t = re.sub(r'^\d{4}/\d{2}/\d{2}\s*', '', t)
    t = re.sub(r'^[\s\-_|]+|[\s\-_|]+$', '', t)
    return t or title


IG_ALPHA = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'


def ig_shortcode(media_id):
    try:
        n = int(str(media_id))
    except ValueError:
        return ''
    sc = ''
    while n > 0:
        sc = IG_ALPHA[n % 64] + sc
        n //= 64
    return sc


def video_url(vid_id, platform, link_map):
    if vid_id in link_map:
        return link_map[vid_id]
    if platform == 'fb':
        return 'https://www.facebook.com/watch/?v=' + str(vid_id)
    sc = ig_shortcode(vid_id)
    return 'https://www.instagram.com/reel/{}/'.format(sc) if sc else ''


def eng_of(v):
    return (v.get('likes') or 0) + (v.get('comments') or 0) + (v.get('shares') or 0) + (v.get('saved') or 0)


def er_of(v):
    plays = v.get('plays') or 0
    return eng_of(v) / plays * 100 if plays else 0.0


def fmt(n):
    return '{:,}'.format(int(n))


def fmt_wan(n):
    n = int(n)
    if n >= 100000:
        return '{:.1f}萬'.format(n / 10000)
    return fmt(n)


EMOJI_RE = re.compile(
    '[\U0001F000-\U0001FAFF\U0001F900-\U0001F9FF☀-➿⬀-⯿'
    '︀-️‍⃣\u2300-\u23FF\uFFFD\u4B94\U000E0000-\U000E007F]+')


def strip_emoji(s):
    return EMOJI_RE.sub('', str(s or '')).strip()


def esc(s):
    return (strip_emoji(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def trunc(s, n):
    s = (s or '').split('\n')[0].strip()
    return s[:n] + '…' if len(s) > n else s


# ── 主要資料組裝 ─────────────────────────────────────────────────────────────
def build_dataset():
    vmap = load_videos()
    camps = load_campaigns()
    max_date = max(c['date'] for c in camps)
    cutoff = str(int(max_date[:4]) - 1) + max_date[4:]
    recent = [c for c in camps if c['date'] >= cutoff]

    # videoId → 貼文原始連結（FB reel 連結含 ID；IG 依 links/metas 順序對應）
    link_map = {}
    for c in recent:
        if c['platform'] == 'fb':
            for l in c['links']:
                m = re.search(r'facebook\.com/reel/(\d+)', l)
                if m:
                    link_map.setdefault(m.group(1), l)
        else:
            for i, mid in enumerate(c['videoIds']):
                if i < len(c['links']):
                    link_map.setdefault(mid, c['links'][i])

    # 商品分組
    groups = {}
    for c in recent:
        key = norm_product(c['title'])
        g = groups.setdefault(key, {
            'product': key, 'camps': [], 'slComments': 0, 'slAddons': 0, 'slSales': 0,
            'plays': 0, 'reach': 0, 'eng': 0, 'vComments': 0, 'nVids': 0,
            'seen': set(), 'last': ''})
        cvids = []
        for vid_id in c['videoIds']:
            v = vmap.get(vid_id)
            if not v:
                continue
            cvids.append((vid_id, v))
            if vid_id in g['seen']:
                continue
            g['seen'].add(vid_id)
            g['plays'] += v.get('plays') or 0
            g['reach'] += v.get('reach') or 0
            g['vComments'] += v.get('comments') or 0
            g['eng'] += eng_of(v)
            g['nVids'] += 1
        g['camps'].append({'c': c, 'videos': cvids})
        g['slComments'] += c['slComments']
        g['slAddons'] += c['slAddons']
        g['slSales'] += c['slSales']
        if c['date'] > g['last']:
            g['last'] = c['date']
    glist = list(groups.values())
    for g in glist:
        g['nCamps'] = len(g['camps'])
        g['camps'].sort(key=lambda e: e['c']['date'], reverse=True)

    # 影片彙整（唯一影片 → 賣過的活動）
    videos = {}
    for c in recent:
        for vid_id in c['videoIds']:
            v = vmap.get(vid_id)
            if not v:
                continue
            e = videos.setdefault(vid_id, {'v': v, 'camps': []})
            e['camps'].append(c)
    for e in videos.values():
        e['camps'].sort(key=lambda c: c['date'], reverse=True)

    # KPI
    tot_plays = sum(e['v'].get('plays') or 0 for e in videos.values())
    tot_reach = sum(e['v'].get('reach') or 0 for e in videos.values())
    matched = sum(1 for c in recent if any(i in vmap for i in c['videoIds']))
    kpi = {
        'nCamps': len(recent), 'matched': matched,
        'plays': tot_plays, 'reach': tot_reach,
        'slComments': sum(c['slComments'] for c in recent),
        'slAddons': sum(c['slAddons'] for c in recent),
        'period': '{} ～ {}'.format(cutoff, max_date),
    }
    return glist, videos, link_map, kpi


# ── PDF 生成 ─────────────────────────────────────────────────────────────────
def make_styles():
    from reportlab.lib.styles import ParagraphStyle
    base = dict(fontName='NotoTC', wordWrap='CJK')
    return {
        'title': ParagraphStyle('title', fontName='NotoTC-Bold', fontSize=22, leading=29,
                                textColor=DARK),
        'subtitle': ParagraphStyle('subtitle', fontSize=12, leading=17, textColor=MUTED, **base),
        'kpi': ParagraphStyle('kpi', fontSize=11.5, leading=16, textColor=DARK, **base),
        'note': ParagraphStyle('note', fontSize=10, leading=14, textColor=MUTED, **base),
        'ghead': ParagraphStyle('ghead', fontName='NotoTC-Bold', fontSize=14.5, leading=19,
                                textColor=DARK),
        'gsum': ParagraphStyle('gsum', fontSize=11, leading=15.5, textColor=MUTED, **base),
        'cell': ParagraphStyle('cell', fontSize=10.5, leading=14.5, textColor=DARK, **base),
        'cellm': ParagraphStyle('cellm', fontSize=10, leading=14, textColor=MUTED, **base),
        'chead': ParagraphStyle('chead', fontName='NotoTC-Bold', fontSize=10.5, leading=14,
                                textColor='#f0e6ce'),
    }


def page_frame(title):
    from reportlab.lib.units import mm

    def draw(canvas, doc):
        canvas.saveState()
        w, h = doc.pagesize
        canvas.setFillColor(DARK)
        canvas.rect(0, h - 12 * mm, w, 12 * mm, stroke=0, fill=1)
        canvas.setFillColor(GOLD)
        canvas.rect(0, h - 12.7 * mm, w, 0.7 * mm, stroke=0, fill=1)
        canvas.setFont('NotoTC-Bold', 9)
        canvas.setFillColor('#e8d9ae')
        canvas.drawString(14 * mm, h - 8 * mm, '電商部 · ' + title)
        canvas.setFont('NotoTC', 8)
        canvas.setFillColor('#b3a98f')
        canvas.drawRightString(w - 14 * mm, h - 8 * mm,
                               tw_now().strftime('%Y-%m-%d %H:%M') + ' 產出')
        canvas.setFont('NotoTC', 8)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(w / 2, 7 * mm, '第 {} 頁'.format(canvas.getPageNumber()))
        canvas.restoreState()
    return draw


def link_para(text, url, style, color=LINK):
    if url:
        return '<link href="{}" color="{}"><u>{}</u></link>'.format(esc(url), color, text)
    return text


def plat_tag(p):
    color = '#1877f2' if p == 'fb' else '#c13584'
    return '<font color="{}">{}</font>'.format(color, p.upper())


def kpi_block(kpi, styles, story):
    from reportlab.platypus import Paragraph, Spacer
    story.append(Paragraph(
        '統計期間 {}｜銷售活動 <b>{}</b>｜對應到影片數據 <b>{}</b>｜'
        '影片總播放 <b>{}</b>｜影片總觸及 <b>{}</b>｜Shopline 留言 <b>{}</b>｜'
        '留言加購 <font color="{}"><b>{}</b></font>'.format(
            kpi['period'], fmt(kpi['nCamps']), fmt(kpi['matched']),
            fmt_wan(kpi['plays']), fmt_wan(kpi['reach']),
            fmt(kpi['slComments']), GOOD, fmt(kpi['slAddons'])),
        styles['kpi']))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        '資料來源：Shopline 貼文銷售匯出 × Meta 影片洞察。未對應的活動多為「照片」貼文'
        '（無影片洞察）及部分由 Shopline 代發、ID 不在粉專影片庫的 FB Reel。'
        '互動 = 讚＋留言＋分享＋收藏；互動率 = 互動 ÷ 播放。藍字皆可點擊開啟貼文／影片。',
        styles['note']))
    story.append(Spacer(1, 12))


def build_product_pdf(glist, link_map, kpi, out_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                    Spacer, Table, TableStyle, KeepTogether)

    styles = make_styles()
    doc = BaseDocTemplate(out_path, pagesize=A4,
                          leftMargin=14 * mm, rightMargin=14 * mm,
                          topMargin=20 * mm, bottomMargin=14 * mm)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='f')
    doc.addPageTemplates([PageTemplate(id='p', frames=[frame],
                                       onPage=page_frame('貼文銷售排行榜（商品版）'))])
    story = []
    story.append(Paragraph('貼文銷售排行榜（商品版）', styles['title']))
    story.append(Paragraph(
        '依「賣過幾次」排序：貼文次數多者在前（次數相同時看影片總播放）。'
        '每項商品列出每一次貼文的日期、平台、貼文連結，以及帶貨影片的播放、觸及與互動率。',
        styles['subtitle']))
    story.append(Spacer(1, 8))
    kpi_block(kpi, styles, story)

    glist = sorted(glist, key=lambda g: (-g['nCamps'], -g['plays'], g['product']))

    col_w = [16 * mm, 21 * mm, 10 * mm, 58 * mm, 18 * mm, 18 * mm, 20 * mm, 21 * mm]
    head = [Paragraph(h, styles['chead']) for h in
            ['次序', '日期', '平台', '貼文 / 帶貨影片（點擊開啟）', '播放', '觸及', '互動率', 'SL成效']]

    for rank, g in enumerate(glist, 1):
        rep = ' <font color="{}">×{}</font>'.format(GOLD, g['nCamps']) if g['nCamps'] >= 2 else ''
        header = Paragraph('#{}　{}{}'.format(rank, esc(g['product']), rep), styles['ghead'])
        summary = Paragraph(
            '賣過 <b>{}</b> 次｜影片 {} 支｜總播放 {}｜總觸及 {}｜影片留言 {}｜互動 {}｜'
            'SL留言 {}｜加購 <font color="{}">{}</font>｜銷售額 NT$ {}｜最近 {}'.format(
                g['nCamps'], g['nVids'], fmt(g['plays']), fmt(g['reach']),
                fmt(g['vComments']), fmt(g['eng']), fmt(g['slComments']),
                GOOD, fmt(g['slAddons']), fmt(g['slSales']), g['last']),
            styles['gsum'])

        rows = [head]
        n = g['nCamps']
        for idx, e in enumerate(g['camps']):
            c = e['c']
            seq = '第{}次'.format(n - idx)
            post_links = '　'.join(
                link_para('貼文{} ↗'.format(j + 1 if len(c['links']) > 1 else ''), l, styles['cell'])
                for j, l in enumerate(c['links'])) or '—'
            sl = 'SL留言 {}<br/>加購 {}'.format(fmt(c['slComments']), fmt(c['slAddons']))
            if c['slSales']:
                sl += '<br/>NT$ {}'.format(fmt(c['slSales']))
            if e['videos']:
                for k, (vid_id, v) in enumerate(e['videos']):
                    vtitle = esc(trunc(v.get('title'), 30)) or '（無標題）'
                    vurl = video_url(vid_id, v.get('platform'), link_map)
                    cell = '▶ ' + link_para(vtitle, vurl, styles['cell'])
                    if k == 0:
                        cell = '{}　{}<br/>'.format(esc(c['postType']), post_links) + cell
                    rows.append([
                        Paragraph(seq if k == 0 else '', styles['cell']),
                        Paragraph(c['date'] if k == 0 else '', styles['cellm']),
                        Paragraph(plat_tag(c['platform']) if k == 0 else '', styles['cell']),
                        Paragraph(cell, styles['cell']),
                        Paragraph('<b>{}</b>'.format(fmt(v.get('plays') or 0)), styles['cell']),
                        Paragraph(fmt(v.get('reach') or 0), styles['cell']),
                        Paragraph('{:.1f}%<br/><font color="{}" size="9">互動 {}</font>'.format(
                            er_of(v), MUTED, fmt(eng_of(v))), styles['cell']),
                        Paragraph(sl if k == 0 else '', styles['cellm']),
                    ])
            else:
                rows.append([
                    Paragraph(seq, styles['cell']),
                    Paragraph(c['date'], styles['cellm']),
                    Paragraph(plat_tag(c['platform']), styles['cell']),
                    Paragraph('{}　{}<br/><font color="{}" size="9.5">無影片數據'
                              '（照片貼文或未對應）</font>'.format(
                                  esc(c['postType']), post_links, MUTED), styles['cell']),
                    Paragraph('—', styles['cellm']),
                    Paragraph('—', styles['cellm']),
                    Paragraph('—', styles['cellm']),
                    Paragraph(sl, styles['cellm']),
                ])
        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), BG_HEAD),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [BG_ROW2, BG_ROW]),
            ('LINEBELOW', (0, 0), (-1, -1), 0.4, LINE),
            ('LINEABOVE', (0, 0), (-1, 0), 1, GOLD),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        block = [header, summary, Spacer(1, 4), tbl, Spacer(1, 14)]
        if len(rows) <= 8:
            story.append(KeepTogether(block))
        else:
            story.extend(block)
    doc.build(story)


def build_video_pdf(videos, link_map, kpi, out_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                    Spacer, Table, TableStyle, KeepTogether)

    styles = make_styles()
    doc = BaseDocTemplate(out_path, pagesize=A4,
                          leftMargin=14 * mm, rightMargin=14 * mm,
                          topMargin=20 * mm, bottomMargin=14 * mm)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='f')
    doc.addPageTemplates([PageTemplate(id='p', frames=[frame],
                                       onPage=page_frame('貼文銷售排行榜（影片版）'))])

    entries = list(videos.values())
    # 綜合排序：播放（流量）與互動率各取百分位排名，平均後高者在前
    n = len(entries)
    by_plays = sorted(entries, key=lambda e: e['v'].get('plays') or 0)
    by_er = sorted(entries, key=lambda e: er_of(e['v']))
    pr = {id(e): i / max(n - 1, 1) for i, e in enumerate(by_plays)}
    err = {id(e): i / max(n - 1, 1) for i, e in enumerate(by_er)}
    for e in entries:
        e['scorex'] = (pr[id(e)] + err[id(e)]) / 2
    entries.sort(key=lambda e: (-e['scorex'], -(e['v'].get('plays') or 0)))

    story = []
    story.append(Paragraph('貼文銷售排行榜（影片版）', styles['title']))
    story.append(Paragraph(
        '依「流量 × 互動率」綜合排序：播放數與互動率分別換算成全體百分位後取平均，'
        '兩者皆高的影片排最前面。每支影片列出它帶貨過的商品、貼文與 Shopline 成效。',
        styles['subtitle']))
    story.append(Spacer(1, 8))
    kpi_block(kpi, styles, story)

    col_w = [11 * mm, 72 * mm, 18 * mm, 18 * mm, 18 * mm, 45 * mm]
    head = [Paragraph(h, styles['chead']) for h in
            ['排名', '影片（點擊開啟）', '播放', '觸及', '互動率', '賣過的商品 / 貼文']]
    rows = [head]
    for rank, e in enumerate(entries, 1):
        v = e['v']
        vid_id = v.get('id')
        vtitle = esc(trunc(v.get('title'), 44)) or '（無標題）'
        vurl = video_url(vid_id, v.get('platform'), link_map)
        vcell = '{}<br/><font color="{}" size="9.5">{}　{}　讚 {}｜留言 {}｜分享 {}</font>'.format(
            link_para(vtitle, vurl, styles['cell']),
            MUTED, plat_tag(v.get('platform') or ''),
            v.get('created_date') or '', fmt(v.get('likes') or 0),
            fmt(v.get('comments') or 0), fmt(v.get('shares') or 0))
        prods = []
        for c in e['camps']:
            pl = c['links'][0] if c['links'] else ''
            sl_bits = []
            if c['slComments']:
                sl_bits.append('SL留言 ' + fmt(c['slComments']))
            if c['slAddons']:
                sl_bits.append('加購 ' + fmt(c['slAddons']))
            if c['slSales']:
                sl_bits.append('NT$ ' + fmt(c['slSales']))
            prods.append('{} <font color="{}" size="9">{}{}</font>'.format(
                link_para(esc(trunc(norm_product(c['title']), 18)), pl, styles['cellm']),
                MUTED, c['date'], ('｜' + '｜'.join(sl_bits)) if sl_bits else ''))
        rows.append([
            Paragraph('<b>{}</b>'.format(rank), styles['cell']),
            Paragraph(vcell, styles['cell']),
            Paragraph('<b>{}</b>'.format(fmt(v.get('plays') or 0)), styles['cell']),
            Paragraph(fmt(v.get('reach') or 0), styles['cell']),
            Paragraph('<b>{:.1f}%</b><br/><font color="{}" size="9">互動 {}</font>'.format(
                er_of(v), MUTED, fmt(eng_of(v))), styles['cell']),
            Paragraph('<br/>'.join(prods) or '—', styles['cellm']),
        ])

    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), BG_HEAD),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [BG_ROW2, BG_ROW]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.4, LINE),
        ('LINEABOVE', (0, 0), (-1, 0), 1, GOLD),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.append(tbl)
    doc.build(story)


def main():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    reg, bold = ensure_fonts()
    pdfmetrics.registerFont(TTFont('NotoTC', reg))
    pdfmetrics.registerFont(TTFont('NotoTC-Bold', bold))
    pdfmetrics.registerFontFamily('NotoTC', normal='NotoTC', bold='NotoTC-Bold',
                                  italic='NotoTC', boldItalic='NotoTC-Bold')

    os.makedirs(OUT_DIR, exist_ok=True)
    glist, videos, link_map, kpi = build_dataset()
    print('  商品 {} 項｜對應影片 {} 支｜活動 {} 個'.format(
        len(glist), len(videos), kpi['nCamps']))

    p1 = os.path.join(OUT_DIR, '貼文銷售排行榜_商品版.pdf')
    build_product_pdf(glist, link_map, kpi, p1)
    print('  ✓ {} ({} KB)'.format(os.path.basename(p1), os.path.getsize(p1) // 1024))

    p2 = os.path.join(OUT_DIR, '貼文銷售排行榜_影片版.pdf')
    build_video_pdf(videos, link_map, kpi, p2)
    print('  ✓ {} ({} KB)'.format(os.path.basename(p2), os.path.getsize(p2) // 1024))


if __name__ == '__main__':
    main()
