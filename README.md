# 物料收纳柜管理系统

一款面向电子工程师 / 实验室的**物料收纳柜管理桌面应用**，支持格位可视化、BOM 批量导入匹配、立创商城联网查价查库存、内置元件库沉淀等功能。

应用名称：物料收纳柜

## GitHub 版本与库存同步

- 设置页可配置私有仓库 `LCZ-195/material-cabinet`、GitHub Token 和启动检查开关。
- Token 仅保存到当前 Windows 用户的 `%LOCALAPPDATA%\\物料收纳柜\\github_token.bin`，使用 Windows DPAPI 保护，不写入数据库、源码、EXE 或 GitHub 快照。
- 版本更新只接受 GitHub Release 中的 EXE，并要求 Release 附带 `SHA256.txt`、`checksums.txt` 或 `checksums.sha256`，清单中必须存在对应 EXE 文件名和 64 位 SHA-256；校验失败会拒绝替换。
- 库存同步只上传 `materials` 与 `inventories` 的白名单字段，使用稳定的格位编码，不上传 `app_settings`、日志、BOM 路径或 API 密钥；远端记录按 `update_time` 合并。

## 本地运行

```bash
pip install -r requirements.txt
python main.py
```

运行时生成的 `inventory.db`、`logs/`、`build/`、`dist/`、`__pycache__/`、用户级 Token 文件和历史归档目录不应提交到仓库。

当前版本：v1.15.7
