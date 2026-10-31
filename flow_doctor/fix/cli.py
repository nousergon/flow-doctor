"""CLI entry point for fix generation: parses issue, generates fix, creates PR."""

from __future__ import annotations

import argparse
import enum
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from flow_doctor.core.config import AutoFixConfig, FlowDoctorConfig, load_config
from flow_doctor.core.models import FixAttempt
from flow_doctor.core.router import RouterUnresolvable
from flow_doctor.fix.generator import FixGenerator
from flow_doctor.fix.pr_creator import PRCreator
from flow_doctor.fix.replay_store import ReplayStore
from flow_doctor.fix.scope_guard import (
    ScopeGuard,
    changed_paths,
    patch_inventory,
    resolve_confined,
    tracked_files,
)
from flow_doctor.fix.validator import TestValidator
from flow_doctor.notify.github import GitHubNotifier


# Categories that cannot be auto-fixed
_UNFIXABLE_CATEGORIES = {"EXTERNAL", "INFRA"}


class FixOutcome(enum.Enum):
    """Terminal state of a fix-generation run.

    Three states, deliberately distinct, because the workflow exit code keys
    off them:

    - ``CREATED``  — a fix PR was opened (or, under --dry-run, would have been).
                     The happy path.
    - ``SKIPPED``  — flow-doctor worked *as designed* and decided no auto-fix
                     applies: the diagnosis is out of auto-fix scope (EXTERNAL/
                     INFRA, credentials, low confidence), there's nothing
                     actionable to patch, or a safety gate (scope guard,
                     test-validation) correctly withheld a PR. This is a
                     NOTIFICATION, not an error — exit 0 so the CI job stays
                     green and no "fix generation failed" alarm fires.
    - ``FAILED``   — the fixer machinery itself broke: couldn't reach GitHub,
                     couldn't read the checkout, no API key, diff wouldn't
                     apply, push/PR failed. A genuine error — exit 1.

    The bug this fixes: EXTERNAL (a provider outage flow-doctor cannot patch)
    was returning the same "failure" signal as a real malfunction, painting the
    run red and double-commenting "fix generation failed" on a correct no-op.
    """

    CREATED = "created"
    SKIPPED = "skipped"
    FAILED = "failed"

    @property
    def is_error(self) -> bool:
        """True only for genuine fixer malfunctions (drives exit code 1)."""
        return self is FixOutcome.FAILED


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
    repo_path: str,
    files: List[str],
    guard: Optional[ScopeGuard] = None,
    refusals: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Read diagnosis-named files from the local checkout, confined.

    ``affected_files`` comes from issue metadata, so every name goes through
    ``resolve_confined`` before anything is opened: it must be a regular,
    tracked file inside ``repo_path``, allowed and not denied by ``guard``,
    and not secret-shaped (alpha-engine-config-I12897). A refused name is
    appended to ``refusals`` as ``path: reason`` and never read.
    """
    guard = guard or ScopeGuard(allow=[], deny=[])
    tracked = tracked_files(repo_path)
    contents: Dict[str, str] = {}
    for f in files:
        # Strip line number references (e.g., "main.py:5" -> "main.py")
        clean = f.split(":")[0].strip()
        path, reason = resolve_confined(repo_path, clean, tracked, guard)
        if path is None:
            if refusals is not None:
                refusals.append(f"{clean!r}: {reason}")
            print(f"[flow-doctor] refused context file {clean!r}: {reason}", file=sys.stderr)
            continue
        try:
            contents[clean] = path.read_text()
        except Exception:
            pass
    return contents


def _find_test_files(
    repo_path: str,
    affected_files: List[str],
    guard: Optional[ScopeGuard] = None,
) -> Dict[str, str]:
    """Find and read ``tests/test_<stem>.py`` for each affected source file.

    Same confined resolver as ``_read_file_contents``: a test file that is a
    symlink, untracked, denied or secret-shaped is not read.
    """
    guard = guard or ScopeGuard(allow=[], deny=[])
    tracked = tracked_files(repo_path)
    contents: Dict[str, str] = {}
    for f in affected_files:
        stem = Path(f.split(":")[0]).stem
        rel = f"tests/test_{stem}.py"
        path, _reason = resolve_confined(repo_path, rel, tracked, guard)
        if path is None:
            continue
        try:
            contents[rel] = path.read_text()
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
) -> Tuple[FixOutcome, str]:
    """Main fix generation flow.

    Returns:
        (outcome, message) — see :class:`FixOutcome`. ``CREATED`` when a PR was
        opened (or would be under dry-run); ``SKIPPED`` when flow-doctor
        deliberately declined to auto-fix (a notification, not an error);
        ``FAILED`` when the fixer machinery itself broke.
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
        return (FixOutcome.FAILED, f"Failed to fetch issue: {e}")

    body = issue.get("body", "")
    metadata = parse_issue_metadata(body)
    if not metadata:
        # The fix workflow only triggers on flow-doctor-filed issues, so a
        # missing metadata block means the issue is malformed or the wrong
        # issue was labelled — a genuine fault, not a working-as-intended skip.
        msg = "No flow-doctor metadata found in issue body"
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

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

    # Gate: unfixable category. EXTERNAL (provider outage) / INFRA are not code
    # bugs — there is nothing to patch. A deliberate skip, not a failure.
    if category in _UNFIXABLE_CATEGORIES:
        msg = f"Category `{category}` is not auto-fixable"
        _comment_skipped(repo, issue_number, token, msg)
        return (FixOutcome.SKIPPED, msg)

    # Gate: CONFIG with credentials — auto-fixing secrets is intentionally out
    # of scope (a safety policy, working as designed).
    if category == "CONFIG" and _is_config_credentials_issue(root_cause):
        msg = "CONFIG issue involving credentials/secrets is not auto-fixable"
        _comment_skipped(repo, issue_number, token, msg)
        return (FixOutcome.SKIPPED, msg)

    # Gate: confidence threshold — too uncertain to propose a fix; declining is
    # the correct behaviour.
    if confidence < af_config.confidence_threshold:
        msg = (
            f"Confidence {confidence:.0%} below threshold "
            f"{af_config.confidence_threshold:.0%}"
        )
        _comment_skipped(repo, issue_number, token, msg)
        return (FixOutcome.SKIPPED, msg)

    # Gate: no affected files — the diagnosis named nothing to patch; there is
    # no actionable fix to attempt.
    if not affected_files:
        msg = "No affected files specified in diagnosis"
        _comment_skipped(repo, issue_number, token, msg)
        return (FixOutcome.SKIPPED, msg)

    print(f"[flow-doctor] Diagnosis: {category} | confidence={confidence:.0%} | files={affected_files}")

    # Read file contents — confined to tracked, in-scope repository files
    # BEFORE anything is opened or sent to a model (alpha-engine-config-I12897).
    scope_guard = ScopeGuard(allow=af_config.scope.allow, deny=af_config.scope.deny)
    refusals: List[str] = []
    file_contents = _read_file_contents(repo_path, affected_files, scope_guard, refusals)
    if not file_contents and refusals:
        # Every named file was refused by the read boundary: the guard
        # working as designed, so a skip. The reasons name rules, not contents.
        msg = f"No affected file passed the read boundary: {'; '.join(refusals)}"
        _comment_skipped(repo, issue_number, token, msg)
        return (FixOutcome.SKIPPED, msg)
    if not file_contents:
        # The diagnosis named files but none exist in the checkout — a real
        # fault (wrong checkout / stale paths), not a working-as-intended skip.
        msg = "Could not read any affected files from the local checkout"
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

    test_contents = _find_test_files(
        repo_path,
        list(file_contents),
        ScopeGuard(allow=[], deny=af_config.scope.deny),
    )

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

    # Generate fix via LLM — same provider/base_url/model_group the diagnosis
    # runs on. `provider: anthropic` was REMOVED from `FixGenerator` in
    # 0.15.0 (the fleet's Anthropic account carries a $0 budget,
    # alpha-engine-config-I7460) — refuse it here with the replacement named,
    # rather than letting `FixGenerator.__init__`'s ValueError surface as an
    # uncaught crash instead of a GitHub comment.
    provider = config.diagnosis.provider

    if not provider:
        # `diagnosis.provider` has NO default (0.15.0) — flow-doctor has no
        # vendor to fall back to. `load_config` already fails loud at load
        # time when `diagnosis.enabled=True`, but auto-fix reads this same
        # field independently of that flag (a fix-generation-only deployment
        # can run with `diagnosis.enabled=False`), so it needs its own gate.
        msg = (
            "diagnosis.provider is not set. flow-doctor has no default LLM "
            "vendor. Set diagnosis.provider='router' with diagnosis.model_group "
            "(one of: low, med, high, ultra), or diagnosis.provider="
            "'openai_compat' with diagnosis.base_url."
        )
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

    if provider == "anthropic":
        msg = (
            "diagnosis.provider='anthropic' is no longer supported for "
            "auto-fix (removed in 0.15.0 — the direct-Anthropic transport "
            "is dead weight against a $0 account budget). Set "
            "diagnosis.provider='router' with diagnosis.model_group (one of: "
            "low, med, high, ultra), or diagnosis.provider='openai_compat' "
            "with diagnosis.base_url."
        )
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

    if provider not in ("router", "openai_compat"):
        msg = (
            f"diagnosis.provider must be 'router' or 'openai_compat', got "
            f"'{provider}'."
        )
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

    if provider == "router" and not config.diagnosis.model_group:
        msg = (
            "diagnosis.provider='router' requires diagnosis.model_group "
            "(one of: low, med, high, ultra) and it is not set."
        )
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

    if provider == "openai_compat" and not config.diagnosis.base_url:
        # Same fail-closed rule as the diagnosis path: no default endpoint.
        msg = (
            "diagnosis.provider='openai_compat' requires diagnosis.base_url "
            "and it is not set. flow-doctor ships no default endpoint."
        )
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

    model = af_config.model or config.diagnosis.model

    if provider == "router":
        # No api_key gate: krepis resolves the credential itself (env var,
        # then the per-consumer SSM secret) — same reasoning as the
        # diagnosis-side RouterProvider wiring in core/client.py.
        print(f"[flow-doctor] Generating fix with router:{config.diagnosis.model_group}...")
        generator = FixGenerator(
            model=model,
            provider="router",
            model_group=config.diagnosis.model_group,
        )
    else:
        api_key = config.diagnosis.api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            msg = "No LLM API key configured (diagnosis.api_key or OPENROUTER_API_KEY)"
            _comment_failure(repo, issue_number, token, msg)
            return (FixOutcome.FAILED, msg)

        print(f"[flow-doctor] Generating fix with {provider}:{model}...")
        generator = FixGenerator(
            api_key=api_key,
            model=model,
            provider=provider,
            base_url=config.diagnosis.base_url,
        )
    try:
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
    except RouterUnresolvable as e:
        # Fail closed, but as a reported FAILED outcome (a GitHub comment)
        # rather than an uncaught crash — same treatment every other gate in
        # this function gets. Never falls back to a direct provider.
        msg = f"Router group {config.diagnosis.model_group!r} could not be resolved: {e}"
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

    if not diff:
        # The model deliberately declined (NO_FIX) — same spirit as the
        # confidence gate: a correct "no confident fix", not a malfunction.
        msg = "LLM could not generate a confident fix (returned NO_FIX)"
        _comment_skipped(repo, issue_number, token, msg)
        return (FixOutcome.SKIPPED, msg)

    # Scope guard — a safety gate. Blocking an out-of-scope diff is the guard
    # working as intended, so withholding the PR is a skip, not a failure.
    # The inventory is every path the patch touches per Git AND its headers:
    # deletions and both sides of a rename/copy (alpha-engine-config-I12896).
    inventory, why = patch_inventory(repo_path, diff)
    if inventory is None:
        msg = f"Scope guard refused an unreadable patch: {why}"
        _comment_skipped(repo, issue_number, token, msg)
        return (FixOutcome.SKIPPED, msg)
    passed, violations = scope_guard.check(inventory)
    if not passed:
        msg = f"Scope guard violations: {'; '.join(violations)}"
        _comment_skipped(repo, issue_number, token, msg)
        return (FixOutcome.SKIPPED, msg)

    # Apply diff. A diff that won't apply is a genuine fault (malformed patch).
    dirty_before = changed_paths(repo_path) or set()
    if not PRCreator.apply_diff(repo_path, diff):
        msg = "Failed to apply generated diff"
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

    # Independent check: what the apply actually changed must be inside the
    # approved inventory. Undo exactly this patch if it is not.
    after = changed_paths(repo_path)
    stray = sorted((after or set()) - dirty_before - set(inventory)) if after is not None else []
    if after is None or stray:
        PRCreator.reverse_diff(repo_path, diff)
        msg = (
            "Applied patch changed paths outside its approved inventory: "
            f"{', '.join(map(repr, stray)) or 'git status unavailable'}"
        )
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

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
        # The validator correctly rejected a fix that broke tests and reverted
        # it. Withholding the PR is the safety gate working as intended — a
        # skip, not a fixer malfunction.
        attempt.rejection_reason = "Tests failed"
        _save_attempt(config, attempt)
        # Revert changes
        _revert_changes(repo_path)
        msg = f"Fix attempted but tests failed:\n```\n{test_output[:2000]}\n```"
        _comment_skipped(repo, issue_number, token, msg)
        return (FixOutcome.SKIPPED, "Tests failed after applying fix")

    # Dry run check
    if dry_run or af_config.dry_run:
        _revert_changes(repo_path)
        print("[flow-doctor] Dry run — skipping PR creation")
        print(f"[flow-doctor] Generated diff:\n{diff}")
        return (FixOutcome.CREATED, "Dry run successful — fix generated and tests passed")

    # Create branch, commit, push, PR
    base_branch = _get_default_branch(repo_path)
    branch = PRCreator.create_branch(repo_path, flow_name)
    commit_msg = f"fix({flow_name}): auto-fix for {category.lower()} issue\n\nDiagnosis: {root_cause[:200]}\nIssue: #{issue_number}\n\nGenerated by Flow Doctor"
    if not PRCreator.commit_and_push(repo_path, branch, commit_msg, paths=inventory):
        msg = "Failed to push fix branch"
        _comment_failure(repo, issue_number, token, msg)
        return (FixOutcome.FAILED, msg)

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
        return (FixOutcome.FAILED, msg)

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
    return (FixOutcome.CREATED, f"PR created: {pr_url}")


def _notify_telegram_pr(config: FlowDoctorConfig, flow_name: str, pr_url: str, issue_number: int) -> None:
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


def _comment_skipped(repo: str, issue_number: int, token: str, reason: str) -> None:
    """Comment that no auto-fix applies — an informational notification.

    Used when flow-doctor worked as designed and deliberately declined to open
    a fix PR (out-of-scope category, low confidence, nothing to patch, a safety
    gate withholding a fix). Phrased as a notice, NOT a failure, because the run
    exits 0 and the issue exists for visibility only.
    """
    body = (
        "ℹ️ **Flow Doctor:** no auto-fix generated — this is expected, "
        "not an error.\n\n"
        f"**Reason:** {reason}\n\n"
        "This issue stands as a notification for human review; flow-doctor has "
        "nothing to patch automatically here."
    )
    GitHubNotifier.comment_on_issue(repo, issue_number, body, token)
    print(f"[flow-doctor] No auto-fix (skipped): {reason}", file=sys.stderr)


def _comment_failure(repo: str, issue_number: int, token: str, reason: str) -> None:
    """Comment that the fixer itself failed — a genuine error worth attention."""
    body = f"⚠️ **Flow Doctor Auto-Fix:** fix generation failed.\n\n**Reason:** {reason}"
    GitHubNotifier.comment_on_issue(repo, issue_number, body, token)
    print(f"[flow-doctor] Fix FAILED: {reason}", file=sys.stderr)


def _save_attempt(config: FlowDoctorConfig, attempt: FixAttempt) -> None:
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

    outcome, message = generate_fix(
        issue_number=args.issue_number,
        repo=repo,
        token=token,
        config_path=args.config,
        dry_run=args.dry_run,
        repo_path=args.repo_path,
    )

    print(f"[flow-doctor] Result [{outcome.value}]: {message}")
    # Exit non-zero ONLY for a genuine fixer malfunction. A deliberate skip
    # (e.g. an EXTERNAL provider outage flow-doctor can't patch) exits 0 so the
    # CI job stays green and the workflow's failure() alarm does not fire.
    sys.exit(1 if outcome.is_error else 0)


if __name__ == "__main__":
    main()
