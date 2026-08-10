"""Bounded A2A 1.x HTTP+JSON transport for independently operated workers."""

from __future__ import annotations

import hashlib
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .artifacts import canonical_json
from .config import A2AAdapter, Profile
from .state import StateStore

_TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}
_ACTIVE = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}


class A2AError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class A2ALimits:
    timeout_seconds: float
    max_body_bytes: int
    poll_interval_seconds: float = 0.1


@dataclass(frozen=True)
class A2AOutcome:
    value: Any
    changeset: Any | None
    task_id: str | None
    card_digest: str
    capability_digest: str
    protocol_version: str
    interface_url: str
    response_digest: str
    response_bytes: int
    statuses: tuple[str, ...]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise A2AError("A2A_URL", "A2A URL is not an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise A2AError("A2A_URL", "A2A URL may not contain credentials or fragments")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise A2AError("A2A_URL", "non-loopback A2A endpoints require HTTPS")
    return parsed.scheme, parsed.hostname, parsed.port


def _request_json(
    url: str,
    *,
    auth: str | None,
    version: str | None,
    timeout: float,
    max_bytes: int,
    method: str = "GET",
    value: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], bytes]:
    body = canonical_json(value) if value is not None else None
    if body is not None and len(body) > max_bytes:
        raise A2AError(
            "A2A_REQUEST_LIMIT", "remote request exceeded the configured byte limit"
        )
    headers = {
        "Accept": "application/a2a+json, application/json",
        "User-Agent": "graph-engineering-a2a/0.1",
    }
    if auth is not None:
        headers["Authorization"] = f"Bearer {auth}"
    if body is not None:
        headers["Content-Type"] = "application/a2a+json"
    if version is not None:
        headers["A2A-Version"] = version
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=max(0.01, timeout)) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"application/json", "application/a2a+json"}:
                raise A2AError(
                    "A2A_CONTENT_TYPE", "remote response is not application/json"
                )
            raw = response.read(max_bytes + 1)
    except A2AError:
        raise
    except urllib.error.HTTPError as exc:
        raise A2AError("A2A_HTTP", f"remote returned HTTP {exc.code}") from None
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
    ) as exc:
        raise A2AError("A2A_TRANSPORT", type(exc).__name__) from None
    if len(raw) > max_bytes:
        raise A2AError(
            "A2A_BODY_LIMIT", "remote response exceeded the configured byte limit"
        )
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise A2AError(
            "A2A_MALFORMED", "remote response is not valid UTF-8 JSON"
        ) from None
    if not isinstance(document, Mapping):
        raise A2AError("A2A_MALFORMED", "remote response must be a JSON object")
    return document, raw


def _preflight(
    adapter: A2AAdapter, auth: str, limits: A2ALimits
) -> tuple[Mapping[str, Any], str, str, str, str]:
    _origin(adapter.agent_card_url)
    card, _ = _request_json(
        adapter.agent_card_url,
        auth=None,
        version=None,
        timeout=limits.timeout_seconds,
        max_bytes=limits.max_body_bytes,
    )
    if card.get("name") != adapter.expected_identity:
        raise A2AError(
            "A2A_IDENTITY_MISMATCH", "Agent Card identity did not match the profile"
        )
    schemes = card.get("securitySchemes")
    requirements = card.get("securityRequirements")
    if not isinstance(schemes, Mapping) or not isinstance(requirements, list):
        raise A2AError(
            "A2A_AUTH_SCHEME",
            "Agent Card must declare securitySchemes and securityRequirements",
        )
    bearer_names = {
        name
        for name, raw in schemes.items()
        if isinstance(name, str)
        and isinstance(raw, Mapping)
        and isinstance(raw.get("httpAuthSecurityScheme"), Mapping)
        and str(raw["httpAuthSecurityScheme"].get("scheme", "")).lower() == "bearer"
    }
    parsed_requirements: list[frozenset[str]] = []
    for requirement in requirements:
        if not isinstance(requirement, Mapping) or set(requirement) != {"schemes"}:
            raise A2AError(
                "A2A_AUTH_SCHEME", "Agent Card has a malformed security requirement"
            )
        required = requirement["schemes"]
        if not isinstance(required, Mapping) or not required:
            raise A2AError(
                "A2A_AUTH_SCHEME", "Agent Card has an empty security requirement"
            )
        names: set[str] = set()
        for name, scopes in required.items():
            if not isinstance(name, str) or name not in schemes:
                raise A2AError(
                    "A2A_AUTH_SCHEME",
                    "Agent Card requirement references an unknown security scheme",
                )
            if (
                not isinstance(scopes, Mapping)
                or set(scopes) != {"list"}
                or not isinstance(scopes["list"], list)
                or not all(isinstance(scope, str) for scope in scopes["list"])
            ):
                raise A2AError(
                    "A2A_AUTH_SCHEME",
                    "Agent Card security requirement scopes are malformed",
                )
            names.add(name)
        parsed_requirements.append(frozenset(names))
    compatible_bearer = [
        requirement
        for requirement in parsed_requirements
        if len(requirement) == 1 and requirement.issubset(bearer_names)
    ]
    if not bearer_names or len(compatible_bearer) != 1:
        raise A2AError(
            "A2A_AUTH_SCHEME",
            "Agent Card does not select exactly one Bearer security requirement",
        )
    interfaces = card.get("supportedInterfaces")
    if not isinstance(interfaces, list):
        raise A2AError("A2A_CARD", "Agent Card has no supportedInterfaces array")
    compatible = [
        item
        for item in interfaces
        if isinstance(item, Mapping)
        and item.get("protocolBinding") == "HTTP+JSON"
        and isinstance(item.get("protocolVersion"), str)
        and item["protocolVersion"].startswith("1.")
        and isinstance(item.get("url"), str)
    ]
    if len(compatible) != 1:
        raise A2AError(
            "A2A_PROTOCOL", "expected exactly one A2A 1.x HTTP+JSON interface"
        )
    interface = compatible[0]
    interface_url = str(interface["url"]).rstrip("/")
    if _origin(interface_url) != _origin(adapter.agent_card_url):
        raise A2AError(
            "A2A_ORIGIN_MISMATCH", "Agent Card redirected execution to another origin"
        )
    skills = card.get("skills")
    if not isinstance(skills, list):
        raise A2AError("A2A_CARD", "Agent Card has no skills array")
    by_id = {
        item.get("id"): item
        for item in skills
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    missing = sorted(set(adapter.allowed_skills) - set(by_id))
    if missing:
        raise A2AError(
            "A2A_CAPABILITY_MISMATCH", f"Agent Card lacks allowed skills: {missing}"
        )
    version = str(interface["protocolVersion"])
    card_digest = _digest(canonical_json(card))
    capability_digest = _digest(
        canonical_json(
            {
                "identity": card["name"],
                "interface": interface,
                "skills": [by_id[name] for name in sorted(adapter.allowed_skills)],
                "securitySchemes": schemes,
                "securityRequirements": requirements,
            }
        )
    )
    return card, interface_url, version, card_digest, capability_digest


def _task(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidate = value.get("task", value)
    if isinstance(candidate, Mapping) and isinstance(candidate.get("id"), str):
        return candidate
    return None


def _bound_task(value: Mapping[str, Any], expected_task_id: str) -> Mapping[str, Any]:
    """Return a Task only when it matches the durable local binding."""

    task = _task(value)
    if task is None:
        raise A2AError("A2A_TASK", "GetTask response did not contain a Task")
    if task["id"] != expected_task_id:
        raise A2AError(
            "A2A_TASK_IDENTITY",
            "remote Task ID did not match the durable local binding",
        )
    return task


def _message(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidate = value.get("message")
    if isinstance(candidate, Mapping):
        return candidate
    if isinstance(value.get("parts"), list):
        return value
    return None


def _status(task: Mapping[str, Any]) -> str:
    status = task.get("status")
    state = status.get("state") if isinstance(status, Mapping) else None
    if not isinstance(state, str):
        raise A2AError("A2A_TASK", "remote Task has no status state")
    return state


def _payload(container: Mapping[str, Any]) -> Any:
    artifacts = container.get("artifacts")
    sources: list[Mapping[str, Any]]
    if isinstance(artifacts, list):
        sources = [item for item in artifacts if isinstance(item, Mapping)]
    else:
        sources = [container]
    candidates: list[Any] = []
    for source in sources:
        parts = source.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            if "data" in part:
                candidates.append(part["data"])
            elif isinstance(part.get("text"), str):
                try:
                    candidates.append(json.loads(part["text"]))
                except json.JSONDecodeError:
                    continue
    if len(candidates) != 1:
        raise A2AError("A2A_ARTIFACT", "expected exactly one JSON result artifact")
    return candidates[0]


def execute_a2a(
    profile: Profile,
    *,
    prompt: str,
    run_id: str,
    node_id: str,
    attempt: int | None,
    state_path: str,
    idempotency_key: str | None,
    auth: str,
    limits: A2ALimits,
    cancelled: Callable[[], bool],
    writer: bool,
) -> A2AOutcome:
    """Submit once, persist Task identity, then poll the same task across resume."""

    adapter = profile.adapter
    if not isinstance(adapter, A2AAdapter):
        raise TypeError("profile is not configured for A2A")
    if not auth:
        raise A2AError(
            "A2A_AUTH", f"required environment variable {adapter.auth_env!r} is missing"
        )
    if attempt is None or attempt < 1:
        raise A2AError(
            "A2A_ATTEMPT_REQUIRED", "A2A execution requires an active attempt"
        )
    started = time.monotonic()
    _, interface_url, version, card_digest, capability_digest = _preflight(
        adapter, auth, limits
    )
    store = StateStore(state_path)
    binding = store.remote_task(run_id, node_id)
    expected_binding = {
        "profile": profile.name,
        "protocol_version": version,
        "interface_url": interface_url,
        "card_digest": card_digest,
        "capability_digest": capability_digest,
    }
    if binding is not None and any(
        binding[key] != value for key, value in expected_binding.items()
    ):
        raise A2AError(
            "A2A_IDENTITY_DRIFT", "pinned Agent Card or capabilities changed on resume"
        )

    statuses: list[str] = []
    raw = b""
    task_id = str(binding["task_id"]) if binding is not None else None
    current: Mapping[str, Any]

    def cancel_remote() -> None:
        assert task_id is not None
        _request_json(
            f"{interface_url}/tasks/{urllib.parse.quote(task_id, safe='')}:cancel",
            auth=auth,
            version=version,
            timeout=0.25,
            max_bytes=limits.max_body_bytes,
            method="POST",
            value={"id": task_id},
        )

    if task_id is not None:
        current, raw = _request_json(
            f"{interface_url}/tasks/{urllib.parse.quote(task_id, safe='')}?historyLength=1",
            auth=auth,
            version=version,
            timeout=max(0.01, limits.timeout_seconds - (time.monotonic() - started)),
            max_bytes=limits.max_body_bytes,
        )
        current = _bound_task(current, task_id)
    if task_id is None:
        stable_message_id = idempotency_key or _digest(
            canonical_json({"run_id": run_id, "node_id": node_id})
        )
        request = {
            "message": {
                "messageId": stable_message_id,
                "role": "ROLE_USER",
                "parts": [{"text": prompt}],
            },
            "configuration": {
                "acceptedOutputModes": ["application/json"],
                "returnImmediately": True,
            },
            "metadata": {"skillIds": list(adapter.allowed_skills)},
        }
        current, raw = _request_json(
            f"{interface_url}/message:send",
            auth=auth,
            version=version,
            timeout=max(0.01, limits.timeout_seconds - (time.monotonic() - started)),
            max_bytes=limits.max_body_bytes,
            method="POST",
            value=request,
        )
        task = _task(current)
        if task is None:
            message = _message(current)
            if message is None:
                raise A2AError(
                    "A2A_RESPONSE", "SendMessage returned neither Task nor Message"
                )
            payload = _payload(message)
            value, changeset = (
                (payload.get("result"), payload.get("changeset"))
                if writer and isinstance(payload, Mapping)
                else (payload, None)
            )
            return A2AOutcome(
                value,
                changeset,
                None,
                card_digest,
                capability_digest,
                version,
                interface_url,
                _digest(raw),
                len(raw),
                tuple(statuses),
            )
        task_id = str(task["id"])
        if not task_id or len(task_id) > 512:
            raise A2AError("A2A_TASK", "remote Task ID is empty or too large")
        store.bind_remote_task(
            run_id,
            node_id,
            task_id=task_id,
            attempt_number=attempt,
            profile=profile.name,
            protocol_version=version,
            interface_url=interface_url,
            card_digest=card_digest,
            capability_digest=capability_digest,
        )
        current = _bound_task(task, task_id)

    while True:
        if cancelled():
            try:
                cancel_remote()
            finally:
                raise A2AError(
                    "A2A_CANCELLED", "local run cancellation requested"
                ) from None
        state = _status(current)
        statuses.append(state)
        if state in _TERMINAL:
            break
        if state not in _ACTIVE:
            raise A2AError(
                "A2A_TASK_STATE", f"remote task requires unsupported handling: {state}"
            )
        remaining = limits.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            try:
                cancel_remote()
            except A2AError:
                pass
            raise A2AError(
                "A2A_TIMEOUT", "remote task exceeded its wall-clock deadline"
            )
        time.sleep(min(limits.poll_interval_seconds, remaining))
        try:
            current, raw = _request_json(
                f"{interface_url}/tasks/{urllib.parse.quote(task_id, safe='')}?historyLength=1",
                auth=auth,
                version=version,
                timeout=remaining,
                max_bytes=limits.max_body_bytes,
            )
        except A2AError as exc:
            if exc.code != "A2A_TRANSPORT":
                raise
            if time.monotonic() - started >= limits.timeout_seconds:
                try:
                    cancel_remote()
                except A2AError:
                    pass
                raise A2AError(
                    "A2A_TIMEOUT", "remote task exceeded its wall-clock deadline"
                ) from None
            continue
        current = _bound_task(current, task_id)

    if state != "TASK_STATE_COMPLETED":
        raise A2AError("A2A_REMOTE_FAILED", f"remote task ended in {state}")
    payload = _payload(current)
    value, changeset = (
        (payload.get("result"), payload.get("changeset"))
        if writer and isinstance(payload, Mapping)
        else (payload, None)
    )
    return A2AOutcome(
        value,
        changeset,
        task_id,
        card_digest,
        capability_digest,
        version,
        interface_url,
        _digest(raw),
        len(raw),
        tuple(statuses),
    )
