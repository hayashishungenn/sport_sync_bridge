# sport_sync_bridge

一个通用的运动记录桥接工具，用来把中国大陆生态里的 FIT 运动文件自动同步到 `Garmin Connect 国际区` 和 `Strava`。

当前实现的源平台:

- `iGPSPORT` 大陆版
- `OneLap / 顽鹿`
- 本地活动库（`FIT` / `GPX` / `TCX` / `ZIP` / 轨迹 `JSON` / `CSV`）

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
- 按 FIT 设备厂商、产品和固件版本选择坐标修正规则
- 在 FIT、GPX、TCX 之间转换活动文件
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

### 设备坐标规则

将 `FIT_COORDINATE_RULES_FILE` 指向的文件写成 JSON 数组。可把 `device_coordinate_rules.example.json` 复制为 `device_coordinate_rules.json` 后填写。文件不存在时按空规则处理，来源平台的坐标模式仍作为未匹配设备的回退值。规则使用 FIT 文件内的数字厂商和产品 ID，固件上下限均包含边界；固件未知时只匹配没有固件范围的规则。会重叠的规则会在启动时被拒绝。

```json
[
  {
    "manufacturer_id": 123,
    "product_id": 456,
    "firmware_min": 5.0,
    "firmware_max": 5.9,
    "coordinate_mode": "gcj02_to_wgs84"
  },
  {
    "manufacturer_id": 789,
    "coordinate_mode": "none"
  }
]
```

`coordinate_mode` 可设为 `gcj02_to_wgs84` 或 `none`。第二条示例不限制产品和固件版本。仓库提供空白模板 `device_coordinate_rules.example.json`，不会预置未经验证的厂商规则。

上面数字只作 JSON 格式占位，不对应已确认的设备或固件阈值。

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

Garmin 和 Strava 默认仍上传 FIT。可按目标分别指定格式，未指定的目标保持 FIT：

```powershell
python sync.py sync --format garmin=fit --format strava=tcx
python sync.py sync --target strava --format strava=gpx
```

也可以直接转换本地 FIT、GPX 或 TCX 文件：

```powershell
python sync.py convert ride.fit --to tcx --output ride.tcx --source igpsport
python sync.py convert ride.gpx --to fit --output ride.fit
```

`convert --source` 只影响 FIT 输入，并为未命中设备规则的文件指定 iGPSPORT 或 OneLap 的坐标模式。转换不会覆盖输入文件。输出文件会报告未能保留的字段；FIT 输出要求每个 GPS 轨迹点都带时间，TCX 输出也要求轨迹点带时间。

## 本地活动库与 GarSync 离线功能

从本地文件或目录导入活动。目录需要显式指定 `--recursive`；ZIP 会在内存中读取，不会按压缩包路径解压到磁盘。加密 ZIP 可通过 `ACTIVITY_ARCHIVE_PASSWORD` 提供密码。

```powershell
python sync.py library import .\activities.zip
python sync.py library import .\activities --recursive
python sync.py library list --from 2026-01-01 --sport cycling
python sync.py library show <活动ID前缀>
python sync.py library stats
python sync.py library report --format html --output .\activities.html
python sync.py library route <活动ID前缀> --to gpx --output .\route.gpx
python sync.py library merge .\part-1.fit .\part-2.fit --output .\merged.fit --name "合并骑行"
python sync.py sync --source local --target strava --format strava=tcx
```

导入文件保存在 `.data/local_imports/`，索引和汇总写入 `.data/sync_state.db`。本地源只向现有 Garmin / Strava 目标提供 `FIT`、`GPX`、`TCX`；健康摘要类 JSON 不会被当成可上传运动文件。轨迹 CSV 需要时间戳、纬度和经度列；活动 JSON 接受 `activity`、`laps` 和 `track_points` 等结构。

`library merge` 按输入顺序合并至少两个同运动类型的 FIT 活动。时间重叠的后续片段会平移到前一段结束后一秒，超过两秒的原有停顿会写成休息圈。输出最多保留 50,000 个记录点，抽稀时保留首尾点和可用指标的全局极值。合并会重新生成 FIT，因此来源设备身份、开发者字段和非记录消息不会复制；其他解析损失会随命令结果列出。该行为依据 APK AOT 静态线索实现，尚未用 GarSync 运行时样例逐字段对照。

GarSync 内置的六种语言训练计划和 33 个 FIT 课表作为本地模板随项目提供，不包含购买目录、数独或音频资源。计划开始日期必须是周一，安装后可导出到日历：

```powershell
python sync.py plans list --locale zh
python sync.py plans show 8w_beginner_run --locale zh
python sync.py plans install 8w_beginner_run --locale zh --start-date 2026-10-05
python sync.py plans installed
python sync.py plans export <计划ID前缀> --output .\training-plan.ics
python sync.py workouts list --sport CYCLING
python sync.py workouts show <课表ID>
python sync.py workouts export <课表ID> --output .\workout.fit
```

健康指标可从 UTF-8 CSV 导入，支持 `date,metric,value,unit` 长表格式及带日期列的宽表。指标包括体重、身高、静息心率、HRV、血氧、睡眠、步数、压力、身体电量和血压；体重与身高齐全时会计算 BMI。

```powershell
python sync.py health import .\health.csv
python sync.py health summary
```

AI 运动分析保留为可选功能。它把单次活动摘要、最近活动的周汇总和本地健康指标发送给 OpenAI 兼容的 Chat Completions 接口，不发送 GPS 坐标。可先检查提示词，再配置自己使用的远端或本地模型：

```powershell
python sync.py ai-analysis <活动ID前缀> --prompt-only
```

在 `.env` 中设置 `AI_API_BASE_URL`、`AI_MODEL`，远端服务需要时再设置 `AI_API_KEY`。请求成功后会在本地 SQLite 中保存模型名和分析正文，可用 `python sync.py ai-analysis <活动ID前缀> --history` 查看。项目不会附带 GarSync 的服务凭据或计费代码。

Wi-Fi 文件导入页默认只监听本机。要让手机从同一局域网访问，显式绑定局域网接口：

```powershell
python sync.py receive --host 0.0.0.0 --port 8765
```

接收页不设访问口令，只应在可信的本地网络中临时开启。

未移植到 Python CLI 的 APK 功能包括其余云平台的私有认证/同步协议、Samba、手机 BLE 与传感器实时录制、路线地图/分享海报/PDF 报告、训练负荷/VO2Max/恢复指标、在线健康数据源、天气、AI 聊天及 AI 计划/课表生成。当前 AI 活动分析使用本地汇总和可配置模型接口。静态 AOT 索引不足以确认这些云端接口的运行期请求、服务端校验或设备交互行为；完整静态盘点见 `GARSYNC_APK_REVERSE.md`。

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
- `.data/converted/<source>/<target>/`，按目标生成的上传文件
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
