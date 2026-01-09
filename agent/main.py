import asyncio
import json
import os
import toml
from .router import CRRouter
from .utils import parse_diff_file_paths, setup_log_redirection

async def main():
    # Load config.toml from environment variable or default path
    config_path = os.environ.get("CR_AGENT_CONFIG")
    if not config_path:
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.toml")
    
    if not os.path.exists(config_path):
        print(f"❌ Config file not found: {config_path}")
        return

    config = toml.load(config_path)
    context_cfg = config.get("context", {})
    json_path = context_cfg.get("json_path", "context.json")
    
    # Handle relative path for json_path
    if not os.path.isabs(json_path):
        json_path = os.path.join(os.path.dirname(config_path), json_path)

    if not os.path.exists(json_path):
        print(f"❌ Context JSON not found: {json_path}")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        context = json.load(f)
    
    result_path = context_cfg.get("result_path", ".")
    os.makedirs(result_path, exist_ok=True)
    log_path = os.path.join(result_path, "run.log")
    log_file = setup_log_redirection(log_path)

    # 提取标准字段
    task_id = context.get("task_id", "")
    project_id = context.get("project_id", "")
    mr_iid = context.get("mr_iid", "")
    title = context.get("title", "")
    description = context.get("description", "")
    base_sha = context.get("base_sha", "")
    head_sha = context.get("head_sha", "")
    source_branch = context.get("source_branch", "")
    target_branch = context.get("target_branch", "")
    diff_content = context.get("diff_content", "")
    project_root = context.get("project_root", ".")
    requirements_doc_path = context.get("requirements_Doc", "")
    
    # 构造 MR Message
    mr_message = f"{title}\n\n{description}"
    
    # 读取需求文档内容
    requirements_content = ""
    if not requirements_doc_path:
        requirements_doc_path = config.get("project", {}).get("requirements_path", "")
    
    if requirements_doc_path:
        if not os.path.isabs(requirements_doc_path):
            requirements_doc_path = os.path.join(os.path.dirname(config_path), requirements_doc_path)
        if os.path.exists(requirements_doc_path):
            with open(requirements_doc_path, "r", encoding="utf-8") as f:
                requirements_content = f.read()
    
    file_paths = parse_diff_file_paths(diff_content)
    
    print(f"📋 Task ID: {task_id}")
    print(f"📦 Project ID: {project_id}")
    print(f"🔀 MR IID: {mr_iid}")
    print(f"🌿 Source Branch: {source_branch} -> Target Branch: {target_branch}")
    print(f"📝 Files Changed: {len(file_paths)}")
    
    # Initialize Router
    llm_config = config.get("llm", {})
    model = llm_config.get("model", "gpt-4o")
    
    # Env Setup for LiteLLM
    api_key = llm_config.get("api_key", os.environ.get("OPENAI_API_KEY", ""))
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["OPENROUTER_API_KEY"] = api_key # 确保 OpenRouter 也能读到 key
    
    if llm_config.get("api_base"):
        os.environ["OPENAI_API_BASE"] = llm_config["api_base"]
        # 如果是 OpenRouter，有时候不设置 OPENAI_API_BASE 反而更稳定，因为模型前缀自带路由

    router = CRRouter(model=model)
    
    status = "success"
    result = {}
    md_output = ""

    try:
        # 执行流程
        result = await router.route_and_aggregate(
            mr_message=mr_message,
            code_diff=diff_content,
            file_paths=file_paths,
            project_root=project_root,
            requirements_content=requirements_content
        )

        # 构造 MD 格式
        summary_content = result.get("summary", "")
        # 去掉 summary 中的 markdown 代码块标记
        if summary_content.startswith("```markdown"):
            summary_content = summary_content[11:]
        elif summary_content.startswith("```"):
            summary_content = summary_content[3:]
        if summary_content.endswith("```"):
            summary_content = summary_content[:-3]
        summary_content = summary_content.strip()

        md_output = f"# 代码审查总结报告\n\n"
        md_output += f"**任务 ID:** {task_id}\n\n"
        md_output += f"**项目 ID:** {project_id}\n\n"
        md_output += f"**MR IID:** {mr_iid}\n\n"
        md_output += f"**源分支:** {source_branch} -> **目标分支:** {target_branch}\n\n"
        md_output += f"**基准 SHA:** {base_sha}\n\n"
        md_output += f"**最新 SHA:** {head_sha}\n\n"
        md_output += "---\n\n"
        md_output += summary_content

        # 输出结果到文件
        with open(os.path.join(result_path, "cr_result.md"), "w", encoding="utf-8") as f:
            f.write(md_output)
            
    except Exception as e:
        status = "failure"
        import traceback
        error_details = traceback.format_exc()
        error_msg = f"❌ Error during execution: {str(e)}\n{error_details}"
        print(error_msg)
        # On failure, we might not have a result object, so we ensure summary is at least the error
        if not result.get("summary"):
            result["summary"] = error_msg

    # 保存 result.json
    with open(os.path.join(result_path, "result.json"), "w", encoding="utf-8") as f:
        json.dump({
            "llm_result": result.get("summary", ""),
            "status": status,
            "log_path": log_path
        }, f, indent=2, ensure_ascii=False)

    # 兼容旧路径，如果需要的话 (RUN.sh 期待 CR_REPORT.md)
    with open("CR_REPORT.md", "w", encoding="utf-8") as f:
        f.write(md_output)

if __name__ == "__main__":
    asyncio.run(main())

