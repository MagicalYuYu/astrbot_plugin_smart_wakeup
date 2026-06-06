# 版本发布操作指引

> 每次完成功能修改或 Bug 修复后，按以下清单逐项执行，确保不遗漏任何环节。

---

## 一、确定版本号

遵循 [语义化版本](https://semver.org/lang/zh-CN/) 规范：

| 变更类型 | 版本递增规则 | 示例 |
|:---------|:-------------|:-----|
| Bug 修复、内部重构 | 补丁号 +1 | 1.0.2 → 1.0.3 |
| 新增功能（向后兼容） | 次版本号 +1，补丁号归零 | 1.0.2 → 1.1.0 |
| 破坏性变更 | 主版本号 +1，其余归零 | 1.0.2 → 2.0.0 |

---

## 二、更新版本号（4 处）

以下文件包含版本号，必须全部同步更新：

| 序号 | 文件 | 位置 | 示例 |
|:----:|:-----|:-----|:-----|
| 1 | `main.py` | `@register(...)` 装饰器第 4 个参数 | `"1.0.2"` → `"1.0.3"` |
| 2 | `metadata.yaml` | `version:` 字段 | `version: 1.0.2` → `version: 1.0.3` |
| 3 | `README.md` | 版本徽章 `badge/version-` 后 | `version-1.0.2-blue` → `version-1.0.3-blue` |
| 4 | `README_EN.md` | 版本徽章 `badge/version-` 后 | `version-1.0.2-blue` → `version-1.0.3-blue` |

**不需要更新版本号的文件：**
- `_conf_schema.json` — 不含版本号
- `LICENSE` — 不含版本号

---

## 三、更新 CHANGELOG.md

在文件顶部（`# 更新日志` 标题之后）插入新版本条目，格式如下：

```markdown
## X.Y.Z (YYYY-MM-DD)

### 新增功能 / Bug 修复 / 优化

- **功能名称**：简要描述变更内容和原因
- **功能名称**：简要描述变更内容和原因
```

### 条目分类

| 分类 | 使用场景 |
|:-----|:---------|
| `### 新增功能` | 新增特性、新增配置项 |
| `### Bug 修复` | 修复已知问题 |
| `### 优化` | 性能优化、逻辑改进、体验提升 |
| `### 破坏性变更` | 不兼容的接口或行为变更 |

### 条目撰写规范

1. 每条以 `**关键词**：` 开头，加粗关键词后用冒号分隔描述
2. 描述应说明"做了什么"和"为什么做"，而非仅写"修复了XXX"
3. 涉及配置项变更时，写明配置项名称和默认值变化
4. 涉及 Bug 修复时，简要说明根因和影响范围

---

## 四、代码验证

在提交前执行以下检查：

```powershell
# 1. Python 编译检查（确保无语法错误）
python -m py_compile main.py

# 2. 查看变更文件清单（确认修改范围）
git status

# 3. 查看变更详情（确认内容正确）
git diff
```

---

## 五、Git 提交与推送

### 提交消息格式

```
<type>: <简短描述>
```

**type 取值：**

| type | 含义 |
|:-----|:-----|
| `feat` | 新增功能 |
| `fix` | Bug 修复 |
| `docs` | 文档变更 |
| `style` | 格式调整（不影响逻辑） |
| `refactor` | 重构（非新功能、非修复） |
| `chore` | 构建/工具/配置变更 |

**示例：**
- `feat: add Sticker detection in low-info message filter`
- `fix: resolve fatal indentation error in splitter module, bump to v1.0.2`
- `docs: update CHANGELOG and version badges for v1.0.3`

### 操作步骤

```powershell
# 1. 暂存变更文件（逐个添加，避免误提交）
git add main.py metadata.yaml CHANGELOG.md README.md README_EN.md

# 2. 提交
git commit -m "<type>: <描述>"

# 3. 推送
git push
```

---

## 六、创建 GitHub Release

### 前置条件

- 已安装并登录 GitHub CLI（`gh auth status` 可验证）
- 已完成 Git 推送

### Release Notes 格式

创建临时文件 `release_notes_tmp.md`，内容格式如下：

```markdown
## vX.Y.Z <分类标题>

### <子分类>

<详细描述，与 CHANGELOG 条目对应但可更详细>

---

**完整更新日志**: https://github.com/MagicalYuYu/astrbot_plugin_smart_wakeup/blob/main/CHANGELOG.md
```

### 操作步骤

```powershell
# 1. 创建 Release Notes 临时文件
# （手动编写或由 AI 生成，保存为 release_notes_tmp.md）

# 2. 创建 Release
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file release_notes_tmp.md

# 3. 确认创建成功（输出中应包含 Release URL）

# 4. 清理临时文件
Remove-Item release_notes_tmp.md
```

### 注意事项

- PowerShell 中 `--notes` 参数对特殊字符敏感，推荐使用 `--notes-file` 传入文件
- Tag 名称格式为 `vX.Y.Z`（带 v 前缀）
- Release 标题与 Tag 一致，如 `v1.0.3`
- 如果 Tag 已存在，`gh release create` 会失败，需先删除旧 Tag 或使用新版本号

---

## 七、完整操作清单（快速核对）

完成功能修改后，按顺序逐项核对：

- [ ] **1. 确定版本号** — 根据变更类型递增版本
- [ ] **2. 更新 `main.py`** — `@register` 装饰器版本号
- [ ] **3. 更新 `metadata.yaml`** — `version` 字段
- [ ] **4. 更新 `README.md`** — 版本徽章
- [ ] **5. 更新 `README_EN.md`** — 版本徽章
- [ ] **6. 更新 `CHANGELOG.md`** — 新增版本条目
- [ ] **7. 代码验证** — `python -m py_compile main.py` 通过
- [ ] **8. 查看变更** — `git status` + `git diff` 确认无误
- [ ] **9. 暂存文件** — `git add` 逐个添加
- [ ] **10. 提交** — `git commit -m "..."`
- [ ] **11. 推送** — `git push`
- [ ] **12. 编写 Release Notes** — 保存为临时文件
- [ ] **13. 创建 Release** — `gh release create vX.Y.Z --title "vX.Y.Z" --notes-file release_notes_tmp.md`
- [ ] **14. 清理临时文件** — 删除 `release_notes_tmp.md`
- [ ] **15. 确认** — 验证 GitHub Release 页面显示正确
- [ ] **16. 汇报** — 生成简短文字汇报，末尾附加合规标识 `📋 已遵照 docs/release-sop.md 执行`

---

## 附录：文件版本号速查

| 文件 | 版本号位置 | 搜索关键词 |
|:-----|:----------|:-----------|
| `main.py` | `@register(...)` 第 4 参数 | `"1.0.` |
| `metadata.yaml` | `version:` 行 | `version:` |
| `README.md` | 版本徽章 | `version-1.0.` |
| `README_EN.md` | 版本徽章 | `version-1.0.` |

---

## 附录：提交提醒机制

本项目配置了两层提醒，确保每次提交前不会遗漏发布流程：

### 1. Git pre-commit 钩子（自动提醒）

项目内置了 `.githooks/pre-commit` 钩子，每次执行 `git commit` 时自动打印版本发布核对要点。

**首次克隆仓库后需激活钩子：**

```powershell
git config core.hooksPath .githooks
```

**如需跳过提醒：**

```powershell
git commit --no-verify -m "..."
```

### 2. AI 助手项目规则（自动遵循）

`.trae/rules/project_rules.md` 中声明了版本发布流程规则，AI 助手在每次执行 git 操作前会自动参考 `docs/release-sop.md`。
