#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 管理器 GUI - 轻量级 tkinter 版本
架构：
- auth.json: 当前激活的账号（由 Factory 客户端维护）
- tokens.json: 备用账号池
- 每个账号用 id（添加时的时间戳）作为唯一标识
"""

import json
import threading
import time
import os
import sys
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
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
CHECK_INTERVAL = 90


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


def generate_id() -> str:
    """生成唯一 ID（时间戳）"""
    return str(int(time.time() * 1000))


def load_backup_tokens():
    """加载备用账号池"""
    if not TOKENS_FILE.exists():
        save_backup_tokens([])
        return []
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "tokens" in data:
                return data["tokens"]
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_backup_tokens(tokens: list):
    """保存备用账号池"""
    atomic_write_json(TOKENS_FILE, tokens)


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

        ok = atomic_write_json(FACTORY_AUTH_FILE, auth_data)
        return bool(ok)
    except Exception:
        return False


def refresh_token(rt: str) -> dict | None:
    try:
        resp = requests.post(
            REFRESH_URL,
            data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        try:
            payload = resp.json()
        except Exception:
            return None

        # 不强依赖 resp.ok，调用方会校验字段是否齐全
        return payload
    except Exception:
        return None


def _do_query(access_token: str) -> tuple[float, dict]:
    """内部查询函数，仅用 access_token 查询一次"""
    try:
        resp = requests.get(
            USAGE_URL,
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": "Mozilla/5.0"},
            timeout=30,
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


def query_usage(access_token: str, refresh_tok: str = None) -> tuple[float, dict, dict | None]:
    """查询额度，失败时尝试刷新 token 重试。返回 (ratio, info, new_tokens)"""
    if access_token:
        ratio, info = _do_query(access_token)
        if ratio >= 0:
            return ratio, info, None
    
    if refresh_tok:
        result = refresh_token(refresh_tok)
        if isinstance(result, dict):
            new_at = (result.get("access_token") or "").strip()
            new_rt = (result.get("refresh_token") or refresh_tok or "").strip()
            if new_at:
                ratio, info = _do_query(new_at)
                if ratio >= 0:
                    return ratio, info, {"access_token": new_at, "refresh_token": new_rt}
    
    return -1, {}, None


class TokenManagerGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Token 管理器")
        self.root.geometry("480x480")
        self.root.resizable(False, False)

        self._ui_thread_id = threading.get_ident()
        self._active_check_inflight = False
        self._check_all_inflight = False
        self._switch_inflight = False

        self.monitoring = False

        self._build_ui()
        self._init_active_token()
        self._sync_on_start()
        self._refresh_list()
        self._check_active_async(user_initiated=False)
        
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _build_ui(self):
        # 当前激活区域
        active_frame = ttk.LabelFrame(self.root, text="当前激活 (auth.json)", padding=5)
        active_frame.pack(fill=tk.X, padx=5, pady=5)

        self.active_label = ttk.Label(active_frame, text="加载中...", font=("", 10))
        self.active_label.pack(side=tk.LEFT, padx=5)

        self.monitor_btn = ttk.Button(active_frame, text="开始监控", command=self._toggle_monitor, width=10)
        self.monitor_btn.pack(side=tk.RIGHT, padx=2)
        self.check_active_btn = ttk.Button(active_frame, text="检查额度", command=lambda: self._check_active_async(user_initiated=True), width=10)
        self.check_active_btn.pack(side=tk.RIGHT, padx=2)

        # 顶部按钮
        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="刷新列表", command=self._refresh_list, width=10).pack(side=tk.LEFT, padx=2)
        self.check_all_btn = ttk.Button(btn_frame, text="检查全部", command=self._check_all_backup_async, width=10)
        self.check_all_btn.pack(side=tk.LEFT, padx=2)
        self.switch_btn = ttk.Button(btn_frame, text="切换选中", command=self._switch_token_async, width=10)
        self.switch_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="导入Token", command=self._import_tokens, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="删除选中", command=self._delete_token, width=10).pack(side=tk.LEFT, padx=2)

        # 备用账号列表
        list_frame = ttk.LabelFrame(self.root, text="备用账号池 (tokens.json)", padding=5)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        columns = ("idx", "id", "status", "usage")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=8)
        self.tree.heading("idx", text="#")
        self.tree.heading("id", text="账号ID")
        self.tree.heading("status", text="状态")
        self.tree.heading("usage", text="额度")
        self.tree.column("idx", width=30, anchor=tk.CENTER)
        self.tree.column("id", width=130, anchor=tk.CENTER)
        self.tree.column("status", width=80, anchor=tk.CENTER)
        self.tree.column("usage", width=200)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scrollbar.set)

        # 日志区
        log_frame = ttk.LabelFrame(self.root, text="日志", padding=5)
        log_frame.pack(fill=tk.X, padx=5, pady=5)

        self.log_text = tk.Text(log_frame, height=6, state=tk.DISABLED)
        self.log_text.pack(fill=tk.X)

    def _log(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{datetime.now():%H:%M:%S}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _call_ui(self, fn, *args, **kwargs):
        self.root.after(0, lambda: fn(*args, **kwargs))

    def _log_safe(self, msg: str):
        if threading.get_ident() == self._ui_thread_id:
            self._log(msg)
        else:
            self._call_ui(self._log, msg)

    def _init_active_token(self):
        """确保 auth.json 中的账号有 id"""
        active = load_active_token()
        if active and not active.get("id"):
            # 优先尝试按 refresh_token 在备用池中匹配，避免生成新 id 导致对不上号
            rt = active.get("refresh_token", "")
            matched_id = None
            if rt:
                for t in load_backup_tokens():
                    if (t.get("refresh_token") or "").strip() == rt:
                        matched_id = t.get("id")
                        break
            active["id"] = matched_id or generate_id()
            save_active_token(active)
            self._log(f"已为当前激活账号生成 ID: {active['id']}")

    def _sync_on_start(self):
        """启动时同步：用 id 判断，如果 auth.json 中的账号在备用池中，更新备用池中的 token"""
        active = load_active_token()
        if not active or not active.get("id"):
            return
        
        active_id = active["id"]
        tokens = load_backup_tokens()
        updated = False
        
        for t in tokens:
            if t.get("id") == active_id:
                t["refresh_token"] = active.get("refresh_token", "")
                t["access_token"] = active.get("access_token", "")
                updated = True
                self._log(f"已同步账号 {active_id} 的最新 token 到备用池")
                break
        
        if updated:
            save_backup_tokens(tokens)

    def _sync_active_to_backup(self, active: dict | None = None):
        active = active or load_active_token()
        if not active or not active.get("id"):
            return

        tokens = load_backup_tokens()
        found = False
        for t in tokens:
            if t.get("id") == active["id"]:
                t["refresh_token"] = active.get("refresh_token", "")
                t["access_token"] = active.get("access_token", "")
                t.setdefault("status", "active")
                found = True
                break

        if not found:
            tokens.insert(0, {
                "id": active["id"],
                "refresh_token": active.get("refresh_token", ""),
                "access_token": active.get("access_token", ""),
                "status": "active",
            })

        save_backup_tokens(tokens)

    def _on_closing(self):
        """退出时同步 auth.json 到 tokens.json，并可选清空 auth.json。"""
        self.monitoring = False

        active = load_active_token()
        try:
            self._sync_active_to_backup(active)
        except Exception:
            pass

        # 是否清空 auth.json 交给用户选择（默认保留）
        choice = messagebox.askyesnocancel(
            "退出",
            "退出前是否清空 auth.json？\n\n"
            "- 选择『是』：清空 auth.json（Factory 会视为未登录/无 token）\n"
            "- 选择『否』：保留 auth.json（建议）\n"
            "- 选择『取消』：返回程序",
        )
        if choice is None:
            return
        if choice is True:
            if not atomic_write_json(FACTORY_AUTH_FILE, {}):
                self._log_safe("清空 auth.json 失败")

        self.root.destroy()

    def _refresh_list(self):
        self._update_active_display()

        for item in self.tree.get_children():
            self.tree.delete(item)

        tokens = load_backup_tokens()
        active = load_active_token()
        active_id = active.get("id") if active else None
        
        display_idx = 0
        for i, t in enumerate(tokens):
            if t.get("id") == active_id:
                continue
            display_idx += 1
            status = t.get("status", "active")
            ratio = t.get("ratio")
            if ratio is not None and ratio >= 0:
                remain_ratio = 1 - ratio
                usage_str = f"已用：{ratio:.1%}，剩余：{remain_ratio:.1%}"
            elif ratio == -1:
                usage_str = "查询失败"
            else:
                usage_str = "未查询"
            token_id = t.get("id", "无ID")
            self.tree.insert("", tk.END, values=(display_idx, token_id, status, usage_str))

        self._log(f"列表已刷新，备用账号: {len(tokens)} 个")

    def _update_active_display(self):
        """更新当前激活账号显示"""
        active = load_active_token()
        if active:
            token_id = active.get("id", "无ID")
            self.active_label.config(text=f"ID: {token_id} (点击'检查额度'查看)")
        else:
            self.active_label.config(text="未找到 auth.json")

    def _check_active_async(self, user_initiated: bool = True):
        """异步检查当前激活账号额度（避免阻塞 UI）。"""
        if self._active_check_inflight:
            return

        active = load_active_token()
        if not active:
            self._log_safe("未找到 auth.json")
            return

        self._active_check_inflight = True
        token_id = active.get("id", "无ID")
        at = active.get("access_token", "")
        rt = active.get("refresh_token", "")

        self.active_label.config(text=f"ID: {token_id} | 查询中...")

        def worker(active_snapshot: dict):
            try:
                ratio, info, new_tokens = query_usage(at, rt)

                if new_tokens:
                    active_snapshot["access_token"] = new_tokens.get("access_token", "")
                    active_snapshot["refresh_token"] = new_tokens.get("refresh_token", "")
                    if save_active_token(active_snapshot):
                        self._log_safe("已刷新并保存 token")
                    else:
                        self._log_safe("已刷新 token，但写入 auth.json 失败")

                def update_ui():
                    try:
                        if ratio >= 0:
                            remain_ratio = 1 - ratio
                            self._log(f"[{token_id}] 已用：{ratio:.1%}，剩余：{remain_ratio:.1%}")
                            self.active_label.config(text=f"ID: {token_id} | 已用：{ratio:.1%} | 剩余：{remain_ratio:.1%}")
                            if user_initiated and ratio >= 0.99:
                                messagebox.showwarning("额度用尽", "当前账号额度已用完！\n请切换到备用账号。")
                        else:
                            self._log(f"[{token_id}] 查询失败")
                            self.active_label.config(text=f"ID: {token_id} | 查询失败")
                    finally:
                        self._active_check_inflight = False

                self._call_ui(update_ui)
            except Exception:
                def update_fail():
                    try:
                        self._log(f"[{token_id}] 查询失败")
                        self.active_label.config(text=f"ID: {token_id} | 查询失败")
                    finally:
                        self._active_check_inflight = False

                self._call_ui(update_fail)

        threading.Thread(target=worker, args=(active,), daemon=True).start()

    def _check_all_backup_async(self):
        """异步一键检查所有备用账号额度（避免阻塞 UI）。"""
        if self._check_all_inflight:
            return

        tokens = load_backup_tokens()
        if not tokens:
            self._log("备用池为空")
            return

        self._check_all_inflight = True
        self.check_all_btn.config(state=tk.DISABLED)
        self._log(f"开始检查 {len(tokens)} 个备用账号...")

        active = load_active_token()
        active_id = active.get("id") if active else None

        def worker(tokens_snapshot: list):
            updated = False
            rows = []
            display_idx = 0
            try:
                for t in tokens_snapshot:
                    if t.get("id") == active_id:
                        continue
                    display_idx += 1
                    token_id = t.get("id", "无ID")
                    at = t.get("access_token", "")
                    rt = t.get("refresh_token", "")
                    status = t.get("status", "active")

                    ratio, info, new_tokens = query_usage(at, rt)

                    if new_tokens:
                        t["access_token"] = new_tokens.get("access_token", "")
                        t["refresh_token"] = new_tokens.get("refresh_token", "")
                        updated = True

                    if ratio >= 0:
                        remain_ratio = 1 - ratio
                        usage_str = f"已用：{ratio:.1%}，剩余：{remain_ratio:.1%}"
                        t["ratio"] = ratio
                        updated = True
                        if ratio >= WARN_THRESHOLD:
                            status = "额度不足"
                            t["status"] = status
                    else:
                        usage_str = "查询失败"
                        status = "失效"
                        t["status"] = status
                        t["ratio"] = -1
                        updated = True

                    rows.append((display_idx, token_id, status, usage_str))

                if updated:
                    save_backup_tokens(tokens_snapshot)

                def update_ui():
                    try:
                        for item in self.tree.get_children():
                            self.tree.delete(item)
                        for r in rows:
                            self.tree.insert("", tk.END, values=r)
                        self._log("备用账号检查完成")
                    finally:
                        self._check_all_inflight = False
                        self.check_all_btn.config(state=tk.NORMAL)

                self._call_ui(update_ui)
            except Exception:
                def update_fail():
                    try:
                        self._log("备用账号检查失败")
                    finally:
                        self._check_all_inflight = False
                        self.check_all_btn.config(state=tk.NORMAL)

                self._call_ui(update_fail)

        threading.Thread(target=worker, args=(tokens,), daemon=True).start()

    def _get_selected_idx(self):
        token_id = self._get_selected_token_id()
        if token_id is None:
            return None
        for i, t in enumerate(load_backup_tokens()):
            if str(t.get("id", "")) == token_id:
                return i
        return None

    def _get_selected_token_id(self):
        """获取选中的账号 ID"""
        sel = self.tree.selection()
        if not sel:
            return None
        return str(self.tree.item(sel[0])["values"][1])

    def _switch_token_async(self):
        """异步切换选中的备用账号到 auth.json（避免额度查询阻塞 UI）。"""
        if self._switch_inflight:
            return

        token_id = self._get_selected_token_id()
        if token_id is None:
            messagebox.showinfo("提示", "请先选择要切换的账号")
            return

        tokens = load_backup_tokens()
        backup_token = None
        backup_idx = -1
        for i, t in enumerate(tokens):
            if t.get("id") == token_id:
                backup_token = t
                backup_idx = i
                break
        
        if backup_token is None:
            return

        self._switch_inflight = True
        self.switch_btn.config(state=tk.DISABLED)
        self._log(f"[{token_id}] 查询额度中...")

        at = backup_token.get("access_token", "")
        rt = backup_token.get("refresh_token", "")

        def worker(tokens_snapshot: list, token_snapshot: dict, token_index: int):
            ratio, info, new_tokens = query_usage(at, rt)

            if new_tokens:
                token_snapshot["access_token"] = new_tokens.get("access_token", "")
                token_snapshot["refresh_token"] = new_tokens.get("refresh_token", "")
                try:
                    save_backup_tokens(tokens_snapshot)
                except Exception:
                    pass

            def continue_ui():
                try:
                    if ratio < 0:
                        messagebox.showerror("错误", f"账号 [{token_id}] 查询额度失败，无法切换")
                        return
                    if ratio >= 1.0:
                        messagebox.showerror("错误", f"账号 [{token_id}] 额度已用完 ({ratio:.1%})，无法切换")
                        return

                    remain_ratio = 1 - ratio
                    if not messagebox.askyesno(
                        "确认切换",
                        f"是否切换到账号 [{token_id}]？\n已用：{ratio:.1%}，剩余：{remain_ratio:.1%}\n\n"
                        f"当前 auth.json 中的账号将移入备用池。",
                    ):
                        return

                    old_active = load_active_token()
                    tokens2 = load_backup_tokens()
                    backup_token2 = None
                    backup_idx2 = -1
                    for i2, t2 in enumerate(tokens2):
                        if t2.get("id") == token_id:
                            backup_token2 = t2
                            backup_idx2 = i2
                            break

                    if backup_token2 is None:
                        self._log(f"切换失败：未在备用池找到 [{token_id}]")
                        return

                    if save_active_token(backup_token2):
                        tokens2.pop(backup_idx2)

                        if old_active and old_active.get("id"):
                            found = False
                            for t2 in tokens2:
                                if t2.get("id") == old_active["id"]:
                                    t2["refresh_token"] = old_active.get("refresh_token", "")
                                    t2["access_token"] = old_active.get("access_token", "")
                                    found = True
                                    break
                            if not found:
                                old_active["status"] = "active"
                                tokens2.insert(0, old_active)

                        save_backup_tokens(tokens2)
                        self._log(f"已切换到 [{token_id}]")
                        self._refresh_list()
                        self._check_active_async(user_initiated=False)
                    else:
                        self._log("切换失败：无法写入 auth.json")
                finally:
                    self._switch_inflight = False
                    self.switch_btn.config(state=tk.NORMAL)

            self._call_ui(continue_ui)

        threading.Thread(target=worker, args=(tokens, backup_token, backup_idx), daemon=True).start()

    def _delete_token(self):
        token_id = self._get_selected_token_id()
        if token_id is None:
            messagebox.showinfo("提示", "请先选择要删除的备用账号")
            return

        if not messagebox.askyesno("确认", f"删除账号 [{token_id}]？"):
            return

        tokens = load_backup_tokens()
        tokens = [t for t in tokens if t.get("id") != token_id]
        save_backup_tokens(tokens)
        self._log(f"已删除: {token_id}")
        self._refresh_list()

    def _import_tokens(self):
        """导入Token，支持单条或多条，格式: refresh_token----access_token----时间戳"""
        win = tk.Toplevel(self.root)
        win.title("导入Token")
        win.geometry("500x250")
        win.transient(self.root)

        ttk.Label(win, text="每行一条，格式: refresh_token----access_token----时间戳").pack(pady=5)
        text = tk.Text(win, height=10, width=60)
        text.pack(padx=10, pady=5)

        def do_import():
            lines = text.get("1.0", tk.END).strip().split("\n")
            tokens = load_backup_tokens()
            added, skipped = 0, 0
            base_ts = int(time.time() * 1000)
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("----")
                if len(parts) >= 2:
                    rt, at = parts[0].strip(), parts[1].strip()
                    if rt and not any((t.get("refresh_token") or "").strip() == rt for t in tokens):
                        tokens.append({"id": str(base_ts + added), "refresh_token": rt, "access_token": at, "status": "active"})
                        added += 1
                    elif rt:
                        skipped += 1
            save_backup_tokens(tokens)
            msg = f"导入完成，新增 {added} 条"
            if skipped:
                msg += f"，跳过 {skipped} 条重复"
            self._log(msg)
            win.destroy()
            self._refresh_list()

        ttk.Button(win, text="导入", command=do_import).pack(pady=10)

    def _toggle_monitor(self):
        if self.monitoring:
            self.monitoring = False
            self.monitor_btn.config(text="开始监控")
            self._log("监控已停止")
        else:
            self.monitoring = True
            self.monitor_btn.config(text="停止监控")
            if CHECK_INTERVAL < 120:
                self._log(f"监控已启动 (每 {CHECK_INTERVAL} 秒)")
            else:
                self._log(f"监控已启动 (每 {CHECK_INTERVAL / 60:.1f} 分钟)")
            self._monitor_tick()

    def _monitor_tick(self):
        if not self.monitoring:
            return
        self._check_active_async(user_initiated=False)
        self.root.after(int(CHECK_INTERVAL * 1000), self._monitor_tick)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    TokenManagerGUI().run()
