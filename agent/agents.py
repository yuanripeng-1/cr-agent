import litellm
import asyncio
from typing import List, Dict, Any
from .prompts import *
from .utils import RunLog

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
    def __init__(
        self,
        model: str = "gpt-4",
        max_retries: int = 3,
        retry_delay: float = 2.0,
        api_base: str | None = None,
        timeout_seconds: int | None = None,
        timeout_base_seconds: int = 180,
        timeout_per_1k_chars: int = 1,
        timeout_max_seconds: int = 600,
    ):
        self.model = model
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.api_base = api_base  # 私有化部署：自定义 API 地址，如 "https://your-server/v1"
        self.timeout_seconds = timeout_seconds
        self.timeout_base_seconds = timeout_base_seconds
        self.timeout_per_1k_chars = timeout_per_1k_chars
        self.timeout_max_seconds = timeout_max_seconds

    def _agent_label(self) -> str:
        return getattr(self, "dimension_name", self.__class__.__name__)

    def _print_llm_output_to_terminal(self, content: str, usage_info: dict) -> None:
        """将 LLM 原始完整输出打印到终端（不写入 run.log）。"""
        agent_label = self._agent_label()
        print(f"\n{'='*20} LLM FULL OUTPUT [{agent_label}] {'='*20}")
        print(
            f"Token 消耗: {usage_info.get('total_tokens', 0)} "
            f"(Input: {usage_info.get('prompt_tokens', 0)}, "
            f"Output: {usage_info.get('completion_tokens', 0)}, "
            f"Cost: ${usage_info.get('cost', 0.0):.6f})"
        )
        print(content)
        print(f"{'='*72}\n")

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
        agent_label = self._agent_label()
        
        # Timeout strategy:
        # 1) If timeout_seconds is configured, use fixed timeout for each call.
        # 2) Otherwise use dynamic timeout based on prompt length.
        if self.timeout_seconds is not None:
            dynamic_timeout = max(1, int(self.timeout_seconds))
        else:
            total_prompt_length = len(system_prompt) + len(user_prompt)
            base_timeout = max(1, int(self.timeout_base_seconds))
            per_1k_chars = max(0, int(self.timeout_per_1k_chars))
            max_timeout = max(base_timeout, int(self.timeout_max_seconds))
            additional_timeout = max(0, (total_prompt_length // 1000) * per_1k_chars)
            dynamic_timeout = min(base_timeout + additional_timeout, max_timeout)
        
        for attempt in range(self.max_retries):
            try:
                current_timeout = int(dynamic_timeout)
                kwargs = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.2,
                    "timeout": current_timeout,
                }
                if self.api_base:
                    kwargs["api_base"] = self.api_base.rstrip("/")
                response = await litellm.acompletion(**kwargs)
                
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

                RunLog.write_agent_output(agent_label, content)
                self._print_llm_output_to_terminal(content, usage_info)
                
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
                        # Increase timeout for next retry attempt only for dynamic timeout mode.
                        if self.timeout_seconds is None:
                            max_timeout = max(1, int(self.timeout_max_seconds))
                            dynamic_timeout = min(dynamic_timeout * 1.5, max_timeout)
                            print(f"   下次重试将使用更长的超时时间: {int(dynamic_timeout)} 秒")
                        else:
                            print(f"   固定超时模式: {int(dynamic_timeout)} 秒")
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
                    RunLog.write_agent_error(agent_label, e)
                    raise
        
        # If we exhausted all retries, raise the last exception
        if last_exception:
            RunLog.write_agent_error(agent_label, last_exception)
            raise last_exception
        else:
            raise Exception("LLM call failed after all retries")

class GenericDimensionAgent(BaseAgent):
    """A generic agent for single-dimension review."""
    def __init__(self, model: str, system_prompt: str, dimension_name: str, api_base: str | None = None, **base_agent_kwargs):
        super().__init__(model, api_base=api_base, **base_agent_kwargs)
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
