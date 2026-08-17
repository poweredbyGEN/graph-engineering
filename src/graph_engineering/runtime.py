"""A leased, fenced ready-queue runtime for validated workflow contracts."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from jsonschema import Draft202012Validator

from .artifacts import ArtifactError, ArtifactStore, canonical_json
from .builtins import BuiltinOperationError, evaluate_predicate
from .contracts import validate_workflow
from .state import (
    ProgressBudgetExpiredError,
    RunLease,
    RunLeaseError,
    StaleAttemptError,
    StateStore,
)


@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    node_id: str
    attempt: int
    inputs: Mapping[str, Any]
    cancelled: Callable[[], bool]
    join: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    deadline_at: float | None = None


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
    artifacts: Mapping[str, _PendingArtifact] = field(default_factory=dict)
    failure: Mapping[str, Any] | None = None
    deterministic_check_count: int | None = None


@dataclass(frozen=True)
class _PendingArtifact:
    digest: str
    schema: dict[str, Any]
    value: Any


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
        "skipped",
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

    def _artifact_value(self, run_id: str, binding: str) -> tuple[Any, str]:
        producer, output = binding.split(".", 1)
        record = self.state.artifact(run_id, producer, output)
        if record is None:
            raise ArtifactError(f"route source {binding!r} has no accepted artifact")
        schema = json.loads(record["schema_json"])
        return self.artifacts.get(record["digest"], schema).value, str(record["digest"])

    @staticmethod
    def _path_value(value: Any, path: list[str | int]) -> Any:
        current = value
        for component in path:
            if isinstance(component, int):
                if not isinstance(current, list) or component >= len(current):
                    raise BuiltinOperationError(
                        f"route path component {component!r} is unavailable"
                    )
                current = current[component]
            else:
                if not isinstance(current, Mapping) or component not in current:
                    raise BuiltinOperationError(
                        f"route path component {component!r} is unavailable"
                    )
                current = current[component]
        return current

    def _process_conditional_routes(
        self, run_id: str, rows: Mapping[str, Mapping[str, Any]], lease: RunLease
    ) -> None:
        for node_id, node in sorted(self.nodes.items()):
            route = node.get("route")
            if route is None or rows[node_id]["status"] != "pending":
                continue
            producer = route["source"].partition(".")[0]
            if rows[producer]["status"] != "succeeded":
                continue
            try:
                value, digest = self._artifact_value(run_id, route["source"])
                selected = self._path_value(value, route.get("path", []))
                matched = evaluate_predicate(selected, route["predicate"])
                self.state.record_route_decision(
                    run_id, node_id, digest, matched, lease
                )
            except Exception as exc:  # noqa: BLE001 - a malformed route fails closed
                self.state.set_node_status(
                    run_id, node_id, "failed", f"conditional route failed: {exc}", lease
                )

    def _loop_region(self, controller_id: str, target_id: str) -> tuple[str, ...]:
        def ancestors(node_id: str) -> set[str]:
            result: set[str] = set()
            pending = list(self.nodes[node_id].get("needs", ()))
            while pending:
                current = pending.pop()
                if current in result:
                    continue
                result.add(current)
                pending.extend(self.nodes[current].get("needs", ()))
            return result

        controller_ancestors = ancestors(controller_id)
        region = {
            node_id
            for node_id in self.nodes
            if node_id in {target_id, controller_id}
            or (target_id in ancestors(node_id) and node_id in controller_ancestors)
        }
        return tuple(sorted(region))

    def _process_loops(
        self, run_id: str, rows: Mapping[str, Mapping[str, Any]], lease: RunLease
    ) -> None:
        for controller_id, node in sorted(self.nodes.items()):
            loop = node.get("loop")
            row = rows[controller_id]
            if loop is None or row["status"] != "succeeded":
                continue
            try:
                value, _digest = self._artifact_value(
                    run_id, f"{controller_id}.{loop['output']}"
                )
                selected = self._path_value(value, loop.get("path", []))
                matched = evaluate_predicate(selected, loop["predicate"])
                self.state.apply_loop_decision(
                    run_id,
                    controller_id,
                    int(row["attempt_count"]),
                    matched,
                    int(loop["max_iterations"]),
                    self._loop_region(controller_id, loop["target"]),
                    lease,
                )
            except Exception as exc:  # noqa: BLE001 - a control failure cannot advance
                self.state.set_node_status(
                    run_id,
                    controller_id,
                    "failed",
                    f"bounded loop failed: {exc}",
                    lease,
                )

    def _join_snapshot(
        self, node: Mapping[str, Any], rows: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, Any] | None:
        """Evaluate a join from persisted node state, never from model judgment."""

        join = node.get("join")
        if join is None:
            return None
        dependency_ids = tuple(node.get("needs", ()))
        settlements = {
            dependency: str(rows[dependency]["status"]) for dependency in dependency_ids
        }
        terminal = {
            dependency: status
            for dependency, status in settlements.items()
            if status in self.TERMINAL
        }
        passed = sum(status == "succeeded" for status in terminal.values())
        cancelled = sum(status == "cancelled" for status in terminal.values())
        failed = len(terminal) - passed - cancelled
        expected = len(dependency_ids)
        received = len(terminal)
        missing = expected - received
        policy = str(join["policy"])
        if policy == "n_of_m":
            threshold = int(join["n"])
        elif policy == "majority":
            threshold = expected // 2 + 1
        elif policy == "any":
            threshold = 1
        else:
            threshold = expected

        if policy == "all":
            required_failure = any(
                settlements[dependency]
                in {"failed", "blocked", "cancelled", "uncertain", "skipped"}
                or (
                    settlements[dependency] == "optional_failed"
                    and bool(rows[dependency]["required"])
                )
                for dependency in dependency_ids
            )
            accepted = all(
                status in {"succeeded", "optional_failed"}
                for status in settlements.values()
            )
            decision = (
                "failed" if required_failure else "succeeded" if accepted else "waiting"
            )
        elif policy == "all_settled":
            decision = "succeeded" if missing == 0 else "waiting"
        elif passed >= threshold:
            decision = "succeeded"
        elif passed + missing < threshold:
            decision = "failed"
        else:
            decision = "waiting"

        return {
            "policy": policy,
            "threshold": threshold,
            "expected": expected,
            "received": received,
            "passed": passed,
            "failed": failed,
            "cancelled": cancelled,
            "missing": missing,
            "decision": decision,
            "settlements": settlements,
        }

    def _persist_join_states(
        self,
        run_id: str,
        rows: Mapping[str, Mapping[str, Any]],
        lease: RunLease,
    ) -> dict[str, str]:
        decisions: dict[str, str] = {}
        for node_id, node in sorted(self.nodes.items()):
            snapshot = self._join_snapshot(node, rows)
            if snapshot is None:
                continue
            persisted = self.state.record_join_state(run_id, node_id, snapshot, lease)
            decisions[node_id] = str(persisted["decision"])
        return decisions

    def _quorum_member_dispatchable(
        self,
        node_id: str,
        rows: Mapping[str, Mapping[str, Any]],
        active_nodes: set[str],
        join_decisions: Mapping[str, str],
    ) -> bool:
        """Keep reserve voters idle until the active quorum slice proves insufficient."""

        for consumer_id, consumer in self.nodes.items():
            join = consumer.get("join")
            if (
                join is None
                or join["policy"] not in {"any", "n_of_m", "majority"}
                or node_id not in consumer.get("needs", ())
                or rows[consumer_id]["status"] not in {"pending", "running"}
            ):
                continue
            decision = join_decisions.get(consumer_id, "waiting")
            if decision != "waiting":
                return False
            dependencies = sorted(consumer["needs"])
            threshold = (
                1
                if join["policy"] == "any"
                else int(join["n"])
                if join["policy"] == "n_of_m"
                else len(dependencies) // 2 + 1
            )
            passed = sum(
                rows[dependency]["status"] == "succeeded" for dependency in dependencies
            )
            active_count = sum(
                dependency in active_nodes or rows[dependency]["status"] == "running"
                for dependency in dependencies
            )
            capacity = max(0, threshold - passed - active_count)
            candidates = [
                dependency
                for dependency in dependencies
                if rows[dependency]["status"] == "pending"
                and dependency not in active_nodes
            ]
            if node_id not in candidates[:capacity]:
                return False
        return True

    def _run_check(
        self,
        check: dict[str, Any],
        context: ExecutionContext,
        outputs: Mapping[str, Any],
    ) -> CheckResult:
        assert self.check_runner is not None
        timeout = check.get("timeout_seconds")
        if context.deadline_at is not None:
            remaining = context.deadline_at - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"check {check['id']!r} reached the persisted progress deadline"
                )
            timeout = remaining if timeout is None else min(float(timeout), remaining)
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
        deadline_at: float = float("inf"),
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
                    cancel_event.is_set()
                    or self.state.cancel_requested(run_id)
                    or time.time() >= deadline_at
                ),
                join=self.state.join_state(run_id, node_id) or {},
                idempotency_key=self._idempotency_key(run_id, node),
                deadline_at=deadline_at,
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

            checked: dict[str, _PendingArtifact] = {}
            for output_name, contract in declared.items():
                schema = contract["schema"]
                if not isinstance(schema, dict):
                    raise ArtifactError(
                        f"external schema references are not supported: {schema!r}"
                    )
                errors = sorted(
                    Draft202012Validator(schema).iter_errors(outputs[output_name]),
                    key=lambda error: list(error.path),
                )
                if errors:
                    raise ArtifactError(
                        f"artifact schema validation failed: {errors[0].message}"
                    )
                digest = hashlib.sha256(
                    canonical_json(outputs[output_name])
                ).hexdigest()
                checked[output_name] = _PendingArtifact(
                    digest=digest,
                    schema=schema,
                    value=outputs[output_name],
                )

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
                        artifacts=checked,
                        failure=failure,
                        deterministic_check_count=len(evidence),
                    )

            # An output schema defines what can be preserved as evidence. An optional
            # acceptance schema defines the narrower result that is allowed to advance
            # the graph. Evaluate it after the declared project checks and only after
            # every output has been content-addressed, so even a check-green rejected
            # result remains inspectable without becoming an accepted edge.
            for output_name, contract in declared.items():
                acceptance_schema = contract.get("acceptance_schema")
                if acceptance_schema is None:
                    continue
                try:
                    errors = sorted(
                        Draft202012Validator(acceptance_schema).iter_errors(
                            outputs[output_name]
                        ),
                        key=lambda error: list(error.path),
                    )
                    if errors:
                        raise ArtifactError(
                            f"artifact schema validation failed: {errors[0].message}"
                        )
                except Exception as exc:  # noqa: BLE001 - preserve typed rejection data
                    evidence_detail = str(exc)[:2000]
                    code = (
                        "OUTPUT_NOT_ACCEPTED"
                        if isinstance(exc, ArtifactError)
                        else "OUTPUT_ACCEPTANCE_ERROR"
                    )
                    detail = (
                        f"output {output_name!r} was preserved but not accepted: "
                        f"{evidence_detail}"
                    )
                    failure = {
                        "code": code,
                        "output_name": output_name,
                        "evidence": evidence_detail,
                        "artifact_receipts": {
                            name: pending.digest
                            for name, pending in sorted(checked.items())
                        },
                    }
                    return _AttemptResult(
                        number,
                        False,
                        _failure_digest(code, detail),
                        f"{type(exc).__name__}: {detail}",
                        artifacts=checked,
                        failure=failure,
                        deterministic_check_count=len(evidence),
                    )

            digest = hashlib.sha256(
                canonical_json(
                    {
                        "artifacts": {
                            name: pending.digest
                            for name, pending in sorted(checked.items())
                        },
                        "checks": evidence,
                    }
                )
            ).hexdigest()
            return _AttemptResult(
                number,
                True,
                digest,
                artifacts=checked,
                deterministic_check_count=len(evidence),
            )
        except Exception as exc:  # noqa: BLE001 - executor failures are attempt data
            code = getattr(exc, "code", type(exc).__name__)
            if not isinstance(code, str) or not code:
                code = type(exc).__name__
            detail = f"{type(exc).__name__}: {exc}"
            return _AttemptResult(
                number,
                False,
                _failure_digest(code, str(exc)),
                detail,
                failure={"code": code, "evidence": str(exc)},
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
        artifacts: dict[str, tuple[str, dict[str, Any]]] = {}
        for name, pending in sorted(result.artifacts.items()):
            artifact = self.artifacts.put(pending.value, pending.schema)
            if artifact.digest != pending.digest:
                raise ArtifactError(
                    f"artifact {name!r} changed between validation and persistence"
                )
            artifacts[name] = (artifact.digest, pending.schema)
        if result.passed:
            self.state.succeed_attempt(
                run_id,
                node_id,
                result.number,
                result.digest,
                artifacts,
                lease,
                deterministic_check_count=result.deterministic_check_count,
            )
            return

        no_progress, progress = self.state.finish_attempt(
            run_id,
            node_id,
            result.number,
            "failed",
            result.digest,
            result.error,
            lease,
            failure=result.failure,
            artifacts=artifacts,
            deterministic_check_count=result.deterministic_check_count,
        )
        if node.get("repair") is not None:
            if progress["decision"] == "continue":
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
            result.number < max_attempts
            and no_progress < no_progress_limit
            and progress["decision"] == "continue"
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
                    acceptance_schema = contract.get("acceptance_schema")
                    if acceptance_schema is not None:
                        self.artifacts.get(record["digest"], acceptance_schema)
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
        lifecycle_resume: bool | None = None,
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
            run_id,
            token=uuid.uuid4().hex,
            ttl_seconds=self.lease_ttl_seconds,
            lifecycle_resume=resume if lifecycle_resume is None else lifecycle_resume,
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
        completion = threading.Event()
        completed: deque[Future[_AttemptResult]] = deque()
        completion_lock = threading.Lock()

        def mark_completed(future: Future[_AttemptResult]) -> None:
            with completion_lock:
                completed.append(future)
            completion.set()

        def launch(node_id: str, node: dict[str, Any]) -> bool:
            if not self.state.admit_attempt(run_id, node_id, lease, resumed=resume):
                return False
            try:
                number = self.state.start_attempt(run_id, node_id, lease)
            except ProgressBudgetExpiredError:
                return False
            timeout = node.get("timeout_seconds", budgets["timeout_seconds"])
            progress_deadline_at = self.state.progress_deadline(run_id, node_id)
            if progress_deadline_at is None:
                raise RuntimeError(f"progress deadline for {node_id!r} was not started")
            progress_remaining = max(0.0, progress_deadline_at - time.time())
            deadline = min(
                workflow_deadline,
                time.monotonic() + float(timeout),
                time.monotonic() + progress_remaining,
            )
            future = _daemon_future(
                lambda n=node, num=number, boundary=progress_deadline_at: self._attempt(
                    run_id, n, num, cancel_event, boundary
                ),
                name=f"graph-node-{node_id}-{number}",
            )
            future.add_done_callback(mark_completed)
            active[future] = _ActiveAttempt(node_id, number, deadline)
            return True

        def timeout_result(entry: _ActiveAttempt) -> _AttemptResult:
            detail = (
                "node exceeded its effective execution deadline (including the "
                "persisted progress budget); result fenced; executor adapter must "
                "terminate its process tree"
            )
            return _AttemptResult(
                entry.number,
                False,
                _failure_digest("PROGRESS_DEADLINE_EXCEEDED", detail),
                f"TimeoutError: {detail}",
                failure={
                    "code": "PROGRESS_DEADLINE_EXCEEDED",
                    "evidence": detail,
                },
            )

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
                    self._persist_join_states(
                        run_id, self.state.node_rows(run_id), lease
                    )
                    return RunResult(run_id, "cancelled", self.state.node_rows(run_id))
                for node_id, node in sorted(self.nodes.items()):
                    if rows[node_id]["status"] != "failed" or not node.get("repair"):
                        continue
                    failure = self.state.last_attempt_failure(run_id, node_id)
                    digest = rows[node_id].get("last_digest")
                    if failure is not None and isinstance(digest, str):
                        self._try_repair(run_id, node, failure, digest, lease)
                rows = self.state.node_rows(run_id)
                self._process_loops(run_id, rows, lease)
                rows = self.state.node_rows(run_id)
                self._process_conditional_routes(run_id, rows, lease)
                rows = self.state.node_rows(run_id)

                # Keep unused quorum members as reserves while the decision is unknown. Once
                # the threshold becomes terminal, claim those members before freezing the
                # snapshot so the audit record distinguishes running work from never-dispatched
                # work, then release the consumer without waiting for them.
                attempts = sum(row["attempt_count"] for row in rows.values())
                slots = budgets["max_concurrency"] - len(active)
                active_nodes = {entry.node_id for entry in active.values()}
                reserves_started = False
                for consumer in self.nodes.values():
                    join = consumer.get("join")
                    if join is None or join["policy"] not in {
                        "any",
                        "n_of_m",
                        "majority",
                    }:
                        continue
                    snapshot = self._join_snapshot(consumer, rows)
                    if snapshot is None or snapshot["decision"] == "waiting":
                        continue
                    for dependency in sorted(consumer["needs"]):
                        if slots <= 0 or attempts >= budgets["max_total_attempts"]:
                            break
                        if (
                            rows[dependency]["status"] != "pending"
                            or dependency in active_nodes
                        ):
                            continue
                        if not launch(dependency, self.nodes[dependency]):
                            continue
                        active_nodes.add(dependency)
                        attempts += 1
                        slots -= 1
                        reserves_started = True
                if reserves_started:
                    rows = self.state.node_rows(run_id)

                join_decisions = self._persist_join_states(run_id, rows, lease)

                for node_id, node in sorted(self.nodes.items()):
                    if rows[node_id]["status"] != "pending":
                        continue
                    if node.get("join") is not None:
                        if join_decisions[node_id] == "failed":
                            snapshot = self.state.join_state(run_id, node_id)
                            self.state.set_node_status(
                                run_id,
                                node_id,
                                "blocked",
                                "join quorum became impossible: "
                                f"passed={snapshot['passed']} missing={snapshot['missing']} "
                                f"threshold={snapshot['threshold']}",
                                lease,
                            )
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
                    elif any(
                        dependency["status"] == "skipped" for dependency in dependencies
                    ):
                        self.state.set_node_status(
                            run_id,
                            node_id,
                            "skipped",
                            "conditional dependency was not selected",
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
                        if not self._quorum_member_dispatchable(
                            node_id, rows, active_nodes, join_decisions
                        ):
                            continue
                        dependency_statuses = [
                            rows[dependency]["status"]
                            for dependency in node.get("needs", [])
                        ]
                        if node.get("join") is not None:
                            if join_decisions.get(node_id) != "succeeded":
                                continue
                        elif not all(
                            status in {"succeeded", "optional_failed"}
                            for status in dependency_statuses
                        ):
                            continue
                        if not launch(node_id, node):
                            continue
                        active_nodes.add(node_id)
                        attempts += 1
                        slots -= 1
                        # A completed worker changes the ready frontier. Recompute before
                        # launching more siblings so fast quorum evidence is not hidden behind
                        # unrelated optional work.
                        if completion.is_set():
                            break

                progressed = False
                with completion_lock:
                    future = completed.popleft() if completed else None
                if future is not None:
                    entry = active.pop(future, None)
                    if entry is not None:
                        try:
                            completed_result = (
                                timeout_result(entry)
                                if time.monotonic() >= entry.deadline
                                else future.result()
                            )
                            self._finalize_attempt(
                                run_id,
                                self.nodes[entry.node_id],
                                completed_result,
                                lease,
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
                    self._finalize_attempt(
                        run_id,
                        self.nodes[entry.node_id],
                        timeout_result(entry),
                        lease,
                    )
                    progressed = True

                # A controller completion can close the apparent DAG while opening a
                # bounded loop-back edge. Checkpoint that decision before terminal-state
                # reduction so a fast final controller cannot bypass its declared loop.
                if progressed:
                    control_rows = self.state.node_rows(run_id)
                    self._process_loops(run_id, control_rows, lease)
                    control_rows = self.state.node_rows(run_id)
                    self._process_conditional_routes(run_id, control_rows, lease)

                if active:
                    if not progressed:
                        next_deadline = min(entry.deadline for entry in active.values())
                        # The caller-owned cancellation Event cannot notify this condition;
                        # retain a small bounded cancellation probe without polling the ready
                        # frontier itself.
                        wake_at = min(
                            next_deadline,
                            workflow_deadline,
                            renew_at,
                            time.monotonic() + 0.05,
                        )
                        completion.wait(max(0.0, wake_at - time.monotonic()))
                        completion.clear()
                    continue
                if progressed:
                    rows = self.state.node_rows(run_id)
                    if not all(row["status"] in self.TERMINAL for row in rows.values()):
                        continue
                else:
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
                        bool(row["required"])
                        and row["status"] not in {"succeeded", "skipped"}
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
