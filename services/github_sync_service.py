# -*- coding: utf-8 -*-
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from config import BASE_DIR, DB_PATH
from models.database import AppSettings, get_cursor

DEFAULT_OWNER = "LCZ-195"
DEFAULT_REPO = "material-cabinet"
SNAPSHOT_NAME = "inventory_sync.json"
VERSION_MARKER_NAME = "VERSION.txt"
INVENTORY_MARKER_NAME = "INVENTORY_VERSION.txt"
USER_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or BASE_DIR, "物料收纳柜")
# 单实例健康端口：必须与 main.py 的 INSTANCE_PORT 保持一致。
# 更新器以“新版是否监听此端口”判定 bootloader 是否真正通过。
INSTANCE_HEALTH_PORT = 47831


def _clone_request(request):
    """克隆 urllib Request：失败后重发需要全新对象，避免内部状态污染。"""
    return urllib.request.Request(
        request.full_url, data=request.data,
        headers={name: value for name, value in request.header_items()},
        method=request.get_method())


_direct_healthy_until = [0.0]


def _norm_cell(value):
    """快照单元格归一化：dict/list 序列化为 JSON 文本，其余标量原样透传。
    防御畸形快照把容器类型直接绑定给 sqlite（InterfaceError）。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _http_error_transient(exc):
    """判定 HTTPError 是否属于"可换通道重试"的瞬时故障：
    429 限流、5xx 服务端故障、407 代理认证失败都可以换另一条通道再试；
    401/403/404/422 等是明确的服务端业务判定，换通道结果不会变，直接抛出。"""
    code = exc.code
    return code == 429 or code == 407 or 500 <= code <= 599


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跨主机重定向时剥离 Authorization 头。
    Python 的 redirect_request 会把原请求 headers 复制给重定向请求：
    Token一旦跟随 302 跳到第三方域（CDN/统计域名）就会泄露。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).netloc
            new_host = urllib.parse.urlsplit(newurl).netloc
            if old_host != new_host:
                for name in [h for h in new.headers if h.lower() == "authorization"]:
                    del new.headers[name]
        return new


# 代理/直连两条固定 opener：均安装 _SafeRedirectHandler；
# 直连 opener 额外用空 ProxyHandler 覆盖系统代理设置
_OPENER_PROXY = urllib.request.build_opener(_SafeRedirectHandler())
_OPENER_DIRECT = urllib.request.build_opener(_SafeRedirectHandler(), urllib.request.ProxyHandler({}))


def _urlopen_with_retry(request, timeout):
    """带故障转移的 urlopen：奇数次走系统代理，偶数次强制直连。
    大陆网络环境的典型故障模式是代理节点抖动而部分 GitHub 域名直连可达
    （或相反），交替尝试可显著提高成功率。实测（2026-09）某代理节点完全
    不可用时 api.github.com 直连仍能返回 200。
    直连成功后进入 5 分钟健康期：期间优先直连，避免每次都先等代理超时；
    直连通道自身失败（SSL 超时/连接拒绝等）则立即清除健康缓存。
    HTTPError 默认视为明确的服务端判定直接抛出；仅 429/5xx/407 这类
    瞬时故障换另一条通道重试。"""
    last_exc = None
    prefer_direct = time.time() < _direct_healthy_until[0]
    order = [2, 1] if prefer_direct else [1, 2]
    for step, channel in enumerate(order):
        opener = _OPENER_DIRECT if channel == 2 else _OPENER_PROXY
        try:
            response = opener.open(_clone_request(request), timeout=timeout)
            if channel == 2:
                _direct_healthy_until[0] = time.time() + 300
            return response
        except urllib.error.HTTPError as exc:
            if channel == 2:
                # HTTPError 说明 TLS 握手已完成、服务器有响应——直连通道本身健康
                _direct_healthy_until[0] = time.time() + 300
            if not _http_error_transient(exc):
                raise
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 瞬时网络故障 → 换通道重试
            if channel == 2:
                # 直连也失败：健康缓存已不可信，立即清空让下次回到代理优先
                _direct_healthy_until[0] = 0.0
            last_exc = exc
        if step < len(order) - 1:
            time.sleep(1)
    raise last_exc


TOKEN_FILE = os.path.join(USER_DATA_DIR, "github_token.bin")


class GitHubSyncService:
    def __init__(self, app_name="物料收纳柜", app_version="0.0.0"):
        self.app_name = app_name
        self.app_version = app_version
        self._sync_lock = threading.Lock()
        self._update_lock = threading.Lock()

    @staticmethod
    def _settings():
        settings = AppSettings.all()
        return {
            "owner": str(settings.get("github_owner") or DEFAULT_OWNER).strip(),
            "repo": str(settings.get("github_repo") or DEFAULT_REPO).strip(),
            "auto_update": _as_bool(settings.get("github_auto_update", True)),
            "auto_inventory": _as_bool(settings.get("github_auto_inventory", True)),
            "token_configured": bool(GitHubSyncService._token()),
        }

    @staticmethod
    def _token():
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            return token
        try:
            with open(TOKEN_FILE, "rb") as f:
                raw = f.read()
            if os.name == "nt" and raw:
                return _dpapi_unprotect(raw).decode("utf-8")
            return raw.decode("utf-8")
        except (OSError, ValueError):
            return ""

    @staticmethod
    def save_configuration(owner, repo, token, auto_update=True, auto_inventory=True, clear_token=False):
        owner = str(owner or DEFAULT_OWNER).strip()
        repo = str(repo or DEFAULT_REPO).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            return {"ok": False, "error": "GitHub 用户名和仓库名格式无效"}
        AppSettings.update_many({
            "github_owner": owner,
            "github_repo": repo,
            "github_auto_update": _as_bool(auto_update),
            "github_auto_inventory": _as_bool(auto_inventory),
        })
        if token:
            raw = str(token).strip().encode("utf-8")
            payload = _dpapi_protect(raw) if os.name == "nt" else raw
            os.makedirs(USER_DATA_DIR, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix="github-token-", dir=USER_DATA_DIR)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                os.replace(temp_path, TOKEN_FILE)
                if os.name == "nt":
                    try:
                        os.chmod(TOKEN_FILE, 0o600)
                    except OSError:
                        pass
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        elif clear_token:
            # 显式清除（clear_token=True 才删）：token 输入框留空是常态
            # （前端保存后即清空输入框），空值绝不能误删已保存的 Token
            try:
                os.remove(TOKEN_FILE)
            except OSError:
                pass
        return {"ok": True, "data": {"settings": GitHubSyncService._settings()}}

    @staticmethod
    def _api_url(path):
        encoded = "/".join(urllib.parse.quote(p, safe="") for p in path.strip("/").split("/"))
        return "https://api.github.com/" + encoded

    def _request(self, url, method="GET", data=None, auth=True, timeout=12):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "material-cabinet"}
        token = self._token() if auth else ""
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with _urlopen_with_retry(request, timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 401:
                return exc.code, {"message": f"GitHub Token 无效或已过期（401），请在设置中重新配置。{detail}"}
            if exc.code == 403:
                return exc.code, {"message": f"GitHub 拒绝访问（403）：Token 权限不足或触发接口限流。{detail}"}
            return exc.code, {"message": detail}
        except (OSError, ValueError) as exc:
            return 0, {"message": str(exc)}

    def _read_marker(self, name):
        cfg = self._settings()
        # 优先走 GitHub Raw（单次轻量 GET，公开仓库无需鉴权、比 contents API 快得多），
        # 失败时回退 contents API（兼容私有仓库 + Token 场景）
        raw_url = "https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file}".format(
            owner=cfg["owner"], repo=cfg["repo"], branch="main", file=name)
        try:
            request = urllib.request.Request(raw_url, headers={"User-Agent": "material-cabinet"})
            with _urlopen_with_retry(request, 10) as response:
                raw = response.read().decode("utf-8", errors="replace").lstrip("\ufeff")
            value = raw.strip().splitlines()[0] if raw.strip() else ""
            return {"ok": True, "found": bool(value), "value": value}
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not self._token():
                # 无 Token 时 Raw 404 是权威判定（公开仓库文件不存在）；
                # 有 Token 时私有仓库的 Raw 未鉴权请求同样返回 404（隐藏
                # 存在性），无信息量，必须继续走 contents API 用 Token 确认
                return {"ok": True, "found": False, "value": ""}
        except Exception:  # noqa: BLE001 网络错误 → 走 API 回退
            pass
        status, payload = self._request(self._api_url(f"repos/{cfg['owner']}/{cfg['repo']}/contents/{name}"), auth=True)
        if status == 404:
            return {"ok": True, "found": False, "value": ""}
        if status != 200:
            return {"ok": False, "error": f"读取{name}失败：{payload.get('message', status)}"}
        try:
            raw = base64.b64decode(str(payload.get("content") or "").replace("\n", ""))
            value = raw.decode("utf-8").strip().splitlines()[0] if raw else ""
        except (ValueError, UnicodeError) as exc:
            return {"ok": False, "error": f"{name}格式无效：{exc}"}
        return {"ok": True, "found": bool(value), "value": value, "sha": payload.get("sha")}

    def check_version(self):
        # 标记文件读取失败（网络抖动/限流）不阻断检查：降级走 Release API
        marker = self._read_marker(VERSION_MARKER_NAME)
        tag = (marker.get("value") or "") if marker.get("ok") else ""
        if tag:
            available = _version_tuple(tag.lstrip("vV")) > _version_tuple(self.app_version)
            if not available:
                return {"ok": True, "data": {"available": False, "version": tag.lstrip("vV"), "message": "当前已是最新版本（标记文件）"}}
        status, payload = self._request(self._api_url(f"repos/{self._settings()['owner']}/{self._settings()['repo']}/releases/latest"), auth=True)
        if status == 404:
            return {"ok": True, "data": {"available": False, "message": "无法读取最新发布：请检查仓库名是否正确、仓库是否为公开可见"}}
        if status != 200:
            return {"ok": False, "error": f"版本检查失败：{payload.get('message', status)}"}
        tag = str(payload.get("tag_name") or "").lstrip("vV")
        assets = payload.get("assets", [])
        asset = next((x for x in assets if str(x.get("name", "")).lower().endswith(".exe")), None)
        if not asset:
            return {"ok": True, "data": {"available": False, "version": tag, "message": "Release 未包含 EXE 文件"}}
        checksum_asset = next((x for x in assets if str(x.get("name", "")).lower() in ("sha256.txt", "checksums.txt", "checksums.sha256")), None)
        available = _version_tuple(tag) > _version_tuple(self.app_version)
        # 优先使用 release 资产的 CDN 直链（browser_download_url），下载更快更稳；无直链时退回 API 资产接口
        download_url = (asset.get("browser_download_url") or
                        self._api_url(f"repos/{self._settings()['owner']}/{self._settings()['repo']}/releases/assets/{asset.get('id')}"))
        checksum_url = (checksum_asset.get("browser_download_url") or "") if checksum_asset else ""
        return {"ok": True, "data": {"available": available, "version": tag, "name": asset.get("name"),
                                     "download_url": download_url, "checksum_url": checksum_url,
                                     "release_url": payload.get("html_url")}}

    def schedule_update(self):
        # 互斥锁：前端连点"检查更新"会并发进入，两个线程写同一个 .download
        # 临时文件会互相破坏下载内容，导致 SHA-256 校验失败或替换损坏
        if not self._update_lock.acquire(blocking=False):
            return {"ok": False, "error": "已有更新任务在进行中，请稍候"}
        try:
            return self._schedule_update_locked()
        finally:
            self._update_lock.release()

    def _schedule_update_locked(self):
        result = self.check_version()
        if not result.get("ok") or not result.get("data", {}).get("available"):
            return result
        data = result["data"]
        current = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else os.path.join(BASE_DIR, "物料收纳柜.exe"))
        if not os.path.isfile(current):
            return {"ok": False, "error": "找不到当前 EXE，无法执行替换更新"}
        temp_path = current + ".download"
        try:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)  # 清理上次失败残留
                except OSError:
                    pass
            _download(data["download_url"], temp_path)
            if os.path.getsize(temp_path) < 1024 * 1024:
                raise ValueError("下载文件大小异常")
            expected = _download_checksum(data.get("checksum_url"), data["name"])
            if not expected:
                raise ValueError("Release 缺少 EXE SHA-256 校验文件，已拒绝更新")
            actual = _sha256_file(temp_path)
            if actual.lower() != expected.lower():
                raise ValueError("更新文件 SHA-256 校验失败")
            _schedule_replace(current, temp_path, os.getpid())
            return {"ok": True, "data": {"available": True, "version": data["version"], "restart_required": True}}
        except (OSError, ValueError, urllib.error.URLError) as exc:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            return {"ok": False, "error": f"更新下载失败：{exc}"}

    @staticmethod
    def _snapshot():
        with get_cursor() as cur:
            cur.execute("SELECT material_code,name,category,specification,package,supplier_code,lcsc_code,brand,unit,min_stock,description,datasheet_url,parameters,create_time,update_time FROM materials")
            materials = [dict(row) for row in cur.fetchall()]
            cur.execute("""SELECT s.slot_code,i.material_id,i.quantity,i.batch_no,i.inbound_date,i.note,i.create_time,i.update_time,m.material_code,m.supplier_code
                         FROM inventories i JOIN slots s ON s.id=i.slot_id LEFT JOIN materials m ON m.id=i.material_id""")
            inventories = [dict(row) for row in cur.fetchall()]
            # 历史库存记忆库（component_library）随库存一起同步
            cur.execute("""SELECT lcsc_code,supplier_part,model,name,specification,package,footprint,brand,category,parameters,datasheet,hit_count,source,create_time,update_time
                         FROM component_library""")
            components = [dict(row) for row in cur.fetchall()]
        for item in materials:
            item["parameters"] = _json_value(item.get("parameters"))
        for item in components:
            item["parameters"] = _json_value(item.get("parameters"))
        snapshot = {"schema": 2, "updated_at": datetime.now(timezone.utc).isoformat(),
                    "materials": materials, "inventories": inventories, "components": components}
        snapshot["inventory_version"] = _snapshot_version(snapshot)
        return snapshot

    def export_local_snapshot(self):
        return {"ok": True, "data": self._snapshot()}

    @staticmethod
    def _material_key(item):
        return str(item.get("material_code") or item.get("supplier_code") or item.get("lcsc_code") or item.get("name") or "").strip().lower()

    def merge_snapshot(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("schema") not in (1, 2):
            return {"ok": False, "error": "库存快照格式不受支持"}
        schema = int(snapshot.get("schema") or 1)
        materials = snapshot.get("materials")
        inventories = snapshot.get("inventories")
        components = snapshot.get("components")
        if components is None:
            components = []  # schema 1 旧快照无记忆库字段，视为空集合
        if not isinstance(materials, list) or not isinstance(inventories, list) or not isinstance(components, list) \
                or len(materials) > 100000 or len(inventories) > 100000 or len(components) > 100000:
            return {"ok": False, "error": "库存快照数据无效"}
        material_ids = {}
        changed = 0
        with get_cursor() as cur:
            cur.execute("SELECT id,material_code,supplier_code,lcsc_code,name,update_time FROM materials")
            existing = {self._material_key(dict(row)): dict(row) for row in cur.fetchall()}
            fields = ["material_code","name","category","specification","package","supplier_code","lcsc_code","brand","unit","min_stock","description","datasheet_url","parameters"]
            for item in materials:
                key = self._material_key(item)
                if not key or not item.get("name"):
                    continue
                values = [_norm_cell(item.get(f)) for f in fields]
                for index, field in enumerate(fields):
                    if values[index] is None:
                        values[index] = "" if field != "min_stock" else 0
                try:
                    values[fields.index("min_stock")] = max(0, int(values[fields.index("min_stock")]))
                except (TypeError, ValueError):
                    values[fields.index("min_stock")] = 0
                old = existing.get(key)
                incoming_time = _ts_key(item.get("update_time"))
                # 空 update_time 视为最旧：绝不覆盖本地已有记录（否则畸形快照会
                # 以 now() 盖掉本地较新的数据，破坏"新者胜"合并原则）
                if old and (not incoming_time or _ts_key(old.get("update_time")) >= incoming_time):
                    material_ids[key] = old["id"]
                    continue
                if old:
                    update_time = incoming_time or datetime.now().isoformat()
                    cur.execute("UPDATE materials SET " + ", ".join(f"{f}=?" for f in fields) + ", update_time=? WHERE id=?", values + [update_time, old["id"]])
                    material_ids[key] = old["id"]
                    existing[key] = {"id": old["id"], "update_time": update_time}
                else:
                    update_time = incoming_time or datetime.now().isoformat()
                    cur.execute("INSERT INTO materials (" + ",".join(fields) + ",update_time) VALUES (" + ",".join("?" for _ in fields) + ",?)", values + [update_time])
                    material_ids[key] = cur.lastrowid
                    existing[key] = {"id": cur.lastrowid, "update_time": update_time}
                    changed += 1
            cur.execute("SELECT id,slot_code FROM slots")
            slots = {row[1]: row[0] for row in cur.fetchall()}
            for item in inventories:
                slot_id = slots.get(str(item.get("slot_code") or ""))
                material_id = material_ids.get(self._material_key(item))
                if not slot_id or not material_id:
                    continue
                cur.execute("SELECT id,update_time,batch_no,inbound_date,note FROM inventories WHERE slot_id=? AND material_id=? AND COALESCE(batch_no,'')=COALESCE(?, '')", (slot_id, material_id, item.get("batch_no")))
                old = cur.fetchone()
                incoming_time = _ts_key(item.get("update_time"))
                if old and _ts_key(old[1]) >= incoming_time:
                    continue
                incoming_batch = _norm_cell(item.get("batch_no"))
                incoming_date = _norm_cell(item.get("inbound_date"))
                incoming_note = _norm_cell(item.get("note"))
                if schema < 2 and old:
                    # schema 1 旧快照无批次字段：缺省值回填旧值，避免把本地非空
                    # 批次号/入库日期/备注降级覆盖为 NULL
                    incoming_batch = old[2] if incoming_batch is None else incoming_batch
                    incoming_date = old[3] if incoming_date is None else incoming_date
                    incoming_note = old[4] if incoming_note is None else incoming_note
                try:
                    quantity = max(0, int(item.get("quantity") or 0))
                except (TypeError, ValueError):
                    quantity = 0
                update_time = incoming_time or datetime.now().isoformat()
                if old:
                    cur.execute("UPDATE inventories SET quantity=?,batch_no=?,inbound_date=?,note=?,update_time=? WHERE id=?", (quantity, incoming_batch, incoming_date, incoming_note, update_time, old[0]))
                else:
                    cur.execute("INSERT INTO inventories(slot_id,material_id,quantity,batch_no,inbound_date,note,update_time) VALUES(?,?,?,?,?,?,?)", (slot_id, material_id, quantity, incoming_batch, incoming_date, incoming_note, update_time))
                changed += 1

            # 历史库存记忆库合并：去重键序与 ComponentLib.record 一致
            # （lcsc_code → supplier_part → model+package），update_time 新者胜
            comp_fields = ["lcsc_code", "supplier_part", "model", "name", "specification",
                           "package", "footprint", "brand", "category", "parameters",
                           "datasheet", "hit_count", "source"]
            for item in components:
                lcsc = str(item.get("lcsc_code") or "").strip()
                spart = str(item.get("supplier_part") or "").strip()
                model = str(item.get("model") or "").strip()
                if not (lcsc or spart or model):
                    continue
                values = []
                for field in comp_fields:
                    v = item.get(field)
                    if field == "parameters" and isinstance(v, (dict, list)):
                        v = json.dumps(v, ensure_ascii=False)
                    if field == "hit_count":
                        try:
                            v = max(0, int(v or 0))
                        except (TypeError, ValueError):
                            v = 0
                    if v is None:
                        v = "" if field != "hit_count" else 0
                    values.append(str(v).strip() if isinstance(v, str) else v)
                values = [_norm_cell(v) for v in values]
                old = None
                if lcsc:
                    cur.execute("SELECT id,update_time FROM component_library WHERE lcsc_code=?", (lcsc,))
                    old = cur.fetchone()
                if not old and spart:
                    cur.execute("SELECT id,update_time FROM component_library WHERE supplier_part=?", (spart,))
                    old = cur.fetchone()
                if not old and model:
                    cur.execute("""SELECT id,update_time FROM component_library
                                   WHERE model=? AND COALESCE(package,'')=COALESCE(?, '')""",
                                (model, item.get("package") or ""))
                    old = cur.fetchone()
                incoming_time = _ts_key(item.get("update_time"))
                if old and (not incoming_time or _ts_key(old[1]) >= incoming_time):
                    continue
                update_time = incoming_time or datetime.now().isoformat()
                if old:
                    cur.execute("UPDATE component_library SET " + ", ".join(f"{f}=?" for f in comp_fields)
                                + ", update_time=? WHERE id=?", values + [update_time, old[0]])
                else:
                    cur.execute("INSERT INTO component_library (" + ",".join(comp_fields)
                                + ",update_time) VALUES (" + ",".join("?" for _ in comp_fields) + ",?)",
                                values + [update_time])
                changed += 1
        return {"ok": True, "data": {"changed": changed}}

    def _fetch_snapshot_text(self):
        """下载云端快照文本。
        无 Token：优先 GitHub Raw 直链（免鉴权、不受 contents API 匿名
        60 次/小时限流影响），失败回退 contents API。
        有 Token：优先 contents API——Raw CDN 有约 300 秒缓存，刚上传的快照
        可能读回旧值，导致"按旧云端合并后上传"覆盖其他设备的新数据；
        contents API 无此缓存，是参与上传设备的强一致通道。
        任一通道返回 404 都代表"云端无快照"（权威判定）；单通道网络失败时
        回退另一通道。返回 (status, text)：200=成功，404=云端无快照，0=失败。"""
        cfg = self._settings()
        raw_url = "https://raw.githubusercontent.com/{owner}/{repo}/main/{file}".format(
            owner=cfg["owner"], repo=cfg["repo"], file=SNAPSHOT_NAME)
        api_url = self._api_url(f"repos/{cfg['owner']}/{cfg['repo']}/contents/{SNAPSHOT_NAME}")

        def via_raw():
            try:
                request = urllib.request.Request(raw_url, headers={"User-Agent": "material-cabinet"})
                with _urlopen_with_retry(request, 15) as response:
                    # lstrip("\ufeff")：GitHub 偶发以 UTF-8 BOM 返回文本，BOM 不属于
                    # str.strip() 默认空白集，残留会让 json.loads 与版本比对失败
                    return 200, response.read().decode("utf-8", errors="replace").lstrip("\ufeff")
            except urllib.error.HTTPError as exc:
                return (404, "") if exc.code == 404 else (0, f"Raw 通道错误 {exc.code}")
            except Exception as exc:  # noqa: BLE001 网络异常 → 交由调用方回退
                return 0, str(exc)

        def via_api():
            status, payload = self._request(api_url, auth=True)
            if status == 404:
                return 404, ""
            if status != 200:
                return 0, str(payload.get("message", status))
            try:
                raw = base64.b64decode(str(payload.get("content") or "").replace("\n", ""))
                return 200, raw.decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                return 0, str(exc)

        if self._token():
            primary, fallback = via_api, via_raw
        else:
            primary, fallback = via_raw, via_api
        result = primary()
        if result[0] != 0:
            return result
        # 回退通道返回 200 或 404 都以它为准：404 是"云端无快照"的权威判定，
        # 优于主通道的网络错误（弱网下 api.github.com 不通而 raw 可达是现实场景）
        retry = fallback()
        return retry if retry[0] in (200, 404) else result

    def _download_inventory(self):
        status, text = self._fetch_snapshot_text()
        if status == 404:
            return {"ok": True, "data": {"found": False, "message": "云端尚无库存快照"}}
        if status != 200:
            return {"ok": False, "error": f"下载库存失败：{text}"}
        try:
            snapshot = json.loads(text)
        except ValueError as exc:
            return {"ok": False, "error": f"云端库存快照损坏：{exc}"}
        if not isinstance(snapshot, dict):
            return {"ok": False, "error": "云端库存快照损坏：格式无效"}
        empty = not bool(snapshot.get("materials") or snapshot.get("inventories"))
        result = self.merge_snapshot(snapshot)
        if result.get("ok"):
            result.setdefault("data", {}).update({"found": True, "empty": empty})
        return result

    def download_inventory(self):
        with self._sync_lock:
            return self._download_inventory()

    def _upload_file(self, name, text, message):
        cfg = self._settings()
        url = self._api_url(f"repos/{cfg['owner']}/{cfg['repo']}/contents/{name}")
        content = base64.b64encode(text.encode("utf-8")).decode("ascii")
        for _ in range(2):
            status, current = self._request(url, auth=True)
            payload = {"message": message, "content": content}
            if status == 200 and current.get("sha"):
                payload["sha"] = current["sha"]
            result_status, result = self._request(url, method="PUT", data=payload, auth=True)
            if result_status in (200, 201):
                return {"ok": True}
            if result_status != 409:
                return {"ok": False, "error": f"上传{name}失败：{result.get('message', result_status)}"}
        return {"ok": False, "error": f"上传{name}失败：云端文件发生并发冲突，请稍后重试"}

    def _upload_inventory(self, snapshot=None, message="同步脱敏库存快照"):
        cfg = self._settings()
        if not self._token():
            return {"ok": False, "error": "未配置 GitHub Token，库存不会上传"}
        url = self._api_url(f"repos/{cfg['owner']}/{cfg['repo']}/contents/{SNAPSHOT_NAME}")
        snapshot = snapshot or self._snapshot()
        content = base64.b64encode(json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")
        for _ in range(2):
            status, current = self._request(url, auth=True)
            payload = {"message": message, "content": content}
            if status == 200 and current.get("sha"):
                payload["sha"] = current["sha"]
            result_status, result = self._request(url, method="PUT", data=payload, auth=True)
            if result_status in (200, 201):
                marker = self._upload_file(INVENTORY_MARKER_NAME, snapshot["inventory_version"], "更新库存版本标记")
                if not marker.get("ok"):
                    return marker
                return {"ok": True, "data": {"uploaded": True, "updated_at": snapshot["updated_at"], "inventory_version": snapshot["inventory_version"]}}
            if result_status != 409:
                return {"ok": False, "error": f"上传库存失败：{result.get('message', result_status)}"}
        return {"ok": False, "error": "上传库存失败：云端文件发生并发冲突，请稍后重试"}

    def upload_inventory(self, snapshot=None, message="同步脱敏库存快照"):
        with self._sync_lock:
            return self._upload_inventory(snapshot, message)

    def sync_inventory(self, prefer_local=False):
        with self._sync_lock:
            if prefer_local:
                uploaded = self._upload_inventory(message="清空后覆盖云端库存")
                if not uploaded.get("ok"):
                    return {"ok": False, "data": {"upload": uploaded, "message": uploaded.get("error")}, "error": uploaded.get("error")}
                return {"ok": True, "data": {"upload": uploaded.get("data", {}), "message": "空库存已上传并覆盖云端"}}

            local_snapshot = self._snapshot()
            # 版本标记只用于省流量的提前退出：读取失败（网络抖动）不应阻断同步，
            # 后续下载合并路径自身具备完整容错
            marker = self._read_marker(INVENTORY_MARKER_NAME)
            local_version = local_snapshot.get("inventory_version", "")
            remote_version = marker.get("value", "") if marker.get("ok") else ""
            if remote_version and remote_version == local_version:
                return {"ok": True, "data": {"skipped": True, "inventory_version": local_version, "message": "库存已是最新，无需下载"}}

            # 先下载合并云端（新者胜），失败不再短路——本机修改必须保留上传机会，
            # 否则出现"其他设备修改后永远无法上传"的死锁
            downloaded = self._download_inventory()
            download_ok = bool(downloaded.get("ok"))
            if download_ok and downloaded.get("data", {}).get("empty"):
                # 云端为空库存快照 = 上游设备已清空业务数据：本地同步清空，
                # 避免旧数据本地留存并在下次上传时回填复活云端。
                # 此路径不回传本地：防止空业务数据快照覆盖云端记忆库。
                from models.database import purge_demo_data
                try:
                    purge_demo_data()
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "error": f"同步云端空库存失败：{exc}"}
                return {"ok": True, "data": {
                    "download": downloaded.get("data", {}),
                    "emptied": True,
                    "message": "云端为空库存，本地业务数据已同步清空",
                }}

            if download_ok:
                # 合并成功：重新生成本地快照（已含云端合并结果），上传后云端即双方并集
                local_snapshot = self._snapshot()
            uploaded = self._upload_inventory(snapshot=local_snapshot, message="同步脱敏库存快照")
            upload_ok = bool(uploaded.get("ok"))

            if not download_ok and not upload_ok:
                return {"ok": False, "error": "库存同步失败：下载（%s）；上传（%s）" % (downloaded.get("error"), uploaded.get("error"))}
            if not download_ok:
                return {"ok": True, "data": {
                    "upload": uploaded.get("data", {}), "download_failed": True,
                    "message": "本机库存已上传云端；云端下载失败（%s），可稍后再次检查库存" % downloaded.get("error"),
                }, "warning": downloaded.get("error")}
            if not upload_ok:
                # 只读场景（未配置 Token/网络失败）：下载已成功应用本地，允许部分成功返回
                return {"ok": True, "data": {
                    "download": downloaded.get("data", {}),
                    "uploaded": False,
                    "message": "已应用云端库存；本机未配置 Token 或上传失败，未回传本机库存（%s）" % uploaded.get("error"),
                }, "warning": uploaded.get("error")}
            return {"ok": True, "data": {"download": downloaded.get("data", {}), "upload": uploaded.get("data", {}), "message": "库存同步完成"}}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "是")


def _version_tuple(value):
    nums = re.findall(r"\d+", str(value or ""))
    return tuple(int(x) for x in nums[:4]) + (0,) * max(0, 4 - len(nums))


def _snapshot_version(snapshot):
    payload = {"schema": snapshot.get("schema"), "materials": snapshot.get("materials", []),
               "inventories": snapshot.get("inventories", []), "components": snapshot.get("components", [])}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _auth_headers():
    headers = {"User-Agent": "material-cabinet"}
    token = GitHubSyncService._token()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _download_checksum(url, asset_name):
    if not url:
        return ""
    headers = _auth_headers()
    headers["Accept"] = "application/octet-stream"
    request = urllib.request.Request(url, headers=headers)
    try:
        with _urlopen_with_retry(request, 15) as response:
            text = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError):
        return ""
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].lower() not in ("sha256", "sha-256"):
            name = parts[-1].lstrip("*")
            if name == asset_name or os.path.basename(name) == asset_name:
                candidate = parts[0].lower()
                if re.fullmatch(r"[0-9a-f]{64}", candidate):
                    return candidate
    return ""


def _ts_key(value):
    """时间戳归一化：T/Z/毫秒/斜杠等格式统一为可比较的 'YYYY-MM-DD HH:MM:SS'"""
    s = str(value or "").strip()
    if not s:
        return ""
    t = s.replace("T", " ").replace("Z", "").split(".", 1)[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
                "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(t, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return t


def _validate_pe_file(path):
    """校验文件为合法 Windows PE 可执行文件（MZ 头 + PE 签名）。
    防止把限流页/错误页/半截文件当成 EXE 替换——那是更新后报
    “Failed to load Python DLL 'python312.dll'”的最常见根因。"""
    with open(path, "rb") as f:
        header = f.read(64)
    if len(header) < 64 or header[:2] != b"MZ":
        raise ValueError("下载文件不是有效的 Windows 程序（缺少 MZ 头），已中止替换")
    pe_offset = int.from_bytes(header[60:64], "little")
    if pe_offset <= 0 or pe_offset > 512 * 1024 * 1024:
        raise ValueError("下载文件 PE 结构异常，已中止替换")
    with open(path, "rb") as f:
        f.seek(pe_offset)
        if f.read(4) != b"PE\x00\x00":
            raise ValueError("下载文件 PE 签名校验失败，已中止替换")


def _download(url, path, attempts=2):
    """带重试与 PE 校验的下载：单次网络抖动/限流不再导致更新静默失败。
    外层 attempts=2 × 内层代理/直连交替 2 次 = 最多 4 次连接尝试。"""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            headers = _auth_headers()
            headers["Accept"] = "application/octet-stream"
            request = urllib.request.Request(url, headers=headers)
            with _urlopen_with_retry(request, 60) as response, open(path, "wb") as target:
                shutil.copyfileobj(response, target)
            _validate_pe_file(path)
            return
        except Exception as exc:  # noqa: BLE001 记录后重试
            last_error = exc
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise OSError(f"下载失败（已重试 {attempts * 2} 次）：{last_error}")


def _schedule_replace(current, downloaded, parent_pid=None):
    """Windows 下运行中的 EXE 无法被覆盖。本函数派生隐藏 PowerShell 替换器：
    先等待主进程退出，再把旧版改名 .old、新文件就位（保持原路径），
    启动新版并轮询健康端口：新版只有通过 PyInstaller bootloader（DLL 加载
    成功）并由主程序绑定单实例锁端口后才算存活；若进程存活但端口始终未
    监听（如杀软拦截导致 “Failed to load Python DLL” 报错弹窗阻塞），则
    强杀新进程、自动回滚旧版并重启。新版健康时保留 .old 备份，由新版
    启动成功后自行清理。全程写日志。
    """
    script = os.path.join(
        tempfile.gettempdir(),
        "material-cabinet-update-{}.ps1".format(os.getpid()))
    current_q = current.replace("'", "''")
    downloaded_q = downloaded.replace("'", "''")
    old_q = (current + ".old").replace("'", "''")
    pid = int(parent_pid or os.getpid())
    body = """$ErrorActionPreference='Continue'
$log=Join-Path $env:TEMP 'material-cabinet-update.log'
function W($m){ try { Add-Content -LiteralPath $log -Value ((Get-Date -Format 'HH:mm:ss')+' '+$m) } catch {} }
function Test-Pe($p){
  try {
    $fs=[IO.File]::OpenRead($p); $b=New-Object byte[] 2; $null=$fs.Read($b,0,2); $fs.Close()
    return ($b[0] -eq 77 -and $b[1] -eq 90)
  } catch { return $false }
}
function Test-Port($port){
  try {
    $c=New-Object Net.Sockets.TcpClient
    $done=$c.ConnectAsync('127.0.0.1',$port).Wait(1500)
    $ok=($done -and $c.Connected)
    $c.Close()
    return $ok
  } catch { return $false }
}
W 'updater start'
try {
  $waited=0.0
  while ((Get-Process -Id @PID@ -ErrorAction SilentlyContinue) -and $waited -lt 60) { Start-Sleep -Milliseconds 500; $waited+=0.5 }
  W ('parent exited after '+$waited+'s')
  $cur='@CUR@'
  $dl='@DL@'
  $old='@OLD@'
  $ok=$false
  for ($i=0; $i -lt 20 -and -not $ok; $i++) {
    try {
      if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Force -ErrorAction Stop }
      if (Test-Path -LiteralPath $cur) { Rename-Item -LiteralPath $cur -NewName ([IO.Path]::GetFileName($old)) -Force -ErrorAction Stop }
      Move-Item -LiteralPath $dl -Destination $cur -Force -ErrorAction Stop
      $ok=$true
    } catch { W ('attempt '+$i+' failed: '+$_.Exception.Message); Start-Sleep -Seconds 1 }
  }
  if (-not $ok) {
    W 'replace FAILED, restoring old'
    if ((Test-Path -LiteralPath $old) -and -not (Test-Path -LiteralPath $cur)) { Rename-Item -LiteralPath $old -NewName ([IO.Path]::GetFileName($cur)) -Force -ErrorAction SilentlyContinue }
  } elseif (-not (Test-Pe $cur)) {
    W 'new EXE header invalid, restoring old'
    Remove-Item -LiteralPath $cur -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $old) { Rename-Item -LiteralPath $old -NewName ([IO.Path]::GetFileName($cur)) -Force -ErrorAction SilentlyContinue }
    Start-Process -FilePath $cur -WorkingDirectory (Split-Path $cur -Parent)
  } else {
    W 'replaced, relaunching new version'
    $np = Start-Process -FilePath $cur -WorkingDirectory (Split-Path $cur -Parent) -PassThru
    $portOk=$false
    for ($w=0; $w -lt 12 -and -not $portOk; $w++) {
      Start-Sleep -Seconds 1
      if (-not (Get-Process -Id $np.Id -ErrorAction SilentlyContinue)) { break }
      if (Test-Port @PORT@) { $portOk=$true }
    }
    if ($portOk) {
      W ('new version healthy (port @PORT@ listening, pid '+$np.Id+')')
      W '.old backup kept: cleanup handled by new version after successful startup'
    } else {
      W 'new version failed to bind health port within 12s, rolling back to old version'
      if (-not $np.HasExited) { try { $np | Stop-Process -Force } catch {}; Start-Sleep -Seconds 1 }
      for ($r=0; $r -lt 5 -and (Test-Path -LiteralPath $cur); $r++) { Remove-Item -LiteralPath $cur -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
      if (Test-Path -LiteralPath $old) { Rename-Item -LiteralPath $old -NewName ([IO.Path]::GetFileName($cur)) -Force -ErrorAction SilentlyContinue }
      if (Test-Pe $cur) { Start-Process -FilePath $cur -WorkingDirectory (Split-Path $cur -Parent); W 'rollback complete, old version relaunched' }
      else { W 'rollback FAILED: old backup missing' }
    }
  }
} catch { W ('updater fatal: '+$_.Exception.Message) }
Remove-Item -LiteralPath $dl -Force -ErrorAction SilentlyContinue
W 'updater done'
"""
    body = (body.replace("@PID@", str(pid))
                .replace("@CUR@", current_q)
                .replace("@DL@", downloaded_q)
                .replace("@OLD@", old_q)
                .replace("@PORT@", str(INSTANCE_HEALTH_PORT)))
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(body)
    # PyInstaller onefile bootloader 会向本进程环境注入 _PYI_*（如
    # _PYI_APPLICATION_HOME_DIR 指向本实例临时解压目录）。替换器与它随后
    # 启动的新版程序若继承这些变量，新版 bootloader 会误判为 PyInstaller
    # 子进程而尝试挂接早已删除的旧解压目录，报
    # "Failed to load Python DLL ...\python3xx.dll"。派生前必须剔除。
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("_PYI_")}
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                      "-ExecutionPolicy", "Bypass", "-File", script],
                     env=child_env,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def cleanup_stale_update_files(delay_old=120.0):
    """清理上次更新残留（<exe>.old 旧版备份 / <exe>.download 半成品）。
    .download 是下载中断产物，更新器每次更新前都会清掉重下，可立即删除；
    .old 是更新器的回滚备份——替换完成后更新器会观察启动稳定性，新版异常
    退出就把 .old 改名回滚，本实例必须等存活超过观察窗口（120 秒留富余）
    再删，且只允许持有实例锁的主实例调用。"""
    if not getattr(sys, "frozen", False):
        return
    current = os.path.abspath(sys.executable)
    download_path = current + ".download"
    try:
        if os.path.exists(download_path):
            os.remove(download_path)
    except OSError:
        pass
    old_path = current + ".old"
    if not os.path.exists(old_path):
        return

    def _remove_old():
        try:
            os.remove(old_path)
        except OSError:
            pass

    if delay_old and delay_old > 0:
        threading.Timer(delay_old, _remove_old).start()
    else:
        _remove_old()


def _json_value(value):
    if not value:
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _dpapi_protect(data):
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    source = ctypes.create_string_buffer(data)
    source_blob = Blob(len(data), source)
    output_blob = Blob()
    if not crypt.CryptProtectData(ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(output_blob)):
        raise OSError("Windows 数据保护失败")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel.LocalFree(output_blob.pbData)


def _dpapi_unprotect(data):
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    source = ctypes.create_string_buffer(data)
    source_blob = Blob(len(data), source)
    output_blob = Blob()
    if not crypt.CryptUnprotectData(ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(output_blob)):
        raise OSError("Windows 数据保护读取失败")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel.LocalFree(output_blob.pbData)
