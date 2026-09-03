# -*- coding: utf-8 -*-
"""格位与库存模型（支持单格多物料）"""
from .database import get_cursor, AppSettings
from config import CABINET_ROWS, CABINET_COLS, SLOTS_PER_CELL
from utils import split_part_numbers, list_contains_sql


class Slot:
    """格位数据访问类"""

    @staticmethod
    def all():
        """获取所有格位 + 聚合的库存信息（多物料时只显示主物料 + 计数）"""
        with get_cursor() as cur:
            cur.execute("SELECT * FROM slots ORDER BY row, col, position")
            slots = [dict(r) for r in cur.fetchall()]
            # 为每个格位查所有库存项
            slot_ids = [s["id"] for s in slots]
            if not slot_ids:
                return slots
            placeholders = ",".join(["?"] * len(slot_ids))
            cur.execute(f"""
                SELECT i.*, m.name as material_name, m.category, m.specification,
                       m.package, m.material_code, m.lcsc_code, m.min_stock,
                       m.brand, m.unit
                FROM inventories i
                JOIN materials m ON i.material_id = m.id
                WHERE i.slot_id IN ({placeholders}) AND i.quantity > 0
                ORDER BY i.slot_id, i.quantity DESC
            """, slot_ids)
            invs_by_slot = {}
            for r in cur.fetchall():
                invs_by_slot.setdefault(r["slot_id"], []).append(dict(r))
            for s in slots:
                lst = invs_by_slot.get(s["id"], [])
                if lst:
                    # 取第一个为主显示
                    main = lst[0]
                    s["inv_id"] = main["id"]
                    s["material_id"] = main["material_id"]
                    s["quantity"] = main["quantity"]
                    s["batch_no"] = main.get("batch_no")
                    s["material_name"] = main.get("material_name", "")
                    s["category"] = main.get("category", "")
                    s["specification"] = main.get("specification", "")
                    s["package"] = main.get("package", "")
                    s["material_code"] = main.get("material_code", "")
                    s["lcsc_code"] = main.get("lcsc_code", "")
                    s["min_stock"] = main.get("min_stock", 10)
                    s["unit"] = main.get("unit", "个")
                    # 多物料信息
                    s["multi_count"] = len(lst)
                    s["total_quantity"] = sum(int(i["quantity"] or 0) for i in lst)
                    s["all_inventories"] = lst
                else:
                    s["multi_count"] = 0
                    s["total_quantity"] = 0
                    s["all_inventories"] = []
            return slots

    @staticmethod
    def get(slot_id):
        with get_cursor() as cur:
            cur.execute("SELECT * FROM slots WHERE id=?", (slot_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def get_by_code(slot_code):
        with get_cursor() as cur:
            cur.execute("SELECT * FROM slots WHERE slot_code=?", (slot_code,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def get_cabinet_grid():
        """返回按行列组织的嵌套结构: grid[row][col][position]"""
        slots = Slot.all()
        grid = [[[None] * SLOTS_PER_CELL for _ in range(CABINET_COLS)] for _ in range(CABINET_ROWS)]
        for s in slots:
            r, c, p = s["row"], s["col"], s["position"]
            grid[r][c][p] = s
        return grid

    @staticmethod
    def get_empty_slots():
        """获取真正的空格位（无任何库存记录或数量0）"""
        with get_cursor() as cur:
            cur.execute("""
                SELECT s.* FROM slots s
                LEFT JOIN inventories i ON s.id = i.slot_id AND i.quantity > 0
                GROUP BY s.id
                HAVING COUNT(i.id) = 0
            """)
            return [dict(r) for r in cur.fetchall()]


class Inventory:
    """库存数据访问类（支持单格多物料）"""

    # E 行（row=4，0 起）为正常仓位，自动分配建议同样参与
    RESERVED_MAX_ROW = 4

    @staticmethod
    def all(keyword=""):
        """查询所有库存条目"""
        sql = """
            SELECT i.*, s.slot_code, s.row, s.col, s.position,
                   m.name as material_name, m.category, m.specification, m.package,
                   m.material_code, m.supplier_code, m.lcsc_code, m.brand, m.unit, m.min_stock
            FROM inventories i
            JOIN slots s ON i.slot_id = s.id
            LEFT JOIN materials m ON i.material_id = m.id
            WHERE 1=1
        """
        params = []
        if keyword:
            sql += (" AND (m.name LIKE ? OR m.material_code LIKE ? OR s.slot_code LIKE ?"
                    " OR m.lcsc_code LIKE ? OR m.specification LIKE ?"
                    " OR m.supplier_code LIKE ?)")
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw, kw, kw, kw])
        sql += " ORDER BY s.row, s.col, s.position, i.quantity DESC"
        with get_cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def get(inv_id):
        with get_cursor() as cur:
            cur.execute("""
                SELECT i.*, s.slot_code, s.row, s.col, s.position,
                       m.name as material_name, m.category, m.specification, m.package,
                       m.material_code, m.supplier_code, m.lcsc_code, m.brand, m.unit, m.min_stock
                FROM inventories i
                JOIN slots s ON i.slot_id = s.id
                LEFT JOIN materials m ON i.material_id = m.id
                WHERE i.id=?
            """, (inv_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def list_by_slot(slot_id):
        """获取指定格位的所有库存条目（多物料）"""
        with get_cursor() as cur:
            cur.execute("""
                SELECT i.*, s.slot_code, m.name as material_name, m.category,
                       m.specification, m.package, m.material_code, m.supplier_code,
                       m.lcsc_code, m.brand, m.unit, m.min_stock
                FROM inventories i
                JOIN slots s ON i.slot_id = s.id
                LEFT JOIN materials m ON i.material_id = m.id
                WHERE i.slot_id = ?
                ORDER BY i.quantity DESC
            """, (slot_id,))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def get_by_slot(slot_id):
        """兼容旧接口：只返回格位中数量最大的一条（若没有则新建空记录模式）"""
        lst = Inventory.list_by_slot(slot_id)
        # 过滤掉有物料但数量0的（除非完全没物料）
        with_qty = [x for x in lst if int(x.get("quantity") or 0) > 0 and x.get("material_id")]
        if with_qty:
            return with_qty[0]
        if lst:
            return lst[0]
        return None

    @staticmethod
    def get_by_material(material_id):
        """查找物料所在的所有格位"""
        with get_cursor() as cur:
            cur.execute("""
                SELECT i.*, s.slot_code, s.row, s.col, s.position,
                       m.name as material_name, m.unit, m.min_stock
                FROM inventories i
                JOIN slots s ON i.slot_id = s.id
                JOIN materials m ON i.material_id = m.id
                WHERE i.material_id=? AND i.quantity > 0
                ORDER BY i.quantity DESC
            """, (material_id,))
            return [dict(r) for r in cur.fetchall()]

    # ---------- 单格多物料：写入 ----------
    @staticmethod
    def add_inventory_to_slot(slot_id, material_id, quantity,
                              batch_no=None, note=None):
        """向格位追加一条物料库存（若该格已存在同物料则加量）"""
        if not material_id:
            return None
        with get_cursor() as cur:
            cur.execute("""
                SELECT id, quantity FROM inventories
                WHERE slot_id=? AND material_id=? AND quantity > 0
                LIMIT 1
            """, (slot_id, material_id))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE inventories SET
                        quantity = quantity + ?,
                        batch_no = COALESCE(?, batch_no),
                        note = COALESCE(?, note),
                        update_time = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (quantity, batch_no, note, row["id"]))
                return row["id"]
            else:
                # 先把 slot 里"空占位"记录清掉
                cur.execute("""
                    DELETE FROM inventories
                    WHERE slot_id=? AND (material_id IS NULL OR quantity = 0)
                """, (slot_id,))
                cur.execute("""
                    INSERT INTO inventories (slot_id, material_id, quantity, batch_no, note)
                    VALUES (?, ?, ?, ?, ?)
                """, (slot_id, material_id, quantity, batch_no, note))
                return cur.lastrowid

    @staticmethod
    def set_stock(slot_id, material_id, quantity, batch_no=None, note=None):
        """设置库存（兼容性：单物料模式覆盖）"""
        if material_id is None or quantity <= 0:
            # 清空
            with get_cursor() as cur:
                cur.execute("DELETE FROM inventories WHERE slot_id=?", (slot_id,))
                if material_id and quantity > 0:
                    cur.execute("""
                        INSERT INTO inventories (slot_id, material_id, quantity, batch_no, note)
                        VALUES (?, ?, ?, ?, ?)
                    """, (slot_id, material_id, quantity, batch_no, note))
                    return cur.lastrowid
                return None
        return Inventory.add_inventory_to_slot(slot_id, material_id, quantity, batch_no, note)

    @staticmethod
    def update_quantity(inv_id, new_qty):
        with get_cursor() as cur:
            cur.execute("""
                UPDATE inventories SET quantity=?, update_time=CURRENT_TIMESTAMP WHERE id=?
            """, (new_qty, inv_id))

    @staticmethod
    def add_stock(inv_id, delta):
        with get_cursor() as cur:
            cur.execute("""
                UPDATE inventories
                SET quantity = MAX(0, quantity + ?),
                    update_time = CURRENT_TIMESTAMP
                WHERE id=?
            """, (delta, inv_id))

    @staticmethod
    def deduct_stock(inv_id, quantity):
        """条件扣减：余额足够才扣（单条 UPDATE 原子完成，防先查后扣的并发超扣）"""
        with get_cursor() as cur:
            cur.execute("""
                UPDATE inventories
                SET quantity = quantity - ?,
                    update_time = CURRENT_TIMESTAMP
                WHERE id=? AND quantity >= ?
            """, (quantity, inv_id, quantity))
            return cur.rowcount > 0

    @staticmethod
    def remove_from_slot(slot_id, inv_id=None):
        """移除指定库存条目（或清空格位所有）"""
        with get_cursor() as cur:
            if inv_id:
                cur.execute("DELETE FROM inventories WHERE id=? AND slot_id=?", (inv_id, slot_id))
            else:
                cur.execute("DELETE FROM inventories WHERE slot_id=?", (slot_id,))

    # ---------- 搜索：领料BOM用（分级匹配，逐级放宽） ----------
    @staticmethod
    def search_for_bom(material_code=None, specification=None, package=None, name=None,
                       supplier_part=None, lcsc_code=None):
        """BOM 行库存匹配：按优先级逐级放宽，返回最优先一级的命中结果
        0. 供应商编号精确（lcsc_code / supplier_code） → 1. 物料编码精确
        → 2. 编码模糊 → 3. 规格+封装 → 4. 名称 → 5. 任一字段 OR 兜底
        """
        base_sql = """
            SELECT i.*, s.slot_code, m.material_code, m.name as material_name,
                   m.specification, m.package, m.category
            FROM inventories i
            JOIN slots s ON i.slot_id = s.id
            JOIN materials m ON i.material_id = m.id
            WHERE i.quantity > 0
        """

        def _run(conds, params):
            if not conds:
                return []
            sql = base_sql + " AND (" + " AND ".join(conds) + ") ORDER BY i.quantity DESC"
            with get_cursor() as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

        def _run_or(or_conds, params):
            if not or_conds:
                return []
            sql = base_sql + " AND (" + " OR ".join(or_conds) + ") ORDER BY i.quantity DESC"
            with get_cursor() as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

        # 0) 供应商编号（lcsc_code 优先，其次 supplier_code；C 编号同时匹配 lcsc_code）。
        #    ponytail: 一格可能存多个备选编号（历史逗号拼接），逐编号匹配并
        #    兼容"编号列表包含"形态，避免完整编号反而搜不到。
        if lcsc_code:
            lc = lcsc_code.strip().upper()
            rows = _run_or(
                ["UPPER(m.lcsc_code) = ?", list_contains_sql("m.lcsc_code"),
                 list_contains_sql("m.supplier_code")],
                [lc, f"%,{lc},%", f"%,{lc},%"])
            if rows:
                return rows
        for sp in split_part_numbers(supplier_part):
            if sp.upper().startswith("C"):
                rows = _run_or(
                    ["UPPER(m.supplier_code) = ?", "UPPER(m.lcsc_code) = ?",
                     list_contains_sql("m.supplier_code"),
                     list_contains_sql("m.lcsc_code")],
                    [sp, sp.upper(), f"%,{sp},%", f"%,{sp.upper()},%"])
            else:
                rows = _run_or(
                    ["UPPER(m.supplier_code) = UPPER(?)",
                     list_contains_sql("m.supplier_code")],
                    [sp, f"%,{sp},%"])
            if rows:
                return rows
        # 1) 编码精确（大小写不敏感，与 LIKE 层口径一致）
        if material_code:
            rows = _run(["UPPER(m.material_code) = UPPER(?)"], [material_code])
            if rows:
                return rows
        # 2) 编码模糊
        if material_code:
            rows = _run(["m.material_code LIKE ?"], [f"%{material_code}%"])
            if rows:
                return rows
        # 3) 规格 + 封装（同时给出才要求两者）
        spec_conds, spec_params = [], []
        if specification:
            spec_conds.append("m.specification LIKE ?")
            spec_params.append(f"%{specification}%")
        if package:
            spec_conds.append("m.package LIKE ?")
            spec_params.append(f"%{package}%")
        if spec_conds:
            rows = _run(spec_conds, spec_params)
            if rows:
                return rows
        # 4) 名称
        if name:
            rows = _run(["m.name LIKE ?"], [f"%{name}%"])
            if rows:
                return rows
        # 5) OR 兜底（任一字段命中即可）
        or_conds, or_params = [], []
        if material_code:
            or_conds.append("m.material_code LIKE ?")
            or_params.append(f"%{material_code}%")
        if specification:
            or_conds.append("m.specification LIKE ?")
            or_params.append(f"%{specification}%")
        if package:
            or_conds.append("m.package LIKE ?")
            or_params.append(f"%{package}%")
        if name:
            or_conds.append("m.name LIKE ?")
            or_params.append(f"%{name}%")
        return _run_or(or_conds, or_params)

    # ---------- 搜索：补货BOM - 智能建议存放位置 ----------
    # 封装尺寸排序（学习立创商城 BOM 排序：同单位按大小相邻，整体由小到大）
    PKG_SIZE_ORDER = [
        "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512",
        "SOD-123", "SOD-323", "SOT-23", "SOT-89", "SOT-223", "TO-92", "TO-220",
        "DIP-4", "DIP-8", "DIP-14", "DIP-16", "SOIC-8", "SOIC-14", "SOIC-16",
        "QFN", "QFP", "BGA",
    ]

    @staticmethod
    def _pkg_rank(pkg):
        """封装尺寸排序位次（小封装靠前）；未知封装排最后"""
        p = (pkg or "").upper().strip().replace(" ", "")
        for i, k in enumerate(Inventory.PKG_SIZE_ORDER):
            if k in p:
                return i
        return len(Inventory.PKG_SIZE_ORDER)

    @staticmethod
    def _is_tht(pkg):
        """是否为插件/直插类封装（排完贴片后再排）"""
        p = (pkg or "").upper().strip().replace(" ", "")
        return any(k in p for k in ("DIP", "TO-", "SIP", "插件", "CONN", "插座", "排针", "排母", "接线", "端子"))

    @staticmethod
    def suggest_location_for_restock(specification=None, package=None, category=None,
                                     material_id=None, prefer_empty_last=True,
                                     seed_key=None, exclude_slot_ids=None):
        """补货推荐存放位，返回格位候选列表 [{slot_id, reason, score, extra: {slot_code, ...}}]

        ponytail: 排布/评分是朴素启发式（正则抓数值+单位、按 row/col 顺序），
        规格解析异常时退化为顺序填充；升级路径：独立规格解析器。
        优先级：
        1. 已有相同规格（参数）物料所在格位（封装不同但参数相同也可同格合并）
        2. 已有同一"大类 + 同封装"的物料所在格位
        3. 空格位（按 A1→E8 顺序连续填充，同一批补货通过 exclude_slot_ids 依次排布）
        E 行（row=4）为正常仓位，与其他行同等参与自动分配建议。
        """
        suggestions = []
        seen = set()
        exclude = set(int(x) for x in (exclude_slot_ids or []) if x)

        def _exclude_sql(cond_key, conds, args):
            """追加 NOT IN 排除条件（避免同一批补货都推荐同一格位）"""
            if exclude:
                ph = ",".join("?" * len(exclude))
                conds.append(f"{cond_key} NOT IN ({ph})")
                args.extend(sorted(exclude))

        def _push(slot_id, reason, score, extra=None):
            if slot_id in seen or slot_id in exclude:
                return
            seen.add(slot_id)
            suggestions.append({"slot_id": slot_id, "reason": reason,
                                "score": score, "extra": extra or {}})

        combine = AppSettings.get("combine_same_spec_in_slot")
        excl_row = Inventory.RESERVED_MAX_ROW
        with get_cursor() as cur:
            if material_id:
                conds = ["i.material_id = ?", "i.quantity > 0", "s.row <= ?"]
                args = [int(material_id), excl_row]
                _exclude_sql("i.slot_id", conds, args)
                cur.execute(f"""
                    SELECT i.slot_id, i.quantity, s.slot_code
                    FROM inventories i
                    JOIN slots s ON i.slot_id = s.id
                    WHERE {" AND ".join(conds)}
                    ORDER BY i.quantity DESC, s.row, s.col, s.position
                    LIMIT 10
                """, args)
                for r in cur.fetchall():
                    _push(r["slot_id"], "已有相同物料库存，优先复用此格位", 500,
                          {"slot_code": r["slot_code"], "quantity": r["quantity"]})
            # Level 1: 同规格（参数）的已有物料所在格位
            # 封装不同但参数相同的物料可放在一起：规格匹配不强制封装一致，
            # 仅当无规格信息时才退回按封装聚合
            if combine and (specification or package):
                conds = ["i.quantity > 0", "s.row <= ?"]
                args = [excl_row]
                if specification:
                    conds.append("m.specification LIKE ?")
                    args.append(f"%{specification}%")
                else:
                    conds.append("m.package = ?")
                    args.append(package)
                _exclude_sql("i.slot_id", conds, args)
                cur.execute(f"""
                    SELECT i.slot_id, COUNT(*) as cnt, s.slot_code,
                           GROUP_CONCAT(m.name, '; ') as mats
                    FROM inventories i
                    JOIN materials m ON i.material_id = m.id
                    JOIN slots s ON i.slot_id = s.id
                    WHERE {" AND ".join(conds)}
                    GROUP BY i.slot_id
                    ORDER BY cnt ASC, s.row, s.col, s.position
                    LIMIT 10
                """, args)
                for r in cur.fetchall():
                    _push(r["slot_id"],
                          f"同参数格位：已有 {r['cnt']} 种相同参数物料 ({r['mats'][:30]})",
                          100, {"slot_code": r["slot_code"]})
            # Level 2: 同大类 + 同封装
            if category or package:
                c2 = ["i.quantity > 0", "s.row <= ?"]
                a2 = [excl_row]
                if category:
                    c2.append("m.category = ?")
                    a2.append(category)
                if package:
                    c2.append("m.package = ?")
                    a2.append(package)
                if len(c2) > 2:
                    _exclude_sql("i.slot_id", c2, a2)
                    cur.execute(f"""
                        SELECT DISTINCT i.slot_id, s.slot_code, m.name
                        FROM inventories i
                        JOIN materials m ON i.material_id = m.id
                        JOIN slots s ON i.slot_id = s.id
                        WHERE {" AND ".join(c2)}
                        ORDER BY s.row, s.col, s.position
                        LIMIT 10
                    """, a2)
                    for r in cur.fetchall():
                        _push(r["slot_id"],
                              f"同分类+封装的位置：已有 {r['name'][:12]}",
                              60, {"slot_code": r["slot_code"]})
            empty_slots = [s for s in Slot.get_empty_slots()
                           if s["row"] <= excl_row and s["id"] not in exclude]
            if empty_slots:
                empty_slots.sort(key=lambda x: (x["row"], x["col"], x["position"]))
                for s in empty_slots[:20]:
                    _push(s["id"], "空新格位（按 A1 顺序）", 10,
                          {"slot_code": s["slot_code"]})

        suggestions.sort(key=lambda item: (-int(item.get("score") or 0),
                                            str((item.get("extra") or {}).get("slot_code") or "")))

        # 补充 slot 详情
        for s in suggestions:
            slot = Slot.get(s["slot_id"])
            if slot:
                s["slot_info"] = slot
                invs = Inventory.list_by_slot(s["slot_id"])
                s["all_inventories"] = invs
                s["current_count"] = len(
                    [x for x in invs if int(x.get("quantity") or 0) > 0 and x.get("material_id")]
                )
        return suggestions

    @staticmethod
    def get_low_stock():
        """低库存预警：按物料维度（所有格位总和 vs min_stock）"""
        with get_cursor() as cur:
            cur.execute("""
                SELECT m.id as material_id, m.name as material_name, m.specification,
                       m.min_stock, m.unit, m.category,
                       COALESCE(SUM(i.quantity), 0) as total_qty,
                       GROUP_CONCAT(s.slot_code || ':' || i.quantity, ' | ') as locations
                FROM materials m
                LEFT JOIN inventories i ON m.id = i.material_id
                LEFT JOIN slots s ON i.slot_id = s.id
                GROUP BY m.id
                HAVING total_qty <= m.min_stock
                ORDER BY total_qty ASC
            """)
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def get_statistics():
        with get_cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(DISTINCT s.id) as total_slots,
                    COUNT(DISTINCT CASE WHEN i.quantity > 0 AND i.material_id IS NOT NULL
                                        THEN s.id END) as used_slots,
                    (SELECT COUNT(*) FROM slots)
                    - COUNT(DISTINCT CASE WHEN i.quantity > 0 AND i.material_id IS NOT NULL
                                          THEN s.id END) as empty_slots,
                    COALESCE(SUM(CASE WHEN i.quantity > 0 THEN i.quantity END), 0) as total_qty,
                    COUNT(DISTINCT i.material_id) as distinct_materials,
                    COUNT(DISTINCT i.id) as total_inventory_records
                FROM slots s LEFT JOIN inventories i ON s.id = i.slot_id
            """)
            total = dict(cur.fetchone())
            # 兼容旧字段
            total.setdefault("empty_slots",
                             total["total_slots"] - total["used_slots"])

            cur.execute("""
                SELECT m.category, COUNT(DISTINCT m.id) as cnt,
                       COALESCE(SUM(i.quantity),0) as qty
                FROM materials m
                LEFT JOIN inventories i ON m.id = i.material_id
                GROUP BY m.category
            """)
            by_category = [dict(r) for r in cur.fetchall()]
            return {"total": total, "by_category": by_category}
