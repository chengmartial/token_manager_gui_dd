#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志监控模块"""

import os
import re
import time
import threading
import glob
from pathlib import Path


class LogMonitor:
    """日志监控类"""
    
    def __init__(self, callback=None):
        self.callback = callback
        self.monitoring = False
        self.monitor_thread = None
        self.payment_error_pattern = re.compile(
            r'Ready for more\? Reload your tokens.*?https://app\.factory\.ai/settings/billing'
        )
        self.log_file_positions = {}
    
    def find_droid_log_files(self):
        """查找 Droid 客户端日志文件"""
        possible_paths = [
            'C:/Users/Administrator/.factory/logs/*.log'  # 用户指定的实际日志位置
        ]
        
        found_logs = []
        for path in possible_paths:
            if '*' in path:
                expanded_path = os.path.expanduser(path)
                found_logs.extend(glob.glob(expanded_path))
            else:
                expanded_path = os.path.expanduser(path)
                if os.path.exists(expanded_path):
                    found_logs.append(expanded_path)
        
        return found_logs

    def start_monitoring(self):
        """启动日志监控"""
        if self.monitoring:
            return
        
        self.monitoring = True
        self.monitor_thread = threading.Thread(target=self._monitor_logs_worker, daemon=True)
        self.monitor_thread.start()
        
        if self.callback:
            self.callback("log", "日志监控已启动")

    def stop_monitoring(self):
        """停止日志监控"""
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1)
        
        if self.callback:
            self.callback("log", "日志监控已停止")

    def _monitor_logs_worker(self):
        """日志监控工作线程"""
        log_files = self.find_droid_log_files()
        
        if not log_files:
            if self.callback:
                self.callback("log", "未找到 Droid 日志文件")
            return
        
        if self.callback:
            self.callback("log", f"找到日志文件: {log_files}")
        
        # 初始化文件位置
        for log_file in log_files:
            try:
                self.log_file_positions[log_file] = os.path.getsize(log_file)
            except:
                self.log_file_positions[log_file] = 0
        
        while self.monitoring:
            try:
                for log_file in log_files:
                    self._check_log_updates(log_file)
                time.sleep(1)  # 每秒检查一次
            except Exception as e:
                if self.callback:
                    self.callback("log", f"日志监控出错: {e}")
                time.sleep(5)

    def _check_log_updates(self, log_file):
        """检查单个日志文件的更新"""
        try:
            current_size = os.path.getsize(log_file)
            last_size = self.log_file_positions.get(log_file, 0)
            
            if current_size > last_size:
                with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    f.seek(last_size)
                    new_content = f.read()
                    
                    if self.payment_error_pattern.search(new_content):
                        if self.callback:
                            self.callback("payment_error", "检测到账号无余额，正在自动切换账号...")
                
                self.log_file_positions[log_file] = current_size
        except Exception as e:
            if self.callback:
                self.callback("log", f"检查日志文件 {log_file} 出错: {e}")

    def is_monitoring(self):
        """返回监控状态"""
        return self.monitoring


class CLIPromptHandler:
    """CLI 提示处理器"""
    
    def __init__(self, callback=None):
        self.callback = callback
    
    def prompt_user_continue(self, token_id):
        """提示用户账号已切换完成"""
        print("\n" + "=" * 60)
        print(f"💰 当前账号无余额")
        print(f"✅ 已自动切换到有余额账号: [{token_id}]")
        print("账号切换完成，可以继续工作")
        print("=" * 60)
        
        # 通知切换完成，同时显示弹窗提醒
        if self.callback:
            self.callback("continue_confirmed", token_id)
            self.callback("show_notification", token_id)
    
    def show_error_message(self, message):
        """显示错误消息"""
        print("\n" + "=" * 60)
        print(f"❌ {message}")
        print("请手动切换账号或充值")
        print("访问: https://app.factory.ai/settings/billing")
        print("=" * 60)