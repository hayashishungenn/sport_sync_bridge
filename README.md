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
python sync.py library preview .\activities.zip
python sync.py library import .\activities.zip
python sync.py library import .\activities --recursive
python sync.py library list --from 2026-01-01 --sport cycling
python sync.py library show <活动ID前缀>
python sync.py library stats
python sync.py library period --from 2026-01-01 --to 2026-03-31 --threshold-hr 180
python sync.py library balance --threshold-hr 180 --resting-hr 60 --from 2026-01-01
python sync.py library vdot --from 2026-01-01 --format json
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

导入文件保存在 `.data/local_imports/`，索引和汇总写入 `.data/sync_state.db`。本地源只向现有 Garmin / Strava 目标提供 `FIT`、`GPX`、`TCX`；健康摘要类 JSON 不会被当成可上传运动文件。轨迹 CSV 需要时间戳、纬度和经度列；活动 JSON 接受 `activity`、`laps` 和 `track_points` 等结构。

`library merge` 按输入顺序合并至少两个同运动类型的 FIT 活动。时间重叠的后续片段会平移到前一段结束后一秒，超过两秒的原有停顿会写成休息圈。输出最多保留 50,000 个记录点，抽稀时保留首尾点和可用指标的全局极值。合并会重新生成 FIT，因此来源设备身份、开发者字段和非记录消息不会复制；其他解析损失会随命令结果列出。该行为依据 APK AOT 静态线索实现，尚未用 GarSync 运行时样例逐字段对照。

`library poster` 从本地 FIT、GPX 或 TCX 生成 JPEG 分享海报，显示运动类型、时间、轨迹、距离、用时和一个统计指标；标题可用 `--show-title` 显示。统计指标为累计爬升、平均速度、平均配速或平均功率，默认选择随布局预设变化。`indoor` 布局默认隐藏 GPS 轨迹并显示功率曲线。海报可叠加背景照片、自选水印、字体、文字色和轨迹色。布局使用 APK 中确认的 `classic`、`track_top`、`side_by_side`、`data_below`、`bottom_corner`、`data_above`、`full_info`、`classic_orange` 和 `indoor` 标识；比例为 `portrait`（3:4）或 `square`（1:1）。

`library map` 将本地活动 GPS 轨迹生成可缩放、平移的 HTML 地图，并标出起点和终点。建议在 HTML 所在目录运行 `python -m http.server 8765 --bind 127.0.0.1`，再访问 `http://127.0.0.1:8765/route-map.html`；按 `Ctrl+C` 停止服务。页面使用 OpenStreetMap 在线地图瓦片并显示版权归属，不下载离线地图；浏览地图时，浏览器会向地图服务请求当前视窗的瓦片坐标，活动轨迹数据仍保存在本地 HTML 文件中。

`library chart` 为单次活动导出离线 HTML 时间序列图，自动显示有数据的心率、速度、海拔、功率和踏频。横轴优先使用经过时间，其次使用累计距离，缺少两者时使用采样序号；页面只保存图表数据，不包含 GPS 坐标，也不请求外部资源。

`library samba list` 浏览 SMB 共享中的单层目录，`library samba import` 将指定 FIT、GPX、TCX、JSON、CSV 或 ZIP 文件导入本地活动库。SMB 密码只从 `SAMBA_PASSWORD`（或 `--password-env` 指定的变量）读取；加密 ZIP 密码使用 `ACTIVITY_ARCHIVE_PASSWORD`（或 `--archive-password-env` 指定的变量）。默认后端使用 SMB2/3 直连 TCP。显式添加 `--legacy-smb` 会改用 PySMB，优先协商 SMB2，并在服务器不支持时兼容 SMB1；指定 `:139` 可使用 NetBIOS over TCP，`--server-name` 可覆盖从主机名推导的 NetBIOS 名称。旧协议只在显式选择时启用。两种后端都只浏览和读取，不会修改或删除共享文件。

### BLE 运动传感器

BLE 命令可扫描附近设备、保存设备名称和首选类型、读取标准电量服务，并记录心率、跑步步频、骑行速度/踏频、功率计测量与功率向量、室内单车的标准通知。`heart-rate` 命令输出 CSV；`record` 可将发现的标准测量通知输出到终端或 JSON Lines 文件。CSC 和功率计的轮速传感数据只有在提供轮周长时才会推算速度和距离；设备只提供计数器时仍保留原始计数。普通测量采集只订阅并读取测量数据。BigRun ECG 命令会启动心电带并将原始通知字节以十六进制写入 JSON Lines；`bigrun-ecg-decode` 可将其中的 `0x41` 波形帧解码为 125 Hz 样本，并保留每帧采集时间；`bigrun-ecg-mode` 可设置 `standard`、`hrv` 或 `ecg` 工作模式。波形解码命令可加 --normalize，将整段样本按全局振幅范围映射至 -5 到 5；幅度范围小于 1e-9 时输出零值。该步骤仅处理波形显示尺度，不生成诊断结论。
BLE 命令可扫描附近设备、保存设备名称和首选类型、读取标准电量服务，并记录心率、跑步步频、骑行速度/踏频、功率计测量与功率向量、室内单车的标准通知。`heart-rate` 命令输出 CSV；`record` 可将发现的标准测量通知输出到终端或 JSON Lines 文件。CSC 和功率计的轮速传感数据只有在提供轮周长时才会推算速度和距离；设备只提供计数器时仍保留原始计数。`trainer set-power` 会连接支持 FTMS 的训练台、请求控制权、写入目标功率并等待设备响应。普通测量采集只订阅并读取测量数据。BigRun ECG 命令会启动心电带并将原始通知字节以十六进制写入 JSON Lines；`bigrun-ecg-decode` 可将其中的 `0x41` 波形帧解码为 125 Hz 样本，并保留每帧采集时间；`bigrun-ecg-mode` 可设置 `standard`、`hrv` 或 `ecg` 工作模式。波形解码命令可加 --normalize，将整段样本按全局振幅范围映射至 -5 到 5；幅度范围小于 1e-9 时输出零值。该步骤仅处理波形显示尺度，不生成诊断结论。

```powershell
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
python sync.py ble rename <设备地址> "胸带"
python sync.py ble prefer <设备地址> --type heart_rate
python sync.py ble remove <设备地址>
```

`bigrun-ecg-analyze` 读取 `bigrun-ecg-decode` 生成的 JSON，要求采样率为 100–2000 Hz 且至少有 5 秒样本。它复现 APK 中的 100 ms 移动平均、0.5 Hz 高通、12 Hz 低通、R 峰检测和 R-R 间期计算，只输出心率与间期指标，不给出疾病分类；报告包含非医疗声明。

已保存设备信息位于 `.data/ble_devices.json`。BLE 扫描和连接需要本机蓝牙适配器及操作系统授予的权限；命令必须运行在能够访问该适配器的环境中。WSL 的 NAT 代理警告只说明代理配置未传入 WSL，不能据此判断 BLE 是否可用。

`weather` 按指定的十进制度坐标读取当前天气、可用时的 AQI 和城市名称，并按 AOT 中恢复的阈值生成户外运动建议。结果缓存在 `.data/weather_cache.json` 15 分钟；`--refresh` 可强制更新。坐标会发送到 APK 中发现的 `api.unicgames.com` 天气接口。请求需要在本机 `.env` 设置 `GARSYNC_WEATHER_TOKEN`；示例配置不包含令牌。天气接口属于 GarSync 服务端接口，服务策略或响应格式变化时此功能可能失效。

`library balance` 优先使用 FIT 活动中的 TSS。没有 TSS 时，只有提供 `--threshold-hr` 且存在平均心率和活动时长，才按 GarSync 的 HR-TSS 公式估算。默认静息心率为 60 bpm。每日负荷按 42 天 CTL 和 7 天 ATL 指数平滑，TSB 为 CTL 减 ATL。指定 `--from` 时仍会用此前活动预热负荷，但只输出所选日期范围；默认输出 JSON，也支持 CSV 和 TXT。该算法来自 AOT 静态伪代码并已与 Blutter ARM64 汇编交叉核对。

`library vdot` 只读取本地跑步活动，按天取最高 VDOT 形成趋势，并计算最新趋势点和历史最佳的 1 英里、3K、5K、10K、半马、全马等效成绩，以及 Easy、Marathon、Threshold、Interval、Repetition 配速范围。GarSync 的活动筛选门槛是距离超过 200 米、计时超过 60 秒；命令默认使用全部本地历史，也可通过 `--from`、`--to` 限定日期。筛选门槛、五档配速系数和 50 次二分反算结构来自 APK AOT 伪代码/汇编；VDOT 方程的截距恢复存在差异。APK 没有运行时对照，本地结果尚未和 GarSync 页面逐项比对。

`library period` 默认汇总截至今天的近 90 天，也可指定日期范围和运动类型。报告提供周期总距离/时长/TSS、周一开周的周切片、跑步 VDOT 起止与最高值、活动日志，以及 APK 周期总结中可恢复的最长距离、最高 TSS、最快配速、最高 NP、最高爬升、最长时长和最高均速亮点。`avg_norm_power_w` 是各活动正值 NP 的等权平均，`avg_norm_power_activity_count` 表示参与平均的活动数；无有效值时平均值为空。`avg_cadence` 对已识别的跑步和骑行活动，按活动等权平均非空的活动踏频摘要，`avg_cadence_activity_count` 是参与活动数；零值计入，空值和其他运动类型不计入。GarSync 按跑步步频（步/分钟）和骑行踏频（转/分钟）分别取活动字段，本地 FIT 摘要目前根据轨迹 `cadence` 记录求均值，且运动细分类型映射尚未完全覆盖。`recorded_zone_time_s` 按区间编号累加本地 FIT `time_in_zone` 中已有的心率、速度、踏频和功率秒数，不从轨迹采样点重算；无原始区间数据时对应结果为空。`power_curve_w` 从原始 FIT、GPX、TCX 轨迹点按 AOT 恢复的 10 秒至 6 小时窗口计算均功率，并跨活动保留每个时长的最大整数瓦数。缺少可读取轨迹文件的活动计入 `power_curve_unavailable_activity_count`；有效文件没有足够时长或功率样本时，该活动不会生成曲线值。FIT 内已有的 TSS 优先；未带 TSS 时，提供 `--threshold-hr` 才按 HR-TSS 公式估算。训练类型分布和轨迹重算的区间分布仍在分析中；个人纪录变化见 `pr_changes`。它使用 APK 可确认的距离档位、正负 3% 候选范围、时长比较和 PRChange 输出字段。本地以区间开始日前的最快成绩为历史基准，只报告区间内更快的成绩，首次记录的旧成绩和提升率为空。赛事距离数值按标准米制距离换算，AOT ConstMap 中的精确数值尚未恢复。APK 的历史基准来源未完全恢复，当前基准范围按本地完整活动库适配，日期按 UTC 日界线分组。NP 与踏频平均算法已从 AOT 汇编确认；功率曲线尚未与 GarSync 运行输出逐项比对。

AI 活动分析还会读取 FIT session 中的平均/最大功率、标准化功率、强度因子、有氧/无氧训练效果和 TSS，并将这些值放进活动报告与提示词。FIT `time_in_zone` 消息中的心率、速度、踏频、功率分区用时及其边界和计算参数也会保留在本地活动摘要；各分区结果和 GarSync 主分类会分别提供给 AI。主分类器依据恢复到的 AOT 规则选择心率、功率或速度结果，并处理骑行强度因子。速度区间从活动轨迹和活动前乳酸阈速度推导，采用五点中值、线性插值和 30 秒最大采样间隔。速度分支的 AOT 条件比较乳酸阈速度数值与活动计时秒数，存在跨单位比较；移植保留了该条件，具体字段语义仍不确定。静态恢复的区间时间计算和分类选择尚未与 GarSync 运行时样例逐项校验。GPX、TCX 不包含这些 FIT 活动级汇总，导出时会列明损失；合并 FIT 时也会报告分区数据未复制。

训练计划和课表命令从 `sport_sync_bridge/data/training_plans/` 与 `sport_sync_bridge/data/workouts/` 读取本地模板。模板文件保留在本机并由 Git 忽略；其他副本需自行放入模板文件。计划开始日期必须是周一，安装后可导出到日历：

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

健康指标可从 UTF-8 CSV 导入，支持 `date,metric,value,unit` 长表格式及带日期列的宽表。指标包括体重、身高、静息心率、HRV、血氧、睡眠、步数、压力、身体电量、血压，以及 AOT 中确认的跑步/骑行 VO₂max、睡眠分数、阈值心率/速度、卡路里、楼层、呼吸率、饮水量、恢复时长、HRV 状态、训练准备状态和完全恢复状态；体重与身高齐全时会计算 BMI。汇总默认输出 JSON，也可用文本格式查看指标名称和单位；乳酸阈值速度会同时显示每公里配速。数值指标可用于本地汇总和 AI 活动分析；状态字段只保存 CSV 提供的标签，不计算设备侧准备度或恢复算法。

```powershell
python sync.py health import .\health.csv
python sync.py health summary
python sync.py health summary --format text
```

AI 运动分析保留为可选功能。它把单次活动摘要、最近活动的周汇总和本地健康指标发送给 OpenAI 兼容的 Chat Completions 接口，不发送 GPS 坐标。可先检查提示词，再配置自己使用的远端或本地模型：

```powershell
python sync.py ai-analysis <活动ID前缀> --prompt-only
python sync.py ai-analysis <活动ID前缀> --prompt-only --language en-US --focus recovery --detail detailed
python sync.py ai-analysis <骑行活动ID前缀> --force-vector-json .\force-vector.json --force-vector-focus stability --prompt-only
```

`--language` 接受语言代码，默认 `zh-CN`；`--focus` 可选 `performance`、`health` 或 `recovery`，默认 `performance`；`--detail` 可选 `brief`、`normal` 或 `detailed`，默认 `normal`。提示词会要求模型准确引用已有数值，并避免医疗诊断。健康指标按活动时间筛选：活动开始前各指标最近一次记录，以及活动结束后至结束日 UTC 日末的记录；不把活动之后其他日期的数据带入历史活动分析，所有指标都保留时间戳。

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

Wi-Fi 文件导入页默认只监听本机。要让手机从同一局域网访问，显式绑定局域网接口：

```powershell
python sync.py receive --host 0.0.0.0 --port 8765
```

接收页不设访问口令，只应在可信的本地网络中临时开启。

未移植到 Python CLI 的 APK 功能包括其余云平台的私有认证/同步协议、手机 BLE 配对引导界面、训练台其他控制命令、BigRun ECG 诊断分类规则、设备侧训练准备度和恢复算法（HR-TSS/CTL/ATL/TSB 已实现）、在线健康数据源、AI 聊天及 AI 计划/课表生成。标准 FTMS 目标功率控制已实现；BLE 测量通知也支持心率、跑步步频、骑行速度/踏频、功率计测量与功率向量、室内单车数据。BigRun ECG 已支持原始通知采集、波形解码、工作模式切换、波形归一化及非诊断性 R 峰、R-R 间期和心率指标计算；诊断分类规则尚未移植。Samba 默认使用 SMB2/3；旧协议需要显式添加 `--legacy-smb`，导入始终只读。GarSync 健康状态数值可从本地 CSV 导入，但项目不从手表或云端读取这些数据。当前天气功能需要显式坐标和 GarSync 天气服务令牌；AI 活动分析使用本地汇总和可配置模型接口。静态 AOT 索引不足以确认这些云端接口的运行期请求、服务端校验或设备交互行为。

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
