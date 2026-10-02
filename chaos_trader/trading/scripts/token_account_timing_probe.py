#!/usr/bin/env python3
"""Probe owner/mint token-account history for cleaner first-touch timing.
Read-only Helius RPC. No execution.
"""
from __future__ import annotations

import argparse, json, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from helius_common import rpc_request

LAMPORTS=1_000_000_000

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00','Z') if ts else None

def pubkey_of(k):
    return k.get('pubkey') if isinstance(k, dict) else (str(k) if k is not None else None)

def sig(tx):
    return tx.get('signature') or (((tx.get('transaction') or {}).get('signatures') or [None])[0])

def keys(tx):
    return (((tx.get('transaction') or {}).get('message') or {}).get('accountKeys') or [])

def signer_keys(tx):
    return [k.get('pubkey') for k in keys(tx) if isinstance(k, dict) and k.get('signer') and k.get('pubkey')]

def sol_delta(tx,wallet):
    idx=None
    for i,k in enumerate(keys(tx)):
        if pubkey_of(k)==wallet:
            idx=i; break
    if idx is None: return 0.0
    m=tx.get('meta') or {}; pre=m.get('preBalances') or []; post=m.get('postBalances') or []
    if idx>=len(pre) or idx>=len(post): return 0.0
    return (post[idx]-pre[idx])/LAMPORTS

def tok_delta(tx,wallet,mint):
    m=tx.get('meta') or {}
    pre={}; post={}
    for b in m.get('preTokenBalances') or []:
        if b.get('owner')==wallet and b.get('mint')==mint:
            ui=b.get('uiTokenAmount') or {}; pre[(b.get('accountIndex'),mint)]=(int(ui.get('amount') or 0), int(ui.get('decimals') or 0))
    for b in m.get('postTokenBalances') or []:
        if b.get('owner')==wallet and b.get('mint')==mint:
            ui=b.get('uiTokenAmount') or {}; post[(b.get('accountIndex'),mint)]=(int(ui.get('amount') or 0), int(ui.get('decimals') or 0))
    total=0.0
    for key in set(pre)|set(post):
        pr,pd=pre.get(key,(0,post.get(key,(0,0))[1])); po,dd=post.get(key,(0,pd))
        total += (po-pr)/(10**int(dd or pd or 0))
    return total

def classify(sd,td):
    if td>0 and sd < -1e-5: return 'buy'
    if td<0 and sd > 1e-5: return 'sell'
    if td>0: return 'token_in'
    if td<0: return 'token_out'
    return 'unknown'

def token_accounts(owner,mint):
    res=rpc_request('getTokenAccountsByOwner',[owner, {'mint':mint}, {'encoding':'jsonParsed'}], timeout=30, retries=2)
    out=[]
    for item in (res or {}).get('value') or []:
        info=(((item.get('account') or {}).get('data') or {}).get('parsed') or {}).get('info') or {}
        amt=((info.get('tokenAmount') or {}).get('uiAmount')) or 0
        out.append({'token_account': item.get('pubkey'), 'amount': amt})
    return out

def fetch_address_txs(address,pages=8,limit=100):
    out=[]; before=None
    for _ in range(pages):
        cfg={'transactionDetails':'full','limit':min(100,limit),'sortOrder':'desc','maxSupportedTransactionVersion':0}
        if before: cfg['before']=before
        try:
            res=rpc_request('getTransactionsForAddress',[address,cfg],timeout=45,retries=2)
        except SystemExit:
            if before and out: break
            raise
        batch=(res or {}).get('data',[]) if isinstance(res,dict) else []
        if not batch: break
        out.extend(batch); before=sig(batch[-1])
        if len(batch)<limit or not before: break
        time.sleep(.05)
    seen=set(); uniq=[]
    for tx in out:
        s=sig(tx)
        if s and s in seen: continue
        if s: seen.add(s)
        uniq.append(tx)
    return uniq

def probe(owner,mint,label,pages):
    accounts=token_accounts(owner,mint)
    events=[]
    for acct in accounts:
        txs=fetch_address_txs(acct['token_account'],pages=pages)
        for tx in txs:
            td=tok_delta(tx,owner,mint)
            if not td: continue
            sd=sol_delta(tx,owner)
            events.append({'signature':sig(tx),'slot':tx.get('slot'),'ts':tx.get('blockTime'),'time':iso(tx.get('blockTime')),'kind':classify(sd,td),'token_delta':td,'sol_delta':sd,'signers':signer_keys(tx)[:6]})
    events.sort(key=lambda e:e.get('ts') or 0)
    buys=[e for e in events if e['kind']=='buy']; sells=[e for e in events if e['kind']=='sell']; ins=[e for e in events if e['kind']=='token_in']; outs=[e for e in events if e['kind']=='token_out']
    return {
        'label':label,'owner':owner,'mint':mint,'token_accounts':accounts,'events_found':len(events),
        'buy_count':len(buys),'sell_count':len(sells),'token_in_count':len(ins),'token_out_count':len(outs),
        'sol_spent':round(sum(abs(min(e['sol_delta'],0)) for e in buys),6),
        'sol_received':round(sum(max(e['sol_delta'],0) for e in sells),6),
        'token_in_total':round(sum(e['token_delta'] for e in ins),6),
        'token_out_total':round(abs(sum(e['token_delta'] for e in outs)),6),
        'buy_token_total':round(sum(e['token_delta'] for e in buys),6),
        'sell_token_total':round(abs(sum(e['token_delta'] for e in sells)),6),
        'first_event':events[0] if events else None,'last_event':events[-1] if events else None,'events_sample':events[:12]
    }

def main():
    p=argparse.ArgumentParser(); p.add_argument('--pages',type=int,default=8); p.add_argument('--out',required=True); p.add_argument('items',nargs='+',help='label,owner,mint triples pipe-separated')
    a=p.parse_args(); rows=[]
    for item in a.items:
        label,owner,mint=item.split('|')
        rows.append(probe(owner,mint,label,a.pages))
    path=Path(a.out); path.write_text(json.dumps({'generated_at_utc':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'rows':rows},indent=2)+'\n')
    print(json.dumps({'ok':True,'out':str(path),'rows':len(rows),'events':sum(r['events_found'] for r in rows)},indent=2))
if __name__=='__main__': main()
