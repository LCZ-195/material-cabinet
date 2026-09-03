# -*- coding: utf-8 -*-
"""
backend.py —— 底层业务层（纯 Python，与前端 UI 完全解耦）
================================================================
职责：
  - 物料管理：增删改查、参数比对、替代关系
  - 收纳柜与库存：格位查询、入库 / 出库 / 盘点 / 清格、单格多物料
  - BOM：导入解析、库存比对、领料 / 补货
  - 设置：API 密钥、预警阈值、合并存放开关等
  - 立创商城 API 对接 + 本地参数比对 fallback
  - 报表导出
  - 以「业务门面 Backend」统一对外提供服务

约束：
  - 本文件不导入、不依赖任何窗口 / 网页库（tkinter、pywebview、PyQt 均不出现），
    可被任意前端框架通过 js_api 调用。
  - 前端 UI 文件（material-cabinet-dashboard/ 目录）与本层完全隔离。
  - 所有方法返回值均为 Python 基本类型（str / bool / list / dict），
    可被 pywebview 的 js_api 自动序列化给网页 JS 使用；异常统一为 (False, 错误信息)。

应用名称：物料收纳柜

分层结构：
  models/database.py            → 数据库连接、初始化、应用设置
  models/material_model.py      → 物料数据访问
  models/inventory_model.py     → 格位与库存数据访问
  models/bom_model.py           → BOM 与操作日志数据访问
  services/inventory_service.py → 库存业务（入库/出库/清格/补货）
  services/bom_service.py       → BOM 解析与比对
  services/lcsc_service.py      → 立创 API + 本地参数比对
  backend.py                    → 业务门面（组装以上模块 + 对外接口）
  main.py                       → 桌面窗口层（DPI、窗口、动态注入、前后端桥接）
"""

import os
import json
import logging
import threading
import re
from datetime import datetime

from config import EXPORT_DIR, MATERIAL_CATEGORIES

from models.database import init_database, purge_demo_data, factory_reset, AppSettings
from models.material_model import Material
from models.inventory_model import Slot, Inventory
from models.bom_model import BomRecord, BomItem, OperationLog
from services.inventory_service import InventoryService, ExportService
from services.bom_service import BomImporter, BomMatcher
from services.lcsc_service import LCSCApi, LocalParameterMatcher
from services.deepseek_service import DeepSeekService
from services.github_sync_service import GitHubSyncService, INVENTORY_MARKER_NAME

logger = logging.getLogger(__name__)


class Backend:
    """业务门面：为桌面层提供统一、可 JSON 序列化的调用接口。"""

    def __init__(self):
        # 确保数据库表结构与 80 个格位已初始化
        try:
            init_database()
        except Exception as e:  # noqa: BLE001
            print(f"[backend] init_database failed: {e}")
        # ponytail: 全局互斥锁——单进程桌面场景足够；多线程高并发入库时再按表/事务细分
        self._lock = threading.Lock()
        self._lcsc = LCSCApi()
        self._matcher = LocalParameterMatcher()
        self._ai = DeepSeekService()
        self._github = GitHubSyncService("物料收纳柜", "2.16.0")

    # ================================================================
    # 基础辅助
    # ================================================================
    @staticmethod
    def _ok(**kwargs):
        # 数据统一包装在 data 字段内，前端通过 res.data.xxx 访问
        return {"ok": True, "data": {**kwargs}}

    @staticmethod
    def _fail(error):
        return {"ok": False, "error": str(error)}

    @staticmethod
    def _slot_status(quantity, min_stock=None, occupied=False):
        """根据数量与预警值返回状态：空 / 偏低 / 充足。
        min_stock 为 None 时回退默认 10；显式 0 表示「不预警」而非按 10 预警。
        """
        qty = int(quantity or 0)
        if not occupied or qty <= 0:
            return "empty"
        ms = 10 if min_stock is None else int(min_stock or 0)
        return "low" if qty <= ms else "ok"

    @staticmethod
    def _fmt_time(ts):
        if not ts:
            return ""
        try:
            return str(ts)[:16].replace("T", " ")
        except Exception:  # noqa: BLE001
            return str(ts)

    # ================================================================
    # 概览仪表盘
    # ================================================================
    def get_dashboard(self):
        """概览页 KPIs：格位、物料数、低库存、最近操作、分类占比"""
        with self._lock:
            stats = Inventory.get_statistics()
            total = stats.get("total", {})
            low_stock = Inventory.get_low_stock() or []
            recent_ops = OperationLog.list_recent(limit=10) or []
            by_category = stats.get("by_category", [])

            materials = Material.all()
            material_count = len(materials)
            # 本月领料（出库次数，近30天）
            month_out = 0
            try:
                ops = OperationLog.list_recent(limit=500, op_type="outbound")
                month_out = len(ops)
            except Exception:  # noqa: BLE001
                month_out = 0

            # 分类占比: 前端需要 {name, percent} 列表，去零
            cat_pct = []
            total_qty = sum(int(c.get("qty") or 0) for c in by_category) or 1
            for c in by_category[:8]:
                q = int(c.get("qty") or 0)
                pct = round(q / total_qty * 100, 1)
                cat_pct.append({"name": c.get("category") or "其他",
                                "count": int(c.get("cnt") or 0),
                                "qty": q, "percent": pct})

            # 格位统计 (字段名与 inventory_model.get_statistics 对齐)
            occupied_slots = int(total.get("used_slots", 0))
            empty_slots = int(total.get("empty_slots", 0))

            # 从操作日志统计入库/出库/日志总数 (供 Analytics 页使用)
            from models.database import get_cursor
            total_in = total_out = log_count = 0
            try:
                with get_cursor() as cur:
                    cur.execute(
                        "SELECT operation_type, COUNT(*) as cnt FROM operation_logs "
                        "GROUP BY operation_type")
                    for r in cur.fetchall():
                        if r["operation_type"] in ("inbound", "restock"):
                            total_in += int(r["cnt"])
                        elif r["operation_type"] == "outbound":
                            total_out += int(r["cnt"])
                    cur.execute("SELECT COUNT(*) FROM operation_logs")
                    log_count = int(cur.fetchone()[0])
            except Exception:  # noqa: BLE001
                pass
            # 周转率 = 出库次数 / (入库次数+1) 防除零
            turnover = round(total_out / (total_in + 1) * 100, 1) if total_in else 0
            total_qty = int(total.get("total_qty", 0))

            # 月度出入库趋势（近 6 个月，从操作日志按 create_time 聚合）
            monthly_trend = []
            try:
                from collections import OrderedDict
                import datetime
                now = datetime.date.today()
                months = []
                for off in range(5, -1, -1):
                    y, m = (now.year, now.month - off)
                    while m <= 0:
                        y, m = y - 1, m + 12
                    months.append(f"{y}-{m:02d}")
                agg = OrderedDict((k, {"month": k, "inbound": 0, "outbound": 0})
                                  for k in months)
                with get_cursor() as cur:
                    cur.execute("""
                        SELECT strftime('%Y-%m', create_time) AS ym,
                               operation_type, COUNT(*) AS cnt
                        FROM operation_logs
                        WHERE create_time >= date('now', '-6 months')
                        GROUP BY ym, operation_type
                    """)
                    for r in cur.fetchall():
                        key = r["ym"]
                        if key in agg:
                            if r["operation_type"] in ("inbound", "restock"):
                                agg[key]["inbound"] += int(r["cnt"])
                            elif r["operation_type"] == "outbound":
                                agg[key]["outbound"] += int(r["cnt"])
                monthly_trend = list(agg.values())
            except Exception:  # noqa: BLE001
                monthly_trend = []

            # 最近一次 BOM 的未匹配物料 + 缺料总数（首页「未匹配物料」面板）
            unmatched_materials = []
            shortage_count = 0
            try:
                with get_cursor() as cur:
                    cur.execute("""
                        SELECT bi.material_name, bi.comment, bi.supplier_part,
                               bi.specification, bi.required_qty
                        FROM bom_records br
                        JOIN bom_items bi ON bi.bom_id = br.id
                        WHERE br.id = (SELECT id FROM bom_records ORDER BY id DESC LIMIT 1)
                          AND bi.match_status NOT IN ('fully', 'partial', 'replaced', 'restock_ok')
                        ORDER BY bi.required_qty DESC
                        LIMIT 10
                    """)
                    for r in cur.fetchall():
                        qty = int(r["required_qty"] or 0)
                        shortage_count += qty
                        unmatched_materials.append({
                            "material_name": r["material_name"] or r["comment"] or "未知物料",
                            "specification": r["specification"] or "",
                            "supplier_part": r["supplier_part"] or "",
                            "required_qty": qty,
                        })
            except Exception:  # noqa: BLE001
                unmatched_materials, shortage_count = [], 0

            return self._ok(
                stats=total,
                material_count=material_count,
                occupied_slots=occupied_slots,
                empty_slots=empty_slots,
                low_stock=low_stock,
                low_stock_count=len(low_stock),
                low_stock_materials=low_stock,
                recent_ops=recent_ops,
                categories=cat_pct,
                category_stats=cat_pct,
                month_outbound=month_out,
                month_picks=month_out,
                total_stock_in=total_in,
                total_stock_out=total_out,
                total_quantity=total_qty,
                turnover_rate=f"{turnover}%",
                log_count=log_count,
                monthly_trend=monthly_trend,
                unmatched_materials=unmatched_materials,
                shortage_count=shortage_count,
            )

    def get_overview_slots(self):
        """概览页迷你格位：按实体格(行列)聚合，含内/外两仓占用状态"""
        with self._lock:
            slots = Slot.all() or []
            # 聚合：key = (row, col)，内部按 position(0内/1外) 整理
            cells = {}
            for s in slots:
                key = (s["row"], s["col"])
                cell = cells.setdefault(key, {
                    "row": s["row"], "col": s["col"],
                    "entities": [], "occupied": 0, "total_qty": 0,
                })
                occupied = int(s.get("total_quantity") or 0) > 0
                cell["entities"].append({
                    "slot_id": s["id"],
                    "slot_code": s["slot_code"],
                    "position": s["position"],
                    "position_label": "前仓" if s.get("position") == 0 else "后仓",
                    "occupied": occupied,
                    "material_name": s.get("material_name", ""),
                    "quantity": int(s.get("total_quantity") or 0),
                    "status": self._slot_status(
                        s.get("total_quantity"), s.get("min_stock"), occupied),
                })
                if occupied:
                    cell["occupied"] += 1
                    cell["total_qty"] += int(s.get("total_quantity") or 0)
            result = []
            for (r, c), cell in cells.items():
                cell["label"] = f"{chr(ord('A') + r)}{c + 1}"
                cells_sort = sorted(cell["entities"], key=lambda x: x["position"])
                result.append({"row": r, "col": c, "label": cell["label"],
                               "inner": cells_sort[0] if len(cells_sort) > 0 else None,
                               "outer": cells_sort[1] if len(cells_sort) > 1 else None,
                               "occupied_count": cell["occupied"],
                               "total_qty": cell["total_qty"]})
            result.sort(key=lambda x: (x["row"], x["col"]))
            return self._ok(cells=result, total_cells=len(result))

    # ================================================================
    # 收纳柜
    # ================================================================
    def get_cabinet(self):
        """收纳柜页：返回全部格位（含占用/状态增强字段）+ 摘要统计"""
        with self._lock:
            slots = Slot.all() or []
            summary = {"total": len(slots), "occupied": 0, "empty": 0, "low": 0}
            rows = {}
            enhanced = []
            for s in slots:
                occupied = int(s.get("total_quantity") or 0) > 0
                status = self._slot_status(s.get("total_quantity"),
                                           s.get("min_stock"), occupied)
                if status == "empty":
                    summary["empty"] += 1
                elif status == "low":
                    summary["low"] += 1
                    summary["occupied"] += 1
                else:
                    summary["occupied"] += 1
                item = {
                    "slot_id": s["id"], "slot_code": s["slot_code"],
                    "row": s["row"], "col": s["col"], "position": s["position"],
                    "position_label": "前仓" if s.get("position") == 0 else "后仓",
                    "occupied": occupied, "status": status,
                    "material_name": s.get("material_name", ""),
                    "category": s.get("category", ""),
                    "specification": s.get("specification", ""),
                    "quantity": int(s.get("total_quantity") or 0),
                    "unit": s.get("unit", "个"),
                    "multi_count": int(s.get("multi_count") or 0),
                    "min_stock": int(s.get("min_stock") or 10),
                }
                rows.setdefault(s["row"], []).append(item)
                enhanced.append(item)
            # 按行列排序输出（slots 同样携带增强字段，供前端直接渲染）
            enhanced.sort(key=lambda x: (x["row"], x["col"], x["position"]))
            return self._ok(slots=enhanced,
                            rows={str(k): v for k, v in rows.items()},
                            summary=summary)

    def get_slot(self, slot_code):
        """单个格位详情（含多物料）"""
        with self._lock:
            slot = Slot.get_by_code(slot_code)
            if not slot:
                return self._fail(f"格位不存在: {slot_code}")
            invs = Inventory.list_by_slot(slot["id"]) or []
            return self._ok(slot=slot, inventories=invs)

    def get_slot_inventories(self, slot_code):
        with self._lock:
            slot = Slot.get_by_code(slot_code)
            if not slot:
                return self._fail(f"格位不存在: {slot_code}")
            return self._ok(inventories=Inventory.list_by_slot(slot["id"]) or [])

    # ================================================================
    # 物料
    # ================================================================
    def _enrich_material(self, m):
        """给物料补上库存总量与所在格位"""
        if not m:
            return m
        try:
            invs = Inventory.get_by_material(m["id"]) or []
        except Exception:  # noqa: BLE001
            invs = []
        m["total_qty"] = sum(int(i.get("quantity") or 0) for i in invs)
        m["locations"] = " / ".join(i.get("slot_code", "") for i in invs)
        m["slot_codes"] = m["locations"]
        m["material_name"] = m.get("name", "")
        m["status"] = self._slot_status(m["total_qty"], m.get("min_stock"),
                                        m["total_qty"] > 0)
        return m

    def list_materials(self, keyword="", category=""):
        with self._lock:
            mats = Material.all(keyword=keyword or "", category=category or "") or []
            enriched = [self._enrich_material(dict(m)) for m in mats]
            # 统计：本周新增、低库存、无替代料
            from models.database import get_cursor
            this_week_new = 0
            no_replacement = 0
            low_stock_count = sum(
                1 for m in enriched
                if int(m.get("total_qty") or 0) <= int(m.get("min_stock") or 0)
            )
            try:
                with get_cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM materials "
                        "WHERE create_time >= datetime('now','-7 days')")
                    this_week_new = int(cur.fetchone()[0])
                    cur.execute(
                        "SELECT COUNT(*) FROM materials m "
                        "WHERE NOT EXISTS (SELECT 1 FROM material_replacements mr "
                        "WHERE mr.material_id = m.id)")
                    no_replacement = int(cur.fetchone()[0])
            except Exception:  # noqa: BLE001
                pass
            return self._ok(materials=enriched,
                            this_week_new=this_week_new,
                            low_stock_count=low_stock_count,
                            no_replacement=no_replacement)

    def get_material(self, material_id):
        with self._lock:
            m = Material.get(material_id)
            if not m:
                return self._fail(f"物料不存在: {material_id}")
            return self._ok(material=self._enrich_material(dict(m)),
                            replacements=Material.get_replacements(material_id) or [])

    def create_material(self, data):
        with self._lock:
            data = dict(data or {})
            if not data.get("name"):
                return self._fail("物料名称不能为空")
            slot = None
            qty = 0
            if data.get("inventory") and data.get("slot_code"):
                try:
                    qty = int(data.get("inventory") or 0)
                except (TypeError, ValueError):
                    return self._fail("入库数量无效")
                if qty > 0:
                    slot = Slot.get_by_code(data["slot_code"])
                    if not slot:
                        return self._fail(f"格位不存在: {data['slot_code']}")
            try:
                mid = Material.create(data)
            except Exception as e:  # noqa: BLE001
                return self._fail(f"创建物料失败：{e}")
            if slot:
                try:
                    InventoryService.stock_in(
                        slot["id"], mid, qty,
                        batch_no=data.get("batch_no"),
                        note="新建物料入库",
                    )
                except Exception as e:  # noqa: BLE001
                    return self._fail(f"物料已创建，但入库失败：{e}")
            return self._ok(id=mid)

    def update_material(self, material_id, data):
        with self._lock:
            m = Material.get(material_id)
            if not m:
                return self._fail(f"物料不存在: {material_id}")
            Material.update(material_id, dict(data or {}))
            return self._ok(id=material_id)

    def delete_material(self, material_id):
        with self._lock:
            m = Material.get(material_id)
            if not m:
                return self._fail(f"物料不存在: {material_id}")
            # 先移除库存记录，避免外键残留
            for inv in (Inventory.get_by_material(material_id) or []):
                Inventory.remove_from_slot(inv["slot_id"], inv["id"])
            Material.delete(material_id)
            return self._ok(id=material_id)

    def get_material_parameters(self, material_id):
        with self._lock:
            return self._ok(parameters=Material.get_parameters(material_id) or {})

    def add_replacement(self, material_id, replace_material_id, score=80, note=""):
        with self._lock:
            Material.add_replacement(material_id, replace_material_id,
                                     source="manual", score=int(score or 80),
                                     note=note or "")
            return self._ok()

    def get_replacement_map(self):
        with self._lock:
            return self._ok(replacements=Material.get_replacement_map() or [])

    def get_material_categories(self):
        return self._ok(categories=MATERIAL_CATEGORIES)

    def list_component_library(self, keyword=""):
        with self._lock:
            from models.component_lib_model import ComponentLib
            return self._ok(records=ComponentLib.all(keyword=keyword or ""),
                            total=ComponentLib.count())

    def update_component_library(self, lib_id, data):
        with self._lock:
            from models.component_lib_model import ComponentLib
            if not ComponentLib.get(lib_id):
                return self._fail(f"历史库存记录不存在: {lib_id}")
            if not ComponentLib.update(lib_id, dict(data or {})):
                return self._fail("没有可更新的字段")
            return self._ok(id=lib_id)

    def delete_component_library(self, lib_id):
        with self._lock:
            from models.component_lib_model import ComponentLib
            if not ComponentLib.delete(lib_id):
                return self._fail(f"历史库存记录不存在: {lib_id}")
            return self._ok(id=lib_id)

    # ================================================================
    # 库存
    # ================================================================
    def list_inventories(self, keyword=""):
        with self._lock:
            invs = Inventory.all(keyword=keyword or "") or []
            for i in invs:
                i["status"] = self._slot_status(i.get("quantity"),
                                                i.get("min_stock"),
                                                int(i.get("quantity") or 0) > 0)
            return self._ok(inventories=invs)

    def get_empty_slots(self):
        with self._lock:
            return self._ok(slots=Slot.get_empty_slots() or [])

    def get_slot_options(self, keyword=""):
        with self._lock:
            slots = Slot.all() or []
            keyword = (keyword or "").strip().lower()
            if keyword:
                slots = [s for s in slots if keyword in str(s.get("slot_code") or "").lower()]
            return self._ok(slots=[{"id": s["id"], "slot_code": s["slot_code"]} for s in slots])

    def stock_in(self, slot_code, material_id, quantity, batch_no=None, note=None):
        with self._lock:
            slot = Slot.get_by_code(slot_code)
            if not slot:
                return self._fail(f"格位不存在: {slot_code}")
            try:
                qty = int(quantity)
                if qty <= 0:
                    return self._fail("数量必须大于 0")
                inv_id = InventoryService.stock_in(
                    slot["id"], int(material_id), qty,
                    batch_no=batch_no, note=note)
                return self._ok(id=inv_id, slot_id=slot["id"])
            except ValueError as e:
                return self._fail(str(e))

    def stock_out(self, inv_id, quantity, note=None):
        with self._lock:
            try:
                InventoryService.stock_out(int(inv_id), int(quantity), note=note)
                return self._ok(id=inv_id)
            except Exception as e:  # noqa: BLE001
                return self._fail(str(e))

    def adjust_stock(self, inv_id, new_qty, reason=""):
        with self._lock:
            try:
                InventoryService.adjust_stock(int(inv_id), int(new_qty), reason=reason)
                return self._ok(id=inv_id)
            except Exception as e:  # noqa: BLE001
                return self._fail(str(e))

    def clear_slot(self, slot_code):
        with self._lock:
            slot = Slot.get_by_code(slot_code)
            if not slot:
                return self._fail(f"格位不存在: {slot_code}")
            InventoryService.clear_slot(slot["id"])
            return self._ok(slot_id=slot["id"])

    def remove_inventory(self, slot_code, inv_id):
        with self._lock:
            slot = Slot.get_by_code(slot_code)
            if not slot:
                return self._fail(f"格位不存在: {slot_code}")
            InventoryService.remove_single_inventory(slot["id"], int(inv_id))
            return self._ok(slot_id=slot["id"])

    def suggest_location(self, specification="", package="", category=""):
        """补货推荐存放位置"""
        with self._lock:
            suggs = Inventory.suggest_location_for_restock(
                specification=specification or None,
                package=package or None,
                category=category or None,
            ) or []
            return self._ok(suggestions=suggs)

    # ================================================================
    # BOM
    # ================================================================
    def list_bom_records(self):
        with self._lock:
            return self._ok(records=BomRecord.all() or [])

    def get_bom(self, bom_id):
        with self._lock:
            record = BomRecord.get(bom_id)
            if not record:
                return self._fail(f"BOM 不存在: {bom_id}")
            items = BomItem.list_by_bom(bom_id) or []
            return self._ok(record=record, items=items)

    def parse_bom(self, file_path, bom_type="pick", bom_name="", project_name="",
                  append_bom_id=None):
        """解析 BOM 文件并建档入库，返回 bom_id 与明细。

        append_bom_id 非空时，新解析的行**追加**到该 BOM（实现多次导入合并
        展示/匹配），否则新建 BOM。文件内与追加时均按 same_material 合并
        同物料行（相同供应商编号/相同参数），数量累加。返回聚合后的 bom_id
        与全部明细。
        """
        with self._lock:
            if not file_path or not os.path.exists(file_path):
                return self._fail(f"文件不存在: {file_path}")
            err, rows = BomImporter.parse_file(file_path)
            if err:
                return self._fail(err)
            if not rows:
                return self._fail("未能从文件中解析出有效数据行")
            rows = BomImporter.merge_rows(rows)
            name = bom_name or os.path.splitext(os.path.basename(file_path))[0]
            bom_id = None
            if append_bom_id:
                rec = BomRecord.get(append_bom_id)
                # 仅当目标 BOM 存在且操作类型一致时才合并（避免领料/补货混行）
                if rec and rec.get("bom_type") == bom_type:
                    bom_id = rec["id"]
            if bom_id is None:
                bom_id = BomRecord.create(name, project_name=project_name or "",
                                          file_path=file_path, bom_type=bom_type)
            # 追加时行号续接；与既有行同物料的直接并入（数量累加）而非新增行
            existing = BomItem.list_by_bom(bom_id) or []
            line_no = max([int(x.get("line_no") or 0) for x in existing], default=0)
            for it in rows:
                twin = next((ex for ex in existing
                             if BomImporter.same_material(ex, it)), None)
                if twin:
                    BomItem.merge_into(
                        twin["id"], int(it.get("required_qty") or 0),
                        supplier_part=BomImporter.union_parts([twin, it], "supplier_part"),
                        comment=BomImporter.union_parts([twin, it], "comment"))
                    continue
                line_no += 1
                BomItem.create(
                    bom_id,
                    line_no,
                    it.get("material_code") or "",
                    it.get("material_name") or "",
                    it.get("specification") or "",
                    it.get("package") or "",
                    int(it.get("required_qty") or 0),
                    it.get("note") or "",
                    comment=it.get("comment") or "",
                    supplier_part=it.get("supplier_part") or "",
                    footprint=it.get("footprint") or "",
                )
            items = BomItem.list_by_bom(bom_id) or []
            matched = sum(1 for x in items
                          if x.get("match_status") in ("fully", "partial", "replaced", "restock_ok"))
            BomRecord.update_status(bom_id, "pending", total_items=len(items),
                                    matched_items=matched)
            record = BomRecord.get(bom_id)
            return self._ok(bom_id=bom_id, bom_name=record.get("bom_name", "") if record else name,
                            record=record, items=items, total=len(items))

    def match_bom(self, bom_id, force=False):
        """对 BOM 全部行做库存比对，返回匹配汇总。

        读库在锁内、联网/并行匹配在锁外、落库回锁串行——
        避免 8 线程联网匹配期间阻塞其它后台操作。
        """
        import concurrent.futures as _fut
        with self._lock:
            items = BomItem.list_by_bom(bom_id) or []
            if not items:
                return self._ok(bom_id=bom_id, items=[], matched=0, total=0)
            need = list(items) if force else [
                it for it in items
                if it.get("match_status") not in ("fully", "replaced")
            ]
            pre_matched = 0 if force else len(items) - len(need)

        def _work(it):
            try:
                status, inv_id, _cands = BomMatcher.match_bom_item(it)
                return (it["id"], status, inv_id)
            except Exception:  # noqa: BLE001
                logger.exception("BOM 第 %s 行匹配异常", it.get("line_no"))
                return (it["id"], "unmatched", None)

        # 锁外并行匹配（worker 内沉淀元件库写入自带 try/except 容错）
        results = {}
        if need:
            with _fut.ThreadPoolExecutor(max_workers=8) as ex:
                for rid, status, inv_id in ex.map(_work, need):
                    results[rid] = (status, inv_id)
        # 主线程串行落库（重新取锁，避免并发写冲突）
        with self._lock:
            matched = pre_matched
            for it in need:
                rid = it["id"]
                if rid not in results:
                    continue
                status, inv_id = results[rid]
                try:
                    BomItem.update_match(rid, inv_id, status)
                except Exception:  # noqa: BLE001
                    logger.exception("BOM 第 %s 行写库异常", it.get("line_no"))
                if status in ("fully", "replaced", "partial"):
                    matched += 1
            BomRecord.update_status(bom_id, "processing", matched_items=matched)
            items = BomItem.list_by_bom(bom_id) or []
            return self._ok(bom_id=bom_id, items=items,
                            matched=matched, total=len(items))

    def confirm_pick(self, item_id, picked_qty):
        with self._lock:
            try:
                # 领料：扣除对应库存（按「剩余需求」与「库存现量」双重限幅，防重复超扣/虚领）
                it = BomItem.get(item_id)
                if not it:
                    return self._fail("BOM 明细不存在")
                req = int(it.get("required_qty") or 0)
                already = int(it.get("picked_qty") or 0)
                remaining = max(req - already, 0)
                if remaining <= 0:
                    return self._fail("该行需求已领完，请勿重复领料")
                qty = min(int(picked_qty or 0), remaining)
                if qty <= 0:
                    return self._fail("领料数量无效")
                match_status = it.get("match_status") or "unmatched"
                inv_id = it.get("matched_inventory_id")
                if match_status in ("fully", "partial") and inv_id:
                    cur_qty = int((Inventory.get(inv_id) or {}).get("quantity") or 0)
                    deduct = min(qty, cur_qty)
                    if deduct <= 0:
                        return self._fail("对应库存已为 0，无法领料，请先补货或调整匹配")
                    InventoryService.stock_out(inv_id, deduct,
                                               note=f"BOM#{it.get('bom_id')} 领料")
                    qty = deduct
                else:
                    return self._fail("该行尚未匹配到可领库存，不能确认领料")
                BomItem.confirm_pick(int(item_id), qty)
                return self._ok(id=item_id, picked=qty)
            except Exception as e:  # noqa: BLE001
                return self._fail(str(e))

    def restock_from_bom(self, bom_id, auto_location=True):
        """执行补货 BOM 入库"""
        with self._lock:
            try:
                ok, fail, skip = InventoryService.restock_from_bom(
                    int(bom_id), auto_location=auto_location)
                return self._ok(ok=ok, fail=fail, skipped=skip)
            except Exception as e:  # noqa: BLE001
                return self._fail(str(e))

    def get_restock_plan(self, bom_id):
        """返回购入 BOM 每一行的剩余数量与推荐存放格位。

        同一批补货按行顺序排布：已建议的格位传给后续行，避免全部推荐到
        A1 前仓；分类/参数优先归堆（参数相同合并、同分类+封装集中）。
        """
        with self._lock:
            record = BomRecord.get(int(bom_id))
            if not record:
                return self._fail(f"BOM 不存在: {bom_id}")
            items = BomItem.list_by_bom(int(bom_id)) or []

            def _number(value):
                text = (str(value or '').lower().replace('μ', 'u')
                        .replace('µ', 'u').replace('ω', 'ohm').replace('Ω', 'ohm'))
                match = re.search(r'([0-9]+(?:[.][0-9]+)?)\s*(pf|p|nf|n|uf|u|mf|f|kohm|k|meg|m|ohm|r)?', text)
                if not match:
                    return (999, 0.0)
                unit = (match.group(2) or '').lower()
                ranks = {'p': 0, 'pf': 0, 'n': 1, 'nf': 1, 'u': 2, 'uf': 2,
                         'μf': 2, 'mf': 3, 'f': 4, 'r': 5, 'k': 6,
                         'ohm': 5, 'kohm': 6, 'm': 7, 'meg': 7}
                return (ranks.get(unit, 8), float(match.group(1)))

            def _sort_key(item):
                text = ' '.join(str(item.get(k) or '') for k in
                                ('category', 'mat_category', 'material_name', 'comment', 'specification', 'mat_spec')).lower()
                is_capacitor = ('电容' in text or 'capacitor' in text or
                                bool(re.search(r'\d+(?:\.\d+)?\s*(pf|nf|uf|mf|f)\b', text)))
                is_resistor = 0 if ('电阻' in text or 'resistor' in text or
                                     'ohm' in text or 'kohm' in text or
                                     'ω' in text or 'Ω' in text or
                                     re.search(r'\d+(?:\.\d+)?\s*r(?:\b|[^a-z])', text)) else 1
                is_rc = 0 if (is_capacitor or is_resistor == 0) else 1
                return (is_rc, is_resistor,
                        _number(item.get('specification') or item.get('comment')),
                        str(item.get('package') or item.get('mat_pkg') or ''),
                        int(item.get('line_no') or 0))

            items = sorted(items, key=_sort_key)
            plan = []
            used_slot_ids = set()
            for item in items:
                remaining = max(0, int(item.get("required_qty") or 0) - int(item.get("picked_qty") or 0))
                material = Material.find_or_create_from_bom(item)
                material_id = material.get("id") if material else None
                stock_matches = Inventory.get_by_material(material_id) if material_id else []
                suggestions = []
                if remaining > 0 and material_id:
                    category = (item.get("category")
                                or item.get("mat_category") or "").strip()
                    suggestions = Inventory.suggest_location_for_restock(
                        specification=item.get("specification") or item.get("mat_spec"),
                        package=item.get("package") or item.get("mat_pkg"),
                        category=category or None,
                        material_id=material_id,
                        seed_key=item.get("material_code") or item.get("material_name"),
                        exclude_slot_ids=used_slot_ids,
                    ) or []
                    if suggestions:
                        used_slot_ids.add(suggestions[0]["slot_id"])
                suggested_slot_id = suggestions[0]["slot_id"] if suggestions else item.get("suggested_slot_id")
                can_restock = bool(material_id and remaining > 0 and suggested_slot_id)
                plan.append({"item": item, "remaining_qty": remaining,
                             "material_id": material_id,
                             "material_ready": bool(material_id),
                             "has_stock_match": bool(stock_matches),
                             "suggested_slot_id": suggested_slot_id,
                             "can_restock": can_restock,
                             "suggestions": suggestions[:5]})
            return self._ok(bom_id=int(bom_id), record=record, plan=plan)

    def remove_bom_items(self, bom_id, item_ids):
        """从 BOM 中删除指定行（操作完成后清除已勾选行），返回剩余明细"""
        with self._lock:
            if not bom_id or not item_ids:
                return self._fail("参数不完整")
            ids = [int(x) for x in item_ids if str(x).isdigit()]
            if not ids:
                return self._fail("未指定要移除的行")
            for iid in ids:
                try:
                    BomItem.delete_item(iid)
                except Exception:  # noqa: BLE001
                    continue
            items = BomItem.list_by_bom(int(bom_id)) or []
            matched = sum(1 for x in items
                          if x.get("match_status") in ("fully", "partial", "replaced", "restock_ok"))
            BomRecord.update_status(
                int(bom_id), "pending",
                total_items=len(items), matched_items=matched,
            )
            return self._ok(items=items)

    def _record_component_lib(self, item, material):
        """确认入库后沉淀内置元件库：供应商编号 + 立创编号 + 参数。"""
        from models.component_lib_model import ComponentLib
        sp = (item.get("supplier_part") or "").strip()
        lcsc = (item.get("material_code") or "").strip()
        has_lcsc = bool(lcsc.upper().startswith("C") and lcsc[1:].isdigit())
        mat_code = (material.get("lcsc_code") or material.get("supplier_code") or "").strip()
        if not (sp or has_lcsc or mat_code):
            return
        ComponentLib.record({
            "lcsc_code": lcsc if has_lcsc else (mat_code if mat_code.upper().startswith("C") else ""),
            "supplier_part": sp or (material.get("supplier_code") or ""),
            "model": ("" if has_lcsc else lcsc) or sp or "",
            "name": material.get("name") or "",
            "specification": material.get("specification") or "",
            "package": material.get("package") or "",
            "brand": material.get("brand") or "",
            "category": material.get("category") or "",
            "parameters": material.get("parameters") or None,
        }, source="restock-confirm")

    def confirm_restock_item(self, item_id, slot_id, quantity=None, batch_no=None, note=None):
        """确认单条购入 BOM 入库，写入库存并更新 BOM 明细。"""
        with self._lock:
            try:
                item = BomItem.get(int(item_id))
                if not item:
                    return self._fail("BOM 明细不存在")
                if not slot_id:
                    return self._fail("请选择存放格位")
                remaining = max(0, int(item.get("required_qty") or 0) - int(item.get("picked_qty") or 0))
                qty = remaining if quantity is None else int(quantity)
                if qty <= 0 or qty > remaining:
                    return self._fail(f"补货数量必须在 1 到 {remaining} 之间")
                slot = Slot.get(int(slot_id))
                if not slot:
                    return self._fail("存放格位不存在")
                material = Material.find_or_create_from_bom(item)
                if not material:
                    return self._fail("BOM 行缺少物料编码和名称，无法自动建档")
                inv_id = InventoryService.stock_in(
                    slot["id"], material["id"], qty,
                    batch_no=batch_no or f"REPLENISH-{item.get('bom_id')}",
                    note=note or f"BOM#{item.get('bom_id')} 行{item.get('line_no')} 补货入库",
                )
                # 确认入库：沉淀到内置元件库（供应商编号+参数），后续匹配优先查库
                try:
                    self._record_component_lib(item, material)
                except Exception:
                    logger.exception("内置元件库沉淀失败（不影响入库）")
                BomItem.confirm_pick(int(item_id), qty)
                new_remaining = remaining - qty
                BomItem.update_match(int(item_id), inv_id, "restock_ok", None, slot["id"])
                return self._ok(item_id=int(item_id), inventory_id=inv_id,
                                slot_code=slot["slot_code"], quantity=qty,
                                remaining_qty=new_remaining)
            except Exception as e:  # noqa: BLE001
                return self._fail(str(e))

    def delete_bom(self, bom_id):
        with self._lock:
            from models.database import get_cursor
            with get_cursor() as cur:
                cur.execute("DELETE FROM bom_items WHERE bom_id=?", (bom_id,))
                cur.execute("DELETE FROM bom_records WHERE id=?", (bom_id,))
            return self._ok(id=bom_id)

    # ================================================================
    # 报表导出
    # ================================================================
    def _export(self, func, suffix):
        os.makedirs(EXPORT_DIR, exist_ok=True)
        name = f"{suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        path = os.path.join(EXPORT_DIR, name)
        if func(path):
            return self._ok(path=path)
        return self._fail("导出失败")

    def export_inventories(self):
        with self._lock:
            return self._export(ExportService.export_inventories, "库存清单")

    def export_materials(self):
        with self._lock:
            return self._export(ExportService.export_materials, "物料主数据")

    def export_bom_picklist(self, bom_id):
        with self._lock:
            if not BomRecord.get(bom_id):
                return self._fail(f"BOM 不存在: {bom_id}")
            return self._export(
                lambda p: ExportService.export_bom_picklist(int(bom_id), p),
                f"BOM单_{bom_id}")

    # ================================================================
    # 设置
    # ================================================================
    def get_settings(self):
        with self._lock:
            return self._ok(settings=AppSettings.all())

    def save_settings(self, data):
        with self._lock:
            data = dict(data or {})
            allow = ["deepseek_api_key", "deepseek_api_base", "deepseek_model",
                     "default_min_stock", "combine_same_spec_in_slot",
                     "auto_pick_location"]
            cleaned = {k: v for k, v in data.items() if k in allow}
            # 布尔/数值类型原样保存
            if "default_min_stock" in cleaned:
                try:
                    cleaned["default_min_stock"] = int(cleaned["default_min_stock"])
                except (ValueError, TypeError):
                    cleaned["default_min_stock"] = 10
            AppSettings.update_many(cleaned)
            self._lcsc = LCSCApi()
            return self._ok(settings=AppSettings.all())

    def get_github_settings(self):
        return self._ok(settings=self._github._settings())

    def save_github_settings(self, data):
        data = dict(data or {})
        return self._github.save_configuration(
            data.get("owner"), data.get("repo"), data.get("token"),
            data.get("auto_update", True), data.get("auto_inventory", True))

    def check_github_version(self):
        return self._github.check_version()

    def update_github_version(self):
        return self._github.schedule_update()

    def check_github_inventory(self):
        return self._github.download_inventory()

    def sync_github_inventory(self):
        prefer_local = bool(AppSettings.get("inventory_clear_pending", False))
        result = self._github.sync_inventory(prefer_local=prefer_local)
        if result.get("ok") and prefer_local:
            AppSettings.set("inventory_clear_pending", False)
        return result

    def probe_github_inventory(self):
        """只读探测：云端库存标记与本地快照哈希是否不一致（是否有新库存可同步）。
        不做下载/上传，供启动时快速弹出提示。
        """
        marker = self._github._read_marker(INVENTORY_MARKER_NAME)
        if not marker.get("ok"):
            return marker
        remote = marker.get("value") or ""
        if not remote:
            return {"ok": True, "data": {"has_new": False, "reason": "no-remote-marker"}}
        try:
            local = (self._github._snapshot() or {}).get("inventory_version", "")
        except Exception:  # noqa: BLE001
            local = ""
        return {"ok": True, "data": {"has_new": remote != local,
                                     "remote": remote, "local": local}}

    def startup_probe(self):
        """启动快速检查：版本标记 + 库存标记各一次轻量 Raw 请求（只读、不做传输），并行发起。"""
        import concurrent.futures as _fut
        with _fut.ThreadPoolExecutor(max_workers=2) as _ex:
            _fv = _ex.submit(self._github.check_version)
            _fi = _ex.submit(self.probe_github_inventory)
            version = _fv.result()
            inv = _fi.result()
        vdata = version.get("data", {}) if version.get("ok") else {}
        idata = inv.get("data", {}) if inv.get("ok") else {}
        return {"ok": True, "data": {
            "version": {"ok": version.get("ok", False),
                        "available": bool(vdata.get("available")),
                        "version": vdata.get("version", ""),
                        "message": vdata.get("message", ""),
                        "error": version.get("error", "")},
            "inventory": {"ok": inv.get("ok", False),
                          "has_new": bool(idata.get("has_new")),
                          "error": inv.get("error", "")}}}

    def _clear_and_sync_empty(self, clear_func):
        with self._lock:
            clear_func()
            AppSettings.set("inventory_clear_pending", True)
        result = self._github.sync_inventory(prefer_local=True)
        if result.get("ok"):
            AppSettings.set("inventory_clear_pending", False)
            result.setdefault("data", {})["local_cleared"] = True
            return result
        result["local_cleared"] = True
        data = dict(result.get("data") or {})
        data["local_cleared"] = True
        data["cloud_sync_failed"] = True
        data["message"] = "本地数据已清空，但空库存上传失败，请稍后重试"
        return {"ok": True, "data": data}

    def clear_demo(self):
        return self._clear_and_sync_empty(purge_demo_data)

    def factory_reset(self):
        return self._clear_and_sync_empty(factory_reset)

    # ================================================================
    # 立创商城
    # ================================================================
    def search_lcsc(self, keyword, page=1, page_size=20):
        with self._lock:
            products = self._lcsc.search_product(keyword or "", page, page_size) or []
            return self._ok(products=products)

    def get_lcsc_detail(self, lcsc_code):
        with self._lock:
            detail = self._lcsc.get_product_detail(lcsc_code) or {}
            return self._ok(detail=detail)

    def compare_parameters(self, params1, params2):
        with self._lock:
            result = self._ai.compare_parameters(
                dict(params1 or {}), dict(params2 or {}))
            return self._ok(**result)

    def find_replacement_candidates(self, material_id):
        with self._lock:
            m = Material.get(material_id)
            if not m:
                return self._fail(f"物料不存在: {material_id}")
            # 先用本地匹配器找候选
            cands = self._matcher.find_candidates_from_db(dict(m)) or []
            # 如果有 AI 可用，用 AI 重新排序
            if self._ai.is_available() and cands:
                cands = self._ai.find_replacements(dict(m), cands) or cands
            return self._ok(candidates=cands)

    def get_ai_status(self):
        """返回 AI 服务状态"""
        return self._ok(available=self._ai.is_available())

    def ai_match_bom(self, bom_id):
        """使用 DeepSeek AI 对 BOM 进行智能匹配"""
        with self._lock:
            items = BomItem.list_by_bom(int(bom_id)) or []
            mats = Material.all() or []
        if not items:
            return self._fail("BOM 无明细")
        if not mats:
            return self._fail("本地无物料数据，请先录入物料")
        enriched_mats = [self._enrich_material(dict(m)) for m in mats]
        ai = self._ai
        # AI 网络调用不占用全局锁
        result = ai.match_bom_items(items, enriched_mats)
        if result.get("offline"):
            # AI 不可用，回退到本地匹配
            return self._fail("AI 服务不可用，请使用本地匹配")
        # 更新匹配状态
        with self._lock:
            for r in result.get("results", []):
                idx = r.get("bom_index")
                mat_id = r.get("matched_material_id")
                # JSON 解析后可能为 float，统一转 int
                try:
                    idx = int(idx) if idx is not None else None
                except (ValueError, TypeError):
                    idx = None
                try:
                    mat_id = int(mat_id) if mat_id else None
                except (ValueError, TypeError):
                    mat_id = None
                if idx is not None and 0 <= idx < len(items):
                    item = items[idx]
                    if mat_id:
                        invs = Inventory.get_by_material(mat_id) or []
                        inv_id = invs[0]["id"] if invs else None
                        status = "fully" if r.get("confidence", 0) >= 80 else "partial"
                        BomItem.update_match(item["id"], inv_id, status)
                    else:
                        BomItem.update_match(item["id"], None, "unmatched")
            items = BomItem.list_by_bom(int(bom_id)) or []
        return self._ok(items=items, offline=result.get("offline", False))

    # ================================================================
    # 操作日志
    # ================================================================
    def list_operations(self, limit=50, op_type=None):
        with self._lock:
            return self._ok(operations=OperationLog.list_recent(
                limit=int(limit or 50), op_type=op_type or None) or [])

    # ================================================================
    # 连接自检（页面加载后由桥接调用）
    # ================================================================
    def ping(self):
        return "pong"
