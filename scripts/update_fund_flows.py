#!/usr/bin/env python3
"""Optional, non-blocking sector-fund-flow snapshots for YY ETF V42.

Does not mutate ETF prices or the ETF manifest. If the public sources fail,
keeps prior same-day verified BK values but does NOT label stale data as today's.
THS same-name rows are NOT BK series; marked as separate unverified-day references.
No artificial inflows are generated and values from different sources are never summed.
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'data'/'sector-flow.json'
BJ=ZoneInfo('Asia/Shanghai')
CODE_RE=re.compile(r'^BK\d+$')
LOG=logging.getLogger('fund-flow')
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
SESSION=requests.Session()
SESSION.headers.update({'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36','Accept':'application/json,text/html,*/*','Referer':'https://data.eastmoney.com/'})


def nowbj():return dt.datetime.now(BJ)

def valid_num(value):
    try:
        if value is None or str(value).strip() in ('','-','—','None'):return None
        value=float(str(value).replace(',','').replace('%','').strip())
        if not(-1e14<value<1e14) or value!=value:return None
        return value
    except (ValueError,TypeError,OverflowError):return None


def old_live():
    repo=os.getenv('GITHUB_REPOSITORY','')
    if '/' not in repo:return {}
    owner,name=repo.split('/',1)
    base=f'https://{owner}.github.io/{name}/data/sector-flow.json'
    try:
        res=SESSION.get(base,params={'t':int(time.time())},timeout=18)
        if res.ok:
            payload=res.json()
            return payload if payload.get('schema')=='yy-flow-v1' else {}
    except Exception as exc:LOG.warning('Could not retrieve previous sector snapshot: %s',str(exc)[:150])
    return {}


def eastmoney_rank(category):
    # Same provider, two different delivery domains; not independent feeds.
    fs='m:90+t:2' if category=='industry' else 'm:90+t:3'
    params={'pn':1,'pz':500,'po':1,'np':1,'fltt':2,'invt':2,'fs':fs,'fid':'f62',
            'fields':'f12,f14,f3,f62,f184,f124','ut':'bd1d9ddb04089700cf9c27f6f7426281'}
    for host in ('push2.eastmoney.com','push2delay.eastmoney.com'):
        try:
            resp=SESSION.get(f'https://{host}/api/qt/clist/get',params=params,timeout=16)
            resp.raise_for_status()
            rows=resp.json().get('data',{}).get('diff',[])
            if isinstance(rows,dict):rows=list(rows.values())
            if not isinstance(rows,list) or not rows:continue
            result={}
            for q in rows:
                code=str(q.get('f12',''))
                net=valid_num(q.get('f62'))
                stamp=valid_num(q.get('f124'))
                if not CODE_RE.fullmatch(code) or net is None or not stamp or stamp<=1e9:continue
                srcdate=dt.datetime.fromtimestamp(stamp,BJ)
                result[code]={'net':net,'pct':valid_num(q.get('f3')),
                  'ratio':valid_num(q.get('f184')),'sourceTs':int(stamp),
                  'sourceDay':srcdate.date().isoformat(),'name':str(q.get('f14',''))[:32],
                  'source':'东方财富','group':'行业' if category=='industry' else '概念'}
            if result:return result
        except Exception as e:LOG.warning('Eastmoney %s %s failed: %s',category,host,str(e)[:140])
    return {}


def amount_yi(cell):
    x=str(cell).strip().replace(' ','').replace(',','').replace('−','-')
    for sym in ('亿','亿元'):
        x=x.replace(sym,'')
    return valid_num(x)


def ths_rank(kind):
    # Different provider and a DIFFERENT board constituent definition.
    url=('https://data.10jqka.com.cn/funds/hyzjl/' if kind=='行业'
         else 'https://data.10jqka.com.cn/funds/gnzjl/')
    try:
        res=SESSION.get(url,timeout=23,headers={'Referer':'https://data.10jqka.com.cn/'})
        res.raise_for_status();res.encoding=res.apparent_encoding or 'utf-8'
        soup=BeautifulSoup(res.text,'html.parser')
        output={}
        for table in soup.find_all('table'):
            for tr in table.find_all('tr'):
                cols=tr.find_all('td',recursive=False)
                if len(cols)<7:continue
                name=cols[1].get_text(' ',strip=True).replace(' ','')
                if not name or len(name)>24 or not re.fullmatch(r'[\u3400-\u9fffA-Za-z0-9·（）()＋+-]{2,24}',name):continue
                net=amount_yi(cols[6].get_text(' ',strip=True))
                if net is None or abs(net)>10000:continue # website values in 亿元
                pct=valid_num(cols[3].get_text(' ',strip=True))
                output[name]={'net':round(net*1e8), 'pct':pct,'group':kind,'source':'同花顺',
                     'dateVerified':False,'name':name}
        LOG.info('THS %s reference: %d rows',kind,len(output))
        return output
    except Exception as e:
        LOG.warning('THS %s unavailable: %s',kind,str(e)[:160]);return {}


def run():
    now=nowbj();day=now.date().isoformat()
    old=old_live();prior={}
    if old.get('generated_day')==day:
        prior={key:q for key,q in old.get('by_code',{}).items()
               if CODE_RE.fullmatch(key) and q.get('sourceDay')==day}
    data={};data.update(prior)
    fetched={}
    for category in ('industry','concept'):
        fetched.update(eastmoney_rank(category))
    for code,quote in fetched.items():
        if quote['sourceDay']!=day:continue
        oldq=data.get(code)
        if oldq and int(oldq.get('sourceTs',0))>quote['sourceTs']:continue
        data[code]=quote
    reference={}
    # THS funds are fallback references, never silently merged into a BK row.
    for kind in ('行业','概念'):
        for name,q in ths_rank(kind).items():
            if name not in reference or kind=='行业':reference[name]=q
    if not reference and old.get('generated_day')==day:
        reference={key:q for key,q in old.get('by_name',{}).items() if q.get('source')=='同花顺'}
    payload={'schema':'yy-flow-v1','generated_at':now.isoformat(timespec='seconds'),
             'generated_day':day,'note':'东财BK源日验证；同花顺仅同名独立参考，源端交易日无法核验。两口径不能相加。',
             'by_code':data,'by_name':reference,
             'stats':{'eastmoney_verified_today':len(data),'ths_independent_reference':len(reference)}}
    DEST.parent.mkdir(parents=True,exist_ok=True)
    DEST.write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    LOG.info('Saved fund flows: verified BK today=%d; THS unverified-day references=%d',len(data),len(reference))

if __name__=='__main__':run()
