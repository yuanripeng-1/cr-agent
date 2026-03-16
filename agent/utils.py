import re
import sys
import os
from typing import List, Dict, Tuple, Optional, Any

# 代码文件扩展名列表（需要 review 的文件）
CODE_FILE_EXTENSIONS = {
    # Python
    '.py', '.pyx', '.pyi',
    # Go
    '.go',
    # Java
    '.java',
    # JavaScript/TypeScript
    '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs',
    # C/C++
    '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx',
    # Rust
    '.rs',
    # PHP
    '.php', '.phtml',
    # Ruby
    '.rb',
    # Swift
    '.swift',
    # Kotlin
    '.kt', '.kts',
    # Scala
    '.scala',
    # C#
    '.cs',
    # Objective-C
    '.m', '.mm',
    # Shell
    '.sh', '.bash', '.zsh',
    # R
    '.r',
    # Lua
    '.lua',
    # Perl
    '.pl', '.pm',
    # Dart
    '.dart',
    # Elixir
    '.ex', '.exs',
    # Clojure
    '.clj', '.cljs', '.cljc',
    # Haskell
    '.hs',
    # Erlang
    '.erl', '.hrl',
    # OCaml
    '.ml', '.mli',
    # F#
    '.fs', '.fsi', '.fsx',
    # Groovy
    '.groovy', '.gvy',
    # Makefile
    'Makefile',
}

# 非代码文件扩展名（明确排除的文件）
NON_CODE_FILE_EXTENSIONS = {
    # 文档
    '.md', '.txt', '.rst', '.adoc', '.org',
    # 配置文件
    '.json', '.yaml', '.yml', '.toml', '.ini', '.conf', '.properties', '.xml',
    '.lock', '.log', '.diff', '.patch',
    # 图片
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp',
    # 字体
    '.ttf', '.otf', '.woff', '.woff2',
    # 数据文件
    '.csv', '.tsv', '.xlsx', '.xls',
    # 其他
    '.zip', '.tar', '.gz', '.pdf',
}

def is_code_file(file_path: str) -> bool:
    """
    判断文件是否为代码文件。
    
    Args:
        file_path: 文件路径（可以是相对路径或绝对路径）
        
    Returns:
        True 如果是代码文件，False 否则
    """
    if not file_path:
        return False
    
    # 提取文件名（去除路径）
    filename = os.path.basename(file_path)
    
    # 检查是否为 Makefile（无扩展名但需要 review）
    if filename in ('Makefile', 'makefile', 'GNUmakefile'):
        return True
    
    # 提取扩展名
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    
    # 如果扩展名为空，检查是否为常见的无扩展名脚本文件
    if not ext:
        # 检查文件是否在常见的脚本目录中
        if any(part in file_path.lower() for part in ['bin/', 'scripts/', 'script/']):
            return True
        return False
    
    # 明确排除非代码文件
    if ext in NON_CODE_FILE_EXTENSIONS:
        return False
    
    # 检查是否为代码文件扩展名
    if ext in CODE_FILE_EXTENSIONS:
        return True
    
    # 默认：如果不在排除列表中，且不在代码列表中，根据扩展名长度判断
    # 通常代码文件有较短的扩展名（1-4个字符）
    if len(ext) <= 4 and ext[0] == '.':
        # 可能是代码文件，但不在我们的列表中，保守处理：返回 True
        # 用户可以根据需要调整这个逻辑
        return True
    
    return False


def filter_code_diff(diff_content: str) -> str:
    """
    过滤 diff 内容，只保留代码文件的修改。
    
    支持两种 diff 格式：
    1. 标准格式：包含文件头（--- a/path, +++ b/path 或 diff --git）
    2. 简化格式：只有 hunk 内容（@@ -x,y +a,b @@），这种情况下无法过滤，返回原内容
    
    Args:
        diff_content: 原始 diff 内容
        
    Returns:
        过滤后的 diff 内容（只包含代码文件的修改）
    """
    if not diff_content:
        return ""
    
    # 检查是否有文件头（标准格式或 git diff 格式）
    has_file_headers = any(
        line.startswith('--- a/') or 
        line.startswith('+++ b/') or 
        line.startswith('diff --git')
        for line in diff_content.split('\n')
    )
    
    # 如果没有文件头，无法确定文件类型，返回原内容（保守策略）
    if not has_file_headers:
        print("⚠️  Diff 格式不完整（缺少文件头），无法过滤，保留所有内容")
        return diff_content
    
    lines = diff_content.split('\n')
    filtered_lines = []
    current_file = None
    in_code_file_block = False
    current_block_lines = []
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # 检查文件头：--- a/path 或 +++ b/path
        if line.startswith('--- a/'):
            # 保存之前的块（如果有）
            if in_code_file_block and current_block_lines:
                filtered_lines.extend(current_block_lines)
            
            # 重置状态
            current_block_lines = [line]
            in_code_file_block = False
            # 继续读取下一行（应该是 +++ b/path）
            i += 1
            if i < len(lines):
                next_line = lines[i]
                if next_line.startswith('+++ b/'):
                    file_path = next_line[6:].strip()  # 移除 '+++ b/'
                    if file_path == '/dev/null':
                        # 文件被删除，跳过
                        current_file = None
                        current_block_lines = []
                        in_code_file_block = False
                    else:
                        current_file = file_path
                        current_block_lines.append(next_line)
                        in_code_file_block = is_code_file(file_path)
                        if not in_code_file_block:
                            # 不是代码文件，清空当前块
                            current_block_lines = []
                else:
                    # 格式异常，保留当前行
                    current_block_lines.append(next_line)
            i += 1
            continue
        
        # 检查文件头：+++ b/path（如果没有 --- a/path 前缀）
        elif line.startswith('+++ b/'):
            # 保存之前的块（如果有）
            if in_code_file_block and current_block_lines:
                filtered_lines.extend(current_block_lines)
            
            file_path = line[6:].strip()  # 移除 '+++ b/'
            if file_path == '/dev/null':
                current_file = None
                current_block_lines = []
                in_code_file_block = False
            else:
                current_file = file_path
                current_block_lines = [line]
                in_code_file_block = is_code_file(file_path)
                if not in_code_file_block:
                    current_block_lines = []
            i += 1
            continue
        
        # 检查 diff --git 格式的文件头
        elif line.startswith('diff --git'):
            # 保存之前的块（如果有）
            if in_code_file_block and current_block_lines:
                filtered_lines.extend(current_block_lines)
            
            # 从 'diff --git a/path b/path' 中提取路径
            match = re.match(r'diff --git a/(.+?)\s+b/(.+?)(?:\s|$)', line)
            if match:
                file_path = match.group(2)  # 使用 b 侧（新文件）路径
                current_file = file_path
                current_block_lines = [line]
                in_code_file_block = is_code_file(file_path)
                if not in_code_file_block:
                    current_block_lines = []
            else:
                # 格式异常，保留当前行
                current_block_lines = [line]
                in_code_file_block = True  # 保守处理
            i += 1
            continue
        
        # 检查 "new file mode" 或 "deleted file mode" 行（非标准格式）
        elif line.startswith('new file mode') or line.startswith('deleted file mode') or line.startswith('old mode') or line.startswith('index '):
            # 这些行属于当前文件块的一部分，如果当前文件是代码文件则保留
            if in_code_file_block:
                current_block_lines.append(line)
            i += 1
            continue
        
        # 其他行：如果当前在代码文件块中，保留；否则跳过
        else:
            if in_code_file_block:
                current_block_lines.append(line)
            i += 1
    
    # 保存最后一个块
    if in_code_file_block and current_block_lines:
        filtered_lines.extend(current_block_lines)
    
    return '\n'.join(filtered_lines)


def annotate_diff_with_line_numbers(diff_content: str, project_root: str) -> str:
    """
    在 diff 的每一行新增代码（+ 行）前添加实际文件中的行号。
    这样 Agent 就能直接看到正确的行号，而不需要从 diff 格式推断。
    
    Args:
        diff_content: 原始 diff 内容
        project_root: 项目根目录
        
    Returns:
        添加了行号注释的 diff 内容
    """
    if not diff_content or not project_root:
        return diff_content
    
    lines = diff_content.split('\n')
    annotated_lines = []
    current_file = None
    current_file_lines = None
    hunk_new_start = 0
    new_line_counter = 0  # 当前 hunk 内的新行计数器
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # 检查文件头：+++ b/path/to/file
        if line.startswith('+++ b/'):
            current_file = line[6:].strip()  # 移除 '+++ b/'
            if current_file == '/dev/null':
                current_file = None
                current_file_lines = None
            else:
                # 读取实际文件内容
                full_path = os.path.join(project_root, current_file)
                if os.path.exists(full_path):
                    try:
                        with open(full_path, 'r', encoding='utf-8') as f:
                            current_file_lines = [l.rstrip() for l in f.readlines()]
                    except Exception:
                        current_file_lines = None
                else:
                    current_file_lines = None
            annotated_lines.append(line)
            i += 1
            continue
        
        # 检查 hunk 头：@@ -old_start,old_count +new_start,new_count @@
        elif line.startswith('@@'):
            match = re.search(r'@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@', line)
            if match:
                hunk_new_start = int(match.group(3))  # 新文件中的起始行号
                new_line_counter = 0  # 重置计数器
            annotated_lines.append(line)
            i += 1
            continue
        
        # 处理新增行（+ 开头的行）
        elif line.startswith('+') and not line.startswith('+++'):
            new_line_counter += 1
            actual_line_num = hunk_new_start + new_line_counter - 1
            
            # 如果文件存在，尝试验证行号是否正确
            if current_file_lines and actual_line_num > 0 and actual_line_num <= len(current_file_lines):
                # 验证：检查这一行的内容是否匹配
                code_content = line[1:].strip()  # 移除 '+' 前缀
                file_line = current_file_lines[actual_line_num - 1].strip()
                
                # 如果内容匹配，在行首添加行号前缀，格式：0438| + code
                if code_content == file_line or code_content in file_line or file_line in code_content:
                    annotated_lines.append(f"{actual_line_num:04d}| {line}")
                else:
                    # 内容不匹配，尝试在文件中搜索
                    found_line = None
                    for j, file_line_check in enumerate(current_file_lines):
                        if code_content == file_line_check.strip() or code_content in file_line_check:
                            found_line = j + 1
                            break
                    
                    if found_line:
                        annotated_lines.append(f"{found_line:04d}| {line}")
                    else:
                        # 找不到匹配，使用估算行号前缀
                        annotated_lines.append(f"~{actual_line_num:04d}| {line}")
            else:
                # 文件不存在或行号超出范围，使用估算行号前缀
                annotated_lines.append(f"~{actual_line_num:04d}| {line}")
            
            i += 1
            continue
        
        # 其他行保持不变
        else:
            annotated_lines.append(line)
            i += 1
    
    return '\n'.join(annotated_lines)


def parse_diff_file_paths(diff_content: str) -> List[str]:
    """
    Parses git diff content to extract changed file paths.
    Supports both standard unified diff format and git diff format.
    """
    file_paths = set()
    
    # Method 1: Look for '+++ b/path/to/file' (standard unified diff)
    pattern_plus = re.compile(r'^\+\+\+ b/(.+)$', re.MULTILINE)
    matches = pattern_plus.findall(diff_content)
    for match in matches:
        if match != '/dev/null':
            file_paths.add(match.strip())
    
    # Method 2: Look for 'diff --git a/path b/path' (git diff format, including non-standard)
    # This handles cases where there's no '+++ b/path' line
    pattern_git = re.compile(r'^diff --git a/(.+?)\s+b/(.+?)(?:\s|$)', re.MULTILINE)
    matches_git = pattern_git.findall(diff_content)
    for old_path, new_path in matches_git:
        # Use the new path (b side), or old path if new is /dev/null
        if new_path != '/dev/null':
            file_paths.add(new_path.strip())
        elif old_path != '/dev/null':
            file_paths.add(old_path.strip())
    
    return list(file_paths)

class Tee(object):
    def __init__(self, *files):
        self.files = files
    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()
    def flush(self):
        for f in self.files:
            f.flush()

def setup_log_redirection(log_path: str):
    """
    Redirects stdout and stderr to both console and a log file.
    """
    log_file = open(log_path, 'a', encoding='utf-8')
    sys.stdout = Tee(sys.stdout, log_file)
    sys.stderr = Tee(sys.stderr, log_file)
    return log_file
def parse_diff_line_ranges(diff_content: str) -> Dict[str, List[Tuple[int, int]]]:
    """
    Parses git diff content to build a mapping of file paths to valid line number ranges.
    
    Returns:
        Dict mapping file paths to list of (start_line, end_line) tuples.
        Each tuple represents a hunk in the new file (based on '+' lines).
    """
    result: Dict[str, List[Tuple[int, int]]] = {}
    current_file = None
    
    lines = diff_content.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Check for file header: +++ b/path/to/file
        if line.startswith('+++ b/'):
            current_file = line[6:].strip()  # Remove '+++ b/'
            if current_file == '/dev/null':
                current_file = None
            elif current_file not in result:
                result[current_file] = []
        
        # Check for hunk header: @@ -old_start,old_count +new_start,new_count @@
        elif line.startswith('@@') and current_file:
            # Parse: @@ -x,y +a,b @@
            match = re.search(r'@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@', line)
            if match:
                old_start = int(match.group(1))
                old_count = int(match.group(2)) if match.group(2) else 1
                new_start = int(match.group(3))
                new_count = int(match.group(4)) if match.group(4) else 1
                
                # Calculate the range in the new file
                new_end = new_start + new_count - 1
                if new_start > 0 and new_end >= new_start:
                    result[current_file].append((new_start, new_end))
        
        i += 1
    
    return result

def search_code_snippet_in_file(file_path: str, snippet: str, project_root: str) -> Optional[Tuple[int, int]]:
    """
    Searches for a code snippet in a file and returns the line number range.
    
    Args:
        file_path: Relative path to the file
        snippet: Code snippet to search for
        project_root: Root directory of the project
        
    Returns:
        Tuple of (start_line, end_line) if found, None otherwise.
        Returns the first match if multiple matches exist.
    """
    if not snippet or not snippet.strip():
        return None
    
    full_path = os.path.join(project_root, file_path)
    if not os.path.exists(full_path):
        return None
    
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            file_content = f.read()
            file_lines = file_content.split('\n')
    except Exception:
        return None
    
    # Clean the snippet: remove leading/trailing whitespace, normalize
    snippet_clean = snippet.strip()
    snippet_lines = [line.rstrip() for line in snippet_clean.split('\n')]
    
    # Try to find the snippet in the file
    # Strategy: find the first line, then verify the rest
    if not snippet_lines:
        return None
    
    first_line = snippet_lines[0]
    for i, file_line in enumerate(file_lines):
        if first_line in file_line or file_line.strip() == first_line.strip():
            # Found potential match, verify the rest
            if len(snippet_lines) == 1:
                return (i + 1, i + 1)  # Line numbers are 1-indexed
            
            # Check if subsequent lines match
            match = True
            for j, snippet_line in enumerate(snippet_lines[1:], start=1):
                if i + j >= len(file_lines):
                    match = False
                    break
                file_line_check = file_lines[i + j].rstrip()
                if snippet_line not in file_line_check and file_line_check.strip() != snippet_line.strip():
                    match = False
                    break
            
            if match:
                start_line = i + 1
                end_line = i + len(snippet_lines)
                return (start_line, end_line)
    
    return None

def generate_line_number_feedback(
    comment: Dict[str, Any],
    project_root: str,
    diff_content: str,
    validation_result: Dict[str, Any]
) -> Optional[str]:
    """
    生成行号验证反馈信息，用于帮助 LLM 重新推断正确的行号。
    
    Returns:
        Feedback string with validation details, or None if validation passed
    """
    validation_status = validation_result.get("validation_status", "valid")
    
    if validation_status == "valid":
        return None
    
    new_path = comment.get("new_path", "")
    start_line = comment.get("start_line", 0)
    end_line = comment.get("end_line", 0)
    body = comment.get("body", "")
    error_msg = validation_result.get("validation_error", "")
    
    feedback_parts = []
    feedback_parts.append(f"行号验证失败：文件 {new_path}，行号 {start_line}-{end_line}")
    feedback_parts.append(f"错误原因：{error_msg}")
    
    # 添加 diff 中的相关行号范围
    diff_ranges = parse_diff_line_ranges(diff_content)
    if new_path in diff_ranges:
        valid_ranges = diff_ranges[new_path]
        feedback_parts.append(f"Diff 中该文件的有效行号范围：{valid_ranges}")
    
    # 添加实际文件内容（如果文件存在）
    full_path = os.path.join(project_root, new_path) if project_root else new_path
    if os.path.exists(full_path):
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                file_lines = f.readlines()
            total_lines = len(file_lines)
            feedback_parts.append(f"文件总行数：{total_lines}")
            
            # 提取评论相关的代码上下文（前后各5行）
            context_start = max(0, start_line - 6)
            context_end = min(total_lines, end_line + 5)
            context_lines = file_lines[context_start:context_end]
            context_text = "".join([f"{context_start + i + 1:4d}| {line}" for i, line in enumerate(context_lines)])
            feedback_parts.append(f"相关代码上下文（行 {context_start + 1}-{context_end}）：\n{context_text}")
            
            # 尝试从 body 中提取代码片段，在文件中搜索
            if body:
                # 提取代码块中的代码
                import re
                code_blocks = re.findall(r'```(?:\w+)?\n(.*?)```', body, re.DOTALL)
                if code_blocks:
                    for code_block in code_blocks:
                        # 清理代码块
                        code_lines = [line.strip() for line in code_block.split('\n') if line.strip() and not line.strip().startswith('//')]
                        if code_lines:
                            # 在文件中搜索这些代码行
                            for i, file_line in enumerate(file_lines):
                                if code_lines[0].strip() in file_line:
                                    feedback_parts.append(f"在文件中找到相似代码，位于行 {i + 1}：{file_line.strip()}")
                                    break
        except Exception as e:
            feedback_parts.append(f"无法读取文件内容：{e}")
    
    return "\n".join(feedback_parts)


def correct_line_number_with_feedback(
    comment: Dict[str, Any],
    project_root: str,
    diff_content: str,
    validation_feedback: str
) -> Optional[Dict[str, Any]]:
    """
    使用反馈信息尝试自动修正行号。
    基于实际文件内容和 diff 信息进行智能匹配。
    
    Returns:
        Corrected comment dict with updated line numbers, or None if cannot correct
    """
    new_path = comment.get("new_path", "")
    body = comment.get("body", "")
    
    if not new_path or not body:
        return None
    
    full_path = os.path.join(project_root, new_path) if project_root else new_path
    if not os.path.exists(full_path):
        return None
    
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            file_content = f.read()
            file_lines = [line.rstrip() for line in file_content.split('\n')]
    except Exception:
        return None
    
    # 方法1: 从 body 中提取代码片段（修改建议中的代码）
    import re
    code_blocks = re.findall(r'```(?:\w+)?\n(.*?)```', body, re.DOTALL)
    
    # 方法2: 从 body 的描述中提取关键代码行（如 "aStr, _ := reader.ReadString"）
    # 提取可能的代码模式
    code_patterns = re.findall(r'`([^`]+)`', body)  # 提取反引号中的代码
    code_patterns.extend(re.findall(r'(\w+.*?:=.*?)', body))  # 提取赋值语句
    
    best_match = None
    best_match_score = 0
    
    # 尝试匹配代码块
    for code_block in code_blocks:
        # 清理代码块，提取关键代码行
        code_lines = [line.strip() for line in code_block.split('\n') 
                     if line.strip() and not line.strip().startswith('//') and not line.strip().startswith('#')]
        
        if not code_lines:
            continue
        
        # 在文件中搜索匹配
        for i in range(len(file_lines)):
            # 检查是否匹配第一行（更宽松的匹配）
            first_line_clean = code_lines[0].strip()
            file_line_clean = file_lines[i].strip()
            
            # 尝试多种匹配方式
            if (first_line_clean in file_line_clean or 
                file_line_clean in first_line_clean or
                first_line_clean == file_line_clean or
                # 提取关键标识符进行匹配
                any(keyword in file_line_clean for keyword in first_line_clean.split() if len(keyword) > 3)):
                
                # 验证后续行是否匹配
                match_score = 1
                all_match = True
                for j, code_line in enumerate(code_lines[1:], start=1):
                    if i + j >= len(file_lines):
                        all_match = False
                        break
                    code_line_clean = code_line.strip()
                    file_line_check = file_lines[i + j].strip()
                    if (code_line_clean in file_line_check or 
                        file_line_check in code_line_clean or
                        code_line_clean == file_line_check):
                        match_score += 1
                    else:
                        # 允许跳过空行和注释
                        if file_line_check and not file_line_check.startswith('//') and not file_line_check.startswith('#'):
                            # 如果关键标识符匹配，也算匹配
                            key_words = [w for w in code_line_clean.split() if len(w) > 3]
                            if key_words and any(kw in file_line_check for kw in key_words):
                                match_score += 0.5
                            else:
                                all_match = False
                                break
                
                if all_match and match_score > best_match_score:
                    best_match = (i + 1, i + len(code_lines))
                    best_match_score = match_score
    
    # 如果代码块匹配失败，尝试匹配单个代码模式
    if not best_match:
        for pattern in code_patterns:
            pattern_clean = pattern.strip()
            if len(pattern_clean) < 5:  # 太短的模式不可靠
                continue
            
            # 在文件中搜索这个模式
            for i, file_line in enumerate(file_lines):
                if pattern_clean in file_line or file_line.strip() == pattern_clean:
                    # 找到匹配，检查上下文是否合理
                    # 如果这是唯一匹配，使用它
                    best_match = (i + 1, i + 1)
                    best_match_score = 0.5  # 单行匹配分数较低
                    break
            if best_match:
                break
    
    # 如果找到匹配，更新行号
    if best_match:
        corrected_comment = comment.copy()
        corrected_comment["start_line"] = best_match[0]
        corrected_comment["end_line"] = best_match[1]
        corrected_comment["validation_status"] = "corrected"
        corrected_comment["original_start_line"] = comment.get("start_line")
        corrected_comment["original_end_line"] = comment.get("end_line")
        return corrected_comment
    
    return None


def validate_line_comment_by_file(
    comment: Dict[str, Any],
    project_root: str,
    expert_reports: Dict[str, Any],
    diff_content: str
) -> Dict[str, Any]:
    """
    Validates and potentially fixes a line comment by checking against actual file content.
    
    Args:
        comment: Line comment dict with new_path, start_line, end_line, body
        project_root: Root directory of the project
        expert_reports: Dictionary of expert reports (for extracting code snippets)
        diff_content: Git diff content for quick range validation
        
    Returns:
        Updated comment dict with validation results. May include:
        - needs_review: bool flag if validation failed
        - original_start_line, original_end_line: if corrected
        - validation_status: "valid" | "corrected" | "needs_review" | "invalid"
    """
    result = comment.copy()
    result.setdefault("validation_status", "valid")
    
    # Basic validation
    new_path = comment.get("new_path")
    start_line = comment.get("start_line")
    end_line = comment.get("end_line")
    
    if not new_path or not isinstance(start_line, int) or not isinstance(end_line, int):
        result["validation_status"] = "invalid"
        result["needs_review"] = True
        return result
    
    if start_line <= 0 or end_line < start_line:
        result["validation_status"] = "invalid"
        result["needs_review"] = True
        return result
    
    # Quick diff range validation
    diff_ranges = parse_diff_line_ranges(diff_content)
    if new_path in diff_ranges:
        valid_ranges = diff_ranges[new_path]
        in_range = any(
            start <= start_line <= end or start <= end_line <= end or
            (start_line <= start and end_line >= end)
            for start, end in valid_ranges
        )
        if not in_range:
            # Not in diff range, but continue to file validation
            pass
    
    # File existence check
    # Normalize project_root to absolute path if it's relative
    if project_root and not os.path.isabs(project_root):
        project_root = os.path.abspath(project_root)
    
    full_path = os.path.join(project_root, new_path) if project_root else new_path
    if not os.path.exists(full_path):
        # Try to find the file in git repository if it's a git repo
        if project_root and os.path.exists(project_root):
            try:
                import subprocess
                # Check if it's a git repository
                git_check = subprocess.run(
                    ["git", "-C", project_root, "rev-parse", "--git-dir"],
                    capture_output=True,
                    check=False
                )
                if git_check.returncode == 0:
                    # Try to find file by name in git
                    filename = os.path.basename(new_path)
                    git_result = subprocess.run(
                        ["git", "-C", project_root, "ls-files", "**/" + filename],
                        capture_output=True,
                        text=True,
                        check=False
                    )
                    if git_result.returncode == 0 and git_result.stdout.strip():
                        # Found file(s) with this name, use the first one
                        found_paths = [p.strip() for p in git_result.stdout.strip().split('\n') if p.strip()]
                        if found_paths:
                            # Use the first match, or try to find the one that matches the diff
                            # For now, use the first one
                            actual_path = found_paths[0]
                            print(f"🔍 Found file in git: {actual_path} (was looking for: {new_path})")
                            new_path = actual_path
                            full_path = os.path.join(project_root, actual_path)
                            result["original_path"] = comment.get("new_path")  # Save original path
                            result["new_path"] = actual_path  # Update to actual path
            except Exception as e:
                print(f"⚠️ Error searching for file in git: {e}")
        
        # Check again after git search
        if not os.path.exists(full_path):
            # Log detailed error for debugging
            print(f"⚠️ File not found for validation: {full_path} (project_root: {project_root}, new_path: {new_path})")
            result["validation_status"] = "invalid"
            result["needs_review"] = True
            result["validation_error"] = f"File not found: {full_path}"
            return result
    
    # Read file and check line range
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            file_lines = f.readlines()
        total_lines = len(file_lines)
        
        if start_line > total_lines or end_line > total_lines:
            result["validation_status"] = "needs_review"
            result["needs_review"] = True
            return result
    except Exception:
        result["validation_status"] = "needs_review"
        result["needs_review"] = True
        return result
    
    # Code snippet matching validation (if we can extract from expert reports)
    # Try to find code snippet in expert reports that matches this comment
    snippet = None
    for report_name, report_content in expert_reports.items():
        if isinstance(report_content, dict):
            # Try to find existing_code or evidence_code in the report
            for key in ['existing_code', 'evidence_code']:
                if key in str(report_content):
                    # Simple extraction - look for code blocks
                    import yaml
                    try:
                        if isinstance(report_content, str):
                            report_dict = yaml.safe_load(report_content)
                        else:
                            report_dict = report_content
                        
                        # Search in nested structures
                        def find_code_in_dict(d, target_key):
                            if isinstance(d, dict):
                                for k, v in d.items():
                                    if k == target_key and isinstance(v, str):
                                        return v
                                    if isinstance(v, (dict, list)):
                                        result = find_code_in_dict(v, target_key)
                                        if result:
                                            return result
                            elif isinstance(d, list):
                                for item in d:
                                    result = find_code_in_dict(item, target_key)
                                    if result:
                                        return result
                            return None
                        
                        snippet = find_code_in_dict(report_dict, key)
                        if snippet:
                            break
                    except Exception:
                        pass
                if snippet:
                    break
        if snippet:
            break
    
    # If we found a snippet, try to match it in the file
    if snippet:
        matched_range = search_code_snippet_in_file(new_path, snippet, project_root)
        if matched_range:
            matched_start, matched_end = matched_range
            # If the matched range is different from the comment's range, correct it
            if matched_start != start_line or matched_end != end_line:
                result["original_start_line"] = start_line
                result["original_end_line"] = end_line
                result["start_line"] = matched_start
                result["end_line"] = matched_end
                result["validation_status"] = "corrected"
                return result
        else:
            # Snippet not found in file - might be Agent hallucination
            result["validation_status"] = "needs_review"
            result["needs_review"] = True
            return result
    
    # All validations passed
    result["validation_status"] = "valid"
    return result
