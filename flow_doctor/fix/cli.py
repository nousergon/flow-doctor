"""CLI entry point for fix generation: parses issue, generates fix, creates PR."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from flow_doctor.core.config import AutoFixConfig, load_config
from flow_doctor.core.models import FixAttempt
from flow_doctor.fix.generator import FixGenerator
from flow_doctor.fix.pr_creator import PRCreator
from flow_doctor.fix.replay_store import ReplayStore
from flow_doctor.fix.scope_guard import ScopeGuard
from flow_doctor.fix.validator import TestValidator
from flow_doctor.notify.github import GitHubNotifier


# Categories that cannot be auto-fixed
_UNFIXABLE_CATEGORIES = {"EXTERNAL", "INFRA"}


def parse_issue_metadata(body: str) -> Optional[Dict[str, str]]:
    """Extract flow-doctor metadata from a GitHub issue body.

    Looks for the hidden HTML comment block:
    <!-- flow-doctor-metadata
    key: value
    ...
    -->
    """
    pattern = r"<!-- flow-doctor-metadata\s*\n(.*?)\n-->"
    match = re.search(pattern, body, re.DOTALL)
    if not match:
        return None

    metadata: Dict[str, str] = {}
    for line in match.group(1).strip().split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()
    return metadata


def fetch_issue(repo: str, issue_number: int, token: str) -> Dict[str, Any]:
    """Fetch a GitHub issue by number."""
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    req = Request(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _is_config_credentials_issue(root_cause: str) -> bool:
    """Check if a CONFIG issue is about credentials/secrets."""
    keywords = ["credential", "secret", "password", "token", "api_key", "api key"]
    lower = root_cause.lower()
    return any(kw in lower for kw in keywords)


def _read_file_contents(
    repo_path: str, files: List[str]
) -> Dict[str, str]:
    """Read file contents from the local repo checkout."""
    contents: Dict[str, str] = {}
    for f in files:
        # Strip line number references (e.g., "main.py:5" -> "main.py")
        clean = f.split(":")[0]
        path = Path(repo_path) / clean
        if path.is_file():
            try:
                contents[clean] = path.read_text()
            except Exception:
                pass
    return contents


def _find_test_files(
    repo_path: str, affected_files: List[str]
) -> Dict[str, str]:
    """Find and read test files corresponding to affected source files."""
    contents: Dict[str, str] = {}
    tests_dir = Path(repo_path) / "tests"
    if not tests_dir.is_dir():
        return contents

    for f in affected_files:
        clean = f.split(":")[0]
        stem = Path(clean).stem
        # Look for test_<stem>.py
        test_path = tests_dir / f"test_{stem}.py"
        if test_path.is_file():
            try:
                contents[str(test_path.relative_to(repo_path))] = test_path.read_text()
            except Exception:
                pass
    return contents


def _get_default_branch(repo_path: str) -> str:
    """Get the default branch name from the local repo."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode == 0:
            # e.g., "refs/remotes/origin/main" -> "main"
            return result.stdout.strip().split("/")[-1]
    except Exception:
        pass
    return "main"


def generate_fix(
    issue_number: int,
    repo: str,
    token: str,
    config_path: Optional[str] = None,
    dry_run: bool = False,
    repo_path: Optional[str] = None,
) -> Tuple[bool, str]:
    """Main fix generation flow.

    Returns:
        (success, message) — success is True if a PR was created (or would be in dry-run).
    """
    repo_path = repo_path or os.getcwd()

    # Load config. The fix CLI only consumes auto_fix/diagnosis (+ the --token
    # arg for all GitHub ops) — it never reads the notify/github blocks. Skip
    # them so their ${VAR}s (e.g. ${EMAIL_SENDER} / ${FLOW_DOCTOR_GITHUB_TOKEN}
    # on a CI runtime that has no email creds) don't abort the load. Resolution
    # stays strict for what's kept, so a genuinely-missing diagnosis.api_key
    # still fails loud.
    config = load_config(config_path=config_path, skip_sections=("notify", "github"))
    af_config = config.auto_fix

    # Fetch issue
    print(f"[flow-doctor] Fetching issue #{issue_number} from {repo}...")
    try:
        issue = fetch_issue(repo, issue_number, token)
    except Exception as e:
        return (False, f"Failed to fetch issue: {e}")

    body = issue.get("body", "")
    metadata = parse_issue_metadata(body)
    if not metadata:
        msg = "No flow-doctor metadata found in issue body"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    # Extract diagnosis fields
    category = metadata.get("category", "")
    confidence = float(metadata.get("confidence", "0"))
    root_cause = metadata.get("root_cause", "")
    remediation = metadata.get("remediation", "")
    affected_files_str = metadata.get("affected_files", "")
    affected_files = [f.strip() for f in affected_files_str.split(",") if f.strip()]
    flow_name = metadata.get("flow_name", config.flow_name)
    diagnosis_id = metadata.get("diagnosis_id", "")
    error_signature = metadata.get("error_signature", "")

    # Gate: unfixable category
    if category in _UNFIXABLE_CATEGORIES:
        msg = f"Category `{category}` is not auto-fixable"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    # Gate: CONFIG with credentials
    if category == "CONFIG" and _is_config_credentials_issue(root_cause):
        msg = "CONFIG issue involving credentials/secrets is not auto-fixable"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    # Gate: confidence threshold
    if confidence < af_config.confidence_threshold:
        msg = (
            f"Confidence {confidence:.0%} below threshold "
            f"{af_config.confidence_threshold:.0%}"
        )
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    # Gate: no affected files
    if not affected_files:
        msg = "No affected files specified in diagnosis"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    print(f"[flow-doctor] Diagnosis: {category} | confidence={confidence:.0%} | files={affected_files}")

    # Read file contents
    file_contents = _read_file_contents(repo_path, affected_files)
    if not file_contents:
        msg = "Could not read any affected files from the local checkout"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    test_contents = _find_test_files(repo_path, affected_files)

    # Check replay store for prior rejections
    prior_rejections: List[str] = []
    try:
        from flow_doctor.storage.sqlite import SQLiteStorage
        storage = SQLiteStorage(config.store.path)
        storage.init_schema()
        replay = ReplayStore(storage)
        if diagnosis_id:
            prior_rejections = replay.get_rejections(diagnosis_id)
    except Exception:
        pass

    # Generate fix via LLM
    api_key = (
        config.diagnosis.api_key
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if not api_key:
        msg = "No Anthropic API key configured"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    model = af_config.model or config.diagnosis.model
    print(f"[flow-doctor] Generating fix with {model}...")

    generator = FixGenerator(api_key=api_key, model=model)
    diff = generator.generate(
        category=category,
        root_cause=root_cause,
        confidence=confidence,
        remediation=remediation,
        affected_files=affected_files,
        file_contents=file_contents,
        test_contents=test_contents,
        prior_rejections=prior_rejections or None,
    )

    if not diff:
        msg = "LLM could not generate a confident fix (returned NO_FIX)"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    # Scope guard
    diff_files = FixGenerator.extract_files_from_diff(diff)
    scope_guard = ScopeGuard(allow=af_config.scope.allow, deny=af_config.scope.deny)
    passed, violations = scope_guard.check(diff_files)
    if not passed:
        msg = f"Scope guard violations: {'; '.join(violations)}"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    # Apply diff
    if not PRCreator.apply_diff(repo_path, diff):
        msg = "Failed to apply generated diff"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    # Run tests
    print(f"[flow-doctor] Running tests: {af_config.test_command}")
    validator = TestValidator()
    test_passed, test_output = validator.run(af_config.test_command, repo_path)

    # Record fix attempt
    attempt = FixAttempt(
        diagnosis_id=diagnosis_id,
        diff=diff,
        test_passed=test_passed,
        test_output=test_output[:5000] if test_output else None,
    )

    if not test_passed:
        attempt.rejection_reason = "Tests failed"
        _save_attempt(config, attempt)
        # Revert changes
        _revert_changes(repo_path)
        msg = f"Fix attempted but tests failed:\n```\n{test_output[:2000]}\n```"
        _comment_failure(repo, issue_number, token, msg)
        return (False, "Tests failed after applying fix")

    # Dry run check
    if dry_run or af_config.dry_run:
        _revert_changes(repo_path)
        print("[flow-doctor] Dry run — skipping PR creation")
        print(f"[flow-doctor] Generated diff:\n{diff}")
        return (True, "Dry run successful — fix generated and tests passed")

    # Create branch, commit, push, PR
    base_branch = _get_default_branch(repo_path)
    branch = PRCreator.create_branch(repo_path, flow_name)
    commit_msg = f"fix({flow_name}): auto-fix for {category.lower()} issue\n\nDiagnosis: {root_cause[:200]}\nIssue: #{issue_number}\n\nGenerated by Flow Doctor"
    if not PRCreator.commit_and_push(repo_path, branch, commit_msg):
        msg = "Failed to push fix branch"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    pr_title = f"fix({flow_name}): {root_cause[:60]}"
    pr_body = (
        f"## Auto-Fix for #{issue_number}\n\n"
        f"**Category:** {category}\n"
        f"**Confidence:** {confidence:.0%}\n"
        f"**Root Cause:** {root_cause}\n\n"
        f"### Changes\n```diff\n{diff}\n```\n\n"
        f"### Test Results\nAll tests passed.\n\n"
        f"---\n*Generated by [Flow Doctor](https://github.com/brianmcmahon/flow-doctor)*"
    )

    pr_url = PRCreator.create_pr(
        repo=repo,
        head=branch,
        base=base_branch,
        title=pr_title,
        body=pr_body,
        token=token,
        labels=["flow-doctor", "auto-fix"],
    )

    if not pr_url:
        msg = "Branch pushed but PR creation failed"
        _comment_failure(repo, issue_number, token, msg)
        return (False, msg)

    attempt.pr_url = pr_url
    attempt.pr_status = "open"
    _save_attempt(config, attempt)

    # Comment on the issue with the PR link
    comment = f"Fix PR created: {pr_url}"
    GitHubNotifier.comment_on_issue(repo, issue_number, comment, token)

    # Ping Telegram so the fix PR is as visible as the original issue alert.
    # Best-effort: a telegram failure must not flip the (succeeded) PR result,
    # but it is logged to stderr so the notification path itself fails loud.
    _notify_telegram_pr(config, flow_name, pr_url, issue_number)

    print(f"[flow-doctor] PR created: {pr_url}")
    return (True, f"PR created: {pr_url}")


def _notify_telegram_pr(config, flow_name: str, pr_url: str, issue_number: int) -> None:
    """Send a Telegram ping announcing an auto-generated fix PR.

    Honours a ``telegram`` notifier in the flow-doctor config (creds already
    ``${VAR}``-resolved by ``load_config``); falls back to the
    ``FLOW_DOCTOR_TELEGRAM_*`` env vars. No telegram configured → skip quietly.
    Failures are logged to stderr, never raised — the PR already exists.
    """
    try:
        from flow_doctor.notify.telegram import TelegramNotifier

        notifier: Optional[TelegramNotifier] = None
        for nc in getattr(config, "notify", []) or []:
            if getattr(nc, "type", None) == "telegram" and getattr(nc, "bot_token", None):
                notifier = TelegramNotifier(
                    bot_token=nc.bot_token,
                    chat_id=nc.chat_id,
                    message_thread_id=getattr(nc, "message_thread_id", None),
                    parse_mode=getattr(nc, "parse_mode", "Markdown"),
                    disable_notification=getattr(nc, "disable_notification", False),
                )
                break

        if notifier is None:
            bot_token = os.environ.get("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN")
            raw_chat = os.environ.get("FLOW_DOCTOR_TELEGRAM_CHAT_ID")
            if not bot_token or not raw_chat:
                print(
                    "[flow-doctor] No telegram notifier configured — skipping PR ping",
                    file=sys.stderr,
                )
                return
            chat_id: Any = int(raw_chat) if raw_chat.lstrip("-").isdigit() else raw_chat
            thread = os.environ.get("FLOW_DOCTOR_TELEGRAM_MESSAGE_THREAD_ID")
            notifier = TelegramNotifier(
                bot_token=bot_token,
                chat_id=chat_id,
                message_thread_id=int(thread) if thread and thread.isdigit() else None,
            )

        # Plain text: PR URLs contain '_' / '-' that Markdown would mangle.
        msg = f"🔧 Flow Doctor fix PR for {flow_name} (issue #{issue_number}): {pr_url}"
        if notifier.send_raw(msg, parse_mode=None) is None:
            print(
                "[flow-doctor] Telegram PR ping returned failure (see notifier log)",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001 - secondary notification, PR already created
        print(f"[flow-doctor] Telegram PR ping failed: {e}", file=sys.stderr)


def _comment_failure(repo: str, issue_number: int, token: str, reason: str) -> None:
    """Comment on the issue explaining why the fix was not generated."""
    body = f"**Flow Doctor Auto-Fix:** Could not generate fix.\n\n**Reason:** {reason}"
    GitHubNotifier.comment_on_issue(repo, issue_number, body, token)
    print(f"[flow-doctor] Fix skipped: {reason}", file=sys.stderr)


def _save_attempt(config, attempt: FixAttempt) -> None:
    """Best-effort save of fix attempt to storage."""
    try:
        from flow_doctor.storage.sqlite import SQLiteStorage
        storage = SQLiteStorage(config.store.path)
        storage.init_schema()
        storage.save_fix_attempt(attempt)
    except Exception:
        pass


def _revert_changes(repo_path: str) -> None:
    """Revert any uncommitted changes."""
    import subprocess
    try:
        subprocess.run(
            ["git", "checkout", "."],
            cwd=repo_path, capture_output=True,
        )
    except Exception:
        pass


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="flow-doctor",
        description="Flow Doctor auto-fix CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    fix_parser = subparsers.add_parser(
        "generate-fix",
        help="Generate a fix PR for a diagnosed issue",
    )
    fix_parser.add_argument(
        "--issue-number", type=int, required=True,
        help="GitHub issue number to fix",
    )
    fix_parser.add_argument(
        "--repo", type=str, default=None,
        help="GitHub repo (owner/name). Defaults to config or GITHUB_REPOSITORY env var.",
    )
    fix_parser.add_argument(
        "--token", type=str, default=None,
        help="GitHub token. Defaults to GITHUB_TOKEN env var.",
    )
    fix_parser.add_argument(
        "--config", type=str, default=None,
        help="Path to flow-doctor.yaml config file",
    )
    fix_parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate the fix but don't create a PR",
    )
    fix_parser.add_argument(
        "--repo-path", type=str, default=None,
        help="Path to the local repo checkout (defaults to cwd)",
    )

    args = parser.parse_args()

    if args.command != "generate-fix":
        parser.print_help()
        sys.exit(1)

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    token = args.token or os.environ.get("GITHUB_TOKEN")

    if not repo:
        print("Error: --repo is required (or set GITHUB_REPOSITORY)", file=sys.stderr)
        sys.exit(1)
    if not token:
        print("Error: --token is required (or set GITHUB_TOKEN)", file=sys.stderr)
        sys.exit(1)

    success, message = generate_fix(
        issue_number=args.issue_number,
        repo=repo,
        token=token,
        config_path=args.config,
        dry_run=args.dry_run,
        repo_path=args.repo_path,
    )

    print(f"[flow-doctor] Result: {message}")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
