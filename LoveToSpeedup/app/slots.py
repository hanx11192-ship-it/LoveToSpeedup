import json
import os
import threading
import time

from .ajiasu_manager import AjiasuManager
from .forwarder import TcpForwarder


class Slot:
    """一个端口槽：= 一个独立的 ajiasu 实例 + 一个独立的 TCP 转发器。

    - ``forward_port`` 固定不变（对外/宿主机/docker 网络可达），对应原版的 10801。
    - ``local_port`` 是 ajiasu 运行时实际监听的本地端口（自动探测得到）。
    - ``mode`` 为 ``short``（带 ttl 的临时连接）或 ``long``（常驻，掉线自动重连）。
    """

    def __init__(self, slot_id, forward_port, manager, forwarder, mode="short",
                 account_id=None, region=None, ttl_sec=None, persistent=False):
        self.slot_id = slot_id
        self.forward_port = forward_port
        self.manager = manager
        self.forwarder = forwarder
        self.mode = mode
        self.account_id = account_id
        self.region = region
        self.ttl_sec = ttl_sec
        self.persistent = persistent
        self.local_port = None
        self.reconnects = 0


class SlotManager:
    def __init__(self, ajiasu_path, data_dir, store, main_forward_port=10801,
                 base_local_port=1080, extra_port_start=10802, extra_port_count=18):
        self.ajiasu_path = ajiasu_path
        self.data_dir = data_dir
        self.store = store
        self.main_forward_port = main_forward_port
        self.base_local_port = base_local_port
        self.extra_ports = list(range(extra_port_start, extra_port_start + extra_port_count))
        self.slots = {}
        self.lock = threading.RLock()
        self.persist_path = os.path.join(data_dir, "slots.json")
        # 用于 login/list 等只读操作（一次性子进程，不长期占用端口）
        self.meta = AjiasuManager(ajiasu_path, data_dir, slot_id="meta", sys_env_id="meta")
        self._ensure_main()
        self._load_persistent()
        self.ensure_initial_slots()

    # ---- 槽构造 ----

    def _make_manager(self, slot_id):
        return AjiasuManager(
            self.ajiasu_path, self.data_dir, slot_id=slot_id,
            sys_env_id=slot_id, base_local_port=self.base_local_port,
        )

    def _ensure_main(self):
        if "main" in self.slots:
            return
        mgr = self._make_manager("main")
        fwd = TcpForwarder(self.main_forward_port, "127.0.0.1", self.base_local_port)
        # 主槽改回原版「按需」语义：默认 short，不预绑账户、不自动常驻。
        self.slots["main"] = Slot("main", self.main_forward_port, mgr, fwd,
                                  mode="short", account_id=None, region=None, ttl_sec=None)

    def _make_slot(self, slot_id, forward_port, mode, account_id=None, region=None,
                   ttl_sec=None, persistent=False):
        mgr = self._make_manager(slot_id)
        fwd = TcpForwarder(forward_port, "127.0.0.1", self.base_local_port)
        # 未连接前不指向任何后端端口（避免误转发到 1080 主槽）
        fwd.target_port = None
        slot = Slot(slot_id, forward_port, mgr, fwd, mode=mode,
                    account_id=account_id, region=region, ttl_sec=ttl_sec, persistent=persistent)
        self.slots[slot_id] = slot
        if not fwd.running:
            fwd.start()
        return slot

    def ensure_initial_slots(self, count=2):
        """首次启动且无任何额外槽时，预置 count 个未连接的固定槽（监听但不连，等用户在面板绑定账户/地区）。"""
        extra = [s for s in self.slots.values() if s.slot_id != "main"]
        if extra:
            return
        created = []
        for _ in range(count):
            with self.lock:
                used = {s.forward_port for s in self.slots.values()}
                free = [p for p in self.extra_ports if p not in used]
                if not free:
                    break
                fp = free[0]
                sid = "slot-%d" % fp
                self._make_slot(sid, fp, "short", account_id=None, region=None, ttl_sec=None, persistent=False)
            created.append(sid)
        if created:
            self._save()

    # ---- 持久化 ----

    def _save(self):
        data = [
            {
                "slot_id": s.slot_id, "forward_port": s.forward_port, "mode": s.mode,
                "account_id": s.account_id, "region": s.region, "ttl_sec": s.ttl_sec,
                "persistent": s.persistent, "is_main": s.slot_id == "main",
            }
            for s in self.slots.values()
        ]
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self.persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.persist_path)
        except Exception:
            pass

    def _load_persistent(self):
        if not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            return
        if isinstance(items, dict):
            items = items.get("slots", [])
        for it in items:
            sid = it.get("slot_id")
            if not sid:
                continue
            if sid == "main":
                # 主槽改为按需：启动时不预绑账户/地区，避免自动常驻。
                continue
            if sid in self.slots:
                continue
            try:
                self._make_slot(
                    sid, int(it["forward_port"]), it.get("mode", "short"),
                    account_id=it.get("account_id"), region=it.get("region"),
                    ttl_sec=it.get("ttl_sec"), persistent=bool(it.get("persistent")),
                )
            except Exception:
                pass

    # ---- 连接控制 ----

    def connect(self, slot_id, region=None, ttl_sec=None, account_id=None, force=False):
        slot = self.slots.get(slot_id)
        if not slot:
            return {"ok": False, "error": "槽不存在"}
        eff_account = account_id if account_id is not None else slot.account_id
        eff_region = region if region is not None else slot.region
        if not eff_account:
            return {"ok": False, "error": "请先为该槽选择账户（account_id）"}
        if not self.store.get(eff_account):
            return {"ok": False, "error": "账户不存在: %s" % eff_account}
        # 账户排他性：爱加速同一账户同一时刻只能有一个活动代理，避免 main / 额外槽互相抢账户。
        # 整个连接过程持有 SlotManager 锁，确保两个连接不会并发落到同一账户上。
        with self.lock:
            owner = self._account_owner(eff_account, except_slot_id=slot_id)
            if owner:
                return {"ok": False, "error": "账户 %s 正被槽 %s 占用，请先断开该槽再切换" % (eff_account, owner)}
            slot.account_id = eff_account
            slot.region = eff_region
            self._save()
            res = slot.manager.acquire(
                self.store,
                region=eff_region,
                ttl_sec=ttl_sec if ttl_sec is not None else slot.ttl_sec,
                account_id=eff_account,
                force=force,
                lister=self.meta,
            )
        if res.get("ok"):
            slot.local_port = res.get("local_port")
            slot.forwarder.target_port = res.get("local_port")
        return res

    def _account_owner(self, account_id, except_slot_id=None):
        """返回当前正在使用 account_id 的其它槽（已连接且账户匹配），无则返回 None。"""
        if not account_id:
            return None
        for s in self.slots.values():
            if s.slot_id == except_slot_id:
                continue
            st = s.manager.status()
            if st.get("connected") and s.manager.connected.get("account_id") == account_id:
                return s.slot_id
        return None

    def disconnect_slot(self, slot_id):
        slot = self.slots.get(slot_id)
        if not slot:
            return {"ok": False, "error": "槽不存在"}
        slot.manager.disconnect()
        slot.local_port = None
        slot.forwarder.target_port = None
        return {"ok": True}

    def create_slot(self, account_id=None, region=None, mode="short", ttl_sec=60, auto_connect=True):
        if mode not in ("short", "long"):
            return {"ok": False, "error": "mode 只能是 short 或 long"}
        with self.lock:
            used = {s.forward_port for s in self.slots.values()}
            free = [p for p in self.extra_ports if p not in used]
            if not free:
                return {"ok": False, "error": "没有可用额外端口（已用完 %d 个）" % len(self.extra_ports)}
            fp = free[0]
            sid = "slot-%d" % fp
            persistent = (mode == "long")
            try:
                self._make_slot(sid, fp, mode, account_id=account_id, region=region,
                                ttl_sec=ttl_sec, persistent=persistent)
            except OSError as ex:
                return {"ok": False, "error": "端口 %d 绑定失败: %s" % (fp, ex)}
            self._save()
        if not auto_connect:
            # 仅建槽监听，不连接（用于预置固定槽）
            return {"ok": True, "slot_id": sid, "forward_port": fp, "persistent": persistent,
                    "connected": False, "proxy": "http://127.0.0.1:%d" % fp}
        # 槽是常驻端口，创建即连；长期槽由 keeper 维持，短期槽带 ttl 到期自动断开
        res = self.connect(sid, region=region, ttl_sec=None if persistent else ttl_sec, account_id=account_id)
        if not res.get("ok"):
            # 连接失败 → 回滚：移除槽并释放端口，避免留下孤儿转发器导致后续 500
            with self.lock:
                slot = self.slots.pop(sid, None)
                if slot:
                    try:
                        slot.manager.disconnect()
                    except Exception:
                        pass
                    try:
                        slot.forwarder.stop()
                    except Exception:
                        pass
                self._save()
        return {"ok": True, "slot_id": sid, "forward_port": fp, "persistent": persistent, **res}

    def destroy_slot(self, slot_id):
        if slot_id == "main":
            return {"ok": False, "error": "主槽不可删除"}
        with self.lock:
            slot = self.slots.pop(slot_id, None)
            if not slot:
                return {"ok": False, "error": "槽不存在"}
            try:
                slot.manager.disconnect()
            except Exception:
                pass
            try:
                slot.forwarder.stop()
            except Exception:
                pass
            self._save()
        return {"ok": True}

    # ---- 状态 ----

    def slot_status(self, slot_id):
        slot = self.slots.get(slot_id)
        if not slot:
            return None
        st = slot.manager.status()
        return {
            "slot_id": slot_id,
            "forward_port": slot.forward_port,
            "mode": slot.mode,
            "account_id": slot.account_id,
            "region": slot.region,
            "ttl_sec": slot.ttl_sec,
            "persistent": slot.persistent,
            "local_port": slot.local_port,
            "connected": st.get("connected"),
            "node": st.get("node"),
            "exit_ip": st.get("exit_ip"),
            "lease_remaining": st.get("lease_remaining"),
            "reconnects": slot.reconnects,
        }

    def list_slots(self):
        return [self.slot_status(sid) for sid in self.slots]

    # ---- 启动 / 后台维持 ----

    def start(self):
        for slot in self.slots.values():
            if not slot.forwarder.running:
                slot.forwarder.start()
            threading.Thread(target=slot.manager.watchdog, daemon=True).start()
        # 绑定了账户的常驻槽 / 主槽 → 立即连接，使固定端口随容器启动即在线
        for slot in self.slots.values():
            if self._should_stay_up(slot):
                try:
                    self.connect(slot.slot_id, region=slot.region, account_id=slot.account_id, ttl_sec=None)
                except Exception:
                    pass
        threading.Thread(target=self._keeper_loop, daemon=True).start()

    def _should_stay_up(self, slot):
        """仅长期槽（persistent 且已绑定账户）保持在线；主槽改为按需，不参与启动自连/掉线重连。"""
        return bool(slot.account_id) and slot.persistent and slot.slot_id != "main"

    def pick_free_account(self, exclude_slot_id=None):
        """返回一个当前未被其它已连接槽占用的账户 id；无可用则返回 None。用于主槽按需自动选账户。"""
        used = set()
        for s in self.slots.values():
            if s.slot_id == exclude_slot_id:
                continue
            if s.manager.status().get("connected"):
                used.add(s.manager.connected.get("account_id"))
        for acc in self.store.all():
            if acc["id"] not in used:
                return acc["id"]
        return None

    def _keeper_loop(self):
        while True:
            time.sleep(10)
            with self.lock:
                targets = [s for s in self.slots.values() if self._should_stay_up(s)]
            for slot in targets:
                try:
                    st = slot.manager.status()
                    if st.get("connected") and st.get("exit_ip"):
                        continue
                    if slot.manager.lock.acquire(blocking=False):
                        try:
                            r = self.connect(slot.slot_id, region=slot.region,
                                             account_id=slot.account_id, ttl_sec=None)
                            if r.get("ok"):
                                slot.reconnects += 1
                        except Exception:
                            pass
                        finally:
                            slot.manager.lock.release()
                except Exception:
                    pass
