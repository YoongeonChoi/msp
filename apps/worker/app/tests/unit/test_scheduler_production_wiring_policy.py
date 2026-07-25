from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_PRODUCTION_COMPOSITION_PATHS = (
    APP_ROOT / "container.py",
    APP_ROOT / "bootstrap.py",
    APP_ROOT / "main.py",
    APP_ROOT / "tools" / "run_execution_v2_operations.py",
)
OPTIONAL_PRODUCTION_COMPOSITION_PATHS = (
    APP_ROOT / "application" / "services" / "durable_scheduler_loop.py",
)
SCHEDULER_FACTORY_PATH = (
    APP_ROOT / "infrastructure" / "scheduler_runtime_capability_factory.py"
)
SCHEDULER_DEADLINE_PATH = (
    APP_ROOT / "application" / "services" / "scheduler_invocation_deadline.py"
)
DURABLE_SCHEDULER_LOOP_PATH = (
    APP_ROOT / "application" / "services" / "durable_scheduler_loop.py"
)

FORBIDDEN_PRODUCTION_SCHEDULER_SYMBOLS = frozenset(
    {
        "ConvergeDurableSchedulerDefinitions",
        "OperationsLoop",
        "RunDurableSchedulerOnce",
        "RunOperationsV2",
        "SCHEDULER_RESULT_VALIDATORS",
        "SchedulerJobBinding",
        "create_scheduler_runtime_capability",
        "create_supabase_scheduler_job_bindings",
    }
)
FORBIDDEN_PUBLIC_AUTHORITY_FACTORIES = frozenset(
    {
        "create_scheduler_runtime_capability",
        "create_supabase_scheduler_job_bindings",
    }
)
PRIVATE_AUTHORITY_SYMBOLS = frozenset(
    {
        "_create_scheduler_runtime_capability",
        "_create_supabase_scheduler_binding_registry",
        "_assert_dispatch_registry_provenance",
        "_authorized_registry",
        "_effect_authorized",
        "_dispatch_registry_provenance",
        "_issue_dispatch_registry_seal",
        "_issue_effect_authorized_scheduler_registry",
        "_issue_scheduler_dispatch_registry_seal",
    }
)
PRIVATE_AUTHORITY_ALLOWED_PATHS = frozenset({SCHEDULER_FACTORY_PATH})
PRIVATE_AUTHORITY_ADDITIONAL_ALLOWED_PATHS = {
    "_assert_dispatch_registry_provenance": frozenset({SCHEDULER_DEADLINE_PATH}),
}
FACADE_LIFECYCLE_ALLOWED_PATHS = frozenset(
    {SCHEDULER_FACTORY_PATH, DURABLE_SCHEDULER_LOOP_PATH}
)
DIRECT_STAGE_ACTIONS = frozenset({"dispatch_once", "run_once"})
STAGE_NAME_MARKERS = ("command", "execution", "settlement", "reconcil", "outbox")


def test_production_composition_uses_only_sealed_scheduler_boundary() -> None:
    missing_paths = [path for path in REQUIRED_PRODUCTION_COMPOSITION_PATHS if not path.is_file()]
    assert not missing_paths, _format_missing_paths(missing_paths)

    paths = (*REQUIRED_PRODUCTION_COMPOSITION_PATHS, *_existing_optional_paths())
    violations: list[str] = []
    for path in paths:
        tree = _parse(path)
        violations.extend(_forbidden_symbol_violations(path, tree))
        violations.extend(_direct_stage_invocation_violations(path, tree))

    assert not violations, "\n".join(
        [
            "production scheduler composition must use only the sealed runner/loop facade:",
            *sorted(violations),
        ]
    )


def test_scheduler_factory_does_not_export_raw_binding_registry() -> None:
    assert SCHEDULER_FACTORY_PATH.is_file(), _format_missing_paths([SCHEDULER_FACTORY_PATH])
    tree = _parse(SCHEDULER_FACTORY_PATH)
    public_bindings = _top_level_bindings(tree)

    leaked = public_bindings & FORBIDDEN_PUBLIC_AUTHORITY_FACTORIES
    assert not leaked, (
        "scheduler_runtime_capability_factory.py must keep raw runtime/binding authority "
        f"behind its opaque facade; leaked bindings: {sorted(leaked)}"
    )


def test_private_effect_authority_hooks_are_factory_only() -> None:
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = _parse(path)
        violations.extend(_private_authority_reference_violations(path, tree))
    assert not violations, "\n".join(
        ["private scheduler authority hooks are factory-only:", *sorted(violations)]
    )


def test_private_authority_policy_rejects_alias_and_dynamic_access() -> None:
    probe_path = APP_ROOT / "application" / "authority_policy_probe.py"
    snippets = (
        "from app.x import _issue_scheduler_dispatch_registry_seal as make\nmake()",
        "build = RunDurableSchedulerOnce._effect_authorized\nbuild()",
        "build = getattr(runner, '_effect_authorized')\nbuild()",
        "build = vars(runner)['_effect_authorized']\nbuild()",
        "build = runner.__dict__['_effect_authorized']\nbuild()",
    )

    for source in snippets:
        violations = _private_authority_reference_violations(
            probe_path,
            ast.parse(source),
        )
        assert violations, source


def test_scheduler_facade_lifecycle_is_loop_only() -> None:
    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        violations.extend(_facade_lifecycle_violations(path, _parse(path)))
    assert not violations, "\n".join(
        ["scheduler facade lifecycle is durable-loop-only:", *sorted(violations)]
    )


def test_scheduler_facade_policy_rejects_direct_and_dynamic_access() -> None:
    probe_path = APP_ROOT / "application" / "facade_policy_probe.py"
    snippets = (
        "await facade.converge_step()",
        "await facade.run_once()",
        "run = facade.run_once\nawait run()",
        "run = getattr(facade, 'run_once')\nawait run()",
        "run = vars(facade)['run_once']\nawait run()",
        "run = facade.__dict__['run_once']\nawait run()",
        "await runtime.scheduler_loop._runtime.run_once()",
    )

    for source in snippets:
        violations = _facade_lifecycle_violations(
            probe_path,
            ast.parse(source),
        )
        assert violations, source


def test_stage_invocation_policy_distinguishes_aggregate_and_per_stage_calls() -> None:
    assert not _is_direct_stage_invocation(("runtime", "scheduler_loop", "run_once"))
    assert not _is_direct_stage_invocation(("self", "sealed_runner", "run_once"))

    for stage_name in ("commands", "execution", "settlement", "reconciliation", "outbox"):
        assert _is_direct_stage_invocation(("run_operations", stage_name, "run_once"))
        assert _is_direct_stage_invocation((stage_name, "dispatch_once"))

    assert _is_direct_stage_invocation(("run_execution_once",))
    assert _is_direct_stage_invocation(("dispatch_alert_outbox_once",))


def _existing_optional_paths() -> tuple[Path, ...]:
    return tuple(path for path in OPTIONAL_PRODUCTION_COMPOSITION_PATHS if path.is_file())


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _forbidden_symbol_violations(path: Path, tree: ast.Module) -> list[str]:
    violations: set[tuple[int, int, str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names = {alias.name.rpartition(".")[2]}
                if alias.asname is not None:
                    imported_names.add(alias.asname)
                for imported_name in imported_names & FORBIDDEN_PRODUCTION_SCHEDULER_SYMBOLS:
                    violations.add((node.lineno, node.col_offset, "import", imported_name))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in FORBIDDEN_PRODUCTION_SCHEDULER_SYMBOLS:
                    violations.add((node.lineno, node.col_offset, "import", alias.name))
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_PRODUCTION_SCHEDULER_SYMBOLS:
                violations.add((node.lineno, node.col_offset, "identifier", node.id))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in FORBIDDEN_PRODUCTION_SCHEDULER_SYMBOLS
        ):
            violations.add((node.lineno, node.col_offset, "attribute", node.attr))

    return [
        _format_violation(path, line, column, f"forbidden {kind} {symbol}")
        for line, column, kind, symbol in sorted(violations)
    ]


def _private_authority_reference_violations(
    path: Path,
    tree: ast.Module,
) -> list[str]:
    violations: set[tuple[int, int, str]] = set()
    for node in ast.walk(tree):
        references: tuple[str, ...] = ()
        if isinstance(node, ast.ImportFrom):
            references = tuple(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            references = (node.id,)
        elif isinstance(node, ast.Attribute):
            references = (node.attr,)
        elif isinstance(node, ast.Constant) and type(node.value) is str:
            references = (node.value,)

        for symbol in PRIVATE_AUTHORITY_SYMBOLS.intersection(references):
            allowed_paths = PRIVATE_AUTHORITY_ALLOWED_PATHS | (
                PRIVATE_AUTHORITY_ADDITIONAL_ALLOWED_PATHS.get(symbol, frozenset())
            )
            if path in allowed_paths:
                continue
            violations.add(
                (
                    getattr(node, "lineno", 0),
                    getattr(node, "col_offset", 0),
                    symbol,
                )
            )

    return [
        _format_violation(path, line, column, f"private authority reference {symbol}")
        for line, column, symbol in sorted(violations)
    ]


def _facade_lifecycle_violations(path: Path, tree: ast.Module) -> list[str]:
    if path in FACADE_LIFECYCLE_ALLOWED_PATHS:
        return []
    violations: set[tuple[int, int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            chain = _callable_chain(node)
            if _is_direct_facade_lifecycle_reference(chain):
                violations.add((node.lineno, node.col_offset, ".".join(chain)))
            elif node.attr == "_runtime":
                owner = _callable_chain(node.value)
                if any("schedulerloop" in _normalize_identifier(part) for part in owner):
                    violations.add((node.lineno, node.col_offset, ".".join(chain)))
        elif (
            isinstance(node, ast.Call)
            and _is_dynamic_facade_lifecycle_access(node)
        ) or (
            isinstance(node, ast.Subscript)
            and _is_dynamic_facade_lifecycle_subscript(node)
        ):
            violations.add((node.lineno, node.col_offset, "dynamic facade lifecycle"))
    return [
        _format_violation(path, line, column, f"facade lifecycle reference {reference}")
        for line, column, reference in sorted(violations)
    ]


def _is_direct_facade_lifecycle_reference(call_chain: tuple[str, ...]) -> bool:
    if len(call_chain) < 2 or call_chain[-1] not in {"converge_step", "run_once"}:
        return False
    receiver = tuple(_normalize_identifier(part) for part in call_chain[:-1])
    if call_chain[-1] == "converge_step":
        return True
    if receiver[-1].endswith("schedulerloop"):
        return False
    return any("facade" in part or "schedulerruntime" in part for part in receiver) or (
        any("schedulerloop" in part for part in receiver)
        and receiver[-1] in {"runtime", "_runtime"}
    )


def _is_dynamic_facade_lifecycle_access(node: ast.Call) -> bool:
    call_chain = _callable_chain(node.func)
    if not call_chain or call_chain[-1] != "getattr" or len(node.args) < 2:
        return False
    attribute = node.args[1]
    if (
        not isinstance(attribute, ast.Constant)
        or attribute.value not in {"converge_step", "run_once"}
    ):
        return False
    receiver = _callable_chain(node.args[0])
    return _looks_like_facade_receiver(receiver)


def _is_dynamic_facade_lifecycle_subscript(node: ast.Subscript) -> bool:
    if (
        not isinstance(node.slice, ast.Constant)
        or node.slice.value not in {"converge_step", "run_once"}
    ):
        return False

    receiver: ast.expr | None = None
    if isinstance(node.value, ast.Call):
        accessor = _callable_chain(node.value.func)
        if accessor and accessor[-1] == "vars" and node.value.args:
            receiver = node.value.args[0]
    elif isinstance(node.value, ast.Attribute) and node.value.attr == "__dict__":
        receiver = node.value.value

    return receiver is not None and _looks_like_facade_receiver(
        _callable_chain(receiver)
    )


def _looks_like_facade_receiver(receiver: tuple[str, ...]) -> bool:
    return any(
        "facade" in _normalize_identifier(part)
        or "schedulerruntime" in _normalize_identifier(part)
        for part in receiver
    )


def _direct_stage_invocation_violations(path: Path, tree: ast.Module) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_chain = _callable_chain(node.func)
        if not call_chain or not _is_direct_stage_invocation(call_chain):
            continue
        violations.append(
            _format_violation(
                path,
                node.lineno,
                node.col_offset,
                f"direct stage invocation {'.'.join(call_chain)}",
            )
        )
    return violations


def _callable_chain(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if not isinstance(node, ast.Attribute):
        return ()
    prefix = _callable_chain(node.value)
    return (*prefix, node.attr) if prefix else (node.attr,)


def _is_direct_stage_invocation(call_chain: tuple[str, ...]) -> bool:
    callable_name = call_chain[-1]
    if callable_name in DIRECT_STAGE_ACTIONS:
        return any(_contains_stage_marker(part) for part in call_chain[:-1])

    normalized_callable = _normalize_identifier(callable_name)
    invokes_once = normalized_callable.endswith("once") and (
        normalized_callable.startswith("run") or normalized_callable.startswith("dispatch")
    )
    return invokes_once and _contains_stage_marker(normalized_callable)


def _contains_stage_marker(identifier: str) -> bool:
    normalized = _normalize_identifier(identifier)
    return any(marker in normalized for marker in STAGE_NAME_MARKERS)


def _normalize_identifier(identifier: str) -> str:
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", identifier)
    return "".join(word.casefold() for word in words)


def _top_level_bindings(tree: ast.Module) -> set[str]:
    bindings: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: Iterable[ast.expr]
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                bindings.update(_bound_names(target))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bindings.add(alias.asname or alias.name.partition(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bindings.add(alias.asname or alias.name)
    return bindings


def _bound_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {name for item in target.elts for name in _bound_names(item)}
    return set()


def _format_violation(
    path: Path,
    line: int,
    column: int,
    description: str,
) -> str:
    relative_path = path.relative_to(APP_ROOT.parent)
    return f"{relative_path}:{line}:{column + 1}: {description}"


def _format_missing_paths(paths: Iterable[Path]) -> str:
    relative_paths = [str(path.relative_to(APP_ROOT.parent)) for path in paths]
    return f"missing production composition path(s): {', '.join(relative_paths)}"
