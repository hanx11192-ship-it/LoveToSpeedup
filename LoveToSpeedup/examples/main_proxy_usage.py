"""以 main 端口为例：宿主机 与 Docker 网络内 如何调用爱加速代理网关（Python）。

====================================================================
架构回顾（main 端口）
--------------------------------------------------------------------
  爱加速 ajiasu 本地监听    1080
    └─ 容器内转发器监听     PROXY_FORWARD_PORT = 10801
         └─ 容器端口映射    宿主 = 容器 10801（两侧一致）

因此「同一个 main 端口代理」有两套地址：

  · Docker 网络内（同 lovespeedup_default / proxypool_default 的其他容器）
        面板/API : http://lovespeedup:8000
        代理地址 : http://lovespeedup:10801      # 直接用 API 返回的 proxy 字段

  · 宿主机（VPS 本机，或其他直连宿主的进程）
        面板/API : http://127.0.0.1:5702         # 或 http://<VPS公网IP>:5702
        代理地址 : http://127.0.0.1:10801         # 宿主=容器 10801（端口两侧一致）

⚠️ 关键坑：API 返回的 proxy 字段是 PUBLIC_PROXY = "http://lovespeedup:10801"，
   这个域名只在 Docker 网络内能解析。所以：
     - 在 Docker 网络内：可直接用 lease["proxy"]。
     - 在宿主机上：必须改写成 http://127.0.0.1:10801（端口不变，仅把域名换成 127.0.0.1），否则连不上。
====================================================================

用法：
    # 在宿主机运行：
    GW_LOCATION=host  GW_PASSWORD=<登录密码>  python3 main_proxy_usage.py

    # 在 Docker 网络内的容器运行（如已进入 lovespeedup 同网络的其他容器）：
    GW_LOCATION=docker  GW_PASSWORD=<登录密码>  python3 main_proxy_usage.py
"""

import os
import sys

import requests

# ---- 两套地址（按运行位置切换）----
HOST_BASE = "http://127.0.0.1:5702"        # 宿主机访问面板/API
DOCKER_BASE = "http://lovespeedup:8000"     # Docker 网络内访问面板/API
HOST_PROXY = "http://127.0.0.1:10801"       # 宿主机使用 main 代理（宿主=容器 10801）
DOCKER_PROXY = "http://lovespeedup:10801"   # Docker 网络内使用 main 代理

LOCATION = os.environ.get("GW_LOCATION", "host").lower()
PASSWORD = os.environ.get("GW_PASSWORD", "")
REGION = os.environ.get("GW_REGION", "上海")
TTL = int(os.environ.get("GW_TTL", "120"))


def main():
    if not PASSWORD:
        sys.exit("请先设置环境变量 GW_PASSWORD（网关登录密码，即 API Key）")
    if LOCATION not in ("host", "docker"):
        sys.exit("GW_LOCATION 只能填 host 或 docker")

    BASE = HOST_BASE if LOCATION == "host" else DOCKER_BASE
    print(f"[位置] {LOCATION}  |  BASE={BASE}")

    # 0. 健康检查（免认证）
    ping = requests.get(BASE + "/api/ping", timeout=5)
    print("ping:", ping.json())

    api = requests.Session()
    api.headers["X-API-Key"] = PASSWORD

    # 1. 按需获取 main 端口连接
    #    main 是「按需」语义，必须 acquire 之后才有代理（B 方案后不再自动常驻）。
    r = api.post(
        BASE + "/api/acquire",
        json={"account_id": None, "region": REGION, "ttl": TTL, "force": True},
        timeout=120,
    )
    lease = r.json()
    print("acquire:", lease)
    if not lease.get("ok"):
        raise SystemExit("获取 main 端口失败: " + str(lease))

    # 2. 选择真正可用的代理地址
    #    API 返回的 proxy 是容器内部地址，在宿主机上要替换成宿主映射端口。
    if LOCATION == "docker":
        proxy = lease["proxy"]          # http://lovespeedup:10801（网络内可直接用）
    else:
        proxy = HOST_PROXY              # 宿主机改写成映射端口 10802
    print("使用代理:", proxy)
    assert proxy == (DOCKER_PROXY if LOCATION == "docker" else HOST_PROXY)

    proxies = {"http": proxy, "https": proxy}
    try:
        # 3. 通过代理发请求，验证出口 IP
        resp = requests.get("http://ip.3322.net", proxies=proxies, timeout=20)
        print("出口 IP 验证:", resp.text.strip())

        # HTTPS 同样走 CONNECT 隧道
        resp2 = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=30)
        print("HTTPS 出口:", resp2.json())
    finally:
        # 4. 用完释放（也可等 ttl 到期自动断开）
        print("release:", api.post(BASE + "/api/release", timeout=10).json())


if __name__ == "__main__":
    main()
