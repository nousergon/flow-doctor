## What & why

<!-- What does this change and why? Link any related issue. -->

## Checklist

- [ ] Tests added/updated for the behavior change
- [ ] `pytest` passes locally — the coverage floor in `pyproject.toml` is a ratchet, raised as coverage improves and never lowered to make a change pass
- [ ] `CHANGELOG.md` updated
- [ ] No secrets or proprietary prompt content committed
- [ ] `pyproject.toml::version` bumped if this PR touches `flow_doctor/**` (see `version-bump-check.yml`)
- [ ] Fail-loud preserved outside the deliberate capture-path degrade exception — no new silent `except: pass` swallows

## Test plan

<!-- How you verified this works. -->

---

**Prepared by:** <!-- model name, e.g. claude-opus-5, claude-sonnet-5, claude-haiku-4-5 -->
