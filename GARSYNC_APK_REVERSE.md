# GarSync base.apk 整包静态逆向报告

分析对象：仓库根目录的 base.apk<br>
SHA-256：EEC71C4EA16B8E6F4458268DE65B7536E4F43C1AC55B1BF016F3255B0CA59B9D<br>
分析日期：2026-09-23 至 2026-09-24

## 结论

APK 容器、Android Manifest、资源、DEX/smali、Flutter AOT 元数据、字符串与主要 assets 已做整包静态盘点。AOTopsy 对 arm64 Flutter AOT 建立了函数、类、调用边和调用图索引；索引记录 61,983 个函数和 7,686 个类，并生成 16,514 个汇编文件、3,326 个控制流图文件。`export-dart --app-only` 又为 40,460 个方法、2,795 个类导出伪代码，共 1,728 个 `.dart` 命名文件、59,421,511 字节和约 1,409,727 行。另对 21 个转换、解析、坐标、导入和同步关键方法生成了定向伪代码并逐项复核。

这属于覆盖面较广的静态逆向，不能称为源码级的“全部逆向”。Dart AOT 导出为伪代码，并非可编译的原始 Dart 源码；局部变量、字段和动态调用仍有无法解析之处。已逐项复核 21 个关键方法，但没有逐个解释其余导出方法和索引条目。没有在模拟器或真机运行 APK，没有登录平台账号、抓取运行期网络流量，也没有验证服务器交互和所有分支。

## APK 与工具结果

| 项目 | 结果 |
| --- | --- |
| Android applicationId | com.unicgames.garsync |
| 版本 | 2.11.0.8359，versionCode 8359 |
| SDK | min 26，target 36，compile 36 |
| APK | 47,354,160 字节，778 个 ZIP 条目，ZIP 完整性检查通过 |
| DEX | 一个 classes.dex，4,785,424 字节 |
| Flutter AOT | arm64-v8a/libapp.so，24,249,264 字节 |
| Flutter 引擎 | arm64-v8a/libflutter.so，11,107,920 字节 |
| Dart 版本 | AOTopsy 元数据显示 3.10.7；Blutter 使用 3.10.8 VM 成功解析。两者观察到同一快照标识 `1ce86630892e2dca9a8543fdb8ed8e22`，补丁版本标签差异仍未消解 |
| Apktool | 3.0.3 解包成功，生成 Manifest、资源、原生库和 smali |
| JADX | 生成约 4,472 个 Java 文件；报告 31 个反编译错误，进程以错误状态结束。Android 资源和 Manifest 可读 |
| AOTopsy | v1.6.0 Windows 发布版，校验官方 SHA-256 后执行；建立 61,983 个函数、7,686 个类和调用图索引，并为 40,460 个方法导出 Dart 伪代码 |
| 定向伪代码 | 对 21 个关键方法逐项复核，覆盖六种格式转换、FIT 解析/生成、坐标修正、导入、重复活动检查和同步 |
| Blutter | 对应 Dart VM 在 WSL 构建成功；生成 2,576 个 Dart 汇编文件（其中 GarSync 包 414 个）、对象池、汇编索引、IDA 命名脚本和 Frida 脚本。3 个可选命名参数分析报错，但主体解析和产物导出完成 |

APK 中有 arm64-v8a、armeabi-v7a、x86_64 三种 ABI 目录；Flutter 主体 AOT 库位于 arm64-v8a。原始 APK 未修改。APK 文件 SHA-256 在分析开始和收尾时一致。

## 应用结构与入口

这是一个 Flutter 应用。Dart 包路径显示主应用按 blocs、config、controllers、models、pages、providers、repositories、services、utils、widgets 分层；数据同步核心拆在 data_sync_api 包中，FIT 解析使用 fit_sdk。

Android 主入口是 com.unicgames.garsync.MainActivity，继承音频服务 Activity。原生代码注册两个 Flutter MethodChannel：com.unicgames.garsync/native 与 com.garsync.app/cookie。MainActivity 在启动/恢复时把 DeepLink 交给 Dart 侧处理；WebView 调试只在 application debuggable 标志存在时开启。微信入口和微信支付各有回调 Activity。

Manifest 的文件关联包含 file URI 的 FIT、ZIP、JSON、TCX、GPX；content URI 支持 FIT、GPX、TCX 和通用二进制 MIME；另有 ACTION_SEND 及 garsync: 自定义 scheme。可确认应用预期接收其他应用分享或打开的运动文件。Wi-Fi 导入网页还允许 CSV。

权限覆盖联网、网络状态、BLE 扫描/连接/广播、GPS、前台媒体/定位/健康服务、通知、传感器高采样、活动识别、前后台定位、唤醒锁、开机接收和 Google Play 计费。GPS feature 标为 required。Manifest 同时声明 usesCleartextTraffic=true，并引用 network_security_config；后者有一条允许明文访问 47.103.51.143 的 domain-config。这里只记录静态声明，未验证 Android 版本上的最终网络策略或实际请求。

## 导入、文件与同步流程

AOT 中能看到 Import Wizard 的 source、scanning、activity list、executing、result 步骤，以及 FIT、GPX、TCX、ZIP、JSON 相关解析/导入路径。字符串和符号包含 ZIP ZipCrypto 解码、Workout 文件解压和导入到本地活动库的方法。损坏文件的所有边界行为没有通过运行测试。

本地传输包含两种明显路径：

- Wi-Fi：应用启动 WiFi Transfer Service 并显示监听端口；内置 upload.html 使用 multipart POST 到 /upload，上传完成后提示在手机 GarSync 中查看。
- Samba：有 SambaDataSource、协议、凭据/URL 工厂与导入步骤；字符串还表明目录扫描尚不支持，并包含远端文件删除/校验日志。具体删除时机需运行验证。

同步侧可见 DataSourceManager、账号管理器、账户认证策略、同步任务、设备自动同步、取消/进度/成功/失败处理及重复活动检查。活动和健康数据通过统一模型写入本地库，再由各平台适配器拉取或推送。个别适配器的能力不同，不能把有代码路径等同为每个平台的所有数据类型都双向同步；AOT 字符串明确存在仅支持部分数据类型的提示。

### 云平台适配器

AOT 中检测到以下 35 个平台数据源模块。模块文件还包括账号授权、数据模型、运动类型映射、上传器或 workout 转换器等平台专用代码。资源图标只作为佐证，不表示某个平台在当前账号或版本中一定可连接。

Blackbird、Codoon、Concept2、COROS、Cycling Analytics、Fitbit、Garmin、Giant、Hammerhead、Honor、Huawei Archive、Huawei Web、iGPSPORT、Intervals.icu、Joyrun、Keep、Komoot、MapMyFitness、MyWhoosh、Nike、Nolio、OneLap、Polar、Ride with GPS、RQRun、Smashrun、Strava、Suunto、TrainingPeaks、Wahoo、Withings、Xiaomi、Xingzhe、Zepp、Zwift。

另外能看到 ZIP archive 与本地文件/文件系统、Samba、SQLite 数据源。认证结构包含 basic 与 OAuth 2；Garmin 目录还包含 Garth、OAuth 1、SSO 相关实现。字符串中有 Garmin、COROS、Huawei/Honor、iGPSPORT、OneLap、Strava、Suunto、Wahoo、Xiaomi/Zepp、MyWhoosh、TrainingPeaks 等服务端点，也有 OpenAI、DeepSeek、阿里 DashScope、火山方舟等 AI 服务端点，以及高德、腾讯地图、OpenStreetMap、ArcGIS 等地图端点。报告不复制 APK 内的应用凭据或 OAuth 配置值。

## 运动文件模型、格式转换与坐标

AOT 中确认了 FIT、GPX、TCX 六种有向转换器，以及统一活动模型、FIT parser/generator、GPX/TCX reader/writer 和 FIT merger。统一模型包含 Activity、ActivityDetail、TrackPoint、TimeSeriesPoint、LapData、设备信息、天气、动态指标和跑步/骑行/游泳指标等类型。静态结构支持轨迹、时间、海拔、速度、距离、心率、踏频、圈段及运动类型等字段；转换到目标格式时的完整字段映射、舍入和损失策略没有全部恢复。

### AOT 函数级复核

定向伪代码中的地址是 libapp.so 内的函数地址。文件保存在 `.data/apk_reverse/aotopsy/targeted/`。

| 路径 | 复核到的逻辑 |
| --- | --- |
| FIT→GPX，`FitToGpxConverter.convert`（0x10af34c） | 调用 FIT `Decode.read`，使用 session 信息建立 GPX 轨迹和 segment，遍历 FIT record 构造 track point，最后交给 `GpxWriter`；坐标使用 FIT semicircle 到经纬度的比例常量 |
| FIT→TCX，`FitToTcxConverter.convert`（0x10adec4） | 解码 FIT 并创建 TCX activity；可见路径读取 session 的 sport、开始时间、计时、距离、卡路里，并构建 TcxLap 与 TcxTrackPoint 后交给 `TcxWriter` 输出 |
| GPX→FIT，`GpxToFitConverter.convert`（0x10a83dc） | 先经 `GpxReader` 读取，再通过 FIT `Encode` 与 profile message 写文件；伪代码可见创建 FileIdMesg、ActivityMesg、SessionMesg、LapMesg 和 RecordMesg，并调用运动类型/时间转换辅助函数 |
| TCX→FIT，`TcxToFitConverter.convert`（0x10ad2e8） | 经 `TcxReader` 和 FIT `Encode` 生成 FIT 消息；伪代码可见创建 FileIdMesg、ActivityMesg、SessionMesg、LapMesg 和 RecordMesg，并调用运动类型映射函数 |
| GPX→TCX、TCX→GPX | 伪代码分别显示 `GpxReader`→`TcxWriter` 和 `TcxReader`→`GpxWriter` |
| `ActivityFitParser.parse`（0xe21090） | 可见读取 manufacturer、serial number、session 起始时间和 RecordMesg；构造 TrackPoint、ActivityDetail，解析 lap 与游泳长度，并存在 records-only 解析路径 |
| FIT generator 与 merger | generator 通过 FIT `Encode` 和 profile message 写活动；`FitMerger._mergeInternal`（0x1027a90）先扫描输入，无有效 session 时抛出错误，再创建 file ID、设备、运动类型、session 和 activity 消息。记录会经过时间戳映射并排序；代码还调用 `_writeRestLap`，以及 `_isGlobalExtreme` 和 `_DecimationPolicy.countWritten` |
| `ActivityPatcher.patchGCJ02ToWGS84`（0xe367dc） | 通过 map closure 更新 ActivityDetail 中的 TrackPoint/LapData 集合；`MapUtils.gcj02ToWGS84`（0xd55300）执行 GCJ-02 到 WGS-84 坐标计算 |
| 导入与去重 | 单文件预览路径（0x11428a8）调用 `SingleFileDataSource.list`；本地导入路径为 0xfee6b8；`ImportProvider._checkIfActivityExists`（0xd93758）访问数据库查询 |
| 同步 | `DataSyncService.bindAccount`（0xe52014）解析平台并获取数据源；`refreshMetadata`（0xc2a2b8）、`fetchContent`（0xe2f2e0）和 `pushActivity`（0x1001c64）包含数据源初始化、调用、异常处理与断开路径；设备自动同步入口位于 0xf6f23c |

这些伪代码由 AOT 控制流恢复生成。方法中仍有大量 `fN`、`local_mN`、间接 dispatch 和孤立 CFG 块，因此表格只记录可以由命名符号和调用序列支撑的结论，不把字段编号猜成确定的业务字段，也不据此断言每种格式完整保留所有信息。

### 活动合并器补充复核

本轮继续查看 `FitMerger._scanForStats`、`_buildForcedTimestamps`、`_isGlobalExtreme`、`_DecimationPolicy.forStats/countWritten`、`_writeRestLap` 和 `ActivityFitMerger.merge`：

- 公共入口把选择的文件转换为输入列表；空列表抛出 `No files to merge`。内部扫描后若有效 session 数为零，抛出 `No valid sessions found in source files`。
- 内部对各输入解码 `RecordMesg`，调用时间戳映射辅助函数并对记录排序。伪代码和汇编显示会构建强制时间戳表，但字段偏移仍无法可靠说明所有输入情况下具体如何平移。
- 输出前统计运动数据，写入新的 FIT file ID、device info、sport、session 和 activity 消息。内部还调用休息圈写入方法；AOT 字符串 `Garsync merged (decimated: yes/no)`、`New activity created in Local` 和 `Failed to register merged activity` 支持输出会登记为本地新活动。
- 抽稀相关路径包含十进制常量 50,000、已写入/扫描计数、`_isGlobalExtreme` 全局数值极值判断和统计驱动的策略构造。静态结果足以确认有抽稀和极值保留路径，但不足以精确恢复所有阈值、样本选择顺序及边界行为。

本仓库新增的本地 `library merge` 据此重建 FIT 合并流程：按命令输入顺序处理同一运动类型的 FIT，重叠片段平移到上一片段结束后一秒，超过两秒的片段间休息写成休息圈；记录超过 50,000 时均匀抽样并保护首尾和可用指标极值。由于 APK 的 timestamp policy 与 sample policy 仍有未解析字段，这属于有损、可审查的行为移植，不声称二进制级等价。当前 `ActivityFile` 模型之外的开发者字段、设备身份及非记录消息不会从输入 FIT 复制。

应用文案说明 GPX 导出偏 GPS 轨迹，TCX 可带轨迹和心率；XML 扩展常量包含心率、踏频和温度。FIT 可表达的字段更多。APK 证据尚不足以列出每种转换的逐字段损失表，以下本仓库的转换实现不代表已与 GarSync 每个字段逐项等价。

### 训练压力分与训练平衡

AOT 伪代码与 Blutter ARM64 汇编交叉确认了 GarSync 的计算步骤：HRIF 为 `(平均心率 - 静息心率) / (乳酸阈心率 - 静息心率)`，结果限制在 0 到 2；HR-TSS 为 `活动时长秒数 / 3600 × HRIF² × 100`。静息心率缺省 60 bpm，未配置有效乳酸阈心率时无法从心率估算 TSS。活动 FIT 中已有的 TSS 可直接作为负荷输入，多项活动先按日累加。CTL 使用 42 日指数平滑，ATL 使用 7 日指数平滑，TSB 为更新后的 CTL 减 ATL。训练状态边界为 `< -30`、`[-30, -10]`、`(-10, 5]`、`(5, 25]`、`> 25`。

本仓库新增 `python sync.py library balance`，读取 FIT TSS；用户提供乳酸阈心率后也能估算 HR-TSS。结果包含每日 TSS、CTL、ATL、TSB 和状态区间。公式可从 AOT 静态结果复核，但尚未用 GarSync 运行时对相同活动库逐日校准初始化日期和历史边界行为。

### 坐标规则

坐标管理页及字符串包含制造商 ID、产品 ID、固件版本、版本范围、旧/新版本边界和设备清单等字段。内置帮助文案说明：FIT 默认按 WGS-84，部分中国设备可能写入 GCJ-02；设备匹配后可按固件阈值将 GCJ-02 转为 WGS-84。AOT 函数确认存在 `MapUtils.gcj02ToWGS84` 和面向活动轨迹的 patch 路径；GPX 另有 `GpxConvert.gcj2Wgs`。另有 GPS 轨迹 Kalman 平滑选项，文案提示它能减小抖动但可能让急弯略有滞后。规则数据、未命中时行为、未知固件处理与冲突判定没有全部从函数体或运行样例确认。

## 数据库与其他功能

可读 SQL 常量至少暴露这些核心表：activities、ai_analysis、metadata_entries、metadata_snapshots、routes、sleeps、training_phases、training_plans、training_readiness、training_tasks、user_summaries、weights、workouts。另有 schedule_items 的查询、删除和索引语句。迁移字符串显示活动记录含来源、运动类型、开始时间等字段；同步元数据含远端 ID、文件格式和 fingerprint。SQL 常量扫描不是完整数据库 schema 转储，字段定义应以运行库迁移结果为准。

还存在以下功能模块：

- 活动详情、分段、统计、图表、轨迹地图、活动合并、分享海报、文本/PDF 报告、训练负荷/VO2Max/恢复等分析。
- 健康数据模型覆盖步数、心率、HRV、血氧、睡眠、压力、体重、血压、水分、呼吸、训练准备度、身体电量等类别。
- 训练计划、阶段、日程、训练课表和 workout 生成；内置 42 份训练计划 JSON，分别覆盖英语、西班牙语、法语、意大利语、葡萄牙语和中文，每种语言包含 7 个游泳、骑行、跑步计划模板，周期为 8、12 或 16 周。
- AI 教练、单次训练和计划生成、训练后 AI 分析；可见 AI 健康数据上下文构造器与多个第三方模型服务端点。
- BLE 设备扫描、设备状态/电量和 BigRun ECG/心率服务。
- 内购目录含 6 种 gems 商品与 4 种 VIP 商品；另有 Sudoku 五档题库和森林/海洋/雨声三条音频资源。

中文隐私协议和使用条款随包发布，隐私协议标记生效日 2026-03-23。协议称运动账号凭据保存在本机并用于对应平台登录；AI 计划会把相关信息发送到 AI 服务，PB 排名会上报服务端。以上是随 APK 附带的政策文本，不代表已通过网络抓包核实的运行事实。

## 对 sport_sync_bridge 的映射

当前仓库的目标比 GarSync 窄：iGPSPORT/OneLap 下载运动 FIT，再上传 Garmin Connect 国际区和 Strava，并用 SQLite 记录同步状态。现有实现已包含按 FIT 厂商/产品/固件匹配坐标规则、FIT/GPX/TCX 六方向转换和按目标格式生成上传文件。这与 GarSync 的统一活动模型、格式转换器和设备坐标管理结构相吻合。

整包逆向完成后，按用户要求继续把可在当前 Python CLI 中独立运行的本地能力改写进仓库。新增本地活动库、FIT/GPX/TCX 与 ZIP/轨迹 JSON/CSV 导入、活动汇总与报告、本地健康 CSV、训练计划日历、FIT 课表模板导出、Wi-Fi 上传页，以及可配置 Chat Completions 接口的 AI 活动分析和本地结果历史。AOT 静态索引显示 GarSync 的 `AiAnalysisRepository` 按活动读取分析结果，并将来源、活动 ID、模型名、正文、时间和元数据写入 SQLite；本项目保存模型名和正文，可按活动查看历史。AI 分析按用户明确要求保留；请求只发送活动汇总、周汇总和健康指标，不发送 GPS 坐标。项目没有复制内购目录、支付流程、数独或音频资源。

本地活动源已接入现有 Garmin / Strava 上传流程；原始文件存入 `.data/local_imports/`，数据库记录摘要和指纹。42 份训练计划模板覆盖六种语言，33 个 FIT 课表模板也随项目提供。健康数据通过用户提供的 CSV 导入；该实现不登录或抓取 APK 内的健康平台账号。已将 HR-TSS、CTL、ATL、TSB 计算加入本地活动库。FIT session 的平均/最大功率、标准化功率、强度因子、有氧/无氧训练效果和 TSS 会保留到活动摘要及 AI 分析上下文；GPX/TCX 导出会报告这些 FIT 活动级字段的损失。

本轮已把活动合并和训练平衡接入本地命令，但它们依据静态可确认行为重写，尚未与 GarSync 运行结果逐项校准。仍未移植其余云平台的私有认证与同步协议、GarSync 账号系统、Samba 连接、手机 BLE 与传感器实时录制、路线地图与分享海报、PDF 报告、训练准备度/VO2Max/其他恢复指标、在线健康数据源、天气服务、AI 聊天及 AI 计划/课表生成。当前 AI 活动分析保留为本地活动汇总和可配置的兼容模型接口。AOT 静态索引无法单独证明运行时签名、认证交换、服务器校验和硬件交互；这些功能不能仅从类名或端点字符串推断完成。

## 证据文件与限制

完整解包和静态分析产物保存在被 `.gitignore` 忽略的 `.data/apk_reverse/`，没有随报告提交：

- `apktool/`：解码后的 Manifest、smali、资源、Flutter assets 和 native libs。
- `jadx/`：DEX 的 Java 反编译结果和 Android 资源；JADX 报告 31 个类反编译错误。
- `aotopsy/pipeline/`：AOTopsy v1.6.0 对 arm64 `libapp.so` 生成的函数/类清单、调用边、调用图、字符串引用、类型/派发索引及部分函数汇编和 CFG。索引覆盖 61,983 个函数条目与 7,686 个类；当前汇编与 CFG 输出分别有 16,514、3,326 个文件。
- `aotopsy/reconstructed_dart_all/`：AOTopsy 的 app-only 全量方法伪代码导出，共 40,460 个方法、2,795 个类、1,728 个 `.dart` 命名文件，约 1.4 百万行；不是原始或可编译 Dart 源码。
- `aotopsy/targeted/`：21 个关键函数的定向伪代码，包括六个转换器、活动 FIT parser/generator、坐标 patcher、导入/去重和同步入口。
- WSL `/root/.cache/sport_sync_bridge/garsync_blutter/`：Blutter 生成的对象池、Dart AOT 汇编、IDA 命名脚本和 Frida 脚本，包含训练平衡计算路径；产物留在本机分析缓存，没有提交。
- `aot_ascii_strings.txt`、`aot_feature_strings.txt`、`aot_hosts.txt`：AOT 静态字符串筛选结果；完整字符串和端点转储可能含应用配置，不应公开或提交。
- `app_package_paths.txt`、`format_package_paths.txt`、`data_adapter_modules.txt`、`aot_sql_strings.txt`：用于模块、格式和 SQL 名称盘点。

静态分析仍无法回答所有函数的真实行为、每个适配器的实际认证/签名和完整请求响应协议、所有平台的数据方向、转换字段损失精度、远端文件删除的准确触发条件，以及隐私声明与实际流量的一致性。JADX 有反编译错误；AOT 伪代码和汇编仍有动态派发、变量/字段恢复限制，Blutter 另有 3 个可选命名参数解析错误；Dart 元数据解析器对补丁版本判断也不一致。APK v2/v3 签名证书未检查，当前工作未运行 APK、未做 Frida 动态跟踪或账号/API 联调。

因此，若“全部逆向”指每个 Dart 函数都恢复成原始源码并通过运行验证，本次尚未达到。已完成的是整包静态清单与模块盘点、全量 AOT 索引/调用图、全量 app-only 伪代码导出、资源和 Android 层解包，以及 21 个关键函数的逐项复核。剩余方法仍需按模块解释；还需要在可运行设备上用脱敏样本和测试账号做动态验证。

参考工具说明：JADX 官方文档说明反编译结果不保证 100% 完整；AOTopsy 对 Dart AOT 快照做静态解析并提供汇编/伪代码与调用图，输出受 AOT 符号、动态派发和控制流恢复能力限制；Blutter 依赖对应 Dart VM 运行时。

- JADX: https://github.com/skylot/jadx
- Apktool: https://github.com/iBotPeaches/Apktool
- Blutter: https://github.com/worawit/blutter
- AOTopsy: https://github.com/BroNils/aotopsy
- AOTopsy v1.6.0 发布版: https://github.com/BroNils/aotopsy/releases/tag/v1.6.0
