# 物料收纳柜管理系统

## 1. 项目定位

物料收纳柜是一款面向电子工程师、实验室和小型元器件仓储场景的 Windows 桌面应用，用于管理物料主数据、80 个实体仓位、库存进出、BOM 导入匹配、历史元件记忆库以及可选的 GitHub 云端同步。

当前源码版本：**v2.16.7**

## 2. 核心能力

### 2.1 收纳柜与仓位

- 柜体为 5 行（A-E）× 8 列（1-8），共 40 个大格。
- 每个大格包含“内 / 外”两个小仓，共 80 个可用仓位。
- 支持仓位可视化、占用状态、低库存状态和空仓状态展示。
- 支持从物料编辑弹窗直接调整仓位。
- 调仓时，目标仓位已有相同物料则自动合并数量；没有相同物料则迁移原库存记录。

### 2.2 物料管理

- 新增、编辑、删除和搜索物料。
- 支持物料编码、名称、规格、封装、分类、供应商编号、立创编号、最低库存等字段。
- 分类和封装为空时显示 `-`，避免表格出现无意义的空白。
- 支持物料库存明细、所在仓位、总库存和库存调整。
- 支持内置元件记忆库，用于沉淀确认过的联网匹配结果。

### 2.3 BOM 导入与匹配

- 支持 CSV、XLSX、XLS 等 BOM 文件。
- 支持多文件导入和当前清单合并。
- 自动识别常见表头别名，包括 `Supplier Part`、`Comment`、`Footprint`、`Quantity` 等。
- 支持多工作表 BOM，自动选择有效表头命中最多的工作表。
- 支持供应商编号、规格、封装、名称、本地元件库和立创商城接口的融合匹配。
- 支持补货入库与领料出库两种模式。
- 支持批量推荐仓位、库存缺口计算和确认后自动更新库存。

### 2.4 联网能力

- 立创商城公开接口：用于物料匹配、型号查询和库存/价格信息查询。
- DeepSeek API：可选，用于 AI 物料智能匹配。
- GitHub Release：可选，用于版本更新。
- GitHub Contents API：可选，用于脱敏库存快照同步。
- GitHub Token 只保存在当前 Windows 用户目录，并使用 DPAPI 加密，不写入源码、数据库或云端快照。

### 2.5 GitHub 同步

- 支持版本标记检查和库存标记检查。
- 支持库存变更后的后台自动上传。
- 自动同步开关在真正上传前重新读取，关闭后不会继续产生上传副作用。
- 上传失败会保留待同步事件，以便后续重试。
- 上传期间产生的新库存变化不会被完成逻辑误清除。
- 同步网络采用超时、重试、代理/直连故障转移等策略。
- 云端库存快照仅包含白名单字段，不上传 Token、日志、BOM 本地路径等敏感内容。

## 3. 页面与操作入口

- `index.html`：库存总览和柜体状态。
- `cabinet.html`：收纳柜仓位查看与操作。
- `materials.html`：物料主数据和库存编辑。
- `bom.html`：BOM 导入、匹配、补货和领料。
- `analytics.html`：库存统计、报表和导出。
- `component-library.html`：历史元件记忆库。
- `admin-settings.html`：DeepSeek、GitHub、库存规则和数据清理设置。
- 所有页面底部左侧均保留“检查版本 / 检查库存”按钮，紧邻版本号右侧。
- 设置页“测试连接 / 保存设置”位于 DeepSeek AI 智能匹配栏正下方。

## 4. 技术架构

```text
pywebview 桌面窗口
        │
        ├── main.py
        │     ├── 本地 HTTP 静态文件服务器
        │     ├── JS/Python Bridge
        │     ├── CDN 到本地 vendor 的资源改写
        │     └── DPI、自启动、单实例健康端口
        │
        └── backend.py（业务门面）
              ├── models/database.py       SQLite 建库与设置
              ├── models/material_model.py 物料数据访问
              ├── models/inventory_model.py库存与仓位数据访问
              ├── models/bom_model.py      BOM 与操作日志
              ├── services/inventory_service.py 库存业务
              ├── services/bom_service.py      BOM 解析与匹配
              ├── services/lcsc_service.py     立创接口
              ├── services/deepseek_service.py AI 匹配
              └── services/github_sync_service.py 更新与库存同步
```

## 5. 目录说明

```text
物料查询/
├── main.py                         桌面入口、HTTP 服务、前后端桥接
├── backend.py                      业务门面
├── config.py                       路径、柜体规格和默认配置
├── models/                         SQLite 数据访问模型
├── services/                       库存、BOM、联网和同步服务
├── utils/                          共享工具
├── material-cabinet-dashboard/     HTML/CSS/JS 页面和本地 vendor
├── self_check.py                   临时数据库自检
├── 物料收纳柜.spec                  PyInstaller 打包规格
├── app_icon.ico                    应用图标
├── demo_bom.xlsx                   BOM 导入示例
├── requirements.txt                Python 依赖
├── VERSION.txt                     当前版本号
├── SHA256.txt                      发布包校验值
├── README.md                       项目说明
└── PROJECT_OVERVIEW.md             本项目详细技术与交付说明
```

运行时数据、构建产物、缓存、历史归档和上传辅助脚本已移入 `_trash_20261113/`，并通过 `.gitignore` 排除，不参与代码仓库发布。

## 6. 本地运行

### 环境

- Windows 10/11
- Python 3.8+
- 建议 Python 3.10+

### 安装依赖

```powershell
python -m pip install -r requirements.txt
```

### 启动

```powershell
python main.py
```

首次启动会自动创建 SQLite 数据库和 80 个仓位。源码运行时数据库位于项目目录；打包运行时数据库位于 EXE 所在目录。

## 7. 测试与构建

### 自检

```powershell
python self_check.py
```

自检使用临时数据库，不会修改真实 `inventory.db`。

### Python 编译检查

```powershell
python -m compileall -q .
```

### PyInstaller 构建

```powershell
python -m PyInstaller --noconfirm --clean 物料收纳柜.spec
```

构建结果位于 `dist/`，发布前应使用 `SHA256.txt` 记录 EXE 的 SHA-256。

## 8. 数据与安全约束

- 不提交 `inventory.db`、日志、导出文件、Token、密码或本机配置。
- 不把真实 GitHub Token 写入源码、README、测试数据或 Release 说明。
- GitHub 库存快照只允许上传白名单字段。
- 所有外部输入在进入 SQL、文件解析和远程 API 前应经过现有边界处理。
- 本地数据库操作和远程同步保持解耦：本地库存事务成功不应因网络故障回滚。

## 9. v2.16.7 交付变更

- 恢复全部页面底部的“检查版本 / 检查库存”入口。
- 将设置页“测试连接 / 保存设置”移动到 DeepSeek AI 智能匹配栏下方。
- 统一页面静态版本号为 v2.16.7，并继续由后端动态刷新。
- 清理构建产物、运行数据库、缓存、历史同步脚本和临时文件。
- 增加项目忽略规则，避免运行数据和本地凭据进入私人代码仓库。

## 10. 当前验证状态

- `python self_check.py`：通过。
- `python -m compileall -q .`：通过。
- PyInstaller v2.16.7 构建：通过。
- Release v2.16.7 EXE 与本地文件 SHA-256：一致。
