# -*- coding: utf-8 -*-
"""物料模型"""
import json
from datetime import datetime

from utils import PART_SEP_RE, split_part_numbers, is_lcsc_code, list_contains_sql

from .database import get_cursor


class Material:
    """物料数据访问类"""

    @staticmethod
    def all(keyword="", category=""):
        """查询物料列表，支持关键字和分类筛选"""
        sql = "SELECT * FROM materials WHERE 1=1"
        params = []
        if keyword:
            sql += (" AND (name LIKE ? OR material_code LIKE ? OR specification LIKE ?"
                    " OR lcsc_code LIKE ? OR supplier_code LIKE ?)")
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw, kw, kw])
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY update_time DESC"
        with get_cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def get(material_id):
        """按ID获取物料"""
        with get_cursor() as cur:
            cur.execute("SELECT * FROM materials WHERE id=?", (material_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def get_by_code(code):
        """按物料编码获取；精确未命中且本身是单编号时，
        兼容历史"逗号拼接编码"（列表包含），避免完整编号反而查不到"""
        with get_cursor() as cur:
            cur.execute("SELECT * FROM materials WHERE material_code=?", (code,))
            row = cur.fetchone()
            if row:
                return dict(row)
            if code and not PART_SEP_RE.search(str(code)):
                cur.execute(
                    "SELECT * FROM materials WHERE "
                    + list_contains_sql("material_code"), (f"%,{code},%",))
                row = cur.fetchone()
                return dict(row) if row else None
            return None

    @staticmethod
    def get_by_lcsc(lcsc_code):
        """按立创编号获取"""
        with get_cursor() as cur:
            cur.execute("SELECT * FROM materials WHERE lcsc_code=?", (lcsc_code,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def find_or_create_from_bom(item):
        """BOM 行物料兜底：编码查找 → 名称精确匹配 → 都没有则自动建档，返回物料 dict 或 None
        名称回退顺序：material_name → comment → supplier_part；
        编码回退顺序：material_code → supplier_part（同时记入 supplier_code）。
        ponytail: 编码/编号可能含多个备选值（逗号分隔），逐编号查找；
        建档时 material_code 只取首个编号，避免历史"逗号拼接编码"导致
        精确搜索与 BOM 编号匹配失效。
        """
        material = None
        code = (item.get("material_code") or "").strip()
        sp = (item.get("supplier_part") or "").strip()
        name = (item.get("material_name") or "").strip() \
            or (item.get("comment") or "").strip() \
            or sp
        if not code and sp:
            code = sp
        tokens = split_part_numbers(code)
        for token in tokens:
            material = Material.get_by_code(token)
            if not material and is_lcsc_code(token):
                material = Material.get_by_lcsc(token.upper())
            if material:
                break
        if not material and name:
            for cand in Material.all(keyword=name):
                if cand.get("name") == name:
                    material = cand
                    break
        if material:
            return material
        if not code and not name:
            return None
        lcsc = next((t.upper() for t in tokens if is_lcsc_code(t)), "")
        data = {
            "material_code": tokens[0] if tokens
            else ("AUTO-" + datetime.now().strftime("%Y%m%d%H%M%S%f")),
            "name": name or code,
            "specification": item.get("specification"),
            "package": item.get("package"),
            "category": item.get("category"),
            "supplier_code": sp or "",
            "lcsc_code": lcsc or "",
        }
        try:
            return Material.get(Material.create(data))
        except Exception:
            # 编码冲突等异常时回退为按编码查询（可能已被其他流程建档）
            return Material.get_by_code(data["material_code"]) if code else None

    @staticmethod
    def create(data):
        """创建物料"""
        fields = ["material_code", "name", "category", "specification", "package",
                  "supplier_code", "lcsc_code", "brand", "unit", "min_stock",
                  "description", "datasheet_url", "parameters"]
        placeholders = ", ".join(["?"] * len(fields))
        field_sql = ", ".join(fields)
        values = [data.get(f) for f in fields]
        if isinstance(values[-1], dict):
            values[-1] = json.dumps(values[-1], ensure_ascii=False)
        with get_cursor() as cur:
            cur.execute(f"INSERT INTO materials ({field_sql}) VALUES ({placeholders})", values)
            return cur.lastrowid

    @staticmethod
    def update(material_id, data):
        """更新物料"""
        fields = ["material_code", "name", "category", "specification", "package",
                  "supplier_code", "lcsc_code", "brand", "unit", "min_stock",
                  "description", "datasheet_url", "parameters"]
        sets = []
        values = []
        for f in fields:
            if f in data:
                sets.append(f"{f}=?")
                v = data[f]
                if f == "parameters" and isinstance(v, dict):
                    v = json.dumps(v, ensure_ascii=False)
                values.append(v)
        if sets:
            sets.append("update_time=CURRENT_TIMESTAMP")
            values.append(material_id)
            with get_cursor() as cur:
                cur.execute(f"UPDATE materials SET {', '.join(sets)} WHERE id=?", values)

    @staticmethod
    def delete(material_id):
        """删除物料"""
        with get_cursor() as cur:
            cur.execute("DELETE FROM materials WHERE id=?", (material_id,))

    @staticmethod
    def get_parameters(material_id):
        """获取参数dict"""
        m = Material.get(material_id)
        if m and m.get("parameters"):
            try:
                return json.loads(m["parameters"])
            except Exception:
                return {}
        return {}

    @staticmethod
    def find_replacement_candidates(specification=None, package=None, category=None):
        """根据规格/封装/分类查找可能的替代物料"""
        sql = "SELECT * FROM materials WHERE 1=1"
        params = []
        if specification:
            sql += " AND specification LIKE ?"
            params.append(f"%{specification}%")
        if package:
            sql += " AND package = ?"
            params.append(package)
        if category:
            sql += " AND category = ?"
            params.append(category)
        with get_cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def get_replacement_map():
        """获取所有替换映射"""
        with get_cursor() as cur:
            cur.execute("""
                SELECT mr.*, m1.name as mat_name, m2.name as rep_name,
                       m1.specification as mat_spec, m2.specification as rep_spec
                FROM material_replacements mr
                LEFT JOIN materials m1 ON mr.material_id = m1.id
                LEFT JOIN materials m2 ON mr.replace_material_id = m2.id
                ORDER BY mr.match_score DESC
            """)
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def add_replacement(material_id, replace_material_id, source="manual", score=80, note=""):
        """添加替换关系"""
        with get_cursor() as cur:
            try:
                cur.execute("""
                    INSERT OR IGNORE INTO material_replacements
                    (material_id, replace_material_id, source, match_score, note)
                    VALUES (?, ?, ?, ?, ?)
                """, (material_id, replace_material_id, source, score, note))
            except Exception:
                pass

    @staticmethod
    def get_replacements(material_id):
        """获取指定物料的替换物料"""
        with get_cursor() as cur:
            cur.execute("""
                SELECT m.*, mr.match_score, mr.source, mr.note as rep_note
                FROM material_replacements mr
                JOIN materials m ON mr.replace_material_id = m.id
                WHERE mr.material_id = ?
                ORDER BY mr.match_score DESC
            """, (material_id,))
            return [dict(r) for r in cur.fetchall()]
