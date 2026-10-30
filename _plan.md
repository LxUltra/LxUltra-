# 分区助手重设计与功能验收实施计划

> **For agentic workers:** 必须逐任务使用 superpowers:executing-plans。每一步都要按复选框追踪。

**Goal:** 在保留 LxUltra 外壳和主题的前提下，将分区助手改成参考图的信息布局，修复功能与交互缺陷，并用临时 VHD 验证写入型操作。

**Architecture:** 重做 partition_ui.js 与 partition_ui.css；继续调用 app_wrapper.py 中的 PowerShell API，只修复实际发现的后端缺陷并补齐克隆路径。Playwright 测试从源码目录加载前端，后端集成测试只作用于本轮创建的临时 VHD/VHDX。

**Tech Stack:** Windows 11、Python 3.11、PowerShell Storage/Hyper-V 模块、pywebview、JavaScript、CSS、Playwright/Edge。

**Spec:** docs/superpowers/specs/2026-10-28-partition-assistant-redesign-design.md

## Global Constraints

- 只改分区助手工作区，保留 LxUltra 标题栏、左侧导航及全局主题。
- 工具条、表格、磁盘分区图、右侧详情区按参考图组织。
- 整体使用宿主主题 token；分区图用有对比度的主题色区分类型与占用。
- 真实物理磁盘不得接受格式化、删除、清零、转换、克隆或引导修复写入。
- 破坏性集成测试只能对本轮临时目录中的 VHD/VHDX 执行。
- 任何可见操作都必须有可解释的完成、拒绝或错误状态。
- 当前目录无 Git 元数据；修改前创建完整的本轮源码/测试快照。

## Review Focus

- 刷新或磁盘枚举失败：显示错误并可重试，不丢失当前选择与待执行队列。由任务 3/4 测试。
- 空闲/未分配空间和极小分区：图形宽度仍准确、可访问、不会溢出。由任务 3 测试。
- 快速分区布局中的空值、重复盘符、超容量：提交前拒绝，且不触碰磁盘。由任务 4/6 测试。
- 系统盘的卷标、EFI/MSR/恢复卷及 MBR 操作：后端也拒绝错误目标。由任务 2/5/6 测试。
- 克隆源/目标相同、只读、容量不足或目标带数据：安全拒绝或要求逐字确认。由任务 2/5/6 测试。

---

### Task 1: 建立源码前端验收契约

**Files:**
- Modify: ui_test.py
- Test: ui_test.py

- [x] 将测试服务器的 CSS/JS 源改为从项目源码目录读取，保留实际 PowerShell API 后端。
- [x] 新增结构断言：工具条、表格区、磁盘图区、右侧操作区、撤销/重做控件必须存在。
- [x] 新增交互断言：表格与图形选择同步；操作队列可撤销、重做、移除、清空；关闭/取消对话框不会排队。
- [x] 运行 UI 契约测试，确认它因当前缺少新布局和撤销/重做而失败。

### Task 2: 添加后端安全与功能回归测试

**Files:**
- Modify: test_wrapper.py
- Create: backend_contract_test.py
- Test: backend_contract_test.py

- [x] 添加 mocked-PowerShell 后端测试，证明系统卷改标签在调用 Set-Volume 前被拒绝。
- [x] 添加目标磁盘参数测试，证明重建 MBR 使用所选的非系统 MBR 目标，不对 SYS 执行写入。
- [x] 添加克隆硬盘/分区契约测试：支持的非系统数据卷能进入执行路径；源目标相同、系统盘、保护分区、只读目标和无确认输入被拒绝。
- [x] 先运行这些新增测试，记录当前缺陷的预期失败。

### Task 3: 重做工作区布局和分区图

**Files:**
- Modify: partition_ui.js
- Modify: partition_ui.css
- Test: ui_test.py

- [x] 将 shell 改成 AOMEI 信息排列：顶栏工具条；左侧可滚动表格在上、横向磁盘行在下；右侧详情和操作面板。
- [x] 保持宿主颜色 token；磁盘固定信息列、比例分区块、类型色、占用条、名称和百分比信息齐全。
- [x] 磁盘行和分区块支持选择；与表格选中状态联动；保证键盘焦点和 aria-label。
- [x] 窄窗口下折叠为纵向布局，工具条可换行且菜单不被容器裁切。
- [x] 运行 Task 1 的断言并检查 1280×800 与窄视口截图。

### Task 4: 完善操作交互和队列生命周期

**Files:**
- Modify: partition_ui.js
- Modify: partition_ui.css
- Test: ui_test.py

- [x] 为所有当前可见动作补上对话框、目标摘要、字段校验、确认和队列数据断言。
- [x] 增加撤销/重做栈；每次加入/移除/清空操作后保持按钮状态和计数一致。
- [x] 提交二次确认显示完整目标、风险和逐条操作；提交期间禁用重复提交。
- [x] 任务轮询错误不再被吞掉；显示错误状态和重试/刷新入口；完成后刷新磁盘并保留结果。
- [x] 为菜单切换、Escape、点击遮罩、快捷键、空数据、API 异常和重复点击加入 UI 回归测试。
- [x] 运行所有 UI 测试，确认逐项新增断言通过。

### Task 5: 修复真实后端缺陷并接通克隆

**Files:**
- Modify: app_wrapper.py
- Modify: partition_ui.js
- Modify: partition_ui.css
- Test: backend_contract_test.py

- [x] 修复 set_label 的目标解析与保护卷拦截，避免可经 API 修改启动卷标签。
- [x] 修正 rebuild_mbr 的目标磁盘校验和 bootsect 参数，使它只处理被选中的非系统 MBR 数据盘。
- [x] 增加 clone_partition 操作；clone_disk 与 clone_partition 仅支持非系统数据卷文件级复制，要求目标确认、容量和可写状态检查。
- [x] 克隆前明确显示目标数据会被清除；不宣称生成可启动系统克隆。后端独立复查系统盘、EFI/MSR/恢复卷和源目标冲突。
- [x] 将前端“实验停用”改为可用的目标选择流程；不支持的文件系统或布局要显示原因。
- [x] 运行 Task 2 测试确认先前失败项转绿。

### Task 6: 临时 VHD 写入型端到端验收

**Files:**
- Create: vhd_acceptance.py
- Test: vhd_acceptance.py

- [x] 创建临时 256–512 MB VHDX 夹具并记录磁盘号、路径、盘符、序列号；确认所有目标均非物理盘且非系统盘。
- [x] 在夹具上完整验证格式化、卷标、盘符、缩容/扩展、检查、整理、测速、删除分区、快速分区、删除全部、GPT/MBR 转换、重建 MBR、擦除、VHD 挂载/卸载及克隆。
- [x] 每个步骤检查磁盘/分区状态变化和 API 作业结果；对错误/超容量输入确认写操作未发生。
- [x] 在 finally 中卸载并清理仅由本脚本创建的 VHDX；若卸载失败则保留文件并报告准确路径，不删除未知目标。
- [x] 运行夹具并确认完整结果数、失败项和清理结果。

### Task 7: 构建、桌面回归与交付

**Files:**
- Modify: build_patched.py（仅在新前端打包/自检确有必要时）
- Create: 本轮验收报告与截图
- Deploy: D:\LxUltra\LxUltra.exe 及 _internal\inventory 前端资源

- [x] 在实现前创建 _backup_redesign_20261028 快照；运行 UI、后端、压力和 VHD 基线。
- [x] 运行 Python 3.11 构建、自检、JavaScript 语法检查和全部自动测试。
- [x] 将 exe 与 CSS/JS 部署到安装目录前备份现有版本并比对 SHA-256。
- [ ] 打开安装版，确认磁盘列表、选择联动、操作栏、队列与主题颜色；不对真实磁盘提交修改。（未完成：Windows 界面控制器窗口句柄失效，详见复测验收报告。）
- [x] 生成验收报告，逐项标注自动通过、手动通过和未能安全执行的边界。





## 用户追加项

- [x] 将系统修复和网络修复合并到“电脑修复”，同页保留两个功能面板。
- [x] 常用软件缓存默认不勾选。
- [x] 文件页标签与分区工具栏按钮统一为标准矩形按钮；悬停不位移。
