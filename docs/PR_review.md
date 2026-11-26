# 审 PR

给 Claude 看的：审学弟 wzxlll33 和其他网友提的 PR 时照这里做（2026-11-14 写）。用户是最高执行人：我只审，给出结论和建议。在 GitHub 上留言、批准、合并，往别人的分支推送，都要用户点头以后再做。

## 1. 先看清是什么 PR

- `gh pr view <N>`、`gh pr diff <N> --name-only`：谁提的、合进哪个分支、改了哪些文件
- 流程对不对：wzx 的功能先合进 `beta`，再把整个 `beta` 提一个 PR 进 `main`（他直接对 `main` 提的，请他先合进 `beta`）；他自己想的功能要先有用户同意过的 Issue。外部网友从 fork 直接对 `main` 提就行
- 在 [todo.md](../claude/todo.md) 里搜这件事：用户可能已经定过做法，或者说过不要

## 2. 拉到本机（不碰正式数据）

- 开一个单独的 worktree，不在 `E:\Evo` 里切分支（别的窗口可能正在改）：
  ```
  git fetch origin pull/<N>/head:pr-<N>
  git worktree add E:\Evo-pr<N> pr-<N>
  ```
- Python 用项目的虚拟环境 `E:\Evo\.venv\Scripts\python.exe`：依赖都装在那里，系统自带的 `python` 里只有 pip。worktree 里没有 `.venv`，所以测试和程序都用这个完整路径来跑。PR 加了新依赖时，另建一个虚拟环境，不往 `E:\Evo\.venv` 里装
- 测试要用的本机文件：`captures/` 用目录联接（PowerShell：`New-Item -ItemType Junction -Path E:\Evo-pr<N>\captures -Target E:\Evo\captures`），`tests/replay_20261016_teammates.csv`、`tests/in_match_teammates.json` 复制过去
- 运行他的 `evo_app.py`、`panel_app.py` 前，**先把 `EVO_DATA_DIR` 设成复制出来的数据文件夹**，绝不用 `E:\Evo Tracker Data`（11-07 测 0.5.5b 的时候，真实数据库被升到了版本 10，退不回去）
- 审完收拾：先 `cmd /c rmdir E:\Evo-pr<N>\captures` 只删掉链接（**不要用 `Remove-Item -Recurse`**，可能连真截图一起删），删之前和删之后各数一次 `E:\Evo\captures` 里的文件，数量要一样；再删掉复制过去的两个文件，`git worktree remove E:\Evo-pr<N>`，删掉 `pr-<N>` 分支

## 3. 逐条对照

**项目原则（AGENTS.md）**
- 只看不动：在 diff 里搜有没有模拟键盘鼠标输入、全局钩子、读写内存、游戏上的浮层（`SendInput`、`pynput`、`keyboard`、`pyautogui`、`SetWindowsHookEx`、`ReadProcessMemory`、置顶窗口……）。有就是红线，不能合
- 轻量：新依赖（`tools/evo.spec`、requirements）要问理由，看对体积和内存的影响；对局中（每 0.3 秒一张）不能加 OCR 或别的重活，拿不准时实测内存和 CPU
- 开源：不是从别的项目抄来的代码；图片、声音这类素材要写清来源和许可（PR #8 的地图图片版权归 EA）
- 数据可信：读不出来的留空、不当 0，拿不准的标待确认；出错时不悄悄吞掉，也不悄悄换成一个空库
- 配置驱动：区域、阈值、关键字放在 `config/`，不写死在代码里
- 识别的老规矩：停在总结页、还没存下时只读总结页；对局中只比外观

**数据库**：改 `SCHEMA_VERSION` 只能在 `beta` 上做；只加列、加表；用真实数据库的副本试一次升级，确认旧版本还打得开

**安全**：pywebview 给页面调用的函数（名字以下划线开头的也能被页面调用）、打开链接或程序、subprocess、下载和一键更新（latest.json、Mirror酱、增量包）、新的联网地址（隐私说明要跟着改）。改了 `.github/workflows/`、`tools/release.py`、`tools/publish.py` 的要格外仔细：这些碰得到密钥和发布

**仓库**
- 没带进截图、数据库、别的玩家的名字和 ID、密钥、小程序的文件；GitHub 上没有云同步那几个文件，也要能运行
- 文件放对文件夹（`code/`、`tools/`、`tests/`）；项目代码不能 import 测试；加、删、改名了文件，要在 `tools/code_map.py` 里分组并重新生成代码地图
- 改了模型：流程图和[对局全流程图.md](对局全流程图.md)跟着改了没有（没改的话合并时我们补）
- 改了页面：打开看效果（深色、浅色主题都看），截图给用户看

找 bug 可以先跑一次 code-review skill 作参考，但上面这几条要自己逐条对。

## 4. 跑测试

- 照[测试SOP](测试SOP.md)挑。作者那里没有截图，所以改了模型、`storage.py` 的，在本机跑全部 `python tests/run_all.py`（放在后台跑）
- "⏭ 跳过"不算通过；作者说他跑过的测试，我们也自己再跑一次
- 在 worktree 里跑，"源码运行"那一组的"截图在项目的 captures/"一项一定不过：链接被解析成了 `E:\Evo\captures`，和 PR 无关。在 `E:\Evo` 的 `main` 上跑一次 `tests/test_source_run.py`，通过就行
- 发现的问题要用真实截图或本机数据复现出来，结论里写清楚是怎么复现的

## 5. 先在对话里给用户结论

用大白话说，按这个顺序：改了什么（几句话）→ 有没有违背项目原则 → 跑了哪些测试、结果怎样 → 有哪些问题（分"必须改"和"建议改"，每条说清怎么发现的）→ 我的建议，四选一：

- 直接合并
- 小问题我来改：在他的分支后面加一个提交（像 PR #3、#6 那样），他的提交全部保留
- 请作者改：列出要改的地方，等他更新（像 PR #8 那样）
- 不合：说明原因

## 6. 用户定了以后

- 留言用中文（作者用英文就用英文）：先道谢，按编号写我们改了什么或者请他改什么，每条怎么验证的。不写截图里别的玩家的名字，不贴只在本机的东西
- 往他的分支推修改，要他勾了"允许维护者修改"；推完以后用户自己批准就算数
- 合并选 "Create a merge commit"，保留他的署名
- 合并以后：本机 `git fetch`，把 `main` 合进来；更新 todo.md 里相关的那一条；清理 worktree
