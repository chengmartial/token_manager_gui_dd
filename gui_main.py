#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 主界面模块"""

import os
import sys
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

from token_manager import TokenManager
from log_monitor import LogMonitor, CLIPromptHandler

# 导入必要的常量
from token_manager import WARN_THRESHOLD

# exe 运行时用 exe 所在目录，否则用脚本目录
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

LOCK_FILE = BASE_DIR / ".token_manager.lock"
CHECK_INTERVAL = 90


class TokenManagerGUI:
    """Token 管器 GUI 主类"""
    
    def __init__(self):
        # 单实例检查：尝试创建并独占锁文件
        self._lock_file = None
        try:
            # Windows下尝试以独占模式打开，如果失败说明已有实例在运行
            if sys.platform == 'win32':
                import msvcrt
                try:
                    self._lock_file = open(LOCK_FILE, 'w')
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    self._lock_file.write(str(os.getpid()))
                    self._lock_file.flush()
                except (IOError, OSError):
                    if self._lock_file:
                        self._lock_file.close()
                    messagebox.showerror("错误", "Token 管理器已在运行！\n请勿重复启动。")
                    sys.exit(0)
            else:
                # Unix/Linux使用fcntl
                import fcntl
                try:
                    self._lock_file = open(LOCK_FILE, 'w')
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._lock_file.write(str(os.getpid()))
                    self._lock_file.flush()
                except (IOError, OSError):
                    if self._lock_file:
                        self._lock_file.close()
                    messagebox.showerror("错误", "Token 管理器已在运行！\n请勿重复启动。")
                    sys.exit(0)
        except Exception as e:
            messagebox.showerror("错误", f"单实例检查失败：{str(e)}")
            sys.exit(0)

        self.root = tk.Tk()
        self.root.title("Token 管理器")
        # 两行按钮布局会占用更多垂直空间；同时放宽宽度以避免按钮被挤压
        self.root.geometry("520x560")
        self.root.minsize(520, 560)
        self.root.resizable(True, False)

        self._ui_thread_id = threading.get_ident()
        self._active_check_inflight = False
        self._check_all_inflight = False
        self._check_selected_inflight = False
        self._switch_inflight = False
        
        # 初始化核心组件
        self.token_manager = TokenManager()
        self.log_monitor = LogMonitor(callback=self._log_monitor_callback)
        self.cli_prompt = CLIPromptHandler(callback=self._cli_callback)
        
        self.monitoring = False
        
        # 在控制台显示启动信息
        print("=" * 60)
        print("Token 管理器已启动")
        print("功能: 自动检测付款错误并切换账号")
        print("当检测到付款问题时，会在命令行提示输入 '继续'")
        print("=" * 60)

        self._build_ui()
        self._init_active_token()
        self._sync_on_start()
        self._refresh_list()
        self._check_active_async(user_initiated=False)
        
        # 自动启动日志监控
        self.log_monitor.start_monitoring()
        
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _log_monitor_callback(self, event_type, message):
        """日志监控回调"""
        if event_type == "log":
            self._log_safe(message)
        elif event_type == "payment_error":
            self._log_safe(message)
            self.token_manager.auto_switch_to_available_account(callback=self._auto_switch_callback)
    
    def _cli_callback(self, event_type, data):
        """CLI 回调"""
        if event_type == "continue_confirmed":
            self._log_safe("用户确认继续工作")
        elif event_type == "continue_cancelled":
            self._log_safe("用户取消继续")
        elif event_type == "continue_interrupted":
            self._log_safe("用户中断操作")
        elif event_type == "continue_error":
            self._log_safe(f"CLI输入错误: {data}")
        elif event_type == "show_notification":
            self._show_switch_notification(data)
    
    def _auto_switch_callback(self, status, data):
        """自动切换回调"""
        if status == "success":
            token_id = data
            self._log_safe(f"✅ 已自动切换到账号 [{token_id}]")
            # 注意：该回调可能来自日志监控线程，涉及 Tk 的操作必须切回 UI 线程
            self._call_ui(self._prompt_user_continue, token_id)
            self._call_ui(self._refresh_list)
            self._call_ui(self._check_active_async, False)
        elif status == "error":
            self._log_safe(f"❌ {data}")
            self._call_ui(self._show_error_notification)
    
    def _build_ui(self):
        # 当前激活区域
        active_frame = ttk.LabelFrame(self.root, text="当前激活 (auth.json)", padding=5)
        active_frame.pack(fill=tk.X, padx=5, pady=5)

        self.active_label = ttk.Label(active_frame, text="加载中...", font=("", 10))
        self.active_label.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        self.log_monitor_btn = ttk.Button(active_frame, text="启动日志监控", command=self._toggle_log_monitor, width=12)
        self.log_monitor_btn.pack(side=tk.RIGHT, padx=2)
        self.monitor_btn = ttk.Button(active_frame, text="开始监控", command=self._toggle_monitor, width=10)
        self.monitor_btn.pack(side=tk.RIGHT, padx=2)

        # 顶部按钮
        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill=tk.X)

        # 一行按钮在 480/520 宽度下会被挤压，拆成两行展示
        btn_row1 = ttk.Frame(btn_frame)
        btn_row1.pack(fill=tk.X)
        btn_row2 = ttk.Frame(btn_frame)
        btn_row2.pack(fill=tk.X, pady=(4, 0))

        ttk.Button(btn_row1, text="刷新列表", command=self._refresh_list, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row1, text="导入Token", command=self._import_tokens, width=10).pack(side=tk.LEFT, padx=2)

        self.switch_btn = ttk.Button(btn_row2, text="切换选中", command=self._switch_token_async, width=10)
        self.switch_btn.pack(side=tk.LEFT, padx=2)
        self.check_selected_btn = ttk.Button(btn_row2, text="检查选中", command=self._check_selected_async, width=10)
        self.check_selected_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row2, text="删除选中", command=self._delete_tokens, width=10).pack(side=tk.LEFT, padx=2)

        # 备用账号列表
        list_frame = ttk.LabelFrame(self.root, text="备用账号池 (tokens.json)", padding=5)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        columns = ("idx", "id", "status", "usage")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=8, selectmode="extended")
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
        message = self.token_manager.init_active_token()
        if message:
            self._log(message)

    def _sync_on_start(self):
        """启动时同步"""
        message = self.token_manager.sync_on_start()
        if message:
            self._log(message)

    def _on_closing(self):
        """退出时处理"""
        self.monitoring = False
        self.log_monitor.stop_monitoring()

        active = self.token_manager.load_active_token()
        try:
            # 获取最后的额度信息
            at = active.get("access_token", "") if active else ""
            rt = active.get("refresh_token", "") if active else ""
            ratio, info, new_tokens = self.token_manager.query_usage(at, rt, timeout=5)
            
            if new_tokens and active:
                active["access_token"] = new_tokens.get("access_token", "")
                active["refresh_token"] = new_tokens.get("refresh_token", "")
                self.token_manager.save_active_token(active)
            
            self.token_manager.sync_active_to_backup(active, ratio if ratio >= 0 else None)
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
            if not TokenManager.atomic_write_json(Path(os.path.expanduser("~")) / ".factory" / "auth.json", {}):
                self._log_safe("清空 auth.json 失败")

        # 释放锁文件
        try:
            if self._lock_file:
                self._lock_file.close()
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        self.root.destroy()

    def _refresh_list(self):
        self._update_active_display()

        for item in self.tree.get_children():
            self.tree.delete(item)

        tokens = self.token_manager.load_backup_tokens()
        active = self.token_manager.load_active_token()
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
        active = self.token_manager.load_active_token()
        if active:
            token_id = active.get("id", "无ID")
            self.active_label.config(text=f"ID: {token_id} (开启监控会自动查询)")
        else:
            self.active_label.config(text="未找到 auth.json")

    def _check_active_async(self, user_initiated: bool = True):
        """异步检查当前激活账号额度"""
        if self._active_check_inflight:
            return

        active = self.token_manager.load_active_token()
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
                ratio, info, new_tokens = self.token_manager.query_usage(at, rt)

                if new_tokens:
                    active_snapshot["access_token"] = new_tokens.get("access_token", "")
                    active_snapshot["refresh_token"] = new_tokens.get("refresh_token", "")
                    if self.token_manager.save_active_token(active_snapshot):
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
        """异步一键检查所有备用账号额度"""
        if self._check_all_inflight:
            return

        tokens = self.token_manager.load_backup_tokens()
        if not tokens:
            self._log("备用池为空")
            return

        self._check_all_inflight = True
        self._log(f"开始检查 {len(tokens)} 个备用账号...")

        active = self.token_manager.load_active_token()
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

                    ratio, info, new_tokens = self.token_manager.query_usage(at, rt)

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
                    self.token_manager.save_backup_tokens(tokens_snapshot)

                def update_ui():
                    try:
                        for item in self.tree.get_children():
                            self.tree.delete(item)
                        for r in rows:
                            self.tree.insert("", tk.END, values=r)
                        self._log("备用账号检查完成")
                    finally:
                        self._check_all_inflight = False

                self._call_ui(update_ui)
            except Exception:
                def update_fail():
                    try:
                        self._log("备用账号检查失败")
                    finally:
                        self._check_all_inflight = False

                self._call_ui(update_fail)

        threading.Thread(target=worker, args=(tokens,), daemon=True).start()

    def _get_selected_idx(self):
        token_id = self._get_selected_token_id()
        if token_id is None:
            return None
        for i, t in enumerate(self.token_manager.load_backup_tokens()):
            if str(t.get("id", "")) == token_id:
                return i
        return None

    def _get_selected_token_id(self):
        """获取选中的账号 ID"""
        sel = self.tree.selection()
        if not sel:
            return None
        return str(self.tree.item(sel[0])["values"][1])

    def _get_selected_token_ids(self) -> list[str]:
        """获取选中的账号 ID 列表（支持多选）"""
        selected_items = self.tree.selection()
        token_ids: list[str] = []
        for item in selected_items:
            values = self.tree.item(item).get("values") or []
            if len(values) >= 2:
                token_ids.append(str(values[1]))
        return token_ids

    def _check_selected_async(self):
        """异步检查选中账号的额度（支持多选）。"""
        if self._check_selected_inflight:
            return

        token_ids = self._get_selected_token_ids()
        if not token_ids:
            messagebox.showinfo("提示", "请先选择要检查的账号")
            return

        self._check_selected_inflight = True
        self.check_selected_btn.config(state=tk.DISABLED)
        self._log(f"开始检查选中账号：{len(token_ids)} 个...")

        def worker(token_ids_snapshot: list[str]):
            updated = False
            try:
                tokens = self.token_manager.load_backup_tokens()
                tokens_by_id = {str(t.get("id", "")): t for t in tokens}

                for token_id in token_ids_snapshot:
                    t = tokens_by_id.get(str(token_id))
                    if not t:
                        continue

                    at = t.get("access_token", "")
                    rt = t.get("refresh_token", "")
                    ratio, info, new_tokens = self.token_manager.query_usage(at, rt)

                    if new_tokens:
                        t["access_token"] = new_tokens.get("access_token", "")
                        t["refresh_token"] = new_tokens.get("refresh_token", "")
                        updated = True

                    if ratio >= 0:
                        t["ratio"] = ratio
                        t["status"] = "额度不足" if ratio >= WARN_THRESHOLD else "active"
                        updated = True
                        self._log_safe(f"[{token_id}] 已用：{ratio:.1%}")
                    else:
                        t["ratio"] = -1
                        t["status"] = "失效"
                        updated = True
                        self._log_safe(f"[{token_id}] 查询失败")

                if updated:
                    self.token_manager.save_backup_tokens(tokens)

                def update_ui():
                    try:
                        self._refresh_list()
                        self._log("选中账号检查完成")
                    finally:
                        self._check_selected_inflight = False
                        self.check_selected_btn.config(state=tk.NORMAL)

                self._call_ui(update_ui)
            except Exception:
                def update_fail():
                    try:
                        self._log("选中账号检查失败")
                    finally:
                        self._check_selected_inflight = False
                        self.check_selected_btn.config(state=tk.NORMAL)

                self._call_ui(update_fail)

        threading.Thread(target=worker, args=(token_ids,), daemon=True).start()

    def _switch_token_async(self):
        """异步切换选中的备用账号到 auth.json"""
        if self._switch_inflight or self._check_all_inflight or self._check_selected_inflight:
            return

        token_id = self._get_selected_token_id()
        if token_id is None:
            messagebox.showinfo("提示", "请先选择要切换的账号")
            return

        tokens = self.token_manager.load_backup_tokens()
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
            ratio, info, new_tokens = self.token_manager.query_usage(at, rt)

            if new_tokens:
                token_snapshot["access_token"] = new_tokens.get("access_token", "")
                token_snapshot["refresh_token"] = new_tokens.get("refresh_token", "")
                try:
                    self.token_manager.save_backup_tokens(tokens_snapshot)
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

                    old_active = self.token_manager.load_active_token()
                    tokens2 = self.token_manager.load_backup_tokens()
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

                    if self.token_manager.save_active_token(backup_token2):
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

                        self.token_manager.save_backup_tokens(tokens2)
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

    def _delete_tokens(self):
        """删除选中的账号（支持多选）"""
        selected_items = self.tree.selection()
        if not selected_items:
            messagebox.showinfo("提示", "请先选择要删除的备用账号")
            return
        
        selected_ids = []
        for item in selected_items:
            token_id = str(self.tree.item(item)["values"][1])
            selected_ids.append(token_id)
        
        if len(selected_ids) == 1:
            confirm_msg = f"删除账号 [{selected_ids[0]}]？"
        else:
            confirm_msg = f"删除选中的 {len(selected_ids)} 个账号？"
        
        if not messagebox.askyesno("确认", confirm_msg):
            return
        
        tokens = self.token_manager.load_backup_tokens()
        tokens = [t for t in tokens if t.get("id") not in selected_ids]
        self.token_manager.save_backup_tokens(tokens)
        
        if len(selected_ids) == 1:
            self._log(f"已删除: {selected_ids[0]}")
        else:
            self._log(f"已删除 {len(selected_ids)} 个账号")
        
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
            tokens = self.token_manager.load_backup_tokens()
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
            self.token_manager.save_backup_tokens(tokens)
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
            # 作为“手动检查额度”的替代：开启监控时立刻查询一次
            self._check_active_async(user_initiated=True)
            self._monitor_tick()

    def _monitor_tick(self):
        if not self.monitoring:
            return
        self._check_active_async(user_initiated=False)
        self.root.after(int(CHECK_INTERVAL * 1000), self._monitor_tick)

    def _toggle_log_monitor(self):
        """切换日志监控状态"""
        if self.log_monitor.is_monitoring():
            self.log_monitor.stop_monitoring()
            self.log_monitor_btn.config(text="启动日志监控")
        else:
            self.log_monitor.start_monitoring()
            self.log_monitor_btn.config(text="停止日志监控")

    def _prompt_user_continue(self, token_id):
        """提示用户在CLI中继续工作"""
        def show_notification():
            # 创建简单的通知窗口，不要求输入
            notification_window = tk.Toplevel(self.root)
            notification_window.title("账号已切换")
            notification_window.geometry("400x120")
            notification_window.transient(self.root)
            
            # 设置窗口始终在最前面
            notification_window.attributes('-topmost', True)
            
            ttk.Label(notification_window, text="💰 检测到付款错误", font=("", 12, "bold")).pack(pady=10)
            ttk.Label(notification_window, text=f"已自动切换到账号: [{token_id}]", font=("", 10)).pack(pady=5)
            ttk.Label(notification_window, text="请在命令行中输入 '继续' 以继续工作", font=("", 10, "italic")).pack(pady=5)
            
            # 10秒后自动关闭
            notification_window.after(10000, notification_window.destroy)
            
            # 同时在GUI日志中显示提示
            self._log_safe("=" * 50)
            self._log_safe("💰 检测到付款错误，已自动切换账号")
            self._log_safe(f"新账号: [{token_id}]")
            self._log_safe("请在命令行中输入 '继续' 以继续工作")
            self._log_safe("=" * 50)
        
        self._call_ui(show_notification)
        
        # 在CLI中等待用户输入
        self.cli_prompt.prompt_user_continue(token_id)

    def _show_error_notification(self):
        """显示错误通知"""
        def show_error():
            error_window = tk.Toplevel(self.root)
            error_window.title("切换失败")
            error_window.geometry("400x100")
            error_window.transient(self.root)
            error_window.attributes('-topmost', True)
            
            ttk.Label(error_window, text="❌ 自动切换失败", font=("", 12, "bold")).pack(pady=10)
            ttk.Label(error_window, text="请手动切换账号或充值", font=("", 10)).pack(pady=5)
            
            error_window.after(5000, error_window.destroy)
        
        self._call_ui(show_error)
        self.cli_prompt.show_error_message("自动切换失败")

    def _show_switch_notification(self, token_id):
        """显示账号切换通知"""
        def show_notification():
            # 创建简单的通知窗口
            notification_window = tk.Toplevel(self.root)
            notification_window.title("账号已切换")
            notification_window.geometry("400x120")
            notification_window.transient(self.root)
            
            # 设置窗口始终在最前面
            notification_window.attributes('-topmost', True)
            
            ttk.Label(notification_window, text="💰 当前账号无余额", font=("", 12, "bold")).pack(pady=10)
            ttk.Label(notification_window, text=f"已切换到有余额账号: [{token_id}]", font=("", 10)).pack(pady=5)
            ttk.Label(notification_window, text="可以继续工作", font=("", 10, "italic")).pack(pady=5)
            
            # 5秒后自动关闭
            notification_window.after(5000, notification_window.destroy)
        
        self._call_ui(show_notification)

    def run(self):
        self.root.mainloop()