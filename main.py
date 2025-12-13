#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 管理器主入口文件"""

import sys
from gui_main import TokenManagerGUI

def main():
    """主函数"""
    try:
        app = TokenManagerGUI()
        app.run()
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序运行出错: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()