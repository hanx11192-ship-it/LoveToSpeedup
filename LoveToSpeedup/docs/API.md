# 爱加速代理网关 API 文档

所有接口除 `/api/ping` 外均需认证。Base URL 即网关地址，默认端口 `8000`（本仓库 compose 映射为宿主机 `5702`，以实际部署为准）。

## 认证

以下三种方式任选其一：

| 方式 | 用法 | 适用场景 |
|------|------|----------|
| Session | 浏览器登录面板后自动携带 Cookie | Web 面板 |
| 请求头 | `-H "X-API-Key: <登录密码>"` | 脚本、其他容器（推荐） |
| 查询参数 | URL 追加 `?key=<登录密码>` | 快速 curl 调试 |

登录密码由环境变量 `ADMIN_PASSWORD` 指定；未设置时首次启动自动生成并打印到容器日志，同时持久化在数据卷 `data/gateway_auth.json`。

未认证的 API 请求返回：

```json
{"ok": false, "error": "unauthorized: 请先登录，或携带 X-API-Key / ?key= 参数"}
```

HTTP 状态码约定：`200` 成功、`400` 参数错误、`401` 未认证、`404` 资源不存在、`409` 业务失败（如无可用节点）、`429` 登录尝试过于频繁。

---

## 端点总览

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/api/ping` | 无 | 存活探针，容器健康检查用 |
| GET | `/login` | 无 | 登录页 |
| POST | `/login` | 无 | 提交登录密码，成功后写入会话 |
| POST/GET | `/logout` | 会话 | 退出登录 |
| GET | `/api/status` | 需要 | 当前连接状态、租约剩余 |
| GET | `/api/accounts` | 需要 | 账户列表（不含密码） |
| POST | `/api/accounts` | 需要 | 添加账户并立即验证登录 |
| DELETE | `/api/accounts/<id>` | 需要 | 删除账户，若是当前连接账户则断开 |
| GET | `/api/accounts/<id>/nodes` | 需要 | 某账户节点列表 |
| GET | `/api/regions` | 需要 | 全部可用地区名 |
| GET | `/api/nodes` | 需要 | 可用节点列表，可按地区过滤 |
| POST | `/api/acquire` | 需要 | 获取指定地区的临时代理（核心接口） |
| GET/POST | `/api/extract` | 需要 | 提取式接口，直接返回 `ip:port` 文本，兼容常见代理池采集器 |
| POST | `/api/release` | 需要 | 立即断开当前代理连接 |

---

## 端点详情

### GET /api/ping

```json
{"ok": true}
```

### GET /api/status

```json
{
  "ok": true,
  "connected": true,
  "node": "上海 #259",
  "exit_ip": "116.128.189.42",
  "lease_id": "1788727985732",
  "lease_remaining": 95.3,
  "forward_port": 10801,
  "public_proxy": "http://lovespeedup:10801"
}
```

### POST /api/accounts

请求体：

```json
{"user": "13800000000", "password": "******"}
```

响应（`check` 为爱加速登录验证结果）：

```json
{
  "ok": true,
  "id": "1788727900001",
  "user": "13800000000",
  "check": {"ok": true, "membership": "VIP", "expiration": "2026-12-31"}
}
```

### GET /api/accounts/<id>/nodes

查询参数 `refresh=1` 强制重新拉取节点（默认走缓存）。

```json
{"ok": true, "nodes": [{"id": "vvn-2466-1878", "status": "ok", "name": "上海 #259"}]}
```

### GET /api/regions

```json
{"ok": true, "regions": ["上海", "江苏", "南京"]}
```

### GET /api/nodes

查询参数：`region`（地区关键词过滤，可省略）、`account_id`（限定账户，可省略）。

```json
{"ok": true, "count": 2, "nodes": [{"account_id": "...", "account_user": "138...", "id": "vvn-2466-1878", "status": "ok", "name": "上海 #259"}]}
```

### POST /api/acquire

请求体（JSON，字段均可省略）：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `region` | string | 随机 | 地区关键词，匹配节点名（如「上海」「江苏」「南京」） |
| `ttl` | int | 60 | 租约有效期（秒），到期自动断开 |
| `force` | bool | false | 已有连接时是否强制切换（爱加速同时仅允许一个连接） |
| `account_id` | string | 自动 | 指定使用哪个账户 |

```bash
curl -s -X POST http://<网关地址>/api/acquire \
  -H "X-API-Key: <登录密码>" \
  -H "Content-Type: application/json" \
  -d '{"region":"上海","ttl":120}'
```

成功响应：

```json
{
  "ok": true,
  "lease_id": "1788727985732",
  "proxy": "http://lovespeedup:10801",
  "ip": "116.128.189.42",
  "node": "上海 #259",
  "account": "13800000000",
  "expire_in": 120
}
```

- `proxy`：供其他任务直接使用的 HTTP 代理地址（取自环境变量 `PUBLIC_PROXY`）。
- `ip`：已实测验证的出口 IP。
- 同地区重复调用会续租并复用当前连接（`reused` 相关信息见 status）。

失败响应（HTTP 409）：

```json
{"ok": false, "error": "已有活动连接，需先 release 或用 force=1 强制切换"}
```

### GET/POST /api/extract

与 `/api/acquire` 等价，但返回格式面向代理池采集器。地区参数支持 `region`、`city`、`province` 等别名，额外支持 `format`：

```bash
curl "http://<网关地址>/api/extract?region=上海&ttl=120&key=<登录密码>"
# 纯文本响应:
# 116.128.189.42:10802
```

`format=json` 时：

```json
{"ok": true, "ip": "<网关地址主机名>", "port": 10802, "proxy": "主机:端口", "node": "上海 #259", "exit_ip": "116.128.189.42", "expire_in": 120, "reused": false}
```

注意：`ip`/`port` 来自 `PUBLIC_PROXY` 解析，跨主机使用时请把 `PUBLIC_PROXY` 配置为公网可达地址。

### POST /api/release

```json
{"ok": true}
```

### POST /api/change_password

修改登录密码（即 API Key）。需先用旧密码校验。

请求体：

```json
{"old_password": "当前密码", "new_password": "新密码"}
```

成功响应（立即生效，并写入 `data/gateway_auth.json`）：

```json
{"ok": true}
```

失败响应（HTTP 403，旧密码不正确 / HTTP 400 新密码为空）：

```json
{"ok": false, "error": "旧密码不正确"}
```

### GET /api/auth_info

返回当前登录密码，便于找回自动生成的密码。

```json
{"ok": true, "password": "G3x...yZ", "source": "generated", "generated": true}
```

---

## 端口槽（多端口 / 多账户并行）

> 主槽 `main` 对应固定端口 `PROXY_FORWARD_PORT`（默认 10801），行为同上方 `/api/acquire`。
> 额外端口槽允许把**不同账户**绑定到**不同对外端口**并行在线；`long` 模式常驻并保持掉线自连。

### GET /api/slots

列出全部槽（含 `main` 与动态创建的额外槽）。

```json
{"ok": true, "slots": [
  {"slot_id": "main", "forward_port": 10801, "mode": "short", "persistent": false,
   "account_id": null, "region": null, "connected": true, "node": "上海 #259",
   "exit_ip": "116.128.189.42", "lease_remaining": 95.3, "proxy": "http://lovespeedup:10801"},
  {"slot_id": "slot-10802", "forward_port": 10802, "mode": "long", "persistent": true,
   "account_id": "1788727900001", "region": "江苏", "connected": true, "node": "江苏 #12",
   "exit_ip": "58.x.x.x", "lease_remaining": null, "proxy": "http://lovespeedup:10802"}
]}
```

### POST /api/slots

创建额外端口槽（端口从 `SLOT_EXTRA_PORT_START..` 自动分配，默认 10802 起）。

请求体：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `account_id` | string | 自动 | 绑定哪个账户；留空由网关自动挑选 |
| `region` | string | 不限 | 绑定地区（节点名关键词） |
| `mode` | string | `short` | `short` 带 ttl 到期断开；`long` 长期常驻+掉线自连 |
| `ttl` | int | 60 | `short` 模式下的有效期秒数 |

```bash
curl -s -X POST http://<网关地址>/api/slots \
  -H "X-API-Key: <登录密码>" -H "Content-Type: application/json" \
  -d '{"account_id":"1788727900001","region":"江苏","mode":"long"}'
```

成功响应：

```json
{"ok": true, "slot_id": "slot-10802", "forward_port": 10802, "persistent": true,
 "proxy": "http://lovespeedup:10802", "ip": "58.x.x.x", "node": "江苏 #12"}
```

### POST /api/slots/<id>/connect

连接/重连指定槽，可覆盖地区与账户。`force=1` 强制重连。

```bash
curl -s -X POST http://<网关地址>/api/slots/slot-10802/connect \
  -H "X-API-Key: <登录密码>" -H "Content-Type: application/json" \
  -d '{"force": true}'
```

### POST /api/slots/<id>/disconnect

断开指定槽（保留槽配置，可再次 connect）。主槽不可用此接口关闭长期行为。

### DELETE /api/slots/<id>

删除额外端口槽（停止其转发器与 ajiasu 实例，并从 `data/slots.json` 移除）。`main` 不可删。

### GET /api/slots/<id>/proxy

返回该槽对外代理地址（`http://<网关主机>:<forward_port>`）。

---

## 代理使用方式

拿到租约后，用任意 HTTP 客户端指向网关代理端口即可：

```bash
export http_proxy=http://<网关IP>:<代理端口>
export https_proxy=http://<网关IP>:<代理端口>
curl http://ip.sb
```

Python 示例见 [examples/acquire_demo.py](../examples/acquire_demo.py)。
