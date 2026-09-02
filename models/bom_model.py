# -*- coding: utf-8 -*-
"""BOM模型"""
from .database import get_cursor


class BomRecord:
    """BOM导入记录"""

    @staticmethod
    def all():
        with get_cursor() as cur:
            cur.execute("SELECT * FROM bom_records ORDER BY create_time DESC")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def get(bom_id):
        with get_cursor() as cur:
            cur.execute("SELECT * FROM bom_records WHERE id=?", (bom_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def create(bom_name, project_name="", file_path="", bom_type="pick"):
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO bom_records (bom_name, project_name, file_path, bom_type)
                VALUES (?, ?, ?, ?)
            """, (bom_name, project_name, file_path, bom_type))
            return cur.lastrowid

    @staticmethod
    def update_status(bom_id, status, **kwargs):
        fields = ["status = ?"]
        values = [status]
        if "matched_items" in kwargs:
            fields.append("matched_items = ?")
            values.append(kwargs["matched_items"])
        if "total_items" in kwargs:
            fields.append("total_items = ?")
            values.append(kwargs["total_items"])
        if status == "completed":
            fields.append("complete_time = CURRENT_TIMESTAMP")
        values.append(bom_id)
        with get_cursor() as cur:
            cur.execute(f"UPDATE bom_records SET {', '.join(fields)} WHERE id=?", values)


class BomItem:
    """BOM明细项"""

    @staticmethod
    def list_by_bom(bom_id):
        with get_cursor() as cur:
            cur.execute("""
                SELECT bi.*, s.slot_code, s2.slot_code as suggested_slot_code,
                       i.quantity as inventory_quantity,
                       m.name as mat_name, m.specification as mat_spec,
                       m.package as mat_pkg, m.description as mat_desc,
                       m.brand as mat_brand, m.category as mat_category,
                       rm.name as rep_name, rm.specification as rep_spec
                FROM bom_items bi
                LEFT JOIN inventories i ON bi.matched_inventory_id = i.id
                LEFT JOIN slots s ON i.slot_id = s.id
                LEFT JOIN slots s2 ON bi.suggested_slot_id = s2.id
                LEFT JOIN materials m ON i.material_id = m.id
                LEFT JOIN materials rm ON bi.replace_material_id = rm.id
                WHERE bi.bom_id = ?
                ORDER BY bi.line_no
            """, (bom_id,))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def get(item_id):
        with get_cursor() as cur:
            cur.execute("SELECT * FROM bom_items WHERE id=?", (item_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def create(bom_id, line_no, material_code, material_name, specification,
               package, required_qty, note="", comment=None,
               supplier_part=None, footprint=None):
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO bom_items (bom_id, line_no, material_code, material_name,
                    specification, package, required_qty, note,
                    comment, supplier_part, footprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (bom_id, line_no, material_code, material_name,
                  specification, package, required_qty, note,
                  comment, supplier_part, footprint))
            return cur.lastrowid

    @staticmethod
    def update_match(item_id, matched_inventory_id=None, match_status="unmatched",
                     replace_material_id=None, suggested_slot_id=None):
        with get_cursor() as cur:
            fields = ["matched_inventory_id=?", "match_status=?", "replace_material_id=?"]
            args = [matched_inventory_id, match_status, replace_material_id]
            if suggested_slot_id is not None:
                fields.append("suggested_slot_id=?")
                args.append(suggested_slot_id)
            args.append(item_id)
            cur.execute(f"""
                UPDATE bom_items SET {', '.join(fields)} WHERE id=?
            """, args)

    @staticmethod
    def merge_into(item_id, add_qty, supplier_part=None, comment=None):
        """追加导入时并入既有行：数量累加、编号/备注取并集（调用方先算好），
        并重置匹配状态，待按新数量重新匹配库存"""
        with get_cursor() as cur:
            cur.execute("""
                UPDATE bom_items
                SET required_qty = required_qty + ?,
                    supplier_part = ?,
                    comment = ?,
                    match_status = 'unmatched',
                    matched_inventory_id = NULL
                WHERE id = ?
            """, (add_qty, supplier_part, comment, item_id))

    @staticmethod
    def confirm_pick(item_id, picked_qty):
        """记录已处理数量：领料=已领取，补货=已入库；get_restock_plan 依此计算剩余量"""
        with get_cursor() as cur:
            cur.execute("""
                UPDATE bom_items SET picked_qty = picked_qty + ? WHERE id=?
            """, (picked_qty, item_id))
            # 更新状态
            cur.execute("SELECT required_qty, picked_qty FROM bom_items WHERE id=?", (item_id,))
            row = cur.fetchone()
            if row:
                req, picked = row[0], row[1]
                if picked >= req:
                    new_status = "fully"
                elif picked > 0:
                    new_status = "partial"
                else:
                    new_status = "unmatched"
                cur.execute("UPDATE bom_items SET match_status=? WHERE id=?", (new_status, item_id))

    @staticmethod
    def delete_item(item_id):
        """删除单个 BOM 行（操作完成后清除已勾选行）"""
        with get_cursor() as cur:
            cur.execute("DELETE FROM bom_items WHERE id=?", (item_id,))
            return cur.rowcount > 0


class OperationLog:
    """操作日志"""

    @staticmethod
    def add(op_type, target_type=None, target_id=None, detail=None):
        import json
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO operation_logs (operation_type, target_type, target_id, detail)
                VALUES (?, ?, ?, ?)
            """, (op_type, target_type, target_id, detail))

    @staticmethod
    def list_recent(limit=200, op_type=None):
        sql = "SELECT * FROM operation_logs"
        params = []
        if op_type:
            sql += " WHERE operation_type = ?"
            params.append(op_type)
        sql += " ORDER BY create_time DESC LIMIT ?"
        params.append(limit)
        with get_cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
