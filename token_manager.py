#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 管理核心逻辑模块"""

import json
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime
import requests

# exe 运行时用 exe 所在目录，否则用脚本目录
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

TOKENS_FILE = BASE_DIR / "tokens.json"
FACTORY_AUTH_FILE = Path(os.path.expanduser("~")) / ".factory" / "auth.json"
REFRESH_URL = "https://api.workos.com/user_management/authenticate"
USAGE_URL = "https://app.factory.ai/api/organization/members/chat-usage"
CLIENT_ID = "client_01HNM792M5G5G1A2THWPXKFMXB"
WARN_THRESHOLD = 0.9


class TokenManager:
    """Token 管理核心类"""
    
    def __init__(self):
        self._switch_inflight = False
        self._last_active_ratio = None
    
    @staticmethod
    def atomic_write_json(path: Path, data) -> bool:
        """原子写入 JSON，避免写入中断导致文件损坏。"""
        tmp_path = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
            return True
        except Exception:
            try:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    @staticmethod
    def generate_id() -> str:
        """生成唯一 ID（时间戳）"""
        return str(int(time.time() * 1000))

    @staticmethod
    def load_backup_tokens():
        """加载备用账号池"""
        if not TOKENS_FILE.exists():
            TokenManager.save_backup_tokens([])
            return []
        try:
            with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "tokens" in data:
                    return data["tokens"]
                return data if isinstance(data, list) else []
        except Exception:
            return []

    @staticmethod
    def save_backup_tokens(tokens: list):
        """保存备用账号池"""
        TokenManager.atomic_write_json(TOKENS_FILE, tokens)

    @staticmethod
    def load_active_token():
        """从 ~/.factory/auth.json 读取当前激活的 token"""
        if not FACTORY_AUTH_FILE.exists():
            return None
        try:
            with open(FACTORY_AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                rt = data.get("refresh_token", "").strip()
                at = data.get("access_token", "").strip()
                token_id = data.get("id", "")
                if rt or at:
                    return {"id": token_id, "refresh_token": rt, "access_token": at}
        except Exception:
            pass
        return None

    @staticmethod
    def save_active_token(token_info: dict):
        """保存 token 到 ~/.factory/auth.json"""
        try:
            auth_data = {}
            if FACTORY_AUTH_FILE.exists():
                with open(FACTORY_AUTH_FILE, "r", encoding="utf-8") as f:
                    auth_data = json.load(f)
            
            auth_data["access_token"] = token_info.get("access_token", "")
            auth_data["refresh_token"] = token_info.get("refresh_token", "")
            auth_data["id"] = token_info.get("id", "")

            ok = TokenManager.atomic_write_json(FACTORY_AUTH_FILE, auth_data)
            return bool(ok)
        except Exception:
            return False

    @staticmethod
    def refresh_token(rt: str, timeout: float = 30) -> dict | None:
        try:
            resp = requests.post(
                REFRESH_URL,
                data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=timeout,
            )
            try:
                payload = resp.json()
            except Exception:
                return None

            # 不强依赖 resp.ok，调用方会校验字段是否齐全
            return payload
        except Exception:
            return None

    @staticmethod
    def _do_query(access_token: str, timeout: float = 30) -> tuple[float, dict]:
        """内部查询函数，仅用 access_token 查询一次"""
        try:
            resp = requests.get(
                USAGE_URL,
                headers={"Authorization": f"Bearer {access_token}", "User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
            if resp.ok:
                data = resp.json()
                if "usage" in data:
                    usage = data["usage"].get("standard", {})
                    total = usage.get("totalAllowance", 0)
                    used = usage.get("orgTotalTokensUsed", 0)
                    remain = total - used
                    ratio = used / total if total > 0 else 0
                    return ratio, {"total": total, "used": used, "remain": remain}
        except Exception:
            pass
        return -1, {}

    @staticmethod
    def query_usage(access_token: str, refresh_tok: str = None, timeout: float = 30) -> tuple[float, dict, dict | None]:
        """查询额度，失败时尝试刷新 token 重试。返回 (ratio, info, new_tokens)"""
        if access_token:
            ratio, info = TokenManager._do_query(access_token, timeout=timeout)
            if ratio >= 0:
                return ratio, info, None
        
        if refresh_tok:
            result = TokenManager.refresh_token(refresh_tok, timeout=timeout)
            if isinstance(result, dict):
                new_at = (result.get("access_token") or "").strip()
                new_rt = (result.get("refresh_token") or refresh_tok or "").strip()
                if new_at:
                    ratio, info = TokenManager._do_query(new_at, timeout=timeout)
                    if ratio >= 0:
                        return ratio, info, {"access_token": new_at, "refresh_token": new_rt}
        
        return -1, {}, None

    def init_active_token(self):
        """确保 auth.json 中的账号有 id"""
        active = TokenManager.load_active_token()
        if active and not active.get("id"):
            # 优先尝试按 refresh_token 在备用池中匹配，避免生成新 id 导致对不上号
            rt = active.get("refresh_token", "")
            matched_id = None
            if rt:
                for t in TokenManager.load_backup_tokens():
                    if (t.get("refresh_token") or "").strip() == rt:
                        matched_id = t.get("id")
                        break
            active["id"] = matched_id or TokenManager.generate_id()
            TokenManager.save_active_token(active)
            return f"已为当前激活账号生成 ID: {active['id']}"
        return None

    def sync_on_start(self):
        """启动时同步：用 id 判断，如果 auth.json 中的账号在备用池中，更新备用池中的 token"""
        active = TokenManager.load_active_token()
        if not active or not active.get("id"):
            return None
        
        active_id = active["id"]
        tokens = TokenManager.load_backup_tokens()
        updated = False
        
        for t in tokens:
            if t.get("id") == active_id:
                t["refresh_token"] = active.get("refresh_token", "")
                t["access_token"] = active.get("access_token", "")
                updated = True
                message = f"已同步账号 {active_id} 的最新 token 到备用池"
                break
        
        if updated:
            TokenManager.save_backup_tokens(tokens)
            return message
        
        return None

    def sync_active_to_backup(self, active: dict | None = None, ratio: float | None = None):
        """将当前激活账号同步到备用池"""
        active = active or TokenManager.load_active_token()
        if not active or not active.get("id"):
            return

        tokens = TokenManager.load_backup_tokens()

        def apply_usage_fields(t: dict):
            if ratio is None:
                return
            if ratio >= 0:
                t["ratio"] = ratio
                if ratio >= WARN_THRESHOLD:
                    t["status"] = "额度不足"
                else:
                    t["status"] = "active"

        found = False
        for t in tokens:
            if t.get("id") == active["id"]:
                t["refresh_token"] = active.get("refresh_token", "")
                t["access_token"] = active.get("access_token", "")
                t.setdefault("status", "active")
                apply_usage_fields(t)
                found = True
                break

        if not found:
            new_entry = {
                "id": active["id"],
                "refresh_token": active.get("refresh_token", ""),
                "access_token": active.get("access_token", ""),
                "status": "active",
            }
            apply_usage_fields(new_entry)
            tokens.insert(0, new_entry)

        TokenManager.save_backup_tokens(tokens)

    def auto_switch_to_available_account(self, callback=None):
        """自动切换到可用账号"""
        if self._switch_inflight:
            return False
        
        try:
            tokens = TokenManager.load_backup_tokens()
            active = TokenManager.load_active_token()
            active_id = active.get("id") if active else None
            
            # 筛选可用账号（排除当前账号）
            available_tokens = []
            for t in tokens:
                if t.get("id") != active_id:
                    ratio = t.get("ratio", 0)
                    if ratio < WARN_THRESHOLD:  # 额度充足
                        available_tokens.append(t)
            
            if not available_tokens:
                if callback:
                    callback("error", "没有可用的备用账号")
                return False
            
            # 选择额度最充足的账号
            best_token = min(available_tokens, key=lambda t: t.get("ratio", 1))
            token_id = best_token.get("id")
            
            # 执行自动切换
            if self._perform_auto_switch(token_id):
                if callback:
                    callback("success", token_id)
                return True
            else:
                if callback:
                    callback("error", "自动切换失败")
                return False
                
        except Exception as e:
            if callback:
                callback("error", f"自动切换过程出错: {e}")
            return False

    def _perform_auto_switch(self, token_id):
        """执行自动切换到指定账号"""
        if self._switch_inflight:
            return False
        
        self._switch_inflight = True
        
        try:
            tokens = TokenManager.load_backup_tokens()
            backup_token = None
            backup_idx = -1
            
            for i, t in enumerate(tokens):
                if t.get("id") == token_id:
                    backup_token = t
                    backup_idx = i
                    break
            
            if not backup_token:
                return False
            
            # 验证账号可用性
            at = backup_token.get("access_token", "")
            rt = backup_token.get("refresh_token", "")
            ratio, info, new_tokens = TokenManager.query_usage(at, rt)
            
            if ratio < 0 or ratio >= 1.0:
                return False
            
            # 执行切换
            old_active = TokenManager.load_active_token()
            
            if TokenManager.save_active_token(backup_token):
                tokens.pop(backup_idx)
                
                if old_active and old_active.get("id"):
                    found = False
                    for t in tokens:
                        if t.get("id") == old_active["id"]:
                            t["refresh_token"] = old_active.get("refresh_token", "")
                            t["access_token"] = old_active.get("access_token", "")
                            found = True
                            break
                    if not found:
                        old_active["status"] = "active"
                        tokens.insert(0, old_active)
                
                TokenManager.save_backup_tokens(tokens)
                return True
            
            return False
        except Exception as e:
            return False
        finally:
            self._switch_inflight = False