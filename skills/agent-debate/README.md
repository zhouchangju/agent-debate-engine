# Agent Debate Skill

`agent-debate` 把一个技术问题交给多个 Agent 角色，按“独立提案 → 交叉批评 →
修订 → Judge 裁决”的流程进行有界讨论，并保存完整的可审计结果。

它默认使用两个只读 Codex 角色，不修改仓库，也不会自动启用 Kimi、Generic
命令或全权限模式。最终状态由引擎的确定性规则决定；`exhausted`、`blocked`
或 `timed_out` 都不能称为“已达成共识”。

## 怎么问

直接在对话中调用 Skill，并写清目标、约束和关注点：

```text
$agent-debate A 方案和 B 方案哪个更适合这个项目？
重点比较迁移成本、长期维护成本和失败回滚，使用 standard 深度。
```

```text
$agent-debate 让多个 Agent 评审当前认证模块的架构。
请重点攻击权限边界、并发安全和可观测性，不要修改代码。
```

```text
$agent-debate 深度讨论我们是否应该从 SQLite 迁移到 PostgreSQL。
结合当前仓库证据，明确支持与反对理由、迁移前提和未解决风险。
```

```text
$agent-debate 先只预览这次辩论会使用哪些角色和阶段，不要调用模型。
```

```text
$agent-debate 继续运行上次返回的 run_dir：/path/to/exact-run-dir，
并打开结果网页。
```

深度可选：

- `quick`：一轮，适合快速获得第二意见；
- `standard`：最多三轮，默认选择；
- `deep`：最多五轮，适合重要或争议较大的决策。

## 命令行示例

只查看执行计划，不调用模型：

```bash
uv run python skills/agent-debate/scripts/run_debate.py plan \
  --workspace . \
  --depth standard
```

将问题保存到 UTF-8 文本文件后运行，并打开结果页：

```bash
uv run python skills/agent-debate/scripts/run_debate.py run \
  --workspace . \
  --depth standard \
  --task-file /tmp/agent-debate-task.md \
  --open-dashboard
```

继续一个未完成的运行：

```bash
uv run python skills/agent-debate/scripts/run_debate.py resume \
  /path/to/exact-run-dir \
  --open-dashboard
```

## 查看结果网页

`run` 或 `resume` 加上 `--open-dashboard`，会自动启动本地服务并打开当前
运行的详情页。也可以在仓库根目录手动浏览全部历史结果：

```bash
uv run agent-debate-dashboard --root .agent-debate
```

默认地址是 <http://127.0.0.1:8765/>。网页只监听本机，但其中可能包含仓库
内容、Prompt 和模型输出，不要随意对外暴露。

每次运行还会保存 `final.md`、`result.json`、`evidence.md` 和
`manifest.json`，分别用于阅读结论、程序消费、核查完整证据和恢复运行。
