# -*- coding: utf-8 -*-
"""共享小工具：物料编号处理"""
import re

# 供应商编号分隔符（中英文逗号/分号/斜杠/顿号），一格可含多个备选编号
PART_SEP_RE = re.compile(r"[,，;；/、]")


def split_part_numbers(raw):
    """把可能含多个备选编号的字符串拆成编号列表（去空白、去空项）"""
    return [t.strip() for t in PART_SEP_RE.split(str(raw or "")) if t.strip()]


def is_lcsc_code(text):
    """是否立创商城 C 编号（C + 数字，与既有编码列判定口径一致）"""
    return bool(re.fullmatch(r"[Cc]\d+", str(text or "").strip()))


def list_contains_sql(column: str):
    """SQL 条件：column 以逗号列表形式包含某编号（兼容历史"逗号拼接编码"数据）"""
    return (f"(',' || REPLACE({column}, ' ', '') || ',') LIKE ?")
