# Security Policy

## Reporting a vulnerability

If you find a security vulnerability in flow-doctor, please report it privately:

- **Preferred:** open a [GitHub Security Advisory](https://github.com/nousergon/flow-doctor/security/advisories/new). This keeps the discussion private until a fix ships.
- **Alternative:** email `security@nousergon.ai` with a description and reproduction steps.

Please **do not** open a public issue for security reports. I aim to acknowledge within 72 hours and ship a fix or mitigation within 14 days for high-severity issues.

## Scope

flow-doctor is a self-hosted pipeline error handler: it captures failures, deduplicates them, diagnoses them (optionally via an LLM), and can auto-fix and notify (Telegram/Slack). The sensitive surface is **credential handling, the LLM/diagnosis path, and the auto-fix path**. In scope:

- **Credential exposure:** any path that leaks an LLM API key, a Telegram/Slack token, or other configured secret — through logs, error messages, captured tracebacks, diagnosis output, or notifications.
- **Injection / escalation:** prompt injection via captured error content that escalates into unintended auto-fix actions; command or code injection in the capture, diagnosis, or auto-fix path; unsafe handling of model output that reaches a shell, filesystem path, or patch application.
- **Auto-fix safety:** any path where flow-doctor applies a fix beyond its documented, opt-in scope, or without the configured review gate.
- **Supply-chain:** a dependency or install path that could execute untrusted code during install or normal operation.

Out of scope:

- DoS via traffic volume (single-user self-host infrastructure).
- Cost-runaway from your own LLM provider configuration (set your own provider spend limits).
- Vulnerabilities in upstream dependencies not yet publicly disclosed — report those upstream first.
- Issues requiring local filesystem/process access (if your machine is compromised, the threat model has already failed — your `.env` and configured credentials live there).

## Threat model assumptions

- **Single-user self-host.** There is no multi-user model in the public engine.
- **Credentials live in your own environment/config** (`.env` or your process manager's secret store), not in flow-doctor itself. Protect them with filesystem permissions; rotate if exposed.
- **Captured error content and model diagnosis output are treated as untrusted text.** If you extend the auto-fix path to act on model output with elevated privileges, re-evaluate this assumption.
- **HTTPS** is assumed for all provider (LLM, Telegram, Slack) traffic.

## Hardening recommendations for self-hosters

- Scope any LLM/notification API keys to least privilege and keep them out of version control.
- Keep `.env` at `600` and never commit it; use your platform's secret store for any non-local deploy.
- Set provider-side spend limits (your LLM provider) — flow-doctor's own budget controls are a backstop, not a substitute for account-level limits.
- Review auto-fix output before it is applied unattended, especially on pipelines with write access to production data.
