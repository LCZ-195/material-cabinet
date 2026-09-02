# -*- coding: utf-8 -*-
"""全局配置"""
import os
import sys


def _app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _app_dir()
DB_PATH = os.path.join(BASE_DIR, "inventory.db")
CABINET_ROWS = 5
CABINET_COLS = 8
SLOTS_PER_CELL = 2
SLOT_POSITIONS = ["内", "外"]
LCSC_API_BASE = "https://api.szlcsc.com/open/api"
LCSC_APP_KEY = ""
LCSC_APP_SECRET = ""
DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_API_KEY = ""
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_MIN_STOCK = 10
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")
for d in [LOG_DIR, EXPORT_DIR]:
    os.makedirs(d, exist_ok=True)
MATERIAL_CATEGORIES = [
    "电阻", "电容", "电感", "二极管", "三极管", "MOS管",
    "IC芯片", "连接器", "排针排母", "晶振", "LED", "按键开关",
    "继电器", "传感器", "PCB", "结构件", "线材", "其他"
]