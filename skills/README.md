# 项目 Skills

Skill 是按需加载的 Agent 使用说明。Agent 启动时只读取每个 Skill 的 `name` 和
`description`，真正需要时才通过专用只读工具加载完整 `SKILL.md`，再按其中的路由读取
必要的支持文件。这样可以避免把所有说明一次塞进模型上下文。

## 创建步骤

1. 复制 `_template`，并把目录改成小写 kebab-case 名称，例如 `python-test-helper`。
2. 把 `SKILL.md` front matter 中的 `name` 改成与目录完全相同的名称。
3. 用一行 `description` 同时说明“这个 Skill 做什么”和“什么情况下使用”。
4. 在正文写清操作规则；较长资料放到 `references/`，示例脚本可放到 `scripts/`，并在
   `SKILL.md` 中明确什么情况下需要读取哪个文件。
5. 重启 Agent，让启动目录重新发现新增 Skill。

最小 front matter 只支持两个字段，不能添加未知或重复字段，也不支持 YAML 多行值：

```text
---
name: python-test-helper
description: 指导编写和排查 Python unittest；在新增测试或测试失败时使用。
---
```

安全限制：每个有效 Skill 必须只有一个大小写不敏感的 `SKILL.md`；所有文件必须是
UTF-8 普通单链接文本文件，不允许符号链接、junction、reparse point 或硬链接。
支持文件的数量、深度、单文件大小和总大小均有限制。`scripts/` 只会作为文本读取，
不会自动执行。整个 `skills/` 目录对普通读取、搜索和验证型 shell 工具不可见；在默认
`partial-control` 下，任何修改还必须经过敏感路径人工审批。建议由开发者在 Agent 外审查
和维护。

只有 `_template` 会被明确忽略；其它未知根条目会让发现过程 fail closed。根条目数、
每个 Skill 的目录数也有固定上限，避免异常目录树拖垮启动扫描。
