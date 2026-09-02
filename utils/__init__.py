# -*- coding: utf-8 -*-
"""共享小工具：物料编号处理"""
import re

PART_SEP_RE = re.compile(r"[,，;；/、]")


def split_part_numbers(raw):
    return [t.strip() for t in PART_SEP_RE.split(str(raw or "")) if t.strip()]


def is_lcsc_code(text):
    return bool(re.fullmatch(r"[Cc]\d+", str(text or "").strip()))


def list_contains_sql(column: str):
    return f"(',' || REPLACE({column}, ' ', '') || ',') LIKE ?"
