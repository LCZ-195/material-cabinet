# -*- coding: utf-8 -*-
"""
main.py —— 程序入口（pywebview 桌面窗口层）
================================================================
职责：
  - DPI 自适应
  - 本地 HTTP 静态服务器托管 material-cabinet-dashboard 资源
  - CDN 链接改写为本地 vendor 文件（离线运行）
  - UiDispatcher 将 Python 侧事件安全推送到网页 JS
  - Bridge 作为 js_api 桥接前端与 backend.py
  - 运行时注入 RESET_CSS + BRIDGE_JS 脚本

应用名称：物料收纳柜
"""

# ── DPI 自适应（Windows）──────────────────────────────────
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── 标准库 ────────────────────────────────────────────────
import json
import mimetypes
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── 第三方 ────────────────────────────────────────────────
import webview

# ── 业务层 ────────────────────────────────────────────────
from backend import Backend

# ══════════════════════════════════════════════════════════
#  常量
# ══════════════════════════════════════════════════════════
APP_VERSION = "1.15.11"
APP_NAME = "物料收纳柜"
INSTANCE_SOCKET = "127.0.0.1"
INSTANCE_PORT = 47831
DESIGN_WIDTH = 1440
DESIGN_HEIGHT = 900


def resource_path(rel):
    """兼容源码运行与 PyInstaller --onefile（_MEIPASS）"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


UI_ROOT = resource_path('material-cabinet-dashboard')

# CDN → 本地 vendor 映射（HTML 内的 CDN 链接改写为本地路径）
CDN_LOCAL_MAP = {
    'https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.3.1/dist/index.global.js': '/vendor/tailwind.js',
    'https://unpkg.com/lucide@1.8.0/dist/umd/lucide.min.js': '/vendor/lucide.js',
    'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js': '/vendor/chart.js',
}


# ══════════════════════════════════════════════════════════
#  本地 HTTP 静态服务器
# ══════════════════════════════════════════════════════════
class UiHandler(BaseHTTPRequestHandler):
    """静态文件服务 + CDN 链接改写 + 防目录穿越"""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path in ('/', '/index.html'):
            path = '/pages/index.html'

        root_abs = os.path.abspath(UI_ROOT)
        full = os.path.abspath(os.path.join(root_abs, os.path.normpath(path.lstrip('/'))))

        # 防目录穿越
        if full != root_abs and not full.startswith(root_abs + os.sep):
            self.send_error(403)
            return
        if not os.path.isfile(full):
            self.send_error(404)
            return

        with open(full, 'rb') as f:
            data = f.read()

        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'

        # HTML 文件：改写 CDN 链接为本地 vendor
        if path.endswith('.html'):
            text = data.decode('utf-8')
            for cdn_url, local_path in CDN_LOCAL_MAP.items():
                text = text.replace(cdn_url, local_path)
            if 'rel="icon"' not in text:
                text = text.replace(
                    '<head>',
                    '<head><link rel="icon" href="/assets/app-icon.ico" type="image/x-icon">',
                    1,
                )
            data = text.encode('utf-8')
            ctype = 'text/html; charset=utf-8'

        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class UiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_ui_server(port=0):
    server = UiServer(('127.0.0.1', port), UiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ══════════════════════════════════════════════════════════
#  UiDispatcher — 将 Python 侧事件安全推送到网页 JS
# ══════════════════════════════════════════════════════════
class UiDispatcher:
    """专用线程 + 队列，将 JS 代码安全注入到网页"""

    def __init__(self, window):
        self._window = window
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def push(self, js):
        """将一段 JS 代码推送到队列，由工作线程注入"""
        self._queue.put(js)

    def _loop(self):
        while True:
            js = self._queue.get()
            try:
                self._window.evaluate_js(js)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════
#  Bridge — pywebview js_api 桥接对象
# ══════════════════════════════════════════════════════════
class Bridge:
    """
    前后端桥接对象，作为 pywebview 的 js_api 参数。
    所有 public 方法自动暴露给前端 JS（通过 window.pywebview.api.method()）。
    """

    def __init__(self):
        self._b = Backend()
        self._window = None

    # ── 通用 ──────────────────────────────────────────────
    def ping(self):
        return self._b.ping()

    def get_version(self):
        return {"ok": True, "version": APP_VERSION}

    # ── 概览仪表盘 ────────────────────────────────────────
    def get_dashboard(self):
        return self._b.get_dashboard()

    def get_overview_slots(self):
        return self._b.get_overview_slots()

    # ── 收纳柜格位 ────────────────────────────────────────
    def get_cabinet(self):
        return self._b.get_cabinet()

    def get_slot(self, slot_code):
        return self._b.get_slot(slot_code)

    def get_slot_inventories(self, slot_code):
        return self._b.get_slot_inventories(slot_code)

    # ── 物料主数据 ────────────────────────────────────────
    def list_materials(self, keyword="", category=""):
        return self._b.list_materials(keyword, category)

    def get_material(self, material_id):
        return self._b.get_material(material_id)

    def create_material(self, data):
        return self._b.create_material(data)

    def update_material(self, material_id, data):
        return self._b.update_material(material_id, data)

    def delete_material(self, material_id):
        return self._b.delete_material(material_id)

    def get_material_parameters(self, material_id):
        return self._b.get_material_parameters(material_id)

    def add_replacement(self, material_id, replace_material_id, score=80, note=""):
        return self._b.add_replacement(material_id, replace_material_id, score, note)

    def get_replacement_map(self):
        return self._b.get_replacement_map()

    def get_material_categories(self):
        return self._b.get_material_categories()

    def list_component_library(self, keyword=""):
        return self._b.list_component_library(keyword)

    def update_component_library(self, lib_id, data):
        return self._b.update_component_library(lib_id, data)

    def delete_component_library(self, lib_id):
        return self._b.delete_component_library(lib_id)

    # ── 库存管理 ──────────────────────────────────────────
    def list_inventories(self, keyword=""):
        return self._b.list_inventories(keyword)

    def get_empty_slots(self):
        return self._b.get_empty_slots()

    def get_slot_options(self, keyword=""):
        return self._b.get_slot_options(keyword)

    def stock_in(self, slot_code, material_id, quantity, batch_no=None, note=None):
        return self._b.stock_in(slot_code, material_id, quantity, batch_no, note)

    def stock_out(self, inv_id, quantity, note=None):
        return self._b.stock_out(inv_id, quantity, note)

    def adjust_stock(self, inv_id, new_qty, reason=""):
        return self._b.adjust_stock(inv_id, new_qty, reason)

    def clear_slot(self, slot_code):
        return self._b.clear_slot(slot_code)

    def remove_inventory(self, slot_code, inv_id):
        return self._b.remove_inventory(slot_code, inv_id)

    def suggest_location(self, specification="", package="", category=""):
        return self._b.suggest_location(specification, package, category)

    # ── BOM ────────────────────────────────────────────────
    def list_bom_records(self):
        return self._b.list_bom_records()

    def get_bom(self, bom_id):
        return self._b.get_bom(bom_id)

    def parse_bom(self, file_path, bom_type="pick", bom_name="", project_name=""):
        return self._b.parse_bom(file_path, bom_type, bom_name, project_name)

    def match_bom(self, bom_id, force=False):
        return self._b.match_bom(bom_id, force)

    def confirm_pick(self, item_id, picked_qty):
        return self._b.confirm_pick(item_id, picked_qty)

    def restock_from_bom(self, bom_id, auto_location=True):
        return self._b.restock_from_bom(bom_id, auto_location)

    def get_restock_plan(self, bom_id):
        return self._b.get_restock_plan(bom_id)

    def confirm_restock_item(self, item_id, slot_id, quantity=None, batch_no=None, note=None):
        return self._b.confirm_restock_item(item_id, slot_id, quantity, batch_no, note)

    def remove_bom_items(self, bom_id, item_ids):
        return self._b.remove_bom_items(bom_id, item_ids)

    def delete_bom(self, bom_id):
        return self._b.delete_bom(bom_id)

    # ── 导出 ──────────────────────────────────────────────
    def export_inventories(self):
        return self._b.export_inventories()

    def export_materials(self):
        return self._b.export_materials()

    def export_bom_picklist(self, bom_id):
        return self._b.export_bom_picklist(bom_id)

    # ── 设置 ──────────────────────────────────────────────
    def get_settings(self):
        return self._b.get_settings()

    def save_settings(self, data):
        return self._b.save_settings(data)

    def get_github_settings(self):
        return self._b.get_github_settings()

    def save_github_settings(self, data):
        return self._b.save_github_settings(data)

    def check_github_version(self):
        return self._b.check_github_version()

    def update_github_version(self):
        result = self._b.update_github_version()
        if result.get("ok") and result.get("data", {}).get("restart_required") and self._window:
            threading.Timer(0.5, self._window.destroy).start()
        return result

    def check_github_inventory(self):
        return self._b.check_github_inventory()

    def sync_github_inventory(self):
        return self._b.sync_github_inventory()

    def clear_demo(self):
        return self._b.clear_demo()

    def factory_reset(self):
        return self._b.factory_reset()

    # ── 立创 API ──────────────────────────────────────────
    def search_lcsc(self, keyword, page=1, page_size=20):
        return self._b.search_lcsc(keyword, page, page_size)

    def get_lcsc_detail(self, lcsc_code):
        return self._b.get_lcsc_detail(lcsc_code)

    def compare_parameters(self, params1, params2):
        return self._b.compare_parameters(params1, params2)

    def find_replacement_candidates(self, material_id):
        return self._b.find_replacement_candidates(material_id)

    def get_ai_status(self):
        return self._b.get_ai_status()

    def ai_match_bom(self, bom_id):
        return self._b.ai_match_bom(bom_id)

    # ── 操作日志 ──────────────────────────────────────────
    def list_operations(self, limit=50, op_type=None):
        return self._b.list_operations(limit, op_type)

    # ── 文件对话框（需要 window 引用）────────────────────
    def import_bom(self, bom_type="pick", append_bom_id=None):
        """打开文件对话框并解析 BOM 文件（支持一次选择多个）

        append_bom_id 非空时全部行合并到该 BOM（与当前清单合并展示/匹配）。
        """
        if not self._window:
            return {"ok": False, "error": "窗口未初始化"}
        try:
            # 兼容新旧 pywebview API：FileDialog.OPEN (新) / OPEN_DIALOG (旧,已废弃)
            dialog_type = getattr(getattr(webview, 'FileDialog', None), 'OPEN', None) \
                or getattr(webview, 'OPEN_DIALOG', 10)
            multi = getattr(getattr(webview, 'FileDialog', None), 'ALLOW_MULTISELECT', None)
            if multi:
                dialog_type = dialog_type | multi
            result = self._window.create_file_dialog(
                dialog_type,
                file_types=('Excel Files (*.xlsx;*.xls)', 'CSV Files (*.csv)'),
            )
            if not result:
                return {"ok": False, "error": "未选择文件"}
            # pywebview 返回 tuple/list of str 或单个 str
            if isinstance(result, str):
                paths = [result]
            else:
                paths = [p for p in (result or []) if isinstance(p, str) and p]
            if not paths:
                return {"ok": False, "error": "未选择有效文件路径"}
            if len(paths) == 1:
                return self._b.parse_bom(paths[0], bom_type=bom_type,
                                         append_bom_id=append_bom_id)
            # 多文件：全部合并到同一 BOM（首个文件建/追加，后续追加到它）
            imported, failed = [], []
            cur_bom_id = append_bom_id
            for p in paths:
                res = self._b.parse_bom(p, bom_type=bom_type,
                                        append_bom_id=cur_bom_id)
                if res.get("ok"):
                    d = res.get("data") or {}
                    cur_bom_id = d.get("bom_id") or cur_bom_id
                    imported.append({"bom_id": cur_bom_id,
                                     "bom_name": d.get("bom_name"),
                                     "total": d.get("total")})
                else:
                    failed.append({"file": os.path.basename(str(p)),
                                   "error": res.get("error")})
            return {"ok": True, "data": {
                "imported": imported, "failed": failed,
                "summary": f"成功导入 {len(imported)} 个 BOM"
                           + (f"，{len(failed)} 个失败" if failed else "")}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def import_bom_bytes(self, file_name, base64_data, bom_type="pick",
                         append_bom_id=None):
        """接收前端拖拽上传的文件（base64），写临时文件后解析。

        append_bom_id 非空时行追加到该 BOM（与当前清单合并）。
        """
        import base64 as _base64
        import os as _os
        import tempfile as _tempfile
        try:
            if not base64_data:
                return {"ok": False, "error": "未收到文件数据"}
            # 去除 data URL 前缀（data:...;base64,xxx）
            payload = base64_data.strip()
            if payload.startswith("data:") and "," in payload:
                payload = payload.split(",", 1)[1]
            raw = _base64.b64decode(payload)
            ext = _os.path.splitext(file_name or "")[1].lower()
            if ext not in (".csv", ".xlsx", ".xls"):
                return {"ok": False, "error": "仅支持 .csv / .xlsx 文件"}
            fd, tmp_path = _tempfile.mkstemp(suffix=ext)
            try:
                with _os.fdopen(fd, "wb") as f:
                    f.write(raw)
                display_name = _os.path.splitext(file_name)[0]
                return self._b.parse_bom(tmp_path, bom_type=bom_type,
                                         bom_name=display_name,
                                         append_bom_id=append_bom_id)
            finally:
                try:
                    _os.remove(tmp_path)
                except OSError:
                    pass
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════
#  RESET_CSS — 运行时注入的重置样式
# ══════════════════════════════════════════════════════════
RESET_CSS = """
/* ── pywebview 容器重置 ── */
html, body {
    margin: 0 !important;
    padding: 0 !important;
    width: 100% !important;
    height: 100% !important;
    overflow-x: hidden;
}
/* 滚动条美化 */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(100,116,139,0.3); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: rgba(100,116,139,0.5); }
/* ── 图标对齐修复 ── */
svg { flex-shrink: 0; vertical-align: middle; }
[data-lucide], .lucide { display: inline-flex; flex-shrink: 0; vertical-align: middle; }
/* select 内置箭头清除（自定义样式时） */
select { appearance: none; -webkit-appearance: none; }
.mc-action-busy { cursor: wait !important; opacity: .8; }
.mc-action-busy [data-lucide] { animation: mc-spin .8s linear infinite; }
@keyframes mc-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .mc-action-busy [data-lucide] { animation: none; } }
/* ── BOM 拖拽上传高亮 ── */
.drag-active {
    border-color: var(--mc-primary, #06b6d4) !important;
    background: rgba(6, 182, 212, 0.06) !important;
}
"""


# ══════════════════════════════════════════════════════════
#  BRIDGE_JS — 运行时注入的前后端桥接脚本
# ══════════════════════════════════════════════════════════
BRIDGE_JS = r"""
(function () {
  'use strict';
  if (window.__bridgeInjected) return;
  window.__bridgeInjected = true;

  var api = null;
  var pendingBomId = null;
  var pendingBomItems = [];

  /* ── 工具函数 ── */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return (root || document).querySelectorAll(sel); }
  function fmtTime(ts) {
    if (!ts) return '--';
    var s = String(ts);
    return s.length >= 16 ? s.slice(0, 16).replace('T', ' ') : s;
  }
  function refreshIcons() { if (window.lucide && window.lucide.createIcons) window.lucide.createIcons(); }
  function updateFooterTime() {
    var el = document.querySelector('[data-dom-id="footer-refresh-time"]');
    if (el) el.textContent = new Date().toLocaleString('zh-CN', { hour12: false });
  }
  function updateVersionLabels() {
    if (!api || typeof api.get_version !== 'function') return;
    api.get_version().then(function (res) {
      if (!res || !res.version) return;
      $$('footer').forEach(function (footer) {
        footer.innerHTML = footer.innerHTML.replace(/v\d+(?:\.\d+){1,3}/g, 'v' + res.version);
      });
      refreshIcons();
    });
  }
  function navigate(page) { window.location.href = page; }
  function setPendingAction(action) { sessionStorage.setItem('pending_global_action', action); }
  function bindGlobalSearch() {
    var search = $('.search-input');
    if (!search || search.dataset.globalSearchBound) return;
    search.dataset.globalSearchBound = '1';
    var saved = sessionStorage.getItem('global_search_keyword') || '';
    if (saved && !search.value) search.value = saved;
    search.addEventListener('input', function () {
      sessionStorage.setItem('global_search_keyword', search.value);
    });
    search.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      var keyword = search.value.trim();
      sessionStorage.setItem('global_search_keyword', keyword);
      sessionStorage.setItem('material_search_keyword', keyword);
      if (getCurrentPage() === 'materials') {
        var materialSearch = $('[data-dom-id="filter-material-search"]');
        if (materialSearch) {
          materialSearch.value = keyword;
          materialSearch.dispatchEvent(new Event('input'));
        }
      } else {
        navigate('materials.html');
      }
    });
  }
  function showToast(msg, type) {
    type = type || 'info';
    var colors = { info: '#3b82f6', success: '#22c55e', warning: '#f59e0b', error: '#ef4444' };
    var t = document.createElement('div');
    t.style.cssText = 'position:fixed;top:20px;right:20px;z-index:9999;padding:12px 20px;border-radius:8px;color:#fff;font-size:14px;font-weight:600;box-shadow:0 4px 12px rgba(0,0,0,.15);background:' + (colors[type] || colors.info) + ';';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () { t.style.transition = 'opacity .3s'; t.style.opacity = '0'; setTimeout(function () { t.remove(); }, 300); }, 2500);
  }
  function setActionBusy(buttons, busy, label) {
    buttons.filter(Boolean).forEach(function (button) {
      if (busy) {
        if (!button.dataset.actionHtml) button.dataset.actionHtml = button.innerHTML;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.classList.add('mc-action-busy');
        var icon = button.querySelector('[data-lucide]');
        if (icon) icon.setAttribute('data-lucide', 'loader-circle');
        var text = button.querySelector('.mc-action-label');
        if (text) text.textContent = label;
        else {
          var labels = button.querySelectorAll('.mc-action-runtime-label');
          if (labels.length) labels[0].textContent = label;
          else {
            var runtimeLabel = document.createElement('span');
            runtimeLabel.className = 'mc-action-runtime-label';
            runtimeLabel.textContent = label;
            button.appendChild(runtimeLabel);
          }
        }
      } else {
        button.disabled = false;
        button.removeAttribute('aria-busy');
        button.classList.remove('mc-action-busy');
        if (button.dataset.actionHtml) button.innerHTML = button.dataset.actionHtml;
        delete button.dataset.actionHtml;
      }
    });
    refreshIcons();
  }
  function actionElapsed(start) { return ((Date.now() - start) / 1000).toFixed(1) + '秒'; }
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* ── 页面检测 ── */
  function getCurrentPage() {
    var match = window.location.pathname.match(/\/pages\/(.+?)\.html/);
    return match ? match[1] : 'index';
  }

  /* ════════════════════════════════════════
     index.html — 概览仪表盘
     ════════════════════════════════════════ */
  function initIndex() {
    loadIndexData();

    /* 按钮绑定（dataset 标记防止刷新时重复注册） */
    var refreshBtn = $('[data-dom-id="btn-refresh-dashboard"]');
    if (refreshBtn && !refreshBtn.dataset.bridgeBound) {
      refreshBtn.dataset.bridgeBound = '1';
      refreshBtn.addEventListener('click', function () { loadIndexData(); });
    }

    var exportBtn = $('[data-dom-id="btn-export-report"]');
    if (exportBtn && !exportBtn.dataset.bridgeBound) {
      exportBtn.dataset.bridgeBound = '1';
      exportBtn.addEventListener('click', function () {
        api.export_inventories().then(function (res) {
          if (res.ok) showToast('\u5bfc\u51fa\u6210\u529f\uff1a' + (res.data && res.data.path || ''), 'success');
          else showToast('\u5bfc\u51fa\u5931\u8d25\uff1a' + (res.error || ''), 'error');
        });
      });
    }


    /* CTA 链接 */
    var ctaCabinet = $('[data-dom-id="cta-cabinet"]');
    if (ctaCabinet && !ctaCabinet.dataset.bridgeBound) {
      ctaCabinet.dataset.bridgeBound = '1';
      ctaCabinet.addEventListener('click', function () { navigate('cabinet.html'); });
    }
    var ctaMaterials = $('[data-dom-id="cta-materials"]');
    if (ctaMaterials && !ctaMaterials.dataset.bridgeBound) {
      ctaMaterials.dataset.bridgeBound = '1';
      ctaMaterials.addEventListener('click', function () { navigate('materials.html'); });
    }
  }

  function loadIndexData() {
    Promise.all([api.get_dashboard(), api.get_overview_slots()]).then(function (res) {
      var dash = res[0], slots = res[1];
      if (!dash.ok || !slots.ok) return;

      var d = dash.data;

      /* KPI 卡片（按 id 精确挂载） */
      var kpiMap = { 'kpi-materials': d.material_count, 'kpi-low': d.low_stock_count, 'kpi-picks': d.month_picks };
      Object.keys(kpiMap).forEach(function (id) {
        var el = document.getElementById(id);
        if (el && kpiMap[id] !== undefined) el.textContent = kpiMap[id];
      });
      var slotEl = document.getElementById('kpi-slots');
      if (slotEl && slotEl.firstElementChild && d.occupied_slots !== undefined) {
        slotEl.firstElementChild.textContent = d.occupied_slots;
      }

      /* 迷你格位网格（前/后仓；取料需求 绿=可取红=缺料，补货需求 显示供应商编号，点击标记完成） */
      var grid = $('#overview-slot-grid');
      if (grid && slots.data && slots.data.cells) {
        var demand = {}, restock = {};
        try { demand = JSON.parse(localStorage.getItem('pick_demand') || '{}'); } catch (e) { demand = {}; }
        try { restock = JSON.parse(localStorage.getItem('restock_demand') || '{}'); } catch (e) { restock = {}; }
        /* 标题按模式切换：取料概览 / 补货概览 / 占用概览 */
        var titleEl = document.getElementById('overview-title');
        if (titleEl) {
          titleEl.textContent = Object.keys(demand).length ? '\u6536\u7eb3\u67dc\u53d6\u6599\u6982\u89c8'
            : (Object.keys(restock).length ? '\u6536\u7eb3\u67dc\u8865\u8d27\u6982\u89c8' : '\u6536\u7eb3\u67dc\u5360\u7528\u6982\u89c8');
        }
        var html = '';
        slots.data.cells.forEach(function (cell) {
          var innerSt = cell.inner ? (cell.inner.status || 'ok') : 'empty';
          var outerSt = cell.outer ? (cell.outer.status || 'ok') : 'empty';
          var innerCls = cell.inner && cell.inner.occupied ? (innerSt === 'low' ? 'is-low' : 'is-occupied') : '';
          var outerCls = cell.outer && cell.outer.occupied ? (outerSt === 'low' ? 'is-low' : 'is-occupied') : '';
          var innerExtra = '', innerText = '', innerClick = '', outerExtra = '', outerText = '', outerClick = '';
          var icode = (cell.inner && cell.inner.slot_code) || '';
          var ocode = (cell.outer && cell.outer.slot_code) || '';
          if (icode && demand[icode]) {
            var d1 = demand[icode];
            if (d1.picked) { innerExtra = ' is-demand-done'; innerText = d1.need; }
            else { innerExtra = d1.ok ? ' is-demand-ok' : ' is-demand-low'; innerText = d1.need; innerClick = ' data-demand-slot="' + escapeHtml(icode) + '"'; }
          } else if (icode && restock[icode]) {
            var r1 = restock[icode];
            var rtxt = r1.sp ? (r1.sp.length > 6 ? r1.sp.slice(0, 6) : r1.sp) : '\u653e';
            if (r1.picked) { innerExtra = ' is-demand-done'; innerText = rtxt; }
            else { innerExtra = ' is-restock-demand'; innerText = rtxt; innerClick = ' data-restock-slot="' + escapeHtml(icode) + '"'; }
          }
          if (ocode && demand[ocode]) {
            var d2 = demand[ocode];
            if (d2.picked) { outerExtra = ' is-demand-done'; outerText = d2.need; }
            else { outerExtra = d2.ok ? ' is-demand-ok' : ' is-demand-low'; outerText = d2.need; outerClick = ' data-demand-slot="' + escapeHtml(ocode) + '"'; }
          } else if (ocode && restock[ocode]) {
            var r2 = restock[ocode];
            var rtxt2 = r2.sp ? (r2.sp.length > 6 ? r2.sp.slice(0, 6) : r2.sp) : '\u653e';
            if (r2.picked) { outerExtra = ' is-demand-done'; outerText = rtxt2; }
            else { outerExtra = ' is-restock-demand'; outerText = rtxt2; outerClick = ' data-restock-slot="' + escapeHtml(ocode) + '"'; }
          }
          html += '<div class="overview-slot" title="' + escapeHtml(cell.label) + '">'
            + '<span class="overview-slot-number">' + escapeHtml(cell.label) + '</span>'
            + '<div class="overview-bins">'
            + '<span class="overview-bin ' + innerCls + innerExtra + '" title="\u524d\u4ed3"' + innerClick + '>' + innerText + '</span>'
            + '<span class="overview-bin ' + outerCls + outerExtra + '" title="\u540e\u4ed3"' + outerClick + '>' + outerText + '</span>'
            + '</div></div>';
        });
        grid.innerHTML = html;
        /* 点击取料/补货仓 → 标记完成（白底黑字）；全部完成 → 退出概览模式 */
        if (!grid.dataset.pickBound) {
          grid.dataset.pickBound = '1';
          grid.addEventListener('click', function (e) {
            var bin = e.target.closest ? e.target.closest('.overview-bin') : null;
            if (!bin) return;
            var isRestock = !!bin.getAttribute('data-restock-slot');
            var code = bin.getAttribute('data-demand-slot') || bin.getAttribute('data-restock-slot');
            if (!code) return;
            var storeKey = isRestock ? 'restock_demand' : 'pick_demand';
            var store = {};
            try { store = JSON.parse(localStorage.getItem(storeKey) || '{}'); } catch (err) { store = {}; }
            if (!store[code] || store[code].picked) return;
            store[code].picked = true;
            try { localStorage.setItem(storeKey, JSON.stringify(store)); } catch (err) { /* 忽略 */ }
            var allDone = Object.keys(store).every(function (k) { return store[k].picked; });
            if (allDone) {
              try { localStorage.removeItem(storeKey); } catch (err) { /* 忽略 */ }
              showToast(isRestock ? '\u8865\u8d27\u5b8c\u6210\uff0c\u5df2\u56de\u5230\u603b\u6982\u89c8' : '\u53d6\u6599\u5b8c\u6210\uff0c\u5df2\u56de\u5230\u603b\u6982\u89c8', 'success');
            }
            loadIndexData();
          });
        }
      }

      /* 低库存预警 */
      var lowStockContainer = $('#low-stock-list');
      if (lowStockContainer) {
        var lowList = (d.low_stock_materials || []).slice(0, 5);
        if (!lowList.length) {
          lowStockContainer.innerHTML = '<div class="py-3 text-sm text-muted-foreground text-center">\u6682\u65e0\u4f4e\u5e93\u5b58\u7269\u6599</div>';
        } else {
          var lowHtml = '';
          lowList.forEach(function (m) {
            lowHtml += '<div class="flex items-center justify-between py-3">'
              + '<div class="flex items-center gap-3">'
              + '<span class="w-2 h-2 rounded-full bg-[var(--mc-state-warning)]"></span>'
              + '<div><div class="text-sm font-medium">' + escapeHtml(m.material_name) + '</div>'
              + '<div class="text-xs text-muted-foreground">' + escapeHtml(m.specification || '') + '</div></div></div>'
              + '<div class="text-right"><div class="text-sm font-semibold text-[var(--mc-state-warning)]">' + (m.total_qty || 0) + '</div>'
              + '<div class="text-xs text-muted-foreground">' + escapeHtml(m.unit || '\u4e2a') + '</div></div></div>';
          });
          lowStockContainer.innerHTML = lowHtml;
        }
      }

      /* 最近操作（表头：时间/操作/结果） */
      var opsBody = $('#recent-ops');
      if (opsBody) {
        var opsList = d.recent_ops || [];
        if (!opsList.length) {
          opsBody.innerHTML = '<tr><td colspan="3" class="px-4 py-8 text-center text-sm text-muted-foreground">\u6682\u65e0\u64cd\u4f5c\u8bb0\u5f55</td></tr>';
        } else {
          var opsHtml = '';
          opsList.slice(0, 8).forEach(function (op) {
            opsHtml += '<tr class="hover:bg-muted/50">'
              + '<td class="px-4 py-3 text-sm">' + fmtTime(op.create_time) + '</td>'
              + '<td class="px-4 py-3 text-sm font-medium">' + escapeHtml(op.operation_type || '') + '</td>'
              + '<td class="px-4 py-3 text-sm text-muted-foreground">' + escapeHtml(op.detail || '') + '</td></tr>';
          });
          opsBody.innerHTML = opsHtml;
        }
      }

      /* 未匹配物料 + 缺少物料个数 */
      var shortageEl = document.getElementById('kpi-shortage');
      if (shortageEl) shortageEl.textContent = d.shortage_count || 0;
      var unmatchedBox = $('#unmatched-list');
      if (unmatchedBox) {
        var um = d.unmatched_materials || [];
        if (!um.length) {
          unmatchedBox.innerHTML = '<div class="py-6 text-sm text-muted-foreground text-center">\u65e0\u672a\u5339\u914d\u7269\u6599</div>';
        } else {
          var umHtml = '';
          um.slice(0, 6).forEach(function (m) {
            umHtml += '<div class="flex items-center justify-between py-2.5">'
              + '<div class="flex items-center gap-3 min-w-0">'
              + '<span class="w-2 h-2 rounded-full bg-[var(--mc-state-warning)] shrink-0"></span>'
              + '<div class="min-w-0"><div class="text-sm font-medium truncate">' + escapeHtml(m.material_name) + '</div>'
              + (m.specification ? '<div class="text-xs text-muted-foreground truncate">' + escapeHtml(m.specification) + '</div>' : '')
              + '</div></div>'
              + '<div class="text-right shrink-0"><div class="text-sm font-semibold text-[var(--mc-state-warning)]">' + (m.required_qty || 0) + '</div>'
              + '<div class="text-xs text-muted-foreground">\u7f3a</div></div></div>';
          });
          unmatchedBox.innerHTML = umHtml;
        }
      }

      updateFooterTime();
      refreshIcons();
    });
  }

  /* ════════════════════════════════════════
     cabinet.html — 收纳柜格位
     ════════════════════════════════════════ */
  function initCabinet() {
    api.get_cabinet().then(function (res) {
      if (!res.ok) return;
      var data = res.data;

      /* 摘要统计（按 id 精确挂载） */
      var summary = data.summary || {};
      var statMap = { 'stat-total': summary.total, 'stat-occupied': summary.occupied, 'stat-empty': summary.empty, 'stat-low': summary.low };
      Object.keys(statMap).forEach(function (id) {
        var el = document.getElementById(id);
        if (el && statMap[id] !== undefined) el.textContent = statMap[id];
      });

      /* 格位网格 */
      var grid = $('#cabinet-grid');
      if (grid && data.slots) {
        /* 按行列分组 */
        var cells = {};
        data.slots.forEach(function (s) {
          var key = s.row + '-' + s.col;
          if (!cells[key]) cells[key] = { row: s.row, col: s.col, label: String.fromCharCode(65 + s.row) + (s.col + 1), slots: [] };
          cells[key].slots.push(s);
        });
        var cellList = Object.keys(cells).map(function (k) { return cells[k]; }).sort(function (a, b) {
          return (a.row - b.row) || (a.col - b.col);
        });

        var html = '';
        cellList.forEach(function (cell) {
          var inner = cell.slots.find(function (s) { return s.position === 0; }) || {};
          var outer = cell.slots.find(function (s) { return s.position === 1; }) || {};
          var occupied = (inner.occupied || 0) + (outer.occupied || 0);
          var isLow = inner.status === 'low' || outer.status === 'low';
          var cellStatus = occupied === 0 ? 'empty' : (isLow ? 'low' : 'ok');
          var stateText = cellStatus === 'empty' ? '\u7a7a\u95f2' : (cellStatus === 'low' ? '\u4f4e\u5e93\u5b58' : '\u6b63\u5e38');
          var dataStatus = occupied > 0 ? 'occupied' : 'free';

          html += '<div class="slot-card' + (inner.status === 'error' || outer.status === 'error' ? ' has-error' : '') + '" data-status="' + dataStatus + '" data-slot-label="' + escapeHtml(cell.label) + '">'
            + '<div class="slot-card-header">'
            + '<span class="slot-number">' + escapeHtml(cell.label) + '</span>'
            + '<span class="slot-state">' + stateText + '</span>'
            + '</div><div class="slot-compartments">';

          [inner, outer].forEach(function (s, idx) {
            var label = idx === 0 ? '\u524d\u4ed3' : '\u540e\u4ed3';
            var st = s.occupied ? (s.status || 'ok') : 'empty';
            var dotCls = st === 'empty' ? 'is-empty' : (st === 'low' ? 'is-low' : (st === 'error' ? 'is-error' : ''));
            var matName = s.material_name ? escapeHtml(s.material_name) : '\u7a7a';
            var qty = s.quantity || 0;
            html += '<div class="slot-compartment">'
              + '<div class="compartment-head">'
              + '<span class="compartment-name">' + label + '</span>'
              + '<span class="status-dot ' + dotCls + '"></span>'
              + '</div>'
              + '<div class="material-name">' + matName + '</div>'
              + '<div class="material-qty">' + qty + '</div>'
              + '</div>';
          });

          html += '</div></div>';
        });
        grid.innerHTML = html;
      }

      refreshIcons();
    });

    /* 筛选（all/occupied/free，按 data-status 匹配） */
    var filter = $('[data-dom-id="filter-cabinet-status"]');
    if (filter && !filter.dataset.bridgeBound) {
      filter.dataset.bridgeBound = '1';
      filter.addEventListener('change', function () {
        var val = filter.value;
        $$('.slot-card').forEach(function (card) {
          card.style.display = (!val || val === 'all' || card.getAttribute('data-status') === val) ? '' : 'none';
        });
      });
    }

    var batchBtn = $('[data-dom-id="btn-batch-pick"]');
    if (batchBtn) batchBtn.addEventListener('click', function () { navigate('bom.html'); });
  }

  function materialDialogMarkup() {
    return '<div id="material-dialog" class="fixed inset-0 z-[100] hidden items-center justify-center bg-black/40 p-4">'
      + '<form id="material-dialog-form" class="w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-card p-6 shadow-lg">'
      + '<div class="flex items-center justify-between mb-5"><h2 id="material-dialog-title" class="text-lg font-semibold">新增物料</h2><button type="button" id="material-dialog-close" class="text-muted-foreground" aria-label="关闭"><i data-lucide="x" class="w-5 h-5"></i></button></div>'
      + '<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">'
      + ['material_code|物料编码|必填','name|物料名称|必填','supplier_part|供应商编号|','lcsc_code|立创编号|','specification|规格型号|','package|封装|','category|分类|','min_stock|最低库存|'].map(function (item) { var p = item.split('|'); return '<label class="grid gap-1 text-sm"><span class="text-muted-foreground">' + p[1] + (p[2] ? ' <em class="text-[var(--mc-state-error)]">' + p[2] + '</em>' : '') + '</span><input name="' + p[0] + '" class="field" ' + (p[0] === 'min_stock' ? 'type="number" min="0" value="10"' : '') + '></label>'; }).join('')
      + '<label class="grid gap-1 text-sm sm:col-span-2"><span class="text-muted-foreground">参数（JSON或文本）</span><textarea name="parameters" class="field min-h-24 py-2"></textarea></label>'
      + '</div><p id="material-dialog-hint" class="mt-4 text-sm text-muted-foreground"></p>'
      + '<div class="mt-6 flex justify-end gap-2"><button type="button" id="material-dialog-cancel" class="btn">取消</button><button type="submit" class="btn btn-primary"><i data-lucide="save" class="w-4 h-4"></i>保存并推荐仓位</button></div></form></div>';
  }
  function openMaterialDialog(material) {
    if (!$('#material-dialog')) { document.body.insertAdjacentHTML('beforeend', materialDialogMarkup()); refreshIcons(); }
    var dialog = $('#material-dialog');
    var form = $('#material-dialog-form');
    var title = $('#material-dialog-title');
    if (!form || !dialog) return;
    form.reset();
    var fields = ['material_code','name','supplier_part','lcsc_code','specification','package','category','min_stock','parameters'];
    fields.forEach(function (key) {
      var field = form.elements[key];
      if (!field || !material) return;
      var val = material[key];
      field.value = (val && typeof val === 'object') ? JSON.stringify(val) : (val || '');
    });
    if (title) title.textContent = material ? '编辑物料' : '新增物料';
    dialog.classList.remove('hidden'); dialog.classList.add('flex');
    form.dataset.materialId = material && material.id ? material.id : '';
    var first = form.elements.material_code; if (first) first.focus();
  }
  function closeMaterialDialog() { var dialog = $('#material-dialog'); if (dialog) { dialog.classList.add('hidden'); dialog.classList.remove('flex'); } }
  function bindMaterialDialog() {
    if ($('#material-dialog-form')) return;
    document.body.insertAdjacentHTML('beforeend', materialDialogMarkup());
    refreshIcons();
    $('#material-dialog-close').addEventListener('click', closeMaterialDialog);
    $('#material-dialog-cancel').addEventListener('click', closeMaterialDialog);
    $('#material-dialog').addEventListener('click', function (event) { if (event.target.id === 'material-dialog') closeMaterialDialog(); });
    $('#material-dialog-form').addEventListener('submit', function (event) {
      event.preventDefault();
      var form = event.currentTarget;
      var data = {};
      Array.prototype.forEach.call(form.elements, function (field) { if (field.name) data[field.name] = field.value.trim(); });
      if (!data.material_code || !data.name) { showToast('物料编码和名称不能为空', 'error'); return; }
      data.min_stock = parseInt(data.min_stock || '0', 10) || 0;
      data.unit = '个';
      var params = data.parameters;
      try { data.parameters = params ? JSON.parse(params) : {}; } catch (e) { data.parameters = { text: params }; }
      var id = form.dataset.materialId;
      var request = id ? api.update_material(Number(id), data) : api.create_material(data);
      request.then(function (res) {
        if (!res.ok) { showToast('保存失败：' + (res.error || ''), 'error'); return; }
        closeMaterialDialog();
        var searchEl = $('#filter-material-search');
        loadMaterials(searchEl ? searchEl.value.trim() : '', '');
        return api.suggest_location(data.specification || '', data.package || '', data.category || '').then(function (locationRes) {
          var list = locationRes && locationRes.ok && locationRes.data && locationRes.data.suggestions;
          var item = list && list[0];
          var code = item ? ((item.extra && item.extra.slot_code) || item.slot_code) : '';
          showToast(code ? ((id ? '物料已更新' : '物料已新增') + '，推荐仓位：' + code) : (id ? '物料已更新' : '物料已新增，暂无可用推荐仓位'), 'success');
        });
      }).catch(function (error) { showToast('保存失败：' + error, 'error'); });
    });
  }

  /* ════════════════════════════════════════
     materials.html — 物料主数据
     ════════════════════════════════════════ */
  function initMaterials() {
    var search = $('[data-dom-id="filter-material-search"]');
    var savedKeyword = sessionStorage.getItem('material_search_keyword') ||
      sessionStorage.getItem('global_search_keyword') || '';
    if (search) search.value = savedKeyword;
    var categoryFilter = $('[data-dom-id="filter-material-category"]');
    loadMaterials(savedKeyword, categoryFilter ? categoryFilter.value : '');

    if (categoryFilter) {
      categoryFilter.addEventListener('change', function () {
        loadMaterials(search ? search.value.trim() : '', categoryFilter.value);
      });
    }

    /* 搜索 */
    if (search) {
      var timer = null;
      search.addEventListener('input', function () {
        clearTimeout(timer);
        var kw = search.value.trim();
        sessionStorage.setItem('material_search_keyword', kw);
        sessionStorage.setItem('global_search_keyword', kw);
        timer = setTimeout(function () {
          loadMaterials(kw, categoryFilter ? categoryFilter.value : '');
        }, 300);
      });
    }

    /* 新增物料 */
    bindMaterialDialog();
    var addBtn = $('[data-dom-id="btn-add-material-page"]');
    if (addBtn && !addBtn.dataset.bridgeBound) {
      addBtn.dataset.bridgeBound = '1';
      addBtn.addEventListener('click', function () { openMaterialDialog(); });
    }
    if (sessionStorage.getItem('pending_global_action') === 'add-material') {
      sessionStorage.removeItem('pending_global_action');
      openMaterialDialog();
    }
    var importBtn = $('[data-dom-id="btn-import-lcsc"]');
    if (importBtn) importBtn.addEventListener('click', function () {
      var kw = prompt('\u8f93\u5165\u7acb\u521b\u5546\u57ce\u5173\u952e\u8bcd\u6216\u7f16\u53f7:') || '';
      if (!kw) return;
      showToast('\u6b63\u5728\u641c\u7d22\u7acb\u521b\u5546\u57ce\u2026', 'info');
      api.search_lcsc(kw, 1, 10).then(function (res) {
        if (!res.ok) { showToast('\u641c\u7d22\u5931\u8d25\uff1a' + (res.error || ''), 'error'); return; }
        var products = (res.data && res.data.products) || [];
        if (!products.length) { showToast('\u672a\u627e\u5230\u7ed3\u679c', 'warning'); return; }
        var list = products.slice(0, 8).map(function (p, i) {
          return (i + 1) + '. ' + (p.productModel || p.model || '') + ' | ' + (p.brand || '') + ' | ' + (p.price || '');
        }).join('\n');
        var pick = prompt('\u9009\u62e9\u5e8f\u53f7\u5bfc\u5165(1-' + products.length + '):\n' + list);
        var idx = parseInt(pick, 10) - 1;
        if (isNaN(idx) || idx < 0 || idx >= products.length) return;
        var p = products[idx];
        var pCode = p.productModel || p.model || '';
        api.create_material({
          material_code: pCode,
          name: pCode,
          specification: p.specification || '',
          package: p.package || '',
          category: p.category || '\u672a\u5206\u7c7b',
          min_stock: 10, unit: '\u4e2a'
        }).then(function (r) {
          if (r.ok) { showToast('\u7269\u6599\u5df2\u5bfc\u5165', 'success'); loadMaterials('', ''); }
          else showToast('\u5bfc\u5165\u5931\u8d25\uff1a' + (r.error || ''), 'error');
        });
      });
    });

    /* 加入BOM — 事件委托 */
    var tbody = $('#materials-tbody');
    if (tbody && !tbody.dataset.bridgeBound) {
      tbody.dataset.bridgeBound = '1';
      tbody.addEventListener('click', function (e) {
        var editBtn = e.target.closest('[data-dom-id="btn-edit-material"]');
        if (editBtn) {
          var mat = materialsCache.filter(function (item) { return String(item.id) === String(editBtn.getAttribute('data-material-id')); })[0];
          if (mat) openMaterialDialog(mat);
          return;
        }
        var btn = e.target.closest('[data-dom-id="btn-add-to-bom"]');
        if (!btn) return;
        navigate('bom.html');
      });
    }
  }

  var materialsCache = [];
  function loadMaterials(keyword, category) {
    api.list_materials(keyword, category).then(function (res) {
      if (!res.ok) return;
      var data = res.data;

      /* 统计（按 id 精确挂载） */
      var statMap = {
        'kpi-total': data.materials ? data.materials.length : 0,
        'kpi-week': data.this_week_new || 0,
        'kpi-low': data.low_stock_count || 0,
        'kpi-no-replace': data.no_replacement || 0
      };
      Object.keys(statMap).forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.textContent = statMap[id];
      });
      var pageInfo = $('#page-info');
      if (pageInfo) pageInfo.textContent = '\u5171 ' + (data.materials ? data.materials.length : 0) + ' \u6761';

      /* 表格 */
      var tbody = $('#materials-tbody');
      if (!tbody) return;
      var mats = data.materials || [];
      materialsCache = mats;
      if (mats.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="px-4 py-8 text-center text-sm text-muted-foreground">\u6682\u65e0\u7269\u6599\u6570\u636e</td></tr>';
        return;
      }
      var html = '';
      mats.forEach(function (m) {
        var statusCls = m.total_qty <= (m.min_stock || 0) ? 'text-[var(--mc-state-warning)]' : 'text-[var(--mc-state-success)]';
        var statusText = m.total_qty <= (m.min_stock || 0) ? '\u4f4e\u5e93\u5b58' : '\u6b63\u5e38';
        var slots = String(m.slot_codes || '').trim();
        html += '<tr class="border-b border-border last:border-0 hover:bg-muted/50">'
          + '<td class="px-4 py-3 text-sm font-mono whitespace-nowrap">' + escapeHtml(m.material_code) + '</td>'
          + '<td class="px-4 py-3 text-sm font-medium">' + escapeHtml(m.material_name || '')
          + (m.specification ? '<div class="text-xs text-muted-foreground font-normal">' + escapeHtml(m.specification) + '</div>' : '') + '</td>'
          + '<td class="px-4 py-3 text-sm text-muted-foreground whitespace-nowrap">' + escapeHtml(m.category || '') + '</td>'
          + '<td class="px-4 py-3 text-sm text-muted-foreground whitespace-nowrap">' + escapeHtml(m.package || '') + '</td>'
          + '<td class="px-4 py-3 text-sm font-semibold text-right whitespace-nowrap">' + (m.total_qty || 0) + '</td>'
          + '<td class="px-4 py-3 text-sm text-muted-foreground" title="' + escapeHtml(slots) + '">' + (slots
            ? '<span class="inline-block max-w-[150px] overflow-hidden text-ellipsis align-bottom whitespace-nowrap">' + escapeHtml(slots) + '</span>'
            : '-') + '</td>'
          + '<td class="px-4 py-3 text-sm ' + statusCls + ' font-medium whitespace-nowrap">' + statusText + '</td>'
          + '<td class="px-4 py-3"><div class="flex items-center gap-2 whitespace-nowrap">'
          + '<a data-dom-id="link-cabinet" href="cabinet.html" class="text-[var(--mc-blue)] hover:underline text-sm">\u67e5\u770b</a>'
          + '<button type="button" data-dom-id="btn-edit-material" class="text-[var(--mc-blue)] hover:underline text-sm" data-material-id="' + m.id + '">\u7f16\u8f91</button>'
          + '<button data-dom-id="btn-add-to-bom" class="text-[var(--mc-blue)] hover:underline text-sm" data-material-id="' + m.id + '">\u52a0\u5165BOM</button>'
          + '</div></td></tr>';
      });
      tbody.innerHTML = html;
      refreshIcons();
    });
  }

  /* 导入成功后默认全选（操作后刷新时不再自动全选）——全局标志，供 renderBomItems 等访问 */
  var pendingAutoSelect = false;
  function selectAllRows() {
    $$('.row-check').forEach(function (cb) {
      if (!cb.disabled) cb.checked = true;
    });
    syncCheckAll();
  }
  /* 渲染后同步全选框状态（防止重渲染后「全选勾了但行未勾」） */
  function syncCheckAll() {
    var rows = $$('.row-check');
    var ca = $('#check-all-rows');
    if (!ca) return;
    var selectable = Array.prototype.filter.call(rows, function (cb) {
      return !cb.disabled;
    });
    ca.checked = selectable.length > 0 && selectable.every(function (cb) {
      return cb.checked;
    });
  }

  /* ════════════════════════════════════════
     bom.html — BOM 导入与领料
     ════════════════════════════════════════ */
  function initBom() {
    var typeSelect = $('[data-dom-id="bom-operation-type"]');
    /* 操作类型跨页面保留：进入页面时恢复上次选择 */
    if (typeSelect) {
      var savedType = sessionStorage.getItem('bom_operation_type');
      if (savedType === 'restock' || savedType === 'pick') typeSelect.value = savedType;
    }
    var restockMode = typeSelect && typeSelect.value === 'restock';
    var confirmBtn = $('[data-dom-id="btn-confirm-action"]');
    var pendingRestockPlan = [];
    var slotOptions = [];
    /* ── BOM 会话缓存：切换页面返回时直接恢复现场，避免每次重新加载/重新联网匹配 ── */
    var BOM_SESSION_KEY = 'bom_session_state';
    function saveBomSession(stage) {
      try {
        sessionStorage.setItem(BOM_SESSION_KEY, JSON.stringify({
          bom_id: pendingBomId,
          mode: restockMode ? 'restock' : 'pick',
          stage: stage || 2,
          items: pendingBomItems || [],
          plan: pendingRestockPlan || []
        }));
      } catch (e) { /* 忽略 */ }
    }
    function clearBomSession() {
      try { sessionStorage.removeItem(BOM_SESSION_KEY); } catch (e) { /* 忽略 */ }
    }
    function restoreBomSession() {
      try { return JSON.parse(sessionStorage.getItem(BOM_SESSION_KEY) || 'null'); }
      catch (e) { return null; }
    }
    api.get_slot_options('').then(function (res) {
      if (res.ok) {
        slotOptions = res.data.slots || [];
        if (restockMode && pendingRestockPlan.length) renderRestockPlan(pendingRestockPlan);
      }
    });   /* 补货计划缓存（供确认后构建概览需求） */
    /* 合并确认按钮：领料/补货模式切换文案与样式 */
    function updateConfirmBtn() {
      if (!confirmBtn) return;
      var label = $('#btn-confirm-label');
      if (label) label.textContent = restockMode ? '\u786e\u8ba4\u8865\u8d27\u5165\u5e93' : '\u786e\u8ba4\u9886\u6599';
      confirmBtn.className = restockMode
        ? 'inline-flex items-center gap-2 px-4 py-2 rounded-md bg-[var(--mc-state-success)] text-white text-sm font-medium hover:opacity-90 transition-colors'
        : 'inline-flex items-center gap-2 px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors';
      var ic = confirmBtn.querySelector('[data-lucide]');
      if (ic) ic.setAttribute('data-lucide', restockMode ? 'package-check' : 'check-circle');
      var hint = $('#upload-mode-hint');
      if (hint) hint.textContent = restockMode ? '\u8865\u8d27\u5165\u5e93' : '\u9886\u6599\u51fa\u5e93';
      var s4 = $('#step4-label');
      if (s4) s4.textContent = restockMode ? '\u786e\u8ba4\u8865\u8d27' : '\u786e\u8ba4\u9886\u6599';
      refreshIcons();
    }
    /* 刷新流程步骤状态：当前步骤黑、前序绿、后续灰（stage=1..4） */
    function refreshStepState(stage) {
      $$('[data-step]').forEach(function (el) {
        var n = Number(el.getAttribute('data-step'));
        var done = n < stage;
        var current = n === stage;
        var dot = el.querySelector('.step-dot');
        var label = el.querySelector('.step-label');
        var line = el.querySelector('.step-line');
        if (dot) dot.className = 'step-dot w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold ' + (done ? 'bg-[var(--mc-state-success)] text-white' : (current ? 'bg-foreground text-background' : 'bg-muted text-muted-foreground'));
        if (label) label.className = 'step-label text-sm font-medium ' + (done ? 'text-[var(--mc-state-success)]' : (current ? 'text-foreground' : 'text-muted-foreground'));
        if (line) line.style.backgroundColor = done ? 'var(--mc-state-success)' : '#e5e7eb';
      });
    }
    updateConfirmBtn();
    refreshStepState(1);   /* 初始：上传 BOM 为当前步骤 */
    swapBomTableHeaders(restockMode);
    if (typeSelect) typeSelect.addEventListener('change', function () {
      restockMode = typeSelect.value === 'restock';
      sessionStorage.setItem('bom_operation_type', typeSelect.value);
      pendingBomItems = [];
      pendingRestockPlan = [];
      clearBomSession();
      refreshStepState(1);
      updateConfirmBtn();
      swapBomTableHeaders(restockMode);
      /* 仅渲染目标模式的空态，避免 10/11 列表头与旧行短暂错位 */
      if (restockMode) renderRestockPlan([]);
      else renderBomItems([]);
      if (pendingBomId) {
        showToast('操作类型已切换，正在按新类型重新匹配', 'info');
        if (restockMode) refreshRestockPlan();
        else autoMatchAndRender();
      }
    });

    /* 上传按钮 + 拖拽上传 */
    var uploadBtn = $('[data-dom-id="btn-upload-bom"]');

    function applyBomResult(res, fallbackName) {
      if (!res.ok) { showToast(res.error || '\u5bfc\u5165\u5931\u8d25', 'error'); return; }
      refreshStepState(2);   /* 已上传并解析：解析匹配为当前步骤 */
      var d = res.data || {};
      /* 多文件批量导入：后端返回 {imported, failed, summary} */
      if (d.summary) {
        var okCount = (d.imported || []).length;
        var badCount = (d.failed || []).length;
        showToast('BOM \u6279\u91cf\u5bfc\u5165\uff1a' + d.summary, badCount ? 'warning' : 'success');
        if (badCount) {
          (d.failed || []).forEach(function (f) { showToast(f.file + '\uff1a' + f.error, 'error'); });
        }
        var lastOk = (d.imported || []).slice(-1)[0];
        if (lastOk && lastOk.bom_id) {
          pendingBomId = lastOk.bom_id;
          pendingAutoSelect = true;
          saveBomSession(2);
          if (restockMode) refreshRestockPlan();
          else autoMatchAndRender();
        }
        return;
      }
      showToast('BOM \u5bfc\u5165\u6210\u529f\uff1a' + (d.bom_name || fallbackName || ''), 'success');
      pendingBomId = d.bom_id;
      pendingBomItems = d.items || [];
      pendingAutoSelect = true;
      saveBomSession(2);
      if (restockMode) refreshRestockPlan();
      else autoMatchAndRender();
    }

    /* 领料模式导入后自动执行库存匹配：先立即显示导入清单，匹配完成再刷新结果 */
    function autoMatchAndRender() {
      if (pendingBomItems && pendingBomItems.length) renderBomItems(pendingBomItems);
      api.match_bom(pendingBomId).then(function (mr) {
        if (mr.ok) {
          refreshStepState(3);   /* 匹配完成：库存核对为当前步骤 */
          pendingBomItems = mr.data.items || [];
          renderBomItems(pendingBomItems);
          saveBomSession(3);
        } else {
          showToast(mr.error || '\u81ea\u52a8\u5339\u914d\u5931\u8d25\uff0c\u53ef\u70b9\u51fb\u300c\u5339\u914d\u5e93\u5b58\u300d\u91cd\u8bd5', 'warning');
          api.get_bom(pendingBomId).then(function (bomRes) {
            if (bomRes.ok && bomRes.data && bomRes.data.items) {
              pendingBomItems = bomRes.data.items;
              renderBomItems(pendingBomItems);
            }
          });
        }
      });
    }

    /* 支持一次拖入多个 BOM 文件：全部合并到当前清单（curBomId 续接追加） */
    function handleBomFiles(fileList) {
      var files = Array.prototype.slice.call(fileList || []).filter(function (f) {
        return /\.(csv|xlsx|xls)$/i.test(f.name || '');
      });
      if (!files.length) {
        showToast('\u4ec5\u652f\u6301 .csv / .xlsx / .xls \u6587\u4ef6', 'error');
        return;
      }
      var curBomId = pendingBomId || null;
      var chain = Promise.resolve();
      var results = [];
      files.forEach(function (file) {
        chain = chain.then(function () {
          return new Promise(function (resolve) {
            var reader = new FileReader();
            reader.onload = function () {
              var result = String(reader.result || '');
              var idx = result.indexOf(',');
              var base64 = idx >= 0 ? result.slice(idx + 1) : result;
              showToast('\u6b63\u5728\u89e3\u6790 ' + (file.name || '') + ' \u2026', 'info');
              api.import_bom_bytes(file.name, base64, restockMode ? 'restock' : 'pick', curBomId).then(function (res) {
                results.push(res);
                if (res && res.ok && res.data && res.data.bom_id) curBomId = res.data.bom_id;
                resolve();
              });
            };
            reader.onerror = function () {
              showToast('\u6587\u4ef6\u8bfb\u53d6\u5931\u8d25\uff1a' + (file.name || ''), 'error');
              resolve();
            };
            reader.readAsDataURL(file);
          });
        });
      });
      chain.then(function () {
        var okItems = results.filter(function (r) { return r && r.ok; });
        if (!okItems.length) {
          showToast((results[0] && results[0].error) || '\u5bfc\u5165\u5931\u8d25', 'error');
          return;
        }
        if (files.length > 1) {
          showToast('\u6279\u91cf\u5bfc\u5165\u5b8c\u6210\uff1a' + okItems.length + '/' + files.length + ' \u4e2a\u6210\u529f', 'success');
        }
        var last = okItems[okItems.length - 1];
        applyBomResult(last, last && last.data && last.data.bom_name);
      });
    }

    if (uploadBtn && !uploadBtn.dataset.bridgeBound) {
      uploadBtn.dataset.bridgeBound = '1';
      uploadBtn.addEventListener('click', function () {
        api.import_bom(restockMode ? 'restock' : 'pick', pendingBomId || null).then(function (res) {
          applyBomResult(res);
        });
      });
    }
    if (sessionStorage.getItem('pending_global_action') === 'import-bom') {
      sessionStorage.removeItem('pending_global_action');
      setTimeout(function () { if (uploadBtn) uploadBtn.click(); }, 100);
    }

    var dropZone = $('#bom-drop-zone');
    if (dropZone && !dropZone.dataset.bridgeBound) {
      dropZone.dataset.bridgeBound = '1';
      if (uploadBtn) dropZone.addEventListener('click', function () { uploadBtn.click(); });
      ['dragenter', 'dragover'].forEach(function (evt) {
        dropZone.addEventListener(evt, function (e) {
          e.preventDefault();
          e.stopPropagation();
          dropZone.classList.add('drag-active');
        }, false);
      });
      ['dragleave', 'drop'].forEach(function (evt) {
        dropZone.addEventListener(evt, function (e) {
          e.preventDefault();
          e.stopPropagation();
          dropZone.classList.remove('drag-active');
        }, false);
      });
      dropZone.addEventListener('drop', function (e) {
        var files = e.dataTransfer && e.dataTransfer.files;
        if (files && files.length) handleBomFiles(files);
      }, false);
    }

    /* 确认补货入库：仅处理已勾选且已指定格位的行，成功后跳转概览 */
    function doConfirmRestock() {
      var rows = $$('[data-restock-item-id]');
      var checked = getCheckedIds();
      var tasks = [];
      var submittedIds = [];
      var demandMap = {};
      if (!checked.size) { showToast('\u8bf7\u5148\u52fe\u9009\u8981\u8865\u8d27\u7684\u7269\u6599', 'warning'); return; }
      rows.forEach(function (row) {
        var itemId = row.getAttribute('data-restock-item-id');
        if (!checked.has(itemId)) return;
        var slot = row.querySelector('[data-restock-slot]');
        var qty = row.querySelector('[data-restock-qty]');
        var slotId = slot && slot.getAttribute('data-slot-id');
        if (slot && slotId && qty && Number(qty.value) > 0) {
          submittedIds.push(itemId);
          tasks.push(api.confirm_restock_item(itemId, Number(slotId), Number(qty.value)));
          var selectedSlot = slotOptions.filter(function (candidate) {
            return String(candidate.id) === String(slotId);
          })[0];
          var code = selectedSlot ? selectedSlot.slot_code : '';
          var sp = '';
          var planItem = (pendingRestockPlan || []).filter(function (p) { return String(p.item.id) === String(itemId); })[0];
          if (planItem) sp = planItem.item.supplier_part || planItem.item.comment || planItem.item.material_name || '';
          if (code) demandMap[code] = { sp: sp, picked: false };
        }
      });
      if (tasks.length !== checked.size) { showToast('\u5df2\u52fe\u9009\u7269\u6599\u4e2d\u5b58\u5728\u672a\u6307\u5b9a\u683c\u4f4d\u6216\u6570\u91cf\u65e0\u6548\uff0c\u8bf7\u8865\u9f50\u540e\u518d\u786e\u8ba4', 'warning'); return; }
      if (!tasks.length) { showToast('\u8bf7\u5148\u52fe\u9009\u6709\u6548\u8865\u8d27\u9879', 'warning'); return; }
      Promise.all(tasks).then(function (results) {
        var failed = results.filter(function (r) { return !r.ok; });
        var successfulIds = new Set();
        results.forEach(function (result, index) {
          if (result.ok) successfulIds.add(String(submittedIds[index]));
        });
        if (failed.length) {
          refreshStepState(3);
          showToast(failed[0].error || '部分补货失败，失败行已保留', 'error');
        } else {
          refreshStepState(4);
          showToast('补货入库已确认，库存已更新', 'success');
        }
        if (Object.keys(demandMap).length) {
          try { localStorage.setItem('restock_demand', JSON.stringify(demandMap)); } catch (e) { /* 忽略 */ }
        }
        /* 与确认领料一致：跳转概览查看补货位置 */
        if (successfulIds.size) {
          removeCheckedItems(successfulIds).then(function () {
            if (!failed.length) {
              pendingBomId = null;
              pendingBomItems = [];
              pendingRestockPlan = [];
              clearBomSession();
              showToast('\u8865\u8d27\u5b8c\u6210\uff0c\u5df2\u8df3\u8f6c\u6982\u89c8\u67e5\u770b\u8865\u8d27\u4f4d\u7f6e', 'success');
              setTimeout(function () { navigate('index.html'); }, 400);
            }
          });
        }
        /* 全部失败：保留勾选行与补货计划，停留在库存核对等待处理 */
      });
    }

    function refreshRestockPlan() {
      if (!pendingBomId) return;
      api.match_bom(pendingBomId, true).then(function (res) {
        if (!res.ok) {
          showToast(res.error || '\u5e93\u5b58\u5339\u914d\u5931\u8d25', 'error');
          return;
        }
        pendingBomItems = res.data.items || [];
        return api.get_restock_plan(pendingBomId);
      }).then(function (res) {
        if (!res) return;
        if (res.ok) {
          refreshStepState(3);
          pendingRestockPlan = res.data.plan || [];
          renderRestockPlan(pendingRestockPlan);
          saveBomSession(3);
        } else {
          showToast(res.error || '\u65e0\u6cd5\u52a0\u8f7d\u8865\u8d27\u5efa\u8bae', 'error');
        }
      });
    }

    /* 匹配按钮 — 补货强制刷新库存与仓位，领料按原有 AI/本地流程 */
    var matchBtn = $('[data-dom-id="btn-match-inventory"]');
    if (matchBtn) matchBtn.addEventListener('click', function () {
      if (!pendingBomId) { showToast('\u8bf7\u5148\u5bfc\u5165 BOM \u6587\u4ef6', 'warning'); return; }
      matchBtn.disabled = true; matchBtn.textContent = '\u5339\u914d\u4e2d\u2026';
      if (restockMode) {
        api.match_bom(pendingBomId, true).then(function (res) {
          if (!res.ok) {
            showToast(res.error || '\u5339\u914d\u5931\u8d25', 'error');
            return;
          }
          pendingBomItems = res.data.items || [];
          return api.get_restock_plan(pendingBomId);
        }).then(function (res) {
          matchBtn.disabled = false; matchBtn.textContent = '\u5e93\u5b58\u5339\u914d';
          if (!res) return;
          if (res.ok) {
            refreshStepState(3);
            pendingRestockPlan = res.data.plan || [];
            renderRestockPlan(pendingRestockPlan);
            saveBomSession(3);
            showToast('\u5f53\u524d\u5e93\u5b58\u5df2\u91cd\u65b0\u5339\u914d\uff0c\u5b58\u653e\u4f4d\u5df2\u91cd\u65b0\u63a8\u8350', 'success');
          } else {
            showToast(res.error || '\u65e0\u6cd5\u52a0\u8f7d\u8865\u8d27\u5efa\u8bae', 'error');
          }
        });
        return;
      }
      api.get_ai_status().then(function (aiRes) {
        var useAI = aiRes.ok && aiRes.data && aiRes.data.available;
        var matchFn = useAI ? api.ai_match_bom(pendingBomId) : api.match_bom(pendingBomId);
        matchFn.then(function (res) {
          matchBtn.disabled = false; matchBtn.textContent = '\u5e93\u5b58\u5339\u914d';
          if (!res.ok) {
            if (useAI) {
              showToast('AI \u5339\u914d\u5931\u8d25\uff0c\u56de\u9000\u672c\u5730\u5339\u914d', 'warning');
              api.match_bom(pendingBomId).then(function (r2) {
                if (r2.ok) { pendingBomItems = r2.data.items || []; renderBomItems(pendingBomItems); }
              });
            } else {
              showToast(res.error || '\u5339\u914d\u5931\u8d25', 'error');
            }
            return;
          }
          showToast(useAI ? 'AI \u667a\u80fd\u5339\u914d\u5b8c\u6210' : '\u672c\u5730\u5e93\u5b58\u5339\u914d\u5b8c\u6210', 'success');
          refreshStepState(3);
          pendingBomItems = res.data.items || [];
          /* 匹配统计提示 */
          var stats = { matched: 0, unmatched: 0 };
          pendingBomItems.forEach(function (it2) {
            if (it2.match_status === 'fully' || it2.match_status === 'partial' || it2.match_status === 'replaced') stats.matched++;
            else stats.unmatched++;
          });
          if (stats.unmatched) {
            showToast('\u5339\u914d ' + stats.matched + ' \u9879\uff0c\u672a\u5339\u914d ' + stats.unmatched + ' \u9879\uff08\u53ef\u5148\u5b8c\u5584\u7f16\u53f7\u540e\u91cd\u65b0\u5339\u914d\uff09', 'info');
          }
          renderBomItems(pendingBomItems);
          saveBomSession(3);
        });
      });
    });

    /* 确认领料：只处理勾选行（未勾选时处理全部已匹配行），完成后跳转概览显示需求 */
    function doConfirmPick() {
      if (!pendingBomItems.length) { showToast('\u65e0\u53ef\u9886\u6599\u9879\u76ee', 'warning'); return; }
      var checked = getCheckedIds();
      var scope = pendingBomItems.filter(function (it) {
        if (checked.size) return checked.has(String(it.id));
        return it.match_status === 'fully' || it.match_status === 'partial' || it.match_status === 'replaced';
      });
      if (!scope.length) {
        showToast(checked.size ? '\u8bf7\u52fe\u9009\u8981\u9886\u6599\u7684\u7269\u6599' : '\u65e0\u5339\u914d\u6210\u529f\u7684\u9879\u76ee', 'warning');
        return;
      }
      /* 领料前快照：构建 仓格 -> {need, ok, picked} 映射（用领料前库存判断是否够取） */
      var demand = {};
      scope.forEach(function (it) {
        var need = parseInt(it.required_qty || 0) - parseInt(it.picked_qty || 0);
        if (need > 0 && it.slot_code) {
          demand[it.slot_code] = { need: need, ok: parseInt(it.inventory_quantity || 0) >= need, picked: false };
        }
      });
      /* 只领未领足的差值，避免重复扣库存 */
      var tasks = [];
      scope.forEach(function (it) {
        var need = parseInt(it.required_qty || 0) - parseInt(it.picked_qty || 0);
        if (need > 0) tasks.push(api.confirm_pick(it.id, need));
      });
      if (!tasks.length) { showToast('\u52fe\u9009\u9879\u76ee\u5df2\u5168\u90e8\u9886\u5b8c', 'info'); return; }
      Promise.all(tasks).then(function () {
        refreshStepState(4);
        try { localStorage.setItem('pick_demand', JSON.stringify(demand)); } catch (e) { /* 忽略 */ }
        if (checked.size) {
          /* 操作后清除已勾选行 */
          removeCheckedItems(checked).then(function () {
            pendingBomId = null;
            pendingBomItems = [];
            pendingRestockPlan = [];
            clearBomSession();
            showToast('\u9886\u6599\u5b8c\u6210\uff0c\u5df2\u79fb\u9664\u52fe\u9009\u884c', 'success');
            setTimeout(function () { navigate('index.html'); }, 400);
          });
        } else {
          showToast('\u9886\u6599\u5b8c\u6210\uff0c\u5df2\u8df3\u8f6c\u6982\u89c8\u67e5\u770b\u4ed3\u683c\u9700\u6c42', 'success');
          setTimeout(function () { navigate('index.html'); }, 600);
        }
      });
    }

    /* 合并确认按钮：按模式分发到领料/补货 */
    if (confirmBtn) confirmBtn.addEventListener('click', function () {
      if (restockMode) doConfirmRestock();
      else doConfirmPick();
    });

    /* 清除：清空本地状态并回退到第一步（上传 BOM） */
    var clearBtn = $('[data-dom-id="btn-clear-bom"]');
    if (clearBtn) clearBtn.addEventListener('click', function () {
      var resetView = function () {
        pendingBomId = null; pendingBomItems = []; pendingRestockPlan = [];
        clearBomSession();
        if (restockMode) renderRestockPlan([]);
        else renderBomItems([]);
        refreshStepState(1);
        updateConfirmBtn();
        syncCheckAll();
      };
      if (pendingBomId) {
        api.delete_bom(pendingBomId).then(function () {
          showToast('BOM \u5df2\u6e05\u9664', 'success');
          resetView();
        });
      } else {
        resetView();
      }
    });

    /* 全选当前清单 */
    var checkAll = $('#check-all-rows');
    if (checkAll && !checkAll.dataset.bridgeBound) {
      checkAll.dataset.bridgeBound = '1';
      checkAll.addEventListener('change', function () {
        var checked = checkAll.checked;
        $$('.row-check').forEach(function (cb) {
          if (!cb.disabled) cb.checked = checked;
        });
        syncCheckAll();
      });
    }
    var bomTable = $('#bom-tbody');
    if (bomTable && !bomTable.dataset.checkBound) {
      bomTable.dataset.checkBound = '1';
      bomTable.addEventListener('change', function (event) {
        if (event.target.classList.contains('row-check')) syncCheckAll();
      });
    }

    /* 收集勾选的 item_id 集合 */
    function getCheckedIds() {
      var ids = new Set();
      $$('.row-check').forEach(function (cb) {
        if (cb.checked && cb.getAttribute('data-check-item')) ids.add(cb.getAttribute('data-check-item'));
      });
      return ids;
    }

    /* 操作完成后从清单移除勾选行（后端删除 + 前端刷新） */
    function removeCheckedItems(ids) {
      if (!ids || !ids.size) return Promise.resolve();
      var list = Array.from(ids);
      return api.remove_bom_items(pendingBomId, list).then(function (res) {
        if (res.ok && res.data && res.data.items) {
          pendingBomItems = res.data.items;
        } else {
          pendingBomItems = pendingBomItems.filter(function (it) { return !ids.has(String(it.id)); });
        }
        if (restockMode) loadRestockPlan();
        else renderBomItems(pendingBomItems);
        saveBomSession(3);
        var ca = $('#check-all-rows');
        if (ca) ca.checked = false;
      });
    }

    function swapBomTableHeaders(isRestock) {
      var thead = $('#bom-thead-row');
      if (!thead) return;
      if (isRestock) {
        thead.innerHTML = '<th class="px-4 py-3 font-medium w-10">勾选</th>'
          + '<th class="px-4 py-3 font-medium">行号</th>'
          + '<th class="px-4 py-3 font-medium">物料名称/编码</th>'
          + '<th class="px-4 py-3 font-medium">Supplier Part</th>'
          + '<th class="px-4 py-3 font-medium">需求数量</th>'
          + '<th class="px-4 py-3 font-medium">待补数量</th>'
          + '<th class="px-4 py-3 font-medium">推荐存放格位</th>'
          + '<th class="px-4 py-3 font-medium">补货数量</th>'
          + '<th class="px-4 py-3 font-medium">匹配说明</th>';
      } else {
        thead.innerHTML = '<th class="px-4 py-3 font-medium w-10">勾选</th>'
          + '<th class="px-4 py-3 font-medium">行号</th>'
          + '<th class="px-4 py-3 font-medium">物料名称/编码</th>'
          + '<th class="px-4 py-3 font-medium">Supplier Part</th>'
          + '<th class="px-4 py-3 font-medium">需求数量</th>'
          + '<th class="px-4 py-3 font-medium">匹配状态</th>'
          + '<th class="px-4 py-3 font-medium">所在格位</th>'
          + '<th class="px-4 py-3 font-medium">缺口</th>';
      }
    }

    function loadRestockPlan() {
      if (!pendingBomId) return;
      api.get_restock_plan(pendingBomId).then(function (res) {
        if (res.ok) {
          refreshStepState(3);   /* 补货计划已生成 = 库存核对完成 */
          renderRestockPlan(res.data.plan || []);
          saveBomSession(3);
        }
        else showToast(res.error || '无法加载补货建议', 'error');
      });
    }

    function normalizeSlotCode(value) {
      return String(value || '').trim().toLowerCase()
        .replace(/^([a-e])0+(\d)/, '$1$2')
        .replace(/\s+/g, '');
    }

    function getSlotLabel(slot) {
      var code = slot ? String(slot.slot_code || '') : '';
      code = code.replace(/^([A-E])(\d)-/, '$10$2-');
      return code.replace('-内', ' 前仓').replace('-外', ' 后仓');
    }

    function bindRestockSlotInput(input) {
      if (!input) return;
      var update = function () {
        var value = String(input.value || '').trim().toLowerCase();
        var found = slotOptions.filter(function (slot) {
          var raw = normalizeSlotCode(slot.slot_code);
          var label = normalizeSlotCode(getSlotLabel(slot));
          return raw === normalizeSlotCode(value) || label === normalizeSlotCode(value);
        })[0];
        input.setAttribute('data-slot-id', found ? found.id : '');
        input.setCustomValidity(found || !value ? '' : '请选择有效格位');
        input.classList.toggle('border-red-500', Boolean(value && !found));
        var row = input.closest('[data-restock-item-id]');
        var checkbox = row && row.querySelector('.row-check');
        if (checkbox && input.disabled === false) checkbox.disabled = !found;
        if (checkbox && !found) checkbox.checked = false;
        syncCheckAll();
      };
      input.addEventListener('input', update);
      input.addEventListener('change', update);
      update();
    }

    function renderRestockPlan(plan) {
      pendingRestockPlan = plan || [];
      var tbody = $('#bom-tbody');
      if (!tbody) return;
      if (!plan.length) {
        var emptyMsg = pendingBomId ? '\u8865\u8d27 BOM \u5df2\u5168\u90e8\u5b8c\u6210'
                                    : '\u5bfc\u5165 BOM \u540e\u5c06\u663e\u793a\u8865\u8d27\u6e05\u5355';
        tbody.innerHTML = '<tr><td colspan="9" class="px-5 py-8 text-center text-sm text-muted-foreground">' + emptyMsg + '</td></tr>';
        return;
      }
      var html = '';
      var lists = [];
      plan.forEach(function (entry, index) {
        var item = entry.item || {};
        var suggestions = entry.suggestions || [];
        var suggested = suggestions.length ? suggestions[0] : null;
        var qty = Number(entry.remaining_qty || 0);
        var materialReady = Boolean(entry.material_ready);
        var stockMatched = Boolean(entry.has_stock_match);
        var canRestock = Boolean(entry.can_restock);
        var canSelect = Boolean(canRestock && suggested && qty > 0);
        var inputEnabled = Boolean(materialReady && qty > 0);
        var listId = 'restock-slots-' + index;
        lists.push({ id: listId, slots: slotOptions });
        var suggestedRawCode = suggested && ((suggested.extra && suggested.extra.slot_code)
          || (suggested.slot_info && suggested.slot_info.slot_code) || '');
        var suggestedCode = suggestedRawCode ? getSlotLabel({ slot_code: suggestedRawCode }) : '';
        var suggestedId = suggested ? suggested.slot_id : '';
        var options = slotOptions.map(function (slot) {
          return '<option value="' + escapeHtml(getSlotLabel(slot)) + '"></option>';
        }).join('');
        var checkboxDisabled = canSelect ? '' : ' disabled';
        var inputDisabled = inputEnabled ? '' : ' disabled';
        var checked = canSelect ? ' checked' : '';
        var displayName = item.comment || item.material_name || item.material_code || '';
        var reason = !materialReady ? '物料信息不足，无法建立物料档案' :
          (suggested ? (stockMatched ? '已有相同物料库存，优先复用原格位' : '当前无库存，已推荐空仓，可直接补货') : '暂无可用格位，请手动指定');
        html += '<tr class="border-b border-border last:border-0 hover:bg-muted/50" data-restock-item-id="' + item.id + '">'
          + '<td class="px-4 py-3"><input type="checkbox" class="row-check" data-check-item="' + item.id + '"' + checked + checkboxDisabled + '></td>'
          + '<td class="px-4 py-3 font-mono">' + escapeHtml(item.line_no || '') + '</td>'
          + '<td class="px-4 py-3 text-sm" style="max-width:200px">' + escapeHtml(displayName)
          + (item.material_code ? '<div class="text-xs text-muted-foreground font-mono">' + escapeHtml(item.material_code) + '</div>' : '') + '</td>'
          + '<td class="px-4 py-3 text-sm font-mono text-muted-foreground" title="' + escapeHtml(item.supplier_part || '') + '"><span class="inline-block max-w-[130px] overflow-hidden text-ellipsis align-bottom whitespace-nowrap">' + escapeHtml(item.supplier_part || '') + '</span></td>'
          + '<td class="px-4 py-3">' + (item.required_qty || 0) + '</td>'
          + '<td class="px-4 py-3 font-semibold">' + qty + '</td>'
          + '<td class="px-4 py-3"><input data-restock-slot list="' + listId + '" value="' + escapeHtml(suggestedCode) + '" data-slot-id="' + escapeHtml(suggestedId) + '" placeholder="输入检索格位" class="max-w-[150px] h-8 rounded border border-border bg-card px-2 text-xs"' + inputDisabled + '><datalist id="' + listId + '">' + options + '</datalist></td>'
          + '<td class="px-4 py-3"><input data-restock-qty type="number" min="1" max="' + qty + '" value="' + qty + '" class="w-16 h-8 rounded border border-border bg-card px-2 text-sm"' + inputDisabled + '></td>'
          + '<td class="px-4 py-3 text-xs text-muted-foreground" title="' + escapeHtml(reason) + '"><span class="inline-block max-w-[140px] overflow-hidden text-ellipsis align-bottom whitespace-nowrap">' + escapeHtml(reason) + '</span></td></tr>';
      });
      tbody.innerHTML = html;
      $$('[data-restock-slot]').forEach(bindRestockSlotInput);
      refreshIcons();
      if (pendingAutoSelect) { pendingAutoSelect = false; selectAllRows(); }
      syncCheckAll();
    }

    /* 启动：优先恢复上次 BOM 会话（切换页面返回不重新匹配/不重新加载），
       无会话时才回退到最近一条 BOM 记录 */
    var session = restoreBomSession();
    if (session && session.bom_id
        && session.mode === (restockMode ? 'restock' : 'pick')) {
      pendingBomId = session.bom_id;
      pendingRestockPlan = session.plan || [];
      pendingBomItems = session.items || [];
      var restoredStage = Math.max(2, Math.min(4, Number(session.stage) || 2));
      if (restockMode) {
        /* 补货计划等 slotOptions 就绪后由 get_slot_options 回调统一渲染 */
        refreshStepState(restoredStage);
        updateConfirmBtn();
      } else if (pendingBomItems.length) {
        renderBomItems(pendingBomItems);
        refreshStepState(restoredStage);
        updateConfirmBtn();
      }
    } else {
      api.list_bom_records().then(function (res) {
        if (res.ok && res.data && res.data.records && res.data.records.length > 0) {
          var latest = res.data.records[0];
          pendingBomId = latest.id;
          if (restockMode) refreshRestockPlan();
          else api.get_bom(latest.id).then(function (bomRes) {
            if (bomRes.ok && bomRes.data && bomRes.data.items) {
              pendingBomItems = bomRes.data.items;
              renderBomItems(pendingBomItems);
            }
          });
        }
      });
    }
  }

  function renderBomItems(items) {
    var tbody = $('#bom-tbody');
    if (!tbody) return;
    if (!items || items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="px-4 py-8 text-center text-sm text-muted-foreground">\u5bfc\u5165 BOM \u540e\u5c06\u663e\u793a\u5339\u914d\u7ed3\u679c</td></tr>';
      return;
    }
    var html = '';
    items.forEach(function (it, idx) {
      var isMatched = it.match_status === 'fully' || it.match_status === 'partial' || it.match_status === 'replaced';
      var matchCls = isMatched ? 'text-[var(--mc-state-success)]' : 'text-[var(--mc-state-warning)]';
      var matchText = it.match_status === 'fully' ? '\u5df2\u5339\u914d' :
                      it.match_status === 'partial' ? '\u90e8\u5206\u5339\u914d' :
                      it.match_status === 'replaced' ? '\u66ff\u4ee3\u5339\u914d' : '\u672a\u5339\u914d';
      var need = parseInt(it.required_qty || 0);
      var picked = parseInt(it.picked_qty || 0);
      var slotInfo = it.slot_code ? escapeHtml(it.slot_code) : '-';
      var diff = need - picked;
      var diffText = diff > 0 ? '<span class="text-[var(--mc-state-warning)]">\u7f3a ' + diff + '</span>' : '<span class="text-[var(--mc-state-success)]">\u5145\u8db3</span>';
      /* 显示选中目标以 Comment 为主，编码作辅助行；规格与封装并入名称列，避免列过多导致横向溢出 */
      var displayName = it.comment || it.material_name || it.material_code || '';
      var specFoot = [it.specification, it.footprint].filter(Boolean).join(' \u00b7 ');
      html += '<tr class="border-b border-border last:border-0 hover:bg-muted/50">'
        + '<td class="px-4 py-3"><input type="checkbox" class="row-check" data-check-item="' + it.id + '"></td>'
        + '<td class="px-4 py-3 text-sm font-medium">' + (idx + 1) + '</td>'
        + '<td class="px-4 py-3 text-sm" style="max-width:200px">' + escapeHtml(displayName)
        + (it.material_code ? '<div class="text-xs text-muted-foreground font-mono">' + escapeHtml(it.material_code) + '</div>' : '')
        + (specFoot ? '<div class="text-xs text-muted-foreground">' + escapeHtml(specFoot) + '</div>' : '') + '</td>'
        + '<td class="px-4 py-3 text-sm font-mono text-muted-foreground" title="' + escapeHtml(it.supplier_part || '') + '"><span class="inline-block max-w-[130px] overflow-hidden text-ellipsis align-bottom whitespace-nowrap">' + escapeHtml(it.supplier_part || '') + '</span></td>'
        + '<td class="px-4 py-3 text-sm font-semibold">' + need + '</td>'
        + '<td class="px-4 py-3 text-sm ' + matchCls + ' font-medium">' + matchText + '</td>'
        + '<td class="px-4 py-3 text-sm text-muted-foreground" title="' + escapeHtml(it.slot_code || '') + '"><span class="inline-block max-w-[120px] overflow-hidden text-ellipsis align-bottom whitespace-nowrap">' + slotInfo + '</span></td>'
        + '<td class="px-4 py-3 text-sm">' + diffText + '</td></tr>';
    });
    tbody.innerHTML = html;
    refreshIcons();
    if (pendingAutoSelect) { pendingAutoSelect = false; selectAllRows(); }
    syncCheckAll();
  }

  /* ════════════════════════════════════════
     analytics.html — 统计分析
     ════════════════════════════════════════ */
  function initAnalytics() {
    Promise.all([api.get_dashboard(), api.list_operations(20)]).then(function (res) {
      var dash = res[0], ops = res[1];
      if (!dash.ok) return;
      var d = dash.data;

      /* KPI（按 id 精确挂载） */
      var kpiMap = {
        'kpi-stock-in': d.total_stock_in || 0,
        'kpi-stock-out': d.total_stock_out || 0,
        'kpi-turnover': d.turnover_rate || '0%',
        'kpi-log-count': d.log_count || 0
      };
      Object.keys(kpiMap).forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.textContent = kpiMap[id];
      });

      /* 月度出入库趋势（动态渲染，无演示数据） */
      var trendBox = $('#monthly-trend-chart');
      if (trendBox) {
        var trend = d.monthly_trend || [];
        if (!trend.length) {
          trendBox.innerHTML = '<span class="text-sm text-muted-foreground">\u6682\u65e0\u51fa\u5165\u5e93\u8bb0\u5f55</span>';
        } else {
          var maxV = 10;
          trend.forEach(function (t) { maxV = Math.max(maxV, t.inbound || 0, t.outbound || 0); });
          var W = 460, H = 240, mL = 50, mR = 20, mT = 20, mB = 28;
          var plotW = W - mL - mR, plotH = H - mT - mB;
          var n = trend.length;
          function y(v) { return mT + plotH - (v / maxV) * plotH; }
          function x(i) { return mL + (n > 1 ? (plotW / (n - 1)) * i : plotW / 2); }
          var gridLines = '';
          for (var g = 0; g <= 4; g++) {
            var gy = mT + (plotH / 4) * g;
            gridLines += '<line x1="' + mL + '" y1="' + gy + '" x2="' + (W - mR) + '" y2="' + gy + '" stroke="var(--mc-border)" stroke-width="1"></line>';
          }
          var xLabels = '', ptsIn = [], ptsOut = [], dots = '';
          trend.forEach(function (t, i) {
            var xi = x(i);
            var lb = t.month ? String(t.month).slice(5) + '\u6708' : (i + 1) + '\u6708';
            xLabels += '<text x="' + xi.toFixed(1) + '" y="' + (H - 8) + '" text-anchor="middle" fill="var(--mc-muted-foreground)" style="font-size:11px;">' + lb + '</text>';
            ptsIn.push(xi.toFixed(1) + ',' + y(t.inbound || 0).toFixed(1));
            ptsOut.push(xi.toFixed(1) + ',' + y(t.outbound || 0).toFixed(1));
            dots += '<circle cx="' + xi.toFixed(1) + '" cy="' + y(t.inbound || 0).toFixed(1) + '" r="3.5" fill="var(--mc-primary)"><title>' + (t.month || '') + ' \u5165\u5e93 ' + (t.inbound || 0) + '</title></circle>';
            dots += '<circle cx="' + xi.toFixed(1) + '" cy="' + y(t.outbound || 0).toFixed(1) + '" r="3.5" fill="var(--mc-muted-foreground)"><title>' + (t.month || '') + ' \u51fa\u5e93 ' + (t.outbound || 0) + '</title></circle>';
          });
          trendBox.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" class="w-full" style="height:' + H + 'px;">'
            + gridLines
            + '<line x1="' + mL + '" y1="' + (H - mB) + '" x2="' + (W - mR) + '" y2="' + (H - mB) + '" stroke="var(--mc-border)" stroke-width="1"></line>'
            + '<polyline fill="none" stroke="var(--mc-primary)" stroke-width="2.5" points="' + ptsIn.join(' ') + '"></polyline>'
            + '<polyline fill="none" stroke="var(--mc-muted-foreground)" stroke-width="2.5" points="' + ptsOut.join(' ') + '"></polyline>'
            + dots + xLabels + '</svg>';
        }
      }

      /* 分类库存分布（按数量占比） */
      var catDist = $('#category-distribution');
      if (catDist) {
        var cats = d.category_stats || [];
        if (!cats.length) {
          catDist.innerHTML = '<p class="text-sm text-muted-foreground">\u6682\u65e0\u5206\u7c7b\u6570\u636e</p>';
        } else {
          var catHtml = '';
          cats.slice(0, 8).forEach(function (cat) {
            var pct = cat.percent || 0;
            catHtml += '<div class="flex-1 min-w-[120px]">'
              + '<div class="text-center mb-2">'
              + '<div class="text-2xl font-semibold text-foreground">' + pct + '%</div>'
              + '<div class="text-xs font-medium text-foreground">' + escapeHtml(cat.name) + '</div>'
              + '<div class="text-xs text-muted-foreground">' + (cat.qty || 0) + ' \u4ef6</div>'
              + '</div>'
              + '<div class="h-1.5 rounded-full bg-muted overflow-hidden">'
              + '<div class="h-full rounded-full bg-[var(--mc-blue)]" style="width:' + pct + '%"></div>'
              + '</div></div>';
          });
          catDist.innerHTML = catHtml;
        }
      }

      /* 低库存 TOP 10 表（6 列：物料/当前库存/安全库存/缺口/建议/操作） */
      var lowBody = $('#low-stock-tbody');
      if (lowBody) {
        var lowList = (d.low_stock_materials || []).slice(0, 10);
        if (!lowList.length) {
          lowBody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-sm text-muted-foreground">\u6682\u65e0\u4f4e\u5e93\u5b58\u7269\u6599</td></tr>';
        } else {
          var lowHtml = '';
          lowList.forEach(function (m) {
            var gap = Math.max((parseInt(m.min_stock || 0, 10) || 0) - (parseInt(m.total_qty || 0, 10) || 0), 0);
            lowHtml += '<tr class="hover:bg-muted/50">'
              + '<td class="px-4 py-3"><div class="text-sm font-medium">' + escapeHtml(m.material_name) + '</div>'
              + '<div class="text-xs text-muted-foreground">' + escapeHtml(m.specification || '') + '</div></td>'
              + '<td class="px-4 py-3 text-right text-sm font-semibold text-[var(--mc-state-warning)]">' + (m.total_qty || 0) + '</td>'
              + '<td class="px-4 py-3 text-right text-sm text-muted-foreground">' + (m.min_stock || 0) + '</td>'
              + '<td class="px-4 py-3 text-right text-sm">' + gap + '</td>'
              + '<td class="px-4 py-3 text-sm text-muted-foreground">' + (gap > 0 ? '\u8865\u8d27 ' + gap + ' ' + escapeHtml(m.unit || '\u4e2a') : '-') + '</td>'
              + '<td class="px-4 py-3 text-center"><a href="bom.html" class="text-[var(--mc-blue)] hover:underline text-sm">\u53bb\u8865\u8d27</a></td></tr>';
          });
          lowBody.innerHTML = lowHtml;
        }
      }

      /* 操作日志表（4 列：时间/操作类型/目标物料/结果） */
      var opsBody = $('#ops-log-tbody');
      if (opsBody && ops.ok) {
        var opsList = (ops.data && ops.data.operations) || [];
        if (!opsList.length) {
          opsBody.innerHTML = '<tr><td colspan="4" class="px-4 py-8 text-center text-sm text-muted-foreground">\u6682\u65e0\u64cd\u4f5c\u65e5\u5fd7</td></tr>';
        } else {
          var opsHtml = '';
          opsList.forEach(function (op) {
            var target = op.target_id ? (op.target_type ? op.target_type + ' #' + op.target_id : String(op.target_id)) : '-';
            opsHtml += '<tr class="hover:bg-muted/50">'
              + '<td class="px-4 py-3 text-sm font-mono">' + fmtTime(op.create_time) + '</td>'
              + '<td class="px-4 py-3 text-sm font-medium">' + escapeHtml(op.operation_type || '') + '</td>'
              + '<td class="px-4 py-3 text-sm text-muted-foreground">' + escapeHtml(target) + '</td>'
              + '<td class="px-4 py-3 text-sm text-muted-foreground">' + escapeHtml(op.detail || '') + '</td></tr>';
          });
          opsBody.innerHTML = opsHtml;
        }
      }

      refreshIcons();
    });

    /* 导出按钮 */
    var exportBtn = $('[data-dom-id="btn-export-logs"]');
    if (exportBtn) exportBtn.addEventListener('click', function () {
      api.export_inventories().then(function (res) {
        if (res.ok) showToast('\u5bfc\u51fa\u6210\u529f\uff1a' + (res.data && res.data.path || ''), 'success');
        else showToast('\u5bfc\u51fa\u5931\u8d25\uff1a' + (res.error || ''), 'error');
      });
    });
  }

  function initComponentLibrary() {
    var search = $('[data-dom-id="component-library-search"]');
    var tbody = $('#component-library-tbody');
    var count = $('#component-library-count');
    var checkAll = $('#component-library-check-all');
    var deleteSelected = $('#component-library-delete-selected');
    var records = [];
    function fmtParams(v) { return v && typeof v === 'object' ? JSON.stringify(v) : (v || ''); }
    function selectedIds() { return Array.prototype.map.call($$('.component-library-check:checked'), function (el) { return Number(el.value); }); }
    function updateSelection() {
      var ids = selectedIds();
      if (deleteSelected) deleteSelected.disabled = !ids.length;
      if (checkAll) checkAll.checked = !!records.length && ids.length === records.length;
    }
    function render() {
      if (!tbody) return;
      if (count) count.textContent = '共 ' + records.length + ' 条';
      tbody.innerHTML = records.length ? records.map(function (m) {
        return '<tr class="border-b border-border">'
          + '<td class="px-4 py-3"><input type="checkbox" class="component-library-check" value="' + Number(m.id) + '"></td>'
          + '<td class="px-4 py-3 font-mono">' + escapeHtml(m.model || '') + '</td><td class="px-4 py-3">' + escapeHtml(m.name || '') + '</td>'
          + '<td class="px-4 py-3">' + escapeHtml(m.supplier_part || '') + '</td><td class="px-4 py-3">' + escapeHtml(m.lcsc_code || '') + '</td>'
          + '<td class="px-4 py-3">' + escapeHtml(m.specification || '') + '</td><td class="px-4 py-3">' + escapeHtml(m.package || '') + '</td><td class="px-4 py-3">' + escapeHtml(m.category || '') + '</td><td class="px-4 py-3 text-right">' + (m.hit_count || 0) + '</td>'
          + '<td class="px-4 py-3 whitespace-nowrap"><button type="button" class="text-blue-600 mr-3" data-library-edit="' + Number(m.id) + '">编辑</button><button type="button" class="text-red-600" data-library-delete="' + Number(m.id) + '">删除</button></td></tr>';
      }).join('') : '<tr><td colspan="10" class="px-4 py-8 text-center text-muted-foreground">暂无历史库存记忆</td></tr>';
      updateSelection();
    }
    function load() { return api.list_component_library(search ? search.value.trim() : '').then(function (res) { if (!res.ok) { showToast(res.error || '加载失败', 'error'); return; } records = (res.data && res.data.records) || []; render(); }); }
    function close() { var dialog = $('#component-library-dialog'); if (dialog) { dialog.classList.add('hidden'); dialog.classList.remove('flex'); } }
    function edit(record) {
      var fields = ['model','name','supplier_part','lcsc_code','specification','package','category','brand','source'];
      var box = $('#component-library-fields');
      var form = $('#component-library-form');
      if (!box || !form) return;
      form.dataset.id = record.id;
      box.innerHTML = fields.map(function (key) { return '<label class="grid gap-1 text-sm"><span class="text-muted-foreground">' + key + '</span><input class="field" name="' + key + '" value="' + escapeHtml(record[key] || '') + '"></label>'; }).join('') + '<label class="grid gap-1 text-sm sm:col-span-2"><span class="text-muted-foreground">parameters</span><textarea class="field py-2" name="parameters">' + escapeHtml(fmtParams(record.parameters)) + '</textarea></label>';
      var dialog = $('#component-library-dialog'); dialog.classList.remove('hidden'); dialog.classList.add('flex');
    }
    load();
    if (search) { var timer; search.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(load, 250); }); }
    if (checkAll) checkAll.addEventListener('change', function () { $$('.component-library-check').forEach(function (el) { el.checked = checkAll.checked; }); updateSelection(); });
    if (tbody) tbody.addEventListener('change', updateSelection);
    if (tbody) tbody.addEventListener('click', function (event) { var editId = event.target.getAttribute('data-library-edit'); var deleteId = event.target.getAttribute('data-library-delete'); var record = records.filter(function (item) { return Number(item.id) === Number(editId || deleteId); })[0]; if (editId && record) edit(record); if (deleteId) { if (!window.confirm('确认删除这条历史库存记录吗？')) return; api.delete_component_library(Number(deleteId)).then(function (res) { if (res.ok) load(); else showToast(res.error || '删除失败', 'error'); }); } });
    if (deleteSelected) deleteSelected.addEventListener('click', function () {
      var ids = selectedIds();
      if (!ids.length || !window.confirm('确认删除已选的 ' + ids.length + ' 条记录吗？')) return;
      var failedCount = 0;
      Promise.all(ids.map(function (id) {
        return api.delete_component_library(id).then(function (res) { if (!res.ok) failedCount++; });
      })).then(function () {
        if (failedCount) showToast(failedCount + ' 条记录删除失败', 'error');
        updateSelection();
        load();
      });
    });
    var dlgClose = $('#component-library-dialog-close');
    if (dlgClose) dlgClose.addEventListener('click', close);
    var dlgCancel = $('#component-library-dialog-cancel');
    if (dlgCancel) dlgCancel.addEventListener('click', close);
    var dlgForm = $('#component-library-form');
    if (dlgForm) dlgForm.addEventListener('submit', function (event) { event.preventDefault(); var form = event.currentTarget; var data = {}; Array.prototype.forEach.call(form.elements, function (field) { if (field.name) data[field.name] = field.value.trim(); }); try { data.parameters = data.parameters ? JSON.parse(data.parameters) : {}; } catch (e) {} api.update_component_library(Number(form.dataset.id), data).then(function (res) { if (!res.ok) { showToast(res.error || '保存失败', 'error'); return; } close(); load(); showToast('历史库存已更新', 'success'); }); });
  }

  /* ════════════════════════════════════════
     admin-settings.html — 系统设置
     ════════════════════════════════════════ */
  function initAdmin() {
    api.get_settings().then(function (res) {
      if (!res.ok) return;
      var settings = (res.data && res.data.settings) || {};

      /* 填充表单字段 */
      $$('[data-config-key]').forEach(function (field) {
        var key = field.getAttribute('data-config-key');
        var val = settings[key];
        if (val === undefined || val === null) return;
        if (field.type === 'checkbox') {
          field.checked = (val === true || val === 'true' || val === 1);
        } else {
          field.value = val;
        }
      });
    });
    api.get_github_settings().then(function (res) {
      var settings = res && res.data && res.data.settings || {};
      $('#github-owner').value = settings.owner || 'LCZ-195';
      $('#github-repo').value = settings.repo || 'material-cabinet';
      $('#github-auto-update').checked = settings.auto_update !== false;
      $('#github-auto-inventory').checked = settings.auto_inventory !== false;
      var tokenState = $('#github-token-state');
      if (tokenState) tokenState.textContent = settings.token_configured ? 'Token 已配置（DPAPI 本机加密）' : 'Token 未配置';
    });
    var githubCheckUpdate = $('#github-check-update');
    if (githubCheckUpdate && !githubCheckUpdate.dataset.bridgeBound) {
      githubCheckUpdate.dataset.bridgeBound = '1';
      githubCheckUpdate.addEventListener('click', function () {
        var started = Date.now();
        setActionBusy([githubCheckUpdate], true, '检查中…');
        showToast('正在检查版本并读取标记文件…', 'info');
        api.check_github_version().then(function (res) {
          var data = res && res.data || {};
          if (!res || !res.ok) { showToast((res && res.error) || '版本检查失败（' + actionElapsed(started) + '）', 'error'); return; }
          if (!data.available) { showToast((data.message || '当前已是最新版本') + '（已用' + actionElapsed(started) + '）', 'success'); return; }
          setActionBusy([githubCheckUpdate], true, '更新中…');
          showToast('发现新版本 v' + data.version + '，正在下载并校验…', 'warning');
          return api.update_github_version().then(function (u) {
            if (u && u.ok) showToast('更新校验完成，程序即将重启', 'success');
            else showToast((u && u.error) || '自动更新失败', 'error');
          });
        }).catch(function (err) { showToast('版本检查失败：' + err + '（' + actionElapsed(started) + '）', 'error'); }).finally(function () { setActionBusy([githubCheckUpdate], false); });
      });
    }
    var githubSave = $('#github-save');
    if (githubSave && !githubSave.dataset.bridgeBound) {
      githubSave.dataset.bridgeBound = '1';
      githubSave.addEventListener('click', function () {
        var data = { owner: $('#github-owner').value, repo: $('#github-repo').value,
          token: $('#github-token').value, auto_update: $('#github-auto-update').checked,
          auto_inventory: $('#github-auto-inventory').checked };
        api.save_github_settings(data).then(function (res) {
          if (res.ok) { $('#github-token').value = ''; showToast('GitHub 配置已保存', 'success'); }
          else showToast('GitHub 配置保存失败：' + (res.error || ''), 'error');
        });
      });
    }

    /* 保存设置：克隆按钮以移除页面原有 demo 监听 */
    var saveBtn = $('[data-dom-id="admin-save-settings"]');
    if (saveBtn) {
      var newSave = saveBtn.cloneNode(true);
      saveBtn.parentNode.replaceChild(newSave, saveBtn);
      newSave.addEventListener('click', function () {
        var data = {};
        $$('[data-config-key]').forEach(function (field) {
          var key = field.getAttribute('data-config-key');
          if (field.type === 'checkbox') data[key] = field.checked;
          else data[key] = field.value;
        });
        api.save_settings(data).then(function (res) {
          var status = $('#save-status');
          if (res.ok) {
            if (status) status.textContent = '\u8bbe\u7f6e\u5df2\u4fdd\u5b58\u3002';
            showToast('\u8bbe\u7f6e\u4fdd\u5b58\u6210\u529f', 'success');
          } else {
            if (status) status.textContent = '\u4fdd\u5b58\u5931\u8d25\uff1a' + (res.error || '');
            showToast('\u4fdd\u5b58\u5931\u8d25\uff1a' + (res.error || ''), 'error');
          }
        });
      });
    }

    /* 测试连接 */
    var testBtn = $('[data-dom-id="admin-test-connection"]');
    if (testBtn) {
      var newTest = testBtn.cloneNode(true);
      testBtn.parentNode.replaceChild(newTest, testBtn);
      newTest.addEventListener('click', function () {
        var status = $('#save-status');
        if (status) status.textContent = '\u6d4b\u8bd5\u8fde\u63a5\u4e2d\u2026';
        api.ping().then(function (res) {
          if (status) status.textContent = '\u670d\u52a1\u8fd0\u884c\u6b63\u5e38\uff08\u79bb\u7ebf\u6a21\u5f0f\uff09\u3002';
        });
      });
    }

    /* 清除演示数据 */
    var clearDemoBtn = $('[data-dom-id="admin-clear-demo"]');
    if (clearDemoBtn) {
      var newClear = clearDemoBtn.cloneNode(true);
      clearDemoBtn.parentNode.replaceChild(newClear, clearDemoBtn);
      newClear.addEventListener('click', function () {
        var started = Date.now();
        setActionBusy([newClear, newClearAll], true, '\u6e05\u7a7a\u4e2d\u2026');
        showToast('\u6b63\u5728\u6e05\u7a7a\u672c\u5730\u4e1a\u52a1\u6570\u636e\u2026', 'info');
        api.clear_demo().then(function (res) {
          if (res.ok) {
            var data = res.data || {};
            if (data.cloud_sync_failed) showToast(data.message || '\u672c\u5730\u5df2\u6e05\u7a7a\uff0c\u4f46\u7a7a\u5e93\u5b58\u4e0a\u4f20\u5931\u8d25', 'warning');
            else showToast(data.message || '\u6f14\u793a\u6570\u636e\u5df2\u6e05\u9664\u5e76\u540c\u6b65\u7a7a\u5e93\u5b58', 'success');
            setTimeout(function () { window.location.reload(); }, 1000);
          } else showToast('\u6e05\u9664\u5931\u8d25\uff1a' + (res.error || '') + '\uff08' + actionElapsed(started) + '\uff09', 'error');
        }).catch(function (err) {
          showToast('\u6e05\u9664\u5931\u8d25\uff1a' + err + '\uff08' + actionElapsed(started) + '\uff09', 'error');
        }).finally(function () { setActionBusy([newClear, newClearAll], false); });
      });
    }

    /* 完全清空 */
    var clearAllBtn = $('[data-dom-id="admin-clear-all"]');
    if (clearAllBtn) {
      var newClearAll = clearAllBtn.cloneNode(true);
      clearAllBtn.parentNode.replaceChild(newClearAll, clearAllBtn);
      newClearAll.addEventListener('click', function () {
        var started = Date.now();
        setActionBusy([newClear, newClearAll], true, '\u6e05\u7a7a\u4e2d\u2026');
        showToast('\u6b63\u5728\u6e05\u7a7a\u672c\u5730\u4e1a\u52a1\u6570\u636e\u2026', 'info');
        api.factory_reset().then(function (res) {
          if (res.ok) {
            var data = res.data || {};
            if (data.cloud_sync_failed) showToast(data.message || '\u672c\u5730\u5df2\u6e05\u7a7a\uff0c\u4f46\u7a7a\u5e93\u5b58\u4e0a\u4f20\u5931\u8d25', 'warning');
            else showToast(data.message || '\u6570\u636e\u5df2\u6e05\u7a7a\u5e76\u540c\u6b65\u7a7a\u5e93\u5b58', 'success');
            setTimeout(function () { window.location.reload(); }, 1000);
          } else showToast('\u6e05\u7a7a\u5931\u8d25\uff1a' + (res.error || '') + '\uff08' + actionElapsed(started) + '\uff09', 'error');
        }).catch(function (err) {
          showToast('\u6e05\u7a7a\u5931\u8d25\uff1a' + err + '\uff08' + actionElapsed(started) + '\uff09', 'error');
        }).finally(function () { setActionBusy([newClear, newClearAll], false); });
      });
    }
  }

  function runVersionCheck(autoUpdate) {
    var buttons = $$('[data-dom-id="btn-check-version"],[data-dom-id="footer-check-version"]');
    var started = Date.now();
    setActionBusy(buttons, true, '检查中…');
    showToast('正在读取云端版本标记…', 'info');
    api.check_github_version().then(function (res) {
      var data = res && res.data || {};
      if (!res || !res.ok) showToast((res && res.error) || '版本检查失败（' + actionElapsed(started) + '）', 'error');
      else if (data.available) {
        showToast('发现新版本 v' + data.version + '（已用' + actionElapsed(started) + '）', 'warning');
        if (autoUpdate) {
          setActionBusy(buttons, true, '更新中…');
          return api.update_github_version().then(function (updateRes) {
            if (updateRes && updateRes.ok) showToast('更新校验完成，程序即将重启', 'success');
            else showToast((updateRes && updateRes.error) || '自动更新失败', 'error');
          });
        }
        if (getCurrentPage() === 'admin-settings') window.scrollTo(0, 0);
      } else showToast((data.message || '当前已是最新版本') + '（已用' + actionElapsed(started) + '）', 'success');
    }).catch(function (err) { showToast('版本检查失败：' + err + '（' + actionElapsed(started) + '）', 'error'); }).finally(function () { setActionBusy(buttons, false); });
  }
  function runInventoryCheck() {
    var buttons = $$('[data-dom-id="btn-check-inventory"],[data-dom-id="footer-check-inventory"]');
    var started = Date.now();
    setActionBusy(buttons, true, '同步中…');
    showToast('正在下载并合并云端库存，然后上传本地快照…', 'info');
    api.sync_github_inventory().then(function (res) {
      if (res && res.ok) { showToast((res.data && res.data.message) || '库存同步完成（' + actionElapsed(started) + '）', 'success'); setTimeout(function () { window.location.reload(); }, 700); }
      else showToast((res && (res.error || res.data && res.data.message)) || '库存同步失败（' + actionElapsed(started) + '）', 'error');
    }).catch(function (err) { showToast('库存同步失败：' + err + '（' + actionElapsed(started) + '）', 'error'); }).finally(function () { setActionBusy(buttons, false); });
  }
  function bindSyncActions() {
    $$('[data-dom-id="btn-check-version"],[data-dom-id="footer-check-version"]').forEach(function (el) {
      if (!el.dataset.bridgeBound) { el.dataset.bridgeBound = '1'; el.addEventListener('click', function () { runVersionCheck(false); }); }
    });
    $$('[data-dom-id="btn-check-inventory"],[data-dom-id="footer-check-inventory"]').forEach(function (el) {
      if (!el.dataset.bridgeBound) { el.dataset.bridgeBound = '1'; el.addEventListener('click', runInventoryCheck); }
    });
  }
  function autoSyncOnce() {
    if (sessionStorage.getItem('github_auto_checked')) return;
    sessionStorage.setItem('github_auto_checked', '1');
    api.get_github_settings().then(function (res) {
      var settings = res && res.data && res.data.settings || {};
      if (settings.auto_inventory) runInventoryCheck();
      if (settings.auto_update) runVersionCheck(false);
    });
  }

  /* ── 全局导航绑定 ── */
  function bindGlobalNav() {
    bindGlobalSearch();
    bindSyncActions();
    /* 导航项激活状态 */
    var page = getCurrentPage();
    $$('.nav-item').forEach(function (item) {
      var href = item.getAttribute('href') || '';
      if (href.indexOf(page + '.html') !== -1) {
        item.classList.add('active');
      } else {
        item.classList.remove('active');
      }
    });

    var addBtn = $('[data-dom-id="btn-add-material"]');
    if (addBtn && !addBtn.dataset.bridgeBound) {
      addBtn.dataset.bridgeBound = '1';
      addBtn.addEventListener('click', function () { setPendingAction('add-material'); navigate('materials.html'); });
    }
    var importBtn = $('[data-dom-id="btn-import-bom"]');
    if (importBtn && !importBtn.dataset.bridgeBound) {
      importBtn.dataset.bridgeBound = '1';
      importBtn.addEventListener('click', function () { setPendingAction('import-bom'); navigate('bom.html'); });
    }

    /* 全局阻止默认拖放（防止拖入文件被当作文档打开） */
    if (!window.__dragGuardBound) {
      window.__dragGuardBound = true;
      ['dragover', 'drop'].forEach(function (evt) {
        window.addEventListener(evt, function (e) { e.preventDefault(); }, false);
      });
    }
  }

  /* ── 启动 ── */
  function boot() {
    /* pywebview 先注入骨架对象、异步填充方法，须确认方法已挂载（get_dashboard 为
       Bridge 固有公有方法，作为就绪探针），否则偶发 TypeError: xxx is not a function */
    var apiObj = (window.pywebview && window.pywebview.api) || null;
    if (!apiObj || typeof apiObj.get_dashboard !== 'function') {
      setTimeout(boot, 200);
      return;
    }
    api = apiObj;

    var page = getCurrentPage();
    switch (page) {
      case 'index': initIndex(); break;
      case 'cabinet': initCabinet(); break;
      case 'materials': initMaterials(); break;
      case 'bom': initBom(); break;
      case 'analytics': initAnalytics(); break;
      case 'admin-settings': initAdmin(); break;
      case 'component-library': initComponentLibrary(); break;
    }
    bindGlobalNav();
    updateVersionLabels();
    autoSyncOnce();
    updateFooterTime();
    setInterval(updateFooterTime, 30000);
  }

  boot();
})();
"""


# ══════════════════════════════════════════════════════════
#  构建注入脚本
# ══════════════════════════════════════════════════════════
def build_inject_script():
    """构建注入脚本：通过 DOM API 注入 CSS + 直接执行 BRIDGE_JS"""
    css_json = json.dumps(RESET_CSS)
    # 用 DOM API 创建 style 元素注入 CSS（evaluate_js 只接受纯 JS）
    inject_css = (
        'var _s=document.createElement("style");'
        '_s.id="pywebview-reset-css";'
        '_s.textContent=' + css_json + ';'
        'document.head.appendChild(_s);'
    )
    # BRIDGE_JS 已是 IIFE，直接拼接执行
    return inject_css + BRIDGE_JS


# ══════════════════════════════════════════════════════════
#  DPI 缩放与窗口尺寸
# ══════════════════════════════════════════════════════════
def _system_dpi_scale():
    try:
        import ctypes
        hdc = ctypes.windll.user32.GetDC(0)
        LOGPIXELSX = 88
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def compute_window_size():
    scale = _system_dpi_scale()
    import ctypes
    user32 = ctypes.windll.user32
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    avail_w = int(sw / scale)
    avail_h = int(sh / scale)

    ratio = min(avail_w / DESIGN_WIDTH, avail_h / DESIGN_HEIGHT, 1.0)
    if ratio < 0.6:
        ratio = 0.6
    w = int(DESIGN_WIDTH * ratio)
    h = int(DESIGN_HEIGHT * ratio)

    if w > avail_w:
        w = avail_w
    if h > avail_h:
        h = avail_h

    return w, h


# ══════════════════════════════════════════════════════════
#  主函数
# ══════════════════════════════════════════════════════════
def cleanup_stale_instances():
    if os.name != 'nt':
        return
    current_pid = os.getpid()
    project_entry = os.path.normcase(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ['wmic', 'process', 'where', "name='python.exe' or name='pythonw.exe'", 'get', 'ProcessId,CommandLine', '/format:csv'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode != 0:
        try:
            result = subprocess.run(
                [
                    'powershell', '-NoProfile', '-Command',
                    'Get-CimInstance Win32_Process | Where-Object { $_.Name -in @("python.exe", "pythonw.exe") } | ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }',
                ],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return
    for line in result.stdout.splitlines():
        lower = line.lower()
        if project_entry not in os.path.normcase(lower):
            continue
        if '\t' in line:
            parts = line.split('\t', 1)
            pid_text = parts[0].strip()
        else:
            parts = line.rsplit(',', 1)
            pid_text = parts[1].strip() if len(parts) == 2 else ''
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        try:
            subprocess.run(
                ['taskkill', '/PID', str(pid), '/T', '/F'],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        time.sleep(0.15)


def acquire_instance_lock():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        lock.bind((INSTANCE_SOCKET, INSTANCE_PORT))
        lock.listen(1)
        return lock
    except OSError:
        lock.close()
        return None


def main():
    cleanup_stale_instances()
    instance_lock = acquire_instance_lock()
    if instance_lock is None:
        return
    # 启动本地 HTTP 服务器
    server = start_ui_server(0)
    port = server.server_address[1]
    url = 'http://127.0.0.1:{}/pages/index.html'.format(port)

    # 创建 Bridge 实例
    bridge = Bridge()

    # 计算窗口尺寸
    win_w, win_h = compute_window_size()

    # 创建窗口
    window = webview.create_window(
        title='{} v{}'.format(APP_NAME, APP_VERSION),
        url=url,
        width=win_w,
        height=win_h,
        min_size=(960, 600),
        resizable=True,
        text_select=False,
        js_api=bridge,
    )

    # 设置 Bridge 的窗口引用
    bridge._window = window

    # 页面加载完成后注入脚本
    def on_loaded():
        window.evaluate_js(build_inject_script())

    window.events.loaded += on_loaded

    # 启动 pywebview
    webview.start()


if __name__ == '__main__':
    main()
