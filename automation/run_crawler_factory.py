from __future__ import annotations

import argparse
import ast
import fcntl
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from observability import configure_logging

from automation.codex_auth import codex_auth_reason
from automation.crawler_registry import CrawlerRegistry
from build_crawlers_with_codex import MODEL, country_code_for_url, crawler_folder_name


DEFAULT_LOCK_PATH = Path("/var/lib/crawler-factory/factory.lock")
DEFAULT_RUNS_DIR = Path("/var/lib/crawler-factory/runs")
DEFAULT_VALIDATION_TIMEOUT_MINUTES = 15
METADATA_PATTERN = re.compile(
    r"<!-- crawler-factory-metadata\s*(\{.*?\})\s*-->",
    re.DOTALL,
)
ALLOWED_REASON_CODES = {
    "no_current_events",
    "access_blocked",
    "no_parseable_source",
    "implementation_failed",
}
logger = logging.getLogger(__name__)
ALLOWED_CHILD_ENV = {
    "CODEX_BIN",
    "CODEX_HOME",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "OPENAI_API_KEY",
    "PATH",
    "PLAYWRIGHT_MCP_EXECUTABLE_PATH",
    "PLAYWRIGHT_MCP_HEADLESS",
    "PLAYWRIGHT_MCP_NO_SANDBOX",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TZ",
}


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=check,
        text=True,
        capture_output=True,
    )


def write_supervisor_result(
    path: Path | None,
    *,
    run_id: str,
    claimed_count: int,
    results: list[dict],
    status: str,
    pull_request_url: str | None = None,
    base_commit_sha: str | None = None,
    auth_reason_code: str | None = None,
    auth_context: dict | None = None,
) -> None:
    if path is None:
        return
    status_counts = {
        outcome: sum(result.get("status") == outcome for result in results)
        for outcome in {str(result.get("status")) for result in results}
    }
    payload = {
        "run_id": run_id,
        "claimed_count": claimed_count,
        "attempted_count": len(results),
        "status_counts": status_counts,
        "status": status,
        "pull_request_url": pull_request_url,
        "base_commit_sha": base_commit_sha,
        "auth_reason_code": auth_reason_code,
        "auth_context": auth_context,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def crawler_directory(url: str, country_code: str | None = None) -> Path:
    country = country_code or country_code_for_url(url)
    return Path("crawlers") / country.lower() / crawler_folder_name(url)


def pending_crawler_directory(url: str) -> Path:
    return Path("crawlers") / "_pending" / crawler_folder_name(url)


def resolved_crawler_directory(
    slug: str,
    geographic_scope: str,
    country_code: str | None,
) -> Path:
    parent = "common" if geographic_scope == "multi_country" else str(country_code).lower()
    return Path("crawlers") / parent / slug


def source_directory(source: dict) -> Path:
    if not source.get("crawler_path"):
        raise ValueError(f"Crawler source {source['id']} has no assigned crawler_path")
    path = Path(source["crawler_path"])
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Crawler source {source['id']} has an unsafe crawler_path")
    return path


def parse_blocked_metadata(path: Path) -> dict:
    match = METADATA_PATTERN.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError("BLOCKED.md has no crawler-factory metadata block")
    metadata = json.loads(match.group(1))
    required = {"url", "country_code", "reason_code", "attempted_at", "retry_after"}
    if not required.issubset(metadata):
        raise ValueError("BLOCKED.md metadata is incomplete")
    return metadata


def generated_geography(crawler_path: Path) -> tuple[str, str | None]:
    main_path = crawler_path / "main.py"
    if main_path.exists():
        tree = ast.parse(main_path.read_text(encoding="utf-8"), str(main_path))
        values = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if name != "CrawlerConfig":
                continue
            for keyword in node.keywords:
                if keyword.arg == "country_code":
                    values.append(ast.literal_eval(keyword.value))
        if len(values) != 1:
            raise ValueError("generated crawler must declare country_code exactly once")
        country_code = values[0]
        if country_code is None:
            return "multi_country", None
        if not isinstance(country_code, str) or not re.fullmatch(r"[A-Za-z]{2}", country_code):
            raise ValueError("generated crawler country_code must be None or an ISO alpha-2 code")
        return "country", country_code.upper()

    metadata = parse_blocked_metadata(crawler_path / "BLOCKED.md")
    scope = metadata.get("geographic_scope")
    country_code = metadata.get("country_code")
    if scope == "multi_country" and country_code is None:
        return scope, None
    if scope == "country" and isinstance(country_code, str) and re.fullmatch(
        r"[A-Za-z]{2}", country_code
    ):
        return scope, country_code.upper()
    raise ValueError("blocked crawler must declare a resolved geographic scope")


def relocate_generated_directory(
    workspace: Path,
    current: Path,
    final: Path,
) -> None:
    if current == final:
        return
    target = workspace / final
    if target.exists():
        raise FileExistsError(f"resolved crawler directory already exists: {final}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(workspace / current), str(target))


def resolve_source_url(url: str, timeout_seconds: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalCrawlerFactory/1.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.geturl()
    except Exception:
        return url


def sanitized_child_env(run_dir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in ALLOWED_CHILD_ENV}
    child_home = Path(env.get("CODEX_HOME") or run_dir / "codex-home")
    child_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(child_home)
    env["CODEX_HOME"] = str(child_home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def changed_paths(workspace: Path) -> list[str]:
    return sorted(
        {
            path
            for entry in changed_entries(workspace)
            for path in (entry["path"], entry.get("original_path"))
            if path
        }
    )


def changed_entries(workspace: Path) -> list[dict[str, str | None]]:
    result = run_command(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=workspace,
    )
    entries_out = []
    entries = result.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        original_path = None
        if status[0] in {"R", "C"} and index < len(entries):
            original_path = entries[index]
            index += 1
        entries_out.append(
            {"status": status, "path": path, "original_path": original_path}
        )
    return entries_out


def allowed_scope_paths(
    expected_directory: Path,
    previous_directory: Path | None = None,
) -> set[str]:
    allowed = {
        str(expected_directory / "main.py"),
        str(expected_directory / "BLOCKED.md"),
    }
    if previous_directory and previous_directory != expected_directory:
        allowed.update(
            {
                str(previous_directory / "main.py"),
                str(previous_directory / "BLOCKED.md"),
            }
        )
    return allowed


def cleanup_untracked_scope_artifacts(
    workspace: Path,
    expected_directory: Path,
    report_path: Path,
    previous_directory: Path | None = None,
) -> list[str]:
    """Remove unexpected files only when every violation is untracked."""

    allowed = allowed_scope_paths(expected_directory, previous_directory)
    before = changed_entries(workspace)
    unexpected = [
        entry
        for entry in before
        if entry["path"] not in allowed
        or (entry.get("original_path") and entry["original_path"] not in allowed)
    ]
    removed: list[str] = []
    if unexpected and all(entry["status"] == "??" for entry in unexpected):
        removed = sorted({str(entry["path"]) for entry in unexpected})
        run_command(["git", "clean", "-fd", "--", *removed], cwd=workspace)
    report = {"before": before, "removed_untracked_paths": removed}
    report["after"] = changed_entries(workspace)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return removed


def validate_change_scope(
    workspace: Path,
    expected_directory: Path,
    previous_directory: Path | None = None,
) -> None:
    allowed = allowed_scope_paths(expected_directory, previous_directory)
    paths = set(changed_paths(workspace))
    unexpected = sorted(paths - allowed)
    if not paths:
        raise RuntimeError("Codex produced no repository changes")
    if unexpected:
        raise RuntimeError(f"Codex changed files outside the allowed scope: {unexpected}")
    main_path = workspace / expected_directory / "main.py"
    blocked_path = workspace / expected_directory / "BLOCKED.md"
    if main_path.exists() == blocked_path.exists():
        raise RuntimeError("Codex must produce exactly one of main.py or BLOCKED.md")


def normalize_blocked_metadata(
    workspace: Path,
    url: str,
    geographic_scope: str,
    country_code: str | None,
    crawler_path: Path,
    attempted: date,
) -> None:
    path = workspace / crawler_path / "BLOCKED.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    match = METADATA_PATTERN.search(text)
    existing = {}
    if match:
        try:
            existing = json.loads(match.group(1))
        except json.JSONDecodeError:
            existing = {}
    reason_code = existing.get("reason_code", "implementation_failed")
    if reason_code not in ALLOWED_REASON_CODES:
        reason_code = "implementation_failed"
    metadata = {
        "url": url,
        "geographic_scope": geographic_scope,
        "country_code": country_code.upper() if country_code else None,
        "reason_code": reason_code,
        "attempted_at": attempted.isoformat(),
        "retry_after": (attempted + timedelta(days=30)).isoformat(),
    }
    block = (
        "<!-- crawler-factory-metadata\n"
        f"{json.dumps(metadata, separators=(',', ':'))}\n"
        "-->"
    )
    if match:
        text = f"{text[:match.start()]}{block}{text[match.end():]}"
    else:
        text = f"{block}\n\n{text}"
    path.write_text(text, encoding="utf-8")


def git_commit(workspace: Path, source: dict, status: str) -> str:
    directory = source_directory(source)
    run_command(["git", "add", "--", str(directory)], cwd=workspace)
    slug = directory.name
    subject = (
        f"Add crawler for {slug}"
        if status == "generated"
        else f"Record blocked crawler for {slug}"
    )
    run_command(["git", "commit", "-m", subject], cwd=workspace)
    return run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()


def validate_generated_crawler(
    workspace: Path,
    run_dir: Path,
    source: dict,
    crawler_path: Path,
    child_env: dict[str, str],
    timeout_minutes: int,
) -> dict:
    slug = crawler_path.name
    report_path = run_dir / f"{source['id']}-{slug}-validation.json"
    command = [
        sys.executable,
        "-m",
        "automation.validate_generated_crawler",
        "--crawler-directory",
        str(crawler_path),
    ]
    try:
        completed = run_command(
            command,
            cwd=workspace,
            env=child_env,
            timeout=timeout_minutes * 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        report = {
            "status": "inconclusive_runtime",
            "record_count": None,
            "issue_count": None,
            "issues": [],
            "error": f"full scrape exceeded {timeout_minutes} minutes",
            "duration_seconds": timeout_minutes * 60,
        }
    else:
        try:
            report = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            report = {
                "status": "execution_error",
                "record_count": None,
                "issue_count": None,
                "issues": [],
                "error": "validator did not return valid JSON",
                "duration_seconds": None,
            }
        if not isinstance(report, dict):
            report = {
                "status": "execution_error",
                "record_count": None,
                "issue_count": None,
                "issues": [],
                "error": "validator JSON must be an object",
                "duration_seconds": None,
            }
        if completed.returncode != 0 and report.get("status") == "passed":
            report["status"] = "execution_error"
            report["error"] = "validator exited unsuccessfully after reporting success"
        if completed.stderr.strip():
            report["validator_stderr"] = completed.stderr.strip()[:1000]
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def validation_error(report: dict) -> str:
    status = report.get("status", "execution_error")
    if status == "data_quality_failure":
        count = report.get("issue_count")
        examples = report.get("issues") or []
        detail = "; ".join(
            f"record {issue.get('record')} {issue.get('field')}: {issue.get('reason')}"
            for issue in examples[:3]
        )
        return f"live validation found {count} data-quality issue(s): {detail}"
    return f"live validation {status}: {report.get('error') or 'unknown error'}"


def persist_failure_diagnostics(
    workspace: Path,
    run_dir: Path,
    source: dict,
    crawler_directory: Path,
) -> Path:
    diagnostic_dir = run_dir / f"{source['id']}-{crawler_directory.name}-failure"
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    (diagnostic_dir / "git-status.json").write_text(
        json.dumps(changed_entries(workspace), indent=2) + "\n",
        encoding="utf-8",
    )
    unstaged = run_command(
        ["git", "diff", "--no-ext-diff", "--binary"], cwd=workspace, check=False
    ).stdout
    staged = run_command(
        ["git", "diff", "--cached", "--no-ext-diff", "--binary"],
        cwd=workspace,
        check=False,
    ).stdout
    (diagnostic_dir / "tracked.patch").write_text(
        unstaged + staged,
        encoding="utf-8",
    )
    generated = workspace / crawler_directory
    if generated.exists():
        shutil.copytree(
            generated,
            diagnostic_dir / "generated-crawler",
            dirs_exist_ok=True,
            symlinks=True,
        )
    return diagnostic_dir


def read_builder_report(builder_report: Path, item_summary_path: Path, result: dict) -> None:
    if not builder_report.exists():
        return
    try:
        builder_data = json.loads(builder_report.read_text(encoding="utf-8"))
        if not builder_data:
            return
        latest = builder_data[-1]
        result["final_response"] = latest.get("final_response")
        result["builder_status"] = latest.get("status")
        result["auth_reason_code"] = latest.get("auth_reason_code")
        if latest.get("error"):
            result["builder_error"] = str(latest["error"])
        summaries = latest.get("item_summaries") or []
        item_summary_path.write_text(
            json.dumps(summaries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result["builder_item_summary_path"] = str(item_summary_path)
    except (json.JSONDecodeError, OSError, TypeError, AttributeError) as exc:
        warning = f"builder report could not be read: {type(exc).__name__}"
        result["generation_warning"] = (
            f"{result['generation_warning']}; {warning}"
            if result["generation_warning"]
            else warning
        )


def attempt_source(
    workspace: Path,
    run_dir: Path,
    source: dict,
    timeout_minutes: int,
    child_env: dict[str, str],
    model: str = MODEL,
    validation_timeout_minutes: int = DEFAULT_VALIDATION_TIMEOUT_MINUTES,
    identity_resolver: Callable[[dict, str, str | None, str], dict] | None = None,
    *,
    run_id: str | None = None,
    attempt_id: int | None = None,
) -> dict:
    started = time.monotonic()
    url = source["canonical_url"]
    directory = source_directory(source)
    slug = directory.name
    builder_report = run_dir / f"{source['id']}-{slug}-builder.json"
    builder_items = run_dir / f"{source['id']}-{slug}-builder-items.json"
    scope_report = run_dir / f"{source['id']}-{slug}-scope.json"
    stage = "builder"
    context = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "source_id": source["id"],
        "url": url,
        "crawler_directory": str(directory),
    }
    result = {
        "source_id": source["id"],
        "url": url,
        "resolved_url": url,
        "crawler_directory": str(directory),
        "geographic_scope": source.get("geographic_scope", "unknown"),
        "country_code": source.get("country_code"),
        "status": "generation_failed",
        "commit": None,
        "generation_warning": None,
        "validation_status": None,
        "validation_record_count": None,
        "validation_duration_seconds": None,
        "final_response": None,
        "error": None,
        "failure_stage": None,
        "scope_report_path": str(scope_report),
        "diagnostic_path": None,
        "builder_item_summary_path": None,
        "builder_status": None,
        "builder_error": None,
        "auth_reason_code": None,
    }
    logger.info(
        "Starting crawler source attempt",
        extra={"event": "factory_source_started", **context},
    )
    command = [
        sys.executable,
        str(workspace / "build_crawlers_with_codex.py"),
        "--workspace",
        str(workspace),
        "--model",
        model,
        "--url",
        url,
        "--crawler-directory",
        str(directory),
        "--retry-blocked",
        "--sandbox",
        "full-access",
        "--results",
        str(builder_report),
    ]
    if source.get("country_code"):
        command[command.index("--crawler-directory"):command.index("--crawler-directory")] = [
            "--country-code",
            source["country_code"],
        ]
    try:
        try:
            generation = run_command(
                command,
                cwd=workspace,
                env=child_env,
                timeout=timeout_minutes * 60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output_parts = []
            for part in (exc.stdout, exc.stderr):
                if isinstance(part, bytes):
                    part = part.decode(errors="replace")
                if part:
                    output_parts.append(part)
            builder_output = "".join(output_parts)
            (run_dir / f"{source['id']}-{slug}-builder.log").write_text(
                builder_output, encoding="utf-8"
            )
            unit = "minute" if timeout_minutes == 1 else "minutes"
            result["generation_warning"] = f"builder exceeded {timeout_minutes} {unit}"
            builder_exit_code = None
        else:
            builder_output = generation.stdout + generation.stderr
            (run_dir / f"{source['id']}-{slug}-builder.log").write_text(
                builder_output,
                encoding="utf-8",
            )
            if generation.returncode != 0:
                result["generation_warning"] = (
                    f"builder exited with status {generation.returncode}"
                )
            builder_exit_code = generation.returncode
        read_builder_report(builder_report, builder_items, result)
        auth_reason = (
            result.get("auth_reason_code")
            or codex_auth_reason(result.get("builder_error"))
            or codex_auth_reason(builder_output)
        )
        if result.get("builder_status") == "auth_required" or auth_reason:
            result["status"] = "auth_required"
            result["auth_reason_code"] = auth_reason or "login_required"
            result["error"] = "Codex authentication is required"
            return result
        logger.info(
            "Crawler builder completed",
            extra={
                "event": "factory_source_builder_completed",
                **context,
                "builder_exit_code": builder_exit_code,
                "warning": result["generation_warning"],
            },
        )
        initial_directory = directory
        stage = "scope_validation"
        removed_paths = cleanup_untracked_scope_artifacts(
            workspace,
            initial_directory,
            scope_report,
        )
        if removed_paths:
            logger.warning(
                "Removed untracked files outside crawler scope",
                extra={
                    "event": "factory_source_scope_cleanup",
                    **context,
                    "removed_paths": removed_paths,
                    "scope_report_path": str(scope_report),
                },
            )
        validate_change_scope(workspace, initial_directory)
        stage = "geography_resolution"
        geographic_scope, country_code = generated_geography(workspace / directory)
        final_directory = resolved_crawler_directory(
            directory.name,
            geographic_scope,
            country_code,
        )
        relocate_generated_directory(workspace, directory, final_directory)
        directory = final_directory
        result["crawler_directory"] = str(directory)
        result["geographic_scope"] = geographic_scope
        result["country_code"] = country_code
        context["crawler_directory"] = str(directory)
        stage = "scope_validation"
        validate_change_scope(workspace, directory, initial_directory)
        if identity_resolver:
            source = identity_resolver(
                source,
                geographic_scope,
                country_code,
                str(directory),
            )
            if source.get("status") == "duplicate":
                result["status"] = "duplicate"
                return result
        normalize_blocked_metadata(
            workspace,
            url,
            geographic_scope,
            country_code,
            directory,
            datetime.now(UTC).date(),
        )
        output_path = workspace / directory
        blocked = (output_path / "BLOCKED.md").exists()
        if blocked:
            result["status"] = "blocked"
            result["validation_status"] = "not_applicable"
        else:
            stage = "live_validation"
            validation = validate_generated_crawler(
                workspace,
                run_dir,
                source,
                directory,
                child_env,
                validation_timeout_minutes,
            )
            result["validation_status"] = validation.get("status")
            result["validation_record_count"] = validation.get("record_count")
            result["validation_duration_seconds"] = validation.get("duration_seconds")
            logger.info(
                "Crawler source validation completed",
                extra={
                    "event": "factory_source_validation_completed",
                    **context,
                    "validation_status": result["validation_status"],
                    "record_count": result["validation_record_count"],
                    "duration_seconds": result["validation_duration_seconds"],
                },
            )
            if validation.get("status") != "passed":
                result["error"] = validation_error(validation)
                return result
            result["status"] = "generated"
        stage = "commit"
        result["commit"] = git_commit(workspace, source, result["status"])
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        if result["status"] not in {"generated", "blocked", "duplicate"}:
            result["failure_stage"] = stage
            try:
                result["diagnostic_path"] = str(
                    persist_failure_diagnostics(workspace, run_dir, source, directory)
                )
            except Exception as exc:
                logger.warning(
                    "Could not preserve crawler failure diagnostics",
                    extra={
                        "event": "factory_source_diagnostics_failed",
                        **context,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:1000],
                    },
                )
            logger.error(
                "Crawler source attempt failed",
                extra={
                    "event": "factory_source_failed",
                    **context,
                    "status": result["status"],
                    "stage": stage,
                    "duration_seconds": result["duration_seconds"],
                    "warning": result["generation_warning"],
                    "error_message": (result["error"] or "unknown error")[:2000],
                    "diagnostic_path": result["diagnostic_path"],
                },
            )
        else:
            logger.info(
                "Crawler source attempt completed",
                extra={
                    "event": "factory_source_completed",
                    **context,
                    "status": result["status"],
                    "duration_seconds": result["duration_seconds"],
                    "warning": result["generation_warning"],
                },
            )


def reset_failed_attempt(workspace: Path) -> None:
    paths = changed_paths(workspace)
    if paths:
        run_command(["git", "clean", "-fd"], cwd=workspace)
        run_command(["git", "restore", "--worktree", "--staged", "."], cwd=workspace)


def reconcile_pull_requests(registry: CrawlerRegistry, workspace: Path) -> dict[str, int]:
    grouped: dict[str, list[dict]] = {}
    for source in registry.pr_open_sources():
        grouped.setdefault(source["pull_request_url"], []).append(source)
    counts = {"retry_wait": 0, "needs_attention": 0}
    terminal_failures = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}
    for pr_url, sources in grouped.items():
        try:
            result = run_command(
                [
                    "gh",
                    "pr",
                    "view",
                    pr_url,
                    "--json",
                    "state,mergedAt,statusCheckRollup",
                ],
                cwd=workspace,
                timeout=30,
            )
            data = json.loads(result.stdout)
        except Exception as exc:
            logger.warning(
                "Could not reconcile pull request",
                extra={
                    "event": "factory_pr_reconcile_failed",
                    "pull_request_url": pr_url,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            continue
        ids = [source["id"] for source in sources]
        if data.get("state") == "CLOSED" and not data.get("mergedAt"):
            registry.transition_sources(
                ids,
                "retry_wait",
                retry_after=datetime.now(UTC) + timedelta(days=7),
            )
            counts["retry_wait"] += len(ids)
            continue
        conclusions = {
            str(check.get("conclusion") or check.get("state") or "").upper()
            for check in data.get("statusCheckRollup") or []
        }
        if conclusions & terminal_failures:
            registry.transition_sources(ids, "needs_attention")
            counts["needs_attention"] += len(ids)
    return counts


def pr_body(results: list[dict], model: str, run_id: str) -> str:
    rows = [
        "| Source | URL | Result | Validation | Warning | Failure | Directory |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in results:
        validation = result.get("validation_status") or "not run"
        if validation == "passed":
            validation = (
                f"passed ({result.get('validation_record_count')} records, "
                f"{result.get('validation_duration_seconds')}s)"
            )
        warning = str(result.get("generation_warning") or "none").replace("|", "\\|").replace("\n", " ")[:500]
        failure = str(result.get("error") or "none").replace("|", "\\|").replace("\n", " ")[:500]
        rows.append(
            f"| {result['source_id']} | {result['url']} | {result['status']} | {validation} | "
            f"{warning} | {failure} | "
            f"`{result['crawler_directory']}` |"
        )
    sections = [
        "Automated crawler-factory batch.",
        "",
        *rows,
        "",
        f"- Run ID: `{run_id}`",
        f"- Model: `{model}`",
        f"- Total attempts: {len(results)}",
        "",
        "Detailed sanitized logs and JSON reports are retained by the worker.",
    ]
    for result in results:
        if result.get("final_response"):
            sections.extend(
                [
                    "",
                    f"<details><summary>Codex report for {result['url']}</summary>",
                    "",
                    result["final_response"],
                    "",
                    "</details>",
                ]
            )
    return "\n".join(sections)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and publish a database-backed crawler batch.")
    parser.add_argument(
        "--repository",
        default=os.getenv("CRAWLER_FACTORY_REPOSITORY"),
        help=(
            "Git clone URL. Defaults to the CRAWLER_FACTORY_REPOSITORY "
            "environment variable."
        ),
    )
    parser.add_argument("--base-branch", default="master")
    parser.add_argument("--max-urls", type=int, default=5)
    parser.add_argument(
        "--model",
        default=MODEL,
        help=f"Codex model for this batch (default: {MODEL}).",
    )
    parser.add_argument("--timeout-minutes", type=int, default=60)
    parser.add_argument(
        "--validation-timeout-minutes",
        type=int,
        default=int(
            os.getenv(
                "CRAWLER_FACTORY_VALIDATION_TIMEOUT_MINUTES",
                str(DEFAULT_VALIDATION_TIMEOUT_MINUTES),
            )
        ),
        help="Maximum minutes for the authoritative full scrape (default: 15).",
    )
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument(
        "--result-path",
        type=Path,
        help="Write an atomic machine-readable batch summary for the supervisor.",
    )
    parser.add_argument("--url", action="append", dest="urls")
    parser.add_argument("--country-code")
    parser.add_argument("--source-id", action="append", type=int, dest="source_ids")
    parser.add_argument("--no-push", action="store_true", help="Generate commits but do not push or open a PR")
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()
    if not args.repository:
        parser.error(
            "--repository is required when CRAWLER_FACTORY_REPOSITORY is not set"
        )
    return args


def run_factory(args: argparse.Namespace, registry: CrawlerRegistry) -> None:
    if args.max_urls < 1:
        raise SystemExit("--max-urls must be at least 1")
    if args.validation_timeout_minutes < 1:
        raise SystemExit("--validation-timeout-minutes must be at least 1")
    if args.urls and args.source_ids:
        raise SystemExit("--url and --source-id are mutually exclusive")
    run_id = f"{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:8]}"
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{run_id}"
    run_dir = args.runs_dir / run_id
    run_dir.mkdir(parents=True)
    workspace = Path(tempfile.mkdtemp(prefix="crawler-factory-"))
    results: list[dict] = []
    claimed_count = 0
    successful_source_ids: list[int] = []
    base_commit_sha: str | None = None
    run_created = False
    supervisor_result_written = False
    auth_reason_code: str | None = None
    auth_context: dict | None = None
    try:
        if os.getenv("GH_TOKEN"):
            run_command(["gh", "auth", "setup-git"])
        run_command(
            ["git", "clone", "--single-branch", "--branch", args.base_branch, args.repository, str(workspace)]
        )
        base_commit_sha = run_command(
            ["git", "rev-parse", "HEAD"], cwd=workspace
        ).stdout.strip()
        today = datetime.now(UTC).date()
        branch = f"crawler-factory/{today.isoformat()}-{run_id[-8:]}"
        run_command(["git", "switch", "-c", branch], cwd=workspace)
        run_command(
            ["git", "config", "user.name", os.getenv("CRAWLER_FACTORY_GIT_NAME", "ClassicalBot")],
            cwd=workspace,
        )
        run_command(
            [
                "git",
                "config",
                "user.email",
                os.getenv("CRAWLER_FACTORY_GIT_EMAIL", "classicalbot@users.noreply.github.com"),
            ],
            cwd=workspace,
        )
        manual_ids = list(args.source_ids or [])
        if args.urls:
            for url in args.urls:
                country = args.country_code or country_code_for_url(url)
                source = registry.ingest_source(
                    url,
                    country,
                    discovered_by="factory_manual",
                )
                manual_ids.append(source["id"])
        registry.preflight()
        registry.recover_expired_leases()
        registry.reconcile_workspace(workspace)
        reconcile_pull_requests(registry, workspace)
        registry.reconcile_run_statuses()
        registry.create_run(run_id, worker_id, branch, args.model)
        run_created = True
        child_env = sanitized_child_env(run_dir)
        for _ in range(args.max_urls):
            source = registry.claim_next(
                worker_id,
                lease_minutes=args.timeout_minutes + 15,
                source_ids=manual_ids or None,
            )
            if not source:
                break
            claimed_count += 1
            attempt_id = registry.start_attempt(source, run_id)
            resolved_url = resolve_source_url(source["canonical_url"])
            assigned_path = source.get("crawler_path") or str(
                pending_crawler_directory(resolved_url)
            )
            source = registry.assign_resolved_identity(
                source["id"],
                resolved_url,
                assigned_path,
                country_code=source.get("country_code"),
                geographic_scope=source.get("geographic_scope", "unknown"),
            )
            if source["status"] == "duplicate":
                result = {
                    "source_id": source["id"],
                    "url": source["canonical_url"],
                    "resolved_url": resolved_url,
                    "crawler_directory": assigned_path,
                    "status": "duplicate",
                    "commit": None,
                    "generation_warning": None,
                    "final_response": None,
                    "error": None,
                    "duration_seconds": 0,
                }
                registry.complete_attempt(
                    source["id"],
                    attempt_id,
                    "duplicate",
                    resolved_url=resolved_url,
                    crawler_path=assigned_path,
                )
                results.append(result)
                logger.info(
                    "Crawler source attempt completed as duplicate",
                    extra={
                        "event": "factory_source_completed",
                        "run_id": run_id,
                        "attempt_id": attempt_id,
                        "source_id": source["id"],
                        "url": source["canonical_url"],
                        "crawler_directory": assigned_path,
                        "status": "duplicate",
                        "duration_seconds": 0,
                    },
                )
                continue
            result = attempt_source(
                workspace,
                run_dir,
                source,
                args.timeout_minutes,
                child_env,
                args.model,
                args.validation_timeout_minutes,
                identity_resolver=lambda current, scope, country, path: (
                    registry.assign_resolved_identity(
                        current["id"],
                        resolved_url,
                        path,
                        country_code=country,
                        geographic_scope=scope,
                    )
                ),
                run_id=run_id,
                attempt_id=attempt_id,
            )
            result["resolved_url"] = resolved_url
            results.append(result)
            if result["status"] == "auth_required":
                auth_reason_code = result.get("auth_reason_code") or "login_required"
                auth_context = {"source_id": source["id"], "run_id": run_id}
                registry.abandon_attempt(
                    source["id"],
                    attempt_id,
                    error=f"Codex authentication required: {auth_reason_code}",
                )
                reset_failed_attempt(workspace)
                logger.critical(
                    "Codex authentication failure stopped crawler generation",
                    extra={
                        "event": "codex_auth_detected",
                        "component": "crawler-factory",
                        "reason_code": auth_reason_code,
                        **auth_context,
                    },
                )
                break
            registry.complete_attempt(
                source["id"],
                attempt_id,
                result["status"],
                resolved_url=resolved_url,
                crawler_path=result["crawler_directory"],
                commit_sha=result.get("commit"),
                warning=result.get("generation_warning"),
                error=result.get("error"),
            )
            if result["status"] in {"generated", "blocked"}:
                successful_source_ids.append(source["id"])
            else:
                reset_failed_attempt(workspace)

        report_path = run_dir / "batch-report.json"
        report_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        status_counts = {
            status: sum(result["status"] == status for result in results)
            for status in {result["status"] for result in results}
        }
        logger.info(
            "Crawler factory batch attempts completed",
            extra={
                "event": "factory_batch_attempts_completed",
                "run_id": run_id,
                "attempt_count": len(results),
                "generated_count": status_counts.get("generated", 0),
                "blocked_count": status_counts.get("blocked", 0),
                "failed_count": status_counts.get("generation_failed", 0),
                "duplicate_count": status_counts.get("duplicate", 0),
                "report_path": str(report_path),
            },
        )
        if not successful_source_ids:
            registry.finish_run(run_id, "failed" if auth_reason_code else "no_changes")
            write_supervisor_result(
                args.result_path,
                run_id=run_id,
                claimed_count=claimed_count,
                results=results,
                status="auth_required" if auth_reason_code else "no_changes",
                auth_reason_code=auth_reason_code,
                auth_context=auth_context,
            )
            supervisor_result_written = True
            if auth_reason_code:
                logger.critical(
                    "Factory batch stopped because Codex authentication is required",
                    extra={
                        "event": "factory_batch_auth_required",
                        "reason_code": auth_reason_code,
                        "report_path": str(report_path),
                    },
                )
            else:
                logger.info(
                    "Factory batch produced no changes",
                    extra={
                        "event": "factory_batch_no_changes",
                        "report_path": str(report_path),
                    },
                )
            return
        if args.no_push:
            registry.finish_run(run_id, "failed")
            write_supervisor_result(
                args.result_path,
                run_id=run_id,
                claimed_count=claimed_count,
                results=results,
                status="auth_required" if auth_reason_code else "no_push",
                auth_reason_code=auth_reason_code,
                auth_context=auth_context,
            )
            supervisor_result_written = True
            logger.info(
                "Factory commits created without push",
                extra={
                    "event": "factory_commits_created",
                    "commit_count": len(successful_source_ids),
                    "workspace": str(workspace),
                    "report_path": str(report_path),
                },
            )
            args.keep_workspace = True
            return

        run_command(["git", "push", "--set-upstream", "origin", branch], cwd=workspace)
        body_path = run_dir / "pull-request.md"
        body_path.write_text(pr_body(results, args.model, run_id), encoding="utf-8")
        pr_url = run_command(
            [
                "gh",
                "pr",
                "create",
                "--base",
                args.base_branch,
                "--head",
                branch,
                "--title",
                f"Automated crawler batch: {today.isoformat()}",
                "--body-file",
                str(body_path),
            ],
            cwd=workspace,
        ).stdout.strip()
        registry.mark_pr_open(run_id, successful_source_ids, pr_url)
        registry.finish_run(run_id, "pr_open", pull_request_url=pr_url)
        logger.info(
            "Opened crawler-factory pull request",
            extra={"event": "factory_pull_request_opened", "pull_request_url": pr_url},
        )
        write_supervisor_result(
            args.result_path,
            run_id=run_id,
            claimed_count=claimed_count,
            results=results,
            status="auth_required" if auth_reason_code else "pr_open",
            pull_request_url=pr_url,
            base_commit_sha=base_commit_sha,
            auth_reason_code=auth_reason_code,
            auth_context=auth_context,
        )
        supervisor_result_written = True
    except Exception:
        if auth_reason_code and successful_source_ids:
            registry.transition_sources(successful_source_ids, "needs_attention")
        if run_created:
            registry.finish_run(run_id, "failed")
        if not supervisor_result_written:
            write_supervisor_result(
                args.result_path,
                run_id=run_id,
                claimed_count=claimed_count,
                results=results,
                status="auth_required" if auth_reason_code else "failed",
                base_commit_sha=base_commit_sha,
                auth_reason_code=auth_reason_code,
                auth_context=auth_context,
            )
        raise
    finally:
        if not args.keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def main() -> None:
    configure_logging("classical-crawler-factory")
    args = parse_args()
    load_dotenv()
    args.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with args.lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another crawler-factory run is already active")
        with CrawlerRegistry() as registry:
            run_factory(args, registry)


if __name__ == "__main__":
    main()
