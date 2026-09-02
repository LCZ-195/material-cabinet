# -*- coding: utf-8 -*-
"""全局配置"""
import os
import sys


def _app_dir():
    """程序数据目录：开发时为项目目录；PyInstaller 打包后为 exe 所在目录，
    保证 inventory.db / logs / exports 与可执行文件同处、数据不丢失。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# 项目根目录（打包后为 exe 所在目录）
BASE_DIR = _app_dir()

# 数据库配置
DB_PATH = os.path.join(BASE_DIR, "inventory.db")

# 收纳柜规格
CABINET_ROWS = 5          # 行数（高）
CABINET_COLS = 8          # 列数（长）
SLOTS_PER_CELL = 2        # 每大格分内外两格

# 格位位置标签
SLOT_POSITIONS = ["内", "外"]

# 立创商城API配置 (用户需要替换为自己的密钥)
LCSC_API_BASE = "https://api.szlcsc.com/open/api"
LCSC_APP_KEY = ""          # 用户需要填写
LCSC_APP_SECRET = ""       # 用户需要填写

# DeepSeek API配置 (AI智能匹配，可选)
DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_API_KEY = ""      # 用户需要填写
DEEPSEEK_MODEL = "deepseek-v4-flash"

# 库存预警阈值（默认）
DEFAULT_MIN_STOCK = 10

# 日志配置
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

# 导出目录
EXPORT_DIR = os.path.join(BASE_DIR, "exports")

# 确保目录存在
for d in [LOG_DIR, EXPORT_DIR]:
    os.makedirs(d, exist_ok=True)

# 物料分类
MATERIAL_CATEGORIES = [
    "电阻", "电容", "电感", "二极管", "三极管", "MOS管",
    "IC芯片", "连接器", "排针排母", "晶振", "LED", "按键开关",
    "继电器", "传感器", "PCB", "结构件", "线材", "其他"
]
