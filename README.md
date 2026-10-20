# ai-video-platform · P0

可运行的 Python NAS Scanner。仅从已经挂载的 SMB 路径读取视频，不连接 AI API，不上传素材，不写入或修改原片。登记数据库使用本地 SQLite；本阶段未实现 PostgreSQL。

## 环境与运行

需要 Python 3.11+、Git 和系统 FFprobe（FFmpeg 套件）。Python 运行逻辑仅用标准库。

```bash
cd ai-video-platform
python3 -m venv .venv
source .venv/bin/activate
python -m nas_scanner.cli --help
python -m unittest discover -s tests -v
```

可选安装命令入口：`python -m pip install -e .`，随后使用 `nas-scanner`。

## 接入华为消费级 NAS

1. 在 NAS 管理端确认型号支持 SMB 并启用共享，优先为扫描器分配只有读取权限的账户；没有该账户时可使用现有账号，但必须在电脑端以只读方式挂载。具体开关以设备固件为准。
2. 使用操作系统**只读挂载**共享。macOS Finder 普通连接不保证只读，不能直接作为严格模式的 NAS 扫描入口；需要系统 SMB 只读挂载，一般挂载到 `/Volumes/共享名`。凭据保存在系统钥匙串，不放入项目或命令行。
3. Linux 可使用系统 CIFS 挂载到 `/mnt/nas_materials`，挂载选项包含 `ro`，凭据使用单独的受限权限文件。由系统管理员配置开机挂载与 SMB I/O 超时。
4. 确认挂载标志为只读，目录可读取。不要通过实际写入或删除文件测试权限。建议使用共享中**已有的小文件**作为 `--marker`，扫描器只读取它。不要为了测试修改 NAS 原片。
5. 数据库必须位于本地磁盘、素材根目录之外。SMB 上放 SQLite 不在支持范围内。

macOS 示例：

```bash
python -m nas_scanner.cli \
  --root '/Users/lijunkun/.local/share/ai-video-platform/nas-readonly/抖音自播' \
  --database './data/inventory.sqlite3' \
  --stable-seconds 30 \
  --marker '已有的共享标识文件.txt'
```

Linux 将 root 换成 `/mnt/nas_materials/抖音自播`。没有合适标识文件时省略 `--marker`；默认仍检查路径位于非系统根目录的已挂载卷。

本地测试目录必须显式加入 `--allow-local`。生产环境不要使用此选项。它只对已识别的本地文件系统允许测试；SMB/NFS 可写挂载仍会拒绝，其他检查仍生效。

## 扫描语义

- 第一次发现文件登记为 `pending`；后续扫描确认 size、mtime 未变，且距首次观察超过稳定窗口，才计算 SHA256、调用 FFprobe，变为 `ready`。默认需要两次扫描；`--stable-seconds 0` 适合静态测试素材。
- 默认复用 size、mtime 未变的 ready 记录，不重新读取视频。新增或变化文件才计算 SHA256；使用 `--rehash --max-files 0` 可完整复核，包括保留大小与修改时间的内容替换。处理前后以及提交前检查文件属性，发现变化则整批回滚。
- `contents` 按 SHA256 去重保存元数据；`files` 保留每条路径与当前状态。副本仍有独立文件记录；相同内容只探测一次。
- 唯一的旧路径消失、新路径出现且 SHA256 相同，记录 `moved` 事件（包含改名）。旧路径保留为 missing，新路径为 ready。多个相同内容来源无法唯一判断时仅记录 missing，避免虚构移动关系。
- 完整遍历成功并通过提交前健康检查，才确认 missing。任何目录读取、文件读取、FFprobe 或身份检查错误都会回滚该次扫描；历史记录不会因此被清空。
- 根目录、子目录或文件符号链接不会作为素材入口跟随（根路径会解析为实际路径）。支持 MP4/MOV/M4V/MKV/AVI/MTS/M2TS/WebM，大小写不敏感。
- FFprobe 保留原始 JSON，另提取首个视频流的宽、高、编码及容器时长；时长保持 FFprobe 字符串精度。

输出示例：`{"ready": 2, "pending": 1, "missing": 0, "moved": 0, "duplicates": 1}`。duplicates 为当前根目录 ready 文件中多余副本数；missing/moved 为本次新增事件数。成功退出码 0，扫描失败为 2。

## 命令行配置

`--root` 必填；`--database` 默认 `data/inventory.sqlite3`；`--stable-seconds` 默认 30；`--ffprobe` 可指定可执行文件绝对路径；`--timeout` 为单个 FFprobe 调用超时秒数，默认 30；`--marker` 是根目录内已有标识文件的相对路径。

重复执行同一命令即可增量登记。程序使用数据库旁的本地文件锁，同一数据库的重叠扫描直接失败退出；不要通过不同数据库同时扫描同一 NAS。默认串行处理，无并发读视频。

## 已知边界与下一步验收

SMB 只读权限应由 NAS 账户和系统挂载共同保证，程序的读取行为不能替代权限控制。挂载存在与身份文件检查能防止常见断连误判，但不能保证存储服务在无报错情况下返回的目录清单完整，也不是一致性快照。边扫描边移动文件仍可能产生竞态，因此应在拍摄上传相对空闲时扫描。操作系统上的 SMB 读取可能阻塞，FFprobe 超时不覆盖 Python 文件读取；生产调度需要总任务超时和系统 SMB 超时。

下一步在实际 NAS 上选择 20～50 个文件的只读试点目录：扫描两次、核对元数据和 ready 数量；通过其他正常素材管理流程上传副本或移动测试素材后验证识别；断开共享并确认程序报错、数据库历史状态不变。避免对正式原片做验收变更。当前本地测试不替代真实 NAS 网络断连测试。

后续可增加扫描运行审计、错误文件隔离重试、分页查询接口与 PostgreSQL 迁移，再进入 P1 视频理解验证。P0 不包含自动挂载、Web 界面、AI 调用或剪辑渲染。


## 低负载扫描

默认设置优先减少对上传、下载的影响：

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `--read-mib-per-second` | 5 | 视频内容读取速率，单位 MiB/s；每块 256 KiB 后按耗时补足等待，0 关闭限速 |
| `--max-files` | 20 | 每轮最多读取、哈希的视频数量，0 不限；剩余文件保留 pending，下轮继续 |
| `--file-delay` | 0.1 | 每个视频检查前的间歇秒数，复用记录也适用 |
| `--directory-delay` | 0.02 | 每个目录遍历间歇秒数 |
| `--cache-dir` | 系统临时目录 | 本地视频暂存目录，必须在素材根目录外 |
| `--rehash` | 关闭 | 重读未变化的视频进行内容校验 |

新视频以限速流复制到本地临时文件并同时计算 SHA256，FFprobe 只读取本地副本，避免绕过限速访问 NAS。每次只暂存一个视频，完成或报错后自动清理；本地磁盘需容纳最大单个素材。进程被强制终止时可能遗留临时文件。缓存目录应使用本地磁盘，不要指向另一处 SMB 挂载。

工作时段可使用更保守的起始配置，按实际网络体验调整：

```bash
python3 -m nas_scanner.cli \
  --root '/Users/lijunkun/.local/share/ai-video-platform/nas-readonly/抖音自播' \
  --database './data/inventory.sqlite3' \
  --stable-seconds 120 \
  --read-mib-per-second 2 \
  --max-files 10 \
  --file-delay 0.2 \
  --directory-delay 0.05 \
  --cache-dir './data/cache'
```

可先间隔 5～10 分钟执行一次。完整内容复核安排在空闲时段，加入 `--rehash --max-files 0` 并保持限速。`--rehash` 与有限 max-files 一起使用只复核遍历顺序中的前一批，当前没有跨轮校验游标，不能作为完整复核。

输出新增 `reused`（复用数量）、`hashed`（实际读取数量）、`deferred`（因额度推迟数量）、`bytes_read`（本轮视频读取字节数）。达到文件额度后仍完成目录清单遍历，因此不会把尚未处理的视频误判为 missing。

限速约束程序的视频读取节奏；SMB/系统预读可能形成短时突发，目录枚举和属性查询不计入字节限速。目录间歇在枚举该目录后生效，单个超大目录仍会有枚举开销。程序不检测其他用户流量，不能保证零影响；需要结合实际 NAS 监控验收。只读权限和断连回滚保持原有语义。

## P1 视频理解验证（已开始）

NAS 实机验证留待下次，当前可先使用本地视频进行 P1 验证。默认 mock 不联网；真实 Ark 调用需要明确选择提供方并配置密钥。详见 [P1 实施与接入说明](docs/P1.md)。

```bash
python3 -m video_understanding.cli --video '/本地路径/sample.mp4' --provider mock
```

P1 现已默认自动生成 720p/H.264 临时分析副本，分析成功或失败后删除；原片保留，仅 SQLite 分析结果长期保存。原片可超过 40 MiB，压缩后副本仍有 40 MiB 上传保护。需要 FFmpeg 与 FFprobe。


## 严格 NAS 保护（强制）

没有只读账号也可使用现有账号，但扫描入口**必须是操作系统只读挂载**。扫描器检查 `statvfs` 只读标志，可写挂载直接拒绝；每次健康检查重新确认。`--allow-local` 不能绕过 SMB/NFS 只读限制。切勿为了验证保护去尝试创建、删除或重命名 NAS 文件。

数据库及其 WAL/SHM/journal、锁文件、扫描缓存、P1 临时压缩目录，只允许位于识别为本地的文件系统。SMB/CIFS/NFS 或无法识别的存储类型一律拒绝；写入路径中用户符号链接和已有多重硬链接文件也拒绝。仅 macOS 的系统 `/var`、`/tmp` 别名允许解析后检查。源目录内部输出直接拒绝，且在创建锁文件之前完成检查。

程序没有删除、移动、重命名 NAS 原片的逻辑。数据库中的 missing/moved 只描述观察到的路径变化，不执行文件操作。删除仅发生在程序自己创建的本地临时目录。

挂载需由操作系统管理员设置为只读，当前程序不会自动挂载或重挂载。不要在程序运行期间更改挂载、路径别名或输出目录。应用路径检查不是操作系统沙箱；只读挂载提供实际写入拒绝能力，当前本地测试不替代真实 NAS 验证。P1 仍需使用稳定本地样本，暂不直接串联 NAS 分析。


## 素材库范围（固定规则）

当前只将 NAS 共享下 **`抖音自播/` 及其子目录中的视频**登记为目标素材。共享根目录、`设计/`、`腾讯微盘备份/` 以及其他目录不属于检索范围；即使其中有视频也不登记。`抖音自播/` 中的图片、文档、表格等非视频文件也不登记。

NAS 扫描入口必须位于挂载根目录的 `抖音自播` 下，可以选择整个目录或其中一个子目录。误把 `--root` 指向整个共享或其他目录时，在创建数据库、锁文件或扫描前直接报错。`--allow-local` 不会绕过网络目录范围检查。视频扩展名列表见扫描语义；子目录符号链接不跟随。

当前 Mac 的完整目标路径：

```text
/Users/lijunkun/.local/share/ai-video-platform/nas-readonly/抖音自播
```

NAS 上的文件全部保持原样，不移动、不删除。之前小范围验收使用的 `抖音自播/ai视频/0923-Shirley复刻开头` 已在本范围内，无需重做。范围规则不主动启动全量扫描；后续检索接入时须沿用该素材范围。P1 手动分析本地视频仍是独立验证功能，不自动进入 NAS 素材库。

后续模型测试默认使用 Chat API 的 flex 低优档位进行批量处理，不调用待下线的旧批量接口；不支持时不自动回退常规价格。详见 [测试调用政策](docs/TESTING_POLICY.md)。

### P2 本地语义检索与快速预览

复用100条Lite结果，支持中文整句语义检索、关键词检索、主体/动作/时长筛选、动作时间跳转及原始分析查看。默认语义＋关键词；人工反馈已移除。

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[semantic]'
.venv/bin/python -m material_search.cli index
.venv/bin/python -m material_search.cli serve \
  --media-root data/testsets/oatmeal-100-20261019/videos
```

先按 [P2说明](docs/P2.md) 导入Lite结果，再建立索引。首次索引下载约90MB中文向量模型，此后本地推理，不产生模型API费用。向量存于SQLite，无需独立向量数据库。

打开 http://127.0.0.1:4174/ 。需FFmpeg；预览仅访问显式指定的本地素材目录，不回退NAS。480p流式播放，Mac自动尝试硬件加速，不等待整段转码完成，不生成视频磁盘副本。软件编码备用路径及限制见P2说明。
