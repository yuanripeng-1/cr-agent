import subprocess
import os
import litellm
import asyncio
import json
from typing import List, Dict, Any
from .prompts import *

# Import litellm exceptions for better error handling
try:
    from litellm.exceptions import (
        APIError,
        APIConnectionError,
        APITimeoutError,
        RateLimitError,
        ServiceUnavailableError,
        ContentFilterViolationError,
        Timeout  # litellm's Timeout exception
    )
except ImportError:
    # Fallback if litellm exceptions are not available
    APIError = Exception
    APIConnectionError = Exception
    APITimeoutError = Exception
    RateLimitError = Exception
    ServiceUnavailableError = Exception
    ContentFilterViolationError = Exception
    Timeout = Exception

class BaseAgent:
    def __init__(self, model: str = "gpt-4", max_retries: int = 3, retry_delay: float = 2.0):
        self.model = model
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def _is_retryable_error(self, exception: Exception) -> tuple[bool, str]:
        """
        Determine if an error is retryable and return (is_retryable, error_category).
        
        Returns:
            tuple: (is_retryable, error_category)
        """
        error_str = str(exception).lower()
        error_type = type(exception).__name__.lower()
        
        # Check for litellm specific exceptions
        if isinstance(exception, (APIConnectionError, ConnectionError, OSError)):
            return True, "connection"
        elif isinstance(exception, (Timeout, APITimeoutError, TimeoutError, asyncio.TimeoutError)):
            return True, "timeout"
        elif isinstance(exception, RateLimitError):
            return True, "rate_limit"
        elif isinstance(exception, ServiceUnavailableError):
            return True, "service_unavailable"
        elif isinstance(exception, APIError):
            # Some API errors might be retryable
            if any(keyword in error_str for keyword in ["503", "502", "500", "429"]):
                return True, "api_error"
        
        # Check error string for common retryable patterns
        connection_keywords = [
            "connection", "connect", "network", "ssl", "tls", 
            "unreachable", "refused", "reset", "broken pipe"
        ]
        if any(keyword in error_str for keyword in connection_keywords):
            return True, "connection"
        
        timeout_keywords = ["timeout", "timed out", "deadline exceeded"]
        if any(keyword in error_str for keyword in timeout_keywords):
            return True, "timeout"
        
        rate_limit_keywords = ["rate limit", "429", "too many requests", "quota exceeded"]
        if any(keyword in error_str for keyword in rate_limit_keywords):
            return True, "rate_limit"
        
        # JSON parsing errors (often transient)
        json_keywords = [
            "jsondecodeerror", "json decode", "unable to get json",
            "expecting value", "invalid json", "malformed json",
            "parse error", "unexpected token"
        ]
        if any(keyword in error_str for keyword in json_keywords) or "json" in error_type:
            return True, "json_parse"
        
        # Server errors (5xx)
        server_error_keywords = ["500", "502", "503", "504", "service unavailable", "bad gateway"]
        if any(keyword in error_str for keyword in server_error_keywords):
            return True, "server_error"
        
        # Content filter violations are usually not retryable
        if isinstance(exception, ContentFilterViolationError) or "content filter" in error_str:
            return False, "content_filter"
        
        return False, "unknown"

    async def call_llm(self, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
        """
        Call LLM with robust retry mechanism for network errors, timeouts, and transient failures.
        
        Returns:
            tuple: (content, usage_info) where usage_info contains tokens and cost
        """
        last_exception = None
        
        # Calculate dynamic timeout based on prompt length
        # Base timeout: 180 seconds (3 minutes)
        # Add 1 second per 1000 characters in prompt (rough estimate)
        total_prompt_length = len(system_prompt) + len(user_prompt)
        base_timeout = 180  # 3 minutes base timeout
        additional_timeout = max(0, (total_prompt_length // 1000) * 1)  # +1s per 1k chars
        dynamic_timeout = min(base_timeout + additional_timeout, 600)  # Cap at 10 minutes
        
        for attempt in range(self.max_retries):
            try:
                current_timeout = int(dynamic_timeout)
                response = await litellm.acompletion(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.2,
                    timeout=current_timeout  # Dynamic timeout based on prompt size
                )
                
                # Validate response structure
                if not response or not hasattr(response, 'choices') or not response.choices:
                    raise ValueError("Invalid response structure: missing choices")
                
                if not response.choices[0].message.content:
                    raise ValueError("Empty response content")
                
                content = response.choices[0].message.content
                
                # Get token usage and cost
                usage = getattr(response, 'usage', None)
                usage_info = {
                    "prompt_tokens": getattr(usage, 'prompt_tokens', 0) if usage else 0,
                    "completion_tokens": getattr(usage, 'completion_tokens', 0) if usage else 0,
                    "total_tokens": getattr(usage, 'total_tokens', 0) if usage else 0,
                    "cost": 0.0
                }
                
                # Try to get cost if possible
                try:
                    usage_info["cost"] = litellm.completion_cost(completion_response=response) or 0.0
                except Exception:
                    pass
                
                # Log success after retries
                if attempt > 0:
                    print(f"✅ LLM 调用成功 (第 {attempt + 1} 次尝试)")
                
                return content, usage_info
                
            except Exception as e:
                last_exception = e
                is_retryable, error_category = self._is_retryable_error(e)
                
                # Check if we should retry
                if is_retryable and attempt < self.max_retries - 1:
                    # Calculate delay with exponential backoff
                    base_delay = self.retry_delay * (2 ** attempt)
                    
                    # Adjust delay based on error category
                    if error_category == "rate_limit":
                        delay = max(base_delay, 10.0)  # Rate limits need longer delay
                    elif error_category == "server_error":
                        delay = max(base_delay, 5.0)   # Server errors need moderate delay
                    elif error_category == "timeout":
                        # For timeouts, use longer delay and increase timeout for next attempt
                        delay = max(base_delay * 2.0, 5.0)  # Timeouts need longer delay
                        # Increase timeout for next retry attempt (up to 10 minutes)
                        dynamic_timeout = min(dynamic_timeout * 1.5, 600)
                        print(f"   下次重试将使用更长的超时时间: {int(dynamic_timeout)} 秒")
                    else:
                        delay = base_delay
                    
                    # Log retry attempt
                    error_msg = str(e)[:200]  # Limit error message length
                    print(f"⚠️  LLM 调用失败 (尝试 {attempt + 1}/{self.max_retries})")
                    print(f"   错误类型: {error_category}")
                    print(f"   错误信息: {error_msg}")
                    if error_category == "timeout":
                        print(f"   当前超时设置: {current_timeout} 秒")
                    print(f"🔄 {delay:.1f} 秒后重试...")
                    
                    await asyncio.sleep(delay)
                    continue
                else:
                    # Non-retryable error or max retries reached
                    if attempt == self.max_retries - 1:
                        error_msg = str(e)[:200]
                        print(f"❌ LLM 调用失败，已达到最大重试次数 ({self.max_retries})")
                        print(f"   错误类型: {error_category}")
                        print(f"   错误信息: {error_msg}")
                        if error_category == "timeout":
                            print(f"   最终超时设置: {int(dynamic_timeout)} 秒")
                    raise
        
        # If we exhausted all retries, raise the last exception
        if last_exception:
            raise last_exception
        else:
            raise Exception("LLM call failed after all retries")

class GenericDimensionAgent(BaseAgent):
    """A generic agent for single-dimension review."""
    def __init__(self, model: str, system_prompt: str, dimension_name: str):
        super().__init__(model)
        self.system_prompt = system_prompt
        self.dimension_name = dimension_name

    async def run(self, code_diff: str, context_info: str = "", previous_review: str = "", language: str = "python") -> tuple[str, dict]:
        # Language-Specific Red Flags
        lang_checks = {
            "python": "- Watch for: Mutable default arguments, broad except: pass, circular imports, misuse of *args/**kwargs.",
            "go": "- Watch for: Unclosed channels, nil map assignment, goroutine leaks, error ignoring (_), interface pollution.",
            "java": "- Watch for: NullPointerExceptions, raw types, memory leaks in static maps, swallowing exceptions.",
            "javascript": "- Watch for: '==', callback hell, unhandled promises, global variable pollution, 'any' type in TS.",
            "typescript": "- Watch for: 'any' type usage, non-null assertions (!), ignore comments.",
            "node": "- Watch for: Blocking I/O in event loop, unhandled promise rejections, console.log left in production."
        }
        
        specific_instruction = lang_checks.get(language.lower(), "")
        
        user_prompt = f"### CODE DIFF\n{code_diff}\n"
        
        if specific_instruction:
            user_prompt += f"\n### LANGUAGE SPECIFIC CHECKS ({language})\n{specific_instruction}\n"
            
        if context_info:
            user_prompt += f"\n### CONTEXT INFO\n{context_info}\n"
            
        if previous_review:
            user_prompt += f"\n\n{ITERATIVE_REVIEW_INSTRUCTION}\n\n### PREVIOUS REVIEW REPORT ({self.dimension_name})\n{previous_review}"
        
        # Anti-Hallucination Injection (High-confidence only)
        user_prompt += "\n\nIMPORTANT: 仅输出置信度>=85的问题；每条必须带confidence字段(0-100)。如果没有高置信度问题，输出空数组并写明“暂未发现高置信度问题”。严禁为了凑数输出低价值建议。"
        
        return await self.call_llm(self.system_prompt, user_prompt)

class QualityLinterAgent(BaseAgent):
    """Consistency & Style Agent that also runs physical Linter."""
    async def run(self, code_diff: str, file_paths: List[str], project_root: str = ".", language: str = "python", guidelines_path: str = "", previous_review: str = "") -> tuple[str, dict]:
        # Linter execution logic
        linter_output = ""
        linter_cmd = []
        if language.lower() == "python":
            linter_cmd = ["pylint", "--output-format=text"]
        elif language.lower() == "go":
            linter_cmd = ["golangci-lint", "run"]
        
        if linter_cmd:
            for path in file_paths:
                try:
                    cmd = linter_cmd + [path]
                    process = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True, timeout=30)
                    linter_output += f"\nFile: {path}\n{process.stdout}\n{process.stderr}"
                except Exception as e:
                    linter_output += f"\nLinter Error on {path}: {str(e)}"

        # Guidelines
        guidelines_content = "No specific guidelines provided."
        if guidelines_path:
            lang_map = {"python": "python_style.md", "go": "go_style.md", "java": "java_style.md", "javascript": "nodejs_style.md"}
            fname = lang_map.get(language.lower())
            if fname:
                full_path = os.path.join(guidelines_path, fname)
                if os.path.exists(full_path):
                    with open(full_path, "r") as f: guidelines_content = f.read()

        user_prompt = (
            f"### TEAM CODING GUIDELINES\n{guidelines_content}\n\n"
            f"### LINTER OUTPUT\n{linter_output}\n\n"
            f"### CODE DIFF\n{code_diff}\n"
        )
        if previous_review:
            user_prompt += f"\n\n{ITERATIVE_REVIEW_INSTRUCTION}\n\n### PREVIOUS REVIEW REPORT\n{previous_review}"
        
        return await self.call_llm(CONSISTENCY_AGENT_PROMPT, user_prompt)
