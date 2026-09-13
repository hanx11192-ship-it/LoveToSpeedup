#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同时调用 main 端口与某个额外槽位（示例以 10803 为例）的 Python 示例。

架构地址（务必分清，否则解析不到域名）：
  main 端口 10801  -> Docker 网络内 http://lovespeedup:10801  宿主机 http://127.0.0.1:10801（宿主=容器，一致）
  额外槽   10803  -> Docker 网络内 http://lovespeedup:10803  宿主机 http://127.0.0.1:10803

语义差异：
  - main 是「按需」：调用前必须先 POST /api/acquire 把代理拉起，用完 POST /api/release，
    或等 TTL 自动断开。不 acquire 时空跑会连不上。
  - 额外槽（尤其 long 模式）是「常驻」：连上后一直在线，直接把请求打到它的端口即可，
    无需每次 acquire。short 槽才需要 /api/slots/<id>/connect 并受 TTL 限制。

账户互斥（重要）：
  爱加速同一账户同时只允许一个活动连接。main 与槽位必须用「不同账户」，否则 acquire
  会被拒绝（返回 409）。下面的示例让 main 用 A 账户、槽位用 B 账户。

运行方式：
  # 在宿主机（VPS 本机）上
  GW_LOCATION=host  GW_PASSWORD=<面板密码>  \
  MAIN_ACCOUNT=<账户A的id>  SLOT_PORT=10803  SLOT_ACCOUNT=<账户B的id> \
  python3 main_and_slot_usage.py

  # 在 Docker 网络内的其他容器里（能解析 lovespeedup 域名）
  GW_LOCATION=docker  GW_PASSWORD=<面板密码>  MAIN_ACCOUNT=...  SLOT_PORT=10803  SLOT_ACCOUNT=... \
  python3 main_and_slot_usage.py
"""
import os
import sys
import time
import requests
from urllib.parse import urlparse

# ----------------------------------------------------------------------------
# 配置（来自环境变量，方便在宿主机 / Docker 内切换）
# ----------------------------------------------------------------------------
LOCATION   = os.environ.get("GW_LOCATION", "host").lower()   # host | docker
PASSWORD   = os.environ.get("GW_PASSWORD", "")
MAIN_ACCOUNT = os.environ.get("MAIN_ACCOUNT", "")            # main 用的账户 id（留空=自动挑）
SLOT_PORT    = int(os.environ.get("SLOT_PORT", "10803"))     # 目标额外槽的对外端口
SLOT_ACCOUNT = os.environ.get("SLOT_ACCOUNT", "")            # 槽位用的账户 id（留空=用槽上已有账户）

# 面板 / API 基地址
API_BASE = "http://127.0.0.1:5702" if LOCATION == "host" else "http://lovespeedup:8000"
AUTH = {"X-API-Key": PASSWORD} if PASSWORD else {}


def rewrite(proxy_url: str) -> str:
    """把 API 返回的容器内域名代理地址，按运行位置改写成可达地址。

    API 返回的 proxy 形如 http://lovespeedup:10801 或 http://lovespeedup:10803，
    该域名只在 Docker 网络内能解析；宿主机上必须把主机名换成 127.0.0.1。
    """
    p = urlparse(proxy_url)
    host = "127.0.0.1" if LOCATION == "host" else p.hostname
    return "%s://%s:%s" % (p.scheme, host, p.port)


def exit_ip_via(proxy: str) -> str:
    """通过指定代理访问，取出出口 IP（验证代理确实生效）。"""
    proxies = {"http": proxy, "https": proxy}
    try:
        r = requests.get("http://ip.3322.net", proxies=proxies, timeout=15)
        return r.text.strip()
    except Exception as e:  # noqa
        return "<err:%s>" % e


def main_proxy_usage():
    """main 端口：按需 acquire -> 使用 -> release。"""
    print("\n=== main 端口（按需）===")
    # 1) 拉起 main 代理。force=True 表示若被占用则顶掉重建。
    r = requests.post(
        API_BASE + "/api/acquire",
        json={"account_id": MAIN_ACCOUNT or None, "region": None, "ttl": 120, "force": True},
        headers=AUTH, timeout=60,
    )
    j = r.json()
    if not j.get("ok"):
        print("  [!] acquire 失败:", j.get("error"))
        return None
    proxy = rewrite(j["proxy"])   # 容器内域名 -> 可达地址
    print("  acquire 成功, 节点:", j.get("node"), "本地端口:", j.get("forward_port"))
    print("  main 代理地址:", proxy)
    return proxy


def slot_proxy_usage():
    """额外槽位 10803：常驻直连（必要时先 connect）。"""
    print("\n=== 额外槽位 %d（常驻）===" % SLOT_PORT)
    # 先看槽位列表，找到目标端口对应的槽
    r = requests.get(API_BASE + "/api/slots", headers=AUTH, timeout=30)
    slots = (r.json() or {}).get("slots", [])
    target = next((s for s in slots if s.get("forward_port") == SLOT_PORT), None)

    if not target:
        print("  [!] 未找到端口 %d 的槽，请先在面板创建/连接该槽" % SLOT_PORT)
        return None

    # 若槽未连接，则按需 connect（long 槽通常已在线，可跳过）
    if not target.get("connected"):
        r = requests.post(
            API_BASE + "/api/slots/%s/connect" % target["slot_id"],
            json={"account_id": SLOT_ACCOUNT or None, "region": None},
            headers=AUTH, timeout=60,
        )
        j = r.json()
        if not j.get("ok"):
            print("  [!] slot connect 失败:", j.get("error"))
            return None
        print("  slot connect 成功, 节点:", j.get("node"))
        proxy = rewrite(j["proxy"])
    else:
        print("  slot 已在连接状态（mode=%s），直接用端口地址" % target.get("mode"))
        proxy = rewrite(target["proxy"])

    print("  槽 %s 代理地址: %s" % (target["slot_id"], proxy))
    return proxy


def demo_together(main_proxy: str, slot_proxy_url: str):
    """并行使用两个代理，证明它们确实走不同出口。"""
    print("\n=== 同时调用 main + slot ===")
    ip_main = exit_ip_via(main_proxy)
    ip_slot = exit_ip_via(slot_proxy_url)
    print("  main 出口 IP:", ip_main)
    print("  slot 出口 IP:", ip_slot)
    print("  是否不同出口:", "是" if ip_main != ip_slot else "否（同账户/同节点属正常）")


def main():
    if not PASSWORD:
        print("[!] 请通过环境变量 GW_PASSWORD 传入面板密码")
        sys.exit(1)

    main_proxy = main_proxy_usage()
    slot_proxy_url = slot_proxy_usage()

    if main_proxy and slot_proxy_url:
        demo_together(main_proxy, slot_proxy_url)

    # main 用完释放；槽位（long）保持在线不释放
    if main_proxy:
        requests.post(API_BASE + "/api/release", headers=AUTH, timeout=30)
        print("\n[ok] main 已 release（按需断开），槽位 %d 继续常驻在线" % SLOT_PORT)


if __name__ == "__main__":
    main()
