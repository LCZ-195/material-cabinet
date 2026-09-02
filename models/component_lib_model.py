# -*- coding: utf-8 -*-
"""内置元件库：立创联网匹配确认后自动沉淀供应商编号与参数，
后续 BOM 匹配优先查本库，未命中再联网，减少重复网络请求。"""
import json
from .database import get_cursor


class ComponentLib:
    """内置元件库（component_library 表）"""

    @staticmethod
    def record(item, source="lcsc"):
        """沉淀/更新一条元件记录。以 lcsc_code 优先去重，其次 supplier_part。
        item 可含: lcsc_code, supplier_part, model, name, specification,
                   package, footprint, brand, category, parameters(dict), datasheet
        返回记录 id（新建或更新后）。
        """
        lcsc = (item.get("lcsc_code") or "").strip()
        spart = (item.get("supplier_part") or "").strip()
        model = (item.get("model") or "").strip()
        if not (lcsc or spart or model):
            return None
        params = item.get("parameters")
        if isinstance(params, dict):
            params = json.dumps(params, ensure_ascii=False)
        data = {
            "lcsc_code": lcsc, "supplier_part": spart, "model": model,
            "name": (item.get("name") or "").strip(),
            "specification": (item.get("specification") or "").strip(),
            "package": (item.get("package") or "").strip(),
            "footprint": (item.get("footprint") or "").strip(),
            "brand": (item.get("brand") or "").strip(),
            "category": (item.get("category") or "").strip(),
            "parameters": params or None,
            "datasheet": (item.get("datasheet") or "").strip(),
            "source": source,
        }
        with get_cursor() as cur:
            # 去重定位：lcsc_code → supplier_part → model+package
            row = None
            if lcsc:
                cur.execute("SELECT id FROM component_library WHERE lcsc_code=?", (lcsc,))
                row = cur.fetchone()
            if not row and spart:
                cur.execute("SELECT id FROM component_library WHERE supplier_part=?", (spart,))
                row = cur.fetchone()
            if not row and model:
                cur.execute("""SELECT id FROM component_library
                               WHERE model=? AND COALESCE(package,'')=COALESCE(?, '')""",
                            (model, data["package"]))
                row = cur.fetchone()
            if row:
                lib_id = row[0]
                sets, args = [], []
                for k, v in data.items():
                    if v:
                        sets.append(f"{k}=?")
                        args.append(v)
                if sets:
                    sets.append("update_time=CURRENT_TIMESTAMP")
                    args.append(lib_id)
                    cur.execute(f"UPDATE component_library SET {', '.join(sets)} WHERE id=?", args)
                return lib_id
            cur.execute("""
                INSERT INTO component_library
                    (lcsc_code, supplier_part, model, name, specification, package,
                     footprint, brand, category, parameters, datasheet, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (data["lcsc_code"], data["supplier_part"], data["model"], data["name"],
                  data["specification"], data["package"], data["footprint"], data["brand"],
                  data["category"], data["parameters"], data["datasheet"], source))
            return cur.lastrowid

    @staticmethod
    def bump_hit(lib_id):
        """命中计数 +1"""
        with get_cursor() as cur:
            cur.execute("""UPDATE component_library
                           SET hit_count = hit_count + 1 WHERE id=?""", (lib_id,))

    @staticmethod
    def get(lib_id):
        with get_cursor() as cur:
            cur.execute("SELECT * FROM component_library WHERE id=?", (lib_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def find_by_code(supplier_part=None, lcsc_code=None):
        """按供应商编号 / 立创编号精确查找"""
        with get_cursor() as cur:
            if lcsc_code:
                cur.execute("SELECT * FROM component_library WHERE lcsc_code=?",
                            (lcsc_code.strip(),))
                row = cur.fetchone()
                if row:
                    return dict(row)
            if supplier_part:
                cur.execute("SELECT * FROM component_library WHERE supplier_part=? OR model=?",
                            (supplier_part.strip(), supplier_part.strip()))
                row = cur.fetchone()
                if row:
                    return dict(row)
        return None

    @staticmethod
    def search(keyword, limit=10):
        """关键词模糊搜索（型号/供应商编号/描述/规格）"""
        kw = (keyword or "").strip()
        if not kw:
            return []
        like = f"%{kw}%"
        with get_cursor() as cur:
            cur.execute("""
                SELECT * FROM component_library
                WHERE model LIKE ? OR supplier_part LIKE ? OR lcsc_code LIKE ?
                   OR name LIKE ? OR specification LIKE ?
                ORDER BY hit_count DESC, update_time DESC LIMIT ?
            """, (like, like, like, like, like, limit))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def search_by_spec(specification=None, package=None, limit=10):
        """按规格+封装查找（封装不同但参数相同也可命中）。

        ponytail: OR 宽召回是记忆库末级兜底的刻意简化（宁多勿漏，命中后仍按
        hit_count 排序）；误命中变多时升级为 AND 优先 + OR 兜底两段式。
        """
        conds, args = [], []
        if specification:
            conds.append("specification LIKE ?")
            args.append(f"%{specification}%")
        if package:
            conds.append("package LIKE ?")
            args.append(f"%{package}%")
        if not conds:
            return []
        with get_cursor() as cur:
            cur.execute(f"""
                SELECT * FROM component_library WHERE {' OR '.join(conds)}
                ORDER BY hit_count DESC, update_time DESC LIMIT ?
            """, (*args, limit))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def all(keyword="", limit=200):
        kw = f"%{(keyword or '').strip()}%"
        with get_cursor() as cur:
            cur.execute("""
                SELECT * FROM component_library
                WHERE model LIKE ? OR supplier_part LIKE ? OR name LIKE ?
                   OR lcsc_code LIKE ? OR ? = '%%'
                ORDER BY update_time DESC LIMIT ?
            """, (kw, kw, kw, kw, kw, limit))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def update(lib_id, data):
        allowed = {
            "lcsc_code", "supplier_part", "model", "name", "specification",
            "package", "footprint", "brand", "category", "parameters",
            "datasheet", "source",
        }
        values = {}
        for key in allowed:
            if key not in data:
                continue
            value = data[key]
            if key == "parameters" and isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False)
            values[key] = value.strip() if isinstance(value, str) else value
        if not values:
            return False
        sets = [f"{key}=?" for key in values]
        args = list(values.values())
        sets.append("update_time=CURRENT_TIMESTAMP")
        args.append(lib_id)
        with get_cursor() as cur:
            cur.execute(
                f"UPDATE component_library SET {', '.join(sets)} WHERE id=?",
                args,
            )
            return cur.rowcount > 0

    @staticmethod
    def delete(lib_id):
        with get_cursor() as cur:
            cur.execute("DELETE FROM component_library WHERE id=?", (lib_id,))
            return cur.rowcount > 0

    @staticmethod
    def count():
        with get_cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM component_library")
            return cur.fetchone()[0]
