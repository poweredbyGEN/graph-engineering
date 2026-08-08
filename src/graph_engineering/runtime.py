"""A leased, fenced ready-queue runtime for validated workflow contracts."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .artifacts import ArtifactError, ArtifactStore, canonical_json
from .contracts import validate_workflow
from .state import RunLease, RunLeaseError, StaleAttemptError, StateStore


@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    node_id: str
    attempt: int
    inputs: Mapping[str, Any]
    cancelled: Callable[[], bool]
    idempotency_key: str | None = None


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    evidence: str = ""


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    nodes: Mapping[str, Mapping[str, Any]]


Executor = Callable[[ExecutionContext], Mapping[str, Any]]
CheckRunner = Callable[
    [dict[str, Any], ExecutionContext, Mapping[str, Any]], CheckResult | bool
]

REPAIR_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "code",
        "integration_node",
        "integration_attempt",
        "check_id",
        "evidence",
        "failure_digest",
    ],
    "properties": {
        "code": {"const": "CHECK_FAILED"},
        "integration_node": {"type": "string", "minLength": 1},
        "integration_attempt": {"type": "integer", "minimum": 1},
        "check_id": {"type": "string", "minLength": 1},
        "evidence": {"type": "string"},
        "failure_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}


@dataclass(frozen=True)
class _AttemptResult:
    number: int
    passed: bool
    digest: str
    error: str | None = None
    artifacts: Mapping[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    failure: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _ActiveAttempt:
    node_id: str
    number: int
    deadline: float


def _failure_digest(category: str, detail: str) -> str:
    return hashlib.sha256(
        canonical_json({"category": category, "detail": detail})
    ).hexdigest()


def _daemon_future(call: Callable[[], Any], *, name: str) -> Future[Any]:
    """Run in a daemon thread so an uncooperative callable cannot hold runtime shutdown."""

    future: Future[Any] = Future()

    def invoke() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(call())
        except BaseException as exc:  # noqa: BLE001 - transport all thread outcomes
            future.set_exception(exc)

    threading.Thread(target=invoke, name=name, daemon=True).start()
    return future


class Scheduler:
    """Run nodes when dependencies pass while one fenced lease owns all mutations."""

    TERMINAL: ClassVar[set[str]] = {
        "succeeded",
        "failed",
        "optional_failed",
        "blocked",
        "cancelled",
        "uncertain",
    }

    def __init__(
        self,
        workflow: dict[str, Any],
        state_path: str | Path,
        artifact_root: str | Path,
        executors: Mapping[str, Executor],
        check_runner: CheckRunner | None,
        *,
        lease_ttl_seconds: float = 30.0,
    ):
        validate_workflow(workflow)
        self.workflow = workflow
        self.nodes = {node["id"]: node for node in workflow["nodes"]}
        self.state = StateStore(state_path)
        self.artifacts = ArtifactStore(artifact_root)
        self.executors = dict(executors)
        self.check_runner = check_runner
        self.lease_ttl_seconds = lease_ttl_seconds

    def _executor(self, node: dict[str, Any]) -> Executor | None:
        return self.executors.get(node["id"]) or self.executors.get(node["kind"])

    def _replay_safe(self, node: dict[str, Any]) -> bool:
        effect = node.get("effect")
        if effect in {"none", "read"}:
            return True
        if effect == "idempotent_write":
            return bool(node.get("idempotency_key"))
        return effect is None and node.get("permission") == "read"

    def _idempotency_key(self, run_id: str, node: Mapping[str, Any]) -> str | None:
        declared = node.get("idempotency_key")
        if node.get("effect") != "idempotent_write" or not declared:
            return None
        return hashlib.sha256(
            canonical_json(
                {
                    "workflow_id": self.workflow["id"],
                    "run_id": run_id,
                    "node_id": node["id"],
                    "declared_key": declared,
                }
            )
        ).hexdigest()

    def _inputs(self, run_id: str, node: dict[str, Any]) -> dict[str, Any]:
        inputs: dict[str, Any] = {}
        for name, binding in node.get("inputs", {}).items():
            producer, output = binding.split(".", 1)
            record = self.state.artifact(run_id, producer, output)
            if record is None:
                raise ArtifactError(
                    f"required input {binding} has no accepted artifact"
                )
            schema = json.loads(record["schema_json"])
            inputs[name] = self.artifacts.get(record["digest"], schema).value
        for record in self.state.repair_inputs(run_id, node["id"]):
            name = record["input_name"]
            if name in inputs:
                raise ArtifactError(
                    f"repair input {name!r} collides with a static input"
                )
            schema = json.loads(record["schema_json"])
            inputs[name] = self.artifacts.get(record["digest"], schema).value
        return inputs

    def _run_check(
        self,
        check: dict[str, Any],
        context: ExecutionContext,
        outputs: Mapping[str, Any],
    ) -> CheckResult:
        assert self.check_runner is not None
        timeout = check.get("timeout_seconds")
        if timeout is None:
            raw = self.check_runner(check, context, outputs)
        else:
            future = _daemon_future(
                lambda: self.check_runner(check, context, outputs),
                name=f"graph-check-{context.node_id}-{check['id']}",
            )
            try:
                raw = future.result(timeout=timeout)
            except FutureTimeoutError as exc:
                future.add_done_callback(lambda completed: completed.exception())
                raise TimeoutError(
                    f"check {check['id']!r} exceeded {timeout}s; adapter must terminate its process tree"
                ) from exc
        if isinstance(raw, bool):
            return CheckResult(raw)
        if not isinstance(raw, CheckResult):
            raise TypeError(f"check {check['id']!r} returned an invalid result")
        return raw

    def _attempt(
        self,
        run_id: str,
        node: dict[str, Any],
        number: int,
        cancel_event: threading.Event,
    ) -> _AttemptResult:
        node_id = node["id"]
        try:
            executor = self._executor(node)
            if executor is None:
                raise RuntimeError(
                    f"no executor registered for node {node_id!r} or kind {node['kind']!r}"
                )
            if not node.get("checks"):
                raise RuntimeError(
                    f"node {node_id!r} has no deterministic acceptance check"
                )
            if self.check_runner is None:
                raise RuntimeError("no deterministic check runner registered")
            context = ExecutionContext(
                run_id=run_id,
                node_id=node_id,
                attempt=number,
                inputs=self._inputs(run_id, node),
                cancelled=lambda: (
                    cancel_event.is_set() or self.state.cancel_requested(run_id)
                ),
                idempotency_key=self._idempotency_key(run_id, node),
            )
            if context.cancelled():
                raise RuntimeError("run cancelled")
            outputs = dict(executor(context))
            if context.cancelled():
                raise RuntimeError("run cancelled")
            declared = node.get("outputs", {})
            missing = sorted(set(declared) - set(outputs))
            extra = sorted(set(outputs) - set(declared))
            if missing or extra:
                raise ArtifactError(
                    f"output contract mismatch: missing={missing}, extra={extra}"
                )

            checked: dict[str, tuple[str, dict[str, Any]]] = {}
            for output_name, contract in declared.items():
                schema = contract["schema"]
                if not isinstance(schema, dict):
                    raise ArtifactError(
                        f"external schema references are not supported: {schema!r}"
                    )
                artifact = self.artifacts.put(outputs[output_name], schema)
                checked[output_name] = (artifact.digest, schema)

            evidence: list[dict[str, Any]] = []
            for check in node["checks"]:
                result = self._run_check(check, context, outputs)
                evidence.append(
                    {
                        "id": check["id"],
                        "passed": result.passed,
                        "evidence": result.evidence,
                    }
                )
                if not result.passed:
                    detail = f"check {check['id']!r} failed: {result.evidence}"
                    failure = {
                        "code": "CHECK_FAILED",
                        "check_id": check["id"],
                        "evidence": result.evidence,
                    }
                    return _AttemptResult(
                        number,
                        False,
                        _failure_digest("CHECK_FAILED", detail),
                        f"RuntimeError: {detail}",
                        failure=failure,
                    )

            digest = hashlib.sha256(
                canonical_json({"artifacts": checked, "checks": evidence})
            ).hexdigest()
            return _AttemptResult(number, True, digest, artifacts=checked)
        except Exception as exc:  # noqa: BLE001 - executor failures are attempt data
            detail = f"{type(exc).__name__}: {exc}"
            return _AttemptResult(
                number,
                False,
                _failure_digest(type(exc).__name__, str(exc)),
                detail,
                failure={"code": type(exc).__name__, "evidence": str(exc)},
            )

    def _try_repair(
        self,
        run_id: str,
        node: dict[str, Any],
        failure: Mapping[str, Any] | None,
        failure_digest: str,
        lease: RunLease,
    ) -> bool:
        repair = node.get("repair")
        if repair is None or failure is None or failure.get("code") != "CHECK_FAILED":
            return False
        check_id = failure.get("check_id")
        routes = [route for route in repair["routes"] if check_id in route["check_ids"]]
        if len(routes) != 1:
            return False
        route = routes[0]
        row = self.state.node_rows(run_id)[node["id"]]
        payload = {
            "code": "CHECK_FAILED",
            "integration_node": node["id"],
            "integration_attempt": int(row["attempt_count"]),
            "check_id": check_id,
            "evidence": str(failure.get("evidence", "")),
            "failure_digest": failure_digest,
        }
        artifact = self.artifacts.put(payload, REPAIR_EVIDENCE_SCHEMA)
        targets: dict[str, tuple[str, str, dict[str, Any], int]] = {}
        for target in route["targets"]:
            target_node = self.nodes[target["node"]]
            if target_node["permission"] != "write" or not self._replay_safe(
                target_node
            ):
                return False
            targets[target["node"]] = (
                target["input"],
                artifact.digest,
                REPAIR_EVIDENCE_SCHEMA,
                target_node.get("retry", {}).get("max_attempts", 1),
            )
        return self.state.route_repair(
            run_id,
            node["id"],
            route["id"],
            failure_digest,
            max_rounds=route["max_rounds"],
            no_progress_limit=route["no_progress_limit"],
            targets=targets,
            integration_attempt_limit=node.get("retry", {}).get("max_attempts", 1),
            lease=lease,
        )

    def _finalize_attempt(
        self, run_id: str, node: dict[str, Any], result: _AttemptResult, lease: RunLease
    ) -> None:
        node_id = node["id"]
        if result.passed:
            self.state.succeed_attempt(
                run_id,
                node_id,
                result.number,
                result.digest,
                dict(result.artifacts),
                lease,
            )
            return

        no_progress = self.state.finish_attempt(
            run_id,
            node_id,
            result.number,
            "failed",
            result.digest,
            result.error,
            lease,
            failure=result.failure,
        )
        if node.get("repair") is not None:
            self._try_repair(run_id, node, result.failure, result.digest, lease)
            return
        retry = node.get("retry", {})
        max_attempts = min(
            retry.get(
                "max_attempts", self.workflow["budgets"]["max_attempts_per_node"]
            ),
            self.workflow["budgets"]["max_attempts_per_node"],
        )
        no_progress_limit = retry.get("no_progress_limit", 1)
        retry_available = (
            result.number < max_attempts and no_progress < no_progress_limit
        )
        if retry_available and not self._replay_safe(node):
            self.state.set_node_status(
                run_id,
                node_id,
                "uncertain",
                "effect may have occurred; explicit reconciliation is required",
                lease,
            )
        elif retry_available:
            self.state.set_node_status(run_id, node_id, "pending", result.error, lease)
        elif not node["required"]:
            self.state.set_node_status(
                run_id, node_id, "optional_failed", result.error, lease
            )

    def _validate_accepted(
        self, run_id: str, lease: RunLease, *, require_workflow_outputs: bool = False
    ) -> bool:
        rows = self.state.node_rows(run_id)
        valid = True
        for node_id, node in self.nodes.items():
            if rows[node_id]["status"] != "succeeded":
                continue
            for output_name, contract in node.get("outputs", {}).items():
                record = self.state.artifact(run_id, node_id, output_name)
                try:
                    if record is None:
                        raise ArtifactError(
                            f"accepted artifact {node_id}.{output_name} is missing"
                        )
                    schema = contract["schema"]
                    if not isinstance(schema, dict):
                        raise ArtifactError(
                            f"external schema references are not supported: {schema!r}"
                        )
                    self.artifacts.get(record["digest"], schema)
                except ArtifactError as exc:
                    self.state.invalidate_node(run_id, node_id, str(exc), lease)
                    valid = False
        if require_workflow_outputs:
            for binding in self.workflow["outputs"].values():
                node_id, output_name = binding.split(".", 1)
                record = self.state.artifact(run_id, node_id, output_name)
                if rows[node_id]["status"] != "succeeded" or record is None:
                    self.state.invalidate_node(
                        run_id,
                        node_id,
                        f"declared workflow output {binding} is missing",
                        lease,
                    )
                    valid = False
        return valid

    def reconcile_node(self, run_id: str, node_id: str, decision: str) -> None:
        lease = self.state.acquire_lease(run_id, ttl_seconds=self.lease_ttl_seconds)
        try:
            self.state.reconcile_node(run_id, node_id, decision, lease)
        finally:
            self.state.release_lease(lease)

    def run(
        self,
        run_id: str | None = None,
        *,
        resume: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> RunResult:
        cancel_event = cancel_event or threading.Event()
        if resume:
            if run_id is None:
                raise ValueError("resume requires run_id")
            stored = self.state.run(run_id)
            if stored["workflow"] != self.workflow:
                raise ValueError("resume workflow does not match persisted workflow")
        else:
            run_id = self.state.create_run(self.workflow, run_id)
            stored = self.state.run(run_id)

        lease = self.state.acquire_lease(
            run_id, token=uuid.uuid4().hex, ttl_seconds=self.lease_ttl_seconds
        )
        budgets = self.workflow["budgets"]
        # A resume continues the original workflow budget; it must not mint a fresh
        # wall-clock allowance on every process restart. Wall time is persisted,
        # while monotonic time remains the safe clock for this process's wait loop.
        remaining_seconds = max(
            0.0,
            float(stored["created_at"])
            + float(budgets["timeout_seconds"])
            - time.time(),
        )
        workflow_deadline = time.monotonic() + remaining_seconds
        renew_at = time.monotonic() + self.lease_ttl_seconds / 3
        active: dict[Future[_AttemptResult], _ActiveAttempt] = {}

        try:
            if not self._validate_accepted(
                run_id,
                lease,
                require_workflow_outputs=resume and stored["status"] == "succeeded",
            ):
                self.state.finish_run(run_id, "failed", lease)
                return RunResult(run_id, "failed", self.state.node_rows(run_id))
            if resume and stored["status"] == "running":
                uncertain = self.state.recover_interrupted(
                    run_id,
                    lease,
                    {
                        node_id: self._replay_safe(node)
                        for node_id, node in self.nodes.items()
                    },
                )
                if uncertain:
                    self.state.finish_run(run_id, "needs_reconciliation", lease)
                    return RunResult(
                        run_id, "needs_reconciliation", self.state.node_rows(run_id)
                    )

            while True:
                now = time.monotonic()
                if now >= renew_at:
                    self.state.renew_lease(lease)
                    renew_at = now + self.lease_ttl_seconds / 3
                rows = self.state.node_rows(run_id)
                run_in_progress = self.state.run(run_id)["status"] == "running"
                if run_in_progress and (
                    cancel_event.is_set()
                    or self.state.cancel_requested(run_id)
                    or now >= workflow_deadline
                ):
                    cancel_event.set()
                    for future in active:
                        future.add_done_callback(
                            lambda completed: completed.exception()
                        )
                    active.clear()
                    self.state.finish_run(run_id, "cancelled", lease)
                    return RunResult(run_id, "cancelled", self.state.node_rows(run_id))
                for node_id, node in sorted(self.nodes.items()):
                    if rows[node_id]["status"] != "failed" or not node.get("repair"):
                        continue
                    failure = self.state.last_attempt_failure(run_id, node_id)
                    digest = rows[node_id].get("last_digest")
                    if failure is not None and isinstance(digest, str):
                        self._try_repair(run_id, node, failure, digest, lease)
                rows = self.state.node_rows(run_id)

                for node_id, node in sorted(self.nodes.items()):
                    if rows[node_id]["status"] != "pending":
                        continue
                    dependencies = [
                        rows[dependency] for dependency in node.get("needs", [])
                    ]
                    if any(
                        dependency["status"]
                        in {"failed", "blocked", "cancelled", "uncertain"}
                        for dependency in dependencies
                    ):
                        self.state.set_node_status(
                            run_id,
                            node_id,
                            "blocked",
                            "a required dependency did not succeed",
                            lease,
                        )

                rows = self.state.node_rows(run_id)
                if not cancel_event.is_set():
                    attempts = sum(row["attempt_count"] for row in rows.values())
                    slots = budgets["max_concurrency"] - len(active)
                    active_nodes = {entry.node_id for entry in active.values()}
                    for node_id, node in sorted(self.nodes.items()):
                        if slots <= 0 or attempts >= budgets["max_total_attempts"]:
                            break
                        if (
                            rows[node_id]["status"] != "pending"
                            or node_id in active_nodes
                        ):
                            continue
                        dependency_statuses = [
                            rows[dependency]["status"]
                            for dependency in node.get("needs", [])
                        ]
                        if not all(
                            status in {"succeeded", "optional_failed"}
                            for status in dependency_statuses
                        ):
                            continue
                        number = self.state.start_attempt(run_id, node_id, lease)
                        timeout = node.get(
                            "timeout_seconds", budgets["timeout_seconds"]
                        )
                        deadline = min(workflow_deadline, time.monotonic() + timeout)
                        future = _daemon_future(
                            lambda n=node, num=number: self._attempt(
                                run_id, n, num, cancel_event
                            ),
                            name=f"graph-node-{node_id}-{number}",
                        )
                        active[future] = _ActiveAttempt(node_id, number, deadline)
                        attempts += 1
                        slots -= 1

                progressed = False
                for future, entry in list(active.items()):
                    if not future.done():
                        continue
                    active.pop(future)
                    try:
                        self._finalize_attempt(
                            run_id, self.nodes[entry.node_id], future.result(), lease
                        )
                    except StaleAttemptError:
                        pass
                    progressed = True

                now = time.monotonic()
                for future, entry in list(active.items()):
                    if now < entry.deadline:
                        continue
                    active.pop(future)
                    future.add_done_callback(lambda completed: completed.exception())
                    timeout = self.nodes[entry.node_id].get(
                        "timeout_seconds", budgets["timeout_seconds"]
                    )
                    detail = (
                        f"node exceeded {timeout}s; result fenced; executor adapter must terminate "
                        "its process tree"
                    )
                    result = _AttemptResult(
                        entry.number,
                        False,
                        _failure_digest("TimeoutError", detail),
                        f"TimeoutError: {detail}",
                    )
                    self._finalize_attempt(
                        run_id, self.nodes[entry.node_id], result, lease
                    )
                    progressed = True

                if active:
                    if not progressed:
                        time.sleep(0.01)
                    continue
                if progressed:
                    continue

                rows = self.state.node_rows(run_id)
                if all(row["status"] in self.TERMINAL for row in rows.values()):
                    if any(row["status"] == "uncertain" for row in rows.values()):
                        self.state.finish_run(run_id, "needs_reconciliation", lease)
                        return RunResult(
                            run_id,
                            "needs_reconciliation",
                            self.state.node_rows(run_id),
                        )
                    if any(row["status"] == "cancelled" for row in rows.values()):
                        self.state.finish_run(run_id, "cancelled", lease)
                        return RunResult(
                            run_id, "cancelled", self.state.node_rows(run_id)
                        )
                    required_bad = any(
                        bool(row["required"]) and row["status"] != "succeeded"
                        for row in rows.values()
                    )
                    status = "failed" if required_bad else "succeeded"
                    if status == "succeeded" and not self._validate_accepted(
                        run_id, lease, require_workflow_outputs=True
                    ):
                        status = "failed"
                    self.state.finish_run(run_id, status, lease)
                    return RunResult(run_id, status, self.state.node_rows(run_id))
                pending = [row for row in rows.values() if row["status"] == "pending"]
                if (
                    pending
                    and sum(row["attempt_count"] for row in rows.values())
                    >= budgets["max_total_attempts"]
                ):
                    for row in pending:
                        self.state.set_node_status(
                            run_id,
                            row["node_id"],
                            "failed",
                            "total attempt budget exhausted",
                            lease,
                        )
                    continue
                raise RuntimeError("scheduler made no progress with non-terminal nodes")
        finally:
            self.state.release_lease(lease)


__all__ = [
    "CheckResult",
    "ExecutionContext",
    "RunLeaseError",
    "RunResult",
    "Scheduler",
]
