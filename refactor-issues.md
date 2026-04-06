# Fromager Refactor: GitHub Issues

Derived from the codebase refactor analysis. Issues are ordered by priority.

---

## Issue 1: `get_distributions()` crashes with `UnboundLocalError` on JSON parse failure

**Labels:** `bug`, `severity/critical`

### Description

In `build_environment.py:232-244`, if `json.loads(result.strip())` raises an exception, the `except` block logs the error but doesn't return, re-raise, or assign a fallback. Execution falls through to reference the never-assigned `mapping` variable, producing a confusing `UnboundLocalError` that hides the real JSON parsing problem.

### Suggested fix

Re-raise after logging:

```python
except Exception:
    logger.exception("failed to de-serialize JSON: %s", result)
    raise
```

### How to test

Mock `BuildEnvironment.run()` to return invalid JSON, call `get_distributions()`, and assert `json.JSONDecodeError` is raised. Currently raises `UnboundLocalError`.

**Effort:** Small (1 line)

---

## Issue 2: Parallel builds can corrupt the shared resolver cache

**Labels:** `bug`, `severity/critical`, `concurrency`

### Description

`BaseProvider.resolver_cache` (`resolver.py:425`) is a class-level `dict` with no thread safety. `_get_cached_candidates()` returns a direct reference to the cached list, and callers mutate it in-place (`cached_candidates[:] = candidates`). When `build_parallel()` uses `ThreadPoolExecutor`, threads sharing this cache can silently clobber each other's candidate lists, producing wrong dependency resolution.

### Suggested fix

Wrap cache reads/writes with `threading.Lock` (the codebase already has `threading_utils.with_thread_lock()`), or return defensive copies from `_get_cached_candidates()`.

### How to test

- **Defensive copy:** Call `_get_cached_candidates()`, mutate the returned list, call again — assert the mutation didn't leak back.
- **Thread safety:** Stub `find_candidates()` with a brief sleep, launch 4 threads via a `Barrier`, assert `find_candidates()` was called exactly once. Currently all 4 threads bypass the cache.

**Effort:** Small (~10 lines)

---

## Issue 3: Network errors during wheel cache lookup are silently swallowed

**Labels:** `bug`, `severity/critical`

### Description

In `bootstrapper.py:1054-1058`, a bare `except Exception` catches all errors during wheel cache lookup — including network timeouts, DNS failures, and auth errors — and treats them identically to `wheel not found`. This silently falls through to building from source, an expensive operation that hides infrastructure problems from operators.

### Suggested fix

Catch specific exceptions (`requests.ConnectionError`, `requests.Timeout`) separately. Log network failures at `WARNING` level so operators can distinguish infra issues from genuinely missing wheels.

### How to test

Patch `resolver.resolve` to raise `ConnectionError`, `Timeout`, or `HTTPError`. Call `_download_wheel_from_cache()` and assert each propagates. Currently all are swallowed, returning `(None, None)`.

**Effort:** Small (~10 lines)

---

## Issue 4: Extract shared helper for three near-identical dependency-fetching functions

**Labels:** `refactor`, `severity/warning`, `DRY`

### Description

Three functions in `dependencies.py` follow the same ~35-line pattern (log, check cached requirements file, call `overrides.find_and_invoke()`, filter, write, return) with only the filename constant, override method name, and optional params differing:

- `get_build_system_dependencies()` (lines 40-73)
- `get_build_backend_dependencies()` (lines 107-150)
- `get_build_sdist_dependencies()` (lines 179-222)

This is ~180 lines of duplicated logic. Any change to the flow must be replicated three times.

### Suggested fix

Extract a `_get_dependencies()` helper that accepts the varying parts as parameters. Use `**kwargs` forwarding for the differing handler signatures.

### How to test

Assert `_get_dependencies` exists on the module. Use `inspect.getsource()` on each public function and assert it contains `"_get_dependencies"` — proving delegation instead of duplication.

**Effort:** Medium (~50 lines changed)

---

## Issue 5: Extract context manager for repeated `test_mode` error handling

**Labels:** `refactor`, `severity/warning`, `DRY`

### Description

The same 4-line error handling block appears 5 times in `bootstrapper.py` (lines 166, 262, 300, 387, 405):

```python
except Exception as err:
    if not self.test_mode:
        raise
    self._record_test_mode_failure(req, ..., err, "...")
```

Changing test_mode semantics requires updating all 5 locations.

### Suggested fix

Extract a `_test_mode_guard()` context manager:

```python
@contextlib.contextmanager
def _test_mode_guard(self, req, version, failure_type):
    try:
        yield
    except Exception as err:
        if not self.test_mode:
            raise
        self._record_test_mode_failure(req, version, err, failure_type)
```

### How to test

- Assert `_test_mode_guard` exists on `Bootstrapper`.
- In test mode: raise inside the guard, assert it's caught and recorded in `failed_packages`.
- In normal mode: raise inside the guard, assert it re-raises.
- Check `inspect.getsource(_bootstrap_impl)` contains `"_test_mode_guard"` to confirm inline blocks were replaced.

**Effort:** Medium (~40 lines changed)

---

## Issue 6: `BaseProvider` lacks `@abstractmethod` enforcement

**Labels:** `refactor`, `severity/warning`

### Description

In `resolver.py:424`, `cache_key` and `find_candidates()` use `raise NotImplementedError()` instead of `@abstractmethod`. Since `BaseProvider` already inherits from the ABC chain, incomplete subclasses can be instantiated without error — the bug only surfaces at runtime when the missing method is called.

### Suggested fix

Add `@abstractmethod` decorators to `cache_key` and `find_candidates()`.

### How to test

Subclass `BaseProvider` omitting `cache_key`, try to instantiate — assert `TypeError`. Repeat omitting `find_candidates`. Currently both instantiate fine; the error only appears at call time.

**Effort:** Small (~5 lines)

---

## Issue 7: Break up five 100+ line functions with mixed responsibilities

**Labels:** `refactor`, `severity/warning`, `maintainability`

### Description

These functions are hard to test in isolation and easy to introduce bugs into:

| Function | File | Lines |
|----------|------|-------|
| `write_constraints_file()` | `commands/bootstrap.py:210` | 259 |
| `get_project_from_pypi()` | `resolver.py:217` | 207 |
| `_build()` | `commands/build.py:322` | 153 |
| `_bootstrap_impl()` | `bootstrapper.py:307` | 136 |
| `build_parallel()` | `commands/build.py:589` | 122 |

`get_project_from_pypi()` is the worst offender with 12 responsibilities across fetching, filtering, parsing, and output.

### Suggested fix

Extract coherent sub-steps into private functions. For `get_project_from_pypi()`, start with:
- `_is_yanked(candidate) -> bool`
- `_matches_python_version(candidate, python_version) -> bool`
- `_matches_platform_tags(candidate, supported_tags, ignore_platform) -> bool`
- `_parse_candidate_version(candidate) -> Version | None`

### How to test

Use `inspect.getsource()` to count lines per function and assert each is under a threshold (e.g. 80 lines). Assert extracted helpers exist by name (e.g. `_is_yanked`, `_matches_python_version`).

**Effort:** Large (per function)

---

## Issue 8: Rename getter-named functions that perform side effects

**Labels:** `refactor`, `severity/warning`, `naming`

### Description

Functions named `get_*` conventionally imply pure lookups. Several perform network calls, file writes, and state mutations:

- `resolver.py` — `get_project_from_pypi()` makes HTTP requests and invokes plugin hooks
- `bootstrapper.py` — various `get_*` methods modify the dependency graph and write files

### Suggested fix

Rename to `fetch_`, `resolve_`, or `load_` to signal side effects.

### How to test

Assert the old name (`get_project_from_pypi`) no longer exists on the module and the new name (e.g. `fetch_project_from_pypi`) does.

**Effort:** Medium (rename + update all call sites)

---

## Issue 9: Inconsistent `get_resolver_provider` override calls across modules

**Labels:** `refactor`, `severity/warning`, `consistency`

### Description

Five call sites invoke the same `get_resolver_provider` override with different keyword argument sets, making it unclear which parameters are required:

- `resolver.py:94`
- `sources.py:143`
- `wheels.py:502`
- `commands/package.py:146, 236`
- `commands/find_updates.py:196`

Some pass `req_type` and `ignore_platform`, others omit them. The `commands/` callers also vary in whether they pass `ctx` as `wkctx` or `ctx`.

### Suggested fix

Create a single `resolve_provider()` wrapper with explicit parameter defaults to enforce consistency.

### How to test

- Assert a `resolve_provider()` wrapper exists on the resolver module.
- Use `ast.parse()` on each call-site file to extract kwargs passed with `"get_resolver_provider"` and assert all sites use the same set. Currently they differ.

**Effort:** Small-Medium

---

## Issue 10: `get_all_patches()` exposes internal cache to callers

**Labels:** `refactor`, `severity/warning`, `defensive-coding`

### Description

`packagesettings/_pbi.py:114-142` returns a direct reference to its internal `_patches` dict. Any caller that modifies the returned dict or its lists will silently corrupt the cached state for all future callers. No callers do this today, but the API doesn't prevent it.

### Suggested fix

Return `dict(self._patches)` (shallow copy), or store patch lists as `tuple` instead of `list`.

### How to test

Call `get_all_patches()`, mutate the returned dict (add a key) or its lists (append an item), call again — assert the mutations are absent. Currently fails because the internal `_patches` dict is returned directly.

**Effort:** Small

---

## Issue 11: Deduplicate cleanup-or-reuse directory pattern

**Labels:** `refactor`, `severity/warning`, `DRY`

### Description

The same "check if directory exists, decide whether to delete or reuse" logic is written three times:

- `sources.py:392-398`
- `context.py:201-207`
- `context.py:209-215`

### Suggested fix

Extract a utility:

```python
def cleanup_or_reuse(path: Path, cleanup: bool) -> bool:
    """Remove or reuse existing directory. Returns True if reused."""
```

### How to test

Assert the utility exists via `hasattr()`. Test behavior: `cleanup=True` removes the directory, `cleanup=False` keeps it and returns `True`. Currently fails because no such function exists.

**Effort:** Small

---

## Issue 12 (Note): Consider `BuildContext` dataclass for heavy parameter passing

**Labels:** `refactor`, `severity/note`

### Description

The tuple `(ctx, req, version, sdist_root_dir, build_env, extra_environ)` is threaded through many function chains in `dependencies.py`, `sources.py`, `wheels.py`, and `bootstrapper.py`. This creates coupling and makes signature changes painful.

### Suggested fix

Bundle into a `BuildContext` dataclass. This is a significant refactor — weigh against disruption before proceeding.

### How to test

Assert a `BuildContext` dataclass exists with the expected fields. Use `inspect.signature()` on consuming functions to verify they accept it as a parameter.

**Effort:** Large

---

## Issue 13 (Note): Deduplicate download/validation wrapper pattern

**Labels:** `refactor`, `severity/note`

### Description

`sources.py:257-288` (`_download_source_check()`) and `wheels.py:455-465` (`_download_wheel_check()`) wrap `download_url()` then validate the file format with identical structure.

### Suggested fix

Consider a generic `_download_and_validate(url, validator)` if more download types are added.

### How to test

Assert `_download_and_validate` exists. Check `inspect.getsource()` of both `_download_source_check` and `_download_wheel_check` to confirm they delegate to it.

**Effort:** Small

---

## Issue 14 (Note): Consider enums for related boolean flag parameters

**Labels:** `refactor`, `severity/note`

### Description

Multiple boolean parameters create combinatorial complexity in:

- `bootstrapper.py` — `test_mode`, `sdist_only`
- `commands/build.py` — `parallel`, `skip_prebuilt`
- `dependency_graph.py` — `pre_built`, `validate_cache`

### Suggested fix

For related flags (e.g., `sdist_only` + `test_mode`), consider an `Enum` or config dataclass. Single flags on well-scoped functions are fine as-is.

### How to test

Assert the enum/dataclass exists (e.g. `BuildMode`). Verify `Bootstrapper.__init__` accepts it instead of separate booleans via `inspect.signature()`. Assert invalid combinations raise at construction via the type, not an ad-hoc `if` check.

**Effort:** Medium
