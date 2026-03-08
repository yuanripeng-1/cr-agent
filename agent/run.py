import asyncio
import json
import os
import subprocess
import sys
import toml
import yaml
import re
from agent.router import CRRouter
from agent.utils import parse_diff_file_paths, setup_log_redirection, validate_line_comment_by_file, parse_diff_line_ranges, filter_code_diff, annotate_diff_with_line_numbers

def clean_and_parse_yaml(text: str):
    """Robustly extract and parse YAML from LLM output."""
    if not text:
        return {}
    
    # 1. Handle error strings from call_llm
    if text.startswith("Error calling LLM:"):
        return {"raw_content": text, "error": "LLM_CALL_FAILED"}

    # 2. Extract content between ```yaml and ``` or just ``` and ```
    yaml_block_match = re.search(r'```(?:yaml)?\s*([\s\S]*?)```', text)
    if yaml_block_match:
        clean_text = yaml_block_match.group(1).strip()
    else:
        clean_text = text.strip()

    # 3. Parse YAML
    try:
        data = yaml.safe_load(clean_text)
        if isinstance(data, dict):
            return data
        return {"raw_content": text, "error": "NOT_A_DICTIONARY"}
    except yaml.YAMLError as e:
        return {"raw_content": text, "error": f"YAML_PARSE_ERROR: {str(e)}"}

def format_generic_section(title, data):
    """Simplified formatter: Hides perfect sections, focuses on issues."""
    if not isinstance(data, dict): return ""
    
    if "error" in data:
        # Hide errors unless critical to reduce noise, or log them shortly
        return f"### {title} (⚠️ Error)\n> {data.get('error')}\n"

    review = data.get('review', {})
    score = review.get('score', 100)
    
    # 策略1: 满分直接跳过，由 Summary 统一汇总
    if str(score) == '100':
        return ""

    md = f"### {title} ({score}/100)\n"
    
    comments = []
    suggestions = []
    
    possible_keys = ['findings', 'vulnerabilities', 'issues', 'violations', 'suggestions', 'gaps', 'risks', 'bottlenecks']
    for key in possible_keys:
        items = review.get(key)
        if items and isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    # 提取 Header & Desc
                    header = item.get('requirement_name') or item.get('category') or item.get('severity') or ''
                    desc = item.get('analysis') or item.get('description') or item.get('comment') or item.get('risk') or item.get('type') or item.get('issue')
                    
                    # 策略2: 简化引用，仅 Critical 问题显示引用
                    ref = item.get('requirement_reference')
                    ref_str = ""
                    confidence = item.get('confidence', 0)
                    if ref and confidence >= 90:
                        ref_str = f"\n  > *Ref: {ref.strip()[:100]}...*" # 截断长引用

                    if header:
                        comments.append(f"- **{header}**: {desc}{ref_str}")
                    else:
                        comments.append(f"- {desc}{ref_str}")
                    
                    # 策略3: 仅展示 High Confidence 的代码建议
                    sugg = item.get('code_suggestion')
                    if isinstance(sugg, dict) and sugg.get('improved_code') and confidence >= 85:
                        suggestions.append({
                            "header": header,
                            "existing": sugg.get('existing_code', '').strip(),
                            "improved": sugg.get('improved_code', '').strip()
                        })
                else:
                    comments.append(f"- {item}")

    # 渲染评论
    if comments:
        md += "\n".join(comments) + "\n"
    elif review.get('analysis') or review.get('findings'):
        # Fallback for plain string analysis
        content = review.get('analysis') or review.get('findings')
        if len(content) > 200: 
            md += f"{content[:200]}...\n" # 截断过长分析
        else:
            md += f"{content}\n"

    # 渲染建议 (限制数量，最多 3 个)
    if suggestions:
        md += "\n**💡 Top Suggestions**\n"
        for i, s in enumerate(suggestions[:3], 1):
            md += f"**{i}. {s['header']}**\n"
            md += "```python\n"
            # 策略4: 如果 Existing 代码太长，只显示 Improved
            if len(s['existing'].split('\n')) > 5:
                 md += f"# ... existing code hidden ...\n\n# Improved\n{s['improved']}\n"
            else:
                 md += f"# Existing\n{s['existing']}\n\n# Improved\n{s['improved']}\n"
            md += "```\n"
            
    return md + "\n"

def format_summary_report(data):
    if isinstance(data, str): data = clean_and_parse_yaml(data)
    if not isinstance(data, dict): return str(data)
    
    # Check for errors in summary
    if "error" in data:
        return f"⚠️ Summary Generation Failed: {data['error']}\n\nRaw: {data.get('raw_content')}"

    final = data.get('final', {})
    decision = final.get('decision', 'Review').strip()
    score = final.get('overall_score', 'N/A')
    
    md = f"**Decision:** {decision} (Score: {score}/100)\n\n"
    md += f"**Rationale:**\n{final.get('rationale', '')}\n\n"
    
    blockers = final.get('blockers', [])
    if blockers:
        md += "**⛔ Blockers:**\n"
        for b in blockers:
            md += f"- {b}\n"
        md += "\n"
        
    return md

def render_from_summary_schema(summary_data: dict, overview: list[dict] = None) -> str | None:
    """
    Render final report from summary agent schema.
    Returns markdown string if schema is present/valid, otherwise None (caller should fallback).
    """
    if not isinstance(summary_data, dict) or "final" not in summary_data:
        return None
    final = summary_data.get("final", {})
    report = final.get("report", {})
    groups = report.get("groups") if isinstance(report, dict) else None
    if not isinstance(groups, list) or not groups:
        return None
        
    # Mapping for Dimension Group Display (Matches prompt/summary.md grouping)
    # Risk -> security, error_handling, dependency, performance
    # Delivery Quality -> business, testing, documentation
    # Maintainability -> readability, consistency, maintainability
    
    # We use fuzzy matching on group name to determine which dimensions to show
    dim_groups_map = {
        "Risk": ["security", "error_handling", "dependency", "performance"],
        "风险": ["security", "error_handling", "dependency", "performance"],
        "Delivery": ["business", "testing", "documentation"],
        "交付": ["business", "testing", "documentation"],
        "Maintainability": ["readability", "consistency", "maintainability"],
        "维护": ["readability", "consistency", "maintainability"]
    }

    # Helper to find dimensions for a group name
    def get_dims_for_group(g_name: str) -> list[str]:
        for k, v in dim_groups_map.items():
            if k in g_name:
                return v
        return []

    # Map overview list to dict for lookup: dim_key -> status_dict
    dim_status_map = {}
    if overview:
        # Reconstruct mapping based on known order or name
        # compute_dimension_status_overview returns specific order.
        # But wait, compute_dimension_status_overview items don't have the dim_key (e.g. 'security'), only 'name' (Chinese).
        # We need to rely on the Chinese name to map back to dim_key OR just map Chinese Name -> Status directly.
        # Let's map Chinese Name -> Status Dict
        for item in overview:
            dim_status_map[item["name"]] = item
            
    # Also need dim_key -> Chinese Name map to iterate
    dim_key_to_cn = {
        "business": "业务功能", "performance": "代码性能", "security": "安全性", 
        "testing": "测试", "documentation": "文档", "error_handling": "错误处理", 
        "readability": "可读性", "consistency": "一致性", "maintainability": "可维护性", 
        "dependency": "依赖管理"
    }

    decision = (final.get("decision") or "").strip()
    overall_score = final.get("overall_score", "N/A")
    rationale = final.get("rationale", "")
    blockers = final.get("blockers", []) or []
    passed_checks = final.get("passed_checks", []) or []

    # If summary agent didn't provide enough passed checks, pull from dimension overview
    if not passed_checks and overview:
        for it in overview:
            if "✅" in it["status"]:
                passed_checks.append(f"**{it['name']}**：{it['reason']}")

    md = "# 代码评审报告（Code Review Report）\n\n"
    md += "## 📋 结论概览\n"
    md += f"**决策：** {decision}（总分：{overall_score}/100）\n\n"
    md += f"**原因（Rationale）：**\n{rationale}\n\n"
    if blockers:
        md += "**⛔ 必须修改项（Blockers）：**\n"
        for b in blockers:
            md += f"- {b}\n"
        md += "\n"
    # Passed checks: always show section
    md += "## ✅ 检查通过项\n\n"
    if passed_checks:
        for p in passed_checks:
            md += f"- {p}\n"
    else:
        md += "- 暂无可确认的通过项（依据不足则不展示，避免误报）\n"
    md += "\n"

    md += "---\n\n"
    md += "## 🧩 评审摘要（Summary Agent 聚合输出）\n\n"

    prd_refs: list[str] = []

    for g in groups:
        if not isinstance(g, dict):
            continue
        name = (g.get("name") or "").strip() or "未命名分组"
        note = (g.get("note") or "").strip()
        items = g.get("items") if isinstance(g.get("items"), list) else []

        md += f"### {name}\n"
        
        # --- NEW: Inject Dimension Status for this Group ---
        target_dims = get_dims_for_group(name)
        if target_dims and dim_status_map:
            md += "**维度覆盖情况：**\n"
            for d_key in target_dims:
                cn_name = dim_key_to_cn.get(d_key, d_key)
                st = dim_status_map.get(cn_name)
                # Fallback: compute_dimension_status_overview uses slightly different names sometimes?
                # Let's double check compute_dimension_status_overview names.
                # It uses: "业务功能", "代码性能" (modified from "性能"), "安全性", "测试", "文档", "错误处理", "可读性", "一致性", "可维护性", "依赖管理"
                # My map above uses "代码性能" correctly.
                
                if st:
                    # Simplify: if reason is long, just show status
                    icon = "✅" if "通过" in st["status"] else "❌" if "需修改" in st["status"] else "⚠️"
                    reason_text = st["reason"]
                    
                    # New: If marked as Fail (❌) but summary agent didn't output items, 
                    # we must append fallback issues from 'st["issues"]'
                    fallback_issues_md = ""
                    # Check if items list is empty for this group? 
                    # Items are accumulated in 'items' list from summary report.
                    # We can't easily check if THIS specific dimension has items in the summary report 
                    # because summary items are category-based, not dimension-based.
                    # Heuristic: If status is ❌, show the top issue from raw report as a sub-bullet immediately.
                    
                    if "❌" in icon and st.get("issues"):
                         # Pick the first issue as representative
                         top_issue = st["issues"][0]
                         cat = top_issue.get("category", "Issue")
                         desc = top_issue.get("comment", "")
                         if len(desc) > 60: desc = desc[:60] + "..."
                         reason_text = f"发现高置信度问题：{cat} - {desc}"

                    md += f"- {icon} **{st['name']}**：{reason_text}\n"
            md += "\n"
        # ---------------------------------------------------

        if note:
            md += f"- {note}\n\n"
        if not items:
            # Only show "暂未发现" if we DID NOT find issues in the dimensions above.
            # If we showed ❌ in dimension status, we should NOT say "暂未发现".
            
            any_dim_failed = False
            if target_dims and dim_status_map:
                for d_key in target_dims:
                    cn_name = dim_key_to_cn.get(d_key, d_key)
                    st = dim_status_map.get(cn_name)
                    if st and "需修改" in st["status"]:
                        any_dim_failed = True
                        break
            
            if not any_dim_failed:
                if not note:
                    md += "- 暂未发现高置信度问题\n\n"
            else:
                # We have failed dimensions but Summary Agent returned no items.
                # The dimension status block above already shows the details.
                # So we just close the section.
                pass
                
            md += "---\n\n"
            continue

        md += "**评论（仅高置信度）**\n\n"
        suggestions_md = ""

        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                continue
            category = (it.get("category") or "").strip()
            conf = it.get("confidence_0_100", "")
            impacted = (it.get("impacted_dimensions") or "").strip()
            comment = (it.get("comment") or "").strip()
            evidence = (it.get("evidence") or "").strip()
            evidence_code = (it.get("evidence_code") or "").strip()
            req_ref = (it.get("requirement_reference") or "").strip()

            md += f"{i}. **{category}**（置信度：{conf}；影响：{impacted}）\n"
            if comment:
                md += f"   - {comment}\n"
            if evidence:
                md += f"   - 证据（归纳）：\n{indent_multiline(evidence, 4)}\n"
            if evidence_code:
                md += "   - 证据代码：\n"
                md += "```python\n"
                md += f"{evidence_code}\n"
                md += "```\n"
            if req_ref:
                md += f"   - 需求引用（PRD）：{req_ref}\n"
                prd_refs.append(req_ref)
            md += "\n"

            sugg = it.get("suggestion", {}) if isinstance(it.get("suggestion"), dict) else {}
            before_code = (sugg.get("before_code") or "").strip()
            after_code = (sugg.get("after_code") or "").strip()
            if before_code or after_code:
                suggestions_md += f"{i}. **{category}**\n\n"
                suggestions_md += "```python\n"
                if before_code:
                    suggestions_md += f"# 修改前（Before）\n{before_code}\n\n"
                if after_code:
                    suggestions_md += f"# 修改后（After）\n{after_code}\n"
                suggestions_md += "```\n\n"

        if suggestions_md:
            md += "<details><summary>💡 修改建议（Before/After）</summary>\n\n"
            md += suggestions_md
            md += "</details>\n\n"

        md += "---\n\n"

    # PRD references summary (where requirements are "aggregated")
    uniq_refs: list[str] = []
    for r in prd_refs:
        r = r.strip()
        if r and r not in uniq_refs:
            uniq_refs.append(r)
    md += "## 📎 需求归纳（PRD引用汇总）\n\n"
    if uniq_refs:
        for r in uniq_refs:
            md += f"- {r}\n"
    else:
        md += "- 本次报告未引用 PRD（可能因为 diff 信息不足或未触发需求校验项）\n"
    md += "\n"

    return md

def indent_multiline(text: str, indent_spaces: int) -> str:
    prefix = " " * indent_spaces
    return "\n".join(prefix + line for line in text.splitlines())

def compute_dimension_status_overview(reports: dict, confidence_threshold: int = 85) -> list[dict]:
    """
    Best-effort derive 10-dimension status from raw expert reports.
    Returns list of dict: {name, status, reason, issues: [list of issue dicts]}
    """
    dim_order = [
        ("business", "业务功能"),
        ("performance", "代码性能"),
        ("security", "安全性"),
        ("testing", "测试"),
        ("documentation", "文档"),
        ("error_handling", "错误处理"),
        ("readability", "可读性"),
        ("consistency", "一致性"),
        ("maintainability", "可维护性"),
        ("dependency", "依赖管理"),
    ]

    possible_list_keys = ["findings", "vulnerabilities", "issues", "violations", "gaps", "risks", "bottlenecks", "suggestions"]

    overview: list[dict] = []

    for dim_key, dim_name in dim_order:
        raw = reports.get(dim_key, "")
        parsed = clean_and_parse_yaml(raw)

        if not isinstance(parsed, dict) or "error" in parsed:
            overview.append({
                "name": dim_name,
                "status": "⚠️ 需人工确认",
                "reason": "该维度输出解析/调用失败，建议查看 CR_RESULT.json 原始输出",
                "issues": []
            })
            continue

        review = parsed.get("review", {}) if isinstance(parsed.get("review", {}), dict) else {}

        high_conf_issues = []
        for k in possible_list_keys:
            val = review.get(k)
            if isinstance(val, list):
                for item in val:
                    if not isinstance(item, dict):
                        continue
                    conf = item.get("confidence", item.get("confidence_0_100", 0))
                    try:
                        conf_i = int(conf)
                    except Exception:
                        conf_i = 0
                    if conf_i >= confidence_threshold:
                        # Extract minimal issue info for fallback display
                        # Improved fallback extraction to avoid "未知问题描述"
                        raw_comment = item.get("comment") or item.get("description") or item.get("analysis") or item.get("finding") or item.get("issue")
                        if not raw_comment and item.get("suggestion"):
                            # If it's a suggestion object
                            sugg = item.get("suggestion")
                            if isinstance(sugg, dict):
                                raw_comment = f"建议修改: {sugg.get('improved_code', '')[:40]}..."
                            elif isinstance(sugg, str):
                                raw_comment = f"建议: {sugg[:40]}..."
                        
                        issue_info = {
                            "category": item.get("category") or item.get("requirement_name") or k,
                            "comment": raw_comment or "存在未描述的高风险问题",
                            "confidence": conf_i
                        }
                        high_conf_issues.append(issue_info)

        if high_conf_issues:
            # Pick top 1-2 unique issues to avoid flooding
            overview.append({
                "name": dim_name,
                "status": "❌ 需修改",
                "reason": f"发现 {len(high_conf_issues)} 个高置信度问题",
                "issues": high_conf_issues
            })
        else:
            overview.append({
                "name": dim_name,
                "status": "✅ 通过",
                "reason": "未发现高置信度问题",
                "issues": []
            })

    return overview

async def run_agent():
    # Load config.toml from environment variable or default path
    config_path = os.environ.get("CR_AGENT_CONFIG", "config.toml")
    config = toml.load(config_path)
    context_cfg = config.get("context", {})
    result_path = context_cfg.get("result_path", ".")
    os.makedirs(result_path, exist_ok=True)
    log_path = os.path.join(result_path, "run.log")
    log_file = setup_log_redirection(log_path)

    status = "success"
    result = {}
    md_output = ""

    try:
        json_path = context_cfg.get("json_path", "context.json")
        # Handle relative path for json_path
        if not os.path.isabs(json_path):
            # If json_path starts with ./, it's relative to project root
            # Otherwise, it's relative to config file directory
            if json_path.startswith("./"):
                # Get project root (assume it's the directory containing agent/ folder)
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                json_path = os.path.join(project_root, json_path[2:])  # Remove leading ./
            else:
                json_path = os.path.join(os.path.dirname(config_path), json_path)
        
        with open(json_path, "r") as f:
            context = json.load(f)
        
        mr_message = f"Title: {context['title']}\nDescription: {context['description']}"
        code_diff = context.get("diff_content", "")
        
        # 过滤 diff，只保留代码文件的修改
        original_diff_length = len(code_diff)
        code_diff = filter_code_diff(code_diff)
        filtered_diff_length = len(code_diff)
        
        if original_diff_length != filtered_diff_length:
            print(f"🔍 已过滤 diff：原始长度 {original_diff_length} 字符 -> 代码文件长度 {filtered_diff_length} 字符")
        
        # 在 diff 中添加实际行号注释，帮助 Agent 准确识别行号
        project_root = context.get("project_root", ".")
        if project_root and os.path.exists(project_root):
            # 处理相对路径
            if not os.path.isabs(project_root):
                if project_root.startswith("./"):
                    base_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    project_root = os.path.join(base_project_root, project_root[2:])
                else:
                    project_root = os.path.join(os.path.dirname(config_path), project_root)
            project_root = os.path.abspath(project_root)
            
            code_diff = annotate_diff_with_line_numbers(code_diff, project_root)
            print(f"✅ 已在 diff 中添加实际行号注释")
        
        file_paths = parse_diff_file_paths(code_diff)
        
        # Load Previous Review
        # Support: file path, JSON string, or markdown string
        previous_report_value = context.get("previous_report", "")
        previous_review = None
        if previous_report_value:
            # First, try as file path
            if os.path.exists(previous_report_value):
                with open(previous_report_value, "r") as f:
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
        
        # Load Requirements
        requirements_path = config["project"].get("requirements_path", "")
        requirements_content = ""
        if requirements_path and os.path.exists(requirements_path):
            with open(requirements_path, "r") as f: requirements_content = f.read()
        
        # Env Setup（api_key 可选，私有化如 Ollama 可不填）
        llm_config = config.get("llm", {})
        api_key = llm_config.get("api_key") or os.environ.get("OPENAI_API_KEY") or ""
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
            os.environ["OPENROUTER_API_KEY"] = api_key
        api_base = llm_config.get("api_base") or None
        if api_base:
            os.environ["OPENAI_API_BASE"] = api_base
            os.environ["OPENAI_BASE_URL"] = api_base
        router = CRRouter(model=llm_config.get("model", "gpt-4"), api_base=api_base)
        
        result = await router.route_and_aggregate(
            mr_message=mr_message, 
            code_diff=code_diff, 
            file_paths=file_paths,
            project_root=context.get("project_root", "."),
            language=config["project"].get("language", "python"),
            guidelines_path=config["project"].get("guidelines_path", ""),
            requirements_content=requirements_content,
            previous_review=previous_review
        )
        
    except Exception as e:
        status = "failure"
        import traceback
        error_details = traceback.format_exc()
        error_msg = f"❌ Error during CR execution: {str(e)}\n{error_details}"
        print(error_msg)
        if not result.get("summary"):
            result["summary"] = error_msg
    
    # Generate Markdown Report and extract line_comments (outside try block so line_comments_data is accessible)
    line_comments_data = None
    try:
        raw_summary = result.get("summary", "").strip()
        
        # Try to parse as JSON first (new format)
        try:
            # Remove markdown code fences if present
            json_text = raw_summary
            if json_text.startswith("```json"):
                json_text = json_text[7:]
            elif json_text.startswith("```"):
                json_text = json_text[3:]
            if json_text.endswith("```"):
                json_text = json_text[:-3]
            json_text = json_text.strip()
            
            # Try to parse as JSON
            summary_json = json.loads(json_text)
            if isinstance(summary_json, dict) and "markdown_report" in summary_json:
                md_output = summary_json.get("markdown_report", "")
                line_comments_data = summary_json.get("line_comments", {})
                print("✅ Successfully parsed Summary Agent output as JSON")
            else:
                raise ValueError("Not a valid summary JSON format")
        except (json.JSONDecodeError, ValueError) as e:
            # Fallback to old format (Markdown only)
            print(f"⚠️ Summary output is not JSON, using legacy format: {e}")
            # Clean markdown fences if present
            if raw_summary.startswith("```"):
                lines = raw_summary.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                md_output = "\n".join(lines).strip()
            else:
                md_output = raw_summary
            
            # Check if the output actually looks like Markdown (starts with # or similar)
            # If not, it might still be YAML
            if not md_output.startswith("#") and ("final:" in md_output or "decision:" in md_output):
                summary_data = clean_and_parse_yaml(md_output)
                overview = compute_dimension_status_overview(result.get("reports", {}), confidence_threshold=85)
                rendered = render_from_summary_schema(summary_data, overview=overview)
                if rendered:
                    md_output = rendered

        # Validate and fix line_comments if present
        validated_comments = []
        if line_comments_data and isinstance(line_comments_data, dict):
            comments = line_comments_data.get("comments", [])
            if isinstance(comments, list):
                # Switch to head_sha commit if project_root is a git repository
                original_commit = None
                project_root = context.get("project_root", ".")
                head_sha = context.get("head_sha", "")
                
                # Handle relative path for project_root
                if project_root and not os.path.isabs(project_root):
                    if project_root.startswith("./"):
                        base_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                        project_root = os.path.join(base_project_root, project_root[2:])
                    else:
                        project_root = os.path.join(os.path.dirname(config_path), project_root)
                if project_root:
                    project_root = os.path.abspath(project_root)
                
                if head_sha and project_root and os.path.exists(project_root):
                    # Check if it's a git repository
                    git_check = subprocess.run(
                        ["git", "-C", project_root, "rev-parse", "--git-dir"],
                        capture_output=True,
                        check=False
                    )
                    if git_check.returncode == 0:
                        try:
                            # Save current commit
                            git_result = subprocess.run(
                                ["git", "-C", project_root, "rev-parse", "HEAD"],
                                capture_output=True,
                                text=True,
                                check=False
                            )
                            if git_result.returncode == 0:
                                original_commit = git_result.stdout.strip()
                            
                            # Switch to head_sha
                            print(f"🔄 Switching to commit: {head_sha}")
                            git_result = subprocess.run(
                                ["git", "-C", project_root, "checkout", head_sha],
                                capture_output=True,
                                text=True,
                                check=False
                            )
                            if git_result.returncode != 0:
                                print(f"⚠️ Failed to checkout commit {head_sha}: {git_result.stderr}")
                            else:
                                print(f"✅ Switched to commit: {head_sha}")
                        except Exception as e:
                            print(f"⚠️ Git checkout failed: {e}")
                
                print(f"🔍 Validating {len(comments)} line comments...")
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    
                    # Validate the comment
                    validated = validate_line_comment_by_file(
                        comment=comment,
                        project_root=project_root,
                        expert_reports=result.get("reports", {}),
                        diff_content=context.get("diff_content", "")
                    )
                    
                    comment_status = validated.get("validation_status", "valid")
                    if comment_status == "invalid":
                        error_msg = validated.get("validation_error", "Unknown error")
                        print(f"⚠️ Invalid comment removed: {comment.get('new_path')}:{comment.get('start_line')} - {error_msg}")
                        continue  # Skip invalid comments
                    elif comment_status == "corrected":
                        print(f"✅ Corrected line numbers for {validated.get('new_path')}: {validated.get('original_start_line')}-{validated.get('original_end_line')} -> {validated.get('start_line')}-{validated.get('end_line')}")
                    elif comment_status == "needs_review":
                        print(f"⚠️ Comment needs review: {comment.get('new_path')}:{comment.get('start_line')}")
                    
                    # Remove validation metadata before saving
                    clean_comment = {k: v for k, v in validated.items() 
                                   if k not in ["validation_status", "original_start_line", "original_end_line"]}
                    
                    # 在 body 开头添加代码范围信息
                    start_line = clean_comment.get("start_line")
                    end_line = clean_comment.get("end_line")
                    if start_line and end_line:
                        if start_line == end_line:
                            range_info = f"问题代码范围：{start_line}"
                        else:
                            range_info = f"问题代码范围：{start_line}:{end_line}"
                        
                        body = clean_comment.get("body", "")
                        if body and not body.startswith("问题代码范围："):
                            clean_comment["body"] = f"{range_info}\n\n{body}"
                    
                    validated_comments.append(clean_comment)
                
                line_comments_data["comments"] = validated_comments
                print(f"✅ Validated {len(validated_comments)} line comments")
                
                # Restore original commit if we switched
                if original_commit and project_root and os.path.exists(project_root):
                    try:
                        print(f"🔄 Restoring original commit: {original_commit}")
                        subprocess.run(
                            ["git", "-C", project_root, "checkout", original_commit],
                            capture_output=True,
                            check=False
                        )
                    except Exception as e:
                        print(f"⚠️ Failed to restore commit: {e}")
                
                # Restore original commit if we switched
                if original_commit and project_root:
                    try:
                        print(f"🔄 Restoring original commit: {original_commit}")
                        subprocess.run(
                            ["git", "-C", project_root, "checkout", original_commit],
                            capture_output=True,
                            check=False
                        )
                    except Exception as e:
                        print(f"⚠️ Failed to restore commit: {e}")

        print(md_output)
        with open(os.path.join(result_path, "cr_result.md"), "w") as f: 
            f.write(md_output)
    except Exception as e:
        # If parsing fails, use raw summary as fallback
        print(f"⚠️ Error parsing summary output: {e}")
        md_output = result.get("summary", "")
        with open(os.path.join(result_path, "cr_result.md"), "w") as f: 
            f.write(md_output)

    # Save result.json with the requested fields
    # Use parsed markdown content instead of raw summary
    # 提取 token 使用信息（汇总所有 Agent 的统计）
    usage_info = result.get("usage", {}) if isinstance(result, dict) else {}
    tokens_consume = {
        "input_tokens": usage_info.get("prompt_tokens", 0),
        "output_tokens": usage_info.get("completion_tokens", 0),
        "cost": usage_info.get("cost", 0.0)
    }
    
    result_json = {
        "llm_result": md_output if md_output else result.get("summary", ""),
        "status": status,
        "log_path": log_path,
        "tokens_consume": tokens_consume
    }
    
    # Add line_comments if available
    if line_comments_data:
        result_json["line_comments"] = line_comments_data
    
    with open(os.path.join(result_path, "result.json"), "w") as f:
        json.dump(result_json, f, indent=2, ensure_ascii=False)
    
    # Also save the full result for reference
    with open(os.path.join(result_path, "CR_RESULT.json"), "w") as f: 
        json.dump(result, f, indent=2, ensure_ascii=False)
    
    return

    # 2) Fallback: existing hard-coded aggregation renderer (robust)
    # [Rest of the code will be unreachable, but we can clean it up later if this works well]
    
    def extract_comments_and_suggestions(parsed: dict):
        """
        Extract compact comments + suggestions from a single dimension report YAML dict.
        Returns: (score, comments[list[dict]], suggestions[list[dict]])
        """
        if not isinstance(parsed, dict):
            return ("N/A", [], [])
        if "error" in parsed:
            # Do not leak YAML parser internals into the report; keep it user-friendly.
            err = str(parsed.get("error") or "")
            if err.startswith("YAML_PARSE_ERROR"):
                return ("N/A", ["⚠️ 该维度输出解析失败（已降噪隐藏），详见 CR_RESULT.json 原始输出。"], [])
            if err in ("LLM_CALL_FAILED",):
                return ("N/A", ["⚠️ 该维度请求失败（可能限流/网络/认证），详见 CR_RESULT.json 原始输出。"], [])
            return ("N/A", ["⚠️ 该维度输出异常（已降噪隐藏），详见 CR_RESULT.json 原始输出。"], [])
        review = parsed.get("review", {}) if isinstance(parsed.get("review", {}), dict) else {}
        score = review.get("score", "N/A")

        CONFIDENCE_THRESHOLD = 85

        comments: list[dict] = []
        suggestions: list[dict] = []

        possible_keys = ["findings", "vulnerabilities", "issues", "violations", "gaps", "risks", "bottlenecks", "analysis"]
        for key in possible_keys:
            val = review.get(key)
            if isinstance(val, str) and val.strip():
                comments.append(val.strip())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        # Hard filter: only show high-confidence items
                        confidence = item.get("confidence", None)
                        try:
                            confidence_int = int(confidence) if confidence is not None else 0
                        except Exception:
                            confidence_int = 0
                        if confidence_int < CONFIDENCE_THRESHOLD:
                            continue

                        header = item.get("requirement_name") or item.get("category") or item.get("severity") or item.get("type") or item.get("category") or "Issue"
                        desc = item.get("analysis") or item.get("description") or item.get("comment") or item.get("risk") or item.get("issue") or ""
                        header = (str(header) if header is not None else "Issue").replace("\n", " ").strip()
                        desc = desc.strip() if isinstance(desc, str) else str(desc)
                        desc = desc.replace("\n", " ").strip()
                        before_code = ""
                        sugg = item.get("code_suggestion")
                        if isinstance(sugg, dict):
                            before_code = (sugg.get("existing_code") or "").strip()
                            improved = (sugg.get("improved_code") or "").strip()
                            if improved:
                                suggestions.append({
                                    "title": header,
                                    "existing": before_code,
                                    "improved": improved,
                                    "confidence": confidence_int,
                                })

                        comments.append({
                            "title": header,
                            "text": desc,
                            "before": before_code,
                            "confidence": confidence_int,
                        })

                    elif isinstance(item, str) and item.strip():
                        comments.append({"title": "Issue", "text": item.strip(), "before": ""})

        return (score, comments, suggestions)

    GROUPS = {
        "风险（Risk）": ["security", "error_handling", "dependency", "performance"],
        "交付质量（Delivery Quality）": ["business", "testing", "documentation"],
        "可维护性（Maintainability）": ["readability", "consistency", "maintainability"],
    }

    DIM_DISPLAY = {
        "business": "业务功能",
        "performance": "性能",
        "security": "安全",
        "testing": "测试",
        "documentation": "文档",
        "error_handling": "错误处理",
        "readability": "可读性",
        "consistency": "一致性",
        "maintainability": "可维护性",
        "dependency": "依赖管理",
    }

    # Parse all dimension reports once
    parsed_reports: dict[str, dict] = {}
    for dim, content in reports.items():
        parsed_reports[dim] = clean_and_parse_yaml(content)
        
    # Pre-compute status per dimension (to be shown under group headers)
    # We reuse compute_dimension_status_overview logic but access it via dict for easy lookup
    dim_status_map = {}
    overview_list = compute_dimension_status_overview(reports, confidence_threshold=85)
    for item in overview_list:
        # map display name back to key? 
        # Actually compute_dimension_status_overview returns 'name' as display name (e.g. "业务功能").
        # We need to map dim_key -> status/reason.
        # Let's just re-run the logic slightly or use the name matching.
        pass

    # Better: just use the overview list directly if we can map back to group.
    # The GROUPS dict maps GroupName -> [dim_keys].
    # compute_dimension_status_overview returns list of dicts with 'name' (Chinese).
    # We need a map from dim_key to that status dict.
    
    # Re-build a map: dim_key -> {status, reason}
    dim_key_to_status = {}
    # order in compute_dimension_status_overview matches this:
    dim_order_keys = ["business", "performance", "security", "testing", "documentation", "error_handling", "readability", "consistency", "maintainability", "dependency"]
    # The list returned by compute_dimension_status_overview is in the same order.
    
    for i, st in enumerate(overview_list):
        if i < len(dim_order_keys):
            dim_key_to_status[dim_order_keys[i]] = st

    md_output = "# Code Review Report\n\n"
    md_output += f"## 📋 Executive Summary\n{format_summary_report(summary_data)}\n\n"
    md_output += "---\n\n"

    # 3 big groups (scheme B)
    md_output += "## 🧩 Review Summary (合并维度展示)\n\n"
    for group_title, dims in GROUPS.items():
        md_output += f"### {group_title}\n"
        
        # 1. Show Dimension Status Table for this group
        md_output += "**维度覆盖情况：**\n"
        for dim in dims:
            st = dim_key_to_status.get(dim)
            if st:
                icon = "✅" if "通过" in st["status"] else "❌" if "需修改" in st["status"] else "⚠️"
                # Simplify reason if it's just "pass"
                reason = st["reason"]
                if "未发现" in reason and "置信度" in reason:
                     reason = "通过"
                
                md_output += f"- {icon} **{st['name']}**：{reason}\n"
        md_output += "\n"

        # Collect and dedupe high-confidence issues across dims to keep report short
        # We canonicalize common issue categories to prevent near-duplicate spam.
        issues_by_key: dict[str, dict] = {}
        suggestions_by_key: dict[str, dict] = {}

        def canonical_key(title: str, text: str) -> str:
            t = (title or "").lower()
            x = (text or "").lower()
            blob = f"{t} {x}"
            if "sql injection" in blob or "sql注入" in blob or "注入" in blob:
                return "SQL注入"
            if "silent failure" in blob or "静默" in blob or "except: pass" in blob or "except:" in blob:
                return "静默失败"
            if "n+1" in blob:
                return "N+1查询"
            if "resource leak" in blob or "泄漏" in blob or "close" in blob or "__del__" in blob:
                return "资源/连接泄漏"
            if "redundancy" in blob or "依赖" in blob or "library" in blob:
                return "依赖冗余/治理"
            if "points" in blob or "积分" in blob or "amount/5" in blob or "amount / 5" in blob:
                return "业务规则错误（积分）"
            # Map generic severity labels to a stable bucket (fallback)
            if (title or "").strip().upper() in ("HIGH", "CRITICAL"):
                return "其他高风险问题"
            return (title or "其他问题").strip()

        def _norm_text(s: str) -> str:
            s = (s or "").lower()
            # keep letters/numbers/chinese roughly, drop punctuation/extra whitespace
            s = re.sub(r"[\s\r\n\t]+", " ", s)
            s = re.sub(r"[^0-9a-z\u4e00-\u9fff\.\-\_\s]+", "", s)
            return s.strip()

        def _is_redundant(new_text: str, existing_texts: list[str]) -> bool:
            """
            True if new_text is near-duplicate of any existing_text by containment after normalization.
            """
            n = _norm_text(new_text)
            if not n:
                return True
            for e in existing_texts:
                en = _norm_text(e)
                if not en:
                    continue
                # containment / high overlap heuristic
                if n in en or en in n:
                    return True
            return False

        for dim in dims:
            parsed = parsed_reports.get(dim, {})
            score, comments, suggestions = extract_comments_and_suggestions(parsed)
            label = DIM_DISPLAY.get(dim, dim)

            # comments: list[dict] (high-confidence only) or short error strings
            for c in comments:
                if isinstance(c, str):
                    key = f"维度异常::{label}"
                    if key not in issues_by_key:
                        issues_by_key[key] = {
                            "title": "维度异常",
                            "texts": [c],
                            "dims": {label},
                            "confidence": 100,
                        }
                    continue

                title = (c.get("title") or "Issue").strip()
                text = (c.get("text") or "").strip()
                conf = int(c.get("confidence", 0) or 0)

                k = canonical_key(title, text)
                if k not in issues_by_key:
                    issues_by_key[k] = {
                        "title": k,
                        "texts": [text] if text else [],
                        "dims": {label},
                        "confidence": conf,
                    }
                else:
                    issues_by_key[k]["dims"].add(label)
                    # keep at most 2 non-redundant bullets per category to avoid repetition
                    if text and len(issues_by_key[k]["texts"]) < 2:
                        if not _is_redundant(text, issues_by_key[k]["texts"]):
                            issues_by_key[k]["texts"].append(text)
                    issues_by_key[k]["confidence"] = max(int(issues_by_key[k]["confidence"]), conf)

            # suggestions: dedupe by canonical category; keep the highest-confidence one per category
            for s in suggestions:
                after = (s.get("improved") or "").strip()
                if not after:
                    continue
                stitle = (s.get("title") or "Suggestion").strip()
                sconf = int(s.get("confidence", 0) or 0)
                existing = (s.get("existing") or "").strip()
                # Use title + before + after for category inference to avoid misclassification (e.g., SQL fix under points)
                skey = canonical_key(stitle, f"{existing}\n{after}")
                if skey not in suggestions_by_key or sconf > int(suggestions_by_key[skey].get("confidence", 0)):
                    suggestions_by_key[skey] = {
                        "title": skey,
                        "existing": existing,
                        "improved": after,
                        "dims": {label},
                        "confidence": sconf,
                    }
                else:
                    suggestions_by_key[skey]["dims"].add(label)

        issues = sorted(issues_by_key.values(), key=lambda x: int(x.get("confidence", 0)), reverse=True)
        suggestions_list = sorted(suggestions_by_key.values(), key=lambda x: int(x.get("confidence", 0)), reverse=True)

        if not issues and not suggestions_list:
            md_output += "- ✅ 暂未发现高置信度问题\n\n---\n\n"
            continue

        # Comments (deduped)
        md_output += "**Comments（仅高置信度，按类别聚合）**\n\n"
        for i, it in enumerate(issues, 1):
            dims_str = "、".join(sorted(it.get("dims", [])))
            title = it.get("title", "Issue")
            conf = it.get("confidence", 0)
            md_output += f"{i}. **{title}**（影响：{dims_str}；置信度：{conf}）\n"
            for t in it.get("texts", [])[:3]:
                if t:
                    t_short = t if len(t) <= 240 else (t[:240] + "…")
                    md_output += f"   - {t_short}\n"
            md_output += "\n"

        # Suggestions (deduped, in a single collapsible to keep front-end short)
        if suggestions_list:
            md_output += "<details><summary>💡 Suggestions（Before/After，对应高置信度问题，按类别去重）</summary>\n\n"
            for i, s in enumerate(suggestions_list, 1):
                dims_str = "、".join(sorted(s.get("dims", [])))
                md_output += f"{i}. **{s.get('title','Suggestion')}**（来自：{dims_str}；置信度：{s.get('confidence',0)}）\n\n"
                md_output += "```python\n"
                existing = (s.get("existing") or "").strip()
                improved = (s.get("improved") or "").strip()
                if existing:
                    md_output += f"# Before\n{existing}\n\n"
                md_output += f"# After\n{improved}\n"
                md_output += "```\n\n"
            md_output += "</details>\n\n"

        md_output += "---\n\n"

    # NOTE: do not add debug appendix in Markdown; keep it front-end friendly.

    print(md_output)
    with open("CR_RESULT.md", "w") as f: f.write(md_output)
    with open("CR_RESULT.json", "w") as f: json.dump(result, f, indent=2)

if __name__ == "__main__":
    asyncio.run(run_agent())
