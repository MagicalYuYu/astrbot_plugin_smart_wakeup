# GitHub 首次发布操作指南

> 本文档面向从未在 GitHub 上传过项目的用户，手把手引导你完成从零到发布的全流程。

---

## 一、前置准备

### 1.1 安装 Git

1. 访问 [https://git-scm.com/download/win](https://git-scm.com/download/win)，下载 Windows 版安装包
2. 运行安装程序，大部分选项保持默认即可：
   - **编辑器选择**：建议选 VS Code 或 Nano（如果你不熟悉 Vim）
   - **PATH 环境**：选择 "Git from the command line and also from 3rd-party software"
   - **SSH 可执行文件**：选择 "Use bundled OpenSSH"
3. 安装完成后，打开 PowerShell，输入 `git --version` 验证安装成功

### 1.2 配置 Git 用户信息

打开 PowerShell，执行以下命令（将信息替换为你自己的）：

```bash
git config --global user.name "MagicalYuYu"
git config --global user.email "你的GitHub注册邮箱@example.com"
```

> 这里的名字和邮箱会出现在每次提交记录中，建议与 GitHub 账户信息一致。

### 1.3 配置 SSH 密钥

SSH 密钥用于安全地连接 GitHub，无需每次输入密码。

**生成密钥**：

```bash
ssh-keygen -t ed25519 -C "你的GitHub注册邮箱@example.com"
```

- 提示输入保存路径时，直接按 Enter 使用默认路径（`C:\Users\你的用户名\.ssh\id_ed25519`）
- 提示输入密码时，可以直接按 Enter 两次（不设密码），或设置一个密码增加安全性

**查看公钥**：

```bash
cat ~/.ssh/id_ed25519.pub
```

复制输出的完整内容（以 `ssh-ed25519` 开头的一长串字符）。

**添加到 GitHub**：

1. 登录 GitHub，点击右上角头像 → **Settings**
2. 左侧菜单选择 **SSH and GPG keys**
3. 点击 **New SSH key**
4. Title 填写 `My PC` 或任意标识名
5. Key 粘贴刚才复制的公钥内容
6. 点击 **Add SSH key**

**验证连接**：

```bash
ssh -T git@github.com
```

首次连接会提示确认指纹，输入 `yes`。成功后会看到：

```
Hi MagicalYuYu! You've successfully authenticated, but GitHub does not provide shell access.
```

> 如果看到 "Permission denied"，说明密钥配置有误，请重新检查上述步骤。

---

## 二、创建 GitHub 仓库

1. 登录 [GitHub](https://github.com)
2. 点击右上角 **+** → **New repository**
3. 填写仓库信息：
   - **Repository name**：`astrbot_plugin_smart_wakeup`
   - **Description**：`心有灵犀，不唤自来 — AstrBot 自然唤醒插件，让 Bot 拥有社交节律`
   - **可见性**：选择 **Public**（公开，所有人可见）
   - **不要勾选** "Add a README file"（我们已有本地 README.md）
   - **不要选择** .gitignore 模板（我们已有本地 .gitignore）
   - **不要选择** License（我们已有本地 LICENSE）
4. 点击 **Create repository**

> 为什么不勾选那些选项？因为我们的本地项目已经包含了 README.md、.gitignore 和 LICENSE，如果 GitHub 再创建一份，会导致首次推送时冲突。

---

## 三、本地初始化与首次推送

打开 PowerShell，进入项目目录：

```bash
cd "D:\AI\AstrBot\Plugin Development\astrbot_plugin_smart_wakeup"
```

### 3.1 初始化 Git 仓库

```bash
git init
```

这会在项目目录下创建一个隐藏的 `.git` 文件夹，Git 开始跟踪此项目。

### 3.2 添加文件到暂存区

逐个添加需要发布的文件（而非 `git add .` 全部添加），确保不会误提交排除项：

```bash
git add main.py
git add metadata.yaml
git add _conf_schema.json
git add README.md
git add CHANGELOG.md
git add LICENSE
git add .gitignore
git add logo.png
git add docs/usage_guide.md
git add docs/troubleshooting_guide.md
git add docs/github_publish_guide.md
```

> **每个文件的作用**：
> | 文件 | 作用 |
> |:-----|:-----|
> | `main.py` | 插件核心代码 |
> | `metadata.yaml` | AstrBot 插件元数据（名称、版本、作者等） |
> | `_conf_schema.json` | 配置面板定义（所有可配置项） |
> | `README.md` | GitHub 项目首页 |
> | `CHANGELOG.md` | 版本变更记录 |
> | `LICENSE` | MIT 开源协议 |
> | `.gitignore` | Git 忽略规则 |
> | `logo.png` | 插件图标 |
> | `docs/usage_guide.md` | 详细使用指南 |
> | `docs/troubleshooting_guide.md` | 故障处理与调试指南 |
> | `docs/github_publish_guide.md` | GitHub 首次发布操作指南（本文档） |

### 3.3 验证暂存区

```bash
git status
```

确认输出中只有上述文件，不应出现 `__pycache__/`、`.trae/`、`参考/`、`docs/Logs/` 等。如果出现，说明 `.gitignore` 配置有误，需检查。

### 3.4 创建首次提交

```bash
git commit -m "feat: initial release v1.0.0"
```

> **提交信息规范**：推荐使用 Conventional Commits 格式：
> - `feat:` 新功能
> - `fix:` 修复
> - `docs:` 文档
> - `refactor:` 重构
> - `chore:` 杂项
>
> 后续提交也建议遵循此格式，让历史记录更清晰。

### 3.5 关联远程仓库

```bash
git remote add origin git@github.com:MagicalYuYu/astrbot_plugin_smart_wakeup.git
```

### 3.6 推送到 GitHub

```bash
git branch -M main
git push -u origin main
```

- `git branch -M main`：将当前分支命名为 `main`（GitHub 默认分支名）
- `git push -u origin main`：推送到远程仓库，`-u` 设置上游跟踪

推送成功后，访问 `https://github.com/MagicalYuYu/astrbot_plugin_smart_wakeup` 即可看到你的项目！

---

## 四、验证与完善

### 4.1 检查页面显示

1. 打开仓库页面，确认 README.md 正确渲染
2. 确认 logo.png 可正常显示
3. 确认没有 `__pycache__/`、`.trae/` 等不应出现的文件
4. 点击 LICENSE 文件，确认显示标准 MIT 协议

### 4.2 设置仓库信息

1. 在仓库页面点击右上角齿轮图标（About 旁）
2. **Description**：`心有灵犀，不唤自来 — AstrBot 自然唤醒插件，让 Bot 拥有社交节律`
3. **Website**：可填 AstrBot 官网 `https://github.com/AstrBotDevs/AstrBot`
4. **Topics**（标签，帮助他人发现你的项目）：
   - `astrbot`
   - `chatbot`
   - `telegram`
   - `qq`
   - `llm`
   - `plugin`
   - `python`
5. 勾选 **Releases** 和 **Packages**（在 Features 中）
6. 点击 **Save changes**

### 4.3 确认 .gitignore 生效

在仓库页面搜索以下内容，确认均不存在：
- `__pycache__`
- `.trae`
- `参考`
- `Logs`

如果发现遗漏，在本地 `.gitignore` 中补充规则，然后：

```bash
git add .gitignore
git commit -m "chore: update .gitignore"
git push
```

---

## 五、创建 Release

Release 是 GitHub 上的版本发布功能，方便用户下载特定版本的代码。

1. 在仓库页面点击 **Releases** → **Create a new release**
2. **Tag version**：输入 `v1.0.0`，选择 **Create new tag on publish**
3. **Release title**：`v1.0.0 — 首发版本`
4. **Describe this release**：

```markdown
### 灵犀 v1.0.0 — 首发版本

让 Bot 拥有自然的社交节律。

**核心功能**：名称唤醒、概率唤醒、精力系统、心流状态机、冷场救场、消息防抖、复读抑制

**上下文与记忆**：分层对话记忆、上下文压缩、增量注入、绕过核心上下文

**模型与路由**：智能模型路由、级联升级、Token 消耗追踪与异常检测

**输出处理**：消息分段、末尾标点剔除、思考标签过滤、重复回复过滤

**平台兼容**：Telegram + QQ (aiocqhttp)

详见 [CHANGELOG.md](CHANGELOG.md)
```

5. 点击 **Publish release**

---

## 六、上架 AstrBot 插件市场

### 6.1 插件市场机制说明

**仅将代码推送到 GitHub 不会自动出现在 AstrBot 插件市场中。** AstrBot 的插件市场是一个索引系统，需要额外步骤才能让你的插件被市场收录。

目前用户安装插件有两种方式：

| 方式 | 说明 | 是否需要上架市场 |
|:-----|:-----|:----------------:|
| **手动添加仓库地址** | 在 AstrBot WebUI → 插件管理 → 点击右下角 `+` → 输入 GitHub 仓库地址 | 否 |
| **从市场搜索安装** | 在 AstrBot WebUI → 插件市场 → 搜索插件名 → 点击安装 | 是 |

### 6.2 上架市场流程

AstrBot 使用 GitHub 托管插件，上架流程如下：

1. **确保代码已推送到 GitHub 仓库**（按本文档第三章完成）

2. **确保 `metadata.yaml` 格式正确**——这是市场展示信息的来源，必须包含：
   - `name`：插件标识（如 `astrbot_plugin_smart_wakeup`）
   - `display_name`：展示名（如 `灵犀`）
   - `desc`：插件描述
   - `short_desc`：市场卡片短描述
   - `version`：版本号
   - `author`：作者名
   - `repo`：GitHub 仓库地址
   - `support_platforms`：支持的平台列表

3. **确保 `logo.png` 存在**——市场卡片会显示此图标，推荐 256x256 正方形

4. **前往 [AstrBot 插件市场](https://astrbot.app/store) 提交插件**：
   - 进入网站后，点击右下角的 **+** 按钮
   - 填写基本信息、作者信息、仓库信息等内容
   - 点击 **提交到 GITHUB** 按钮
   - 你将被导航到 AstrBot 仓库的 Issue 提交页面
   - 确认信息无误后点击 **Create** 按钮提交，即可完成插件发布

5. **等待审核与收录**——审核通过后，插件会出现在市场列表中

> 在市场收录之前，用户仍可通过手动添加仓库地址的方式安装你的插件，功能完全一致。

### 6.3 体积限制与优化

发布到插件市场的插件压缩包（zip）大小**不得超过 16MB**，超过此限制 CI/CD 流水线将自动拒绝。为确保顺利通过审核：

- **压缩图片等静态资源**：对 logo.png 等资源文件进行压缩
- **清理不必要的文件**：`.git`、`__pycache__`、开发配置等不应提交（我们的 `.gitignore` 已处理）
- **优化依赖体积**：精简或按需引入大型依赖库
- **使用 .gitattributes 或发布分支**：只包含发布所需文件以减小 zip 体积

如果插件确实因业务需要无法压缩到 16MB 以内，可以联系 AstrBot 维护者手动 bypass 此限制。

### 6.4 手动安装方式（供用户参考）

在插件市场上架前，你可以告诉用户这样安装：

1. 打开 AstrBot WebUI → 插件管理
2. 点击右下角 `+` 按钮
3. 输入仓库地址：`https://github.com/MagicalYuYu/astrbot_plugin_smart_wakeup`
4. 点击安装

---

## 七、后续更新流程

当你修改了代码，需要同步到 GitHub 时：

```bash
# 1. 查看哪些文件有变动
git status

# 2. 添加变动的文件（指定文件名，或用 git add -A 添加全部）
git add main.py _conf_schema.json

# 3. 提交（写明改了什么）
git commit -m "fix: 修复分段标点剔除逻辑"

# 4. 推送
git push
```

### 发布新版本

1. 更新 `main.py` 中的 `@register` 版本号
2. 更新 `metadata.yaml` 中的 `version`
3. 更新 `CHANGELOG.md` 添加新版本记录
4. 提交并推送
5. 在 GitHub 上创建新的 Release（如 `v1.1.0`）

---

## 八、注意事项

### 绝对不要上传的内容

- **密钥/Token**：API Key、Bot Token、数据库密码等。如果不小心提交了，立即在 GitHub 上删除该文件，并轮换密钥
- **调试日志**：`docs/Logs/` 中的文件可能包含群 ID、用户名等隐私信息
- **他人代码**：`参考/` 目录中的代码属于其他开发者，不应混入你的仓库
- **IDE 配置**：`.trae/`、`.vscode/` 等是个人开发环境配置，对其他用户无用

### .gitignore 不生效怎么办

如果某个文件已经被 Git 跟踪，后续加入 `.gitignore` 不会自动取消跟踪。需要手动移除：

```bash
git rm -r --cached __pycache__
git rm -r --cached .trae
git commit -m "chore: remove tracked files that should be ignored"
git push
```

### 误提交了敏感信息

1. **立即轮换**该密钥/Token（最关键）
2. 如果只是最近一次提交，可以 `git reset` 后重新提交
3. 如果已经推送，需要使用 `git filter-branch` 或 `BFG Repo-Cleaner` 清理历史（较复杂）
4. 极端情况下，删除仓库重新创建是最简单的方式

---

## 九、常见问题

**Q: `git push` 报错 "fatal: remote origin already exists"**

A: 说明已经添加过远程仓库。可以 `git remote -v` 查看当前配置，或 `git remote set-url origin git@github.com:MagicalYuYu/astrbot_plugin_smart_wakeup.git` 更新地址。

**Q: `git push` 报错 "failed to push some refs"**

A: 远程仓库有本地没有的内容（比如你在 GitHub 网页上编辑了文件）。先 `git pull origin main --rebase`，再 `git push`。

**Q: 推送后 GitHub 上中文显示乱码**

A: 确保文件编码为 UTF-8。可以用 VS Code 打开文件，右下角查看编码，如果不是 UTF-8 则点击转换。

**Q: 如何让仓库显示在 AstrBot 插件市场中？**

A: 仅推送到 GitHub 不会自动上架市场。需要向 AstrBot 官方提交收录申请，详见本文档第六章。在上架之前，用户可通过手动添加仓库地址的方式安装插件。
