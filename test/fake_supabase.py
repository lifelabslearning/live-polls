#!/usr/bin/env python3
"""A small stand-in for the Supabase edge (PostgREST RPC, GoTrue anonymous sign-up, Realtime
broadcast) in front of the local PostgreSQL that supabase-sessions.sql was applied to.
It exists so the pages can be driven end to end by Playwright inside this workspace, which
cannot reach supabase.co. Not a general PostgREST; only what the pages use.

  python3 fake_supabase.py --db "dbname=llpoll user=postgres password=postgres host=127.0.0.1" \
      --serve /path/to/index.html --port 8787
"""
import argparse, asyncio, json, re, uuid, pathlib
import psycopg2, psycopg2.extras
from aiohttp import web, WSMsgType

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--serve", action="append", default=[], help="path[:urlpath] files to serve")
ap.add_argument("--port", type=int, default=8787)
ap.add_argument("--key", default="local-anon-key")
args = ap.parse_args()

psycopg2.extras.register_uuid()
conn = psycopg2.connect(args.db); conn.autocommit = True

def q(sql, params=()):
    cur = conn.cursor(); cur.execute(sql, params)
    rows = cur.fetchall() if cur.description else []
    cur.close(); return rows

def role_of(req):
    auth = (req.headers.get("Authorization") or "").replace("Bearer ", "")
    apikey = req.headers.get("apikey")
    if apikey != args.key: return None, None
    if auth.startswith("fake."): return "authenticated", auth[5:]
    return "anon", None

def pg_error(e, role):
    code = e.pgcode or "P0001"; msg = (e.diag.message_primary or str(e)).strip()
    status = (401 if role == "anon" else 403) if code == "42501" else 400
    if code == "42883": status = 404
    return web.json_response({"code": code, "message": msg, "details": e.diag.message_detail, "hint": e.diag.message_hint}, status=status)

FN_CACHE = {}
def fn_shape(fn):
    if fn not in FN_CACHE:
        rows = q("""select p.proretset, t.typname, p.proargnames from pg_proc p join pg_type t on t.oid = p.prorettype
                    join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname = %s""", (fn,))
        FN_CACHE[fn] = rows[0] if rows else None
    return FN_CACHE[fn]

async def rpc(req):
    fn = req.match_info["fn"]
    role, sub = role_of(req)
    if role is None: return web.json_response({"message": "No API key found in request"}, status=401)
    body = await req.json() if req.can_read_body else {}
    shape = fn_shape(fn)
    if not shape: return web.json_response({"code": "PGRST202", "message": "Could not find the function public.%s in the schema cache" % fn}, status=404)
    retset, typname, argnames = shape
    argnames = [a for a in (argnames or []) if a]
    # PostgREST matches by the exact set of named parameters (defaults may be omitted)
    unknown = [k for k in body if k not in argnames]
    if unknown: return web.json_response({"code": "PGRST202", "message": "Could not find the function public.%s(%s) in the schema cache" % (fn, ", ".join(sorted(body)))}, status=404)
    names = [k for k in argnames if k in body]
    params = []
    for k in names:
        v = body[k]
        params.append(psycopg2.extras.Json(v) if isinstance(v, (dict,)) else (v if not isinstance(v, list) or k == "p_revealed" else psycopg2.extras.Json(v)))
    call = "public.%s(%s)" % (fn, ", ".join("%s := %%s" % k for k in names))
    sql = ("select to_jsonb(r) from %s r" % call) if retset else ("select %s" % call)
    cur = conn.cursor()
    try:
        cur.execute("begin"); cur.execute("set local role " + role)
        cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (sub or "",))
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() if cur.description else []
        cur.execute("commit")
    except psycopg2.Error as e:
        cur.execute("rollback"); cur.close(); return pg_error(e, role)
    cur.close()
    if retset: return web.json_response([r[0] for r in rows])
    val = rows[0][0] if rows else None
    if typname == "void": return web.Response(status=204)
    if typname in ("jsonb", "json"): return web.json_response(val)
    return web.json_response(str(val) if isinstance(val, uuid.UUID) else val)

async def table_get(req):
    table = req.match_info["table"]
    role, sub = role_of(req)
    if role is None: return web.json_response({"message": "No API key found in request"}, status=401)
    if not re.match(r"^[a-z_]+$", table): return web.json_response({"message": "bad table"}, status=400)
    cols = req.query.get("select", "*")
    if not re.match(r"^[a-z_,* ]+$", cols): return web.json_response({"message": "bad select"}, status=400)
    where, params = [], []
    for k, v in req.query.items():
        if k in ("select", "order", "limit"): continue
        m = re.match(r"^(eq|neq)\.(.*)$", v)
        if not m or not re.match(r"^[a-z_]+$", k): return web.json_response({"message": "unsupported filter " + k}, status=400)
        where.append("%s %s %%s" % (k, "=" if m.group(1) == "eq" else "<>")); params.append(m.group(2))
    sql = "select to_jsonb(r) from (select %s from public.%s%s) r" % (cols, table, (" where " + " and ".join(where)) if where else "")
    cur = conn.cursor()
    try:
        cur.execute("begin"); cur.execute("set local role " + role)
        cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (sub or "",))
        cur.execute(sql, tuple(params)); rows = cur.fetchall(); cur.execute("commit")
    except psycopg2.Error as e:
        cur.execute("rollback"); cur.close(); return pg_error(e, role)
    cur.close()
    return web.json_response([r[0] for r in rows])

async def table_write(req):
    role, sub = role_of(req)
    return web.json_response({"code": "42501", "message": "permission denied for table " + req.match_info["table"]}, status=401 if role == "anon" else 403)

async def signup(req):
    if req.headers.get("apikey") != args.key: return web.json_response({"message": "No API key"}, status=401)
    uid = str(uuid.uuid4())
    q("insert into auth.users (id) values (%s)", (uid,))
    return web.json_response({"access_token": "fake." + uid, "token_type": "bearer", "expires_in": 3600, "refresh_token": "r", "user": {"id": uid, "is_anonymous": True}})

# ---------- realtime (Phoenix protocol, broadcast + a basic postgres_changes) ----------
SOCKETS = set()  # (ws, joined topics set, pg_changes filters)
async def realtime(req):
    ws = web.WebSocketResponse(); await ws.prepare(req)
    entry = {"ws": ws, "topics": {}, "changes": []}
    SOCKETS.add(id(entry)); REG[id(entry)] = entry
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT: continue
            m = json.loads(msg.data)
            topic, event, ref, payload = m.get("topic"), m.get("event"), m.get("ref"), m.get("payload") or {}
            if event == "heartbeat":
                await ws.send_json({"topic": "phoenix", "event": "phx_reply", "ref": ref, "payload": {"status": "ok", "response": {}}})
            elif event == "phx_join":
                entry["topics"][topic] = ref
                cfg = payload.get("config", {})
                pcs = cfg.get("postgres_changes") or []
                resp = {"postgres_changes": []}
                for i, pc in enumerate(pcs):
                    pid = 1000 + len(entry["changes"])
                    entry["changes"].append({"id": pid, "topic": topic, "table": pc.get("table"), "filter": pc.get("filter"), "event": pc.get("event", "*"), "access": payload.get("access_token")})
                    resp["postgres_changes"].append(dict(pc, id=pid))
                await ws.send_json({"topic": topic, "event": "phx_reply", "ref": ref, "payload": {"status": "ok", "response": resp}})
                await ws.send_json({"topic": topic, "event": "presence_state", "ref": None, "payload": {}})
            elif event == "phx_leave":
                entry["topics"].pop(topic, None)
                await ws.send_json({"topic": topic, "event": "phx_reply", "ref": ref, "payload": {"status": "ok", "response": {}}})
            elif event == "broadcast":
                pass  # clients may send; the page never does
    finally:
        SOCKETS.discard(id(entry)); REG.pop(id(entry), None)
    return ws
REG = {}

async def pump():
    """Push rows of realtime.messages (the realtime.send stand-in) to subscribers, and table
    changes to postgres_changes listeners (owner-only, by checking RLS as that user)."""
    last = q("select coalesce(max(id), 0) from realtime.messages")[0][0]
    seen = {}
    for t in ("session_answers", "session_text_entries", "session_polls"):
        seen[t] = q("select coalesce(max(extract(epoch from updated_at)), 0) from public.%s" % (t if t != "session_text_entries" else "session_text_entries"))[0][0] if t != "session_text_entries" else 0
    while True:
        await asyncio.sleep(0.25)
        try:
            rows = q("select id, topic, event, payload from realtime.messages where id > %s order by id", (last,))
            for mid, topic, event, payload in rows:
                last = mid
                for e in list(REG.values()):
                    if ("realtime:" + topic) in e["topics"]:
                        await e["ws"].send_json({"topic": "realtime:" + topic, "event": "broadcast", "ref": None, "payload": {"event": event, "payload": payload, "type": "broadcast"}})
            # postgres_changes: notify listeners of any change in the tables they watch (coarse: row counts/updated)
            for e in list(REG.values()):
                for ch in e["changes"]:
                    t = ch["table"]
                    if t not in ("session_answers", "session_text_entries", "session_polls"): continue
                    key = (id(e), ch["id"])
                    stamp = q("select count(*)::text || ':' || coalesce(max(%s)::text, '') from public.%s" % ("updated_at" if t != "session_text_entries" else "created_at", t))[0][0]
                    if seen.get(key) != stamp:
                        if seen.get(key) is not None:
                            await e["ws"].send_json({"topic": ch["topic"], "event": "postgres_changes", "ref": None,
                                "payload": {"ids": [ch["id"]], "data": {"schema": "public", "table": t, "type": "UPDATE", "record": {}, "old_record": {}, "commit_timestamp": "", "errors": None}}})
                        seen[key] = stamp
        except Exception as ex:
            print("pump error", ex)

async def static(req):
    p = SERVE.get(req.path) or SERVE.get(req.path.rstrip("/") or "/")
    if not p: return web.Response(status=404, text="not found")
    ctype = "text/html" if p.suffix == ".html" else "text/javascript" if p.suffix == ".js" else "application/octet-stream"
    return web.Response(body=p.read_bytes(), content_type=ctype)

SERVE = {}
for spec in args.serve:
    path, _, url = spec.partition(":")
    SERVE[url or "/"] = pathlib.Path(path)

@web.middleware
async def cors(req, handler):
    if req.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(req)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    return resp

app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[cors])
app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response(status=204))
app.router.add_post("/rest/v1/rpc/{fn}", rpc)
app.router.add_get("/rest/v1/{table}", table_get)
app.router.add_route("*", "/rest/v1/{table}", table_write)
app.router.add_post("/auth/v1/signup", signup)
app.router.add_get("/realtime/v1/websocket", realtime)
for url in list(SERVE): app.router.add_get(url, static)
async def on_start(app): app["pump"] = asyncio.create_task(pump())
app.on_startup.append(on_start)
print("fake supabase on http://127.0.0.1:%d serving %s" % (args.port, list(SERVE)), flush=True)
web.run_app(app, host="127.0.0.1", port=args.port, print=None)
