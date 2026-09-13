"""爱加速代理网关调用示例。

流程：健康检查 -> 获取指定地区代理 -> 通过代理发请求 -> 释放连接。

用法：
    export GW_BASE_URL=http://<网关地址>:5702
    export GW_PASSWORD=<网关登录密码>
    python3 acquire_demo.py
"""

import os
import sys

import requests

BASE_URL = os.environ.get("GW_BASE_URL", "http://127.0.0.1:5702")
PASSWORD = os.environ.get("GW_PASSWORD", "")
REGION = os.environ.get("GW_REGION", "上海")
TTL = int(os.environ.get("GW_TTL", "120"))


def main():
    if not PASSWORD:
        sys.exit("请先设置环境变量 GW_PASSWORD（网关登录密码，即 API Key）")

    # 1. 健康检查（免认证）
    ping = requests.get(BASE_URL + "/api/ping", timeout=5)
    print("ping:", ping.json())

    api = requests.Session()
    api.headers["X-API-Key"] = PASSWORD

    # 2. 获取指定地区的临时代理
    r = api.post(
        BASE_URL + "/api/acquire",
        json={"region": REGION, "ttl": TTL},
        timeout=120,
    )
    r.raise_for_status()
    lease = r.json()
    print("lease:", lease)

    # proxy 字段来自网关的 PUBLIC_PROXY 配置；跨主机使用时
    # 可自行替换为 http://<网关公网IP>:<代理端口>
    proxy = lease["proxy"]
    proxies = {"http": proxy, "https": proxy}

    try:
        # 3. 通过代理访问目标站点，出口 IP 应为所选地区
        resp = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=30)
        print("出口 IP:", resp.json())
    finally:
        # 4. 用完释放（也可等租约到期自动断开）
        print("release:", api.post(BASE_URL + "/api/release", timeout=10).json())


if __name__ == "__main__":
    main()
