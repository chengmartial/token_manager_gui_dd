# Token 管理器 GUI（重构版）

轻量级 Factory Token 管理工具，基于 tkinter 构建。

## 快速开始

```bash
python main.py
```

（如你用 conda 运行）

```bash
conda run python main.py
```

## 功能特性

- 管理多个 Factory 账号 Token（备用池）
- 一键切换当前账号（写入 `~/.factory/auth.json`）
- 支持批量导入 / 多选删除
- 监控当前账号额度（每 90 秒检查一次；开启监控会立即检查一次）
- 监控 Factory 日志中的“无余额”提示并自动切换账号

## 数据文件

| 文件 | 说明 |
|------|------|
| `~/.factory/auth.json` | 当前激活账号（Factory 客户端使用） |
| `tokens.json` | 备用账号池（程序目录下） |

## 按钮说明

### 当前激活区域

| 按钮 | 功能 |
|------|------|
| 开始监控 | 周期检查当前账号额度（启动时立即检查一次） |
| 启动/停止日志监控 | 监控日志里“无余额”提示，触发自动切换账号 |

### 备用账号操作

| 按钮 | 功能 |
|------|------|
| 刷新列表 | 重新加载备用账号列表 |
| 导入Token | 批量导入账号到备用池 |
| 切换选中 | 将选中账号设为当前使用，原账号移入备用池 |
| 检查选中 | 查询选中账号额度并更新状态（支持多选） |
| 删除选中 | 删除选中的备用账号（支持多选） |

## 导入格式

每行一条：

```
refresh_token----access_token----时间戳
```

（时间戳字段可留空/随意，程序会为导入条目生成 id）

## 额度用完/无余额时如何处理

当 Factory CLI 出现类似提示：

```
Ready for more? Reload your tokens at https://app.factory.ai/settings/billing to keep building.
```

你可以：

1. 打开 GUI
2. 选中一个有额度的账号，点「切换选中」
3. 回到 CLI 直接重试/继续你的操作

如果已开启“日志监控”，检测到该提示后会自动切换账号。

## 注意事项

- 额度 100% 用完的账号无法切换
- 退出程序时会询问是否清空 `auth.json`（默认建议保留）

## 依赖

- Python 3.10+（本项目在 Windows 上使用 tkinter）
- requests
