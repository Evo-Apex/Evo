# Evo（给 Claude 的入口）

项目说明都在 [AGENTS.md](AGENTS.md)（Claude、Antigravity 等都读它，2026-11-26 从这里挪过去）。规矩要加、要改都改 AGENTS.md，这个文件只放下面几条只有 Claude 用得上的。

@AGENTS.md

## 只给 Claude 的

- **窗口之间发消息**：AGENTS.md「协作约定」写了什么时候要给别的窗口说一声；Claude 的做法是先用 `ListAgents` 看开着哪些窗口，再用 `SendMessage` 给相关的窗口发消息。Antigravity 的窗口不在这个列表里、收不到消息，它可能也在同一个文件夹里改东西：提交前照样看 `git status`、`git diff`
- **记忆**：Claude 的记忆（`~/.claude/projects/E--Evo/memory/`）别的工具看不到。用户定下来、所有 AI 助手都要守的规矩写进 AGENTS.md 或对应的 `docs/`，不要只记在记忆里
