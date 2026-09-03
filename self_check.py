# -*- coding: utf-8 -*-
"""断言式自检（无测试框架）：全部跑在临时库上，不触碰真实 inventory.db。

覆盖五块易回归逻辑：
1. 历史库存记忆库：record / 白名单更新 / 参数 JSON 序列化 / 删除返回值
2. 补货推荐：空库可推荐空仓、同批 exclude_slot_ids 去重、E 行同参与推荐
3. BOM 导入合并：同供应商编号/同参数去重、表头列独占、多工作表选择
4. 历史逗号拼接编码：get_by_code / search_for_bom / 库存关键字搜索兼容
5. BOM 兜底建档：material_code 取首个备选编号

运行：python self_check.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import models.database as database
from models.bom_model import BomItem, BomRecord
from models.component_lib_model import ComponentLib
from models.inventory_model import Inventory
from models.material_model import Material
from services.bom_service import BomImporter
from services.github_sync_service import GitHubSyncService, _snapshot_version

database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="selfcheck-"), "check.db")
database.init_database()

# ---- 1) 记忆库 CRUD ----
lib_id = ComponentLib.record({
    "lcsc_code": "C1234", "supplier_part": "SP-1", "model": "LM-1", "name": "测试料",
    "specification": "10k 1%", "package": "0805", "category": "电阻",
    "parameters": {"resistance": "10k"},
}, source="selfcheck")
assert lib_id, "record 应返回新 id"

assert not ComponentLib.update(lib_id, {"hacker_field": "x"}), "白名单外字段应整体拒绝"
row = ComponentLib.get(lib_id)
assert row["name"] == "测试料", "白名单外字段不得写入"

assert ComponentLib.update(lib_id, {"name": "改名", "parameters": {"r": "10k"}}), "白名单内字段应可更新"
row = ComponentLib.get(lib_id)
assert row["name"] == "改名"
assert json.loads(row["parameters"]) == {"r": "10k"}, "dict 参数应序列化为 JSON 文本"

assert not ComponentLib.delete(lib_id + 999), "删除不存在的记录应返回 False"
assert ComponentLib.delete(lib_id), "删除存在的记录应返回 True"
assert ComponentLib.get(lib_id) is None

# ---- 2) 补货推荐 ----
base = dict(specification="10k", package="0805", category="电阻",
            material_id=None, seed_key="R1")
first = Inventory.suggest_location_for_restock(**base) or []
assert first and "slot_id" in first[0], "空库也应推荐出有效空仓"
assert first[0].get("extra", {}).get("slot_code"), "extra.slot_code 供弹窗显示，不得缺失"

second = Inventory.suggest_location_for_restock(
    exclude_slot_ids={first[0]["slot_id"]}, **base) or []
assert not second or second[0]["slot_id"] != first[0]["slot_id"], "同批排除后不得重复推荐同一格位"

# E 行已解锁为正常仓位：排除 A–D 行（id 1..64）后应能自动推荐到 E 行
e_suggestion = Inventory.suggest_location_for_restock(
    exclude_slot_ids=set(range(1, 65)), **base) or []
assert e_suggestion and str(e_suggestion[0]["extra"]["slot_code"]).startswith("E"), "E 行应可被自动推荐"

# ---- 3) BOM 导入合并（同供应商编号/同参数去重） ----
bom_rows = [
    {"material_name": "100pF", "specification": "100pF", "package": "0402",
     "comment": "C14858,C277507,C3089499", "supplier_part": "", "required_qty": 11},
    {"material_name": "100pF", "specification": "100pF", "package": "0402",
     "supplier_part": "C1790", "required_qty": 4},
    {"material_name": "100pF", "specification": "100pF", "package": "0402",
     "supplier_part": "C277507", "required_qty": 4},
    {"material_name": "100pF", "specification": "100pF", "package": "0402",
     "supplier_part": "C3089499", "required_qty": 2},
    {"material_name": "100pF", "specification": "100pF", "package": "0402",
     "supplier_part": "C14858", "required_qty": 5},
]
merged = BomImporter.merge_rows(bom_rows)
assert len(merged) == 2, f"多备选编号行与逐编号行应合并为 2 行，实际 {len(merged)}"
qty_by_sp = {m["supplier_part"]: m["required_qty"] for m in merged}
assert qty_by_sp.get("C14858,C277507,C3089499") == 22, "编号交集行数量应累加(11+5+4+2)"
assert qty_by_sp.get("C1790") == 4, "编号无交集的行不得误并"

assert len(BomImporter.merge_rows([
    {"material_name": "10k", "specification": "10k 1%", "package": "0805", "required_qty": 3},
    {"material_name": "10k", "specification": "10k 1%", "package": "0805", "required_qty": 7},
])) == 1, "均无编号且参数一致的行应合并"

bom_id = BomRecord.create("追加合并自检", bom_type="restock")
row_a = BomItem.create(bom_id, 1, "", "100pF", "100pF", "0402", 11,
                       comment="C14858,C277507,C3089499")
BomItem.create(bom_id, 2, "", "100pF", "100pF", "0402", 4, supplier_part="C1790")
BomItem.merge_into(row_a, 5, supplier_part="C14858,C277507,C3089499",
                   comment="C14858,C277507,C3089499")
after = BomItem.get(row_a)
assert after["required_qty"] == 16, "merge_into 应累加数量"
assert after["match_status"] == "unmatched", "合并后应重置匹配状态待重匹配"

# ---- 4) 表头匹配列独占："规格"不得抢占"规格型号"的 material_code 位 ----
mapping = BomImporter._match_columns(
    ["No.", "Quantity", "Comment", "Footprint", "Supplier Part", "规格", "规格型号"])
assert mapping["material_code"] == 6, "'规格型号' 列应映射为 material_code"
assert mapping["specification"] == 5, "'规格' 列应映射为 specification，两列不得互抢"

only_spec = BomImporter._match_columns(["规格"])
assert only_spec["specification"] == 0 and only_spec["material_code"] is None, \
    "仅有'规格'列时应归 specification，不得被子串误映射为编码"

# ---- 5) 多工作表解析：封面/"说明"页不得抢占真实数据表 ----
from openpyxl import Workbook
wb = Workbook()
cover = wb.active
cover.title = "说明"
cover.append([None, "某某项目 BOM 说明（彩色封面页）"])
cover.append([None, "注意：请按需采购"])
sheet = wb.create_sheet("BOM总表")
sheet.append(["No.", "Quantity", "Comment", "Footprint", "Supplier Part", "规格"])
sheet.append([1, 5, "R001", "0603", "C21190", "1kΩ ±1%"])
xlsx_path = os.path.join(tempfile.mkdtemp(prefix="selfcheck-x-"), "multi.xlsx")
wb.save(xlsx_path)
err, parsed = BomImporter.parse_file(xlsx_path)
assert not err and len(parsed) == 1, "多工作表应选中有效数据表而非封面页"
assert parsed[0]["supplier_part"] == "C21190", "有效表头映射应生效"

# ---- 6) 历史"逗号拼接编码"兼容（完整编号必须搜得到） ----
polluted_id = Material.create({
    "material_code": "C21190,C2907002", "name": "1kΩ",
    "supplier_code": "C21190,C2907002", "lcsc_code": ""})
assert Material.get_by_code("C21190")["id"] == polluted_id, \
    "单编号应兼容历史逗号拼接编码物料"
Inventory.add_inventory_to_slot(1, polluted_id, 10)
assert Inventory.search_for_bom(supplier_part="C2907002"), \
    "BOM 编号匹配应兼容历史逗号拼接编码"

hits = Inventory.all(keyword="C2907002")
assert hits and hits[0]["material_id"] == polluted_id, \
    "库存页关键字搜索应能按供应商编号命中拼接编码物料"
assert hits[0].get("supplier_code") == "C21190,C2907002", \
    "库存查询必须带出物料 supplier_code（修复导出供应商列为空）"
assert Inventory.get(hits[0]["id"]).get("supplier_code"), "单条库存查询同样应带 supplier_code"

# ---- 7) BOM 兜底建档：material_code 取首个备选编号 ----
m = Material.find_or_create_from_bom({
    "material_code": "", "supplier_part": "C9001,C9002",
    "material_name": "自检电感", "specification": "10uH", "package": "0805"})
assert m and m["material_code"] == "C9001", \
    "建档编码应取首个编号而非逗号拼接"

# ---- 8) 云端库存标记与清空优先上传 ----
empty_snapshot = {"schema": 1, "materials": [], "inventories": []}
assert _snapshot_version(empty_snapshot) == _snapshot_version(dict(empty_snapshot)), \
    "空库存快照哈希必须稳定"

class FakeSync(GitHubSyncService):
    def __init__(self):
        super().__init__(app_version="2.15.13")
        self.download_calls = 0
        self.upload_calls = 0

    def _snapshot(self):
        snapshot = dict(empty_snapshot)
        snapshot["inventory_version"] = _snapshot_version(snapshot)
        return snapshot

    def _read_marker(self, name):
        return {"ok": True, "found": True, "value": self._snapshot()["inventory_version"]}

    def _download_inventory(self):
        self.download_calls += 1
        return {"ok": True, "data": {"found": True}}

    def _upload_inventory(self, snapshot=None, message=""):
        self.upload_calls += 1
        return {"ok": True, "data": {"inventory_version": self._snapshot()["inventory_version"]}}

sync = FakeSync()
result = sync.sync_inventory()
assert result["ok"] and result["data"].get("skipped"), "库存标记一致时应跳过完整下载"
assert sync.download_calls == 0 and sync.upload_calls == 0, "库存未变化时不应下载或上传"
result = sync.sync_inventory(prefer_local=True)
assert result["ok"] and sync.upload_calls == 1 and sync.download_calls == 0, \
    "清空后的优先同步只能上传空库存，不得下载旧库存"

# ---- 9) 只读设备降级：下载成功但未配置 Token 时，应部分成功而非整体失败 ----
class FakeReadonlySync(GitHubSyncService):
    def __init__(self):
        super().__init__(app_version="2.15.13")

    def _snapshot(self):
        snapshot = dict(empty_snapshot)
        snapshot["inventory_version"] = "local-diff"
        return snapshot

    def _read_marker(self, name):
        return {"ok": True, "found": False, "value": ""}

    def _download_inventory(self):
        return {"ok": True, "data": {"found": True, "changed": 0}}

    def _upload_inventory(self, snapshot=None, message=""):
        return {"ok": False, "error": "未配置 GitHub Token，库存不会上传"}


ro = FakeReadonlySync()
ro_result = ro.sync_inventory()
assert ro_result["ok"], "只读设备下载成功后不应整体失败"
assert ro_result["data"].get("uploaded") is False, "未配置 Token 时应标记未回传"
assert "已应用云端库存" in ro_result["data"].get("message", ""), "消息应说明已应用本地"
assert ro_result.get("warning"), "应携带上传失败原因供前端提示"

# ---- 10) min_stock=0 语义：0 表示「不预警」，None 才回退默认 10 ----
from backend import Backend as _Backend
assert _Backend._slot_status(5, 0, True) == "ok", "min_stock=0 且库存 5 不应预警"
assert _Backend._slot_status(5, None, True) == "low", "min_stock 缺省时应按 10 预警"
assert _Backend._slot_status(0, 0, False) == "empty", "空仓恒为空状态"

# ---- 11) 云端空快照收敛：空库存快照 = 上游已清空，本地同步清空且不得回传 ----
class FakeCloudEmpty(GitHubSyncService):
    def __init__(self):
        super().__init__(app_version="2.15.13")
        self.upload_calls = 0

    def _snapshot(self):
        snapshot = dict(empty_snapshot)
        snapshot["inventory_version"] = "cloud-empty"
        return snapshot

    def _read_marker(self, name):
        return {"ok": True, "found": False, "value": ""}

    def _download_inventory(self):
        return {"ok": True, "data": {"found": True, "empty": True, "changed": 0}}

    def _upload_inventory(self, snapshot=None, message=""):
        self.upload_calls += 1
        return {"ok": True, "data": {"inventory_version": "cloud-empty"}}


_tmp_id = Material.create({"material_code": "TMP-CLEAR", "name": "待清数据"})
Inventory.add_inventory_to_slot(2, _tmp_id, 5)
ce = FakeCloudEmpty()
ce_result = ce.sync_inventory()
assert ce_result["ok"] and ce_result["data"].get("emptied") is True, \
    "云端空快照应触发本地业务数据同步清空"
assert ce.upload_calls == 0, "清空收敛后不得再上传旧数据回填云端"
assert not Inventory.all() and not Material.all(), "本地业务数据应已被同步清空"

print("self_check: 全部通过")
