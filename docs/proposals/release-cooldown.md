# Release cooldown for version resolution

- Author: Lalatendu Mohanty
- Created: 2026-03-31
- Status: Open
- Issue: [#877](https://github.com/python-wheel-build/fromager/issues/877)

## What

A configurable minimum release age ("cooldown") for version resolution.
When enabled, fromager skips package versions published fewer than N
days ago. One global setting controls all providers. Per-package
overrides allow exceptions.

## Why

Supply-chain attacks often publish a malicious package version and rely
on automated builds picking it up immediately. A cooldown window lets
the community detect and report compromised releases before fromager
consumes them. It also means new versions get broader testing before
entering the build.

References:

- [We should all be using dependency cooldowns](https://blog.yossarian.net/2025/11/21/We-should-all-be-using-dependency-cooldowns)
- [Malicious sha1hulud](https://helixguard.ai/blog/malicious-sha1hulud-2025-11-24)

## Goals

- A single `--min-release-age` CLI option (days, default 0) that
  applies to every resolver provider
- Per-package overrides via `resolver_dist.min_release_age` in package
  settings, taking priority over the CLI default
- Provider-aware fail-closed: providers that support timestamps
  reject candidates with missing `upload_time`; providers that do
  not support timestamps skip cooldown with a warning
- Pre-built wheels exempt (different trust model)
- `list-versions` shows timestamps, ages, and cooldown status
- `list-overrides` shows per-package cooldown values
- Age calculated from bootstrap start time, not wall-clock time during
  resolution

## Non-goals

- **Provider-specific flags** (`--pypi-min-age`, `--github-min-age`).
  The provider a package uses (PyPI, GitHub, GitLab) reflects *how* it
  is obtained, not how trusted it is. Most GitHub/GitLab packages are
  there because of broken PyPI sdists or midstream forks. Separate
  flags per provider would create a confusing configuration matrix and
  cannot coexist cleanly with a global model. This proposal uses one
  global default plus per-package overrides.
- **SSH transport** for git timestamp retrieval.

### Cooldown and `==` pins in constraints

Constraints files (e.g., `constraints.txt`) commonly hard-pin
critical packages with `==` (`torch==2.7.0`, `triton==3.2.0`).
When a pin is bumped to a just-released version that falls within
the cooldown window, the build fails. This creates friction because
every pin bump potentially requires a per-package
`min_release_age: 0` override.

The constraints file is a curated, human-reviewed artifact — it
represents deliberate operator intent, which is a different trust
signal than automatic version resolution. The cooldown is most
valuable for automatically-resolved transitive dependencies where
no human chose the version.

Constraints files use mixed specifiers: `==` for exact pins,
`~=`/`>=`/`<` for ranges. Only `==` represents a truly deliberate
version choice. Range specifiers still involve automatic resolution
within a range, so cooldown should apply to those. The `==` vs
range distinction maps naturally to "human-chosen" vs
"automatically-resolved."

#### Preferred option

**Auto-exempt `==` pins in constraints.**
If a candidate version matches a `==` pin from the constraints
file, skip the cooldown check automatically.

- Pro: zero friction — no YAML overrides needed when bumping pins
- Pro: precisely targets the friction — only exact pins are
  exempt, range specifiers still get cooldown enforcement
- Con: weakens the security model if a compromised version is
  pinned before the cooldown window reveals the compromise

#### Alternative options

If the preferred option is not acceptable:

- **CLI flag to exempt constrained versions**: a
  `--trust-pinned-constraints` flag (default off) that skips
  cooldown for `==` pins — explicit opt-in but one more flag to
  manage
- **Warn-only for pins**: log a warning instead of failing when a
  `==` pin hits cooldown — non-blocking but advisory only
- **Hash-based trust**: skip cooldown when the constraint includes
  `--hash=sha256:...`, since artifact integrity is cryptographically
  verified (stronger than age-based trust, but requires workflow
  change)

## How

### Configuration

#### CLI and environment variable

```python
@click.option(
    "--min-release-age",
    type=click.IntRange(min=0),
    default=0,
    envvar="FROMAGER_MIN_RELEASE_AGE",
    help="Minimum days a release must be public before use (0 = no cooldown)",
)
```

The value is stored on `WorkContext` with a `start_time` captured once
at construction (UTC). A fixed start time ensures consistent results
when the same package is resolved multiple times during a build.

#### Per-package overrides

A new field in `ResolverDist`:

```yaml
# Trusted internal package -- bypass cooldown
resolver_dist:
  min_release_age: 0

# Extra scrutiny -- 2-week cooldown
resolver_dist:
  min_release_age: 14
```

Semantics:

- `None` (default) -- use the global `--min-release-age`
- `0` -- no cooldown for this package
- Positive integer -- override the global value

`PackageBuildInfo.resolver_min_release_age(global_default)` resolves
the effective value.

### Enforcement

The check lives in `BaseProvider.validate_candidate()`, inherited by
every provider:

```text
validate_candidate()
  1. [existing] Reject known bad versions (incompatibilities)
  2. [new]      Cooldown check
                  if provider supports timestamps:
                    upload_time unknown → reject (fail-closed)
                    age < min_release_age → reject
                  if provider does not support timestamps:
                    if per-package override set → reject (fail-closed)
                    otherwise → skip with warning
  3. [existing] Accept if any requirement's specifier and
               constraints are satisfied (is_satisfied_by)
```

Each provider declares whether it supports timestamps via a
class-level `supports_upload_time` flag. Providers that can supply
timestamps (`PyPIProvider`, `GitLabTagProvider`) fail-closed when a
candidate is missing one. Providers that cannot
(`GitHubTagProvider`, `GenericProvider`, `VersionMapProvider`) skip
cooldown with a warning -- unless the operator explicitly sets a
per-package `min_release_age`, in which case fail-closed applies.

`resolver.resolve()` sets `min_release_age_days` and `start_time`
on the provider after creation, so cooldown applies to all
providers including plugin-returned ones. No plugin changes needed.

#### Error messages

When cooldown blocks all candidates, error messages state the
reason clearly so users are not confused by a generic "no match":

- "found N candidate(s) for X but all were published within the last
  M days (cooldown policy)"
- "found N candidate(s) for X but none have upload timestamp metadata;
  cannot enforce the M-day cooldown"

### Timestamp availability

| Provider | `supports_upload_time` | Source |
| -- | -- | -- |
| PyPIProvider | Yes | `upload-time` (PEP 691 JSON API) |
| GitLabTagProvider | Yes | `created_at` (tag or commit) |
| GitHubTagProvider | No | Needs Phase 3 |
| GenericProvider | No | Callback-dependent |
| VersionMapProvider | No | N/A |

Custom providers default to `supports_upload_time = False`. Plugin
authors that populate `upload_time` on candidates should set the
flag to `True` on their provider subclass.

#### PyPI sdists (primary use case)

Most packages resolve through `PyPIProvider`, making PyPI sdists the
largest attack surface and the easiest to protect.

PyPI's PEP 691 JSON API provides `upload-time` per distribution
file, not per version. Each sdist and wheel has its own timestamp.
Fromager already reads this field via the `pypi_simple` library and
stores it on `Candidate.upload_time` -- no extra API calls needed.

When `sdist_server_url` points to a non-PyPI simple index (e.g., a
corporate mirror), `upload-time` may be absent. Fail-closed applies;
use `min_release_age: 0` for packages from indices without timestamps.

#### GitHub timestamps (Phase 3)

The GitHub tags list API does not return dates.
`GitHubTagProvider` sets `supports_upload_time = False`, so it
skips cooldown with a warning until Phase 3 adds timestamp
support via the Releases API and commit date fallback.

### Exempt sources

#### Pre-built wheels

Pre-built wheels are served from curated indices and use a different
trust model. `resolve_prebuilt_wheel()` passes
`min_release_age_days=0` to the provider, bypassing the cooldown.

#### Direct git clone URLs

Requirements with explicit git URLs (`pkg @ git+https://...@tag`)
bypass all resolver providers entirely. No `Candidate` object is
created and `validate_candidate()` never runs, so there is no
insertion point for a cooldown check.

These are also exempt by design:

- Only allowed for top-level requirements, not transitive deps
- The user explicitly specifies the URL and ref -- this is a
  deliberate pin, not automatic version selection
- Git timestamps (author date, committer date) are set by the
  client, not the server, so they cannot be trusted for cooldown
  enforcement the way PyPI's server-side `upload-time` can

### Command updates

**`list-versions`**:

- Shows `upload_time` and age (days) for each candidate
- Marks candidates blocked by cooldown
- `--ignore-per-package-overrides` shows what cooldown would hide

**`list-overrides`** (with `--details`):

- New column for per-package `min_release_age`

## Implementation phases

### Phase 1 -- Core (single PR)

- `WorkContext`: `min_release_age_days`, `start_time`
- CLI: `--min-release-age` / `FROMAGER_MIN_RELEASE_AGE`
- `ResolverDist.min_release_age` field
- `PackageBuildInfo.resolver_min_release_age()` method
- `BaseProvider.validate_candidate()` cooldown check
- `BaseProvider.supports_upload_time` class flag
- `resolver.resolve()`: set cooldown on provider after creation
- `default_resolver_provider()`: per-package lookup
- Pre-built wheel exemption
- Unit tests

PyPI sdists and GitLab-sourced packages work immediately after this
phase (timestamps already available). GitHub-sourced packages require
Phase 3.

### Phase 2 -- Commands (follow-up PR)

- `list-versions` enhancements
- `list-overrides` enhancements

### Phase 3 -- GitHub timestamps (follow-up PR)

- Releases API + commit fallback in `GitHubTagProvider`

**Migration note**: Until Phase 3 ships, GitHub-sourced packages
skip cooldown with a warning (since `GitHubTagProvider` has
`supports_upload_time = False`). No manual `min_release_age: 0`
overrides are needed. Phase 3 enables cooldown enforcement for
these packages by adding timestamp support.

## Examples

```bash
# 7-day cooldown
fromager --min-release-age 7 bootstrap -r requirements.txt

# Same, via environment variable
FROMAGER_MIN_RELEASE_AGE=7 fromager bootstrap -r requirements.txt

# No cooldown (default)
fromager bootstrap -r requirements.txt

# Inspect available versions under a 7-day cooldown
fromager --min-release-age 7 package list-versions torch
```

```yaml
# overrides/settings/internal-package.yaml
resolver_dist:
  min_release_age: 0    # trusted, no cooldown

# overrides/settings/risky-dep.yaml
resolver_dist:
  min_release_age: 14   # 2-week cooldown
```
