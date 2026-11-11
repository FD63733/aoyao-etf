#!/usr/bin/env python3
"""Free personal/family ETF daily-data snapshot builder.

Never fabricates dates or prices. Upstream public interfaces may rate limit or block
GitHub-hosted runners; failed requests retain old dated bars and mark them stale.
Pages deployment holds the shared immutable snapshot; no browser-side bulk scan.
"""
from __future__ import annotations
import json
import logging
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import requests
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request

ROOT = Path(__file__).resolve().parents[1]
DATADIR = ROOT / 'data'
CHUNKS = DATADIR / 'chunks'
LOG = logging.getLogger('yy-etf')
BATCH_LIMIT = max(10, min(260, int(os.getenv('ETF_BATCH_LIMIT', '160'))))
CHUNK_SIZE = 80
MAX_WORKERS = 2
_PACE_LOCK = threading.Lock()
_NEXT_AT = 0.0

def paced_request():
    """Limit combined historical request start rate to about one request per second."""
    global _NEXT_AT
    with _PACE_LOCK:
        now = time.monotonic()
        wait = max(0.0, _NEXT_AT - now)
        _NEXT_AT = max(now, _NEXT_AT) + 1.2
    if wait:
        time.sleep(wait)

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
CODE_RE = re.compile(r'^(?:15\d{4}|5\d{5})$')


def request_json(url: str, timeout: int = 25):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; YYETF-personal/1.0)', 'Accept': 'application/json', 'Cache-Control': 'no-cache'})
    with urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def prior_snapshot():
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    if repo and '/' in repo:
        owner, project = repo.split('/', 1)
        base = 'https://%s.github.io/%s/' % (owner, project)
        if project.lower() == (owner + '.github.io').lower():
            base = 'https://%s.github.io/' % owner
    else:
        base = os.getenv('OLD_SITE_ROOT', '').rstrip('/') + '/'
    if base == '/':
        return {}, {}, None
    try:
        manifest = request_json(base + 'data/manifest.json?t=%d' % int(time.time()))
    except HTTPError as exc:
        if exc.code == 404:
            LOG.info('No previous deployment, starting first run.')
            return {}, {}, None
        raise RuntimeError('Previous shared dataset cannot be loaded; preserving live site') from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError('Previous site unavailable; refusing to reset historic data') from exc
    if manifest.get('schema') != 'yy-etf-v1':
        raise RuntimeError('Previous manifest schema mismatch')
    history = {}
    for chunk in manifest.get('chunks', []):
        data = request_json(base + 'data/' + chunk + '?t=%d' % int(time.time()))
        if not isinstance(data, dict):
            raise RuntimeError('Invalid previous history chunk: ' + chunk)
        history.update(data)
    LOG.info('Recovered %d ETF histories from published Pages', len(history))
    return history, {a['code']: a for a in manifest.get('universe', []) if 'code' in a}, manifest


def clean_universe(df):
    if df is None or df.empty:
        return []
    ans = {}
    for r in df.to_dict('records'):
        code = str(r.get('代码') or r.get('基金代码') or r.get('证券代码') or r.get('code') or '').strip()
        name = str(r.get('名称') or r.get('基金简称') or r.get('基金名称') or r.get('name') or '').strip()
        if CODE_RE.fullmatch(code) and name and 'ETF' in name.upper() and '联接' not in name and 'LOF' not in name.upper():
            ans[code] = {'code': code, 'name': name, 'market': 1 if code.startswith('5') else 0}
    return list(ans.values())


def discover(ak):
    probes = [('AKShare东方财富ETF行情列表', lambda: ak.fund_etf_spot_em()),
              ('AKShare同花顺ETF行情列表（备选，不保证完整）', lambda: ak.fund_etf_spot_ths())]
    for label, func in probes:
        try:
            data = clean_universe(func())
            if len(data) >= 60:
                LOG.info('ETF universe: %s %d', label, len(data))
                return data, label, label.startswith('AKShare东方财富')
            LOG.warning('Universe %s returned %s valid rows', label, len(data))
        except Exception as exc:
            LOG.warning('Universe %s failed: %s', label, exc)
    return [], '本轮名单接口失败，沿用上次有日期的名单', False


def positive(x):
    try:
        val = float(x)
        return val if val > 0 and val < 10**12 else None
    except (TypeError, ValueError):
        return None


def normalized_rows(df, provider):
    if df is None or df.empty:
        return []
    ans = {}
    for r in df.to_dict('records'):
        date = r.get('日期', r.get('date'))
        if hasattr(date, 'strftime'):
            date = date.strftime('%Y-%m-%d')
        date = str(date)[:10]
        o = positive(r.get('开盘', r.get('open')))
        c = positive(r.get('收盘', r.get('close')))
        h = positive(r.get('最高', r.get('high')))
        l = positive(r.get('最低', r.get('low')))
        if not DATE_RE.fullmatch(date) or not all((o, c, h, l)) or h < max(o, c, l) or l > min(o, c, h):
            continue
        amount = positive(r.get('成交额')) if provider == 'em' else None
        ans[date] = {'d': date, 'o': round(o, 6), 'c': round(c, 6), 'h': round(h, 6), 'l': round(l, 6), 'amount': amount}
    return [ans[k] for k in sorted(ans)[-145:]]


def history_one(ak, entry):
    code = entry['code']
    try:
        paced_request()
        df = ak.fund_etf_hist_em(symbol=code, period='daily', start_date='20240101', end_date='20500101', adjust='')
        rows = normalized_rows(df, 'em')
        if len(rows) >= 2:
            return code, rows, 'AKShare东方财富历史日K', None
        raise ValueError('primary returned empty/invalid bars')
    except Exception as first:
        try:
            paced_request()
            df = ak.fund_etf_hist_sina(symbol=('sh' if code.startswith('5') else 'sz') + code)
            rows = normalized_rows(df, 'sina')
            if len(rows) >= 2:
                return code, rows, 'AKShare新浪历史日K（成交额缺失）', None
            raise ValueError('backup returned empty/invalid bars')
        except Exception as second:
            return code, None, None, '%s; %s' % (str(first)[:100], str(second)[:100])


def update_indices(ak):
    """Best-effort shared index/sector daily bars. Never map to another index silently."""
    import re as _re
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    catalog_ids = ['embeddedCatalog', 'embeddedYjbCatalog']
    symbols = {}
    for catalog_id in catalog_ids:
        m = _re.search(r'<script[^>]+id="' + catalog_id + r'"[^>]*>(.*?)</script>', html, _re.S)
        if not m:
            continue
        for item in json.loads(m.group(1)):
            secid = item.get('s')
            if isinstance(secid, str) and _re.fullmatch(r'(?:0|1|2|90)\.[0-9A-Z]+', secid):
                symbols.setdefault(secid, item)
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    old_index = {}
    if '/' in repo:
        owner, proj = repo.split('/', 1)
        root = f'https://{owner}.github.io/' + ('' if proj.lower() == (owner+'.github.io').lower() else proj + '/')
        try:
            prior = request_json(root + 'data/index.json?t=' + str(int(time.time())))
            if prior.get('schema') == 'yy-index-v1':
                old_index = prior
        except HTTPError as e:
            if e.code != 404:
                raise
        except Exception as e:
            LOG.warning('Index prior snapshot unavailable (not overwriting local ETF): %s', e)
    items = {k: v for k,v in old_index.get('items', {}).items() if k in symbols}
    checked_at = old_index.get('checked_at', {})
    queue = sorted(symbols, key=lambda k: (items.get(k, {}).get('series', [{}])[-1].get('d', '') if items.get(k, {}).get('series') else '', checked_at.get(k,'')))[:26]
    for secid in queue:
        checked_at[secid] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        try:
            params = {'secid': secid, 'fields1':'f1,f2,f3,f4,f5,f6', 'fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61', 'klt':'101', 'fqt':'0', 'end':'20500101', 'lmt':'140', 'ut':'fa5fd1943c7b386f172d6893dbfba10b'}
            r = requests.get('https://push2his.eastmoney.com/api/qt/stock/kline/get', params=params, timeout=10)
            r.raise_for_status()
            data = r.json().get('data') or {}
            if str(data.get('code', '')).upper() != secid.split('.',1)[1]:
                raise ValueError('Index code mismatch')
            expected = symbols[secid].get('expectedNames', [])
            name = str(data.get('name', '')).lower().replace(' ', '')
            if expected and not any(str(x).lower().replace(' ', '') in name for x in expected):
                raise ValueError('Index name mismatch')
            rows = []
            for raw in data.get('klines') or []:
                vals = str(raw).split(',')
                if len(vals) < 7:
                    continue
                d = vals[0]
                o,c,h,l = map(positive, vals[1:5])
                if DATE_RE.fullmatch(d) and all((o,c,h,l)) and l <= min(o,c) <= h and l <= max(o,c) <= h:
                    rows.append({'d':d,'o':o,'c':c,'h':h,'l':l,'amount':positive(vals[6])})
            if len(rows) >= 36:
                items[secid] = {'secid':secid,'api_code':data.get('code'),'real_name':data.get('name'), 'source':'服务器定时获取的东方财富公开历史日K', 'series':rows[-130:], 'fetched_at':datetime.now(timezone.utc).isoformat(timespec='seconds')}
        except Exception as e:
            LOG.warning('Index %s update unavailable: %s', secid, str(e)[:90])
            # Protect old index values and original dates.
            continue
        time.sleep(.3)
    output = {'schema':'yy-index-v1','generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'), 'total':len(symbols),'items':items,'checked_at':checked_at}
    (DATADIR/'index.json').write_text(json.dumps(output, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    LOG.info('Shared index cache: %d/%d', len(items), len(symbols))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    import akshare as ak
    CHUNKS.mkdir(parents=True, exist_ok=True)
    existing, old_universe, old = prior_snapshot()
    found, source, from_primary = discover(ak)
    if found:
        universe = {x['code']: x for x in found}
        # Keep previous entries, marked as possibly delisted, rather than silently losing their histories.
        for code, entry in old_universe.items():
            universe.setdefault(code, {**entry, 'old_only': True})
    else:
        universe = old_universe
    if not universe:
        raise RuntimeError('No real ETF universe discovered; will not publish invented sample data')
    previous_check = (old or {}).get('checked_at', {})
    latest_date = {c: (rows[-1]['d'] if rows else '') for c, rows in existing.items()}
    # Oldest verified date first, then oldest check time, across all valid names.
    queue = sorted(universe.values(), key=lambda e: (latest_date.get(e['code'], ''), previous_check.get(e['code'], '')))
    queue = [e for e in queue if not e.get('old_only')][:BATCH_LIMIT]
    LOG.info('Refresh %d/%d ETF historical bars', len(queue), len(universe))
    failures = []
    checked_at = dict(previous_check)
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    # Requests are staggered and intentionally low concurrency to reduce rate-limit pressure.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(history_one, ak, e): e for e in queue}
        for n, fut in enumerate(as_completed(futures), 1):
            code, rows, provider, error = fut.result()
            checked_at[code] = now
            if rows:
                existing[code] = rows
            else:
                failures.append({'code': code, 'reason': error})
            if n % 30 == 0:
                LOG.info('Downloaded %d/%d (failed %d)', n, len(queue), len(failures))
    if not any(len(rows) >= 66 for rows in existing.values()):
        raise RuntimeError('No >=66-day real historical ETF data available; refusing to replace live site')
    for old_file in CHUNKS.glob('*.json'):
        old_file.unlink()
    # Store keyed snapshots to match the pre-existing chart/ranking data shape.
    codes = sorted(existing)
    chunks = []
    for start in range(0, len(codes), CHUNK_SIZE):
        group = {code: existing[code] for code in codes[start:start + CHUNK_SIZE]}
        fname = 'chunks/%03d.json' % (start // CHUNK_SIZE)
        (DATADIR / fname).write_text(json.dumps(group, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        chunks.append(fname)
    univ_rows = [universe[k] for k in sorted(universe)]
    # A successful spot response is a candidate list, not a guarantee that every exchange product is present.
    manifest = {
        'schema': 'yy-etf-v1', 'generated_at': now, 'source': source,
        'universe': univ_rows, 'list_complete': False,
        'discovery_response_ok': bool(from_primary),
        'universe_count': len(univ_rows), 'history_count': sum(bool(v) for v in existing.values()),
        'rankable_count': sum(len(v) >= 66 for v in existing.values()),
        'latest_verified_date': max((rows[-1]['d'] for rows in existing.values() if rows), default=None),
        'latest_date_coverage': {}, 'last_run_success': len(queue) - len(failures),
        'last_run_failed': len(failures), 'last_run_requested': len(queue),
        'checked_at': {c: checked_at[c] for c in universe if c in checked_at},
        'chunks': chunks,
        'notes': '公共非授权行情源仅供家庭观察；未取得新数据时沿用原日期，不声称盘中或当日收盘已核验。'
    }
    for rows in existing.values():
        if rows:
            d = rows[-1]['d']
            manifest['latest_date_coverage'][d] = manifest['latest_date_coverage'].get(d, 0) + 1
    (DATADIR / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    LOG.info('Built %d symbols, %d with >=66 bars; coverage %s', len(univ_rows), manifest['rankable_count'], manifest['latest_date_coverage'])
    update_indices(ak)


if __name__ == '__main__':
    main()
