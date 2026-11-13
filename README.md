# 物料收纳柜管理系统

一款面向电子工程师 / 实验室的**物料收纳柜管理桌面应用**，支持格位可视化、BOM 批量导入匹配、立创商城联网查价查库存、内置元件库沉淀等功能。

应用名称：物料收纳柜

当前版本：**v2.16.7**

## 快速开始

```powershell
python -m pip install -r requirements.txt
python main.py
```

首次启动会自动初始化 SQLite 数据库和 80 个仓位。完整的功能说明、架构、目录、安全约束、测试和构建步骤请阅读 [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)。

## 主要能力

- 5×8 柜体矩阵、前/后仓共 80 个仓位可视化管理。
- 物料新增、编辑、删除、搜索、库存查看和仓位调整。
- CSV/XLSX/XLS 多文件 BOM 导入、匹配、补货入库和领料出库。
- 立创商城联网匹配、DeepSeek AI 可选匹配、历史元件记忆库。
- GitHub Release 更新和脱敏库存快照同步。
- 所有页面底部左侧保留「检查版本 / 检查库存」，设置页 DeepSeek AI 栏下方提供「测试连接 / 保存设置」。

## 验证与构建

```powershell
python self_check.py
python -m compileall -q .
python -m PyInstaller --noconfirm --clean 物料收纳柜.spec
```

## 安全说明

GitHub Token 仅保存在当前 Windows 用户目录并使用 DPAPI 加密，不写入源码、数据库或库存快照。运行数据库、日志、导出文件、构建产物、缓存和本地临时脚本均不应提交到仓库，详细排除规则见 `.gitignore`。
