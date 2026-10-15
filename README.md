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

1. 在 NAS 管理端确认型号支持 SMB 并启用共享，为扫描器分配只有读取权限的账户。具体开关以设备固件为准。
2. 使用操作系统挂载共享。macOS Finder「前往 → 连接服务器」输入 `smb://NAS地址/共享名`，一般挂载到 `/Volumes/共享名`。凭据保存在系统钥匙串，不放入项目或命令行。
3. Linux 可使用系统 CIFS 挂载到 `/mnt/nas_materials`，挂载选项包含 `ro`，凭据使用单独的受限权限文件。由系统管理员配置开机挂载与 SMB I/O 超时。
4. 确认账户无法修改原片，目录可读取。建议使用共享中**已有的小文件**作为 `--marker`，扫描器只读取它。不要为了测试修改 NAS 原片。
5. 数据库必须位于本地磁盘、素材根目录之外。SMB 上放 SQLite 不在支持范围内。

macOS 示例：

```bash
python -m nas_scanner.cli \
  --root '/Volumes/素材库' \
  --database './data/inventory.sqlite3' \
  --stable-seconds 30 \
  --marker '已有的共享标识文件.txt'
```

Linux 将 root 换成 `/mnt/nas_materials`。没有合适标识文件时省略 `--marker`；默认仍检查路径位于非系统根目录的已挂载卷。

本地测试目录必须显式加入 `--allow-local`。生产环境不要使用此选项。它只关闭挂载检查，其他检查仍生效。

## 扫描语义

- 第一次发现文件登记为 `pending`；后续扫描确认 size、mtime 未变，且距首次观察超过稳定窗口，才计算 SHA256、调用 FFprobe，变为 `ready`。默认需要两次扫描；`--stable-seconds 0` 适合静态测试素材。
- 每次 ready 检查重新计算 SHA256，以识别保留大小与修改时间的内容替换。处理前后以及提交前检查文件属性，发现变化则整批回滚。
- `contents` 按 SHA256 去重保存元数据；`files` 保留每条路径与当前状态。副本仍有独立文件记录；相同内容只探测一次。
- 唯一的旧路径消失、新路径出现且 SHA256 相同，记录 `moved` 事件（包含改名）。旧路径保留为 missing，新路径为 ready。多个相同内容来源无法唯一判断时仅记录 missing，避免虚构移动关系。
- 完整遍历成功并通过提交前健康检查，才确认 missing。任何目录读取、文件读取、FFprobe 或身份检查错误都会回滚该次扫描；历史记录不会因此被清空。
- 根目录、子目录或文件符号链接不会作为素材入口跟随（根路径会解析为实际路径）。支持 MP4/MOV/M4V/MKV/AVI/MTS/M2TS/WebM，大小写不敏感。
- FFprobe 保留原始 JSON，另提取首个视频流的宽、高、编码及容器时长；时长保持 FFprobe 字符串精度。

输出示例：`{"ready": 2, "pending": 1, "missing": 0, "moved": 0, "duplicates": 1}`。duplicates 为当前根目录 ready 文件中多余副本数；missing/moved 为本次新增事件数。成功退出码 0，扫描失败为 2。

## 命令行配置

`--root` 必填；`--database` 默认 `data/inventory.sqlite3`；`--stable-seconds` 默认 30；`--ffprobe` 可指定可执行文件绝对路径；`--timeout` 为单个 FFprobe 调用超时秒数，默认 30；`--marker` 是根目录内已有标识文件的相对路径。

重复执行同一命令即可增量登记。P0 是单进程、单次扫描程序，请串行调度，避免重叠运行。可先每 60 秒运行一次；任务调度需设置单实例锁。

## 已知边界与下一步验收

SMB 只读权限应由 NAS 账户和系统挂载共同保证，程序的读取行为不能替代权限控制。挂载存在与身份文件检查能防止常见断连误判，但不能保证存储服务在无报错情况下返回的目录清单完整，也不是一致性快照。边扫描边移动文件仍可能产生竞态，因此应在拍摄上传相对空闲时扫描。操作系统上的 SMB 读取可能阻塞，FFprobe 超时不覆盖 Python 文件读取；生产调度需要总任务超时和系统 SMB 超时。

下一步在实际 NAS 上选择 20～50 个文件的只读试点目录：扫描两次、核对元数据和 ready 数量；通过其他正常素材管理流程上传副本或移动测试素材后验证识别；断开共享并确认程序报错、数据库历史状态不变。避免对正式原片做验收变更。当前本地测试不替代真实 NAS 网络断连测试。

后续可增加扫描运行审计、错误文件隔离重试、分页查询接口与 PostgreSQL 迁移，再进入 P1 视频理解验证。P0 不包含自动挂载、Web 界面、AI 调用或剪辑渲染。
