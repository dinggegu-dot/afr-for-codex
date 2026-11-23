# AFR for Codex

把研报放进本机文件夹，在 Codex 里说「整理这篇研报」。AFR 会生成按论述编排的 Obsidian 白板和完整阅读稿。研究问题从多篇已校读材料中提炼，追问时可以回到固定版本和原文段落。

这是独立的 macOS Codex 插件。它有自己的收件箱、Obsidian 库和授权记录，适合从一个空库开始。

## 安装

需要 macOS、Python 3.11 或更新版本，以及已登录且支持插件的 Codex 桌面版。建议安装 Obsidian 来打开白板。

在终端运行以下两行。如终端找不到 `codex`，请先安装 [官方 Codex CLI](https://developers.openai.com/codex/cli)。

```bash
codex plugin marketplace add dinggegu-dot/afr-for-codex
codex plugin add afr-for-codex@afr-marketplace
```

如果 CLI 提供目录添加命令但未提供插件安装命令，请在 Codex 桌面版的 **Plugins** 页面，从 **Atom Flow Research** 目录安装 **AFR for Codex**。打开一个新对话。首次启动会从官方 PyPI 下载锁定的依赖，并在插件自己的目录里创建 Python 环境。若首次启动超时，可在下载结束后重开 Codex。

## 第一次使用

1. 在 Codex 里发送「设置 AFR，使用独立默认目录」。默认收件箱和库分别是 `~/Library/Application Support/AFR for Codex/Inbox` 与同级 `Library`。也可以在首次设置时指定两个不同的本机目录。插件会拒绝接管已有 AFR 数据的库。
2. 在 Obsidian 中选择「打开本地库」，打开以上 `Library` 或你选的目录。把 [城市积水预警](examples/城市积水预警.md) 下载到收件箱。
3. 发送「收录城市积水预警」。收录后会显示可读状态和固定版本。想生成白板时，发送「我同意 OpenAI Codex 处理这份示例的固定版本，请生成白板」。
4. 等待签发，或发送「看这一份的 AFR 进度」。完成后会得到「白板」与「完整阅读」的位置。再加入 [社区演练评估](examples/社区演练评估.md)，按同样方式处理，然后发送「我同意用这两份已签发研究，梳理一个共同问题，给出原文出处」。
5. 在 Codex 查看候选问题和出处。选中一个后发送「把这个研究问题加入库」。入库后，可以继续问「这两篇对封路规则有什么分歧？」并查看固定原文。

白板用分组、可独立阅读的卡片和连线呈现完整论述。完整阅读沿用同一组主题。归档优先进入匹配的领域目录，低置信度进入「待整理」。收录支持 DOCX、可提取文字的 PDF、Markdown 和 UTF-8 文本。扫描件请先在本机完成 OCR。

## 默认权限和自动化

| 操作 | 默认行为 |
| --- | --- |
| 扫描与固定原文 | 本机完成。指定已有通用文件名是否自动改名是在首次设置时的可选项 |
| 白板生成、校读和研究问题 | 使用已登录的 OpenAI Codex。首次使用按固定版本授权，文件内容变化后产生新版本 |
| Codex 内检索原文 | 只能读取已授权版本及来源已全部获准的研究问题 |
| 写入研究讨论 | 先启用讨论读写，再确认具体写入内容。支持 Markdown 链接，拒绝 HTML 和嵌入 |
| 自动收录 | 默认关闭。可在 Codex 中请求开启 macOS 收件箱监听，默认仅本机收录 |
| 自动研读 | 需单独同意将之后新放入的文件交给 OpenAI Codex。启动前已有版本和用于确认跨篇候选的来源仍需逐份授权 |

监听作用于当前 macOS 登录会话，登录后可重新开启。监听运行时，逐篇授权也会让这篇进入后台研读。撤回某个版本的授权会阻止后续使用，已经发出的请求和本机成果仍按原有记录保留。使用独立库有助于把自行启用的 Obsidian 同步与插件的权限设置分开。

如需由 Codex 客户端逐次弹窗确认工具调用，可在 Codex 配置中为此插件的 `afr` 服务设置 `default_tools_approval_mode = "prompt"`。插件内的授权记录和客户端弹窗是两层控制。详见 [Codex 插件工具权限](https://developers.openai.com/plugins/build/plugins#configure-installed-server-policy)。

遇到 `codex_unavailable` 或 `codex_authentication`，先从终端确认官方 CLI 已安装并已登录。遇到 `job_busy` 时，查询当前任务结束后再提交。存在多个同名 Obsidian 库时，可直接用状态里返回的绝对路径定位产物。

## 项目文档

- [架构与数据边界](docs/architecture.md)
- [验收与回滚](docs/verification.md)
- [合成示例](examples/)

macOS 为首版支持平台。首次验证覆盖 Apple Silicon。Codex 插件目录通过 GitHub 安装。

## 使用范围

此分享版公开供下载、安装和本机使用。
