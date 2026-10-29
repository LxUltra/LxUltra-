# LxUltra 系统迁移验收（2026-10-29）

## 交付

- “克隆”菜单新增“系统克隆（迁移系统）”。先选运行中的 C:，锁定源，再点另一块磁盘上的目标 NTFS 分区。
- “克隆”菜单把原来的“克隆硬盘（数据文件）”和“克隆分区（数据文件）”合并为单一入口“数据克隆（文件镜像）”：选中硬盘时镜像它的可用数据卷，选中分区时只镜像该分区；来源锁定后再点目标，源本身不能作为目标。系统迁移仍使用单独入口。
- 系统迁移预览显示源、目标磁盘和分区标识、精确清除范围、新 EFI 大小、独立暂存卷；必须勾选明确的目标分区清空确认后才能排队，系统迁移须单独提交。
- 后端对源、目标、暂存卷及目标盘完整分区布局做只读预检；在清除目标前，再次比较标识。系统映像从 VSS 快照由 DISM 捕获并应用，使用完整性检查；wimlib 继续校验 WIM。DISM 的 `/ScratchDir` 固定在所选暂存卷，避免占用系统环境默认的临时目录。预览会扫描源卷上的 OneDrive 同步目录并显示在线文件数、估算下载量和路径样例。在线文件夹要求用户先在资源管理器中设为“始终保留在此设备”；在线文件则需用户在目标清空确认框中单独同意下载。确认后，程序通过 Windows Cloud Files API 下载并复核文件已本地可用，然后才创建 VSS 快照。下载或复核失败均在目标分区修改前停止。只删除选中的目标分区，在原范围内创建 EFI 和 Windows 分区；随后写入并检查 BCD。
- 提交中的按钮会立即禁用并显示灰色“提交中”；任务创建后显示灰色“已提交”。进度条移至工具栏下方并在滚动时保持可见；系统迁移显示当前阶段百分比，OneDrive 下载单独占一段进度。
- 当前验收版：`D:\LxUltra\LxUltra_健康与磁盘优化修复版.exe`，资源 `D:\LxUltra\_internal\partition_ui.js`，wimlib 工具及许可证 `D:\LxUltra\migration_tools`。源码：`D:\Codex\LxUltra-PartitionAssistant`。
- 部署前文件备份：`G:\Codex-LxUltra-SystemMigration-20261028\backup_predeploy_20261028`。
- 交互录屏：`G:\Codex-LxUltra-SystemMigration-20261028\shots\system_migration_ui_demo_styled_20261028.webm`；这是内存模拟磁盘的点击演示，不是对真实 L: 的迁移录像。

## 已执行验证

| 项目 | 结果 | 范围 |
| --- | --- | --- |
| 系统迁移隔离 VHD 端到端 | 通过 | VSS 捕获、映像校验、仅重建选中目标分区、应用映像、EFI/BCD 生成、分区身份复核。未触碰其他物理盘分区布局。源是小型模拟 Windows 目录。 |
| 系统迁移 UI 点击链 | 通过 | 源 C: 锁定、同盘拒绝、目标二次点击、第三盘暂存卷、范围预览、目标分区清空勾选确认、队列、提交参数。提交由模拟 API 接收，未清除物理目标。 |
| 统一数据克隆入口 | 通过 | 一个菜单项；分别覆盖整盘数据卷镜像和单分区镜像，源锁定、拒绝源作为目标、确认正确目标并排队。模拟磁盘，不执行真实写入。 |
| 现有分区功能 VHD 验收 | 54/54 通过 | 文件级分区和整盘镜像、快速分区、格式化、卷标、盘符、扩缩容、检查及修复、碎片整理、测速、删除、GPT↔MBR、重建 MBR、擦除、VHD 挂载/卸载与系统保护。只对一次性 VHD 执行，测试后已卸载清理。 |
| 分区工作区 UI 验收 | 65/65 通过 | 实际磁盘只读加载、布局、菜单可达、队列模拟、20 次刷新和 60 次选择。 |
| 后端守卫与单元测试 | 44 项通过 | 覆盖云文件预检、显式下载授权、Cloud Files API 成功/错误处理、目标信息、捕获/还原参数、快照清理与阶段顺序。 |
| WOF 文件回归 | 通过 | 对应报错的 Windows SystemApps 目录共 15 个文件；DISM 从 VSS 捕获和还原均成功，文件 SHA-256 全部一致。 |
| OneDrive 云占位预览 | 通过 | 本机真实同步目录扫描出 2 个未下载文件、约 1.28 MB、0 个扫描错误；Cloud Files API 的本机 DLL/入口存在。实际下载未触发；单元测试用假 API 验证成功、HRESULT 失败与句柄关闭。 |
| 迁移进度与提交状态 UI | 通过 | Playwright 验证提交中立即禁用、任务创建后按钮灰显、阶段百分比不是误报 `1/1=100%`、进度位于工具栏下方并在页面滚动时 sticky。 |
| 云文件授权 UI | 通过 | 计划预览显示文件数/估算大小/样例；下载单独勾选后才能提交；模拟下载失败显示目标信息及错误状态。 |
| 失败结果目标信息 | 通过 | 后端结果带回锁定的磁盘/分区/盘符，界面不再显示 `undefined`。 |
| 后端压力测试 | 13/13 通过 | 40 次连续读取、8 线程共 40 次并发读取、非法输入；句柄与内存保持平稳。 |
| 安装版启动 | 通过 | 新 EXE 进程启动，包装器日志显示 `partition_migration_plan` 与任务接口已注册。 |
| 安装版健康检查 | 通过 | 在实际安装版中点击“测试”→“健康检查 (SMART)”→选择硬盘 0；显示健康状态、健康度估算及温度，设备未提供的指标明确标注。 |
| 真实 C:→L: 系统克隆 | 后端执行成功，启动待验 | 2026-10-29 完成 VSS 捕获、WIM 校验、目标复核、仅重建原 L: 范围、DISM 应用及 EFI/BCD 写入；任务返回成功。目标盘独立启动尚未测试。 |

## 尚未验证

- 2026-10-29 对用户指定的真实 C:（硬盘 1）→ L:（硬盘 0 原分区 19）执行迁移。预检确认 OneDrive 在线文件 0 个；经确认，映像仅排除两个零字节 `nul` 保留名文件。VSS 捕获、wimlib 校验、WIM 文件名检查、目标身份复核、仅在原 L: 范围创建 EFI 与 Windows 分区、DISM 应用、BCD 写入和文件检查均成功；后台任务返回 `done` 与成功结果。新分区为硬盘 0 的 EFI 分区 19（临时 V:）和 Windows 分区 20（L:）。移除临时盘符前，`bcdedit /store V:\EFI\Microsoft\Boot\BCD /enum {default}` 显示 `device`、`osdevice` 均为 L:；C: 与 L: 的 `Windows\System32\winload.efi` SHA-256 一致。验证后已移除 EFI 的临时 V: 访问路径，卷 GUID 路径保留。
- 真实目标盘的独立 UEFI 启动仍未验证。必须从固件启动菜单选择硬盘 0 实测进入 Windows 后，才能称为完整可启动克隆。
- 本次成功运行由源码验收脚本调用与安装版相同的后端，并使用安装目录校验过 SHA-256 的 wimlib；安装版 GUI 的实际点击链由 Playwright 测试覆盖，但没有在 GUI 中再次执行一遍 150 GB 的克隆。
- 模拟 Windows 目录不能作为真实系统启动；新生成 BCD 的结构与目标分区引用已检查，独立 UEFI 启动仍需在真实迁移后通过固件选择目标磁盘验证。
- 本机 BCDBoot 对新 VHD EFI 创建 BCD 时返回 183 (`c0000035`)；引擎在这种明确错误时使用 Windows BCD-Template 和 BCDEdit 创建标准 Windows Boot Loader。虚拟盘检查通过，真实机独立启动未验收。
- 当前仅支持同机 UEFI/GPT → UEFI/GPT、运行中的 C:、另一物理盘上的 NTFS 目标分区、第三块物理盘上的 NTFS 暂存卷。BitLocker 未完全解密的源或目标被拒绝。

## 组件依据

- [wimlib 官方 Windows 说明](https://github.com/ebiggers/wimlib/blob/master/README.WINDOWS.md)、[wimcapture 手册](https://wimlib.net/man1/wimcapture.html)：`--snapshot` 使用 VSS 捕获运行中的 Windows 卷。
- [微软 BCDBoot 文档](https://learn.microsoft.com/windows-hardware/manufacture/desktop/bcdboot-command-line-options-techref-di?view=windows-11)、[UEFI BCD 设置](https://learn.microsoft.com/windows-hardware/manufacture/desktop/bcd-system-store-settings-for-uefi?view=windows-11)：目标 EFI 启动文件和 Windows Boot Loader 配置。
- [微软 DISM 映像管理文档](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/dism-image-management-command-line-options-s14?view=windows-11)：支持带完整性检查的映像捕获/应用和 EA 处理；[DISM 全局参数](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/dism-global-options-for-command-line-syntax?view=windows-11)：`/ScratchDir` 可指定本地临时目录。
- [微软 Cloud Files `CfHydratePlaceholder` 文档](https://learn.microsoft.com/en-us/windows/win32/api/cfapi/nf-cfapi-cfhydrateplaceholder)：确认文件范围可在本地落盘，返回 HRESULT；[Placeholder 指南](https://learn.microsoft.com/en-us/windows-hardware/drivers/ifs/placeholders_guidance)：说明在线占位文件的按需数据行为。
- wimlib 1.14.5 官方发布 ZIP 的 SHA256：`2F446D6FA3866582175F1A22A7BE198EEEE0AEC7ABA5B4E04AD25C99EAE2D265`；安装版工具 EXE 的 SHA256：`34C0C4165591AD1F592837ED99D08273C58D6ED3FE0ED6360CF34E7B0739B353`。随安装保留 GPL/LGPL 与第三方许可证。

## 性能

日常磁盘列表不再读取 BitLocker 状态，单次实测约 1.53 秒；迁移预检仍完整读取加密状态，单次实测约 4.98 秒。测量仅针对当前机器，不能作为其他设备的保证。
