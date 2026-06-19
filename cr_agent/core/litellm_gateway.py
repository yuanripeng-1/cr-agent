"""
本地 LiteLLM proxy 网关(主架构唯一 Anthropic 兼容网关)。

背景:Claude Agent SDK / claude CLI 只会以 Anthropic Messages 协议(`POST /v1/messages`
+ `x-api-key`)发起请求。若把 `[llm].api_base` 直接指到只支持 OpenAI 协议
(`/chat/completions` + `Authorization: Bearer`)的厂商,SDK 拼出的 `/v1/messages`
端点根本不存在、鉴权头也不匹配,必然返回 401。

本模块在本机拉起一个 LiteLLM proxy,对外暴露 Anthropic `/v1/messages`,内部翻译成
上游厂商的 OpenAI `/chat/completions`。bootstrap 把 `ANTHROPIC_BASE_URL` 改写为本地
proxy 地址,`ANTHROPIC_API_KEY` 改写为 proxy 的 master_key,从而打通协议鸿沟。

设计要点:
- 上游 api_key 与 master_key 都经环境变量 `os.environ/<VAR>` 注入 LiteLLM,**不落盘明文**,
  满足"严禁提交/写盘密钥"约束。
- 端口默认取空闲端口;subprocess stdout/stderr 落到 result_dir 下的 litellm_gateway.log。
- start() 阻塞等待健康检查通过;atexit 兜底关闭,避免残留进程。
"""

from __future__ import annotations

import atexit
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.core.litellm_gateway")

# 上游与 master key 注入 LiteLLM 时使用的环境变量名(避免把密钥写进 config 文件)。
_UPSTREAM_KEY_ENV = "CR_AGENT_LITELLM_UPSTREAM_KEY"
_MASTER_KEY_ENV = "CR_AGENT_LITELLM_MASTER_KEY"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resolve_litellm_bin() -> str:
    # 优先用与当前解释器同目录的 litellm,保证与运行环境(cragent)一致;
    # 否则 PATH 上可能命中 base env 的 litellm(缺 proxy 依赖,如 backoff)。
    candidate = Path(sys.executable).parent / "litellm"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("litellm")
    if found:
        return found
    raise RuntimeError(
        "litellm CLI 未找到;请在运行环境(cragent)安装 'litellm[proxy]'。"
    )


class LiteLLMGateway:
    """管理一个本地 LiteLLM proxy 子进程的生命周期。"""

    def __init__(
        self,
        *,
        model: str,
        upstream_api_base: str,
        upstream_api_key: str,
        provider: str = "openai",
        host: str = "127.0.0.1",
        port: int | None = None,
        master_key: str | None = None,
        log_path: Path | None = None,
        startup_timeout_s: float = 120.0,
    ) -> None:
        self.model = model
        self.upstream_api_base = upstream_api_base
        self.upstream_api_key = upstream_api_key
        self.provider = provider or "openai"
        self.host = host
        self.port = port or _find_free_port()
        self.master_key = master_key or ("sk-" + secrets.token_hex(16))
        self.log_path = log_path
        self.startup_timeout_s = startup_timeout_s

        self._proc: subprocess.Popen | None = None
        self._config_dir: str | None = None
        self._claude_config_dir: str | None = None
        self._log_handle = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def api_key(self) -> str:
        return self.master_key

    @property
    def claude_config_dir(self) -> str:
        """
        为 claude CLI 提供的隔离配置目录。使用本网关时必须把它注入 CLAUDE_CONFIG_DIR,
        否则宿主 ~/.claude/settings.json 的 env 块会覆盖 ANTHROPIC_BASE_URL,导致请求
        绕过本网关(实测会打到 settings 里配置的其它厂商而非本地 proxy)。
        """
        if self._claude_config_dir is None:
            self._claude_config_dir = tempfile.mkdtemp(prefix="cr_agent_claude_cfg_")
        return self._claude_config_dir

    def _write_config(self) -> Path:
        # config 里只放 `os.environ/<VAR>` 引用,真实密钥经环境变量注入,不落盘明文。
        config = {
            "model_list": [
                {
                    "model_name": self.model,
                    "litellm_params": {
                        "model": f"{self.provider}/{self.model}",
                        "api_base": self.upstream_api_base,
                        "api_key": f"os.environ/{_UPSTREAM_KEY_ENV}",
                    },
                }
            ],
            "litellm_settings": {
                "drop_params": True,
                # 关键:让 Anthropic /v1/messages 桥接到 OpenAI 兼容厂商时走
                # /chat/completions,而非新版 /responses(很多厂商没有 /responses)。
                "use_chat_completions_url_for_anthropic_messages": True,
            },
            "general_settings": {"master_key": f"os.environ/{_MASTER_KEY_ENV}"},
        }
        self._config_dir = tempfile.mkdtemp(prefix="cr_agent_litellm_")
        config_path = Path(self._config_dir) / "litellm.config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return config_path

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env[_UPSTREAM_KEY_ENV] = self.upstream_api_key
        env[_MASTER_KEY_ENV] = self.master_key
        # 关闭子进程 stdio 缓冲,保证 proxy 访问日志/报错实时落进 litellm_gateway.log。
        env["PYTHONUNBUFFERED"] = "1"
        # 防止把宿主可能存在的 ANTHROPIC_* 透传给 proxy 子进程,污染其行为。
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    def start(self) -> None:
        if self._proc is not None:
            return
        config_path = self._write_config()
        bin_path = _resolve_litellm_bin()
        cmd = [
            bin_path,
            "--config",
            str(config_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--num_workers",
            "1",
        ]

        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = open(self.log_path, "w", encoding="utf-8")
            stdout = self._log_handle
            stderr = subprocess.STDOUT
        else:
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL

        _logger.info(
            "LITELLM_GATEWAY_SPAWN host=%s port=%s model=%s upstream=%s provider=%s",
            self.host,
            self.port,
            self.model,
            self.upstream_api_base,
            self.provider,
        )
        self._proc = subprocess.Popen(  # noqa: S603 - 受控命令,参数非用户拼接
            cmd,
            stdout=stdout,
            stderr=stderr,
            env=self._build_env(),
        )
        atexit.register(self.stop)
        self._wait_ready()

    def _wait_ready(self) -> None:
        url = f"{self.base_url}/health/liveliness"
        deadline = time.monotonic() + self.startup_timeout_s
        last_err = ""
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                self._cleanup_files()
                raise RuntimeError(
                    f"LiteLLM proxy 启动失败(exit={self._proc.returncode});"
                    f"详见日志 {self.log_path}\n{self._log_tail()}"
                )
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                    if resp.status == 200:
                        _logger.info("LITELLM_GATEWAY_READY base_url=%s", self.base_url)
                        return
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last_err = str(exc)
            time.sleep(1.0)
        self.stop()
        raise RuntimeError(
            f"LiteLLM proxy 在 {self.startup_timeout_s}s 内未就绪;最后错误={last_err}\n"
            f"{self._log_tail()}"
        )

    def _log_tail(self, max_lines: int = 30) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            _logger.info("LITELLM_GATEWAY_STOP port=%s", self.port)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None
        self._cleanup_files()

    def _cleanup_files(self) -> None:
        if self._config_dir is not None:
            shutil.rmtree(self._config_dir, ignore_errors=True)
            self._config_dir = None
        if self._claude_config_dir is not None:
            shutil.rmtree(self._claude_config_dir, ignore_errors=True)
            self._claude_config_dir = None
