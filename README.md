# LxUltra 电脑管家 · 分区助手修复源码

这里保存 LxUltra 电脑管家的分区助手后端、工作区界面、相关回归测试，以及电脑修复、文件清理和设置页面的界面改动。

## 源码范围

- `app_wrapper.py`、`migration_engine.py`：分区操作与 Windows 系统克隆后端。
- `partition_ui.js`、`partition_ui.css`：分区工作区。
- `index.html`、`file_ui.js`、`file_ui.css`：应用页面和文件功能界面。
- `*_test.py`、`nav_test.py`、`vhd_acceptance.py`：单元、界面与临时虚拟磁盘验收。
- `build_patched.py`：将后端注入原版 PyInstaller 程序的本地构建工具。

此仓库保存的是修复层源码，不包含原版程序的字节码、用户数据、磁盘映像或安装包。`build_patched.py` 需要本机合法持有的原版 LxUltra 程序与提取的 `app` 条目；单靠此仓库无法生成完整安装版。

## 验证

在 Windows 和 Python 3.11 环境中运行对应测试。界面测试需要 Playwright 与 Edge；虚拟磁盘验收需要管理员权限，并只会操作由测试创建的临时 VHD。

```powershell
python file_ui_regression_test.py
python system_migration_test.py
python ui_test.py
python vhd_acceptance.py
```

最近一次复查见 [2026-10-30 验收记录](docs/acceptance/2026-10-30-recheck.md)。C 盘清理的 AI 缓存与项目类别及软件缓存类别默认关闭；即使打开类别，具体文件也默认不勾选。验收不会对用户磁盘、文件或驱动执行清理和安装。此前真实 C: 到 L: 的系统映像复制、应用和启动配置写入成功；目标盘独立开机仍需从固件启动菜单实测，不能据此认定已经完成可启动性验收。

## 操作说明

系统克隆会清除所选目标分区的数据，并在该范围内重建 EFI 和 Windows 分区。数据克隆是文件级镜像，不复制分区表或系统启动能力。请在确认窗口核对来源和目标；涉及擦除、格式化或转换分区表的操作会清除数据。
