# Context.json 字段规范

## 标准字段定义

以下是 `context.json` 的标准字段格式，所有字段必须严格遵循此规范：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `task_id` | string | ✅ | 任务唯一标识：后端生成的 UUID，用于追踪单次审查任务，关联日志、容器及宿主机目录 |
| `project_id` | number | ✅ | GitLab 项目数字 ID：用于后端调用 GitLab API 时定位具体的代码仓库 |
| `mr_iid` | number | ✅ | 合并请求内部 ID (IID)：MR 在项目内的序列号，用于回写评论和查询 MR 详情 |
| `title` | string | ✅ | 合并请求标题：描述本次代码变更的主题，帮助 AI 理解审查的大方向 |
| `description` | string | ✅ | 合并请求描述：开发者填写的详细变更说明，AI 可据此判断代码实现是否符合预期意图 |
| `base_sha` | string | ✅ | 目标分支基准 SHA：MR 目标分支（如 main）在合并前的最新提交 ID，用于计算 Diff 的起点 |
| `head_sha` | string | ✅ | 源分支最新提交 SHA：本次 MR 待审查代码的最新提交 ID，评论将挂载到此 SHA 之上 |
| `start_sha` | string | ✅ | MR 起始基准 SHA：通常与 base_sha 一致，表示 MR 创建时两个分支的共同祖先节点 |
| `source_branch` | string | ✅ | 源分支名称：开发者开发功能的分支名 |
| `target_branch` | string | ✅ | 目标分支名称：代码最终要合并进去的分支名（如 main 或 develop） |
| `diff_content` | string | ✅ | 代码变更内容 (Diff)：Git 标准格式的差异文本，包含修改的文件、行号及具体增删内容 |
| `project_root` | string | ✅ | 项目代码根目录：在 Docker 容器内部，通过 git clone 下载的全量代码存放路径 |
| `diff_file_path` | string | ✅ | Diff 文件路径：在 Docker 容器内部，将 diff_content 写入后的文件绝对路径，方便脚本读取 |
| `requirements_Doc` | string | ❌ | 需求文档路径：产品需求文档的路径，AI 将据此判断代码实现是否符合业务需求 |

## 示例

```json
{
    "task_id": "uuid-12345",
    "project_id": 101,
    "mr_iid": 5,
    "title": "feat: 增加用户登录校验逻辑",
    "description": "修复了之前空指针的问题，并增加了 JWT 校验",
    "base_sha": "abc1234567890...",
    "head_sha": "def6789012345...",
    "start_sha": "abc1234567890...",
    "source_branch": "feature-login",
    "target_branch": "main",
    "diff_content": "--- a/internal/auth.go\n+++ b/internal/auth.go\n@@ -10,5 +10,7 @@\n func Login(username, password string) (string, error) {\n     if username == \"\" || password == \"\" {\n         return \"\", errors.New(\"invalid credentials\")\n     }\n+    \n+    // JWT validation\n+    token := generateJWT(username)\n+    return token, nil\n }",
    "project_root": "/workspace/project_code",
    "diff_file_path": "/workspace/changes.diff",
    "requirements_Doc": "/workspace/requirements_path"
}
```

## 字段使用说明

### 1. MR 信息字段
- `title` 和 `description` 会被组合成 `mr_message`，传递给 AI 作为审查上下文
- `mr_iid` 用于在 GitLab 中定位具体的 MR

### 2. SHA 字段
- `base_sha` 和 `head_sha` 用于标识代码变更的范围
- `start_sha` 通常与 `base_sha` 相同

### 3. Diff 字段
- `diff_content` 是核心字段，包含实际的代码变更
- `diff_file_path` 是可选的，用于脚本读取 diff 文件

### 4. 需求文档
- `requirements_Doc` 是可选字段
- 如果提供，AI 会读取该文件内容，用于业务逻辑评审

## 代码实现映射

在 `agent/main.py` 中，这些字段会被映射为：

```python
task_id = context.get("task_id", "")
project_id = context.get("project_id", "")
mr_iid = context.get("mr_iid", "")
title = context.get("title", "")
description = context.get("description", "")
base_sha = context.get("base_sha", "")
head_sha = context.get("head_sha", "")
start_sha = context.get("start_sha", "")
source_branch = context.get("source_branch", "")
target_branch = context.get("target_branch", "")
diff_content = context.get("diff_content", "")
project_root = context.get("project_root", ".")
diff_file_path = context.get("diff_file_path", "")
requirements_doc_path = context.get("requirements_Doc", "")

# 组合 MR Message
mr_message = f"{title}\n\n{description}"
```
