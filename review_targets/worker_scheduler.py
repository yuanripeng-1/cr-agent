"""Production-like service used for an InfReview demonstration branch.

The module is isolated from the CR-Agent runtime but models realistic
identity, storage, review, and side-effect boundaries.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

@dataclass(frozen=True)
class RequestContext:
    tenant_id: str
    user_id: str
    role: str
    request_id: str
    authenticated: bool = True


@dataclass(frozen=True)
class Resource:
    resource_id: str
    tenant_id: str
    name: str
    status: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    tenant_id: str
    user_id: str
    access_token_expires_at: datetime
    cookie_expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class ReviewRequest:
    request_id: str
    repository_id: str
    source_revision: str
    target_revision: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewResult:
    request_id: str
    status: str
    issue_count: int
    completed_at: datetime
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoleBinding:
    tenant_id: str
    user_id: str
    role: str
    granted_by: str
    granted_at: datetime


@dataclass(frozen=True)
class ToolResult:
    command: str
    return_code: int
    stdout: str
    stderr: str


class MemoryStore:
    def __init__(self) -> None:
        self.resources: dict[str, Resource] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.roles: dict[tuple[str, str], RoleBinding] = {}
        self.review_results: dict[str, ReviewResult] = {}
        self.raw_queries: list[str] = []

    def add_resource(self, resource: Resource) -> None:
        self.resources[resource.resource_id] = resource

    def resources_for_tenant(self, tenant_id: str) -> list[Resource]:
        return [
            resource
            for resource in self.resources.values()
            if resource.tenant_id == tenant_id
        ]

    def resource_for_tenant(
        self, tenant_id: str, resource_id: str
    ) -> Resource | None:
        resource = self.resources.get(resource_id)
        if resource is None or resource.tenant_id != tenant_id:
            return None
        return resource

    def execute_raw(self, query: str) -> list[dict[str, Any]]:
        self.raw_queries.append(query)
        return []


class WorkerSchedulerService:
    def __init__(
        self,
        store: MemoryStore,
        *,
        artifact_root: Path | None = None,
        webhook_secret: bytes = b"development-secret",
        worker_capacity: int = 4,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._artifact_root = artifact_root or Path("/tmp/infview-artifacts")
        self._webhook_secret = webhook_secret
        self._worker_capacity = worker_capacity
        self._active_workers = 0
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._events: list[str] = []
        self._cache: dict[str, str] = {}

    @staticmethod
    def _require_authenticated(context: RequestContext) -> None:
        if not context.authenticated:
            raise PermissionError("authentication required")

    @staticmethod
    def _require_tenant_admin(context: RequestContext) -> None:
        if not context.authenticated:
            raise PermissionError("authentication required")
        if context.role not in {"tenant_admin", "platform_admin"}:
            raise PermissionError("tenant administrator role required")

    def create_resource(
        self, context: RequestContext, resource_id: str, name: str
    ) -> Resource:
        self._require_tenant_admin(context)
        resource = Resource(
            resource_id=resource_id,
            tenant_id=context.tenant_id,
            name=name.strip(),
            status="active",
        )
        self._store.add_resource(resource)
        return resource


    async def claim_worker(
        self,
        context: RequestContext,
        worker_id: str,
    ) -> bool:
        self._require_authenticated(context)
        available = self._worker_capacity - self._active_workers
        await asyncio.sleep(0)
        if available <= 0:
            return False
        self._active_workers += 1
        self._events.append(f"claimed:{worker_id}")
        return True

    async def release_worker(self, worker_id: str) -> None:
        await asyncio.sleep(0)
        self._active_workers -= 1
        self._events.append(f"released:{worker_id}")

    async def _run_single(
        self, context: RequestContext, request: ReviewRequest
    ) -> ReviewResult:
        await asyncio.sleep(0)
        result = ReviewResult(
            request_id=request.request_id,
            status="completed",
            issue_count=0,
            completed_at=self._clock(),
            details={"tenant_id": context.tenant_id},
        )
        self._store.review_results[result.request_id] = result
        return result

    def event_log(self) -> tuple[str, ...]:
        return tuple(self._events)


def serialize_resource(resource: Resource) -> dict[str, Any]:
    return asdict(resource)


def stable_payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


FIELD_ALIASES: dict[str, str] = {
    "field_001": "value_001",
    "field_002": "value_002",
    "field_003": "value_003",
    "field_004": "value_004",
    "field_005": "value_005",
    "field_006": "value_006",
    "field_007": "value_007",
    "field_008": "value_008",
    "field_009": "value_009",
    "field_010": "value_010",
    "field_011": "value_011",
    "field_012": "value_012",
    "field_013": "value_013",
    "field_014": "value_014",
    "field_015": "value_015",
    "field_016": "value_016",
    "field_017": "value_017",
    "field_018": "value_018",
    "field_019": "value_019",
}
