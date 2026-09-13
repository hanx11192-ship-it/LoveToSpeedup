import hmac
import json
import os
import secrets
import threading
import time


class LoginRateLimiter:
    """按 IP 限制登录失败次数：窗口期内失败超过上限则锁定到窗口结束。"""

    def __init__(self, max_failures=5, window_sec=300):
        self.max_failures = max_failures
        self.window_sec = window_sec
        self._fails = {}  # ip -> [失败时间戳列表]
        self.lock = threading.Lock()

    def _prune(self, now):
        cutoff = now - self.window_sec
        for ip in list(self._fails.keys()):
            kept = [t for t in self._fails[ip] if t > cutoff]
            if kept:
                self._fails[ip] = kept
            else:
                del self._fails[ip]

    def blocked(self, ip):
        with self.lock:
            self._prune(time.time())
            return len(self._fails.get(ip, [])) >= self.max_failures

    def record_failure(self, ip):
        with self.lock:
            now = time.time()
            self._prune(now)
            self._fails.setdefault(ip, []).append(now)
            return len(self._fails[ip])

    def clear(self, ip):
        with self.lock:
            self._fails.pop(ip, None)


def check_password(given, expected):
    return hmac.compare_digest(str(given or ""), str(expected or ""))


def load_auth_config(data_dir, env_password=None):
    """解析网关登录密码和 session 密钥。

    优先级：环境变量 ADMIN_PASSWORD > data 目录下已保存的配置 > 随机生成并保存。
    返回 (password, secret_key, generated)。
    """
    path = os.path.join(data_dir, "gateway_auth.json")
    conf = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception:
        conf = {}

    generated = False
    password = (env_password or "").strip()
    if password:
        source = "env"
    else:
        password = (conf.get("password") or "").strip()
        source = "file"
    if not password:
        password = secrets.token_urlsafe(12)
        source = "generated"
        generated = True

    secret_key = conf.get("secret_key") or secrets.token_hex(32)
    need_save = generated or (not conf.get("secret_key")) or conf.get("password") != password
    if need_save:
        os.makedirs(data_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"password": password, "secret_key": secret_key}, f)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return password, secret_key, generated, source, path


def save_password(data_dir, new_password):
    """写入新的登录密码（保留已有 secret_key），立即生效。返回保存后的密码。"""
    path = os.path.join(data_dir, "gateway_auth.json")
    conf = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception:
        conf = {}
    conf["password"] = new_password
    conf.setdefault("secret_key", secrets.token_hex(32))
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return new_password
