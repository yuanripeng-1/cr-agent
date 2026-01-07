import asyncio
import json
import os
from .router import CRRouter

async def main():
    # 从 context.json 读取任务上下文
    context_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "context.json")
    
    with open(context_path, "r", encoding="utf-8") as f:
        context = json.load(f)
    
    # 提取标准字段
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
    
    # 构造 MR Message（标题 + 描述）
    mr_message = f"{title}\n\n{description}"
    
    # 读取需求文档内容（如果提供了路径）
    requirements_content = ""
    if requirements_doc_path and os.path.exists(requirements_doc_path):
        with open(requirements_doc_path, "r", encoding="utf-8") as f:
            requirements_content = f.read()
    
    # 从 diff_content 中提取涉及的文件路径
    file_paths = []
    for line in diff_content.split("\n"):
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            # 提取文件路径
            file_path = line.split(" ")[1]
            # 移除 a/ 或 b/ 前缀
            if file_path.startswith("a/") or file_path.startswith("b/"):
                file_path = file_path[2:]
            file_paths.append(file_path)
    
    # 去重
    file_paths = list(set(file_paths))
    
    print(f"📋 Task ID: {task_id}")
    print(f"📦 Project ID: {project_id}")
    print(f"🔀 MR IID: {mr_iid}")
    print(f"🌿 Source Branch: {source_branch} -> Target Branch: {target_branch}")
    print(f"📝 Files Changed: {len(file_paths)}")
    print(f"📄 Requirements Doc: {requirements_doc_path if requirements_doc_path else 'None'}")
    
    # Initialize Router
    router = CRRouter(model="gpt-4o")  # any LiteLLM-supported model
    
    # 执行流程
    result = await router.route_and_aggregate(
        mr_message=mr_message,
        code_diff=diff_content,
        file_paths=file_paths,
        project_root=project_root,
        requirements_content=requirements_content
    )

    # 输出结果
    print("\n" + "="*50)
    print("FINAL CR RESULT (JSON wrapper; expert reports/summary are YAML strings)")
    print("="*50)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 也可以输出 MD 格式
    output_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "CR_REPORT.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# 代码审查总结报告\n\n")
        f.write(f"**任务 ID:** {task_id}\n\n")
        f.write(f"**项目 ID:** {project_id}\n\n")
        f.write(f"**MR IID:** {mr_iid}\n\n")
        f.write(f"**源分支:** {source_branch} -> **目标分支:** {target_branch}\n\n")
        f.write(f"**基准 SHA:** {base_sha}\n\n")
        f.write(f"**最新 SHA:** {head_sha}\n\n")
        f.write("---\n\n")
        # 去掉 summary 中的 markdown 代码块标记（如果有）
        summary_content = result["summary"]
        if summary_content.startswith("```markdown"):
            summary_content = summary_content[11:]  # 去掉 ```markdown
        if summary_content.startswith("```"):
            summary_content = summary_content[3:]  # 去掉 ```
        if summary_content.endswith("```"):
            summary_content = summary_content[:-3]  # 去掉结尾的 ```
        summary_content = summary_content.strip()
        f.write(summary_content)

if __name__ == "__main__":
    # 需要设置 OPENAI_API_KEY 环境变量
    asyncio.run(main())

