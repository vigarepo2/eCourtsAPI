import os, re, csv, io, json, time, asyncio, logging, html
from datetime import datetime, timezone, date
from typing import Any, Optional
from urllib.parse import urljoin
import asyncpg, httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

APP_NAME=os.getenv("APP_NAME","eCourts Tracker")
DATABASE_URL=os.getenv("DATABASE_URL","postgresql://ecourts:ecourts@localhost:5432/ecourts")
SOURCE_API_BASE_URL=os.getenv("SOURCE_API_BASE_URL","").rstrip("/")
SOURCE_CASE_PATH=os.getenv("SOURCE_CASE_PATH","/case/{cino}")
SOURCE_ORDERS_PATH=os.getenv("SOURCE_ORDERS_PATH",SOURCE_CASE_PATH)
SOURCE_TIMEOUT=float(os.getenv("SOURCE_TIMEOUT","25"))
BASIC_AUTH_USERNAME=os.getenv("BASIC_AUTH_USERNAME")
BASIC_AUTH_PASSWORD=os.getenv("BASIC_AUTH_PASSWORD")
ORDER_WARN_SECONDS=int(os.getenv("ORDER_WARN_SECONDS","480"))
BULK_REFRESH_SLEEP=float(os.getenv("BULK_REFRESH_SLEEP","0.8"))
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("ecourts-tracker")
security=HTTPBasic(auto_error=False)
app=FastAPI(title=APP_NAME,version="1.0.0")
pool:asyncpg.Pool|None=None

class CaseIn(BaseModel):
    cino:str=Field(...,min_length=4,max_length=32)
    custom_title:Optional[str]=None
    notes:Optional[str]=None
    tags:list[str]=[]
    accused_aliases:list[str]=[]
    raw:Optional[dict[str,Any]]=None
class CasePatch(BaseModel):
    custom_title:Optional[str]=None; official_petitioner:Optional[str]=None; official_respondent:Optional[str]=None
    case_type:Optional[str]=None; fir_number:Optional[str]=None; fir_year:Optional[str]=None; police_station:Optional[str]=None
    court_name:Optional[str]=None; judge_designation:Optional[str]=None; next_date:Optional[str]=None; last_date:Optional[str]=None
    status:Optional[str]=None; purpose:Optional[str]=None; acts_sections:Optional[str]=None
    accused_aliases:Optional[list[str]]=None; advocates:Optional[list[str]]=None; notes:Optional[str]=None; tags:Optional[list[str]]=None
class BulkImportIn(BaseModel): items:str|list[str]; refresh:bool=False
class RestoreIn(BaseModel): data:dict[str,Any]; replace:bool=False

def now_iso(): return datetime.now(timezone.utc).isoformat()
def clean_text(v:Any)->str:
    if v is None: return ""
    if isinstance(v,(dict,list)): v=json.dumps(v,ensure_ascii=False)
    s=BeautifulSoup(str(v),"html.parser").get_text(" ")
    return re.sub(r"\s+"," ",html.unescape(s)).strip()
def parse_date(v:Any)->Optional[str]:
    s=clean_text(v)
    if not s: return None
    for fmt in ("%d-%m-%Y","%d/%m/%Y","%Y-%m-%d","%d.%m.%Y"):
        try: return datetime.strptime(s[:10],fmt).date().isoformat()
        except ValueError: pass
    return None
def array_text(v:Any)->list[str]:
    if not v: return []
    if isinstance(v,list): return [clean_text(x) for x in v if clean_text(x)]
    return [clean_text(x) for x in re.split(r"[,;\n|]+",str(v)) if clean_text(x)]
def split_fir(v:Any):
    s=clean_text(v)
    if "^" in s:
        p=[clean_text(x) for x in s.split("^")]
        return (p[0] if len(p)>0 else None, p[2] if len(p)>2 and re.fullmatch(r"\d{4}",p[2]) else None, p[1] if len(p)>1 else None)
    m=re.search(r"(\d+).*?(\d{4})",s)
    return (m.group(1),m.group(2),None) if m else (None,None,s or None)
def extract_orders(raw_html:Any,kind:str)->list[dict[str,Any]]:
    if not raw_html: return []
    if isinstance(raw_html,list):
        out=[]
        for o in raw_html:
            if isinstance(o,dict): out.append({"kind":kind,"label":clean_text(o.get("order_details") or o.get("label") or o),"order_date":parse_date(o.get("order_date") or o.get("date")),"source_url":o.get("url") or o.get("link") or o.get("source_url"),"raw":o,"link_generated_at":now_iso()})
            else: out.append({"kind":kind,"label":clean_text(o),"order_date":parse_date(o),"source_url":None,"raw":{"value":o},"link_generated_at":now_iso()})
        return out
    soup=BeautifulSoup(str(raw_html),"html.parser"); orders=[]
    for a in soup.find_all("a"):
        href=a.get("href") or a.get("onclick") or ""; label=clean_text(a.get_text(" ")) or "View order"; row=clean_text(a.parent.get_text(" ") if a.parent else label); dm=re.search(r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})",row)
        orders.append({"kind":kind,"label":label,"order_date":parse_date(dm.group(1)) if dm else None,"source_url":href,"row_text":row,"link_generated_at":now_iso()})
    if not orders and clean_text(raw_html):
        txt=clean_text(raw_html); orders.append({"kind":kind,"label":txt[:80],"order_date":parse_date(txt),"source_url":None,"row_text":txt,"link_generated_at":now_iso()})
    return orders

def normalize_case(raw:dict[str,Any],cino_hint:str="")->dict[str,Any]:
    data=raw.get("data",raw) or {}; hist=data.get("history",data.get("historyOfCase",data)) or {}
    if isinstance(hist,str): hist={"history_html":hist}
    cino=clean_text(data.get("cino") or hist.get("cino") or hist.get("cino_no") or cino_hint).upper()
    fir_no,fir_year,ps=split_fir(hist.get("fir_details") or data.get("fir_details"))
    pet=clean_text(hist.get("pet_name") or hist.get("petName") or data.get("pet_name")); res=clean_text(hist.get("res_name") or hist.get("resName") or data.get("res_name"))
    final_o=hist.get("finalOrder_html") or hist.get("finalOrder") or data.get("finalOrder"); interim_o=hist.get("interimOrder_html") or hist.get("interimOrder") or data.get("interimOrder")
    orders=extract_orders(interim_o,"interim")+extract_orders(final_o,"final")
    status="disposed" if clean_text(hist.get("date_of_decision") or hist.get("disp_name") or hist.get("archive") or data.get("date_of_decision")) else "pending"
    hearings=hist.get("historyOfCaseHearing") or data.get("historyOfCaseHearing") or []
    return {"cino":cino,"official_petitioner":pet,"official_respondent":res,"display_title":" vs. ".join([x for x in [pet,res] if x]) or cino,"case_type":clean_text(hist.get("type_name") or hist.get("case_type") or data.get("case_type")),"fir_number":fir_no,"fir_year":fir_year,"police_station":ps,"court_name":clean_text(hist.get("court_name") or data.get("court_name")),"judge_designation":clean_text(hist.get("desgname") or hist.get("judge") or data.get("desgname")),"next_date":parse_date(hist.get("date_next_list") or data.get("date_next_list")),"last_date":parse_date(hist.get("date_last_list") or data.get("date_last_list")),"status":status,"purpose":clean_text(hist.get("purpose_name") or data.get("purpose_name")),"acts_sections":clean_text(hist.get("act") or hist.get("acts") or data.get("act")),"extra_names":array_text(hist.get("str_error1") or hist.get("petNameAdd") or hist.get("resNameAdd")),"advocates":array_text(hist.get("advocates") or hist.get("advocate") or data.get("advocates")),"hearings":hearings if isinstance(hearings,list) else [],"orders":orders,"processes":hist.get("processes") or data.get("processes") or [],"transfer_history":hist.get("transfer") or data.get("transfer") or [],"raw":raw}

async def db():
    assert pool is not None
    async with pool.acquire() as conn: yield conn
async def init_db():
    global pool
    pool=await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=int(os.getenv("DB_POOL_MAX","8")))
    sql="""
    create table if not exists cases(id bigserial primary key,cino text unique not null,custom_title text,official_petitioner text,official_respondent text,display_title text,case_type text,fir_number text,fir_year text,police_station text,court_name text,judge_designation text,next_date date,last_date date,status text default 'pending',purpose text,acts_sections text,extra_names jsonb default '[]',accused_aliases jsonb default '[]',advocates jsonb default '[]',notes text,tags jsonb default '[]',raw jsonb default '{}',created_at timestamptz default now(),updated_at timestamptz default now(),last_refresh_at timestamptz);
    create table if not exists hearing_history(id bigserial primary key,case_id bigint references cases(id) on delete cascade,hearing_date date,purpose text,judge text,business text,raw jsonb default '{}');
    create table if not exists orders(id bigserial primary key,case_id bigint references cases(id) on delete cascade,kind text,order_no text,order_date date,label text,source_url text,row_text text,raw jsonb default '{}',link_generated_at timestamptz,last_refreshed_at timestamptz default now());
    create table if not exists processes(id bigserial primary key,case_id bigint references cases(id) on delete cascade,process_type text,process_date date,party text,raw jsonb default '{}');
    create table if not exists transfer_history(id bigserial primary key,case_id bigint references cases(id) on delete cascade,transfer_date date,from_court text,to_court text,raw jsonb default '{}');
    create table if not exists refresh_logs(id bigserial primary key,case_id bigint references cases(id) on delete set null,cino text,action text,ok boolean,message text,created_at timestamptz default now());
    create table if not exists settings(key text primary key,value jsonb,updated_at timestamptz default now());
    create table if not exists backup_meta(id bigserial primary key,label text,counts jsonb,created_at timestamptz default now());
    create index if not exists idx_cases_cino on cases(cino); create index if not exists idx_cases_status on cases(status); create index if not exists idx_cases_next_date on cases(next_date);
    """
    async with pool.acquire() as c: await c.execute(sql)
@app.on_event("startup")
async def startup(): await init_db()
@app.on_event("shutdown")
async def shutdown():
    if pool: await pool.close()
async def require_auth(req:Request,creds:HTTPBasicCredentials=Depends(security)):
    if not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD: return True
    import secrets
    if not creds or not (secrets.compare_digest(creds.username,BASIC_AUTH_USERNAME) and secrets.compare_digest(creds.password,BASIC_AUTH_PASSWORD)):
        raise HTTPException(status_code=401,detail="Authentication required",headers={"WWW-Authenticate":"Basic"})
    return True
@app.middleware("http")
async def reqlog(request:Request,call_next):
    t=time.time()
    try: resp=await call_next(request)
    except Exception as e: log.exception("request failed"); return JSONResponse({"success":False,"error":str(e)},status_code=500)
    log.info("%s %s %s %.1fms",request.method,request.url.path,resp.status_code,(time.time()-t)*1000); return resp

def ok(data=None,**kw): return {"success":True,"data":data,**kw}
def fail(msg,code=400): raise HTTPException(code,detail=msg)
async def call_source(path,cino):
    if not SOURCE_API_BASE_URL: fail("SOURCE_API_BASE_URL not configured",503)
    url=urljoin(SOURCE_API_BASE_URL+"/",path.format(cino=cino).lstrip("/")); last=None
    async with httpx.AsyncClient(timeout=SOURCE_TIMEOUT,follow_redirects=True) as client:
        for i in range(3):
            try:
                r=await client.get(url); r.raise_for_status(); js=r.json()
                if js.get("success") is False: raise RuntimeError(js.get("error") or js.get("message") or "source returned failure")
                return js
            except Exception as e: last=e; await asyncio.sleep(.7*(i+1))
    raise RuntimeError(f"source fetch failed for {cino}: {last}")
async def fetch_case_by_cino(cino): return await call_source(SOURCE_CASE_PATH,cino)
async def refresh_case_orders(cino): return await call_source(SOURCE_ORDERS_PATH,cino)

async def upsert_case(conn,norm,manual=None):
    manual=manual or {}
    row=await conn.fetchrow("""insert into cases(cino,custom_title,official_petitioner,official_respondent,display_title,case_type,fir_number,fir_year,police_station,court_name,judge_designation,next_date,last_date,status,purpose,acts_sections,extra_names,accused_aliases,advocates,notes,tags,raw,last_refresh_at,updated_at) values($1,$2,$3,$4,coalesce($2,$5),$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb,$18::jsonb,$19::jsonb,$20,$21::jsonb,$22::jsonb,now(),now()) on conflict(cino) do update set official_petitioner=excluded.official_petitioner,official_respondent=excluded.official_respondent,display_title=coalesce(cases.custom_title,excluded.display_title),case_type=excluded.case_type,fir_number=excluded.fir_number,fir_year=excluded.fir_year,police_station=excluded.police_station,court_name=excluded.court_name,judge_designation=excluded.judge_designation,next_date=excluded.next_date,last_date=excluded.last_date,status=excluded.status,purpose=excluded.purpose,acts_sections=excluded.acts_sections,extra_names=excluded.extra_names,advocates=excluded.advocates,raw=excluded.raw,last_refresh_at=now(),updated_at=now() returning id""",norm["cino"],manual.get("custom_title"),norm.get("official_petitioner"),norm.get("official_respondent"),norm.get("display_title"),norm.get("case_type"),norm.get("fir_number"),norm.get("fir_year"),norm.get("police_station"),norm.get("court_name"),norm.get("judge_designation"),norm.get("next_date"),norm.get("last_date"),norm.get("status"),norm.get("purpose"),norm.get("acts_sections"),json.dumps(norm.get("extra_names",[])),json.dumps(manual.get("accused_aliases",[])),json.dumps(norm.get("advocates",[])),manual.get("notes"),json.dumps(manual.get("tags",[])),json.dumps(norm.get("raw",{})))
    cid=row["id"]; await conn.execute("delete from hearing_history where case_id=$1; delete from orders where case_id=$1; delete from processes where case_id=$1; delete from transfer_history where case_id=$1",cid)
    for h in norm.get("hearings",[]):
        await conn.execute("insert into hearing_history(case_id,hearing_date,purpose,judge,business,raw) values($1,$2,$3,$4,$5,$6::jsonb)",cid,parse_date(h.get("date") if isinstance(h,dict) else h),clean_text(h.get("purpose") if isinstance(h,dict) else h),clean_text(h.get("judge") if isinstance(h,dict) else ""),clean_text(h.get("business") if isinstance(h,dict) else h),json.dumps(h))
    for o in norm.get("orders",[]):
        await conn.execute("insert into orders(case_id,kind,order_no,order_date,label,source_url,row_text,raw,link_generated_at,last_refreshed_at) values($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,now())",cid,o.get("kind"),o.get("order_no"),o.get("order_date"),clean_text(o.get("label")),o.get("source_url"),clean_text(o.get("row_text")),json.dumps(o.get("raw",o)),o.get("link_generated_at"))
    for p in norm.get("processes",[]) if isinstance(norm.get("processes"),list) else []:
        await conn.execute("insert into processes(case_id,process_type,process_date,party,raw) values($1,$2,$3,$4,$5::jsonb)",cid,clean_text((p.get("type") or p.get("process")) if isinstance(p,dict) else p),parse_date(p.get("date") if isinstance(p,dict) else p),clean_text(p.get("party") if isinstance(p,dict) else ""),json.dumps(p))
    for tr in norm.get("transfer_history",[]) if isinstance(norm.get("transfer_history"),list) else []:
        await conn.execute("insert into transfer_history(case_id,transfer_date,from_court,to_court,raw) values($1,$2,$3,$4,$5::jsonb)",cid,parse_date(tr.get("date") if isinstance(tr,dict) else tr),clean_text(tr.get("from") if isinstance(tr,dict) else ""),clean_text(tr.get("to") if isinstance(tr,dict) else ""),json.dumps(tr))
    return cid
async def full_case(conn,case_id:int):
    c=await conn.fetchrow("select * from cases where id=$1",case_id)
    if not c: return None
    d=dict(c)
    for k in ["extra_names","accused_aliases","advocates","tags","raw"]:
        if isinstance(d.get(k),str): d[k]=json.loads(d[k])
    d["hearings"]=[dict(x) for x in await conn.fetch("select * from hearing_history where case_id=$1 order by hearing_date desc nulls last,id desc",case_id)]
    d["orders"]=[]
    for x in await conn.fetch("select *,extract(epoch from(now()-coalesce(link_generated_at,last_refreshed_at))) age_seconds from orders where case_id=$1 order by order_date desc nulls last,id desc",case_id):
        od=dict(x); od["expired_likely"]=(od.get("age_seconds") or 999999)>ORDER_WARN_SECONDS; d["orders"].append(od)
    d["processes"]=[dict(x) for x in await conn.fetch("select * from processes where case_id=$1 order by id desc",case_id)]
    d["transfer_history"]=[dict(x) for x in await conn.fetch("select * from transfer_history where case_id=$1 order by id desc",case_id)]
    return d
def ser(x): return x.isoformat() if isinstance(x,(datetime,date)) else x

@app.get("/api/health",dependencies=[Depends(require_auth)])
async def health(conn=Depends(db)): await conn.fetchval("select 1"); return ok({"app":APP_NAME,"db":"ok","source_configured":bool(SOURCE_API_BASE_URL)})
@app.get("/api/cases",dependencies=[Depends(require_auth)])
async def list_cases(q:str="",filter:str="",limit:int=100,offset:int=0,conn=Depends(db)):
    wh=[]; args=[]
    if q:
        args.append(f"%{q.lower()}%"); wh.append("(lower(cino) like $1 or lower(coalesce(custom_title,'')) like $1 or lower(coalesce(display_title,'')) like $1 or lower(coalesce(official_petitioner,'')) like $1 or lower(coalesce(official_respondent,'')) like $1 or lower(coalesce(fir_number,'')) like $1 or lower(coalesce(fir_year,'')) like $1 or lower(coalesce(police_station,'')) like $1 or lower(coalesce(court_name,'')) like $1 or lower(coalesce(judge_designation,'')) like $1 or lower(coalesce(acts_sections,'')) like $1 or lower(coalesce(notes,'')) like $1 or lower(extra_names::text) like $1 or lower(accused_aliases::text) like $1 or lower(advocates::text) like $1 or lower(tags::text) like $1)")
    mapf={"pending":"status='pending'","disposed":"status='disposed'","upcoming":"next_date between current_date and current_date+interval '30 days'","no-next-date":"next_date is null","fir":"fir_number is not null","custom-title":"custom_title is not null and custom_title<>''","has-orders":"exists(select 1 from orders o where o.case_id=cases.id)","expired-orders":"exists(select 1 from orders o where o.case_id=cases.id and now()-coalesce(o.link_generated_at,o.last_refreshed_at)>interval '8 minutes')","process":"exists(select 1 from processes p where p.case_id=cases.id)"}
    if filter in mapf: wh.append(mapf[filter])
    if filter in ("nbw","surety","warrant"): wh.append(f"exists(select 1 from processes p where p.case_id=cases.id and lower(p.raw::text) like '%{filter}%')")
    where=" where "+" and ".join(wh) if wh else ""
    rows=[dict(r) for r in await conn.fetch(f"select *, (select count(*) from orders o where o.case_id=cases.id) order_count from cases {where} order by coalesce(next_date,'2999-12-31'),updated_at desc limit {min(limit,500)} offset {offset}",*args)]
    stats=dict(await conn.fetchrow("select count(*) total,count(*) filter(where status='pending') pending,count(*) filter(where status='disposed') disposed,count(*) filter(where next_date between current_date and current_date+interval '30 day') upcoming from cases"))
    return ok(rows,stats=stats)
@app.get("/api/search",dependencies=[Depends(require_auth)])
async def search(q:str,conn=Depends(db)): return await list_cases(q=q,conn=conn)
@app.post("/api/cases",dependencies=[Depends(require_auth)])
async def add_case(inp:CaseIn,conn=Depends(db)):
    cino=clean_text(inp.cino).upper()
    try:
        raw=inp.raw or await fetch_case_by_cino(cino); cid=await upsert_case(conn,normalize_case(raw,cino),inp.model_dump())
        await conn.execute("insert into refresh_logs(case_id,cino,action,ok,message) values($1,$2,'add',true,'added/refreshed')",cid,cino)
        return ok(await full_case(conn,cid))
    except Exception as e:
        await conn.execute("insert into refresh_logs(cino,action,ok,message) values($1,'add',false,$2)",cino,str(e)); fail(str(e),502)
@app.get("/api/cases/{case_id}",dependencies=[Depends(require_auth)])
async def get_case(case_id:int,conn=Depends(db)):
    d=await full_case(conn,case_id)
    if not d: fail("case not found",404)
    return ok(d)
@app.patch("/api/cases/{case_id}",dependencies=[Depends(require_auth)])
async def patch_case(case_id:int,patch:CasePatch,conn=Depends(db)):
    data=patch.model_dump(exclude_unset=True); sets=[]; vals=[]; i=1
    for k,v in data.items():
        if k in ("accused_aliases","advocates","tags"): vals.append(json.dumps(v)); sets.append(f"{k}=${i}::jsonb")
        else: vals.append(v); sets.append(f"{k}=${i}")
        i+=1
    if sets:
        vals.append(case_id); await conn.execute(f"update cases set {','.join(sets)},display_title=coalesce(custom_title,display_title),updated_at=now() where id=${i}",*vals)
    return ok(await full_case(conn,case_id))
@app.delete("/api/cases/{case_id}",dependencies=[Depends(require_auth)])
async def delete_case(case_id:int,conn=Depends(db)): return ok({"deleted":await conn.execute("delete from cases where id=$1",case_id)})
async def refresh_by_id(conn,case_id:int,orders_only=False):
    c=await conn.fetchrow("select cino,custom_title,notes,tags,accused_aliases from cases where id=$1",case_id)
    if not c: fail("case not found",404)
    raw=await (refresh_case_orders(c["cino"]) if orders_only else fetch_case_by_cino(c["cino"]))
    manual={"custom_title":c["custom_title"],"notes":c["notes"],"tags":c["tags"],"accused_aliases":c["accused_aliases"]}
    cid=await upsert_case(conn,normalize_case(raw,c["cino"]),manual)
    await conn.execute("insert into refresh_logs(case_id,cino,action,ok,message) values($1,$2,$3,true,'ok')",cid,c["cino"],"refresh-orders" if orders_only else "refresh")
    return await full_case(conn,cid)
@app.post("/api/cases/{case_id}/refresh",dependencies=[Depends(require_auth)])
async def refresh_case(case_id:int,conn=Depends(db)): return ok(await refresh_by_id(conn,case_id,False))
@app.post("/api/cases/{case_id}/refresh-orders",dependencies=[Depends(require_auth)])
async def refresh_orders(case_id:int,conn=Depends(db)): return ok(await refresh_by_id(conn,case_id,True))
@app.post("/api/refresh-all",dependencies=[Depends(require_auth)])
async def refresh_all(orders_only:bool=False,selected_date:Optional[str]=None,conn=Depends(db)):
    rows=await (conn.fetch("select id from cases where next_date=$1 order by updated_at",selected_date) if selected_date else conn.fetch("select id from cases order by updated_at")); out=[]
    for r in rows:
        try: out.append({"id":r["id"],"success":True,"case":await refresh_by_id(conn,r["id"],orders_only)})
        except Exception as e: out.append({"id":r["id"],"success":False,"error":str(e)})
        await asyncio.sleep(BULK_REFRESH_SLEEP)
    return ok(out)
@app.post("/api/bulk-import",dependencies=[Depends(require_auth)])
async def bulk_import(inp:BulkImportIn,conn=Depends(db)):
    items=inp.items if isinstance(inp.items,list) else re.split(r"[\s,;]+",inp.items); out=[]
    for cino in [clean_text(x).upper() for x in items if clean_text(x)]:
        try:
            if inp.refresh: cid=await upsert_case(conn,normalize_case(await fetch_case_by_cino(cino),cino),{})
            else: cid=(await conn.fetchrow("insert into cases(cino,display_title) values($1,$1) on conflict(cino) do update set updated_at=now() returning id",cino))["id"]
            out.append({"cino":cino,"success":True,"id":cid})
        except Exception as e: out.append({"cino":cino,"success":False,"error":str(e)})
        await asyncio.sleep(BULK_REFRESH_SLEEP)
    return ok(out)
@app.get("/api/export.json",dependencies=[Depends(require_auth)])
async def export_json(conn=Depends(db)):
    cases=[await full_case(conn,r["id"]) for r in await conn.fetch("select id from cases order by id")]
    return JSONResponse(ok({"exported_at":now_iso(),"cases":cases}),default=ser)
@app.get("/api/export.csv",dependencies=[Depends(require_auth)])
async def export_csv(conn=Depends(db)):
    rows=[dict(r) for r in await conn.fetch("select id,cino,display_title,custom_title,official_petitioner,official_respondent,case_type,fir_number,fir_year,police_station,court_name,judge_designation,next_date,last_date,status,purpose,acts_sections,notes,tags,updated_at,last_refresh_at from cases order by id")]
    buf=io.StringIO(); fields=list(rows[0].keys()) if rows else ["empty"]; w=csv.DictWriter(buf,fieldnames=fields); w.writeheader(); [w.writerow({k:ser(v) for k,v in row.items()}) for row in rows]
    return StreamingResponse(iter([buf.getvalue()]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=ecourts-cases.csv"})
@app.post("/api/backup/restore",dependencies=[Depends(require_auth)])
async def restore_backup(inp:RestoreIn,conn=Depends(db)):
    cases=inp.data.get("cases",[])
    if inp.replace: await conn.execute("truncate cases restart identity cascade")
    for c in cases: await upsert_case(conn,normalize_case(c.get("raw") or {"data":{"history":c}},c.get("cino")),{"custom_title":c.get("custom_title"),"notes":c.get("notes"),"tags":c.get("tags",[]),"accused_aliases":c.get("accused_aliases",[])})
    await conn.execute("insert into backup_meta(label,counts) values('restore',$1::jsonb)",json.dumps({"cases":len(cases)})); return ok({"restored":len(cases)})
@app.get("/api/orders/{order_id}/open",dependencies=[Depends(require_auth)])
async def open_order(order_id:int,conn=Depends(db)):
    o=await conn.fetchrow("select o.*,c.id case_id from orders o join cases c on c.id=o.case_id where o.id=$1",order_id)
    if not o: fail("order not found",404)
    age=await conn.fetchval("select extract(epoch from(now()-coalesce(link_generated_at,last_refreshed_at))) from orders where id=$1",order_id)
    if age and age>ORDER_WARN_SECONDS: await refresh_by_id(conn,o["case_id"],True); o=await conn.fetchrow("select * from orders where id=$1",order_id) or o
    if not o["source_url"]: fail("order URL not available; refresh source API first",404)
    return ok({"url":o["source_url"]})

CSS=''' :root{--bg:#f7f8fb;--card:#fff;--ink:#172033;--muted:#667085;--line:#e7eaf0;--green:#0f9f6e;--red:#dc3f4f;--amber:#b7791f;--shadow:0 16px 45px #1d29391a}*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Arial;color:var(--ink)}button,input,textarea{font:inherit}button{border:0;border-radius:14px;padding:12px 15px;background:#121826;color:white;font-weight:750;cursor:pointer}.ghost{background:white!important;color:var(--ink)!important;border:1px solid var(--line)}.danger{background:var(--red)!important}.good{background:var(--green)!important}.small{padding:8px 10px;border-radius:11px;font-size:12px}.top{position:sticky;top:0;z-index:10;background:#ffffffd9;backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}.bar{max-width:1220px;margin:auto;padding:14px;display:flex;gap:10px;align-items:center}.brand{font-weight:900;font-size:18px;letter-spacing:-.02em}.search{flex:1;display:flex;gap:8px}.search input,input,textarea{width:100%;border:1px solid var(--line);border-radius:15px;padding:13px 14px;background:white;outline:none}.wrap{max-width:1220px;margin:auto;padding:18px 14px 60px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat,.card,.panel{background:var(--card);border:1px solid var(--line);border-radius:24px;box-shadow:var(--shadow)}.stat{padding:16px}.stat b{font-size:28px;display:block}.stat span,.muted{color:var(--muted);font-size:13px}.layout{display:grid;grid-template-columns:260px 1fr;gap:16px;margin-top:16px}.side{padding:14px;height:max-content;position:sticky;top:82px}.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{padding:7px 10px;border-radius:999px;background:#f2f4f7;color:#344054;font-size:12px;font-weight:750}.pending{background:#e8f8f1;color:#08734f}.disposed,.red{background:#fdecef;color:#b42335}.amber{background:#fff4df;color:#9a5b00}.case{padding:15px;margin-bottom:12px}.case h3{margin:0 0 8px;font-size:17px;letter-spacing:-.02em}.meta{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.modal{position:fixed;inset:0;background:#0b122099;z-index:30;display:none;align-items:flex-end}.modal.show{display:flex}.sheet{background:white;width:100%;max-height:92vh;overflow:auto;border-radius:28px 28px 0 0;padding:18px}.sheet-inner{max-width:1000px;margin:auto}.section{padding:14px;border:1px solid var(--line);border-radius:18px;margin:12px 0;background:#fff}.listrow{display:flex;justify-content:space-between;gap:10px;border-top:1px solid var(--line);padding:10px 0}.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#111827;color:white;padding:12px 16px;border-radius:15px;display:none;z-index:99}.toast.show{display:block}.empty{text-align:center;color:var(--muted);padding:60px 20px}.spinner{width:22px;height:22px;border:3px solid #d0d5dd;border-top-color:#111827;border-radius:50%;animation:spin 1s linear infinite;display:none}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:820px){.bar{align-items:stretch;flex-wrap:wrap}.brand{width:100%}.search{order:2;width:100%;flex-basis:100%}.stats{grid-template-columns:repeat(2,1fr)}.layout{display:block}.side{position:static;margin-bottom:14px}.grid{grid-template-columns:1fr}.hide-m{display:none}.actions button{flex:1}.modal{align-items:stretch}.sheet{border-radius:0;max-height:100vh}.listrow{display:block}} '''
HTML=f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>eCourts Tracker</title><style>{CSS}</style></head><body><div class=top><div class=bar><div class=brand>eCourts Tracker</div><div class=search><input id=q placeholder='Search CNR, FIR, title, accused, advocate, section, court...'><button onclick='load()'>Search</button></div><button class=ghost onclick='openAdd()'>Add</button><button class='ghost hide-m' onclick='refreshAll()'>Refresh all</button><div class=spinner id=spin></div></div></div><div class=wrap><div class=stats><div class=stat><span>Total</span><b id=stTotal>0</b></div><div class=stat><span>Pending</span><b id=stPending>0</b></div><div class=stat><span>Disposed</span><b id=stDisposed>0</b></div><div class=stat><span>Upcoming</span><b id=stUpcoming>0</b></div></div><div class=layout><aside class='panel side'><b>Filters</b><div class=chips id=filters></div><div class=section><b>Bulk import</b><textarea id=bulk rows=5 placeholder='Paste CNR/CINO list'></textarea><div class=actions><button class='small good' onclick='bulkImport(true)'>Import + refresh</button><button class='small ghost' onclick='bulkImport(false)'>Import only</button></div></div><div class=actions><button class='ghost small' onclick='location.href=\"/api/export.json\"'>Export JSON</button><button class='ghost small' onclick='location.href=\"/api/export.csv\"'>Export CSV</button></div></aside><main><div id=cases></div></main></div></div><div class=modal id=modal><div class=sheet><div class=sheet-inner><div class=actions style='justify-content:space-between'><button class=ghost onclick='closeModal()'>Close</button><button class=danger id=delBtn>Delete</button></div><div id=detail></div></div></div></div><div class=toast id=toast></div><script>
let FILTER='',CURRENT=null;const filters=['','pending','disposed','upcoming','no-next-date','fir','custom-title','has-orders','expired-orders','nbw','surety','warrant'];const $=id=>document.getElementById(id);function spin(v){{$('spin').style.display=v?'block':'none'}}function toast(t){{$('toast').textContent=t;$('toast').classList.add('show');setTimeout(()=>$('toast').classList.remove('show'),2600)}}async function api(path,opts={{}}){{spin(true);try{{let r=await fetch(path,{{headers:{{'content-type':'application/json'}},...opts}});let j=await r.json();if(!r.ok||!j.success)throw new Error(j.detail||j.error||'Request failed');return j.data}}catch(e){{toast(e.message);throw e}}finally{{spin(false)}}}}function safe(s){{return (s??'').toString().replace(/[&<>]/g,m=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[m]))}}function chip(s){{return `<span class='chip ${{s}}'>${{s||'all'}}</span>`}}function buildFilters(){{$('filters').innerHTML=filters.map(f=>`<button class='small ${{FILTER==f?'good':'ghost'}}' onclick='FILTER="${{f}}";load()'>${{f||'all'}}</button>`).join('')}}async function load(){{buildFilters();let j=await api(`/api/cases?q=${{encodeURIComponent($('q').value)}}&filter=${{FILTER}}`);$('stTotal').textContent=j.stats.total;$('stPending').textContent=j.stats.pending;$('stDisposed').textContent=j.stats.disposed;$('stUpcoming').textContent=j.stats.upcoming;let rows=j;if(!rows.length){{$('cases').innerHTML='<div class="card empty">No cases yet. Add CNR/CINO or bulk import.</div>';return}}$('cases').innerHTML=rows.map(c=>`<div class='card case'><div class=actions style='justify-content:space-between;margin:0'><h3>${{safe(c.display_title||c.cino)}}</h3>${{chip(c.status)}}</div><div class=muted>${{safe(c.cino)}} · ${{safe(c.court_name||'Court not set')}}</div><div class=meta>${{c.next_date?`<span class='chip pending'>Next ${{c.next_date}}</span>`:`<span class='chip amber'>No next date</span>`}}${{c.fir_number?`<span class=chip>FIR ${{safe(c.fir_number)}}/${{safe(c.fir_year||'')}}</span>`:''}}${{c.order_count?`<span class='chip amber'>${{c.order_count}} orders</span>`:''}}</div><div class=grid><div><b>Purpose</b><br><span class=muted>${{safe(c.purpose||'-')}}</span></div><div><b>Judge/Court</b><br><span class=muted>${{safe(c.judge_designation||'-')}}</span></div></div><div class=actions><button class=small onclick='detail(${{c.id}})'>Open</button><button class='small ghost' onclick='refreshCase(${{c.id}})'>Refresh</button><button class='small ghost' onclick='refreshOrders(${{c.id}})'>Orders</button></div></div>`).join('')}}function openAdd(){{CURRENT=null;$('detail').innerHTML=`<h2>Add case</h2><div class=grid><input id=addCino placeholder=CNR/CINO><input id=addTitle placeholder='Custom title for hidden case'></div><input id=addAliases placeholder='Accused / alias names, comma separated'><textarea id=addNotes placeholder=Notes></textarea><div class=actions><button class=good onclick='addCase()'>Add + fetch</button></div>`;$('delBtn').style.display='none';$('modal').classList.add('show')}}async function addCase(){{await api('/api/cases',{{method:'POST',body:JSON.stringify({{cino:$('addCino').value,custom_title:$('addTitle').value,accused_aliases:$('addAliases').value.split(',').map(x=>x.trim()).filter(Boolean),notes:$('addNotes').value}})}});closeModal();toast('Case added');load()}}async function detail(id){{let c=await api('/api/cases/'+id);CURRENT=c;$('delBtn').style.display='inline-block';$('delBtn').onclick=()=>delCase(id);$('detail').innerHTML=`<h2>${{safe(c.display_title)}}</h2><div class=meta>${{chip(c.status)}}${{c.next_date?`<span class='chip pending'>Next ${{c.next_date}}</span>`:''}}${{c.last_refresh_at?`<span class=chip>Refresh ${{new Date(c.last_refresh_at).toLocaleString()}}</span>`:''}}</div><div class=section><div class=grid><div><b>CNR</b><br>${{safe(c.cino)}}</div><div><b>FIR</b><br>${{safe(c.fir_number||'-')}}/${{safe(c.fir_year||'')}}</div><div><b>Police Station</b><br>${{safe(c.police_station||'-')}}</div><div><b>Purpose</b><br>${{safe(c.purpose||'-')}}</div><div><b>Court</b><br>${{safe(c.court_name||'-')}}</div><div><b>Judge</b><br>${{safe(c.judge_designation||'-')}}</div></div></div><div class=section><b>Edit metadata</b><div class=grid><input id=eTitle value='${{safe(c.custom_title||'')}}' placeholder='Custom display title'><input id=eAliases value='${{safe((c.accused_aliases||[]).join(', '))}}' placeholder='Aliases / accused names'></div><textarea id=eNotes rows=3 placeholder=Notes>${{safe(c.notes||'')}}</textarea><div class=actions><button class='good small' onclick='saveMeta(${{c.id}})'>Save</button><button class='ghost small' onclick='refreshCase(${{c.id}})'>Refresh case</button><button class='ghost small' onclick='refreshOrders(${{c.id}})'>Refresh order links</button></div></div><div class=section><b>Orders</b>${{(c.orders||[]).map(o=>`<div class=listrow><div>${{safe(o.label||o.kind)}}<br><span class=muted>${{safe(o.order_date||'No date')}} ${{o.expired_likely?'<span class="chip red">expired likely</span>':''}}</span></div><button class='small ghost' onclick='openOrder(${{o.id}})'>View</button></div>`).join('')||'<p class=muted>No orders stored.</p>'}}</div><div class=section><b>Processes</b>${{(c.processes||[]).map(p=>`<div class=listrow><div>${{safe(p.process_type||JSON.stringify(p.raw))}}</div></div>`).join('')||'<p class=muted>No process data.</p>'}}</div><div class=section><b>Hearing history</b>${{(c.hearings||[]).map(h=>`<details class=listrow><summary>${{safe(h.hearing_date||'Date not found')}} · ${{safe(h.purpose||'')}}</summary><p class=muted>${{safe(h.business||JSON.stringify(h.raw))}}</p></details>`).join('')||'<p class=muted>No hearing history.</p>'}}</div>`;$('modal').classList.add('show')}}async function saveMeta(id){{await api('/api/cases/'+id,{{method:'PATCH',body:JSON.stringify({{custom_title:$('eTitle').value,accused_aliases:$('eAliases').value.split(',').map(x=>x.trim()).filter(Boolean),notes:$('eNotes').value}})}});toast('Saved');detail(id);load()}}async function refreshCase(id){{await api('/api/cases/'+id+'/refresh',{{method:'POST'}});toast('Case refreshed');if(CURRENT)detail(id);load()}}async function refreshOrders(id){{await api('/api/cases/'+id+'/refresh-orders',{{method:'POST'}});toast('Order links refreshed');if(CURRENT)detail(id);load()}}async function openOrder(id){{let d=await api('/api/orders/'+id+'/open');window.open(d.url,'_blank','noopener')}}async function delCase(id){{if(confirm('Delete this case from Postgres?')){{await api('/api/cases/'+id,{{method:'DELETE'}});closeModal();toast('Deleted');load()}}}}async function refreshAll(){{if(confirm('Refresh all cases?')){{await api('/api/refresh-all',{{method:'POST'}});toast('Refresh finished');load()}}}}async function bulkImport(refresh){{await api('/api/bulk-import',{{method:'POST',body:JSON.stringify({{items:$('bulk').value,refresh}})}});toast('Bulk import done');$('bulk').value='';load()}}function closeModal(){{$('modal').classList.remove('show');CURRENT=null}}$('q').addEventListener('keydown',e=>{{if(e.key==='Enter')load()}});load();</script></body></html>"""
@app.get("/",response_class=HTMLResponse,dependencies=[Depends(require_auth)])
async def index(): return HTML
@app.get("/robots.txt")
async def robots(): return PlainTextResponse("User-agent: *\nDisallow: /\n")
