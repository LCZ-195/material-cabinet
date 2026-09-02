# -*- coding: utf-8 -*-
"""立创商城API对接模块

联网策略（三级回退）：
1. 立创商城公开网页接口（无需AppKey，wmsc.lcsc.com，优先使用）
2. 立创开放平台API（open.szlcsc.com，需在 config.py 配置 LCSC_APP_KEY/SECRET）
3. 本地参数比对兜底（完全离线也能工作）
"""
import hashlib
import time
import json
import logging
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None


class LCSCApi:
    """立创商城API客户端"""

    PUB_BASE = "https://wmsc.lcsc.com"

    PUB_HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.szlcsc.com/",
    }

    OFFLINE_COOLDOWN = 60.0

    _shared_instance = None

    @classmethod
    def get_shared(cls):
        """共享实例：让 60s 离线冷却跨 BOM 行生效，避免断网时每行都等网络超时"""
        if cls._shared_instance is None:
            cls._shared_instance = cls()
        return cls._shared_instance

    def __init__(self, app_key: str = "", app_secret: str = "", base_url: str = ""):
        self.pub_base = self.PUB_BASE
        self._headers = dict(self.PUB_HEADERS)
        self._online_ok = False
        self._offline_until = 0.0
        # 公开接口无需密钥；开放平台密钥仅从 config.py 读取（配置栏目已移除）
        from config import LCSC_API_BASE, LCSC_APP_KEY, LCSC_APP_SECRET
        self.app_key = app_key or LCSC_APP_KEY
        self.app_secret = app_secret or LCSC_APP_SECRET
        self.base_url = base_url or LCSC_API_BASE
        self.timeout = 10
        self._mock_data = {}

    # ---------- 公开网页接口（无需AppKey，联网优先路径） ----------
    def _pub_request(self, method: str, url: str, **kwargs) -> Dict:
        """公开接口请求；连续失败时进入60秒冷却，避免每个操作都卡网络超时"""
        if not requests:
            return {}
        if time.time() < self._offline_until:
            return {}
        try:
            resp = requests.request(method, url, timeout=self.timeout,
                                    headers=self._headers, **kwargs)
            if resp.status_code == 200:
                self._online_ok = True
                self._offline_until = 0.0
                try:
                    return resp.json() or {}
                except ValueError:
                    return {}
            logger.warning("LCSC公开接口 HTTP %s: %s", resp.status_code, url)
        except Exception as e:
            logger.warning("LCSC公开接口请求失败: %s (%s)", url, e)
        self._offline_until = time.time() + self.OFFLINE_COOLDOWN
        return {}

    def _pub_search(self, keyword: str, page: int = 1, page_size: int = 20) -> List[Dict]:
        """公开接口商品搜索：POST /ftps/wm/product/query/list"""
        data = self._pub_request(
            "POST", f"{self.pub_base}/ftps/wm/product/query/list",
            json={"currentPage": page, "pageSize": page_size, "keyword": keyword},
        )
        result = data.get("result") or {}
        if isinstance(result, dict):
            return result.get("dataList") or result.get("productList") or result.get("list") or []
        return result if isinstance(result, list) else []

    def _pub_detail(self, product_code: str) -> Optional[Dict]:
        """公开接口商品详情：GET /ftps/wm/product/detail"""
        data = self._pub_request(
            "GET", f"{self.pub_base}/ftps/wm/product/detail",
            params={"productCode": product_code},
        )
        result = data.get("result")
        return result if isinstance(result, dict) else None

    @staticmethod
    def _normalize_product(row: Dict) -> Dict:
        """统一公开接口/开放平台字段命名，前端按 model/brand/specification 取值"""
        params = {}
        for p in row.get("paramVOList") or []:
            if isinstance(p, dict) and (p.get("paramName") or p.get("name")):
                params[str(p.get("paramName") or p.get("name"))] = \
                    p.get("paramValue") if p.get("paramValue") is not None else (p.get("value") or "")
        intro = (row.get("productIntroEn") or row.get("productNameEn")
                 or row.get("productDescEn") or row.get("productKeyAttributes") or "")
        price = ""
        prod_list = row.get("productPriceList") or []
        if prod_list and isinstance(prod_list[0], dict):
            price = prod_list[0].get("productPrice") or prod_list[0].get("moneyPrice") or ""
        return {
            "lcsc_code": row.get("productCode") or "",
            "productModel": row.get("productModel") or "",
            "model": row.get("productModel") or "",
            "name": intro,
            "brand": row.get("brandNameEn") or row.get("brandName") or "",
            "specification": intro,
            "package": row.get("encapStandard") or "",
            "stock": row.get("stockNumber") or 0,
            "price": price,
            "category": (row.get("parentCatalogName") or row.get("catalogName") or ""),
            "parameters": params,
            "datasheet": row.get("pdfUrl") or "",
        }

    # ---------- 基础签名 ----------
    def _sign(self, params: Dict) -> str:
        """生成签名（立创API规范: 按key排序后拼接 + secret，MD5大写）"""
        sorted_keys = sorted(params.keys())
        raw = "".join(f"{k}{params[k]}" for k in sorted_keys) + self.app_secret
        return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()

    def _request(self, method: str, params: Dict) -> Dict:
        """发送请求，失败返回空dict以便上层回退到本地逻辑"""
        if not requests:
            return {}
        if not self.app_key or not self.app_secret:
            # 未配置key时走mock/离线模式
            return {}
        try:
            payload = {
                "app_key": self.app_key,
                "timestamp": str(int(time.time() * 1000)),
                "method": method,
                "sign_method": "md5",
                "format": "json",
                "v": "1.0",
                **params,
            }
            payload["sign"] = self._sign(payload)
            resp = requests.post(self.base_url, data=payload, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"LCSC API HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"LCSC API 请求失败: {e}")
        return {}

    # ---------- 公开接口 ----------
    def search_product(self, keyword: str, page: int = 1, page_size: int = 20) -> List[Dict]:
        """搜索商品（联网优先：公开接口 → 开放平台；均失败返回空列表）"""
        if not (keyword or "").strip():
            return []
        # 1) 公开网页接口（无需AppKey）
        rows = self._pub_search(keyword, page, page_size)
        if rows:
            return [self._normalize_product(r) for r in rows if isinstance(r, dict)]
        # 2) 开放平台API（需AppKey）
        result = self._request("product.search", {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
        })
        rows = result.get("data", {}).get("list", []) or []
        if rows:
            return [self._normalize_product(r) if isinstance(r, dict) and r.get("productCode")
                    else r for r in rows]
        return []

    def get_product_detail(self, lcsc_code: str) -> Optional[Dict]:
        """根据立创编号获取商品详情（含参数、替换料）"""
        code = (lcsc_code or "").strip().upper()
        if not code:
            return None
        # 1) 公开网页接口（无需AppKey）
        row = self._pub_detail(code)
        if row:
            return self._normalize_product(row)
        # 2) 开放平台API（需AppKey）
        result = self._request("product.detail", {"product_code": code})
        detail = result.get("data")
        if isinstance(detail, dict) and detail:
            return self._normalize_product(detail) if detail.get("productCode") else detail
        return None

    def get_replacement_parts(self, lcsc_code: str) -> List[Dict]:
        """获取立创推荐的替代物料"""
        detail = self.get_product_detail(lcsc_code)
        if detail:
            return detail.get("replacement_parts", []) or detail.get("similar_parts", []) or []
        # 兜底：用搜索返回的结果
        products = self.search_product(lcsc_code, page_size=10)
        return products[1:] if len(products) > 1 else []

    def get_product_parameters(self, lcsc_code: str) -> Dict:
        """获取规格参数表"""
        detail = self.get_product_detail(lcsc_code)
        if not detail:
            return {}
        params = detail.get("parameters")
        if isinstance(params, dict) and params:
            return params
        result = {}
        for p in params or []:
            if isinstance(p, dict):
                result[p.get("name", "")] = p.get("value", "")
        return result

    def compare_parameters(self, params1: Dict, params2: Dict) -> Tuple[int, List[str]]:
        """比对两个参数集合，返回 (匹配度0-100, 差异项列表)

        核心价值：当立创编号不同但参数一致/兼容时，可判断是否可替换。
        例如：10kΩ 0805 ±5% 电阻，不同品牌可替换。
        """
        if not params1 or not params2:
            return 0, ["无参数数据"]
        keys1 = set(params1.keys())
        keys2 = set(params2.keys())
        shared = keys1 & keys2
        if not shared:
            return 10, ["无共同参数项"]
        diffs = []
        matches = 0
        # 关键字段加权
        critical_keys = {
            "阻值", "电阻值", "容值", "电容值", "电感值", "封装", "精度",
            "耐压值", "额定电压", "额定电流", "功率", "频率",
            "Resistance", "Capacitance", "Inductance", "Package",
            "Tolerance", "Voltage Rating", "Current Rating", "Power Rating",
        }
        c_w = 3  # 关键字段权重
        n_w = 1  # 普通字段权重
        total_w = 0
        match_w = 0
        for k in shared:
            w = c_w if k in critical_keys else n_w
            total_w += w
            v1 = str(params1[k]).strip().lower().replace(" ", "")
            v2 = str(params2[k]).strip().lower().replace(" ", "")
            # 归一化判断（如 10kΩ == 10kohm == 10000Ω）
            if self._param_equal(v1, v2):
                match_w += w
                matches += 1
            else:
                diffs.append(f"{k}: {params1[k]} ≠ {params2[k]}")
        # 仅一方存在的非关键字段不扣分，但关键缺失标记
        for k in (keys1 - keys2) | (keys2 - keys1):
            if k in critical_keys:
                diffs.append(f"[缺失关键参数] {k}")
                total_w += c_w
        score = int(match_w / total_w * 100) if total_w else 0
        return score, diffs

    @staticmethod
    def _param_equal(a: str, b: str) -> bool:
        """参数值归一化比较"""
        if a == b:
            return True
        # 单位归一化: 电阻 10k 10K 10000
        def normalize(v: str) -> str:
            multipliers = {
                "k": 1e3, "kohm": 1e3, "kω": 1e3,
                "m": 1e6, "mohm": 1e6, "mω": 1e6,
                "nf": 1e-9, "pf": 1e-12, "uf": 1e-6, "μf": 1e-6,
                "mv": 1e-3, "kv": 1e3,
                "ma": 1e-3,
                "w": 1, "mw": 1e-3, "kw": 1e3,
            }
            for unit, mul in multipliers.items():
                if v.endswith(unit):
                    try:
                        num = float(v[:-len(unit)])
                        return f"{num * mul:.10g}"
                    except ValueError:
                        pass
            return v
        na = normalize(a)
        nb = normalize(b)
        if na != a or nb != b:
            try:
                if abs(float(na) - float(nb)) < 1e-6:
                    return True
            except ValueError:
                pass
        return False


# ---------- 本地离线模式：参数比对服务 ----------
class LocalParameterMatcher:
    """本地参数比对（无需立创API也能工作）

    从 materials.parameters (JSON字段) 读取规格，
    为 BOM 比对提供"替换物料"候选。
    """

    def __init__(self):
        self.api = LCSCApi()  # 若配置了key则顺带联网

    def find_candidates_from_db(self, material) -> List[Dict]:
        """在本地物料库中查找可替换物料，返回带匹配度的列表"""
        from models.material_model import Material
        spec = material.get("specification", "")
        pkg = material.get("package", "")
        cat = material.get("category", "")
        params = Material.get_parameters(material["id"]) if material.get("id") else {}

        candidates = Material.find_replacement_candidates(
            specification=spec, package=pkg, category=cat
        )
        scored = []
        for cand in candidates:
            if cand["id"] == material.get("id"):
                continue
            cand_params = Material.get_parameters(cand["id"])
            # 规格/封装直接一致优先
            base = 40
            if cand.get("package") == pkg and pkg:
                base += 25
            if cand.get("specification") == spec and spec:
                base += 25
            if params and cand_params:
                score, _ = self.api.compare_parameters(params, cand_params)
                base = int(base * 0.3 + score * 0.7)
            scored.append({**cand, "_match_score": min(100, base)})
        scored.sort(key=lambda x: x["_match_score"], reverse=True)
        return scored

    def enrich_material_from_lcsc(self, material) -> Dict:
        """尝试联网从立创获取详情，填入parameters字段（不保存）"""
        lcsc = material.get("lcsc_code") or material.get("supplier_code")
        if not lcsc:
            return material
        detail = self.api.get_product_detail(lcsc)
        if detail:
            material.setdefault("_lcsc_detail", detail)
            params = self.api.get_product_parameters(lcsc)
            if params:
                material["_lcsc_params"] = params
        return material
