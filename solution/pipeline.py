from __future__ import annotations
import argparse, csv, hashlib, hmac, json, os, re, sqlite3, sys
from collections import defaultdict
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from cryptography.fernet import Fernet
except ImportError:  # clear error at startup
    Fernet = None

IST = ZoneInfo('Asia/Kolkata')
SCHEMA_DIR = Path(__file__).resolve().parents[1] / 'assignment-a-data' / 'schemas'
DEFAULT_DB = Path('interaction_ledger.sqlite3')


def norm_header(s: str) -> str:
    return re.sub(r'[_\s]+', ' ', (s or '').strip().lower())


def normalize_phone(value: str | None) -> str | None:
    if value is None: return None
    digits = re.sub(r'\D', '', str(value))
    if len(digits) == 12 and digits.startswith('91'): digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'): digits = digits[1:]
    if len(digits) == 10 and digits[0] in '6789': return '+91' + digits
    return None


def parse_date(value: str) -> str | None:
    s = (value or '').strip()
    if not s: return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d-%b-%Y'):
        try: return datetime.strptime(s, fmt).date().isoformat()
        except ValueError: pass
    return None


def parse_decimal(value: str) -> str | None:
    s = (value or '').strip()
    if not s: return None
    s = re.sub(r'^(?:₹|Rs\.?|INR)\s*', '', s, flags=re.I).replace(',', '').strip()
    try:
        d = Decimal(s)
        if not d.is_finite() or d < 0: return None
        return str(d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    except InvalidOperation: return None


def parse_timestamp(value: str) -> datetime:
    s = value.strip()
    if s.endswith('Z'): dt = datetime.fromisoformat(s[:-1] + '+00:00')
    else: dt = datetime.fromisoformat(s)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


class Store:
    def __init__(self, db_path: Path, key_path: Path):
        if Fernet is None: raise RuntimeError('Install dependencies: pip install -r requirements.txt')
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if os.getenv('PII_ENCRYPTION_KEY'):
            key = os.environ['PII_ENCRYPTION_KEY'].encode()
        else:
            if not key_path.exists():
                key_path.parent.mkdir(parents=True, exist_ok=True)
                key_path.write_bytes(Fernet.generate_key())
                try: key_path.chmod(0o600)
                except OSError: pass
            key = key_path.read_bytes().strip()
        self.fernet = Fernet(key)
        self.hmac_key = hashlib.sha256(key + b':phone-index').digest()
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.init_db()

    def enc(self, value):
        return self.fernet.encrypt(str(value).encode()).decode() if value is not None else None
    def dec(self, value):
        return self.fernet.decrypt(value.encode()).decode() if value else None
    def phone_hash(self, phone):
        return hmac.new(self.hmac_key, phone.encode(), hashlib.sha256).hexdigest() if phone else None

    def init_db(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS leads(
          id INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL, phone_hash TEXT NOT NULL,
          pii_phone_enc TEXT NOT NULL, pii_name_enc TEXT, external_ref TEXT NOT NULL,
          batch_id TEXT NOT NULL, fields_json TEXT NOT NULL, UNIQUE(workflow_id,phone_hash));
        CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(workflow_id,phone_hash);
        CREATE TABLE IF NOT EXISTS events(
          event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, workflow_id TEXT NOT NULL,
          call_id TEXT, occurred_at TEXT NOT NULL, occurred_ist TEXT NOT NULL,
          payload_json TEXT NOT NULL, phone_hash TEXT);
        CREATE INDEX IF NOT EXISTS idx_events_call ON events(workflow_id,call_id);
        CREATE TABLE IF NOT EXISTS event_links(
          event_id TEXT PRIMARY KEY REFERENCES events(event_id), lead_id INTEGER REFERENCES leads(id));
        CREATE INDEX IF NOT EXISTS idx_event_links_lead ON event_links(lead_id);
        CREATE TABLE IF NOT EXISTS seen_event_ids(event_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS processed_files(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, kind TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rejections(workflow_id TEXT,batch_id TEXT,row_number INTEGER,reason TEXT,
          PRIMARY KEY(workflow_id,batch_id,row_number));
        CREATE TABLE IF NOT EXISTS quality(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        ''')
        self.db.commit()

    def inc(self, key, amount=1):
        self.db.execute('INSERT INTO quality(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value',(key,amount))
    def q(self, key):
        r=self.db.execute('SELECT value FROM quality WHERE key=?',(key,)).fetchone(); return r['value'] if r else 0
    def file_done(self, path, digest, kind):
        row=self.db.execute('SELECT sha256 FROM processed_files WHERE path=?',(str(path.resolve()),)).fetchone()
        return bool(row and row['sha256']==digest)
    def mark_file(self,path,digest,kind):
        self.db.execute('INSERT OR REPLACE INTO processed_files VALUES(?,?,?)',(str(path.resolve()),digest,kind))


def load_schemas(schema_dir: Path):
    out={}
    for p in sorted(schema_dir.glob('*.json')):
        obj=json.loads(p.read_text(encoding='utf-8')); out[obj['workflow_id']]=obj
    return out


def field_header_map(headers, schema):
    accepted={}
    for f in schema['fields']:
        for name in [f['name'], *f.get('aliases',[])]: accepted[norm_header(name)]=f['name']
    mapping={}
    for h in headers:
        key=norm_header(h)
        if key in accepted: mapping[accepted[key]]=h
    return mapping


def validate_value(value, field):
    s=(value or '').strip()
    if not s: return None, False
    typ=field['type']
    if typ=='string': return s, False
    if typ=='phone':
        p=normalize_phone(s); return p, p is None
    if typ=='date':
        v=parse_date(s); return v, v is None
    if typ=='decimal':
        v=parse_decimal(s); return v, v is None
    if typ=='enum':
        allowed={str(x).lower() for x in field.get('values',[])}
        v=s.lower(); return (v,False) if v in allowed else (None,True)
    return s, False


def ingest_leads(store: Store, drop: Path, schemas):
    lead_root=drop/'leads'
    if not lead_root.exists(): return
    for wf_dir in sorted(p for p in lead_root.iterdir() if p.is_dir()):
        wf=wf_dir.name
        if wf not in schemas: continue
        schema=schemas[wf]
        for file in sorted(wf_dir.glob('*.csv')):
            raw=file.read_bytes(); digest=hashlib.sha256(raw).hexdigest()
            if store.file_done(file,digest,'leads'): continue
            with file.open('r',encoding='utf-8-sig',newline='') as f:
                reader=csv.DictReader(f)
                if not reader.fieldnames: continue
                mapping=field_header_map(reader.fieldnames,schema)
                row_no=0
                for row in reader:
                    if not any((v or '').strip() for v in row.values()): continue
                    row_no+=1; store.inc('lead_rows_total')
                    values={}; warnings=0; reason=None
                    for field in schema['fields']:
                        name=field['name']; rawval=row.get(mapping.get(name,''),'') if name in mapping else ''
                        val,bad=validate_value(rawval,field)                       
                        if field['type'] == 'phone' and (rawval or '').strip() and bad:
                            reason = 'invalid_phone'
                            break                      
                        if field.get('required') and val is None:
                            reason = f'missing_required:{name}'
                            break                        
                        if bad:
                            warnings += 1
                        values[name] = val
                    if reason:
                        store.inc('lead_rows_rejected'); store.inc('reject:'+reason)
                        store.db.execute('INSERT OR REPLACE INTO rejections VALUES(?,?,?,?)',(wf,file.stem,row_no,reason)); continue
                    phone=values.get('phone'); ph=store.phone_hash(phone)
                    ref_field=next(x['name'] for x in schema['fields'] if x.get('reference'))
                    ref=values.get(ref_field)
                    existing=store.db.execute('SELECT * FROM leads WHERE workflow_id=? AND phone_hash=?',(wf,ph)).fetchone()
                    pii_fields={fld['name'] for fld in schema['fields'] if fld.get('pii')}
                    safe_values={k:v for k,v in values.items() if k not in pii_fields}
                    if existing:
                        store.inc('duplicate_lead_rows_merged')
                        old=json.loads(existing['fields_json'])
                        for k,v in safe_values.items():
                            if v is not None: old[k]=v
                        # PII is kept only in encrypted columns, never in fields_json.
                        name_enc=store.enc(values['customer_name']) if values.get('customer_name') is not None else existing['pii_name_enc']
                        store.db.execute('UPDATE leads SET pii_name_enc=?,fields_json=?,external_ref=? WHERE id=?',
                          (name_enc,json.dumps(old,ensure_ascii=False),old.get(ref_field) or existing['external_ref'],existing['id']))
                    else:
                        for fld in schema['fields']:
                            if values.get(fld['name']) is None and 'default' in fld: values[fld['name']]=fld['default']
                        safe_values={k:v for k,v in values.items() if k not in pii_fields}
                        store.db.execute('INSERT INTO leads(workflow_id,phone_hash,pii_phone_enc,pii_name_enc,external_ref,batch_id,fields_json) VALUES(?,?,?,?,?,?,?)',
                          (wf,ph,store.enc(phone),store.enc(values.get('customer_name')),ref,file.stem,json.dumps(safe_values,ensure_ascii=False)))
                    store.inc('field_warnings',warnings)
            store.mark_file(file,digest,'leads'); store.db.commit()
    relink_orphans(store)


def load_dispositions(schema_dir: Path):
    out={}
    p=schema_dir/'dispositions.csv'
    with p.open(encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f): out[(r['workflow_id'],r['code'])]=(r['is_terminal'].lower()=='true',r['is_success'].lower()=='true')
    return out


def find_call_lead(store,wf,call_id):
    if not call_id: return None
    r=store.db.execute('''SELECT l.lead_id FROM event_links l JOIN events e ON e.event_id=l.event_id
      WHERE e.workflow_id=? AND e.call_id=? AND l.lead_id IS NOT NULL LIMIT 1''',(wf,call_id)).fetchone()
    return r['lead_id'] if r else None


def relink_orphans(store):
    # Re-evaluate unresolved phone-linked events after each lead batch; overrides resolve by call_id.
    rows=store.db.execute('''SELECT e.* FROM events e JOIN event_links x ON x.event_id=e.event_id
      WHERE x.lead_id IS NULL''').fetchall()
    for e in rows:
        lead_id=None
        if e['event_type']=='disposition.override': lead_id=find_call_lead(store,e['workflow_id'],e['call_id'])
        elif e['phone_hash']:
            r=store.db.execute('SELECT id FROM leads WHERE workflow_id=? AND phone_hash=?',(e['workflow_id'],e['phone_hash'])).fetchone()
            if r: lead_id=r['id']
        if lead_id: store.db.execute('UPDATE event_links SET lead_id=? WHERE event_id=?',(lead_id,e['event_id']))
    # A second pass links overrides after normal events were linked.
    rows=store.db.execute('''SELECT e.* FROM events e JOIN event_links x ON x.event_id=e.event_id
      WHERE x.lead_id IS NULL AND e.event_type='disposition.override' ''').fetchall()
    for e in rows:
        lid=find_call_lead(store,e['workflow_id'],e['call_id'])
        if lid: store.db.execute('UPDATE event_links SET lead_id=? WHERE event_id=?',(lid,e['event_id']))
    store.db.commit()


def ingest_events(store: Store, drop: Path, dispositions):
    root=drop/'events'
    if not root.exists(): return
    for file in sorted(root.glob('*.jsonl')):
        raw=file.read_bytes(); digest=hashlib.sha256(raw).hexdigest()
        if store.file_done(file,digest,'events'): continue
        for line in raw.decode('utf-8',errors='replace').splitlines():
            if not line.strip(): continue
            store.inc('events_total')
            try: ev=json.loads(line)
            except Exception: store.inc('malformed_events'); continue
            eid=ev.get('event_id')
            if not eid: store.inc('malformed_events'); continue
            if store.db.execute('SELECT 1 FROM seen_event_ids WHERE event_id=?',(str(eid),)).fetchone():
                store.inc('duplicate_events_dropped'); continue
            store.db.execute('INSERT INTO seen_event_ids VALUES(?)',(str(eid),))
            wf=str(ev.get('workflow_id') or ''); typ=str(ev.get('event_type') or '')
            payload=ev.get('payload') or {}; code=payload.get('disposition_code') if isinstance(payload,dict) else None
            if typ.startswith('disposition.') and (wf,str(code)) not in dispositions:
                store.inc('unknown_disposition_events'); continue
            try:
                ts=parse_timestamp(str(ev['occurred_at']))
            except Exception:
                store.inc('malformed_events'); continue
            phone_hash=None
            if typ!='disposition.override' and ev.get('phone'):
                canonical=normalize_phone(str(ev['phone']))
                if canonical: phone_hash=store.phone_hash(canonical)
            store.db.execute('''INSERT INTO events(event_id,event_type,workflow_id,call_id,occurred_at,occurred_ist,payload_json,phone_hash)
              VALUES(?,?,?,?,?,?,?,?)''',(str(eid),typ,wf,ev.get('call_id'),str(ev.get('occurred_at')),ts.isoformat(),json.dumps(payload,ensure_ascii=False),phone_hash))
            lead_id=None
            if typ=='disposition.override': lead_id=find_call_lead(store,wf,ev.get('call_id'))
            elif phone_hash:
                r=store.db.execute('SELECT id FROM leads WHERE workflow_id=? AND phone_hash=?',(wf,phone_hash)).fetchone()
                if r: lead_id=r['id']
            store.db.execute('INSERT INTO event_links VALUES(?,?)',(str(eid),lead_id))
        store.mark_file(file,digest,'events'); store.db.commit()
    relink_orphans(store)


def choose_effective_disposition(items):
    """items are (occurred_ist, event_type, code); any override takes precedence over sets."""
    overrides=[item for item in items if item[1]=='disposition.override']
    chosen=overrides if overrides else items
    return sorted(chosen, key=lambda x: x[0])[-1][2] if chosen else None


def lead_bucket(calls, effective_codes, dispositions, workflow_id):
    if not calls: return 'yet_to_reach_out'
    if any(dispositions[(workflow_id, code)][0] for code in effective_codes.values()): return 'completed'
    if all(call['telephony_failed'] for call in calls): return 'in_progress_telephony_failed'
    return 'in_progress_dialed'


def effective_dispositions(store, lead_id, dispositions):
    evs=store.db.execute('''SELECT e.* FROM events e JOIN event_links x ON x.event_id=e.event_id
      WHERE x.lead_id=? AND e.event_type IN ('disposition.set','disposition.override') ORDER BY e.occurred_ist''',(lead_id,)).fetchall()
    by_call=defaultdict(list)
    for e in evs:
        payload=json.loads(e['payload_json']); code=payload.get('disposition_code')
        if code in {c for (w,c) in dispositions if w==e['workflow_id']}:
            by_call[e['call_id']].append((e['occurred_ist'],0 if e['event_type']=='disposition.set' else 1,code))
    result={}
    for cid,items in by_call.items():
        result[cid]=choose_effective_disposition([(at,'disposition.override' if is_override else 'disposition.set',code) for at,is_override,code in items])
    return result


def build_reports(store: Store, out: Path, schemas, dispositions):
    out.mkdir(parents=True,exist_ok=True)
    leads=store.db.execute('SELECT * FROM leads ORDER BY workflow_id,external_ref').fetchall()
    call_rows=store.db.execute('''SELECT e.*,x.lead_id FROM events e JOIN event_links x ON x.event_id=e.event_id
      WHERE x.lead_id IS NOT NULL AND e.event_type='call.initiated' ORDER BY e.occurred_ist''').fetchall()
    calls_by_lead=defaultdict(list); calls_by_id={}
    for e in call_rows:
        c={'call_id':e['call_id'],'at':e['occurred_ist'],'date':parse_timestamp(e['occurred_ist']).date().isoformat(),'connected':False,'telephony_failed':False}
        calls_by_lead[e['lead_id']].append(c); calls_by_id[(e['workflow_id'],e['call_id'])]=c
    all_events=store.db.execute('''SELECT e.*,x.lead_id FROM events e JOIN event_links x ON x.event_id=e.event_id WHERE x.lead_id IS NOT NULL''').fetchall()
    for e in all_events:
        c=calls_by_id.get((e['workflow_id'],e['call_id']))
        if c:
            if e['event_type']=='call.connected': c['connected']=True
            if e['event_type']=='call.telephony_failed': c['telephony_failed']=True
    batch=defaultdict(lambda:defaultdict(int)); daily=defaultdict(lambda:{'calls':0,'connected':0,'success_leads':set()})
    lead_out=[]
    for l in leads:
        calls=calls_by_lead[l['id']]; eff=effective_dispositions(store,l['id'],dispositions)
        bucket=lead_bucket(calls,eff,dispositions,l['workflow_id'])
        b=batch[(l['workflow_id'],l['batch_id'])]; b['total_leads']+=1; b[bucket]+=1
        success=False
        for c in calls:
            code=eff.get(c['call_id']); d=daily[(l['workflow_id'],c['date'])]; d['calls']+=1; d['connected']+=int(c['connected'])
            if code and dispositions[(l['workflow_id'],code)][1]: success=True; d['success_leads'].add(l['id'])
        latest=max(calls,key=lambda x:x['at']) if calls else None
        # Most recent call that has an effective disposition.
        disp_calls=[c for c in calls if c['call_id'] in eff]
        current=eff[max(disp_calls,key=lambda x:x['at'])['call_id']] if disp_calls else ''
        phone=store.dec(l['pii_phone_enc'])
        masked=phone[:3]+'******'+phone[-4:] if phone else ''
        lead_out.append([l['workflow_id'],l['external_ref'],l['batch_id'],masked,bucket,len(calls),sum(c['connected'] for c in calls),latest['at'] if latest else '',current,str(success).lower()])
    def write_csv(name,header,rows):
        with (out/name).open('w',encoding='utf-8',newline='') as f:
            w=csv.writer(f,lineterminator='\n'); w.writerow(header); w.writerows(rows)
    bucket_names=['yet_to_reach_out','in_progress_telephony_failed','in_progress_dialed','completed']
    write_csv('batch_summary.csv',['workflow_id','batch_id','total_leads',*bucket_names],[[wf,bid,v['total_leads'],*[v[k] for k in bucket_names]] for (wf,bid),v in sorted(batch.items())])
    daily_rows=[]
    for (wf,dt),v in sorted(daily.items()):
        rate=v['connected']/v['calls'] if v['calls'] else 0
        daily_rows.append([wf,dt,v['calls'],v['connected'],f'{rate:.4f}',len(v['success_leads'])])
    write_csv('daily_overview.csv',['workflow_id','date','total_calls','connected_calls','connectivity_rate','success_leads'],daily_rows)
    write_csv('lead_report.csv',['workflow_id','external_ref','batch_id','phone_masked','bucket','total_calls','connected_calls','last_call_at','current_disposition','ever_success'],lead_out)
    rejects=store.db.execute('SELECT workflow_id,batch_id,row_number,reason FROM rejections ORDER BY workflow_id,batch_id,row_number').fetchall()
    write_csv('rejections.csv',['workflow_id','batch_id','row_number','reason'],[[r[x] for x in ('workflow_id','batch_id','row_number','reason')] for r in rejects])
    reasons=defaultdict(int)
    for r in rejects: reasons[r['reason']]+=1
    # Ensure observed reasons are all included, deterministic ordering.
    quality={'lead_rows_total':store.q('lead_rows_total'),'lead_rows_rejected':store.q('lead_rows_rejected'),'rejections_by_reason':dict(sorted(reasons.items())),
      'duplicate_lead_rows_merged':store.q('duplicate_lead_rows_merged'),'field_warnings':store.q('field_warnings'),'events_total':store.q('events_total'),
      'malformed_events':store.q('malformed_events'),'duplicate_events_dropped':store.q('duplicate_events_dropped'),
      'orphan_events':store.db.execute('SELECT COUNT(*) n FROM event_links WHERE lead_id IS NULL').fetchone()['n'],
      'unknown_disposition_events':store.q('unknown_disposition_events')}
    (out/'data_quality.json').write_text(json.dumps(quality,indent=2)+'\n',encoding='utf-8')


def main():
    ap=argparse.ArgumentParser(description='Interaction Ledger & Reporting Pipeline')
    ap.add_argument('--db',default=str(DEFAULT_DB)); ap.add_argument('--schemas',default=str(SCHEMA_DIR)); ap.add_argument('--key-file',default='.pii.key')
    sub=ap.add_subparsers(dest='cmd',required=True)
    ing=sub.add_parser('ingest'); ing.add_argument('drop')
    rep=sub.add_parser('report'); rep.add_argument('--out',default='reports')
    args=ap.parse_args(); schemas=load_schemas(Path(args.schemas)); dispositions=load_dispositions(Path(args.schemas))
    store=Store(Path(args.db),Path(args.key_file))
    if args.cmd=='ingest':
        drop=Path(args.drop); ingest_leads(store,drop,schemas); ingest_events(store,drop,dispositions); relink_orphans(store)
        print(f'Ingested {drop}; database={args.db}')
    else:
        build_reports(store,Path(args.out),schemas,dispositions); print(f'Reports written to {args.out}/')
    store.db.close()

if __name__=='__main__': main()
