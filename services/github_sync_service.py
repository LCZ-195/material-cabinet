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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from config import BASE_DIR, DB_PATH
from models.database import AppSettings, get_cursor

DEFAULT_OWNER = "LCZ-195"
DEFAULT_REPO = "material-cabinet"
SNAPSHOT_NAME = "inventory_sync.json"
USER_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or BASE_DIR, "物料收纳柜")
TOKEN_FILE = os.path.join(USER_DATA_DIR, "github_token.bin")


class GitHubSyncService:
    def __init__(self, app_name="物料收纳柜", app_version="0.0.0"):
        self.app_name = app_name
        self.app_version = app_version
        self._sync_lock = threading.Lock()

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
    def save_configuration(owner, repo, token, auto_update=True, auto_inventory=True):
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
        elif token == "":
            pass
        return {"ok": True, "data": {"settings": GitHubSyncService._settings()}}

    @staticmethod
    def _api_url(path):
        encoded = "/".join(urllib.parse.quote(p, safe="") for p in path.strip("/").split("/"))
        return "https://api.github.com/" + encoded

    def _request(self, url, method="GET", data=None, auth=True, timeout=8):
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
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            return exc.code, {"message": detail}
        except (OSError, ValueError) as exc:
            return 0, {"message": str(exc)}

    def check_version(self):
        status, payload = self._request(self._api_url(f"repos/{self._settings()['owner']}/{self._settings()['repo']}/releases/latest"), auth=True)
        if status == 404:
            return {"ok": True, "data": {"available": False, "message": "仓库尚未发布 Release"}}
        if status != 200:
            return {"ok": False, "error": f"版本检查失败：{payload.get('message', status)}"}
        tag = str(payload.get("tag_name") or "").lstrip("vV")
        assets = payload.get("assets", [])
        asset = next((x for x in assets if str(x.get("name", "")).lower().endswith(".exe")), None)
        if not asset:
            return {"ok": True, "data": {"available": False, "version": tag, "message": "Release 未包含 EXE 文件"}}
        checksum_asset = next((x for x in assets if str(x.get("name", "")).lower() in ("sha256.txt", "checksums.txt", "checksums.sha256")), None)
        available = _version_tuple(tag) > _version_tuple(self.app_version)
        return {"ok": True, "data": {"available": available, "version": tag, "name": asset.get("name"), "download_url": asset.get("browser_download_url"), "checksum_url": checksum_asset.get("browser_download_url") if checksum_asset else "", "release_url": payload.get("html_url")}}

    def schedule_update(self):
        result = self.check_version()
        if not result.get("ok") or not result.get("data", {}).get("available"):
            return result
        data = result["data"]
        current = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else os.path.join(BASE_DIR, "物料收纳柜.exe"))
        if not os.path.isfile(current):
            return {"ok": False, "error": "找不到当前 EXE，无法执行替换更新"}
        temp_path = current + ".download"
        try:
            _download(data["download_url"], temp_path)
            if os.path.getsize(temp_path) < 1024 * 1024:
                raise ValueError("下载文件大小异常")
            expected = _download_checksum(data.get("checksum_url"), data["name"])
            if not expected:
                raise ValueError("Release 缺少 EXE SHA-256 校验文件，已拒绝更新")
            actual = _sha256_file(temp_path)
            if actual.lower() != expected.lower():
                raise ValueError("更新文件 SHA-256 校验失败")
            _schedule_replace(current, temp_path)
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
        for item in materials:
            item["parameters"] = _json_value(item.get("parameters"))
        return {"schema": 1, "updated_at": datetime.now(timezone.utc).isoformat(), "materials": materials, "inventories": inventories}

    def export_local_snapshot(self):
        return {"ok": True, "data": self._snapshot()}

    @staticmethod
    def _material_key(item):
        return str(item.get("material_code") or item.get("supplier_code") or item.get("lcsc_code") or item.get("name") or "").strip().lower()

    def merge_snapshot(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("schema") != 1:
            return {"ok": False, "error": "库存快照格式不受支持"}
        materials = snapshot.get("materials")
        inventories = snapshot.get("inventories")
        if not isinstance(materials, list) or not isinstance(inventories, list) or len(materials) > 100000 or len(inventories) > 100000:
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
                values = [item.get(f) for f in fields]
                if isinstance(values[-1], (dict, list)):
                    values[-1] = json.dumps(values[-1], ensure_ascii=False)
                for index, field in enumerate(fields):
                    if values[index] is None:
                        values[index] = "" if field != "min_stock" else 0
                try:
                    values[fields.index("min_stock")] = max(0, int(values[fields.index("min_stock")]))
                except (TypeError, ValueError):
                    values[fields.index("min_stock")] = 0
                old = existing.get(key)
                incoming_time = str(item.get("update_time") or "")
                if old and incoming_time and str(old.get("update_time") or "") >= incoming_time:
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
                cur.execute("SELECT id,update_time FROM inventories WHERE slot_id=? AND material_id=? AND COALESCE(batch_no,'')=COALESCE(?, '')", (slot_id, material_id, item.get("batch_no")))
                old = cur.fetchone()
                incoming_time = str(item.get("update_time") or "")
                if old and str(old[1] or "") >= incoming_time:
                    continue
                try:
                    quantity = max(0, int(item.get("quantity") or 0))
                except (TypeError, ValueError):
                    quantity = 0
                update_time = incoming_time or datetime.now().isoformat()
                if old:
                    cur.execute("UPDATE inventories SET quantity=?,batch_no=?,inbound_date=?,note=?,update_time=? WHERE id=?", (quantity, item.get("batch_no"), item.get("inbound_date"), item.get("note"), update_time, old[0]))
                else:
                    cur.execute("INSERT INTO inventories(slot_id,material_id,quantity,batch_no,inbound_date,note,update_time) VALUES(?,?,?,?,?,?,?)", (slot_id, material_id, quantity, item.get("batch_no"), item.get("inbound_date"), item.get("note"), update_time))
                changed += 1
        return {"ok": True, "data": {"changed": changed}}

    def download_inventory(self):
        cfg = self._settings()
        status, payload = self._request(self._api_url(f"repos/{cfg['owner']}/{cfg['repo']}/contents/{SNAPSHOT_NAME}"), auth=True)
        if status == 404:
            return {"ok": True, "data": {"found": False, "message": "云端尚无库存快照"}}
        if status != 200:
            return {"ok": False, "error": f"下载库存失败：{payload.get('message', status)}"}
        try:
            raw = base64.b64decode(str(payload.get("content") or "").replace("\n", ""))
            snapshot = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            return {"ok": False, "error": f"云端库存快照损坏：{exc}"}
        result = self.merge_snapshot(snapshot)
        if result.get("ok"):
            result.setdefault("data", {})["found"] = True
        return result

    def upload_inventory(self):
        cfg = self._settings()
        if not self._token():
            return {"ok": False, "error": "未配置 GitHub Token，库存不会上传"}
        url = self._api_url(f"repos/{cfg['owner']}/{cfg['repo']}/contents/{SNAPSHOT_NAME}")
        status, current = self._request(url, auth=True)
        snapshot = self._snapshot()
        content = base64.b64encode(json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")
        payload = {"message": "同步脱敏库存快照", "content": content}
        if status == 200 and current.get("sha"):
            payload["sha"] = current["sha"]
        result_status, result = self._request(url, method="PUT", data=payload, auth=True)
        if result_status not in (200, 201):
            return {"ok": False, "error": f"上传库存失败：{result.get('message', result_status)}"}
        return {"ok": True, "data": {"uploaded": True, "updated_at": snapshot["updated_at"]}}

    def sync_inventory(self):
        with self._sync_lock:
            downloaded = self.download_inventory()
            if not downloaded.get("ok"):
                return downloaded
            uploaded = self.upload_inventory()
        if not uploaded.get("ok"):
            return {"ok": True, "data": {"download": downloaded.get("data", {}), "upload": uploaded, "message": uploaded.get("error")}}
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


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_checksum(url, asset_name):
    if not url:
        return ""
    request = urllib.request.Request(url, headers={"User-Agent": "material-cabinet"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
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


def _download(url, path):
    request = urllib.request.Request(url, headers={"User-Agent": "material-cabinet"})
    with urllib.request.urlopen(request, timeout=30) as response, open(path, "wb") as target:
        shutil.copyfileobj(response, target)


def _schedule_replace(current, downloaded):
    script = os.path.join(tempfile.gettempdir(), "material-cabinet-update.ps1")
    current_q = current.replace("'", "''")
    downloaded_q = downloaded.replace("'", "''")
    backup_q = (current + ".backup").replace("'", "''")
    script_body = "$ErrorActionPreference='Stop'; Start-Sleep -Seconds 2; Copy-Item -LiteralPath '{}' -Destination '{}' -Force; try {{ Move-Item -LiteralPath '{}' -Destination '{}.new' -Force; Move-Item -LiteralPath '{}.new' -Destination '{}' -Force; Start-Process -FilePath '{}' }} catch {{ Copy-Item -LiteralPath '{}' -Destination '{}' -Force; throw }} finally {{ Remove-Item -LiteralPath '{}' -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath '{}' -Force -ErrorAction SilentlyContinue }}".format(current_q, backup_q, downloaded_q, current_q, current_q, current_q, current_q, current_q, backup_q, current_q, downloaded_q, backup_q)
    with open(script, "w", encoding="utf-8") as f:
        f.write(script_body)
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", script], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


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
