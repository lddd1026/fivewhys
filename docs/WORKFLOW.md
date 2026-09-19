# 开发流程

本项目按正式工作流程推进：**每个任务都走这 7 步，不管任务多小。**

> 为什么这么较真：面试官看你的 GitHub 时，看的不只是代码，
> 还有你的**提交历史**和**分支结构**。一个有 50 个"update"提交的项目，
> 和一个每个提交都能看懂、每个改动都能独立回滚的项目，印象完全不同。

---

## 每个任务的 7 步

### 1. 领任务

打开 [`TASKS.md`](TASKS.md)，从「待办」里挑一个 **P0** 任务。
把它的状态从 `[ ]` 改成 `[~]`（进行中），并只挑**一个**。

一次只做一件事。同时开三个任务，最后三个都做不完。

### 2. 建分支

```powershell
git switch main
git pull                      # 如果推到了远程
git switch -c feat/FIV-1-inject-db-pool
```

分支命名规则：

| 前缀 | 用途 | 例子 |
| ---- | ---- | ---- |
| `feat/` | 新功能 | `feat/FIV-1-inject-db-pool` |
| `fix/` | 修 bug | `fix/FIV-2-query-logs-args` |
| `test/` | 只加测试 | `test/FIV-3-loop-retry` |
| `docs/` | 只改文档 | `docs/update-readme` |
| `chore/` | 杂活（依赖升级等） | `chore/bump-litellm` |

**为什么必须建分支**：如果这个任务做砸了，`git switch main` 一句就回到干净状态。
在主干上直接改，做砸了就得一点点往回退。

### 3. 写代码

只改**这个任务该改的文件**。

如果写着写着发现「顺便这里也该改一下」——**停下来**。
那说明还有另一个任务，记进 `TASKS.md`，做完现在这个再说。

这个习惯叫「不夹带私货」，是 code review 里最被看重的一点。

### 4. 本地验证

**这一步不能跳。** 没验证的代码不该提交。

```powershell
# 一键跑完整检查
pwsh -File scripts/check.ps1
```

至少要做到：
- [ ] `pytest` 全绿
- [ ] 手动跑一遍这个功能，确认真的能用（把命令和输出记下来，第 6 步要用）

### 5. 提交

```powershell
git add -A
git commit -m "feat(mock): 实现 db_pool_exhausted 故障注入"
```

提交信息格式：`<类型>(<范围>): <做了什么>`

| 类型 | 用途 |
| ---- | ---- |
| `feat` | 新功能 |
| `fix` | 修 bug |
| `refactor` | 重构（行为不变） |
| `test` | 加/改测试 |
| `docs` | 文档 |
| `chore` | 杂活 |

**写"为什么"，不只是"做了什么"**。对比一下：

```
❌ update scenarios.py
✅ feat(mock): 实现 db_pool_exhausted 故障注入

   日志只写入「现象」（deadline exceeded / connection wait time 飙升），
   不写入「答案」（pool exhausted）。这样 agent 必须靠推理定位根因，
   而不是 grep 一下关键字就完事。
```

### 6. 自查

对着 [`.github/PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md)
逐条检查。哪怕不真的开 PR，这个动作也要做——
它是为了在**合并之前**发现自己漏了什么。

### 7. 合并

```powershell
git switch main
git merge --no-ff feat/FIV-1-inject-db-pool
git branch -d feat/FIV-1-inject-db-pool
```

`--no-ff` 会保留一个合并提交，这样从历史里能一眼看出「这个功能是一个整体」。

然后把 `TASKS.md` 里那个任务改成 `[x]`，提交一次。

---

## 常用命令速查

```powershell
git switch -c feat/FIV-1-xxx     # 建并切到新分支
git status                        # 看当前改了哪些文件
git diff                          # 看具体改了什么
git switch main                   # 回主干
git branch -d feat/FIV-1-xxx      # 删掉已合并的分支
git log --oneline --graph         # 看分支图（加分项：截图放 README）
```

## 推送到 GitHub（可选，但强烈建议）

放到 GitHub 上，你的 commit 历史才是「能被看到的作品」。

```powershell
git remote add origin https://github.com/<你的用户名>/fivewhys.git
git push -u origin main
```

之后每个任务推一次：

```powershell
git push -u origin feat/FIV-1-inject-db-pool
```

然后在网页上开 Pull Request → 自己 review 一遍 → 合并。
**自己 review 自己的 PR 不是走形式**，很多问题就是在这一步被自己看出来的。
