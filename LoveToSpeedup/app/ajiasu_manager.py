import json
import os
import random
import re
import socket
import subprocess
import threading
import time

NODE_LINE_RE = re.compile(r"^\s*(\S+)\s+(\S+)\s+(.+?)\s*$")


def ensure_dir(d):
    os.makedirs(d, exist_ok=True)
    return d


class AccountStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.accounts = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.accounts = {str(a["id"]): a for a in data}
            except Exception:
                self.accounts = {}

    def save(self):
        ensure_dir(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(list(self.accounts.values()), f, ensure_ascii=False, indent=2)

    def add(self, user, password):
        with self.lock:
            for acc in self.accounts.values():
                if acc["user"] == user:
                    acc["password"] = password
                    self.save()
                    return acc
            aid = str(int(time.time() * 1000))
            acc = {"id": aid, "user": user, "password": password}
            self.accounts[aid] = acc
            self.save()
            return acc

    def remove(self, aid):
        with self.lock:
            acc = self.accounts.pop(aid, None)
            if acc:
                self.save()
            return acc

    def get(self, aid):
        return self.accounts.get(aid)

    def all(self):
        return list(self.accounts.values())


class AjiasuManager:
    """管理一个 ajiasu 客户端实例（对应一个端口槽 slot）。

    关键事实（来自对 ajiasu 4.2.3.0 二进制的实测）：
    - conf 没有 port 字段；多个实例会自动从基准端口（默认 1080）顺延。
    - 通过独立的 ``cache_dir`` + ``--sys-env-id`` 让多个实例互不干扰并行存活。
    - 实例实际监听的本地端口需在运行时探测（见 ``_discover_port``）。
    """

    def __init__(self, ajiasu_path, data_dir, slot_id="main", sys_env_id=None, base_local_port=1080):
        self.ajiasu_path = ajiasu_path
        self.slot_id = slot_id
        self.sys_env_id = sys_env_id or slot_id
        self.base_local_port = base_local_port
        self.conf_dir = ensure_dir(os.path.join(data_dir, "confs"))
        self.cache_dir = ensure_dir(os.path.join(data_dir, "slots", slot_id, "cache"))
        self.lock = threading.RLock()
        self.proc = None
        self.reader_thread = None
        self.output_buf = []
        self.connected = {"account_id": None, "node_id": None, "node_name": None}
        self.lease = None
        self.exit_ip = None
        self.local_port = None
        self._node_cache = {}

    # ---- conf / 命令构造 ----

    def conf_file(self, account_id):
        return os.path.join(self.conf_dir, "account_%s.conf" % account_id)

    def _write_conf(self, account):
        conf = (
            "user %s\n"
            "pass %s\n"
            "protocol proxy\n"
            "cache_dir %s\n"
            % (account["user"], account["password"], self.cache_dir)
        )
        with open(self.conf_file(account["id"]), "w") as f:
            f.write(conf)

    def _cmd(self, account, *args):
        self._write_conf(account)
        cmd = [self.ajiasu_path, "-c", self.conf_file(account["id"])]
        if self.sys_env_id:
            cmd += ["--sys-env-id", str(self.sys_env_id)]
        return cmd + list(args)

    def _ajiasu(self, account, *args):
        try:
            p = subprocess.run(self._cmd(account, *args), capture_output=True, text=True, timeout=60)
            return p.stdout or ""
        except Exception:
            return ""

    # ---- 一次性查询（login / list），可安全复用 ----

    def login_check(self, account):
        out = self._ajiasu(account, "login")
        ok = "Login Result" in out and "OK" in out
        membership = ""
        exp = ""
        for line in out.splitlines():
            if line.strip().startswith("Membership:"):
                membership = line.split(":", 1)[1].strip()
            if line.strip().startswith("Expiration:"):
                exp = line.split(":", 1)[1].strip()
        return {"ok": ok, "membership": membership, "expiration": exp}

    def list_nodes(self, account, refresh=False):
        if not refresh and account["id"] in self._node_cache:
            return self._node_cache[account["id"]]
        # list/connect 必须先 login 建立会话（会话缓存在本实例 cache_dir）
        self._ajiasu(account, "login")
        out = self._ajiasu(account, "list")
        nodes = []
        for line in out.splitlines():
            m = NODE_LINE_RE.match(line)
            if not m or not m.group(1).startswith("vvn-"):
                continue
            nodes.append({"id": m.group(1), "status": m.group(2), "name": m.group(3)})
        self._node_cache[account["id"]] = nodes
        return nodes

    def all_candidate_nodes(self, store, region=None, account_id=None, statuses=("ok",), lister=None):
        lister = lister or self
        merged = []
        for acc in store.all():
            if account_id and acc["id"] != account_id:
                continue
            for n in lister.list_nodes(acc):
                if n["status"] not in statuses:
                    continue
                if region and region not in n["name"]:
                    continue
                merged.append({"account_id": acc["id"], "account_user": acc["user"], **n})
        return merged

    # ---- 连接生命周期 ----

    def _start_connect(self, account, node):
        self._write_conf(account)
        self.output_buf = []
        self.connected = {
            "account_id": account["id"],
            "node_id": node["id"],
            "node_name": node["name"],
        }
        cmd = self._cmd(account, "connect", node["id"])
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        def reader():
            for line in self.proc.stdout:
                self.output_buf.append(line.rstrip())

        self.reader_thread = threading.Thread(target=reader, daemon=True)
        self.reader_thread.start()

    @staticmethod
    def _port_open(port, host="127.0.0.1", timeout=2):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    @staticmethod
    def _listening_ports():
        """返回本机 127.0.0.1 / ::1 上处于 LISTEN 状态的端口集合。"""
        ports = set()
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(path) as f:
                    next(f, None)
                    for line in f:
                        parts = line.split()
                        if len(parts) < 4:
                            continue
                        local, st = parts[1], parts[3]
                        if st != "0A":  # 0A = LISTEN
                            continue
                        ip, port = local.split(":")
                        if ip not in ("0100007F", "00000000000000000000000000000001"):
                            continue
                        ports.add(int(port, 16))
            except Exception:
                pass
        return ports

    def _discover_port(self, timeout=40):
        """等待本实例绑定出一个新的本地端口并返回；超时或进程退出返回 None。"""
        before = self._listening_ports()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                return None
            after = self._listening_ports()
            for p in sorted(after - before):
                if self._port_open(p):
                    return p
            time.sleep(0.3)
        return None

    def _probe_exit_ip(self, port):
        import re as _re
        import requests

        proxy = "http://127.0.0.1:%d" % port
        # 多个探测地址：优先国内可达的，最后兜底为 unknown（VPS 连不上海外时不阻断连接）
        urls = [
            "http://ip.3322.net",
            "https://ip.cn",
            "http://myip.ipip.net",
            "http://ip.sb",
        ]
        ip_re = _re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for url in urls:
            try:
                r = requests.get(url, proxies={"http": proxy, "https": proxy}, timeout=12)
                m = ip_re.search(r.text or "")
                if m:
                    return m.group(0)
            except Exception:
                continue
        return "unknown"

    def _renew_lease(self, ttl_sec, region=None):
        ttl = int(ttl_sec) if ttl_sec else None
        old = self.lease or {}
        self.lease = {
            "id": old.get("id") or str(int(time.time() * 1000)),
            "region": region or old.get("region"),
            "ttl_sec": ttl,
            "expire_at": time.time() + ttl if ttl else None,
        }

    def acquire(self, store, region=None, ttl_sec=60, account_id=None, force=False, max_tries=8, lister=None):
        """建立连接并探测本地端口；返回含 ``local_port`` 的结果。"""
        with self.lock:
            if self.proc and self.proc.poll() is None:
                node_name = self.connected.get("node_name") or ""
                same_region = (not region) or (region in node_name)
                if same_region:
                    self._renew_lease(ttl_sec, region=region)
                    return self._lease_payload(reused=True)
                if not force:
                    return {"ok": False, "error": "已有活动连接，需先 disconnect 或用 force=1 强制切换"}
                self.disconnect()

            candidates = self.all_candidate_nodes(store, region=region, account_id=account_id, lister=lister)
            if not candidates:
                return {"ok": False, "error": "没有找到地区「%s」的可用节点" % (region or "任意")}

            random.shuffle(candidates)
            last_err = ""
            logged_in = set()
            for cand in candidates[:max_tries]:
                account = store.get(cand["account_id"])
                # 确保本槽 cache_dir 中存在该账户的登录会话（connect 需要先登录）
                if account["id"] not in logged_in:
                    self._ajiasu(account, "login")
                    logged_in.add(account["id"])
                self._start_connect(account, cand)
                port = self._discover_port()
                if not port:
                    last_err = "等待代理端口就绪超时:\n" + "\n".join(self.output_buf[-8:])
                    self.disconnect()
                    continue
                ip = self._probe_exit_ip(port)
                if not ip:
                    last_err = "代理端口探测失败（出口 IP 获取失败）"
                    self.disconnect()
                    continue
                self.local_port = port
                self.exit_ip = ip
                self.lease = {
                    "id": str(int(time.time() * 1000)),
                    "region": region,
                    "ttl_sec": int(ttl_sec) if ttl_sec else None,
                    "expire_at": time.time() + int(ttl_sec) if ttl_sec else None,
                }
                return {
                    "ok": True,
                    "lease_id": self.lease["id"],
                    "proxy": "http://127.0.0.1:%d" % port,
                    "ip": ip,
                    "node": cand["name"],
                    "account": account["user"],
                    "expire_in": ttl_sec,
                    "local_port": port,
                }
            return {"ok": False, "error": "尝试节点均失败: " + last_err}

    def _lease_payload(self, reused=False):
        return {
            "ok": True,
            "reused": reused,
            "lease_id": self.lease["id"] if self.lease else None,
            "proxy": "http://127.0.0.1:%d" % self.local_port if self.local_port else None,
            "ip": self.exit_ip,
            "node": self.connected.get("node_name"),
            "account": None,
            "expire_in": self.lease.get("ttl_sec") if self.lease else None,
            "local_port": self.local_port,
        }

    def disconnect(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=8)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            self.proc = None
            self.connected = {"account_id": None, "node_id": None, "node_name": None}
            self.lease = None
            self.exit_ip = None
            self.local_port = None
            return True
        return False

    def status(self):
        with self.lock:
            connected = bool(self.proc and self.proc.poll() is None)
            remain = None
            if connected and self.lease and self.lease.get("expire_at"):
                remain = max(0, self.lease["expire_at"] - time.time())
            return {
                "connected": connected,
                "node": self.connected.get("node_name") if connected else None,
                "exit_ip": self.exit_ip,
                "lease_id": self.lease["id"] if connected and self.lease else None,
                "lease_remaining": round(remain, 1) if remain is not None else None,
                "local_port": self.local_port,
            }

    def watchdog(self):
        while True:
            time.sleep(1)
            with self.lock:
                if self.proc and self.proc.poll() is None and self.lease and self.lease.get("expire_at"):
                    if time.time() >= self.lease["expire_at"]:
                        self.disconnect()
