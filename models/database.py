# -*- coding: utf-8 -*-
"""数据库连接和初始化"""
import sqlite3
import os
import json
from contextlib import contextmanager
from config import DB_PATH, CABINET_ROWS, CABINET_COLS, SLOTS_PER_CELL


def get_conn():
    """获取数据库连接（带忙等待超时，容忍并发写同库的短暂锁等待）"""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_cursor():
    """上下文管理器获取游标，自动提交和关闭"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 应用设置（GUI中可修改，持久化到库） ----------
_DEFAULT_SETTINGS = {
    "default_min_stock": 10,
    "combine_same_spec_in_slot": True,   # 允许同参数不同商家物料放同一格
    "auto_pick_location": True,          # 补货时自动推荐存放位置
}


class AppSettings:
    """应用设置（读写字典，JSON存库）"""
    @staticmethod
    def _ensure_table(cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

    @staticmethod
    def all() -> dict:
        with get_cursor() as cur:
            AppSettings._ensure_table(cur)
            cur.execute("SELECT key, value FROM app_settings")
            rows = cur.fetchall()
            saved = {r["key"]: r["value"] for r in rows}
        result = dict(_DEFAULT_SETTINGS)
        for k, v in saved.items():
            try:
                result[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                result[k] = v
        return result

    @staticmethod
    def get(key: str):
        return AppSettings.all().get(key, _DEFAULT_SETTINGS.get(key))

    @staticmethod
    def set(key: str, value):
        if isinstance(value, (dict, list, bool, int, float)):
            v = json.dumps(value, ensure_ascii=False)
        else:
            v = str(value)
        with get_cursor() as cur:
            AppSettings._ensure_table(cur)
            cur.execute(
                "INSERT INTO app_settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, v)
            )

    @staticmethod
    def update_many(data: dict):
        for k, v in data.items():
            AppSettings.set(k, v)

    @staticmethod
    def clear_all():
        with get_cursor() as cur:
            cur.execute("DELETE FROM app_settings")


# ---------- 表结构初始化 + 迁移 ----------
def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return bool(cur.fetchone())


def _add_column_if_missing(cur, table, col, definition):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")


def init_database():
    """初始化数据库表结构 + 增量迁移"""
    with get_cursor() as cur:
        # 物料表
        cur.execute("""
        CREATE TABLE IF NOT EXISTS materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_code TEXT UNIQUE,           -- 物料编码
            name TEXT NOT NULL,                  -- 物料名称
            category TEXT,                       -- 分类
            specification TEXT,                  -- 规格参数
            package TEXT,                        -- 封装
            supplier_code TEXT,                  -- 供应商编号
            lcsc_code TEXT,                      -- 立创编号
            brand TEXT,                          -- 品牌
            unit TEXT DEFAULT '个',              -- 单位
            min_stock INTEGER DEFAULT 10,        -- 最低库存预警
            description TEXT,                    -- 描述
            datasheet_url TEXT,                  -- 数据手册链接
            parameters TEXT,                     -- JSON格式详细参数
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        # 格位表 (8列x5行, 每格2小格(内外))
        cur.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            row INTEGER NOT NULL,                -- 行号 0-4
            col INTEGER NOT NULL,                -- 列号 0-7
            position INTEGER NOT NULL,           -- 0=内, 1=外
            slot_code TEXT UNIQUE NOT NULL,      -- 格位编码 A1-内 A1-外
            note TEXT,                           -- 备注
            UNIQUE(row, col, position)
        )""")

        # 库存表 (物料在格位中的数量) -- 单格可多物料 (slot_id 不唯一)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS inventories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL,
            material_id INTEGER,                 -- 允许空，表示空格位
            quantity INTEGER DEFAULT 0,          -- 当前数量
            batch_no TEXT,                       -- 批次号
            inbound_date DATE,                   -- 入库日期
            note TEXT,
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (slot_id) REFERENCES slots(id) ON DELETE CASCADE,
            FOREIGN KEY (material_id) REFERENCES materials(id) ON DELETE SET NULL
        )""")

        # BOM导入记录表
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bom_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bom_name TEXT NOT NULL,              -- BOM名称
            project_name TEXT,                   -- 项目名称
            file_path TEXT,                      -- 源文件路径
            bom_type TEXT DEFAULT 'pick',        -- pick=领料出库, restock=补货入库
            total_items INTEGER DEFAULT 0,       -- BOM总行数
            matched_items INTEGER DEFAULT 0,     -- 已匹配行数
            status TEXT DEFAULT 'pending',       -- pending/processing/completed/cancelled
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            complete_time TIMESTAMP
        )""")
        _add_column_if_missing(cur, "bom_records", "bom_type", "TEXT DEFAULT 'pick'")

        # BOM明细项
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bom_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bom_id INTEGER NOT NULL,
            line_no INTEGER,                     -- 行号
            material_code TEXT,                  -- BOM中的物料编码
            material_name TEXT,                  -- 物料名称
            specification TEXT,                  -- 规格
            package TEXT,                        -- 封装
            required_qty INTEGER NOT NULL,       -- 需求数量（补/取）
            matched_inventory_id INTEGER,        -- 匹配到的库存ID
            picked_qty INTEGER DEFAULT 0,        -- 已执行数量(已领/已补)
            match_status TEXT DEFAULT 'unmatched', -- unmatched/partial/fully/replaced/restock_ok
            replace_material_id INTEGER,         -- 替换物料ID
            suggested_slot_id INTEGER,           -- 补货模式：建议存放的格位ID
            note TEXT,
            FOREIGN KEY (bom_id) REFERENCES bom_records(id) ON DELETE CASCADE,
            FOREIGN KEY (matched_inventory_id) REFERENCES inventories(id) ON DELETE SET NULL,
            FOREIGN KEY (replace_material_id) REFERENCES materials(id) ON DELETE SET NULL,
            FOREIGN KEY (suggested_slot_id) REFERENCES slots(id) ON DELETE SET NULL
        )""")
        _add_column_if_missing(cur, "bom_items", "suggested_slot_id",
                               "INTEGER REFERENCES slots(id) ON DELETE SET NULL")
        # BOM 原始列扩展：Comment / Supplier Part / Footprint
        _add_column_if_missing(cur, "bom_items", "comment", "TEXT")
        _add_column_if_missing(cur, "bom_items", "supplier_part", "TEXT")
        _add_column_if_missing(cur, "bom_items", "footprint", "TEXT")

        # 操作日志表
        cur.execute("""
        CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_type TEXT NOT NULL,        -- inbound/outbound/edit/delete/import/export/clear/restock
            target_type TEXT,                    -- material/inventory/bom/slot
            target_id INTEGER,
            detail TEXT,                         -- 操作详情JSON
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        # 参数替换映射表 (立创替代关系缓存)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS material_replacements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL,
            replace_material_id INTEGER NOT NULL,
            source TEXT DEFAULT 'lcsc',          -- lcsc/manual
            match_score INTEGER DEFAULT 0,       -- 匹配度 0-100
            note TEXT,
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (material_id) REFERENCES materials(id) ON DELETE CASCADE,
            FOREIGN KEY (replace_material_id) REFERENCES materials(id) ON DELETE CASCADE,
            UNIQUE(material_id, replace_material_id)
        )""")

        # 内置元件库：立创匹配确认后自动沉淀（供应商编号+参数），匹配时优先查库
        cur.execute("""
        CREATE TABLE IF NOT EXISTS component_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lcsc_code TEXT UNIQUE,               -- 立创编号 Cxxxx
            supplier_part TEXT,                  -- 供应商编号/厂商型号
            model TEXT,                          -- 型号
            name TEXT,                           -- 描述(Comment)
            specification TEXT,                  -- 规格/参数摘要
            package TEXT,                        -- 封装
            footprint TEXT,                      -- PCB Footprint
            brand TEXT,                          -- 品牌
            category TEXT,                       -- 分类
            parameters TEXT,                     -- JSON详细参数
            datasheet TEXT,                      -- 数据手册链接
            hit_count INTEGER DEFAULT 0,         -- 命中次数
            source TEXT DEFAULT 'lcsc',          -- lcsc/manual/bom
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_complib_sp ON component_library(supplier_part)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_complib_model ON component_library(model)")

        # 应用设置表
        AppSettings._ensure_table(cur)
        # 清理已废弃的历史设置项（operator / 立创密钥配置栏目）
        cur.execute("DELETE FROM app_settings WHERE key IN "
                    "('operator', 'lcsc_app_key', 'lcsc_app_secret', 'lcsc_api_base')")
        # 迁移：移除 operation_logs 表遗留的 operator 列
        cur.execute("PRAGMA table_info(operation_logs)")
        if any(r[1] == "operator" for r in cur.fetchall()):
            cur.execute("ALTER TABLE operation_logs DROP COLUMN operator")

        # 初始化格位数据 (首次)
        cur.execute("SELECT COUNT(*) FROM slots")
        if cur.fetchone()[0] == 0:
            _init_slots(cur)


def _init_slots(cur):
    """初始化8x5x2个格位"""
    rows = CABINET_ROWS
    cols = CABINET_COLS
    positions = SLOTS_PER_CELL
    for r in range(rows):
        row_label = chr(ord('A') + r)  # A, B, C, D, E
        for c in range(cols):
            for p in range(positions):
                pos_label = "内" if p == 0 else "外"
                slot_code = f"{row_label}{c+1}-{pos_label}"
                cur.execute(
                    "INSERT INTO slots (row, col, position, slot_code) VALUES (?, ?, ?, ?)",
                    (r, c, p, slot_code)
                )


def purge_demo_data():
    """清除演示数据：完整清空全部业务表（保留表结构+设置），确保无残留"""
    with get_cursor() as cur:
        for t in ["bom_items", "bom_records", "inventories",
                  "material_replacements", "materials", "operation_logs"]:
            cur.execute(f"DELETE FROM {t}")


def factory_reset():
    """完全清空所有业务数据（保留表结构+设置）"""
    with get_cursor() as cur:
        for t in ["bom_items", "bom_records", "inventories",
                  "material_replacements", "materials", "operation_logs"]:
            cur.execute(f"DELETE FROM {t}")

