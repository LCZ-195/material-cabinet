# -*- coding: utf-8 -*-
"""库存操作服务层（带日志）"""
from models.inventory_model import Inventory, Slot
from models.material_model import Material
from models.bom_model import OperationLog, BomRecord, BomItem

# 延迟初始化的同步服务实例（避免循环导入）
_sync_service = None


def _mark_inventory_changed():
    """通知 GitHubSyncService 库存发生变更，触发后台自动上传。"""
    global _sync_service
    try:
        if _sync_service is None:
            from services.github_sync_service import GitHubSyncService
            _sync_service = GitHubSyncService()
        _sync_service.mark_inventory_changed()
    except Exception:  # noqa: BLE001
        # 同步标记失败不能影响主业务流程
        pass