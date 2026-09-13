import os
import threading
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

from . import auth
from .ajiasu_manager import AccountStore
from .slots import SlotManager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"))
DATA_DIR = os.environ.get("GATEWAY_DATA_DIR", os.path.join(BASE_DIR, "data"))
AJIASU_PATH = os.environ.get("AJIASU_PATH", "/usr/local/bin/ajiasu")
LOCAL_PORT = int(os.environ.get("AJIASU_LOCAL_PORT", "1080"))
FORWARD_PORT = int(os.environ.get("PROXY_FORWARD_PORT", "10801"))
PUBLIC_PROXY = os.environ.get("PUBLIC_PROXY", "http://127.0.0.1:%d" % FORWARD_PORT)
DEFAULT_TTL = int(os.environ.get("GATEWAY_DEFAULT_TTL", "60"))
EXTRACT_TTL = int(os.environ.get("GATEWAY_EXTRACT_TTL", "60"))

ADMIN_PASSWORD, SECRET_KEY, PASSWORD_GENERATED, PASSWORD_SOURCE, AUTH_FILE = auth.load_auth_config(
    DATA_DIR, os.environ.get("ADMIN_PASSWORD")
)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=1 * 1024 * 1024)
limiter = auth.LoginRateLimiter(max_failures=5, window_sec=300)

# 无需登录即可访问的路径（存活探针）
PUBLIC_PATHS = {"/login", "/api/ping"}

store = AccountStore(os.path.join(DATA_DIR, "accounts.json"))
SLOT_EXTRA_PORT_START = int(os.environ.get("SLOT_EXTRA_PORT_START", "10802"))
SLOT_EXTRA_PORT_COUNT = int(os.environ.get("SLOT_EXTRA_PORT_COUNT", "8"))
slots = SlotManager(
    AJIASU_PATH, DATA_DIR, store, main_forward_port=FORWARD_PORT, base_local_port=LOCAL_PORT,
    extra_port_start=SLOT_EXTRA_PORT_START, extra_port_count=SLOT_EXTRA_PORT_COUNT,
)


def _request_key():
    header_key = request.headers.get("X-API-Key", "")
    query_key = request.args.get("key", "")
    return header_key or query_key


@app.before_request
def require_auth():
    if request.path in PUBLIC_PATHS or request.path.startswith("/static"):
        return None
    if session.get("auth"):
        return None
    if auth.check_password(_request_key(), ADMIN_PASSWORD):
        return None
    if request.path.startswith("/api/") or request.method == "OPTIONS":
        return jsonify({"ok": False, "error": "unauthorized: 请先登录，或携带 X-API-Key / ?key= 参数"}), 401
    return redirect(url_for("login"))


def _fix_text(s):
    if not s:
        return s
    s = str(s).strip()
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        if any("\u4e00" <= c <= "\u9fff" for c in fixed):
            return fixed
    except Exception:
        pass
    return s


def acquire_payload():
    data = request.get_json(silent=True) or {}
    payload = dict(data)
    for k, v in request.args.items():
        payload.setdefault(k, v)
    return payload


def _extract_region(p):
    for key in ("region", "city", "region_city", "province", "region_province"):
        val = _fix_text(p.get(key) or "")
        if val:
            return val
    return None


def _proxy_host():
    return urlparse(PUBLIC_PROXY).hostname or "127.0.0.1"


def slot_proxy(forward_port):
    return "http://%s:%d" % (_proxy_host(), forward_port)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        client_ip = request.remote_addr or "?"
        if limiter.blocked(client_ip):
            return jsonify({"ok": False, "error": "尝试次数过多，请 5 分钟后再试"}), 429
        given = (request.form.get("password") or (request.get_json(silent=True) or {}).get("password") or "").strip()
        if auth.check_password(given, ADMIN_PASSWORD):
            limiter.clear(client_ip)
            session["auth"] = True
            return redirect(url_for("index"))
        limiter.record_failure(client_ip)
        error = "密码错误"
        return render_template("login.html", error=error), 401
    if session.get("auth"):
        return redirect(url_for("index"))
    return render_template("login.html", error=error)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True})


@app.route("/api/auth_info", methods=["GET"])
def auth_info():
    return jsonify({
        "ok": True,
        "password": ADMIN_PASSWORD,
        "source": PASSWORD_SOURCE,
        "generated": PASSWORD_GENERATED,
    })


@app.route("/api/change_password", methods=["POST"])
def change_password():
    global ADMIN_PASSWORD
    data = request.get_json(force=True, silent=True) or {}
    old = (data.get("old_password") or "").strip()
    new = (data.get("new_password") or "").strip()
    if not new:
        return jsonify({"ok": False, "error": "新密码不能为空"}), 400
    if not auth.check_password(old, ADMIN_PASSWORD):
        return jsonify({"ok": False, "error": "旧密码不正确"}), 403
    auth.save_password(DATA_DIR, new)
    ADMIN_PASSWORD = new
    return jsonify({"ok": True})


# ---- 账户 ----

@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    return jsonify([{"id": a["id"], "user": a["user"]} for a in store.all()])


@app.route("/api/accounts", methods=["POST"])
def add_account():
    data = request.get_json(force=True)
    user = (data.get("user") or "").strip()
    password = data.get("password") or ""
    if not user or not password:
        return jsonify({"ok": False, "error": "用户名和密码必填"}), 400
    acc = store.add(user, password)
    check = slots.meta.login_check(acc)
    return jsonify({"ok": True, "id": acc["id"], "user": acc["user"], "check": check})


@app.route("/api/accounts/<aid>", methods=["DELETE"])
def delete_account(aid):
    acc = store.remove(aid)
    if acc:
        # 断掉正在使用该账户的所有槽（避免残留进程占用该账户）
        for s in slots.slots.values():
            if s.manager.connected.get("account_id") == aid:
                s.manager.disconnect()
    return jsonify({"ok": bool(acc)})


@app.route("/api/accounts/<aid>/nodes", methods=["GET"])
def account_nodes(aid):
    acc = store.get(aid)
    if not acc:
        return jsonify({"ok": False, "error": "账户不存在"}), 404
    nodes = slots.meta.list_nodes(acc, refresh=request.args.get("refresh") == "1")
    return jsonify({"ok": True, "nodes": nodes})


@app.route("/api/regions", methods=["GET"])
def regions():
    seen = {}
    for acc in store.all():
        for n in slots.meta.list_nodes(acc):
            if n["status"] == "ok":
                seen.setdefault(n["name"], True)
    names = sorted(seen.keys())
    return jsonify({"ok": True, "regions": names})


@app.route("/api/nodes", methods=["GET"])
def nodes():
    region = request.args.get("region") or None
    account_id = request.args.get("account_id") or None
    nodes = slots.meta.all_candidate_nodes(store, region=region, account_id=account_id)
    return jsonify({"ok": True, "count": len(nodes), "nodes": nodes[:300]})


# ---- 主槽（原版行为：多账户自动/手动切换 + ttl 短期）----

@app.route("/api/acquire", methods=["POST"])
def acquire():
    p = acquire_payload()
    # 主端口改为原版「按需」语义：不预绑账户；未传账户时自动选一个当前未被其它槽占用的账户。
    account_id = p.get("account_id") or None
    if not account_id:
        account_id = slots.pick_free_account(exclude_slot_id="main")
    region = p.get("region") or None
    try:
        ttl = int(p.get("ttl") or DEFAULT_TTL)
    except ValueError:
        ttl = DEFAULT_TTL
    force = str(p.get("force", "")).lower() in ("1", "true", "yes")
    res = slots.connect("main", region=region, ttl_sec=ttl, account_id=account_id, force=force)
    if res.get("ok"):
        res["proxy"] = PUBLIC_PROXY
        res["forward_port"] = FORWARD_PORT
    return jsonify(res), (200 if res.get("ok") else 409)


def _extract_host_port():
    parsed = urlparse(PUBLIC_PROXY)
    host = parsed.hostname or request.host.split(":")[0] or "ajiasu-gateway"
    port = parsed.port or FORWARD_PORT
    return host, port


@app.route("/api/extract", methods=["GET", "POST"])
def extract():
    p = acquire_payload()
    region = _extract_region(p)
    try:
        ttl = int(p.get("ttl") or EXTRACT_TTL)
    except ValueError:
        ttl = EXTRACT_TTL
    if ttl <= 0:
        ttl = EXTRACT_TTL
    account_id = p.get("account_id") or None
    if not account_id:
        account_id = slots.pick_free_account(exclude_slot_id="main")
    res = slots.connect("main", region=region, ttl_sec=ttl, account_id=account_id, force=True)
    if not res.get("ok"):
        return jsonify(res), 409
    host, port = _extract_host_port()
    addr = "%s:%s" % (host, port)
    fmt = (p.get("format") or request.args.get("format") or "").lower()
    if fmt == "json":
        return jsonify({
            "ok": True,
            "ip": host,
            "port": port,
            "proxy": addr,
            "node": res.get("node"),
            "exit_ip": res.get("ip"),
            "expire_in": res.get("expire_in"),
            "reused": res.get("reused", False),
        })
    return Response(addr + "\n", mimetype="text/plain")


@app.route("/api/release", methods=["POST"])
def release():
    slots.disconnect_slot("main")
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def status():
    main = slots.slots["main"]
    st = main.manager.status()
    return jsonify({"ok": True, "forward_port": FORWARD_PORT, "public_proxy": PUBLIC_PROXY, **st})


# ---- 端口槽管理 ----

@app.route("/api/slots", methods=["GET"])
def get_slots():
    out = []
    for s in slots.list_slots():
        s = dict(s)
        s["proxy"] = slot_proxy(s["forward_port"])
        out.append(s)
    return jsonify({"ok": True, "slots": out})


@app.route("/api/slots", methods=["POST"])
def create_slot():
    data = request.get_json(force=True, silent=True) or {}
    account_id = data.get("account_id") or None
    region = data.get("region") or None
    mode = data.get("mode") or "short"
    try:
        ttl = int(data.get("ttl") or DEFAULT_TTL)
    except ValueError:
        ttl = DEFAULT_TTL
    if account_id and not store.get(account_id):
        return jsonify({"ok": False, "error": "账户不存在"}), 404
    res = slots.create_slot(account_id=account_id, region=region, mode=mode, ttl_sec=ttl)
    if res.get("ok"):
        res["proxy"] = slot_proxy(res["forward_port"])
    return jsonify(res), (200 if res.get("ok") else 409)


@app.route("/api/slots/<sid>/connect", methods=["POST"])
def slot_connect(sid):
    data = acquire_payload()
    region = data.get("region") or None
    account_id = data.get("account_id") or None
    ttl = data.get("ttl")
    try:
        ttl = int(ttl) if ttl not in (None, "") else None
    except ValueError:
        ttl = None
    res = slots.connect(sid, region=region, ttl_sec=ttl, account_id=account_id, force=True)
    if res.get("ok"):
        res["proxy"] = slot_proxy(slots.slots[sid].forward_port)
    return jsonify(res), (200 if res.get("ok") else 409)


@app.route("/api/slots/<sid>/disconnect", methods=["POST"])
def slot_disconnect(sid):
    return jsonify(slots.disconnect_slot(sid))


@app.route("/api/slots/<sid>", methods=["DELETE"])
def slot_delete(sid):
    return jsonify(slots.destroy_slot(sid))


@app.route("/api/slots/<sid>/proxy", methods=["GET"])
def slot_proxy_info(sid):
    slot = slots.slots.get(sid)
    if not slot:
        return jsonify({"ok": False, "error": "槽不存在"}), 404
    return jsonify({"ok": True, "slot_id": sid, "forward_port": slot.forward_port, "proxy": slot_proxy(slot.forward_port)})


def start_services():
    if PASSWORD_GENERATED:
        print("[auth] 未设置 ADMIN_PASSWORD，已自动生成登录密码: %s" % ADMIN_PASSWORD, flush=True)
        print("[auth] 密码保存在 %s，也可通过环境变量 ADMIN_PASSWORD 覆盖" % AUTH_FILE, flush=True)
    else:
        print("[auth] 登录密码已启用（来源: %s）" % PASSWORD_SOURCE, flush=True)
    slots.start()


if __name__ == "__main__":
    start_services()
    app.run(host="0.0.0.0", port=int(os.environ.get("GATEWAY_PORT", "8000")), debug=False)
