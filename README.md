# AIDJ · AI 短剧生成器（单机版 + 网页版 + 手机端）

AIDJ 是一个面向个人创作者的 AI 短剧/漫剧生产工具，覆盖从剧本导入、分镜生成、资产生成到视频合成、BGM/配音的完整链路。

> **边缘计算亮点**：内置 **DePIN P2P 边缘加速网络（depina）**——把「用户自己的设备」变成边缘加速节点，
> 网页端访问远端资源时由就近节点代拉源站、经 libp2p 回流，减少公网跳数降低首字节延迟；
> 节点贡献流量赚奖励（单向激励：使用方免费，贡献方赚）。
> 完整设计见 [docs/depin-edge-acceleration.md](docs/depin-edge-acceleration.md)。

## 项目标签

`AI short drama` `AI video generator` `FastAPI` `P2P` `DePIN` `edge computing` `libp2p` `proxy acceleration` `WebRTC` `Android` `Windows desktop`

本仓库包含：

- **单机版**：Windows 桌面软件（WebView2 内嵌前端）+ FastAPI 后端 + 内嵌 Python 3.12 运行时，全程本地 SQLite 记录，唯一外部调用为 `new.aiwyq.bond` 付费网关（需自行申请 key）
- **网页版前后端**：`frontend/` 静态页 + `backend/` FastAPI 服务，可直接本地起服务，也可部署为独立站点
- **手机端客户端**：`android-apk/` 目录下的 Android APK（release / debug），可直接安装或自行用 Android Studio 重新构建
- **DePIN 边缘加速**：`backend/app/services/depina/` + `backend/app/api/depina.py`，libp2p P2P 网络 + HTTP 反代数据面 + 就近选路 + 贡献奖励（详见上方文档）

## 目录结构

```
aidj-opensource/
├── backend/            # FastAPI 后端（app/ 为主）
│   ├── app/
│   │   ├── api/        # 路由（projects / drama / billing / tasks / depina ...）
│   │   ├── services/   # 业务逻辑（drama 生成、billing、depina、ffmpeg_compose ...）
│   │   │   └── depina/ # DePIN 边缘加速（sidecar/registry/contribution/node_service）
│   │   ├── models_depina.py  # DePIN 节点/加速用量 表模型
│   │   └── main.py     # FastAPI 入口（lifespan 自动拉起 depina sidecar）
│   ├── requirements.txt
│   └── ...
├── frontend/           # 网页版前端静态资源（含加速开关页 accel.html）
├── android-apk/        # 手机端 APK（release + debug）
├── docs/
│   └── depin-edge-acceleration.md  # ★ 边缘计算 / DePIN 加速设计说明
├── .gitignore
└── README.md
```

## 边缘计算 / DePIN 加速（depina）

**一句话**：把每个装了 AIDJ 的用户设备变成「边缘 CDN 节点」，用闲置带宽给网页端提速，贡献赚奖励。

```
网页端 ──nearby?url──▶ 中心控制面(156) ──选路──▶ 就近边缘节点(用户设备)
   ▲                                          │
   │              ② http_proxy 代拉源站       │
   └──────────── ④ libp2p P2P 回流响应 ◀──────┘
```

- **P2P 数据面**：Go sidecar（libp2p host）+ Ed25519 身份 + 本地 seed 持久化（peer_id 重启稳定）
- **就近选路**：`registry.find_nearby_node` 按「proxy_enabled + 白名单 host + 90s 心跳 + served_bytes 升序」挑轻负载节点；无可用自动回退直连源站
- **单向激励**：使用方反代流量免费；贡献方按 `每 KB × ACCEL_REWARD_FEN_PER_KB`（1分/KB≈¥1/GB）入账节点 owner 钱包（可折算视频秒数）
- **验证**：`accel_echo`（回显）+ `connectedness`/`relay_peers`（连接状态）排障

> 完整架构图、组件位置、API 端点、选路策略、激励模型、扩展路线：
> [docs/depin-edge-acceleration.md](docs/depin-edge-acceleration.md)


## 单机版（Windows）

> 完整使用说明见原包内 `README.txt`（本次仓库未包含二进制运行时，以下描述与原始单机版保持一致）。

### 功能定位

- 基于 156 线上最新代码（2026-10-16 备份）
- 本地运行，生成记录全部落本地（SQLite + 本地文件），不依赖任何远程 SSO/云盘/七牛/OSS
- 唯一外部调用：`new.aiwyq.bond` 付费网关（需用户自行申请 API key，`sk-` 开头）
- 无后台管理、无注册，内置本机账号自动登录
- 桌面软件窗口（WebView2/Edge 内核内嵌前端）

### 安装与启动

原始单机版提供 `AIDJ安装.exe` 安装器（GUI，可选安装目录 + 进度条）和 `AIDJ.exe` 启动器（`open/start/stop/status/key` 子命令）。

本仓库不包含 Windows 二进制（exe/dll/python 运行时/ffmpeg），需自行打包或参考 `start.bat` 逻辑：

```bat
@echo off
start "aidj-standalone" /D "<backend目录>" python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
start http://127.0.0.1:8000
```

首次启动会自动进入「新手指引」页（`frontend/guide.html`）：

1. 打开 `new.aiwyq.bond` 控制台 → 邮箱注册 → 复制 API key
2. 粘贴到引导页第 2 步 → 保存 key（存到 `backend/data/local_api_key.txt`）
3. 重启服务后 key 生效

## 网页版（前后端分离）

### 后端

```bash
cd backend
pip install -r requirements.txt
# 配置 .env（参考 backend/.env.example）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 前端

`frontend/` 为纯静态资源（HTML/CSS/JS + 图片），直接由 FastAPI 静态路由或任意静态服务器托管：

```bash
# 方式一：由后端直接托管（默认）
# 方式二：单独起静态服务器
npx serve frontend
```

主要页面：

| 路径 | 说明 |
|---|---|
| `/` (`index.html`) | 工作台 |
| `/guide.html` | 新手指引 |
| `/login.html` | 登录 |
| `/accel.html` | 节点加速开关 |
| `/newkey.html` | 新 key 引导 |

## 手机端（Android）

- `android-apk/release/` → 正式包（已签名 release）
- `android-apk/debug/` → 调试包

直接安装 APK 即可使用；如需重新构建，源码位于原 Android 工程（本仓库仅打包产物，未包含完整 Gradle 工程目录）。

## 敏感信息说明（开源前已脱敏）

本次发布已从仓库中排除：

- `backend/.env`（含真实 API key / SECRET_KEY / 支付配置）
- `backend/data/`（本地 SQLite 数据库、`local_api_key.txt`、`agnes_key_pool.json` 等运行时数据）
- `backend/logs/`、`backend/static/generated/`、`backend/static/site/`（运行时生成物）
- `*.bak`、`*.pre-*`（历史备份文件）
- `_probe*.py`、`_oneclick_test.py`（临时探测脚本）
- 内嵌 Python 运行时、WebView2 DLL、ffmpeg 二进制（`python/`、`ffmpeg/`）
- 项目示例数据 `LZD_PAWN_001/`、`_156center/`

请在部署前：

1. 复制 `backend/.env` 为 `.env.example` 并清空所有 key（本仓库未包含 `.env`）
2. 按 `README.txt` 指引申请自己的 `new.aiwyq.bond` API key
3. 数据库会在首次启动时自动初始化

## License

本项目以 Apache-2.0 协议开源，详见 `LICENSE`。

## 致谢

- [FastAPI](https://fastapi.tiangolo.com/)
- [Uvicorn](https://www.uvicorn.org/)
- [WebView2](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)
