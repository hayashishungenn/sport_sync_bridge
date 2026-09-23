# GarSync base.apk 整包静态逆向报告

分析对象：仓库根目录的 base.apk<br>
SHA-256：EEC71C4EA16B8E6F4458268DE65B7536E4F43C1AC55B1BF016F3255B0CA59B9D<br>
分析日期：2026-09-23 至 2026-09-24

## 结论

已完成 APK 容器、Android Manifest、资源、DEX/smali、Flutter AOT 元数据与可见字符串的整包静态盘点。已恢复应用的主要模块边界、35 个云端数据源适配器、FIT/GPX/TCX 转换结构、坐标修正入口、导入路径、主要数据表和应用功能范围。

这不等于恢复了原始源代码，也不能称为 100% 完全逆向。主要业务代码位于 Flutter Dart AOT 快照中。当前提取到 Dart 3.10.8 版本、源文件路径、类名/方法名和常量字符串，但 Blutter 需要编译对应 Dart VM；自动拉取 SDK 源码长时间低速后已停止，所以没有生成 AOT 汇编和对象池反汇编。没有在模拟器或真机上运行 APK，也没有登录账号、抓取运行期流量或验证各个平台接口。

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
| Dart | 3.10.8，快照标识 1ce86630892e2dca9a8543fdb8ed8e22，arm64 Android product AOT |
| Apktool | 3.0.3 解包成功，生成 Manifest、资源、原生库和 smali |
| JADX | 生成约 4,472 个 Java 文件；报告 31 个反编译错误，进程以错误状态结束。Android 资源和 Manifest 可读 |
| Blutter | 没有完成对应运行时构建，未生成函数级 AOT 反汇编 |

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

FIT/GPX/TCX 的实现位于共享格式模块，不是三个孤立的文件重命名器。AOT 路径明确列出六个有向转换器：FIT→GPX、FIT→TCX、GPX→FIT、GPX→TCX、TCX→FIT、TCX→GPX；另有通用 format_converter、GPX/TCX reader/writer、FIT merger，以及活动格式的 parser/generator。

统一活动模型有 Activity、ActivityDetail、TrackPoint、TimeSeriesPoint、LapData、设备信息、天气、动态指标、跑步/骑行/游泳指标等结构。结合模型名和 XML 扩展常量，可确认转换层面向轨迹坐标、时间、海拔、速度、距离、心率、踏频、圈段及运动类型等信息。应用文案说明 GPX 导出偏 GPS 轨迹，TCX 可带轨迹和心率；GPX/TCX 的 XML 扩展常量也包含心率、踏频和温度字段。FIT 可表达的字段更多。字段到字段的精确优先级、插值、舍入和损失规则需要函数级反汇编或样本运行才能确认。

坐标管理页及字符串包含制造商 ID、产品 ID、固件版本、版本范围、旧/新版本边界和设备清单等字段。内置帮助文案说明：FIT 默认按 WGS-84，部分中国设备可能写入 GCJ-02；设备匹配后可按固件阈值将 GCJ-02 转为 WGS-84。另有 GPS 轨迹 Kalman 平滑选项，文案提示它能减小抖动但可能让急弯略有滞后。固件规则冲突、未知版本等精确判定仍未通过 AOT 函数体或运行样例验证。

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

当前仓库的目标比 GarSync 窄：iGPSPORT/OneLap 下载运动 FIT，再上传 Garmin Connect 国际区和 Strava，并用 SQLite 记录同步状态。仓库当前提交 507e9d7 已落地本任务直接相关的两项：按 FIT 厂商/产品/固件匹配坐标规则，以及 FIT/GPX/TCX 六方向转换和按目标格式生成上传文件。这与 GarSync 的统一活动模型、格式转换器和设备坐标管理结构相吻合。

本次整包分析没有把 GarSync 的账号系统、35 个云平台适配器、健康数据、训练计划、AI 教练、BLE 设备、Wi-Fi/Samba 导入、排行榜或内购移植进命令行仓库。它们是独立的产品能力，接入时需要分别核验平台授权、数据类型、API 行为和凭据安全，不能仅凭 APK 中存在相应类名就视作可直接复用的协议实现。

## 证据文件与限制

已生成的完整解包结果留在被 .gitignore 忽略的 .data/apk_reverse/：

- apktool/：解码后的 Manifest、smali、资源、Flutter assets 和 native libs。
- jadx/：DEX 的 Java 反编译结果和 Android 资源；有 31 个类反编译错误。
- aot_ascii_strings.txt、aot_feature_strings.txt、aot_hosts.txt：AOT 静态字符串筛选结果。字符串转储可能包含应用配置，不应公开或提交。
- app_package_paths.txt、format_package_paths.txt、data_adapter_modules.txt、aot_sql_strings.txt：用于本报告的模块、格式和 SQL 名称盘点。

剩余无法由本轮证据回答的问题包括：Dart 各函数的完整伪代码、每个适配器的实际认证/签名和请求响应协议、各平台方向与数据类型的运行状态、转换边界和损失精度、远端文件删除的准确触发条件，以及隐私声明与实际运行流量的一致性。APK v2/v3 签名证书也未检查，当前环境没有 apksigner。要继续回答这些问题，需要成功构建 Blutter AOT 解析器并运行 APK/使用脱敏样本与测试账号做动态验证。

参考工具说明：JADX 能解码 DEX/资源，但官方文档说明无法保证 100% 反编译成功；Blutter 面向 Android arm64 libapp.so，输出带符号汇编、对象池转储和 Frida 模板，工具本身仍将更多代码分析列为待完成项。

- JADX: https://github.com/skylot/jadx
- Apktool: https://github.com/iBotPeaches/Apktool
- Blutter: https://github.com/worawit/blutter
