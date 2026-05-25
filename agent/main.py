import asyncio
import json
import os
import subprocess
import toml
try:
    import yaml
except ImportError:
    yaml = None
from .router import CRRouter
from .utils import parse_diff_file_paths, setup_log_redirection, validate_line_comment_by_file, filter_code_diff, generate_line_number_feedback, correct_line_number_with_feedback, annotate_diff_with_line_numbers, parse_summary_llm_output

def _get_int(config: dict, key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        print(f"⚠️ llm.{key} 配置无效（{value}），将使用默认值 {default}")
        return default

def _get_optional_int(config: dict, key: str) -> int | None:
    value = config.get(key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        print(f"⚠️ llm.{key} 配置无效（{value}），将忽略该项")
        return None

def _get_float(config: dict, key: str, default: float) -> float:
    value = config.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        print(f"⚠️ llm.{key} 配置无效（{value}），将使用默认值 {default}")
        return default

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
        # If json_path starts with ./, it's relative to project root (where RUN.sh is)
        # Otherwise, it's relative to config file directory
        if json_path.startswith("./"):
            # Get project root (assume it's the directory containing agent/ folder)
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            json_path = os.path.join(project_root, json_path[2:])  # Remove leading ./
        else:
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
    # Handle relative path for project_root
    if project_root and not os.path.isabs(project_root):
        if project_root.startswith("./"):
            # Get project root (assume it's the directory containing agent/ folder)
            base_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            project_root = os.path.join(base_project_root, project_root[2:])  # Remove leading ./
        else:
            # Relative to config file directory
            project_root = os.path.join(os.path.dirname(config_path), project_root)
    # Normalize to absolute path
    if project_root:
        project_root = os.path.abspath(project_root)
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
    
    # 过滤 diff，只保留代码文件的修改
    original_diff_length = len(diff_content)
    diff_content = filter_code_diff(diff_content)
    filtered_diff_length = len(diff_content)
    
    if original_diff_length != filtered_diff_length:
        print(f"🔍 已过滤 diff：原始长度 {original_diff_length} 字符 -> 代码文件长度 {filtered_diff_length} 字符")
    
    # 在 diff 中添加实际行号注释，帮助 Agent 准确识别行号
    if project_root and os.path.exists(project_root):
        diff_content = annotate_diff_with_line_numbers(diff_content, project_root)
        print(f"✅ 已在 diff 中添加实际行号注释")

    # Debug: Print CODE DIFF preview as it will be embedded into prompts.
    # Keep output bounded to avoid overwhelming logs on very large diffs.
    diff_lines = diff_content.splitlines()
    preview_line_limit = 120
    print("\n🔎 Prompt CODE DIFF 预览（用于确认传入格式）")
    print("===== BEGIN CODE DIFF IN PROMPT =====")
    print("### CODE DIFF")
    for line in diff_lines[:preview_line_limit]:
        print(line)
    if len(diff_lines) > preview_line_limit:
        print(f"... (已截断，剩余 {len(diff_lines) - preview_line_limit} 行未显示)")
    print("===== END CODE DIFF IN PROMPT =====\n")

    file_paths = parse_diff_file_paths(diff_content)
    
    print(f"📋 Task ID: {task_id}")
    print(f"📦 Project ID: {project_id}")
    print(f"🔀 MR IID: {mr_iid}")
    print(f"🌿 Source Branch: {source_branch} -> Target Branch: {target_branch}")
    print(f"📝 Files Changed: {len(file_paths)}")
    
    # Initialize Router
    llm_config = config.get("llm", {})
    model = llm_config.get("model", "gpt-4o")
    
    # Env Setup for LiteLLM（api_key 可选，私有化如 Ollama 可不填）
    api_key = llm_config.get("api_key") or os.environ.get("OPENAI_API_KEY") or ""
    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENROUTER_API_KEY"] = api_key
    
    api_base = llm_config.get("api_base") or None
    if api_base:
        os.environ["OPENAI_API_BASE"] = api_base
        os.environ["OPENAI_BASE_URL"] = api_base  # LiteLLM 兼容

    router = CRRouter(
        model=model,
        api_base=api_base,
        max_agent_concurrency=max(1, _get_int(llm_config, "max_agent_concurrency", 10)),
        timeout_seconds=_get_optional_int(llm_config, "timeout_seconds"),
        timeout_base_seconds=max(1, _get_int(llm_config, "timeout_base_seconds", 180)),
        timeout_per_1k_chars=max(0, _get_int(llm_config, "timeout_per_1k_chars", 1)),
        timeout_max_seconds=max(1, _get_int(llm_config, "timeout_max_seconds", 600)),
        max_retries=max(1, _get_int(llm_config, "max_retries", 3)),
        retry_delay=max(0.0, _get_float(llm_config, "retry_delay", 2.0)),
    )
    
    # Load Previous Review
    # Support: file path, JSON string, or markdown string
    previous_report_value = context.get("previous_report", "")
    previous_review = None
    if previous_report_value:
        # First, try as file path
        if os.path.exists(previous_report_value):
            with open(previous_report_value, "r", encoding="utf-8") as f:
                previous_review = json.load(f)
        else:
            # If not a file path, try parsing as JSON string
            try:
                previous_review = json.loads(previous_report_value)
            except (json.JSONDecodeError, TypeError):
                # If parsing fails, treat as markdown string and wrap it as summary
                # This allows previous_report to be a simple markdown string
                previous_review = {
                    "reports": {},
                    "summary": previous_report_value
                }
                print(f"ℹ️  previous_report is treated as markdown string (used as summary)")
    
    status = "success"
    result = {}
    md_output = ""
    line_comments_data = None
    issues_data = None
    summary_content = ""  # 初始化，避免在异常情况下未定义
    summary_usage: dict = {}

    try:
        # 执行流程
        result = await router.route_and_aggregate(
            mr_message=mr_message,
            code_diff=diff_content,
            requirements_content=requirements_content,
            previous_review=previous_review,
        )

        # 解析 Summary Agent 输出（可能是 JSON 或 Markdown）
        summary_content = result.get("summary", "")
        line_comments_data = None
        issues_data = None
        summary_usage = result.get("summary_usage", {}) if isinstance(result, dict) else {}
        summary_parse_failed = summary_usage.get("summary_parsed") is False

        # 优先使用统一解析（含 json\n 前缀、大括号提取等兜底）
        parsed_md, parsed_lc, parsed_issues = parse_summary_llm_output(summary_content)
        if parsed_md is not None:
            summary_content = parsed_md
            line_comments_data = parsed_lc or {}
            issues_data = parsed_issues or []
            print("✅ 成功解析 Summary Agent 输出为 JSON 格式")
        elif summary_parse_failed:
            status = "failure"
            error_msg = (
                "Summary Agent 输出在规范化解析后仍无效（已重试 2 次）。"
                "请查看 run.log 中的原始输出。"
            )
            print(f"❌ {error_msg}")
            summary_content = error_msg
        else:
            # 尝试解析为 YAML（兼容旧输出）
            parsed_as_yaml = False
            yaml_text = summary_content.strip()
            if yaml_text.startswith("```yaml"):
                yaml_text = yaml_text[7:]
            elif yaml_text.startswith("```yml"):
                yaml_text = yaml_text[6:]
            elif yaml_text.startswith("```"):
                yaml_text = yaml_text[3:]
            if yaml_text.endswith("```"):
                yaml_text = yaml_text[:-3]
            yaml_text = yaml_text.strip()
            
            if yaml:
                try:
                    summary_yaml = yaml.safe_load(yaml_text)
                    if isinstance(summary_yaml, dict) and "markdown_report" in summary_yaml:
                        markdown_report = summary_yaml.get("markdown_report", "")
                        line_comments_data = summary_yaml.get("line_comments", {})
                        issues_data = summary_yaml.get("issues", [])
                        print("✅ 成功解析 Summary Agent 输出为 YAML 格式")
                        
                        if markdown_report.startswith("```markdown"):
                            markdown_report = markdown_report[11:]
                        elif markdown_report.startswith("```"):
                            markdown_report = markdown_report[3:]
                        if markdown_report.endswith("```"):
                            markdown_report = markdown_report[:-3]
                        summary_content = markdown_report.strip()
                        parsed_as_yaml = True
                except Exception as yaml_error:
                    print(f"⚠️ Summary YAML 解析失败: {yaml_error}")
            
            if not parsed_as_yaml:
                # 回退到旧格式（仅 Markdown）
                print("⚠️ Summary 输出不是 JSON/YAML，使用旧格式（纯 Markdown）")
                # 去掉 summary 中的 markdown 代码块标记
                if summary_content.startswith("```markdown"):
                    summary_content = summary_content[11:]
                elif summary_content.startswith("```"):
                    summary_content = summary_content[3:]
                if summary_content.endswith("```"):
                    summary_content = summary_content[:-3]
                summary_content = summary_content.strip()

        # 验证和修正 line_comments（如果存在）
        validated_comments = []
        original_commit = None  # 在外部定义，确保在 except 块外也能访问
        if line_comments_data and isinstance(line_comments_data, dict):
            comments = line_comments_data.get("comments", [])
            if isinstance(comments, list):
                # 切换到 head_sha 对应的 commit（如果 project_root 是 git 仓库）
                if head_sha and project_root and os.path.exists(project_root):
                    git_dir = os.path.join(project_root, ".git")
                    if os.path.exists(git_dir) or subprocess.run(
                        ["git", "-C", project_root, "rev-parse", "--git-dir"],
                        capture_output=True,
                        check=False
                    ).returncode == 0:
                        try:
                            # 保存当前 commit
                            git_result = subprocess.run(
                                ["git", "-C", project_root, "rev-parse", "HEAD"],
                                capture_output=True,
                                text=True,
                                check=False
                            )
                            if git_result.returncode == 0:
                                original_commit = git_result.stdout.strip()
                            
                            # 切换到 head_sha
                            print(f"🔄 切换到 commit: {head_sha}")
                            git_result = subprocess.run(
                                ["git", "-C", project_root, "checkout", head_sha],
                                capture_output=True,
                                text=True,
                                check=False
                            )
                            if git_result.returncode != 0:
                                print(f"⚠️ 无法切换到 commit {head_sha}: {git_result.stderr}")
                            else:
                                print(f"✅ 已切换到 commit: {head_sha}")
                        except Exception as e:
                            print(f"⚠️ Git checkout 失败: {e}")
                
                print(f"🔍 正在验证 {len(comments)} 个行评论...")
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    
                    # 验证评论
                    validated = validate_line_comment_by_file(
                        comment=comment,
                        project_root=project_root,
                        expert_reports=result.get("reports", {}),
                        diff_content=diff_content
                    )
                    
                    comment_status = validated.get("validation_status", "valid")
                    
                    # 如果验证失败，尝试使用 feedback 机制自动修正
                    if comment_status in ["invalid", "needs_review"]:
                        from .utils import generate_line_number_feedback, correct_line_number_with_feedback
                        
                        # 生成验证反馈
                        feedback = generate_line_number_feedback(
                            comment=comment,
                            project_root=project_root,
                            diff_content=diff_content,
                            validation_result=validated
                        )
                        
                        if feedback:
                            print(f"📝 生成验证反馈:\n{feedback[:200]}...")
                            
                            # 尝试自动修正
                            corrected = correct_line_number_with_feedback(
                                comment=comment,
                                project_root=project_root,
                                diff_content=diff_content,
                                validation_feedback=feedback
                            )
                            
                            if corrected:
                                validated = corrected
                                comment_status = "corrected"
                                print(f"✅ 通过反馈机制自动修正行号: {comment.get('new_path')}: {comment.get('start_line')}-{comment.get('end_line')} -> {corrected.get('start_line')}-{corrected.get('end_line')}")
                            else:
                                # 如果无法自动修正，记录反馈信息
                                validated["validation_feedback"] = feedback
                                if comment_status == "invalid":
                                    error_msg = validated.get("validation_error", "未知错误")
                                    print(f"⚠️ 无效评论已移除: {comment.get('new_path')}:{comment.get('start_line')} - {error_msg}")
                                    continue  # 跳过无效评论
                    
                    if comment_status == "corrected":
                        print(f"✅ 已修正行号 {validated.get('new_path')}: {validated.get('original_start_line')}-{validated.get('original_end_line')} -> {validated.get('start_line')}-{validated.get('end_line')}")
                    elif comment_status == "needs_review":
                        print(f"⚠️ 评论需要审核: {comment.get('new_path')}:{comment.get('start_line')}")
                    
                    # 保存前移除验证元数据
                    clean_comment = {k: v for k, v in validated.items() 
                                   if k not in ["validation_status", "original_start_line", "original_end_line", "validation_feedback", "validation_error"]}
                    
                    validated_comments.append(clean_comment)
                
                line_comments_data["comments"] = validated_comments
                print(f"✅ 已验证 {len(validated_comments)} 个行评论")
        
        # 恢复原来的 commit（如果之前切换过）- 移到 if 块外，确保总是执行
        if original_commit and project_root and os.path.exists(project_root):
            try:
                print(f"🔄 恢复原来的 commit: {original_commit}")
                subprocess.run(
                    ["git", "-C", project_root, "checkout", original_commit],
                    capture_output=True,
                    check=False
                )
            except Exception as e:
                print(f"⚠️ 恢复 commit 失败: {e}")

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
        # 确保 summary_content 有值，即使发生异常
        if not summary_content:
            summary_content = error_msg
        # 确保 md_output 有值
        if not md_output:
            md_output = f"# 代码审查执行失败\n\n{error_msg}"

    # 保存 result.json
    # 提取 token 使用信息（汇总所有 Agent 的统计）
    usage_info = result.get("usage", {}) if isinstance(result, dict) else {}
    tokens_consume = {
        "input_tokens": usage_info.get("prompt_tokens", 0),
        "output_tokens": usage_info.get("completion_tokens", 0),
        "cost": usage_info.get("cost", 0.0)
    }
    
    result_json = {
        "llm_result": summary_content,  # 使用解析后的 Markdown 内容，而不是原始 JSON 字符串
        "status": status,
        "log_path": log_path,
        "tokens_consume": tokens_consume
    }
    if status == "failure" and summary_usage.get("summary_parsed") is False:
        result_json["error"] = (
            "summary_parse_failed: Summary Agent 输出在 3 次请求后仍无法被规范化解析"
        )

    # 添加 line_comments（如果可用）
    if line_comments_data:
        result_json["line_comments"] = line_comments_data

    # 添加 issues（由 Summary Agent 按 summaryRule 评级后输出）
    if isinstance(issues_data, list):
        result_json["issues"] = issues_data

    with open(os.path.join(result_path, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result_json, f, indent=2, ensure_ascii=False)

    # 兼容旧路径：写入 result_path 目录，避免多实例并发写同一文件
    cr_report_path = os.path.join(result_path, "CR_REPORT.md")
    with open(cr_report_path, "w", encoding="utf-8") as f:
        f.write(md_output)

if __name__ == "__main__":
    asyncio.run(main())

