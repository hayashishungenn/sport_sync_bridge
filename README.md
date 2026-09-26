# sport_sync_bridge

一个通用的运动记录桥接工具，用来把中国大陆生态里的 FIT 运动文件自动同步到 `Garmin Connect 国际区` 和 `Strava`。

当前实现的源平台:

- `iGPSPORT` 大陆版
- `OneLap / 顽鹿`
- `Hammerhead`（公开 API 活动来源）
- `Polar Flow`（AccessLink API 活动来源）
- `Fitbit`（Google Health API 活动来源）
- `Withings`（Public API 活动来源）
- `COROS`（官方 MCP 活动来源）
- `Ride with GPS`（官方 API 活动来源）
- `MyWhoosh`（使用未公开应用接口的活动来源）
- 本地活动库（`FIT` / `GPX` / `TCX` / `ZIP` / 轨迹 `JSON` / `CSV`）

当前实现的目标平台:

- `Garmin Connect` 国际区
- `Strava`
- `Wahoo`（FIT 上传）
- `Hammerhead`（路线上传）
- `Ride with GPS`（FIT / GPX / TCX 活动上传）

这个项目参考了以下公开实现，并抽成了统一的 `source adapter + target adapter + SQLite state` 架构：

- `simple4wan/ride-sync`
- `fooooxxxx/igpsport-export-fit-files`
- `DreamMryang/synchronizeTheRecordingOfOnelapToGiant`
- `Dunky-Z/FitSync`
- `cyberjunky/python-garminconnect`
- Strava 官方 `Authentication` / `Uploads` 文档
- Wahoo 官方 Cloud API 文档
- Hammerhead 官方 Public API 文档
- Polar 官方 AccessLink API 文档

## 设计目标

- 自动从 `iGPSPORT` / `OneLap` 拉取活动列表并下载 FIT
- 自动对下载后的 FIT 做坐标修正
- 按 FIT 设备厂商、产品和固件版本选择坐标修正规则
- 在 FIT、GPX、TCX 之间转换活动文件
- 从本地传感器事件流计算跑步动态指标
- 自动上传到 `Garmin Connect 国际区`
- 自动上传到 `Strava`
- 可选上传到 `Wahoo`（FIT）
- 从 Hammerhead 读取活动 FIT，并把活动文件作为路线上传到 Hammerhead
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

Intervals.icu 可作为活动来源和上传目标。配置 `INTERVALS_ICU_ATHLETE_ID` 和 `INTERVALS_ICU_API_KEY` 后，可以读取 FIT 活动，也可以上传 FIT、GPX 或 TCX：

```powershell
python sync.py check --source intervals_icu --target garmin
python sync.py sync --source intervals_icu --target garmin --dry-run
python sync.py check --target intervals_icu
python sync.py sync --source igpsport --target intervals_icu --format intervals_icu=fit --dry-run
```

Garmin Connect 国际区也可以作为可选活动来源，使用 `GARMIN_EMAIL` 和 `GARMIN_PASSWORD` 配置的同一登录会话。来源下载原始 ZIP 并读取其中的 FIT 文件：

```powershell
python sync.py check --source garmin --target strava
python sync.py sync --source garmin --target strava --dry-run
```

### 4. Strava 授权

先输出授权 URL:

```powershell
python sync.py strava-auth-url
```

浏览器打开后，同意 `activity:read_all` 和 `activity:write` 权限。回调 URL 里会带一个 `code=...`。

拿到 code 之后执行:

```powershell
python sync.py strava-exchange --code 你的code
```

新的 `access_token` / `refresh_token` 会写进本地 SQLite，不需要每次再手填。

Strava 也可以作为活动来源向 Garmin 同步：

```powershell
python sync.py check --source strava --target garmin
python sync.py sync --source strava --target garmin --dry-run
```

### Wahoo 活动来源和 FIT 上传

Wahoo 可作为活动来源和上传目标。先在 [Wahoo 开发者门户](https://developers.wahooligan.com/cloud) 注册自己的应用，并配置 `WAHOO_CLIENT_ID`、`WAHOO_CLIENT_SECRET` 和与门户一致的 `WAHOO_REDIRECT_URI`；不要使用 APK 中的客户端凭据。Wahoo 限制 Cloud API 的使用，Sandbox 有调用限制，生产环境需要审核。读取活动需要 `workouts_read`，上传需要 `workouts_write`，默认授权请求 `user_read workouts_read workouts_write`，token 会保存在本地 SQLite。

```powershell
python sync.py wahoo-auth-url
python sync.py wahoo-exchange --code 你的code
python sync.py check --source wahoo --target garmin
python sync.py sync --source wahoo --target garmin
python sync.py sync --source local --target wahoo --format wahoo=fit --dry-run
```

完成授权后，从回调地址的查询参数复制 `code`，再运行 `wahoo-exchange` 保存令牌。来源只列出有训练摘要的已完成活动，并下载 Wahoo 提供的 FIT 文件；Wahoo 不会通过 Cloud API 分享由第三方应用产生的已完成训练。只读取活动时可将 `WAHOO_SCOPE` 设为 `user_read workouts_read`；上传则需要 `workouts_write`。上传会轮询 FIT 处理状态。Wahoo 只支持 FIT，因此 `--format wahoo=gpx` 和 `--format wahoo=tcx` 会被拒绝。

### Concept2 Logbook 活动来源

Concept2 Logbook 可作为活动来源。先阅读 [Concept2 Logbook API 文档](https://log.concept2.com/developers/documentation/) 并注册自己的 OAuth 应用，配置 `CONCEPT2_CLIENT_ID`、`CONCEPT2_CLIENT_SECRET` 和已登记的 `CONCEPT2_REDIRECT_URI`，再获取只读授权：

```powershell
python sync.py concept2-auth-url
python sync.py concept2-exchange --code 你的code
python sync.py check --source concept2 --target garmin
python sync.py sync --source concept2 --target garmin --dry-run
```

授权默认只申请 `user:read,results:read`。令牌保存在本地 SQLite；结果列表按日期过滤并自动翻页，FIT 文件从 Logbook 导出。Logbook 没有该结果的划桨数据时，FIT 导出可能返回 404。

Concept2 API 也提供删除结果的操作。删除不可撤销，因此需单独申请写权限，并在命令提示中输入完整结果 ID。Concept2 要求先在开发环境验证写操作，并取得批准后才能向生产环境写入。默认配置会阻止生产环境删除；测试时将 `CONCEPT2_API_ROOT` 设为 `https://log-dev.concept2.com`。只有获得 Concept2 批准后，才可设置 `CONCEPT2_ALLOW_PRODUCTION_WRITES=true`。

```powershell
python sync.py concept2-auth-url --write
python sync.py concept2-exchange --code 你的code --write
python sync.py concept2-delete --activity-id 结果ID
```

### Hammerhead 活动来源与路线目标

Hammerhead 使用官方 [Public API 文档](https://api.hammerhead.io/v1/docs/openapi.yml)。先创建开发者账号、登记自己的应用并接受 [API 许可协议](https://support.hammerhead.io/hc/en-us/article_attachments/42752245991835)，然后配置 `HAMMERHEAD_CLIENT_ID`、`HAMMERHEAD_CLIENT_SECRET` 和登记过的 `HAMMERHEAD_REDIRECT_URI`。项目不包含 APK 中的客户端凭据。当前默认只请求 `activity:read route:read route:write`，令牌保存在本地 SQLite。

```powershell
python sync.py hammerhead-auth-url
python sync.py hammerhead-exchange --code 回调中的code --state 回调中的state
python sync.py check --source hammerhead --target hammerhead
python sync.py sync --source hammerhead --target garmin --dry-run
python sync.py sync --source local --target hammerhead --format hammerhead=gpx
python sync.py hammerhead-routes --limit 20
python sync.py hammerhead-delete-route --route-id 路线ID
```

活动列表按 API 页码读取，活动 FIT 从公开活动文件端点下载。同步到 Hammerhead 时，FIT、GPX 或 TCX 会作为路线文件上传，不会创建 Hammerhead 活动。删除只适用于由本 API 客户端创建的路线，并要求输入完整路线 ID 确认。API 许可协议当前写明不收许可费，同时保留今后收费的权利；若之后开始收费，请勿启用此连接器。

### Ride with GPS 活动来源和目标

Ride with GPS 适配器使用[官方 API](https://ridewithgps.com/api/v1/doc/)，不复用 APK 私有登录接口或其中的客户端凭据。官方 API 条款说明使用 API 不要求付费套餐，但需要 Ride with GPS 账号和 API client。登录 Ride with GPS 后，在开发者设置中创建自己的 API client，登记重定向地址，并将 client ID、client secret 和完全一致的重定向地址填入 `.env`：

```dotenv
RIDEWITHGPS_CLIENT_ID=
RIDEWITHGPS_CLIENT_SECRET=
RIDEWITHGPS_REDIRECT_URI=http://localhost/
```

生成 OAuth 链接，在浏览器授权后，把回调地址中的 `code` 交给 `ridewithgps-exchange`。令牌会先通过当前用户接口验证，再保存到本地 SQLite。已授权后可读取活动 FIT，也可把本地活动以 FIT、GPX 或 TCX 上传；上传后的异步处理会由 CLI 等待并检查结果。Ride with GPS 要求上传活动包含记录时间点，只有规划路线而没有时间戳的文件会被拒绝。

```powershell
python sync.py ridewithgps-auth-url
python sync.py ridewithgps-exchange --code 回调中的code
python sync.py check --source ridewithgps --target ridewithgps
python sync.py sync --source ridewithgps --target strava --dry-run
python sync.py sync --source local --target ridewithgps --format ridewithgps=fit --dry-run
```

`ridewithgps-delete --trip-id ID` 可删除当前账号自己的 Ride with GPS 活动。命令会要求再次输入完整 ID 确认。

### Polar Flow 活动来源

Polar 使用官方 [AccessLink API](https://www.polar.com/accesslink-api/) 读取活动。先用自己的 Polar Flow 账号在 AccessLink 管理页注册应用并接受 API 许可协议，再配置 `POLAR_CLIENT_ID`、`POLAR_CLIENT_SECRET` 和已登记的可选 `POLAR_REDIRECT_URI`。项目不包含 APK 中的凭据；OAuth 用户令牌保存在本地 SQLite。AccessLink 要求授权后先注册用户，`polar-exchange` 会自动完成注册，失败后可运行 `polar-register` 重试。

```powershell
python sync.py polar-auth-url
python sync.py polar-exchange --code 回调中的code --state 回调中的state
python sync.py sync --source polar --target garmin --dry-run
```

Polar 官方 API 目前只返回用户注册本应用之后上传到 Flow、且最近 30 天内的活动。已授权但未同意必要数据权限时，服务端会拒绝请求。访问令牌被撤销后需重新运行授权流程。

### Fitbit 数据来源（Google Health API）

Fitbit AOT 中的 Fitbit Web API 连接需要迁移到 Google Health API。本项目通过 Google 官方 OAuth 客户端库授权，请求活动、位置、睡眠和健康测量只读权限。先在 Google Cloud 启用 Google Health API，创建 Web application OAuth 客户端，把 `https://www.google.com` 加入授权重定向 URI，并允许 `activity_and_fitness.readonly`、`location.readonly`、`sleep.readonly` 与 `health_metrics_and_measurements.readonly` 这些 scope。应用处于 Testing 状态时，也要把自己的 Google 账号加入测试用户。External + Testing 项目的授权和刷新令牌会在 7 天后过期，需要重新授权。

在 `.env` 中设置 `GOOGLE_HEALTH_CLIENT_ID`、`GOOGLE_HEALTH_CLIENT_SECRET` 和与 Google Cloud 完全一致的 `GOOGLE_HEALTH_REDIRECT_URI`。活动列表使用 Google Health 的分页接口，轨迹通过 [`exportExerciseTcx`](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/exportExerciseTcx) 导出 TCX，再转为同步管线使用的 FIT。此导出接口要求同时授予活动和位置权限。缺少 GPS 轨迹的锻炼无法进入当前 FIT 同步流程。OAuth 凭据保存在本地 SQLite，不会写入 APK 或 Git。

```powershell
python sync.py google-health-auth-url
python sync.py google-health-exchange --code 回调中的code --state 回调中的state
python sync.py check --source fitbit --target garmin
python sync.py sync --source fitbit --target garmin --dry-run
python sync.py health fetch-fitbit --dataset sleep --dataset weight --start-date 2026-08-01 --end-date 2026-08-31
python sync.py health fetch-fitbit --dataset daily-summary --start-date 2026-08-01 --end-date 2026-08-31
```

`health fetch-fitbit` 支持重复 `--dataset` 选择 `sleep`、`weight`、`steps`、`heart-rate`、`daily-resting-heart-rate` 或 `daily-summary`，日期范围包含首尾两天。每日摘要使用 Google Health `dailyRollUp` 获取步数、距离、总卡路里和活动能量，并读取每日静息心率；这些汇总按 Google Health 的 first-party 数据源聚合，可能合并 Fitbit 与 Google 来源。Google 的 `active-energy-burned` 不含基础代谢，因此它不等同于 Fitbit Web API 的 `activityCalories`。睡眠阶段、体重及健康指标写入本地健康记录，供健康摘要和活动分析使用。授权刷新令牌只保存在本地 SQLite。Google Health API 使用独立 OAuth 客户端，[旧 Fitbit Web API 令牌不能直接复用](https://developers.google.com/health/migration/data-access)。公开分发受限数据权限需要 Google 应用验证；Google 可能要求 CASA 第三方安全评估，官方列出的费用为 500–4,500 美元，取决于应用复杂度，详见[验证说明](https://developers.google.com/health/app-verification)。本项目只说明个人测试用法，不包含该发布流程。

### Withings 活动来源

Withings 通过 Public API 的 `user.activity` scope 读取训练摘要、GPS 和日内心率，并在同步时生成 FIT 文件。先在 Withings Developer Portal 注册自己的 Public API 应用，在 `.env` 中填写 `WITHINGS_CLIENT_ID`、`WITHINGS_CLIENT_SECRET` 和已登记的 `WITHINGS_REDIRECT_URI`，再把 `withings` 加入 `SYNC_SOURCES`。OAuth 授权方式和该 scope 可访问的活动接口见 [Withings 官方授权说明](https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/oauth-authorization-url/)。

```powershell
python sync.py withings-auth-url
python sync.py withings-exchange --code 回调中的code --state 回调中的state
python sync.py check --source withings --target garmin
python sync.py sync --source withings --target garmin --dry-run
```

Withings 授权码有效期为 30 秒，授权后应立即运行 `withings-exchange`。访问令牌和刷新令牌存入本地 SQLite；刷新时会替换成 Withings 返回的新刷新令牌，详见[官方令牌说明](https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/access-and-refresh-tokens-no-recover/)。Withings 是只读活动来源，不支持向 Withings 上传第三方活动文件。项目不包含 APK 中的账号凭据。

### COROS 活动来源（官方 MCP）

COROS 官方 MCP 为个人开发者提供 OAuth 自助接入，无需 Partner API 申请。它本身免费，COROS 官方 MCP 暴露活动查询和 FIT 下载工具；FIT 文件下载共用每个账号每 24 小时 50 个文件的额度。本连接器只读取活动和 FIT 文件，不调用训练计划或 workout 写入工具。接口和工具名以后可能调整，运行时会读取服务器公布的工具 schema；用户授权令牌和动态 OAuth 客户端信息保存在本地 SQLite。详见 [COROS 官方 MCP 文档](https://github.com/coroslab/COROS-MCP) 和 [自助开发接入说明](https://support.coros.com/hc/en-us/articles/53181619102996-Build-on-COROS-MCP)。

可在 `.env` 中设置 `COROS_MCP_URL` 和接口要求的 `COROS_TIMEZONE`。默认使用自动选择地区的 `https://mcp.coros.com/mcp`；如遇地区重定向问题，可改用 COROS 公布的地区地址。首次使用运行 `coros-auth`，在浏览器授权后把完整回调地址粘贴回终端，再将 `coros` 作为同步来源：

```powershell
python sync.py coros-auth
python sync.py check --source coros --target garmin
python sync.py sync --source coros --target garmin --dry-run
python sync.py sync --source coros --target strava --limit 5
```

每次同步只在有待上传目标时下载对应 FIT。程序会本地限制 FIT 下载请求不超过每 24 小时 50 次；检查连接和列出活动不消耗 FIT 下载额度。COROS MCP 仅作为活动来源，项目不会把运动文件上传回 COROS。

### MyWhoosh 活动来源

MyWhoosh 连接依据 APK 中的登录、活动列表和 FIT 下载接口重新实现。MyWhoosh 没有为这些接口提供公开稳定的 API 文档，因此服务端调整可能导致连接失效。仅在 `.env` 中填写自己的 `MYWHOOSH_USERNAME` 和 `MYWHOOSH_PASSWORD`，并将 `mywhoosh` 加入 `SYNC_SOURCES`；账号密码不会写入 SQLite，登录令牌和本机生成的设备 ID 保存在本地状态库。

```powershell
python sync.py check --source mywhoosh --target garmin
python sync.py sync --source mywhoosh --target strava --dry-run
```

MyWhoosh 在本项目中只作为活动来源。活动 FIT 下载后会验证文件签名，再交给现有同步流程处理。

### 5. 首次同步

```powershell
python sync.py sync
```

默认流程:

```text
iGPSPORT / OneLap -> 下载 FIT -> 修正坐标 -> 上传 Garmin 国际区 -> 上传 Strava
```

Intervals.icu 来源使用 athlete ID 和个人 API key 读取活动列表并下载 FIT，会跳过来源标记为 `STRAVA` 的活动，避免重复导入。

也可以把 Intervals.icu 的每日 wellness 数据导入本地健康记录库，日期范围包含首尾两天：

```powershell
python sync.py health fetch-intervals-wellness --start-date 2026-08-01 --end-date 2026-08-31
```

抓取会保存 APK 支持的健康数值和备注；重复抓取相同记录不会增加重复行。之后可运行 python sync.py health summary 查看。

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

也可以把 GarSync 中可确认的 FIT 轨迹连续性修复用于本地文件。它会删除时间倒退或与前一个保留轨迹点相隔超过 48 小时的记录，并生成独立文件：

```powershell
python sync.py library repair-fit-continuity ride.fit --output ride.repaired.fit
```

没有时间戳的记录会保留。输入文件不变，修复产物旁会保存校验标记，重复处理时会识别已修复文件。

GarSync 的 GPS 卡尔曼滤波也可用于本地 FIT 轨迹：

```powershell
python sync.py library smooth-gps ride.fit --output ride.smoothed.fit
python sync.py library smooth-gps ride.fit --output ride.smoothed.fit --accuracy-m 5
```

滤波使用记录中的 `gps_accuracy`、速度和相邻点估算的航向。缺失精度值会使用文件内有效精度的中位数；文件完全没有精度字段时，需通过 `--accuracy-m` 指定回退值。算法在以首点为中心的米制方位等距投影中计算，避免把米制精度直接用于经纬度角度。输入文件不变，输出旁会保存校验标记，重复处理时会识别已平滑文件。`--q` 默认是 2.5，`--no-adaptive-q` 可关闭按速度和转弯调整 Q。

## 本地活动库与 GarSync 离线功能

`library period` 会从 FIT 的 NP 和正 IF 生成 `ftp_estimate_from_np_if_w`，并汇总成 `ftp_trend`。有效样本少于两条时没有趋势；恰好两条时采用时间较早的估值；其他情况分别取首尾窗口和整个区间的最高值。跨度不足 14 天时窗口使用跨度天数，达到 14 天时使用跨度的 20% 四舍五入，最终限制在 7 至 30 天。该行为依据 APK AOT 静态分析恢复，尚未与 GarSync 运行结果逐项对照。

从本地文件或目录导入活动。目录需要显式指定 `--recursive`；ZIP 会在内存中读取，不会按压缩包路径解压到磁盘。加密 ZIP 可通过 `ACTIVITY_ARCHIVE_PASSWORD` 提供密码。

Huawei Health 历史数据 ZIP 可直接预览或导入。活动库会读取 `Motion path detail data & description` 中的运动记录 JSON，并把其中多条活动分别加入本地库；同一归档里的其他 JSON 不会当作活动导入。

```powershell
python sync.py library preview .\activities.zip
python sync.py library import .\activities.zip
python sync.py library import .\activities --recursive
python sync.py library list --from 2026-01-01 --sport cycling
python sync.py library show <活动ID前缀>
python sync.py library stats
python sync.py library period --from 2026-01-01 --to 2026-03-31 --threshold-hr 180
python sync.py library balance --threshold-hr 180 --resting-hr 60 --from 2026-01-01
python sync.py library vdot --from 2026-01-01 --format json
python sync.py library running-dynamics .\run-sensors.jsonl --height-cm 175
python sync.py library report --format html --output .\activities.html
python sync.py library report --format pdf --output .\activities.pdf
python sync.py library poster <活动ID前缀> --output .\activity.jpg --layout classic --ratio portrait --show-title
python sync.py library poster <活动ID前缀> --output .\activity.jpg --photo .\background.jpg --user 张三 --metric power --power-curve
python sync.py library route <活动ID前缀> --to gpx --output .\route.gpx
python sync.py library map <活动ID前缀> --output .\route-map.html
python sync.py library chart <活动ID前缀> --output .\activity-chart.html
python sync.py library merge .\part-1.fit .\part-2.fit --output .\merged.fit --name "合并骑行"
python sync.py library samba list smb://nas.local/activities --username athlete --password-env SAMBA_PASSWORD
python sync.py library samba import smb://nas.local/activities/ride.fit --username athlete --password-env SAMBA_PASSWORD
python sync.py library samba list smb://nas.local:139/activities --legacy-smb --server-name NAS
python sync.py weather --lat 30.5728 --lon 104.0668
python sync.py weather --lat 30.5728 --lon 104.0668 --format json --refresh
python sync.py sync --source local --target strava --format strava=tcx
```

导入文件保存在 `.data/local_imports/`，索引和汇总写入 `.data/sync_state.db`。本地源只向现有 Garmin / Strava 目标和 Wahoo FIT 目标提供 `FIT`、`GPX`、`TCX`；健康摘要类 JSON 不会被当成可上传运动文件。轨迹 CSV 需要时间戳、纬度和经度列；活动 JSON 接受 `activity`、`laps` 和 `track_points` 等结构。

`library merge` 按输入顺序合并至少两个同运动类型的 FIT 活动。时间重叠的后续片段会平移到前一段结束后一秒，超过两秒的原有停顿会写成休息圈。输出最多保留 50,000 个记录点，抽稀时保留首尾点和可用指标的全局极值。合并会重新生成 FIT，因此来源设备身份、开发者字段和非记录消息不会复制；其他解析损失会随命令结果列出。该行为依据 APK AOT 静态线索实现，尚未用 GarSync 运行时样例逐字段对照。

`library poster` 从本地 FIT、GPX 或 TCX 生成 JPEG 分享海报，显示运动类型、时间、轨迹、距离、用时和一个统计指标；标题可用 `--show-title` 显示。统计指标为累计爬升、平均速度、平均配速或平均功率，默认选择随布局预设变化。`indoor` 布局默认隐藏 GPS 轨迹并显示功率曲线。海报可叠加背景照片、自选水印、字体、文字色和轨迹色。布局使用 APK 中确认的 `classic`、`track_top`、`side_by_side`、`data_below`、`bottom_corner`、`data_above`、`full_info`、`classic_orange` 和 `indoor` 标识；比例为 `portrait`（3:4）或 `square`（1:1）。

`library map` 将本地活动 GPS 轨迹生成可缩放、平移的 HTML 地图，并标出起点和终点。建议在 HTML 所在目录运行 `python -m http.server 8765 --bind 127.0.0.1`，再访问 `http://127.0.0.1:8765/route-map.html`；按 `Ctrl+C` 停止服务。页面使用 OpenStreetMap 在线地图瓦片并显示版权归属，不下载离线地图；浏览地图时，浏览器会向地图服务请求当前视窗的瓦片坐标，活动轨迹数据仍保存在本地 HTML 文件中。

`library chart` 为单次活动导出离线 HTML 时间序列图，自动显示有数据的心率、速度、海拔、功率和踏频。横轴优先使用经过时间，其次使用累计距离，缺少两者时使用采样序号；页面只保存图表数据，不包含 GPS 坐标，也不请求外部资源。

`library samba list` 浏览 SMB 共享中的单层目录，`library samba import` 将指定 FIT、GPX、TCX、JSON、CSV 或 ZIP 文件导入本地活动库。SMB 密码只从 `SAMBA_PASSWORD`（或 `--password-env` 指定的变量）读取；加密 ZIP 密码使用 `ACTIVITY_ARCHIVE_PASSWORD`（或 `--archive-password-env` 指定的变量）。默认后端使用 SMB2/3 直连 TCP。显式添加 `--legacy-smb` 会改用 PySMB，优先协商 SMB2，并在服务器不支持时兼容 SMB1；指定 `:139` 可使用 NetBIOS over TCP，`--server-name` 可覆盖从主机名推导的 NetBIOS 名称。旧协议只在显式选择时启用。两种后端都只浏览和读取，不会修改或删除共享文件。

### 社交邀请链接

GarSync 好友和群组邀请可在分享菜单中生成。CLI 用 `share friend-invite` 和 `share group-invite` 输出对应邀请 URL；可提供显示名称，URL 参数会按 UTF-8 编码。CLI 不会调用手机系统分享面板。

```powershell
python sync.py share friend-invite <用户UUID> --name "显示名称"
python sync.py share group-invite <群组UUID> --group-name "周末骑行" --name "邀请人"
```

### 社交动态与好友

`social feed` 读取 GarSync 的用户、附近、最新、热门和关注动态，接口地址从 `GARSYNC_SOCIAL_BASE_URL` 读取。关注动态默认从 Nakama 好友列表筛选互相关注者，并把这些用户 ID 传给动态接口；重复提供 `--following-id` 可手动覆盖。`social friends` 使用 Nakama REST API 列出好友、发送好友请求或移除好友；`social thumb` 和 `social unthumb` 可点赞或取消点赞。好友列表默认上限为 2000 条，`--cursor` 可继续读取下一页，`--state` 将数字状态筛选原样传给服务器。状态值为 0 mutual、1 outgoingRequest、2 incomingRequest、3 blocked。

Nakama API 地址从 `GARSYNC_NAKAMA_BASE_URL` 读取，会在本地请求 `/v2/friend`。会话令牌默认从 `GARSYNC_NAKAMA_AUTH_TOKEN` 读取，也可用 `--token-env` 指定其他环境变量。不要把令牌写入命令行参数。

```powershell
python sync.py social friends list
python sync.py social friends list --limit 2000 --cursor "<游标>"
python sync.py social friends add <用户ID>
python sync.py social friends remove <用户ID>
python sync.py social feed follow
python sync.py social feed follow --following-id <用户ID> --following-id <另一个用户ID>
python sync.py social thumb <动态ID>
python sync.py social unthumb <动态ID>
```

`social publish <文件>` 按 APK `ActivitySeedHelper` 的发布摘要字段从 FIT、GPX 或 TCX 生成动态并 POST 到 `/publish`。需要显式提供 GarSync 活动 ID 和显示名称；默认标题取文件中的活动名。动态包含可用的路线折线和首个 GPS 坐标，发布前可用 `--dry-run` 检查 JSON。`--location-name` 接受手动地点名称，不调用 APK 使用的第三方逆向地理编码服务，也不移植其中的服务凭据。

```powershell
python sync.py social publish .data/local_imports/ride.fit --activity-id <GarSync活动ID> --display-name "显示名称" --dry-run
python sync.py social publish .data/local_imports/ride.fit --activity-id <GarSync活动ID> --display-name "显示名称" --location-name "上海"
```

实际发布使用 `GARSYNC_SOCIAL_BASE_URL` 和 `GARSYNC_NAKAMA_AUTH_TOKEN`；`--base-url` 和 `--token-env` 可覆盖默认值。发布会发送活动摘要和 GPS 路线，先检查 `--dry-run` 输出再移除该参数。

### BLE 运动传感器

BLE 命令可扫描附近设备、保存设备名称和首选类型、读取标准电量服务，并记录心率、跑步步频、骑行速度/踏频、功率计测量与功率向量、室内单车的标准通知。`heart-rate` 命令输出 CSV；`record` 可将发现的标准测量通知输出到终端或 JSON Lines 文件。CSC 和功率计的轮速传感数据只有在提供轮周长时才会推算速度和距离；设备只提供计数器时仍保留原始计数。普通测量采集只订阅并读取测量数据。BigRun ECG 命令会启动心电带并将原始通知字节以十六进制写入 JSON Lines；`bigrun-ecg-decode` 可将其中的 `0x41` 波形帧解码为 125 Hz 样本，并保留每帧采集时间；`bigrun-ecg-mode` 可设置 `standard`、`hrv` 或 `ecg` 工作模式。波形解码命令可加 --normalize，将整段样本按全局振幅范围映射至 -5 到 5；幅度范围小于 1e-9 时输出零值。该步骤仅处理波形显示尺度，不生成诊断结论。
BLE 命令可扫描附近设备、保存设备名称和首选类型、读取标准电量服务，并记录心率、跑步步频、骑行速度/踏频、功率计测量与功率向量、室内单车的标准通知。`heart-rate` 命令输出 CSV；`record` 可将发现的标准测量通知输出到终端或 JSON Lines 文件。CSC 和功率计的轮速传感数据只有在提供轮周长时才会推算速度和距离；设备只提供计数器时仍保留原始计数。`trainer set-power` 会连接支持 FTMS 的训练台、请求控制权、写入目标功率并等待设备响应。普通测量采集只订阅并读取测量数据。BigRun ECG 命令会启动心电带并将原始通知字节以十六进制写入 JSON Lines；`bigrun-ecg-decode` 可将其中的 `0x41` 波形帧解码为 125 Hz 样本，并保留每帧采集时间；`bigrun-ecg-mode` 可设置 `standard`、`hrv` 或 `ecg` 工作模式。波形解码命令可加 --normalize，将整段样本按全局振幅范围映射至 -5 到 5；幅度范围小于 1e-9 时输出零值。该步骤仅处理波形显示尺度，不生成诊断结论。

```powershell
python sync.py ble guide
python sync.py ble guide --locale en
python sync.py ble scan --save
python sync.py ble devices
python sync.py ble battery <设备地址>
python sync.py ble heart-rate <设备地址> --duration 3600 --output .\heart-rate.csv
python sync.py ble record <设备地址> --duration 3600 --wheel-circumference-m 2.105 --output .\sensor-data.jsonl
python sync.py ble bigrun-ecg <设备地址> --duration 300 --output .\bigrun-ecg.jsonl
python sync.py ble bigrun-ecg-decode .\bigrun-ecg.jsonl --output .\bigrun-ecg-samples.json
python sync.py ble bigrun-ecg-analyze .\bigrun-ecg-samples.json --output .\bigrun-ecg-report.json
python sync.py ble bigrun-ecg-decode .\bigrun-ecg.jsonl --normalize --output .\bigrun-ecg-normalized.json
python sync.py ble bigrun-ecg-mode <设备地址> hrv
python sync.py ble trainer set-power <训练台地址> --watts 180
python sync.py ble trainer set-resistance <训练台地址>
python sync.py ble trainer preview --ftp 250
python sync.py ble trainer preview .\course.json --ftp 250 --intensity-percent 105
python sync.py ble trainer ride <训练台地址> .\course.json --ftp 250
python sync.py ble rename <设备地址> "胸带"
python sync.py ble prefer <设备地址> --type heart_rate
python sync.py ble remove <设备地址>
```

`ble guide` 说明蓝牙传感器所需的系统访问权限，以及不使用传感器时如何跳过。它不会扫描设备、打开系统设置或触发 Android 授权弹窗；实际权限由运行 CLI 的操作系统管理。

`trainer preview` 不连接蓝牙，可预览内置示例课程或指定课程文件。课程 JSON 使用顶层 `name` 和 `segments`；每段包含 `start_time_s`、`end_time_s`、`start_power_w`、`end_power_w`，`label` 可选。也可直接把 AI 生成骑行训练对应的 `.fit.meta` 文件作为课程输入。使用 `%FTP` 或缺省功率目标时要提供 `--ftp`。`trainer ride` 连接支持 FTMS 的训练台并生成 FIT 活动，默认保存到 `.data/virtual_rides/`；设备提供标准 Indoor Bike Data 时记录功率、速度、距离、心率和踏频，否则仍保存计时数据。交互终端支持 `+`/`-` 每次调整 5% 强度、`s` 跳过当前间歇、`p` 暂停或继续，以及 `e` 在 ERG 目标功率和 0% 目标阻力模式间切换。暂停时目标功率设为 0 W，暂停时长计入 FIT 经过时间，但不计入计时器时间。

`trainer set-resistance` 把标准 FTMS 目标阻力设为 0.0；交互课表按 `e` 切换到该模式，再切回目标功率控制。APK 静态导出的虚拟骑行字节与 FTMS 标准操作码不一致，因此本项目使用标准的目标阻力和目标功率命令，不复刻无法确认语义的字节序列。

`bigrun-ecg-analyze` 读取 `bigrun-ecg-decode` 生成的 JSON，要求采样率为 100–2000 Hz 且至少有 5 秒样本。它复现 APK 中的 100 ms 移动平均、0.5 Hz 高通、12 Hz 低通、R 峰检测和 R-R 间期计算，并按低于 60 bpm、高于 100 bpm 输出心率阈值状态。该状态只复现设备阈值提示，不是疾病分类；报告包含非医疗声明。

已保存设备信息位于 `.data/ble_devices.json`。BLE 扫描和连接需要本机蓝牙适配器及操作系统授予的权限；命令必须运行在能够访问该适配器的环境中。WSL 的 NAT 代理警告只说明代理配置未传入 WSL，不能据此判断 BLE 是否可用。

`weather` 按指定的十进制度坐标读取当前天气、可用时的 AQI 和城市名称，并按 AOT 中恢复的阈值生成户外运动建议。结果缓存在 `.data/weather_cache.json` 15 分钟；`--refresh` 可强制更新。坐标会发送到 APK 中发现的 `api.unicgames.com` 天气接口。请求需要在本机 `.env` 设置 `GARSYNC_WEATHER_TOKEN`；示例配置不包含令牌。天气接口属于 GarSync 服务端接口，服务策略或响应格式变化时此功能可能失效。

`library balance` 优先使用 FIT 活动中的 TSS。没有 TSS 时，只有提供 `--threshold-hr` 且存在平均心率和活动时长，才按 GarSync 的 HR-TSS 公式估算。默认静息心率为 60 bpm。每日负荷按 42 天 CTL 和 7 天 ATL 指数平滑，TSB 为 CTL 减 ATL。指定 `--from` 时仍会用此前活动预热负荷，但只输出所选日期范围；默认输出 JSON，也支持 CSV 和 TXT。该算法来自 AOT 静态伪代码并已与 Blutter ARM64 汇编交叉核对。

`library vdot` 只读取本地跑步活动，按天取最高 VDOT 形成趋势，并计算最新趋势点和历史最佳的 1 英里、3K、5K、10K、半马、全马等效成绩，以及 Easy、Marathon、Threshold、Interval、Repetition 配速范围。GarSync 的活动筛选门槛是距离超过 200 米、计时超过 60 秒；命令默认使用全部本地历史，也可通过 `--from`、`--to` 限定日期。筛选门槛、五档配速系数和 50 次二分反算结构来自 APK AOT 伪代码/汇编；VDOT 方程的截距恢复存在差异。APK 没有运行时对照，本地结果尚未和 GarSync 页面逐项比对。

`library swim-css` 用 200 米和 400 米计时计算 CSS 每 100 米配速，并按 25 米或 50 米泳池长度输出目标分段。时间接受秒数、`M:SS` 或 `H:MM:SS`，默认输出 JSON：

```powershell
python sync.py library swim-css --time-200m 2:40 --time-400m 5:40 --pool-length 25
```

此命令计算显式提供的两项测试成绩；它还没有从本地历史游泳活动自动寻找最佳成绩或生成趋势曲线。

`library swim-rest` 按间歇距离计算默认休息秒数，支持 JSON 和 TXT：

```powershell
python sync.py library swim-rest 100
```

固定距离和其他距离的计算分别按 GarSync CSS 计算器 AOT 汇编确认的分支及 7.5% 距离比例实现；GarSync 运行时结果尚未逐项对照。

`library running-dynamics` 读取按时间排序的 JSON Lines 事件。加速度事件使用 `type=accelerometer`、`timestamp_ms`、`x_mps2`、`y_mps2`、`z_mps2`；GPS 分段事件使用 `type=gps_segment`、`timestamp_ms`、`distance_m`、`step_delta`、`horizontal_accuracy_m`、`speed_mps`。时间戳为毫秒，GPS 距离和步数是相邻定位点之间的增量。输出包括步频、步幅、总步数、垂直振幅和垂直步幅比；身高默认 175 厘米，用于初始化步幅。计算公式按 APK AOT 的 Blutter ARM64 反汇编恢复，尚未用 GarSync 运行时传感器记录逐项对照。

`library period` 默认汇总截至今天的近 90 天，也可指定日期范围和运动类型。报告提供周期总距离/时长/TSS、周一开周的周切片、跑步 VDOT 起止与最高值、活动日志，以及 APK 周期总结中可恢复的最长距离、最高 TSS、最快配速、最高 NP、最高爬升、最长时长和最高均速亮点。`avg_norm_power_w` 是各活动正值 NP 的等权平均，`avg_norm_power_activity_count` 表示参与平均的活动数；无有效值时平均值为空。`avg_cadence` 对已识别的跑步和骑行活动，按活动等权平均非空的活动踏频摘要，`avg_cadence_activity_count` 是参与活动数；零值计入，空值和其他运动类型不计入。GarSync 按跑步步频（步/分钟）和骑行踏频（转/分钟）分别取活动字段；本地优先使用 FIT session 的 `avg_cadence`，没有活动级值时才从轨迹记录求均值，旧摘要也会从 `average_cadence_rpm` 读取。运动细分类型映射尚未完全覆盖。`recorded_zone_time_s` 按区间编号累加本地 FIT `time_in_zone` 中已有的心率、速度、踏频和功率秒数，不从轨迹采样点重算；无原始区间数据时对应结果为空。`power_curve_w` 从原始 FIT、GPX、TCX 轨迹点按 AOT 恢复的 10 秒至 6 小时窗口计算均功率，并跨活动保留每个时长的最大整数瓦数。缺少可读取轨迹文件的活动计入 `power_curve_unavailable_activity_count`；有效文件没有足够时长或功率样本时，该活动不会生成曲线值。FIT 内已有的 TSS 优先；未带 TSS 时，提供 `--threshold-hr` 才按 HR-TSS 公式估算。基于 FIT 的 time_in_zone 时长重建 training_type_distribution：Z2–Z5 中时长最多的分区映射为 easy、tempo、threshold、interval；分区数据未覆盖 Z5 时，running、walking、hiking 活动按 5K、10K、半马或全马距离的相对误差严格小于 3% 时识别 race，其余为 mixed。intensity_model 按 easy、interval 时长比例和 0.4、0.6、0.14 阈值识别 pyramidal、polarized 或 mixed。规则依据 APK AOT 静态恢复，尚未与 GarSync 运行结果逐项对照；新增的 `sampled_zone_time_s` 从活动文件读取轨迹样本，以 FIT `time_in_zone` 中一致递增的心率/速度高边界重算周期分区；缺少阈值或阈值冲突时不生成结果，使用相邻采样线性插值，超过 30 秒的采样间隔跳过。参与重算的活动数记录在 `sampled_zone_activity_count`；插值和缺样处理尚未与 GarSync 运行结果对照。个人纪录变化见 `pr_changes`。它使用 APK 可确认的距离档位、正负 3% 候选范围、时长比较和 PRChange 输出字段。本地以区间开始日前的最快成绩为历史基准，只报告区间内更快的成绩，首次记录的旧成绩和提升率为空。赛事距离数值按标准米制距离换算，AOT ConstMap 中的精确数值尚未恢复。APK 的历史基准来源未完全恢复，当前基准范围按本地完整活动库适配，日期按 UTC 日界线分组。NP 与踏频平均算法已从 AOT 汇编确认；功率曲线尚未与 GarSync 运行输出逐项比对。

AI 活动分析还会读取 FIT session 中的平均/最大功率、标准化功率、强度因子、有氧/无氧训练效果和 TSS，并将这些值放进活动报告与提示词。FIT `time_in_zone` 消息中的心率、速度、踏频、功率分区用时及其边界和计算参数也会保留在本地活动摘要；各分区结果和 GarSync 主分类会分别提供给 AI。主分类器依据恢复到的 AOT 规则选择心率、功率或速度结果，并处理骑行强度因子。速度区间从活动轨迹和活动前乳酸阈速度推导，采用五点中值、线性插值和 30 秒最大采样间隔。速度分支的 AOT 条件比较乳酸阈速度数值与活动计时秒数，存在跨单位比较；移植保留了该条件，具体字段语义仍不确定。静态恢复的区间时间计算和分类选择尚未与 GarSync 运行时样例逐项校验。GPX、TCX 不包含这些 FIT 活动级汇总，导出时会列明损失；合并 FIT 时也会报告分区数据未复制。

训练计划和课表命令从 `sport_sync_bridge/data/training_plans/` 与 `sport_sync_bridge/data/workouts/` 读取本地模板。模板文件保留在本机并由 Git 忽略；其他副本需自行放入模板文件。计划开始日期必须是周一，安装后可导出到日历：

```powershell
python sync.py plans list --locale zh
python sync.py plans show 8w_beginner_run --locale zh
python sync.py plans install 8w_beginner_run --locale zh --start-date 2026-10-05
python sync.py plans installed
python sync.py plans schedule <计划ID前缀>
python sync.py plans link-activity <计划ID前缀> <计划项ID> <活动ID前缀>
python sync.py plans unlink-activity <计划ID前缀> <计划项ID>
python sync.py plans progress <计划ID前缀>
python sync.py plans progress <计划ID前缀> --format json
python sync.py plans export <计划ID前缀> --output .\training-plan.ics
python sync.py workouts list --sport CYCLING
python sync.py workouts show <课表ID>
python sync.py workouts export <课表ID> --output .\workout.fit
python sync.py workouts generate --sport running --task "节奏跑，间歇后放松" --target-mode pace --target-duration 45min --prompt-only
```

`plans schedule` 会列出已安装计划的日期、课表项 ID 和可解析的距离、时长、配速及 TSS 目标。用 `link-activity` 将本地活动关联到一个训练项，`unlink-activity` 可解除关联；同一计划内一项活动只能关联到一个训练项。`plans progress` 对照已关联活动与可读取的目标指标，并报告实际值和差值；`--format json` 输出机器可读结果。

`workouts generate` 用同一个 AI 接口按跑步、骑行或游泳请求生成一个结构化课表，并写成 Garmin FIT workout 文件。默认文件和可浏览的 JSON 元数据保存在 `.data/generated_workouts/`；`workouts list/show/export` 也会读取这些生成文件。可用 `--target-mode`、目标时长/距离/配速/心率/TSS、`--athlete-context` 和重复的 `--feedback` 提供训练要求。`--prompt-only` 只打印 system/user 提示词；FIT 无法直接编码的目标会保留在步骤备注和元数据中，重复组使用 FIT repeat 控制步骤。

健康指标可从 UTF-8 CSV 导入，支持 `date,metric,value,unit` 长表格式及带日期列的宽表。指标包括体重、身高、静息心率、HRV、血氧、睡眠、步数、压力、身体电量、血压，以及 AOT 中确认的跑步/骑行 VO₂max、睡眠分数、阈值心率/速度、卡路里、楼层、呼吸率、饮水量、恢复时长、HRV 状态、训练准备状态和完全恢复状态；体重与身高齐全时会计算 BMI。汇总默认输出 JSON，也可用文本格式查看指标名称和单位；乳酸阈值速度会同时显示每公里配速。数值指标可用于本地汇总和 AI 活动分析；状态字段只保存 CSV 提供的标签，不计算设备侧准备度或恢复算法。

```powershell
python sync.py health import .\health.csv
python sync.py health summary
python sync.py health summary --format text
python sync.py health import-readiness .\training-readiness.json
python sync.py health fetch-readiness --start-date 2026-08-01 --end-date 2026-08-07
python sync.py health fetch-garmin-summary --start-date 2026-08-01 --end-date 2026-08-07
python sync.py health summaries --start-date 2026-08-01 --end-date 2026-08-07
python sync.py health fetch-garmin-details --dataset sleep --dataset hrv --dataset hydration --start-date 2026-08-01 --end-date 2026-08-07
python sync.py health details --dataset sleep --start-date 2026-08-01 --end-date 2026-08-07
python sync.py health readiness --format text --limit 7
python sync.py health readiness --format json
```

`health import-readiness` 接受一个训练准备度 JSON 对象或对象数组，按日期保存在本地 SQLite；重复导入相同记录不会重复写入。记录保留 AOT 模型中的评分、恢复时间、ACWR、急性负荷、压力、HRV、睡眠因子，以及完整的 `inputContext`、`metadata` 和其他字段。该命令展示文件提供的分数，不自行计算设备侧准备度；`--format json` 可查看完整原始字段。

`health fetch-readiness` 按包含首尾的日期范围逐日从 Garmin Connect 获取训练准备度记录，并写入同一 SQLite 历史，需要已配置 Garmin 登录信息。分数和因子直接保存服务器返回的数据，不重新计算设备侧评分。

`ai-analysis` 会把活动开始前最近一条训练准备度记录及其恢复时间、睡眠、HRV 和负荷因子加入提示词；记录时间必须严格早于活动开始，之后的数据和设备元数据不会发送。

`health fetch-garmin-summary` 按包含首尾的日期范围读取 Garmin Connect 每日汇总。原始 JSON 按日期保存在本地数据库，可用 `health summaries` 查看；步数、楼层、心率、HRV、睡眠、血氧、压力、身体电量、距离、卡路里和呼吸等已识别指标也会进入健康历史及 AI 活动分析上下文。每日汇总指标按对应日期的 UTC 日末记时，不会被当作同日活动开始前的测量值。命令需要已配置 Garmin 登录信息。

`health fetch-garmin-details` 可重复指定 `--dataset`，支持睡眠、HRV、压力、身体电量、呼吸、饮水、血压、心率、健身年龄、血氧适应、楼层图表和步数。原始返回值按日期和数据集保存在本地 SQLite，可用 `health details` 查看；识别到的睡眠时长与阶段、HRV、压力、呼吸、饮水、血压、心率、健身年龄、血氧均值、楼层和步数会进入健康历史及 AI 活动分析上下文。身体电量事件、血氧小时序列和楼层图表保留原始返回值。日期范围最多 366 天，命令需要已配置 Garmin 登录信息。

AI 运动分析保留为可选功能。它把单次活动摘要、最近活动的周汇总和本地健康指标发送给 OpenAI 兼容的 Chat Completions 接口，不发送 GPS 坐标。可先检查提示词，再配置自己使用的远端或本地模型：

```powershell
python sync.py ai-analysis <活动ID前缀> --prompt-only
python sync.py ai-analysis <活动ID前缀> --prompt-only --language en-US --focus recovery --detail detailed
python sync.py ai-analysis <骑行活动ID前缀> --force-vector-json .\force-vector.json --force-vector-focus stability --prompt-only
python sync.py ai-settings show
python sync.py ai-settings set --focus recovery --detail brief
python sync.py ai-settings reset
python sync.py ai-profile show
python sync.py ai-profile set --gender female --age 32 --weight-kg 58.5 --height-cm 165 --resting-hr-bpm 52 --max-hr-bpm 190 --lactate-threshold-hr-bpm 172 --vo2-max-run 48 --vo2-max-bike 52 --ftp-w 210 --threshold-pace-s-per-km 300
python sync.py ai-profile set --clear ftp_w --clear vo2_max_bike
python sync.py ai-profile reset
```

`--language` 接受语言代码，默认 `zh-CN`；`--focus` 可选 `performance`、`health` 或 `recovery`，`--detail` 可选 `brief`、`normal` 或 `detailed`。两项默认读取本地 SQLite 中保存的偏好，初始值分别为 `performance` 和 `normal`；命令行显式参数只覆盖本次分析。使用 `ai-settings show|set|reset` 管理偏好。提示词会要求模型准确引用已有数值，并避免医疗诊断。健康指标按活动时间筛选：活动开始前各指标最近一次记录，以及活动结束后至结束日 UTC 日末的记录；不把活动之后其他日期的数据带入历史活动分析，所有指标都保留时间戳。

`ai-profile show|set|reset` 管理本地运动员档案，支持性别、年龄、身高体重、静息/最大/乳酸阈值心率、跑步和骑行 VO₂max、FTP 与阈值配速。`set --clear <字段>` 可逐项清除，字段名使用 JSON 中的 snake_case。档案仅写入本地 SQLite；活动 AI 分析只会在提示词中加入已填写字段，未设置的字段会省略。数值字段会校验类型与合理范围。

`ai-analysis --force-vector-json` 使用 GarSync 骑行 AI 教练的功率矢量提示词，并复用当前模型配置、活动分析历史和 Markdown 结果保存。输入使用本项目约定的 JSON 结构，最多包含 12 个左/右脚 30° 节点数组，以及左右脚的力矩有效性（TE）和踩踏平顺度（PS）百分比；缺少字段会留空，不从缺失测量推算。节点数值单位由来源设备决定，项目不擅自换算。`--force-vector-focus` 支持 `comprehensive`、`stability` 和 `peak_power`；`--detail`、`--language` 与 `--question` 继续生效。`--prompt-only` 可在不请求 AI 服务的情况下查看发送内容。

```json
{
  "left_foot_nodes": [12.0, 15.5, 20.0],
  "right_foot_nodes": [11.0, 14.0, 19.5],
  "left_torque_effectiveness_percent": 75.2,
  "right_torque_effectiveness_percent": 70.4,
  "left_pedal_smoothness_percent": 22.1,
  "right_pedal_smoothness_percent": 19.8
}
```

在 `.env` 中设置 `AI_API_BASE_URL`、`AI_MODEL`，远端服务需要时再设置 `AI_API_KEY`。请求成功后会在本地 SQLite 保存模型名和分析正文，并在数据目录的 `ai_analysis/<活动指纹>/<结果ID>.md` 保存 Markdown 副本；可用 `python sync.py ai-analysis <活动ID前缀> --history` 查看数据库历史。项目不会附带 GarSync 的服务凭据或计费代码。

`ai-report-export` 可从本地缓存导出 Markdown 或 PDF，不会再次请求 AI。省略 `--result-id` 时导出该活动最新的分析；指定完整 ID 或唯一前缀可选择历史结果。`--output` 可指定目标文件。

```powershell
python sync.py ai-report-export <活动ID前缀> --format markdown
python sync.py ai-report-export <活动ID前缀> --format pdf --result-id <结果ID>
```

Wi-Fi 文件导入页默认只监听本机。要让手机从同一局域网访问，显式绑定局域网接口：

```powershell
python sync.py receive --host 0.0.0.0 --port 8765
```

接收页不设访问口令，只应在可信的本地网络中临时开启。

未移植到 Python CLI 的 APK 功能包括其余云平台的私有认证/同步协议、手机 BLE 配对引导界面、设备侧训练准备度评分算法（JSON 快照可导入和查看，通用 HR-TSS/CTL/ATL/TSB 训练负荷指标已实现）及其他未确认的在线健康接口。GarSync 的周期 AI 教练界面受 Pro 权限限制，AI 周计划生成会扣除 gems，按当前范围未接入；单次 AI 课表生成和 AI 活动分析保留为本项目可配置模型功能。标准 FTMS 目标功率控制已实现；BLE 测量通知也支持心率、跑步步频、骑行速度/踏频、功率计测量与功率向量、室内单车数据。BigRun ECG 已支持原始通知采集、波形解码、工作模式切换、波形归一化、非诊断性 R 峰、R-R 间期和心率指标计算，以及按 APK AOT 规则恢复的模式分类标记（normal、tachycardia、bradycardia、atrialFibrillation、pac、pvc、vt、myocardialIschemia、myocardialInfarction）。分类结果仅供运动分析参考，不可替代专业医疗诊断。Samba 默认使用 SMB2/3；旧协议需要显式添加 `--legacy-smb`，导入始终只读。Garmin 每日健康汇总及睡眠、HRV、压力、身体电量、呼吸、饮水、血压、心率、健身年龄、血氧适应、楼层图表和步数详情可以从 Garmin Connect 读取；健康 CSV 仍可手动导入。当前天气功能需要显式坐标和 GarSync 天气服务令牌；AI 活动分析使用本地汇总与可配置模型接口，单次 AI 课表生成只发送命令提供的训练要求。静态 AOT 索引不足以确认其他云端接口的运行期请求、服务端校验或设备交互行为。

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
- `https://cloud-api.wahooligan.com/`
- `https://developers.strava.com/docs/authentication/`
- `https://developers.strava.com/docs/uploads/`
