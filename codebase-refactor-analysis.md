# Fromager Codebase Refactor Analysis

A structural review of all 62 Python files in `src/fromager/` (~15,000 lines of code), examining code duplication, abstraction gaps, correctness risks, cache safety, and code clarity.

**Date:** 2026-04-05

## How to Read This Document

Each finding is rated by severity:

| Severity | Meaning |
|----------|---------|
| **Critical** | Can cause incorrect behavior, data corruption, or crashes in production. Fix promptly. |
| **Warning** | Makes the code harder to maintain or extend. Fix during regular development. |
| **Note** | Minor style or duplication issue. Fix only if you're already working in that area. |

## Executive Summary

The codebase is architecturally sound: good separation of concerns, proper use of dataclasses, no bare `except:` clauses, effective use of `contextvars` for thread-safe logging, and a well-designed plugin/override system. No API or plugin compatibility issues were found.

The 16 findings break down as follows:

| Category | Critical | Warning | Note | Total |
|----------|----------|---------|------|-------|
| Code duplication (DRY) | 1 | 2 | 3 | 6 |
| Correctness | 1 | 1 | 0 | 2 |
| Cache/state safety | 1 | 1 | 0 | 2 |
| Code clarity | 0 | 4 | 2 | 6 |
| **Total** | **3** | **8** | **5** | **16** |

## Priority Roadmap

Fix these in order. Each item lists estimated effort and any dependencies.

| Priority | Finding | What to do | Effort |
|----------|---------|------------|--------|
| 1 | [#2 Uninitialized variable](#2-json-parse-failure-in-get_distributions-crashes-with-wrong-error) | Add `raise` after logging | Small (1 line) |
| 2 | [#1 Thread-unsafe cache](#1-parallel-builds-can-corrupt-the-resolver-cache) | Add a thread lock around `resolver_cache` | Small (~10 lines) |
| 3 | [#10 Silent network errors](#10-network-errors-during-wheel-cache-lookup-are-silently-ignored) | Catch specific exceptions, log at WARNING | Small (~10 lines) |
| 4 | [#3 Dependency function duplication](#3-three-near-identical-dependency-fetching-functions) | Extract shared helper | Medium (~50 lines changed) |
| 5 | [#4 test_mode pattern](#4-repeated-test_mode-error-handling-pattern) | Extract context manager | Medium (~40 lines changed) |
| 6 | [#6 Missing @abstractmethod](#6-baseprovider-lacks-abstractmethod-enforcement) | Add decorators | Small (~5 lines) |
| 7 | [#7 Long functions](#7-five-functions-exceed-100-lines-with-mixed-responsibilities) | Break into sub-functions | Large (per function) |
| 8+ | Remaining warnings and notes | Address during ongoing maintenance | Varies |

---

## Critical Findings

### 1. Parallel builds can corrupt the resolver cache

**Impact:** Concurrent threads can overwrite each other's candidate lists, producing silently wrong dependency resolution during parallel builds.

**Location:**
- `src/fromager/resolver.py:425` — class-level shared cache
- `src/fromager/resolver.py:555-565` — returns mutable reference from cache
- `src/fromager/resolver.py:582` — mutates that reference in-place

**Mechanism:** `BaseProvider.resolver_cache` is a class-level `dict` with no thread lock. The method `_get_cached_candidates()` returns a direct reference to the cached list, and callers overwrite its contents in-place (`cached_candidates[:] = candidates`). The parallel build path in `commands/build.py:645` uses `ThreadPoolExecutor`, and each thread creates provider instances that share this same cache. If two threads resolve the same package simultaneously, one thread's writes can clobber the other's.

**Fix:** Wrap cache access with `threading.Lock` (the codebase already provides `threading_utils.with_thread_lock()`), or return defensive copies from `_get_cached_candidates()`.

---

### 2. JSON parse failure in `get_distributions()` crashes with wrong error

**Impact:** A recoverable JSON parse error turns into a confusing `UnboundLocalError`, hiding the real problem.

**Location:** `src/fromager/build_environment.py:232-244`

**Mechanism:** If `json.loads(result.strip())` raises, the `except` block logs the error but does not return, re-raise, or assign a fallback to `mapping`. Execution falls through to line 244, which references the never-assigned `mapping` variable.

**Fix:** Re-raise after logging:

```python
except Exception:
    logger.exception("failed to de-serialize JSON: %s", result)
    raise
```

---

### 3. Three near-identical dependency-fetching functions

**Impact:** ~180 lines of duplicated logic. Any change to the dependency-fetching flow must be replicated in three places, risking drift.

**Location:**
- `src/fromager/dependencies.py:40-73` — `get_build_system_dependencies()`
- `src/fromager/dependencies.py:107-150` — `get_build_backend_dependencies()`
- `src/fromager/dependencies.py:179-222` — `get_build_sdist_dependencies()`

**Mechanism:** All three follow the same steps: log, check for a cached requirements file, call `overrides.find_and_invoke()` with a method name, filter, write, return. The only differences are the filename constant, the override method name, and whether `build_env`/`extra_environ` are passed.

**Fix:** Extract a shared helper:

```python
def _get_dependencies(
    *,
    ctx, req, version, sdist_root_dir,
    req_file_name, override_method, default_handler,
    build_env=None, extra_environ=None,
) -> set[Requirement]:
    # ~25 lines instead of 3 x ~35 lines
```

**Note:** The three `default_handler` functions have different signatures (the system handler takes 4 parameters, the others take 6), so the helper should forward `**kwargs` to `overrides.find_and_invoke()`.

---

## Warning Findings

### 4. Repeated test_mode error handling pattern

**Impact:** The same 4-line error handling block appears 5 times. Changing test_mode semantics requires updating all 5 locations.

**Location:** `src/fromager/bootstrapper.py` — lines 166-170, 262-266, 300-303, 387-390, 405-408

**The repeated pattern:**

```python
except Exception as err:
    if not self.test_mode:
        raise
    self._record_test_mode_failure(req, ..., err, "...")
```

**Fix:** Extract a context manager:

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

---

### 5. `get_all_patches()` exposes its internal cache to callers

**Impact:** Any caller that modifies the returned dict or its lists will silently corrupt the cached state for all future callers. No callers do this today, but the API doesn't prevent it.

**Location:** `src/fromager/packagesettings/_pbi.py:114-142`

**Fix:** Return `dict(self._patches)` (shallow copy), or store patch lists as `tuple` instead of `list` to make accidental mutation impossible.

---

### 6. BaseProvider lacks `@abstractmethod` enforcement

**Impact:** An incomplete provider subclass can be instantiated without error. The bug only surfaces when the missing method is called at runtime, producing a confusing `NotImplementedError` instead of a clear instantiation failure.

**Location:** `src/fromager/resolver.py:424` — `cache_key` (line 441) and `find_candidates()` (line 459)

**Mechanism:** These methods use `raise NotImplementedError()` instead of `@abstractmethod`. Since `BaseProvider` already inherits from the ABC chain (`ExtrasProvider` -> `BaseAbstractProvider`), the `@abstractmethod` machinery is available at no cost.

**Fix:** Add `@abstractmethod` to `cache_key` and `find_candidates()`.

---

### 7. Five functions exceed 100 lines with mixed responsibilities

**Impact:** These functions are hard to test in isolation, hard to review, and easy to introduce bugs into.

**Functions:**

| Function | File | Lines | Key responsibilities mixed together |
|----------|------|-------|--------------------------------------|
| `write_constraints_file()` | `commands/bootstrap.py:210` | 259 | Conflict detection, formatting, I/O |
| `get_project_from_pypi()` | `resolver.py:217` | 207 | See detailed breakdown below |
| `_build()` | `commands/build.py:322` | 153 | Validation, environment setup, build execution |
| `_bootstrap_impl()` | `bootstrapper.py:307` | 136 | Dependency resolution, env setup, build |
| `build_parallel()` | `commands/build.py:589` | 122 | Graph traversal, thread management, progress |

**`get_project_from_pypi()` detailed breakdown:** This single function handles 12 responsibilities that fall into four groups:

- **Fetching:** HTTP client setup, index page retrieval
- **Filtering:** yanked release checks (PEP 592), Python version matching, platform tag matching, package type validation, package status checks (PEP 792)
- **Parsing:** filename validation, version extraction (sdist vs. wheel), URL override resolution, upload time normalization
- **Output:** candidate object construction, diagnostics logging

Each filtering step could be an independently testable function, reducing `get_project_from_pypi()` to ~80 lines of orchestration:

```python
_is_yanked(candidate) -> bool
_matches_python_version(candidate, python_version) -> bool
_matches_platform_tags(candidate, supported_tags, ignore_platform) -> bool
_parse_candidate_version(candidate) -> Version | None
```

**Fix:** Extract coherent sub-steps into well-named private functions. Start with the largest function or the one most frequently modified.

---

### 8. Getter-named functions that perform side effects

**Impact:** Functions named `get_*` conventionally imply pure lookups. These perform network calls, file writes, and state mutations, making the code misleading to read.

**Location:**
- `src/fromager/resolver.py` — `get_project_from_pypi()` makes HTTP requests and invokes plugin hooks
- `src/fromager/bootstrapper.py` — various `get_*` methods modify the dependency graph and write files

**Fix:** Rename to `fetch_`, `resolve_`, or `load_` to signal side effects.

---

### 9. Duplicated cleanup-or-reuse directory pattern

**Impact:** The same "check if directory exists, decide whether to delete or reuse" logic is written three times.

**Location:**
- `src/fromager/sources.py:392-398`
- `src/fromager/context.py:201-207`
- `src/fromager/context.py:209-215`

**Fix:** Extract a utility:

```python
def cleanup_or_reuse(path: Path, cleanup: bool) -> bool:
    """Remove or reuse existing directory. Returns True if reused."""
```

---

### 10. Network errors during wheel cache lookup are silently ignored

**Impact:** A misconfigured cache server URL or transient network failure silently falls through to building from source — an expensive operation that hides the real problem. Operators have no signal to distinguish infrastructure issues from genuinely missing wheels.

**Location:** `src/fromager/bootstrapper.py:1054-1058`

**Mechanism:** A bare `except Exception` catches everything — network timeouts, DNS failures, authentication errors — and treats them all the same as "wheel not found."

**Fix:** Catch specific exceptions (e.g., `requests.ConnectionError`, `requests.Timeout`) separately from "not found" results. Log network failures at WARNING level.

---

### 11. Inconsistent `get_resolver_provider` override calls across modules

**Impact:** Five call sites invoke the same override with different keyword argument sets, making it unclear which parameters are required and risking subtle behavioral differences.

**Location:**
- `src/fromager/resolver.py:94`
- `src/fromager/sources.py:143`
- `src/fromager/wheels.py:502`
- `src/fromager/commands/package.py:146, 236`
- `src/fromager/commands/find_updates.py:196`

**Mechanism:** Some calls pass `req_type` and `ignore_platform`, others omit them. The `commands/` callers also vary in whether they pass `ctx` as `wkctx` or `ctx`.

**Fix:** Create a single wrapper function with explicit parameter defaults:

```python
def resolve_provider(
    *, ctx, req, include_sdists, include_wheels,
    sdist_server_url, req_type=None, ignore_platform=False,
) -> BaseProvider:
    return overrides.find_and_invoke(
        req.name, "get_resolver_provider",
        default_resolver_provider, ...
    )
```

---

## Note Findings

### 12. Repeated `Path.mkdir()` patterns

10+ locations call `path.mkdir(parents=True, exist_ok=True)` with optional debug logging. The pattern is simple enough that extraction may not improve readability. Fix only if the pattern grows to include additional logic.

**Location:** `sources.py`, `wheels.py`, `context.py`, `bootstrapper.py`

---

### 13. Repeated download/validation wrapper patterns

Two functions wrap `download_url()` then validate the file format with identical structure.

**Location:**
- `src/fromager/sources.py:257-288` — `_download_source_check()`
- `src/fromager/wheels.py:455-465` — `_download_wheel_check()`

**Fix:** Consider a generic `_download_and_validate(url, validator)` if more download types are added.

---

### 14. Heavy parameter passing through function chains

The tuple `(ctx, req, version, sdist_root_dir, build_env, extra_environ)` is threaded through many function chains, creating coupling and making signature changes painful.

**Location:** `dependencies.py`, `sources.py`, `wheels.py`, `bootstrapper.py`

**Fix:** Bundle into a `BuildContext` dataclass. This is a significant refactor — weigh against disruption.

---

### 15. Repeated cache hit/miss logging pattern

Multiple functions repeat: check if file exists, log "already have" or "need to download", return or proceed. Each instance has slightly different log context, so extraction may not improve clarity.

**Location:** `src/fromager/sources.py`, `src/fromager/wheels.py`

---

### 16. Boolean flag parameters controlling behavior

Multiple boolean parameters create combinatorial complexity. Some functions take 2-3 booleans that interact with each other.

**Location:**
- `src/fromager/bootstrapper.py` — `test_mode`, `sdist_only`
- `src/fromager/commands/build.py` — `parallel`, `skip_prebuilt`
- `src/fromager/dependency_graph.py` — `pre_built`, `validate_cache`

**Fix:** For related flags (e.g., `sdist_only` + `test_mode`), consider an `Enum` or config dataclass. Single flags on well-scoped functions are fine as-is.

---

## Cross-Reference by File

Use this table to find all findings relevant to a specific file.

| File | Findings |
|------|----------|
| `resolver.py` | #1, #6, #7, #8, #11 |
| `bootstrapper.py` | #4, #7, #8, #10, #14, #16 |
| `build_environment.py` | #2 |
| `dependencies.py` | #3 |
| `commands/build.py` | #7, #16 |
| `commands/bootstrap.py` | #7 |
| `sources.py` | #9, #11, #12, #13, #15 |
| `wheels.py` | #11, #12, #13, #15 |
| `context.py` | #9 |
| `packagesettings/_pbi.py` | #5 |
| `commands/package.py` | #11 |
| `commands/find_updates.py` | #11 |

## Out of Scope

The following areas were examined and found clean:

- **API/Plugin compatibility:** The override system (`overrides.find_and_invoke()`) correctly filters unsupported kwargs and maintains backward compatibility with plugin modules.
- **Parallel build error handling:** The `build_parallel()` function properly re-raises thread exceptions via `future.result()`, wraps them in `RuntimeError` with package context, and guarantees cleanup via `finally` blocks.
