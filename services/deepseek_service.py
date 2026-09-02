# -*- coding: utf-8 -*-
"""DeepSeek AI 智能匹配服务

通过 DeepSeek API 实现物料智能匹配：
- 无网/未配置 API Key 时：自动回退到本地参数/供应商编号匹配
- 联网+配置 Key 时：使用 AI 做详细参数比对和替代料推荐
"""
import json
import logging
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None


class DeepSeekService:
    """DeepSeek API 客户端 + 本地 fallback"""

    def __init__(self):
        try:
            from models.database import AppSettings
            self._settings = AppSettings
        except Exception:
            self._settings = None
        self._timeout = 30

    def _get_config(self):
        """从 AppSettings 读取配置，回退到 config.py"""
        try:
            from config import DEEPSEEK_API_BASE, DEEPSEEK_API_KEY, DEEPSEEK_MODEL
        except Exception:
            DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"
            DEEPSEEK_API_KEY = ""
            DEEPSEEK_MODEL = "deepseek-v4-flash"

        key = DEEPSEEK_API_KEY
        base = DEEPSEEK_API_BASE
        model = DEEPSEEK_MODEL

        if self._settings:
            key = self._settings.get("deepseek_api_key") or key
            base = self._settings.get("deepseek_api_base") or base
            model = self._settings.get("deepseek_model") or model

        return key, base, model

    def is_available(self) -> bool:
        """是否已配置 API Key 且可联网"""
        key, _, _ = self._get_config()
        if not key:
            return False
        if not requests:
            return False
        return True

    def _chat(self, messages: List[Dict], temperature: float = 0.1) -> Optional[str]:
        """调用 DeepSeek chat completions 接口"""
        key, base, model = self._get_config()
        if not key or not requests:
            return None
        try:
            url = f"{base}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            }
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 4096,
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=self._timeout)
            if resp.status_code != 200:
                logger.warning(f"DeepSeek API HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"DeepSeek API 请求失败: {e}")
            return None

    # ================================================================
    #  BOM 智能匹配
    # ================================================================
    def match_bom_items(self, bom_items: List[Dict], local_materials: List[Dict]) -> Dict:
        """使用 AI 将 BOM 行与本地库存匹配

        Args:
            bom_items: BOM 明细列表 [{material_code, material_name, specification, package, required_qty, ...}]
            local_materials: 本地物料列表 [{id, material_code, name, specification, package, category, total_qty, ...}]

        Returns:
            {results: [{bom_index, matched_material_id, matched_material_code,
                        matched_material_name, confidence, reason, available_qty}], offline: bool}
        """
        if not self.is_available():
            return {"results": [], "offline": True, "error": "DeepSeek API 未配置或不可用"}

        # 构造提示词
        bom_desc = json.dumps([{
            "index": i,
            "code": it.get("material_code", ""),
            "name": it.get("material_name", ""),
            "spec": it.get("specification", ""),
            "package": it.get("package", ""),
            "qty": it.get("required_qty", 0),
        } for i, it in enumerate(bom_items)], ensure_ascii=False)

        local_desc = json.dumps([{
            "id": m.get("id"),
            "code": m.get("material_code", ""),
            "name": m.get("name") or m.get("material_name", ""),
            "spec": m.get("specification", ""),
            "package": m.get("package", ""),
            "category": m.get("category", ""),
            "qty": m.get("total_qty", 0),
        } for m in local_materials], ensure_ascii=False)

        messages = [
            {"role": "system", "content": "你是一个电子元器件匹配专家。用户会给你BOM清单和本地库存列表，"
             "请逐行匹配最合适的物料。如果参数可兼容但供应商编号不同，也可以匹配。"
             "返回JSON格式：{\"results\": [{\"bom_index\": 0, \"matched_material_id\": 1, "
             "\"matched_material_code\": \"R001\", \"matched_material_name\": \"10kΩ电阻\", "
             "\"confidence\": 95, \"reason\": \"参数一致\", \"available_qty\": 100}]}。"
             "如果某行无法匹配，matched_material_id 设为 null，confidence 为 0。"},
            {"role": "user", "content": f"BOM清单：\n{bom_desc}\n\n本地库存：\n{local_desc}"}
        ]

        reply = self._chat(messages)
        if not reply:
            return {"results": [], "offline": True, "error": "AI 请求失败"}

        try:
            # 尝试从回复中提取 JSON
            reply = reply.strip()
            if reply.startswith("```"):
                reply = reply.split("```")[1]
                if reply.startswith("json"):
                    reply = reply[4:]
            result = json.loads(reply)
            return {"results": result.get("results", []), "offline": False}
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"DeepSeek 返回解析失败: {e}")
            return {"results": [], "offline": False, "error": f"AI 返回解析失败: {e}"}

    # ================================================================
    #  参数比对（AI 增强版）
    # ================================================================
    def compare_parameters(self, material1: Dict, material2: Dict) -> Dict:
        """使用 AI 比对两个物料的参数，判断是否可替换

        Returns: {can_replace: bool, confidence: int, reason: str, differences: list}
        """
        if not self.is_available():
            # 回退到本地比对
            return self._local_compare(material1, material2)

        messages = [
            {"role": "system", "content": "你是电子元器件参数比对专家。判断两个物料是否可以互相替换。"
             "考虑：阻值/容值/电感值、封装、精度、耐压、功率等关键参数。"
             "返回JSON：{\"can_replace\": true/false, \"confidence\": 0-100, \"reason\": \"...\", \"differences\": [\"...\"]}"},
            {"role": "user", "content": f"物料1：{json.dumps(material1, ensure_ascii=False)}\n"
             f"物料2：{json.dumps(material2, ensure_ascii=False)}"}
        ]
        reply = self._chat(messages, temperature=0.05)
        if not reply:
            return self._local_compare(material1, material2)
        try:
            reply = reply.strip()
            if reply.startswith("```"):
                reply = reply.split("```")[1]
                if reply.startswith("json"):
                    reply = reply[4:]
            return json.loads(reply)
        except (json.JSONDecodeError, IndexError):
            return self._local_compare(material1, material2)

    @staticmethod
    def _local_compare(m1: Dict, m2: Dict) -> Dict:
        """本地参数比对（离线 fallback）"""
        spec1 = (m1.get("specification") or "").strip().lower().replace(" ", "")
        spec2 = (m2.get("specification") or "").strip().lower().replace(" ", "")
        pkg1 = (m1.get("package") or "").strip().lower()
        pkg2 = (m2.get("package") or "").strip().lower()

        diffs = []
        score = 0

        if spec1 and spec2:
            if spec1 == spec2:
                score += 50
            else:
                diffs.append(f"规格不同: {m1.get('specification')} vs {m2.get('specification')}")
        if pkg1 and pkg2:
            if pkg1 == pkg2:
                score += 30
            else:
                diffs.append(f"封装不同: {m1.get('package')} vs {m2.get('package')}")

        # 供应商编号匹配
        code1 = m1.get("material_code") or m1.get("supplier_code") or ""
        code2 = m2.get("material_code") or m2.get("supplier_code") or ""
        if code1 and code2 and code1 == code2:
            score += 20

        can_replace = score >= 50
        return {
            "can_replace": can_replace,
            "confidence": min(100, score),
            "reason": "本地参数匹配" if can_replace else "参数不兼容",
            "differences": diffs,
        }

    # ================================================================
    #  替代料推荐
    # ================================================================
    def find_replacements(self, material: Dict, candidates: List[Dict]) -> List[Dict]:
        """使用 AI 从候选列表中找出可替换物料"""
        if not self.is_available() or not candidates:
            # 本地排序
            return self._local_find_replacements(material, candidates)

        messages = [
            {"role": "system", "content": "你是电子元器件替代料推荐专家。从候选列表中找出可以替换目标物料的物料。"
             "返回JSON：{\"replacements\": [{\"candidate_index\": 0, \"confidence\": 90, \"reason\": \"参数一致\"}]}"},
            {"role": "user", "content": f"目标物料：{json.dumps(material, ensure_ascii=False)}\n"
             f"候选列表：{json.dumps([{'index': i, **c} for i, c in enumerate(candidates)], ensure_ascii=False)}"}
        ]
        reply = self._chat(messages)
        if not reply:
            return self._local_find_replacements(material, candidates)
        try:
            reply = reply.strip()
            if reply.startswith("```"):
                reply = reply.split("```")[1]
                if reply.startswith("json"):
                    reply = reply[4:]
            result = json.loads(reply)
            replacements = []
            for r in result.get("replacements", []):
                idx = r.get("candidate_index", -1)
                if 0 <= idx < len(candidates):
                    c = dict(candidates[idx])
                    c["confidence"] = r.get("confidence", 0)
                    c["reason"] = r.get("reason", "")
                    replacements.append(c)
            return replacements
        except (json.JSONDecodeError, IndexError):
            return self._local_find_replacements(material, candidates)

    @staticmethod
    def _local_find_replacements(material: Dict, candidates: List[Dict]) -> List[Dict]:
        """本地替代料排序（离线 fallback）"""
        results = []
        target_spec = (material.get("specification") or "").strip().lower().replace(" ", "")
        target_pkg = (material.get("package") or "").strip().lower()

        for c in candidates:
            score = 0
            c_spec = (c.get("specification") or "").strip().lower().replace(" ", "")
            c_pkg = (c.get("package") or "").strip().lower()
            if target_spec and c_spec and target_spec == c_spec:
                score += 50
            if target_pkg and c_pkg and target_pkg == c_pkg:
                score += 30
            if c.get("category") == material.get("category"):
                score += 20
            if score > 0:
                c2 = dict(c)
                c2["confidence"] = min(100, score)
                c2["reason"] = "本地参数匹配"
                results.append(c2)
        results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        return results[:5]
