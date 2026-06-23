import json
import re
import sys
import os
import traceback
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


class DiffContentFilter:
    """
    有状态的 diff 内容过滤器，逐行扫描并保留代码文件的修改块。
    支持 unified diff 与 git diff 两种格式的文件头识别。
    """

    def __init__(self):
        self.filtered_lines: List[str] = []
        self.current_block_lines: List[str] = []
        self.current_file: Optional[str] = None
        self.in_code_file_block: bool = False

    def _flush_block(self) -> None:
        if self.in_code_file_block and self.current_block_lines:
            self.filtered_lines.extend(self.current_block_lines)
        self.current_block_lines = []

    def _begin_block(self, header_lines: List[str], file_path: str) -> None:
        self._flush_block()
        self.current_file = file_path
        self.current_block_lines = list(header_lines)
        self.in_code_file_block = is_code_file(file_path)

    def _skip_block(self) -> None:
        self._flush_block()
        self.current_file = None
        self.current_block_lines = []
        self.in_code_file_block = False

    def _handle_minus_header(self, line: str, lines: List[str], index: int) -> int:
        self.current_block_lines = [line]
        next_index = index + 1
        if next_index < len(lines) and lines[next_index].startswith('+++ b/'):
            plus_line = lines[next_index]
            file_path = plus_line[6:].strip()
            if file_path == '/dev/null':
                self._skip_block()
            else:
                self.current_block_lines.append(plus_line)
                self.current_file = file_path
                self.in_code_file_block = is_code_file(file_path)
                if not self.in_code_file_block:
                    self.current_block_lines = []
            next_index += 1
        return next_index

    def _handle_plus_header(self, line: str) -> None:
        file_path = line[6:].strip()
        if file_path == '/dev/null':
            self._skip_block()
        else:
            self._flush_block()
            self.current_file = file_path
            self.current_block_lines = [line]
            if is_code_file(file_path):
                self.in_code_file_block = True
            if not is_code_file(file_path):
                self.current_block_lines = []

    def _handle_git_header(self, line: str) -> None:
        match = re.match(r'diff --git a/(.+?)\s+b/(.+?)(?:\s|$)', line)
        if match:
            file_path = match.group(2)
            self._flush_block()
            self.current_file = file_path
            self.current_block_lines = [line]
            if is_code_file(file_path):
                self.in_code_file_block = True
            if not is_code_file(file_path):
                self.current_block_lines = []
        else:
            self.current_block_lines = [line]
            self.in_code_file_block = True

    def _handle_metadata_line(self, line: str) -> None:
        if self.in_code_file_block:
            self.current_block_lines.append(line)

    def _handle_content_line(self, line: str) -> None:
        if self.in_code_file_block:
            self.current_block_lines.append(line)

    def process(self, diff_content: str) -> str:
        if not diff_content:
            return ""

        has_file_headers = any(
            line.startswith('--- a/') or
            line.startswith('+++ b/') or
            line.startswith('diff --git')
            for line in diff_content.split('\n')
        )
        if not has_file_headers:
            print("⚠️  Diff 格式不完整（缺少文件头），无法过滤，保留所有内容")
            return diff_content

        lines = diff_content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith('--- a/'):
                i = self._handle_minus_header(line, lines, i)
                continue
            elif line.startswith('+++ b/'):
                self._handle_plus_header(line)
                i += 1
                continue
            elif line.startswith('diff --git'):
                self._handle_git_header(line)
                i += 1
                continue
            elif (
                line.startswith('new file mode')
                or line.startswith('deleted file mode')
                or line.startswith('old mode')
                or line.startswith('index ')
            ):
                self._handle_metadata_line(line)
                i += 1
                continue
            else:
                self._handle_content_line(line)
                i += 1

        self._flush_block()
        return '\n'.join(self.filtered_lines)


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
    
    return DiffContentFilter().process(diff_content)


class DiffLineAnnotator:
    """
    为 diff 中的新增行（+ 行）添加实际文件行号前缀。
    支持多 hunk、内容校验与模糊匹配回退。
    """

    HUNK_PATTERN = re.compile(r'@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@')

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.annotated_lines: List[str] = []
        self.current_file: Optional[str] = None
        self.current_file_lines: Optional[List[str]] = None
        self.hunk_new_start: int = 0
        self.new_line_counter: int = 0

    def _load_file_lines(self, relative_path: str) -> Optional[List[str]]:
        full_path = os.path.join(self.project_root, relative_path)
        if not os.path.exists(full_path):
            return None
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                return [l.rstrip() for l in f.readlines()]
        except Exception:
            return None

    def _reset_hunk_state(self, new_start: int) -> None:
        self.hunk_new_start = new_start
        self.new_line_counter = 0

    def _compute_line_number(self) -> int:
        """根据 hunk 起始行与当前 hunk 内新增行计数计算实际行号。"""
        return self.hunk_new_start + self.new_line_counter

    def _lines_match(self, diff_content: str, file_line: str) -> bool:
        code_content = diff_content.strip()
        file_line_stripped = file_line.strip()
        return (
            code_content == file_line_stripped
            or code_content in file_line_stripped
            or file_line_stripped in code_content
        )

    def _search_line_in_file(self, code_content: str) -> Optional[int]:
        if not self.current_file_lines:
            return None
        for j, file_line_check in enumerate(self.current_file_lines):
            if code_content == file_line_check.strip() or code_content in file_line_check:
                return j + 1
        return None

    def _format_annotated_line(self, line: str, line_num: int, approximate: bool = False) -> str:
        prefix = f"~{line_num:04d}" if approximate else f"{line_num:04d}"
        return f"{prefix}| {line}"

    def _annotate_plus_line(self, line: str) -> str:
        self.new_line_counter += 1
        actual_line_num = self._compute_line_number()

        if (
            self.current_file_lines
            and actual_line_num > 0
            and actual_line_num <= len(self.current_file_lines)
        ):
            code_content = line[1:]
            file_line = self.current_file_lines[actual_line_num - 1]

            if self._lines_match(code_content, file_line):
                return self._format_annotated_line(line, actual_line_num)

            found_line = self._search_line_in_file(code_content.strip())
            if found_line:
                return self._format_annotated_line(line, found_line)

            return self._format_annotated_line(line, actual_line_num, approximate=True)

        return self._format_annotated_line(line, actual_line_num, approximate=True)

    def _handle_file_header(self, line: str) -> None:
        self.current_file = line[6:].strip()
        if self.current_file == '/dev/null':
            self.current_file = None
            self.current_file_lines = None
        else:
            self.current_file_lines = self._load_file_lines(self.current_file)
        self.annotated_lines.append(line)

    def _handle_hunk_header(self, line: str) -> None:
        match = self.HUNK_PATTERN.search(line)
        if match:
            self._reset_hunk_state(int(match.group(3)))
        self.annotated_lines.append(line)

    def annotate(self, diff_content: str) -> str:
        if not diff_content or not self.project_root:
            return diff_content

        lines = diff_content.split('\n')
        for line in lines:
            if line.startswith('+++ b/'):
                self._handle_file_header(line)
            elif line.startswith('@@'):
                self._handle_hunk_header(line)
            elif line.startswith('+') and not line.startswith('+++'):
                self.annotated_lines.append(self._annotate_plus_line(line))
            else:
                self.annotated_lines.append(line)

        return '\n'.join(self.annotated_lines)


def annotate_diff_with_line_numbers(diff_content: str, project_root: str) -> str:
    """
    在 diff 的每一行新增代码（+ 行）前添加实际文件中的行号。
    这样 Agent 就能直接看到正确的行号，而不需要从 diff 格式推断。
    """
    return DiffLineAnnotator(project_root).annotate(diff_content)


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

class RunLog:
    """Structured run.log writer: sub-agent outputs, summary output, model errors only."""

    _path: Optional[str] = None

    @classmethod
    def init(cls, log_path: str) -> None:
        cls._path = log_path
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("十个子 Agent 处理方式完全相同\n\n")

    @classmethod
    def _append(cls, text: str) -> None:
        if not cls._path:
            return
        with open(cls._path, "a", encoding="utf-8") as f:
            f.write(text)
            if text and not text.endswith("\n"):
                f.write("\n")

    @classmethod
    def write_agent_output(cls, agent: str, content: str) -> None:
        cls._append(
            f"===== BEGIN AGENT OUTPUT: {agent} =====\n"
            f"{content}\n"
            f"===== END AGENT OUTPUT: {agent} =====\n"
        )

    @classmethod
    def write_agent_error(cls, agent: str, exc: BaseException) -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        cls._append(
            f"===== BEGIN AGENT ERROR: {agent} =====\n"
            f"exception_type: {type(exc).__name__}\n"
            f"exception_repr: {repr(exc)}\n"
            f"exception_str: {str(exc)}\n"
            f"traceback:\n{tb}"
            f"===== END AGENT ERROR: {agent} =====\n"
        )


def setup_log_redirection(log_path: str):
    """
    Initialize run.log for structured agent output only.
    Terminal stdout/stderr are unchanged (not tee'd into run.log).
    """
    RunLog.init(log_path)
    return None


class SummaryOutputParser:
    """
    解析 Summary Agent LLM 输出的结构化解析器。
    封装多层 JSON 提取、围栏剥离与 malformed payload 容错逻辑。
    """

    FENCE_PREFIXES = ("```json", "```")
    JSON_PREFIXES = ("json\n", "JSON\n", "json\n\n", "JSON\n\n", "```json\n", "```\n")
    MARKDOWN_KEYS = ("markdown_report", "llm_result")
    STRUCTURED_KEYS = ("line_comments", "issues")

    def __init__(self, raw: str):
        self.raw = raw or ""
        self._attempts: List[str] = []

    def strip_fences(self, text: str) -> str:
        t = text.strip()
        if t.startswith("```json"):
            t = t[7:]
        elif t.startswith("```"):
            t = t[3:]
        if t.endswith("```"):
            t = t[:-3]
        return t.strip()

    def strip_json_prefix(self, text: str) -> str:
        for prefix in self.JSON_PREFIXES:
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return text

    def build_candidate_list(self) -> List[str]:
        candidates = [
            self.raw,
            self.strip_fences(self.raw),
            self.strip_json_prefix(self.raw),
            self.strip_json_prefix(self.strip_fences(self.raw)),
        ]
        seen = set()
        ordered: List[str] = []
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                ordered.append(item)
        self._attempts = ordered
        return ordered

    def normalize_fields(
        self, obj: Dict[str, Any]
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        md = None
        for key in self.MARKDOWN_KEYS:
            value = obj.get(key)
            if isinstance(value, str):
                md = value
                break
        if md is None:
            return None, None, None

        line_comments_raw = obj.get("line_comments", {})
        issues_raw = obj.get("issues", [])
        normalized_lc = issues_raw if isinstance(issues_raw, dict) else {}
        normalized_issues = line_comments_raw if isinstance(line_comments_raw, list) else []
        return self.strip_fences(md).strip(), normalized_lc, normalized_issues

    def try_load_dict(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            return None
        return None

    def extract_json_substring(self, text: str) -> Optional[str]:
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_str = False
        escape = False
        quote = None
        i = start
        while i < len(text):
            c = text[i]
            if escape:
                escape = False
                i += 1
                continue
            if c == "\\" and in_str:
                escape = True
                i += 1
                continue
            if not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]
                elif c in ('"', "'"):
                    in_str = True
                    quote = c
            elif c == quote:
                in_str = False
            i += 1
        return None

    def parse_dict_payload(
        self, text: str
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        obj = self.try_load_dict(text)
        if obj is None:
            sub = self.extract_json_substring(text)
            if sub:
                obj = self.try_load_dict(sub)
        if obj is None:
            return None, None, None
        return self.normalize_fields(obj)

    def parse(self) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        if not self.raw.strip():
            return None, None, None
        for candidate in self.build_candidate_list():
            md, lc, issues = self.parse_dict_payload(candidate)
            if md is not None:
                return md, lc, issues
        return None, None, None


def parse_summary_llm_output(raw: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    """
    解析 Summary Agent 的 LLM 输出，支持多种兜底格式。
    兜底包括：Markdown 围栏、json\\n 前缀、从首尾大括号提取 JSON 等。

    Returns:
        (markdown_report, line_comments, issues) 解析成功时返回；否则 (None, None, None)
    """
    parser = SummaryOutputParser(raw)
    quick_result = parser.parse()
    if quick_result[0] is not None:
        return quick_result

    if not raw or not raw.strip():
        return None, None, None

    def _strip_fences(text: str) -> str:
        t = text.strip()
        if t.startswith("```json"):
            t = t[7:]
        elif t.startswith("```"):
            t = t[3:]
        if t.endswith("```"):
            t = t[:-3]
        return t.strip()

    def _strip_json_prefix(text: str) -> str:
        for prefix in ("json\n", "JSON\n", "json\n\n", "JSON\n\n", "```json\n", "```\n"):
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return text

    def _extract_json_substring(text: str) -> Optional[str]:
        """从文本中提取第一个完整 JSON 对象（从 { 到匹配的 }）。"""
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_str = False
        escape = False
        quote = None
        i = start
        while i < len(text):
            c = text[i]
            if escape:
                escape = False
                i += 1
                continue
            if c == "\\" and in_str:
                escape = True
                i += 1
                continue
            if not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]
                elif c in ('"', "'"):
                    in_str = True
                    quote = c
            elif c == quote:
                in_str = False
            i += 1
        return None

    def _load_json_dict(text: str) -> Optional[Dict[str, Any]]:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            return None
        return None

    def _extract_from_obj(obj: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        md = None
        for key in ("markdown_report", "llm_result"):
            value = obj.get(key)
            if isinstance(value, str):
                md = value
                break
        if md is None:
            return None, None, None

        lc = obj.get("line_comments", {})
        issues = obj.get("issues", [])
        normalized_lc = issues if isinstance(issues, dict) else {}
        normalized_issues = lc if isinstance(lc, list) else []
        return _strip_fences(md).strip(), normalized_lc, normalized_issues

    def _parse_nested_payload_from_markdown(md_text: str) -> Optional[Dict[str, Any]]:
        """
        部分模型会把结构化 JSON 放到 llm_result 字符串里（双层 JSON）。
        这里尝试从 markdown 字段中再次提取 JSON 对象。
        """
        candidates = [md_text, _strip_fences(md_text), _strip_json_prefix(md_text)]
        for candidate in candidates:
            obj = _load_json_dict(candidate)
            if obj is not None:
                return obj
            sub = _extract_json_substring(candidate)
            if sub:
                obj = _load_json_dict(sub)
                if obj is not None:
                    return obj
        return None

    def _parse_malformed_summary_payload(text: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        """
        兼容“看起来像 JSON，但 llm_result 内部引号未转义”的坏格式：
        {"llm_result":"...","line_comments":{...},"issues":[...]}
        """
        if '"llm_result"' not in text:
            return None, None, None

        llm_key = '"llm_result"'
        line_comments_markers = (
            '","line_comments":',
            '", "line_comments":',
            '",\n  "line_comments":',
            '",\n "line_comments":',
        )
        issues_markers = (
            ',"issues":',
            ', "issues":',
            ',\n  "issues":',
            ',\n "issues":',
            ',\n  "issues": [',
        )

        llm_key_idx = text.find(llm_key)
        if llm_key_idx == -1:
            return None, None, None

        colon_idx = text.find(":", llm_key_idx + len(llm_key))
        if colon_idx == -1:
            return None, None, None

        llm_value_start = colon_idx + 1
        while llm_value_start < len(text) and text[llm_value_start] in " \t\n\r":
            llm_value_start += 1
        if llm_value_start >= len(text) or text[llm_value_start] != '"':
            return None, None, None

        lc_idx = -1
        lc_marker_len = 0
        for marker in line_comments_markers:
            idx = text.find(marker, llm_value_start + 1)
            if idx != -1 and (lc_idx == -1 or idx < lc_idx):
                lc_idx = idx
                lc_marker_len = len(marker)
        if lc_idx == -1:
            return None, None, None

        llm_raw = text[llm_value_start + 1 : lc_idx]
        llm_raw = llm_raw.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        llm_raw = llm_raw.replace('\\"', '"').replace("\\\\", "\\")
        md = _strip_fences(llm_raw).strip()

        issues_idx = -1
        issues_marker_len = 0
        search_from = lc_idx + lc_marker_len
        for marker in issues_markers:
            idx = text.find(marker, search_from)
            if idx != -1 and (issues_idx == -1 or idx < issues_idx):
                issues_idx = idx
                issues_marker_len = len(marker)
        if issues_idx == -1:
            return md, {}, []

        lc_text = text[lc_idx + lc_marker_len : issues_idx].strip()
        end_brace = text.rfind("}")
        if end_brace == -1 or end_brace <= issues_idx:
            return md, {}, []
        issues_text = text[issues_idx + issues_marker_len : end_brace].strip()

        def _decode_escaped_text(value: str) -> str:
            return (
                value.replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )

        def _find_field_value(raw: str, field: str, cursor: int) -> Tuple[int, int]:
            """定位 JSON 字段值的起止下标（支持冒号后可选空格）。"""
            patterns = (
                f'"{field}": "',
                f'"{field}":"',
                f'"{field}":\\"',
            )
            for pattern in patterns:
                key_idx = raw.find(pattern, cursor)
                if key_idx == -1:
                    continue
                value_start = key_idx + len(pattern)
                i = value_start
                escape = False
                while i < len(raw):
                    ch = raw[i]
                    if escape:
                        escape = False
                        i += 1
                        continue
                    if ch == "\\":
                        escape = True
                        i += 1
                        continue
                    if ch == '"':
                        return value_start, i
                    i += 1
            return -1, -1

        def _parse_line_comments_fallback(raw_line_comments: str) -> Dict[str, Any]:
            comments: List[Dict[str, Any]] = []
            cursor = 0
            while True:
                path_start, path_end = _find_field_value(raw_line_comments, "new_path", cursor)
                if path_start == -1:
                    break
                path_raw = raw_line_comments[path_start:path_end]

                body_start, body_end = _find_field_value(raw_line_comments, "body", path_end)
                if body_start == -1:
                    break
                body_raw = raw_line_comments[body_start:body_end]

                start_key_variants = ('"start_line":', '"start_line": ')
                start_idx = -1
                start_key_len = 0
                for sk in start_key_variants:
                    idx = raw_line_comments.find(sk, body_end)
                    if idx != -1 and (start_idx == -1 or idx < start_idx):
                        start_idx = idx
                        start_key_len = len(sk)
                if start_idx == -1:
                    break

                end_key_variants = ('"end_line":', '"end_line": ')
                end_idx = -1
                end_key_len = 0
                for ek in end_key_variants:
                    idx = raw_line_comments.find(ek, start_idx + start_key_len)
                    if idx != -1 and (end_idx == -1 or idx < end_idx):
                        end_idx = idx
                        end_key_len = len(ek)
                if end_idx == -1:
                    break

                start_raw = raw_line_comments[start_idx + start_key_len : end_idx].strip().rstrip(",")
                end_val_begin = end_idx + end_key_len
                obj_end = raw_line_comments.find("}", end_val_begin)
                if obj_end == -1:
                    break
                end_raw = raw_line_comments[end_val_begin:obj_end].strip().rstrip(",")

                try:
                    start_line = int(start_raw)
                    end_line = int(end_raw)
                    comments.append(
                        {
                            "new_path": _decode_escaped_text(path_raw),
                            "body": _decode_escaped_text(body_raw),
                            "start_line": start_line,
                            "end_line": end_line,
                        }
                    )
                except Exception:
                    pass

                cursor = obj_end + 1

            return {"comments": comments}

        line_comments: Dict[str, Any] = {}
        issues: List[Dict[str, Any]] = []
        try:
            parsed_lc = json.loads(lc_text)
            if isinstance(parsed_lc, dict):
                line_comments = parsed_lc
        except Exception:
            # 二次兜底：直接按字段边界提取 comments
            line_comments = _parse_line_comments_fallback(lc_text)

        try:
            parsed_issues = json.loads(issues_text)
            if isinstance(parsed_issues, list):
                issues = parsed_issues
        except Exception:
            pass

        return md, line_comments, issues

    def _parse_and_extract(text: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        try:
            obj = json.loads(text)
            # 兼容双层 JSON 字符串：第一层解码后仍是 "{...}" 字符串
            if isinstance(obj, str):
                nested = _load_json_dict(obj)
                if nested is None:
                    sub = _extract_json_substring(obj)
                    if sub:
                        nested = _load_json_dict(sub)
                if nested is None:
                    return None, None, None
                obj = nested

            if isinstance(obj, dict):
                merged_lc: Dict[str, Any] = {}
                merged_issues: List[Dict[str, Any]] = []
                current = obj

                # 最多展开 3 层，防止异常输出导致无限递归。
                for _ in range(3):
                    md, lc, issues = _extract_from_obj(current)
                    if md is None:
                        return None, None, None

                    if lc:
                        merged_lc = lc
                    if issues:
                        merged_issues = issues

                    nested_obj = _parse_nested_payload_from_markdown(md)
                    if nested_obj and any(
                        key in nested_obj for key in ("markdown_report", "llm_result", "line_comments", "issues")
                    ):
                        current = nested_obj
                        continue

                    return md, merged_lc, merged_issues
        except (json.JSONDecodeError, TypeError):
            pass
        return None, None, None

    # 1) 标准：去掉围栏后解析
    json_text = _strip_fences(raw)
    md, lc, issues = _parse_and_extract(json_text)
    if md is not None:
        return md, lc, issues

    # 2) 兜底：去掉 json\n 等前缀
    json_text = _strip_json_prefix(raw)
    md, lc, issues = _parse_and_extract(json_text)
    if md is not None:
        return md, lc, issues

    # 3) 兜底：从首尾大括号提取 JSON 子串
    for candidate in (raw, _strip_fences(raw), _strip_json_prefix(raw)):
        sub = _extract_json_substring(candidate)
        if sub:
            md, lc, issues = _parse_and_extract(sub)
            if md is not None:
                return md, lc, issues

    # 4) 兜底：坏 JSON 容错提取（llm_result 内未转义引号）
    for candidate in (raw, _strip_fences(raw), _strip_json_prefix(raw)):
        md, lc, issues = _parse_malformed_summary_payload(candidate)
        if md is not None:
            return md, lc, issues

    return None, None, None


def build_canonical_summary_payload(
    markdown_report: str,
    line_comments: Optional[Dict[str, Any]] = None,
    issues: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """将解析结果序列化为标准 Summary JSON，供下游再次解析或落盘。"""
    payload = {
        "llm_result": markdown_report,
        "line_comments": line_comments if isinstance(line_comments, dict) else {},
        "issues": issues if isinstance(issues, list) else [],
    }
    return json.dumps(payload, ensure_ascii=False)


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


class CommentPathResolver:
    """
    解析行评论中的文件路径，支持相对路径、绝对路径以及 git 仓库内的文件名搜索。
    """

    def __init__(self, project_root: str):
        self.project_root = project_root
        if project_root and not os.path.isabs(project_root):
            self.project_root = os.path.abspath(project_root)

    def resolve_relative_path(self, relative_path: str) -> str:
        """将评论中的相对路径解析为绝对路径。"""
        if not relative_path:
            return ""
        if os.path.isabs(relative_path):
            return relative_path
        base = self.project_root or "."
        return os.path.normpath(os.path.join(base, relative_path))

    def path_exists(self, absolute_path: str) -> bool:
        return bool(absolute_path) and os.path.exists(absolute_path)

    def search_in_git_by_filename(self, relative_path: str) -> Optional[str]:
        if not self.project_root or not os.path.exists(self.project_root):
            return None
        try:
            import subprocess
            git_check = subprocess.run(
                ["git", "-C", self.project_root, "rev-parse", "--git-dir"],
                capture_output=True,
                check=False,
            )
            if git_check.returncode != 0:
                return None
            filename = os.path.basename(relative_path)
            git_result = subprocess.run(
                ["git", "-C", self.project_root, "ls-files", "**/" + filename],
                capture_output=True,
                text=True,
                check=False,
            )
            if git_result.returncode == 0 and git_result.stdout.strip():
                found_paths = [
                    p.strip() for p in git_result.stdout.strip().split("\n") if p.strip()
                ]
                if found_paths:
                    return found_paths[0]
        except Exception as e:
            print(f"⚠️ Error searching for file in git: {e}")
        return None

    def resolve_with_git_fallback(
        self, relative_path: str
    ) -> Tuple[str, Optional[str], str]:
        """
        解析路径，必要时通过 git 搜索同名文件。

        Returns:
            (absolute_path, corrected_relative_path_or_none, status)
            status: "found" | "git_resolved" | "missing"
        """
        absolute_path = self.resolve_relative_path(relative_path)
        if self.path_exists(absolute_path):
            return absolute_path, None, "found"

        git_match = self.search_in_git_by_filename(relative_path)
        if git_match:
            corrected = os.path.join(self.project_root, git_match)
            print(f"🔍 Found file in git: {git_match} (was looking for: {relative_path})")
            return corrected, git_match, "git_resolved"

        return absolute_path, None, "missing"

    def read_file_lines(self, absolute_path: str) -> Optional[List[str]]:
        if not self.path_exists(absolute_path):
            return None
        try:
            with open(absolute_path, "r", encoding="utf-8") as f:
                return f.readlines()
        except Exception:
            return None

    def validate_line_range(
        self, absolute_path: str, start_line: int, end_line: int
    ) -> Tuple[bool, int]:
        lines = self.read_file_lines(absolute_path)
        if lines is None:
            return False, 0
        total = len(lines)
        if start_line > total or end_line > total:
            return False, total
        return True, total


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
    
    # File existence check via CommentPathResolver
    path_resolver = CommentPathResolver(project_root or "")
    full_path, corrected_path, resolve_status = path_resolver.resolve_with_git_fallback(new_path)

    if corrected_path:
        result["original_path"] = comment.get("new_path")
        result["new_path"] = corrected_path
        new_path = corrected_path

    if resolve_status == "missing":
        print(
            f"⚠️ File not found for validation: {full_path} "
            f"(project_root: {project_root}, new_path: {new_path})"
        )
        result["validation_status"] = "invalid"
        result["needs_review"] = True
        result["validation_error"] = f"File not found: {full_path}"
        return result

    range_ok, total_lines = path_resolver.validate_line_range(full_path, start_line, end_line)
    if not range_ok:
        result["validation_status"] = "needs_review"
        result["needs_review"] = True
        return result

    file_lines = path_resolver.read_file_lines(full_path)
    if file_lines is None:
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

