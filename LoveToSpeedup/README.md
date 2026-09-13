# 爱加速代理网关 (ajiasu-gateway)

> **构建声明**：本项目基于 [s1g0day/Aijiasu_Agent_Pool](https://github.com/s1g0day/Aijiasu_Agent_Pool) 的思路构建（该项目提供了爱加速 Linux 客户端的 Python 调用方案），由 [GLM-5.3-flash](https://github.com/zai-org/GLM) 与 DeepSeek-v4-flash 两个 AI 模型协作完成 Docker 化重构、Web 面板、REST API 与密码认证。

> **当前版本：v3.0** —— 全新 Web 面板、端口槽「关于 / API 文档」分页、主端口与额外槽并行调用示例。

一个 Docker 化的 HTTP 代理网关服务：登录你自己的爱加速账户（支持多账户），通过 REST API 或 Web 面板按**地区**获取一个**带有效期**的 HTTP 代理，供其他 Docker 定时任务或脚本使用。代理到期自动断开。

**特性**

- 完全离线构建：`vendor/` 内置 ajiasu 二进制与全部 pip 依赖 wheels，无需访问外网
- 面板与 API 均有登录密码保护，登录失败频控防爆破
- 全新 Web 面板（深色侧边栏 + 卡片布局）：仪表盘、主端口代理、端口槽管理、节点/账户管理、修改密码、关于与 API 文档分页
- API 文档见 [docs/API.md](docs/API.md)；调用示例见 [examples/](examples/)（含主端口与额外端口槽并行调用）

## 架构

```
┌──────────────┐    acquire(region=上海,ttl=60)   ┌──────────────────────────────┐
│ 定时面板/任务  │ ──────────────────────────────► │  ajiasu-gateway (Docker)      │
│ (任意容器)     │                                 │  ├─ Flask Web 面板 (端口 8000) │
│              │  proxy = http://...:10801        │  ├─ 账户管理 (多账户)          │
│   curl -x    │ ◄────────────────────────────── │  ├─ 端口槽管理器 (SlotManager)  │
│  http://...  │  经代理发请求 (http/https 明文)    │  │   ├─ main 槽 → 10801 (原版)  │
│              │                                  │  │   ├─ slot-10802 → 独立账户   │
│  额外端口     │ ──────────────────────────────► │  │   └─ slot-10803 → 长期常驻   │
│  :10802-10809│  各槽独立 ajiasu 实例+转发器      │  └─ TCP 转发 (10801/10802..)   │
└──────────────┘ ───────────────────────────────► │         │                     │
                 转发到各 ajiasu 本地端口(自动探测)  │   ajiasu connect 节点          │
                                                  │   └ 出口 IP = 指定地区          │
                                                  └──────────────────────────────┘
```

设计约束（来自实测）：
- `ajiasu` 客户端**同一时刻只能有一个活动连接**，因此网关采用「单连接 + 按需切换」：请求哪个地区就切换到哪个地区，可通过 `force=1` 抢占或先 `release`。
- 该版本 `ajiasu` 的 SOCKS5 握手不可用，**HTTP 代理模式可用**，故对外只暴露 HTTP 代理。
- `ajiasu` 是静态二进制，随镜像打包，无需在宿主机安装客户端。
- `ajiasu` 的 conf **没有 port 字段**：多实例从 1080 自动顺延端口。网关为每个端口槽启动一个**独立 ajiasu 实例**（独立 `cache_dir` + `--sys-env-id`），并在运行时探测其实际监听的本地端口，再交给该槽的 TCP 转发器转发。

## 多端口 / 多账户并行（新增）

爱加速「单账户同时仅一个连接」，但你可以：
- **主槽（固定端口 10801，原版行为）**：多账户自动/手动切换，按需获取带 ttl 的临时代理，到期自动断开。供宿主机和 docker 网络短期使用。
- **额外端口槽（10802-10809，动态创建）**：每个槽绑定一个指定账户（+ 可选地区），各自独立 ajiasu 实例与转发端口，因此**多个账户可同时在线**。
  - `mode=short`（短期）：带 ttl，到期自动断开，可随时重连。适合短期任务。
  - `mode=long`（长期）：常驻并保持**掉线自动重连**（keeper 后台线程维持），适合需要长期在线的场景。
- 槽配置持久化在 `data/slots.json`，重启后自动重建；`long` 槽重启后自动重连。
- 在 `docker-compose.yml` 中 `10802-10809` 已映射到宿主机，其他容器/宿主机直接指向对应端口即可（如 `http://<VPS_IP>:10802`）。

典型用法：主端口给临时任务轮询地区；把某个长期账户绑到 `slot-10802` 长期常驻，供固定爬虫/服务走 `:10802`；再开 `slot-10803` 绑定另一账户做短期任务。

## 部署到 VPS

```bash
cd ajiasu-gateway
./start.sh            # 等价于 docker compose up -d --build
```

确认运行：
```bash
docker ps                     # 看到 ajiasu-gateway 状态 Up
curl http://127.0.0.1:5702/api/ping   # compose 默认把容器 8000 映射为宿主机 5702
```

## 登录密码（面板与 API 均受保护）

- 面板和所有 `/api/*` 接口都要求认证，默认开启。
- 若部署时设置了环境变量 `ADMIN_PASSWORD`，则用它作为登录密码；否则首次启动自动生成随机密码并打印到日志：
  ```bash
  docker logs ajiasu-gateway | grep "已自动生成"
  ```
- 自动生成的密码持久化在数据卷 `data/gateway_auth.json`，重启后不变；之后可随时用 `ADMIN_PASSWORD` 覆盖。
- 登录失败有频控：同一 IP 5 分钟内错 5 次将被暂时拒绝。

### 修改登录密码

忘记自动生成的密码时，可随时在面板「修改登录密码」卡片查看当前密码并改成自己的密码；也可通过 API：

```bash
curl -s -X POST http://<网关地址>/api/change_password \
  -H "X-API-Key: <当前登录密码>" \
  -H "Content-Type: application/json" \
  -d '{"old_password":"<当前登录密码>","new_password":"<新密码>"}'
```

修改成功后立即生效，且 API Key（即登录密码）同步更新；新密码写入 `data/gateway_auth.json` 持久化。`GET /api/auth_info` 可查看当前密码（用于找回自动生成的密码）。

**其他 Docker/脚本调用 API 的两种方式**（密码即 API Key）：
```bash
# 方式一：X-API-Key 请求头（推荐）
curl -s -X POST http://<VPS_IP>:8000/api/acquire \
  -H "X-API-Key: <登录密码>" \
  -H "Content-Type: application/json" \
  -d '{"region":"上海","ttl":120}'

# 方式二：URL 追加 ?key= 参数
curl -s "http://<VPS_IP>:8000/api/status?key=<登录密码>"
```

## 使用

### 1. 登录面板
浏览器打开 `http://<VPS_IP>:8000`，输入登录密码进入。

### 2. 添加账户（Web 面板）
填入爱加速账号密码，点「添加并验证」。面板会展示登录结果、会员状态、可用节点，可勾选地区实时获取/释放代理。

### 3. 通过 API 添加账户（可选，供脚本批量添加）
```bash
curl -X POST http://<VPS_IP>:8000/api/accounts \
  -H "X-API-Key: <登录密码>" \
  -H "Content-Type: application/json" \
  -d '{"user":"13800000000","password":"******"}'
```

### 3. 获取指定地区代理（其他 docker 任务调用）
```bash
# 上海地区，有效 120 秒
curl -s -X POST http://<VPS_IP>:8000/api/acquire \
  -H "X-API-Key: <登录密码>" \
  -H "Content-Type: application/json" \
  -d '{"region":"上海","ttl":120}'
```
返回：
```json
{"ok":true,"lease_id":"1788727985732","proxy":"http://ajiasu-gateway:10801",
 "ip":"116.128.189.42","node":"上海 #259","expire_in":120}
```

### 4. 定时任务通过代理发请求
```bash
export http_proxy=http://<VPS_IP>:10801
export https_proxy=http://<VPS_IP>:10801
curl http://ip.sb          # 出口 IP 即为上海节点
```

Python：
```python
import requests
proxies = {"http": "http://<VPS_IP>:10801", "https": "http://<VPS_IP>:10801"}
r = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=30)
```

Node.js 定时面板可在任务内先调用 acquire 拿到 lease，执行完调用 release。

## REST API

完整接口文档（含参数与响应示例）见 [docs/API.md](docs/API.md)。端点速览：

除 `/login`、`/api/ping` 外，所有接口均需认证：会话 Cookie（面板登录后自动携带）或 `X-API-Key: <登录密码>` 请求头，或 URL 追加 `?key=<登录密码>`。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/ping` | 存活探针（无需认证） |
| GET | `/api/status` | 当前连接状态、租约剩余、对外代理地址 |
| GET | `/api/accounts` | 账户列表 |
| POST | `/api/accounts` | 添加账户 `{user,password}` |
| DELETE | `/api/accounts/<id>` | 删除账户 |
| GET | `/api/accounts/<id>/nodes` | 某账户节点列表 |
| GET | `/api/regions` | 全部地区名（节点名） |
| GET | `/api/nodes?region=X` | 指定地区可用节点 |
| POST | `/api/acquire` | 获取代理（主槽）`{region, ttl, force, account_id}` |
| POST | `/api/release` | 立即断开主槽连接 |
| GET | `/api/slots` | 列出全部端口槽（含主槽与额外槽） |
| POST | `/api/slots` | 创建额外端口槽 `{account_id, region, mode, ttl}` |
| POST | `/api/slots/<id>/connect` | 连接/重连指定槽 |
| POST | `/api/slots/<id>/disconnect` | 断开指定槽（保留槽，可再连） |
| DELETE | `/api/slots/<id>` | 删除额外端口槽（主槽不可删） |
| GET | `/api/slots/<id>/proxy` | 返回该槽对外代理地址 |
| POST | `/api/change_password` | 修改登录密码 `{old_password, new_password}` |
| GET | `/api/auth_info` | 返回当前登录密码（便于找回自动生成的密码） |

`/api/acquire` 参数：
- `region`：地区关键词，匹配节点名（如「上海」「江苏」「南京」）。不传则随机。
- `ttl`：有效期秒数，到期自动断开。默认 60。
- `force`：`true` 时若已有连接则强制切换。
- `account_id`：指定用哪个账户。

返回的 `proxy` 字段即供任务使用的代理地址；`ip` 是当前出口 IP（已实际验证）。

## 网络说明

- 容器内 `ajiasu` 监听 `127.0.0.1:1080`，仅容器内可达。
- 网关内建 **TCP 转发器**监听 `0.0.0.0:10801` 把流量转发到本地 1080，该端口随 docker compose 发布到宿主机。
- 其他容器与网关在同一 docker 网络时，可用 `PUBLIC_PROXY=http://ajiasu-gateway:10801`；跨主机访问则改成 `http://<VPS公网IP>:10801`（改 `docker-compose.yml` 中 `PUBLIC_PROXY`）。
- 只代理 http/https 流量，不支持 UDP 等。

## 目录结构

```
ajiasu-gateway/
├── app/
│   ├── auth.py              # 登录密码解析/持久化 + session + 登录频控
│   ├── ajiasu_manager.py    # 账户存储 + ajiasu 子进程管理 + 节点筛选 + 租约
│   ├── forwarder.py         # TCP 转发器 (0.0.0.0:10801 -> 127.0.0.1:1080)
│   └── web.py               # Flask REST API + 登录守卫 + Web 面板
├── templates/
│   ├── index.html           # Web 面板
│   └── login.html           # 登录页
├── docs/API.md              # REST API 完整文档
├── examples/acquire_demo.py # Python 调用示例
├── vendor/                  # 离线构建材料：ajiasu 二进制 + pip 依赖 wheels
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── start.sh
```

## 安全与合规

- 仅用于你本人拥有/合法授权账户的代理调度。请遵守爱加速服务条款与你所在地区法律。
- 面板与 API 已启用密码保护（`ADMIN_PASSWORD` 或自动生成，见上文），账户密码明文保存在 `data/accounts.json`，请确保数据卷权限安全。
- 建议同时用防火墙限制 8000/10801 只对可信来源开放（10801 代理端口本身无认证）。

## 致谢

- [s1g0day/Aijiasu_Agent_Pool](https://github.com/s1g0day/Aijiasu_Agent_Pool) —— 爱加速 Linux 客户端调用思路的来源项目
- 爱加速官方 Linux 客户端（`vendor/` 内随镜像分发的静态二进制，版权归爱加速所有，仅供配合本人账户使用）
