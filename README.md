# sport_sync_bridge

一个通用的运动记录桥接工具，用来把中国大陆生态里的 FIT 运动文件自动同步到 `Garmin Connect 国际区` 和 `Strava`。

当前实现的源平台:

- `iGPSPORT` 大陆版
- `OneLap / 顽鹿`

当前实现的目标平台:

- `Garmin Connect` 国际区
- `Strava`

这个项目参考了以下公开实现，并抽成了统一的 `source adapter + target adapter + SQLite state` 架构：

- `simple4wan/ride-sync`
- `fooooxxxx/igpsport-export-fit-files`
- `DreamMryang/synchronizeTheRecordingOfOnelapToGiant`
- `Dunky-Z/FitSync`
- `cyberjunky/python-garminconnect`
- Strava 官方 `Authentication` / `Uploads` 文档

## 设计目标

- 自动从 `iGPSPORT` / `OneLap` 拉取活动列表并下载 FIT
- 自动对下载后的 FIT 做坐标修正
- 自动上传到 `Garmin Connect 国际区`
- 自动上传到 `Strava`
- 用 `SQLite` 记录同步状态，避免重复上传
- 对 `iGPSPORT` / `OneLap` 都支持可选的 `GCJ-02 -> WGS84` 轨迹修正

## 为什么要做坐标修正

`2026-03-04` 的顽鹿公告已经明确提到，导出的骑行轨迹会按 `GCJ-02` 存储/展示；而 `Strava` / `Garmin Connect` 按 `WGS84` 解释坐标。  
如果直接上传，轨迹会发生偏移。

因此本项目默认对 `iGPSPORT` 和 `OneLap` 的 FIT 文件都先执行坐标修正，再上传:

- `IGPSPORT_COORD_MODE=gcj02_to_wgs84`
- `ONELAP_COORD_MODE=gcj02_to_wgs84`

如果你确认自己的文件不需要修正，可以改成:

- `IGPSPORT_COORD_MODE=none`
- `ONELAP_COORD_MODE=none`

## 快速开始

### 1. 环境要求

- Python `3.10+`

### 2. 安装依赖

```powershell
cd C:\Users\Hayas\Github\sport_sync_bridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. 配置

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`。

最少需要填:

- `IGPSPORT_USERNAME` / `IGPSPORT_PASSWORD` 或 `IGPSPORT_ACCESS_TOKEN`
- `ONELAP_USERNAME` / `ONELAP_PASSWORD` 或 `ONELAP_COOKIE`
- `GARMIN_EMAIL` / `GARMIN_PASSWORD`
- `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET`

### 4. Strava 授权

先输出授权 URL:

```powershell
python sync.py strava-auth-url
```

浏览器打开后，同意 `activity:write` 权限。回调 URL 里会带一个 `code=...`。

拿到 code 之后执行:

```powershell
python sync.py strava-exchange --code 你的code
```

新的 `access_token` / `refresh_token` 会写进本地 SQLite，不需要每次再手填。

### 5. 首次同步

```powershell
python sync.py sync
```

默认流程:

```text
iGPSPORT / OneLap -> 下载 FIT -> 修正坐标 -> 上传 Garmin 国际区 -> 上传 Strava
```

常用参数:

```powershell
python sync.py sync --source igpsport --target garmin

python sync.py sync --from 2026-01-01 --to 2026-03-01

python sync.py sync --dry-run

python sync.py sync --loop --interval 900
```

## OneLap 登录说明

`OneLap` 的登录接口已经被公开项目确认带有签名校验。当前实现优先顺序如下:

1. `ONELAP_COOKIE`
2. `ONELAP_USERNAME` + `ONELAP_PASSWORD` + 登录签名

如果后续 OneLap 再次调整接口，最稳的兜底方式通常是浏览器登录后，把 Cookie 填到 `ONELAP_COOKIE`。

## 目录说明

运行后默认会生成:

- `.data/sync_state.db`
- `.data/sync.log`
- `.data/downloads/`
- `.data/repaired/`
- `.data/.garmin_session/`

## 状态查看

```powershell
python sync.py status
```

示例输出:

```text
activities=128
success=240
duplicate=16
failed=3
```

## 注意事项

- `Garmin` 端使用的是社区库 `python-garminconnect`，本质上不是 Garmin 官方公开上传 API。
- `Strava` 上传使用官方 Uploads API，需要 `activity:write` 权限。
- `Strava` 的 `refresh_token` 会轮换，项目会把新 token 持久化到 SQLite。
- `iGPSPORT` / `OneLap` 的坐标修正会尝试保留原始 FIT 消息结构，但没有在你的真实数据上做过回归测试。
- 如果修正失败，而对应源平台的 `*_COORD_STRICT=false`，程序会退回上传原始 FIT。

## Docker 部署

项目包含 `Dockerfile` 和 `docker-compose.yml` 可以快速在本地或服务器上通过 Docker 部署。

1. 确保在 `.env` 文件中配置了必要的环境变量。
2. 运行以下命令启动服务：

```powershell
docker-compose up -d
```

这将会在后台启动一个容器，并按照 `.env` 文件中 `SYNC_INTERVAL` 指定的时间间隔自动进行循环同步。



## 参考来源

- `https://github.com/simple4wan/ride-sync`
- `https://github.com/fooooxxxx/igpsport-export-fit-files`
- `https://github.com/DreamMryang/synchronizeTheRecordingOfOnelapToGiant`
- `https://github.com/Dunky-Z/FitSync`
- `https://github.com/cyberjunky/python-garminconnect`
- `https://developers.strava.com/docs/authentication/`
- `https://developers.strava.com/docs/uploads/`
