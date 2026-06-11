from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Awaitable[dict]]


class ToolProvider(Protocol):
    def list_tools(self) -> list[ToolSpec]:
        ...

    def name(self) -> str:
        ...

