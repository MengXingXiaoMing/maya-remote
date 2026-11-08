# maya-remote

Windows 上从外部驱动 Autodesk Maya 的技能（Trae / DSH 通用）：启动指定版本、打开命令端口、执行 Python、截图诊断、**点掉模态弹窗**，以及崩溃/卡死后的自动重启。

给"让 agent 自己跑一晚上"准备的：Maya 挂了会自己拉起来，卡在弹窗上会自己点掉，而不是一律杀掉重来。

## 能力

| 能力 | 命令 |
| --- | --- |
| 检测已安装 / 正在运行的 Maya | `maya_ctl.py detect` |
| 启动、等待端口就绪 | `maya_ctl.py ensure [--version 2025] [--scene x.mb]` |
| 在 Maya 里跑 Python | `maya_ctl.py exec --code "..."` / `--file test.py` |
| 状态 / 连通性 / 重启 / 停止 | `status` `ping` `restart` `kill` |
| 截图取证（窗口 + 全屏） | `diagnose [--full-screen]` |
| 列出窗口与可点控件 | `targets --pid <pid>` |
| 带坐标网格的窗口截图 | `grid --pid <pid> --title "Security Warning"` |
| 点掉弹窗（控件 / 坐标） | `click --pid <pid> --text Allow` / `--at 147,331` |
| 崩溃 / 卡死自动重启 | `watchdog --start` / `--status` / `--stop` |

所有命令输出一个 JSON 对象；退出码 0 表示 `ok: true`。

## 安装

把整个文件夹放到技能目录，并确保已在技能配置里注册：

```
~/.trae-cn/skills/maya-remote      # Trae
~/.dsh/skills/maya-remote          # DSH（需要开启 skill-filesystem）
```

用任意 Python 3 调用 `scripts/maya_ctl.py` 即可，不依赖第三方库。

## 最小示例

```bash
python scripts/maya_ctl.py ensure --version 2025 --timeout 300
python scripts/maya_ctl.py exec --code "import maya.cmds as cmds
result = {'nodes': len(cmds.ls())}"
python scripts/maya_ctl.py diagnose          # 卡住时先看截图
python scripts/maya_ctl.py targets --pid 1234
python scripts/maya_ctl.py click --pid 1234 --title "Security Warning" --text Allow
```

## 为什么"卡住"不等于"死机"

Maya 弹一个模态对话框（插件安全警告、缺文件、许可提示）时，命令端口照常接受连接但一个字节都不执行——和真死机一模一样。先 `diagnose` 看图，再 `targets` 找按钮，`click` 点掉，会话和复现都保住了；真的挂了才 `restart`。

Qt 自绘的弹窗没有原生子控件，`targets` 的 `clickable` 会是空的：用 `grid` 截一张带 50px 网格的图，读出按钮像素位置，`click --at x,y`（坐标以窗口左上角为原点）。

## 环境

- Windows（启动、进程检查、截图、点击都是 Win32 调用）
- Maya 2018 / 2019 / 2023 / 2024 / 2025 自动识别，默认最新版
- 客户端需要 Python 3（Maya 2023+ 的 `mayapy.exe` 也可以）
- 端到端验证于 **Maya 2025.2**；2018/2019 为 Python 2.7，仅 `detect` 保证可用

完整说明见 [SKILL.md](SKILL.md)，协议细节见 [references/command-port.md](references/command-port.md)，排障手册见 [references/ops.md](references/ops.md)。
