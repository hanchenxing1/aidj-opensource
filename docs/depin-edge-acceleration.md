# 边缘计算提速设计 · DePIN P2P 加速网络（depina）

AIDJ 内置一套 **DePIN（去中心化基础设施）边缘加速网络**，把「用户自己的设备」变成边缘加速节点：
单机版/手机端的 sidecar 在后台提供一个 **HTTP 反代数据面**，网页端访问远端资源时，
中心控制面就近选一个「已开通加速 + 可服务该 host + 最近活跃」的节点，
由该节点从源站拉回响应、经 **libp2p P2P 通道**回流给发起方，从而减少「网页端 → 源站」之间的公网跳数，降低首字节延迟。

> 这是「闲置带宽再利用 + 单向激励」模型：节点侧贡献流量赚奖励（分 → 可折算视频时长），
> 使用侧（网页用户）反代流量免费，不做 P2P 计费扣费。

## 架构总览

```
┌──────────────┐    ① registry/nearby?url=...    ┌──────────────┐
│  网页端        │ ───────────────────────────────▶ │  中心控制面    │
│  (browser/H5) │ ◀─────────────────────────────── │  (156 中心)   │
└──────┬───────┘   返回「就近节点 peer_id + 地址」  └──────┬───────┘
       │                                                  │ ② 下发「请代拉 url」
       │                                                  ▼
       │                                    ┌────────────────────────┐
       │  ④ P2P 回流（响应体经 libp2p）     │  边缘节点 A（用户设备） │
       └──────────────────────────────────▶ │  sidecar（libp2p host）│
                                            │  - set_proxy_enabled   │
                                            │  - http_proxy 反代     │
                                            └───────────┬────────────┘
                                                        │ ③ 代拉源站
                                                        ▼
                                                   源站（CDN/服务器）
```

四个步骤：

1. **就近选路**：网页端把要访问的 URL 交给中心 `POST /api/depina/registry/nearby`；
   中心从节点注册表挑一个可用节点（`proxy_enabled=True`、白名单含目标 host、
   `last_seen_at` 90s 内有心跳），按「已服务流量升序」选轻负载节点随机打破平局。
2. **任务下发**：中心/发起方通过 libp2p 数据面（QUIC/TCP，relay 兜底）
   向该节点 sidecar 发 `http_proxy` 请求。
3. **边缘代拉**：节点 sidecar 作为临时边缘代理去拉源站，命中「离用户更近的接入点」，
   减少跨境/跨 ISP 的 RTT 跳数。
4. **P2P 回流 + 激励**：响应经 P2P 回流给发起方；节点贡献按
   `served_bytes` 记账，奖励 = 每 KB × `ACCEL_REWARD_FEN_PER_KB`（当前 1 分/KB ≈ ¥1/GB），
   原子写入节点 owner 钱包（`users.balance_fen`）并累计 `depina_nodes.reward_fen`。

## 组件与代码位置

| 层 | 文件 | 职责 |
|---|---|---|
| P2P 身份 | `backend/app/services/depina/sidecar_client.py` | 拉起 Go sidecar 子进程（libp2p host），本地持久化 Ed25519 seed（`data/libp2p_seed.b64`），重启后 peer_id 稳定；封装 `status / add_peer / dial_peer / accel_echo / connectedness / relay_peers / dht_lookup / http_proxy / set_proxy_enabled` |
| 中心控制面 | `backend/app/services/depina/registry.py` | `depina_nodes` 注册表 + 就近选路 + 贡献记账 + 每日加速用量（`depina_accel_usage`，按 user+日 唯一） |
| 激励 | `backend/app/services/depina/contribution.py` | 贡献日志（JSONL 一行/条）+ Merkle Root（MVP 不上链，预留上链锚定扩展点），根由节点密钥 Ed25519 签名 |
| 单机版开关 | `backend/app/services/depina/node_service.py` | 用户开关「加速服务」→ sidecar `set_proxy_enabled` + 向中心注册/心跳（60s 间隔）+ 贡献奖励查询；关机注销 |
| API 路由 | `backend/app/api/depina.py` | 全部对外端点（节点生命周期 / P2P 节点管理 / 加速数据面 / 注册表 / 贡献） |
| 鉴权闸门 | `backend/app/services/billing/subscription.py` | `require_accel`：订阅（plus/pro 含 `accel_gb_per_day`）或余额 > 0 按量付费才允许开通节点 |

### 关键端点（`/api/depina/*`）

```text
节点生命周期：
  POST /api/depina/node/start        # 幂等启动 sidecar
  POST /api/depina/node/stop
  GET  /api/depina/node/status
  POST /api/depina/node/enable       # 开/关反代开关（req.proxy_enabled，开须过 require_accel）
  GET  /api/depina/node/contribution # 本节点累计 served_bytes/served_peers

P2P 节点管理（v12+）：
  POST /api/depina/peers/add         # 把目标 multiaddr 注入 peerstore
  POST /api/depina/peers/dial        # 与目标 peerId 建 P2P 连接（QUIC/TCP/relay）
  GET  /api/depina/peers/status      # 对端连接状态（Connected/Disconnected/Limited）
  GET  /api/depina/peers/relay       # 列出当前所有 P2P 连接
  POST /api/depina/peers/lookup      # DHT 查目标 peerId 的已知地址
  POST /api/depina/accel/echo        # 发一条回显流，验证 P2P 数据面

加速数据面（v15）：
  POST /api/depina/accel/proxy       # 让对端节点代拉某个 URL（P2P 回流响应）

中心注册表 + 就近选路 + 激励（v15）：
  POST /api/depina/registry/register   # 节点向中心注册（首次 + 60s 心跳，幂等）
  POST /api/depina/registry/nearby     # 网页端按 url 找就近节点
  POST /api/depina/registry/contribute # 贡献上报 + 奖励入账（同事务原子提交）
```

## 就近选路策略（MVP v1）

`registry.find_nearby_node(url)` 的筛选顺序：

1. `proxy_enabled == True`（节点当前开了反代）
2. `allowed_hosts` 白名单包含目标 host（节点未设白名单 = 全放行）
3. `last_seen_at` 距今 < `STALE_AFTER_SEC`（90s，心跳 60s 间隔保证不离线）
4. 候选集按 `served_bytes` 升序（轻负载优先），取前 `limit` 个后随机选 1

无可用节点时返回 `{"node": null}`，网页端**自动回退直连源站**，不影响可用性——
P2P 加速是「有则更好」的旁路，不是强依赖。

## 单向激励模型（v15.x）

```
用户（网页端）使用反代：一律免费，不扣余额（payg_bytes 恒 0）
节点（贡献方）  赚反代：  reward_fen = ceil(served_bytes/1024) × ACCEL_REWARD_FEN_PER_KB
                         原子写入 users.balance_fen（可折算视频秒数）
                         + 累计 depina_nodes.reward_fen（节点维度统计）
```

- **订阅用户（plus/pro）**：每日 `accel_gb_per_day` 配额（1GB / 10GB）内免费，超额 403 拒绝，次日 UTC 0 点恢复。
- **余额用户（无订阅）**：配额 = 0 视为不限量免费，仅节点侧赚贡献奖励。
- **为什么这样设计**：激励曲线单向（只有贡献方赚），使用方零摩擦，
  鼓励「把闲置带宽变成收益」而不是「用 P2P 计费」。配额/费率参数都在 `registry.py` 常量里，生产可调。

## 身份与安全边界

- 节点身份 = libp2p Ed25519 公钥；seed 本地存 `data/libp2p_seed.b64`（重启稳定），
  `GET /api/depina/seed` 可导出（**私钥，仅 owner 应调用**）。
- `http_proxy` 支持 `allowed_hosts` 白名单，防止节点被滥用成开放代理。
- `contrib` 记录 Merkle Root 由节点密钥签名，**MVP 不上链**，链上锚定是预留扩展点。
- P2P 连接状态/relay/连通性均可查（`connectedness`、`relay_peers`），便于排障。

## 与 AIDJ 主链路的关系

AIDJ 的核心是「剧本 → 分镜 → 资产 → 视频合成」的短剧生产链路。
`depina` 是**独立旁路模块**：

- 主链路（分镜生成/视频合成/BGM/配音）**不依赖** DePIN；关掉 sidecar 主功能照常。
- sidecar 自动拉起（`main.py` lifespan 调 `depina.start_sidecar_quietly()`，失败仅告警不阻塞）。
- 单用户机器也可**贡献方 + 使用方**身份合一：本机既赚贡献奖励，也能当加速节点。

## 扩展路线（Roadmap）

- **v2 就近策略**：当前 MVP 是「活跃 + 轻负载 + 随机」；下一步按 IP 地理归属 / ASN 选路，
  或引入 BGP/延迟探测打分（`dht_lookup` 已能查目标节点地址，具备选路数据基础）。
- **贡献验证上链**：`contribution.py` 的 Merkle Root 已签名，预留「定期把 root 锚到链上」
  做抗篡改证明（MVP 不上链以省 gas）。
- **跨节点带宽联盟**：当前是单节点代拉；可扩展为节点联盟联合加速（multi-hop relay）。

## 快速验证（单机）

```bash
# 1. 启动 sidecar + 查状态
curl -X POST http://127.0.0.1:8000/api/depina/node/start
curl http://127.0.0.1:8000/api/depina/node/status

# 2. 开反代（需登录 + require_accel 通过）
curl -X POST http://127.0.0.1:8000/api/depina/node/enable \
  -H 'Content-Type: application/json' -d '{"enabled": true, "allowed_hosts": []}'

# 3. 回显验证 P2P 数据面（先 add_peer + dial_peer 建连接）
curl -X POST http://127.0.0.1:8000/api/depina/accel/echo \
  -H 'Content-Type: application/json' -d '{"target": "<peer_id>", "payload_hex": "deadbeef"}'
```

> 单机版前端「加速开关页」（`frontend/accel.html`）+「新 key 引导页」（`newkey.html`）
> 是这套 DePIN 机制的用户入口；开启后会看到本机 `peer_id`、监听地址与累计奖励。
