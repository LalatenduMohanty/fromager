"""Tests for expected (fixed) behavior of issues from refactor-issues.md.

These tests assert CORRECT behavior. They FAIL against the current code
and will PASS once the fixes are applied.
"""

import ast
import inspect
import json
import pathlib
import threading
import time
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests
import resolvelib.resolvers
from packaging.requirements import Requirement
from packaging.version import Version

from fromager import bootstrapper, build_environment, dependencies, resolver, sources
from fromager.candidate import Candidate
from fromager.commands import bootstrap as cmd_bootstrap
from fromager.commands import build as cmd_build
from fromager.context import WorkContext

# ---------------------------------------------------------------------------
# Issue 1: get_distributions() crashes with UnboundLocalError on JSON parse failure
#
# Current bug: json.loads() failure leaves `mapping` unassigned, causing
# UnboundLocalError.  Fixed behavior: the original exception propagates.
# ---------------------------------------------------------------------------


class TestIssue1GetDistributionsJsonFailure:
    """get_distributions() should propagate the JSON parse error, not crash
    with an UnboundLocalError."""

    def test_invalid_json_raises_json_error(self) -> None:
        """Invalid JSON should raise a JSON-related exception, not UnboundLocalError."""
        mock_env = MagicMock(spec=build_environment.BuildEnvironment)
        mock_env.python = "/usr/bin/python3"
        mock_env.run.return_value = "this is not valid json"

        with pytest.raises(json.JSONDecodeError):
            build_environment.BuildEnvironment.get_distributions(mock_env)

    def test_empty_output_raises_json_error(self) -> None:
        """Empty output should raise a JSON-related exception, not UnboundLocalError."""
        mock_env = MagicMock(spec=build_environment.BuildEnvironment)
        mock_env.python = "/usr/bin/python3"
        mock_env.run.return_value = ""

        with pytest.raises(json.JSONDecodeError):
            build_environment.BuildEnvironment.get_distributions(mock_env)

    def test_valid_json_still_works(self) -> None:
        """Sanity check: valid JSON continues to work correctly."""
        mock_env = MagicMock(spec=build_environment.BuildEnvironment)
        mock_env.python = "/usr/bin/python3"
        mock_env.run.return_value = json.dumps({"setuptools": "69.5.1", "pip": "24.0"})

        result = build_environment.BuildEnvironment.get_distributions(mock_env)
        assert result == {"pip": Version("24.0"), "setuptools": Version("69.5.1")}


# ---------------------------------------------------------------------------
# Issue 2: Parallel builds can corrupt the shared resolver cache
#
# Current bug: _get_cached_candidates() returns a direct mutable reference
# to the internal cache list.  Fixed behavior: return a defensive copy so
# callers cannot corrupt shared state.
# ---------------------------------------------------------------------------


class TestIssue2ResolverCacheDefensiveCopy:
    """_get_cached_candidates() should return a copy, not a direct reference
    to the internal cache list."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self) -> None:
        resolver.BaseProvider.clear_cache()

    def test_cache_returns_defensive_copy(self) -> None:
        """Mutating the returned list must NOT affect the internal cache."""

        class StubProvider(resolver.BaseProvider):
            provider_description = "stub"

            @property
            def cache_key(self) -> str:
                return "stub-key"

            def find_candidates(self, identifier: str) -> list:
                return []

        provider = StubProvider()
        cache_list = provider._get_cached_candidates("pkg-a")

        # Mutating the returned list should NOT affect the cache
        sentinel = Candidate(
            name="injected",
            version=Version("0.0.0"),
            url="https://example.com/fake.whl",
        )
        cache_list.append(sentinel)

        cache_list_again = provider._get_cached_candidates("pkg-a")
        assert sentinel not in cache_list_again, (
            "_get_cached_candidates should return a defensive copy, "
            "not a direct reference to the internal cache"
        )

    def test_concurrent_cache_access_is_safe(self) -> None:
        """Concurrent threads resolving the same identifier should not
        produce corrupted results.

        The bug: _find_cached_candidates() does a check-then-act on a shared
        list with no locking.  Thread A sees the list empty, calls
        find_candidates(), then does ``cached_candidates[:] = candidates``.
        Thread B does the same concurrently and overwrites the list with its
        own (potentially different) candidates.  We force this interleaving
        by inserting a delay inside find_candidates() so all threads enter
        the unprotected window together.
        """
        call_count = 0
        call_count_lock = threading.Lock()

        class SlowProvider(resolver.BaseProvider):
            provider_description = "slow"

            @property
            def cache_key(self) -> str:
                return "slow-key"

            def find_candidates(self, identifier: str) -> list:
                nonlocal call_count
                with call_count_lock:
                    call_count += 1
                # Slow down so all threads overlap in the unprotected window
                time.sleep(0.2)
                return [
                    Candidate(
                        name=identifier,
                        version=Version("1.0"),
                        url="https://example.com/fake.whl",
                    )
                ]

        barrier = threading.Barrier(4)

        def resolve_in_thread(provider: SlowProvider, ident: str) -> None:
            barrier.wait(timeout=5)
            list(provider._find_cached_candidates(ident))

        # All 4 threads share a SINGLE provider instance (same cache_key)
        provider = SlowProvider()
        threads = [
            threading.Thread(
                target=resolve_in_thread,
                args=(provider, "shared-pkg"),
                name=f"resolver-{i}",
            )
            for i in range(4)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # With proper locking or defensive copies, only ONE thread should
        # call find_candidates(); the others should hit the cache.
        # Without locking, all 4 threads see the empty cache and all call
        # find_candidates().
        assert call_count == 1, (
            f"find_candidates() was called {call_count} times; expected 1. "
            "Without thread-safe caching, multiple threads bypass the cache "
            "and redundantly call find_candidates()."
        )


# ---------------------------------------------------------------------------
# Issue 3: Network errors during wheel cache lookup are silently swallowed
#
# Current bug: bare `except Exception` catches network errors and returns
# (None, None).  Fixed behavior: network/infra errors should propagate
# (or at least be distinguishable from "wheel not found").
# ---------------------------------------------------------------------------


class TestIssue3NetworkErrorsMustPropagate:
    """Network and infrastructure errors during wheel cache lookup must not
    be silently swallowed."""

    def test_connection_error_propagates(self, tmp_context: WorkContext) -> None:
        """A ConnectionError should propagate, not be silently swallowed."""
        bt = bootstrapper.Bootstrapper(
            tmp_context, cache_wheel_server_url="https://cache.example.com/simple/"
        )

        with patch.object(
            resolver,
            "resolve",
            side_effect=requests.ConnectionError("DNS failure"),
        ):
            with pytest.raises(requests.ConnectionError):
                bt._download_wheel_from_cache(
                    Requirement("some-package"), Version("1.0.0")
                )

    def test_timeout_error_propagates(self, tmp_context: WorkContext) -> None:
        """A Timeout should propagate, not be silently swallowed."""
        bt = bootstrapper.Bootstrapper(
            tmp_context, cache_wheel_server_url="https://cache.example.com/simple/"
        )

        with patch.object(
            resolver, "resolve", side_effect=requests.Timeout("timed out")
        ):
            with pytest.raises(requests.Timeout):
                bt._download_wheel_from_cache(
                    Requirement("some-package"), Version("1.0.0")
                )

    def test_auth_error_propagates(self, tmp_context: WorkContext) -> None:
        """An HTTP 401/403 error should propagate, not be silently swallowed."""
        bt = bootstrapper.Bootstrapper(
            tmp_context, cache_wheel_server_url="https://cache.example.com/simple/"
        )

        response = Mock()
        response.status_code = 401
        http_error = requests.HTTPError("401 Unauthorized", response=response)

        with patch.object(resolver, "resolve", side_effect=http_error):
            with pytest.raises(requests.HTTPError):
                bt._download_wheel_from_cache(
                    Requirement("some-package"), Version("1.0.0")
                )

    def test_package_not_found_still_returns_none(
        self, tmp_context: WorkContext
    ) -> None:
        """A legitimate 'package not found' error should still return
        (None, None) — the fix must not break the happy path."""
        bt = bootstrapper.Bootstrapper(
            tmp_context, cache_wheel_server_url="https://cache.example.com/simple/"
        )

        with patch.object(
            resolver,
            "resolve",
            side_effect=resolvelib.resolvers.ResolverException(
                "found no match for some-package"
            ),
        ):
            result = bt._download_wheel_from_cache(
                Requirement("some-package"), Version("1.0.0")
            )
            assert result == (None, None), (
                "Genuine 'not found' errors should still return (None, None)"
            )


# ---------------------------------------------------------------------------
# Issue 4: Three near-identical dependency-fetching functions in dependencies.py
#
# Current bug: get_build_system_dependencies, get_build_backend_dependencies,
# and get_build_sdist_dependencies duplicate ~35 lines each with the same
# pattern: log, check cache, invoke override, filter, write, return.
#
# Fixed behavior: a shared _get_dependencies() helper should exist that the
# three public functions delegate to.
# ---------------------------------------------------------------------------


class TestIssue4DependencyFunctionsDRY:
    """The three get_*_dependencies functions should delegate to a shared helper."""

    def test_shared_helper_exists(self) -> None:
        """A shared _get_dependencies() helper should exist in the module."""
        assert hasattr(dependencies, "_get_dependencies"), (
            "dependencies module should have a _get_dependencies() helper "
            "that the three public functions delegate to"
        )

    def test_build_system_deps_delegates_to_helper(self) -> None:
        """get_build_system_dependencies should call _get_dependencies()."""
        source = inspect.getsource(dependencies.get_build_system_dependencies)
        assert "_get_dependencies" in source, (
            "get_build_system_dependencies should delegate to _get_dependencies()"
        )

    def test_build_backend_deps_delegates_to_helper(self) -> None:
        """get_build_backend_dependencies should call _get_dependencies()."""
        source = inspect.getsource(dependencies.get_build_backend_dependencies)
        assert "_get_dependencies" in source, (
            "get_build_backend_dependencies should delegate to _get_dependencies()"
        )

    def test_build_sdist_deps_delegates_to_helper(self) -> None:
        """get_build_sdist_dependencies should call _get_dependencies()."""
        source = inspect.getsource(dependencies.get_build_sdist_dependencies)
        assert "_get_dependencies" in source, (
            "get_build_sdist_dependencies should delegate to _get_dependencies()"
        )


# ---------------------------------------------------------------------------
# Issue 5: Repeated test_mode error handling in bootstrapper.py
#
# Current bug: the same 4-line try/except pattern for test_mode appears 5
# times.  Fixed behavior: a _test_mode_guard() context manager should exist.
# ---------------------------------------------------------------------------


class TestIssue5TestModeGuardContextManager:
    """The test_mode error handling pattern should be extracted into a
    context manager."""

    def test_test_mode_guard_exists(self) -> None:
        """A _test_mode_guard() context manager should exist on Bootstrapper."""
        assert hasattr(bootstrapper.Bootstrapper, "_test_mode_guard"), (
            "Bootstrapper should have a _test_mode_guard() context manager"
        )

    def test_test_mode_guard_is_context_manager(self, tmp_context: WorkContext) -> None:
        """_test_mode_guard should be usable as a context manager."""
        bt = bootstrapper.Bootstrapper(tmp_context, test_mode=True)
        guard = bt._test_mode_guard(Requirement("test"), None, "resolution")  # type: ignore[attr-defined]
        assert hasattr(guard, "__enter__") and hasattr(guard, "__exit__"), (
            "_test_mode_guard should be a context manager"
        )

    def test_test_mode_guard_catches_and_records(
        self, tmp_context: WorkContext
    ) -> None:
        """In test_mode, _test_mode_guard should catch exceptions and record
        them instead of re-raising."""
        bt = bootstrapper.Bootstrapper(tmp_context, test_mode=True)
        with bt._test_mode_guard(Requirement("test-pkg"), "1.0", "bootstrap"):  # type: ignore[attr-defined]
            raise RuntimeError("simulated failure")
        # Should not raise — failure should be recorded
        assert len(bt.failed_packages) == 1
        assert bt.failed_packages[0]["package"] == "test-pkg"

    def test_test_mode_guard_reraises_when_not_test_mode(
        self, tmp_context: WorkContext
    ) -> None:
        """When test_mode is False, _test_mode_guard should re-raise."""
        bt = bootstrapper.Bootstrapper(tmp_context, test_mode=False)
        with pytest.raises(RuntimeError, match="simulated failure"):
            with bt._test_mode_guard(Requirement("test-pkg"), None, "resolution"):  # type: ignore[attr-defined]
                raise RuntimeError("simulated failure")

    def test_bootstrap_uses_guard(self) -> None:
        """bootstrap() method should use _test_mode_guard instead of
        inline try/except blocks."""
        source = inspect.getsource(bootstrapper.Bootstrapper._bootstrap_impl)
        assert "_test_mode_guard" in source, (
            "_bootstrap_impl should use _test_mode_guard() context manager "
            "instead of inline try/except test_mode blocks"
        )


# ---------------------------------------------------------------------------
# Issue 6: BaseProvider lacks @abstractmethod enforcement
#
# Current bug: cache_key and find_candidates() use raise NotImplementedError()
# instead of @abstractmethod, allowing incomplete subclasses to be instantiated.
#
# Fixed behavior: incomplete subclasses should fail at instantiation time.
# ---------------------------------------------------------------------------


class TestIssue6AbstractMethodEnforcement:
    """BaseProvider subclasses missing cache_key or find_candidates should
    fail at instantiation, not at call time."""

    def test_missing_cache_key_prevents_instantiation(self) -> None:
        """A subclass missing cache_key should not be instantiable."""

        class IncompleteCacheKey(resolver.BaseProvider):
            provider_description = "incomplete"

            def find_candidates(self, identifier: str) -> list:
                return []

        with pytest.raises(TypeError):
            IncompleteCacheKey()

    def test_missing_find_candidates_prevents_instantiation(self) -> None:
        """A subclass missing find_candidates should not be instantiable."""

        class IncompleteFindCandidates(resolver.BaseProvider):
            provider_description = "incomplete"

            @property
            def cache_key(self) -> str:
                return "key"

        with pytest.raises(TypeError):
            IncompleteFindCandidates()

    def test_complete_subclass_instantiates(self) -> None:
        """A subclass implementing all required methods should instantiate."""

        class CompleteProvider(resolver.BaseProvider):
            provider_description = "complete"

            @property
            def cache_key(self) -> str:
                return "key"

            def find_candidates(self, identifier: str) -> list:
                return []

        provider = CompleteProvider()
        assert provider is not None


# ---------------------------------------------------------------------------
# Issue 9: Inconsistent get_resolver_provider override calls
#
# Current bug: 5 call sites invoke get_resolver_provider with different
# keyword argument sets.  Some pass req_type/ignore_platform, others omit
# them.  The commands/ callers also vary in whether they pass ctx as wkctx
# or ctx.
#
# Fixed behavior: a single resolve_provider() wrapper should exist with
# explicit parameter defaults.
# ---------------------------------------------------------------------------


class TestIssue9ConsistentResolverProvider:
    """A centralized resolve_provider() wrapper should enforce consistent
    parameter passing for get_resolver_provider calls."""

    def test_resolve_provider_wrapper_exists(self) -> None:
        """A resolve_provider() function should exist in the resolver module
        or a shared location."""
        # Check resolver module first, then sources as a fallback
        has_wrapper = hasattr(resolver, "resolve_provider") or hasattr(
            sources, "resolve_provider"
        )
        assert has_wrapper, (
            "A centralized resolve_provider() wrapper should exist to "
            "enforce consistent get_resolver_provider calls"
        )

    def test_call_sites_use_consistent_kwargs(self) -> None:
        """All call sites invoking get_resolver_provider should pass the
        same set of keyword arguments (via the wrapper)."""
        # Parse the source files that call get_resolver_provider and
        # extract the keyword argument names at each call site.
        source_files = [
            "resolver.py",
            "sources.py",
            "wheels.py",
            "commands/package.py",
            "commands/find_updates.py",
        ]
        base = pathlib.Path(resolver.__file__).parent

        kwarg_sets: list[tuple[str, set[str]]] = []
        for rel_path in source_files:
            filepath = base / rel_path
            tree = ast.parse(filepath.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # Look for calls that include "get_resolver_provider" as a string arg
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and arg.value == "get_resolver_provider"
                    ):
                        kwargs = {kw.arg for kw in node.keywords if kw.arg is not None}
                        kwarg_sets.append((rel_path, kwargs))
                        break

        assert len(kwarg_sets) >= 6, (
            f"Expected at least 6 call sites, found {len(kwarg_sets)}"
        )

        # All call sites should use the same kwargs
        first_file, first_kwargs = kwarg_sets[0]
        for other_file, other_kwargs in kwarg_sets[1:]:
            assert first_kwargs == other_kwargs, (
                f"Inconsistent kwargs between {first_file} ({first_kwargs}) "
                f"and {other_file} ({other_kwargs}). "
                "All call sites should use a consistent wrapper."
            )


# ---------------------------------------------------------------------------
# Issue 10: get_all_patches() exposes internal cache to callers
#
# Current bug: get_all_patches() returns a direct reference to self._patches.
# Mutating the returned dict corrupts the cached state for all future callers.
#
# Fixed behavior: return a defensive copy.
# ---------------------------------------------------------------------------


class TestIssue10PatchesCacheExposure:
    """get_all_patches() should return a defensive copy, not the internal
    cache reference."""

    def test_returned_dict_is_not_internal_cache(
        self, testdata_context: WorkContext
    ) -> None:
        """Mutating the returned dict should NOT affect future calls."""
        pbi = testdata_context.settings.package_build_info("test-pkg")
        patches1 = pbi.get_all_patches()

        # Mutate the returned dict
        patches1[Version("99.99.99")] = [pathlib.Path("/tmp/evil.patch")]

        patches2 = pbi.get_all_patches()
        assert Version("99.99.99") not in patches2, (
            "get_all_patches() should return a defensive copy; "
            "mutations leaked into the internal cache"
        )

    def test_returned_lists_are_not_internal_cache(
        self, testdata_context: WorkContext
    ) -> None:
        """Mutating patch lists inside the returned dict should NOT affect
        future calls."""
        pbi = testdata_context.settings.package_build_info("test-pkg")
        patches1 = pbi.get_all_patches()

        # Pick any version key and mutate its list
        for key in patches1:
            patches1[key].append(pathlib.Path("/tmp/evil.patch"))
            break

        patches2 = pbi.get_all_patches()
        for key in patches2:
            assert pathlib.Path("/tmp/evil.patch") not in patches2[key], (
                "get_all_patches() should return defensive copies of patch lists; "
                "mutations leaked into the internal cache"
            )


# ---------------------------------------------------------------------------
# Issue 11: Duplicate cleanup-or-reuse directory pattern
#
# Current bug: the same check-exists/delete-or-reuse logic is written 3
# times in sources.py and context.py.
#
# Fixed behavior: a shared cleanup_or_reuse() utility should exist.
# ---------------------------------------------------------------------------


class TestIssue11CleanupOrReuseUtility:
    """A shared cleanup_or_reuse utility should exist to replace the
    duplicated pattern."""

    def test_utility_function_exists(self) -> None:
        """A cleanup_or_reuse function should exist in a shared location."""
        from fromager import context as ctx_mod
        from fromager import sources as src_mod

        has_utility = (
            hasattr(ctx_mod, "cleanup_or_reuse")
            or hasattr(src_mod, "cleanup_or_reuse")
            or hasattr(ctx_mod, "_cleanup_or_reuse")
            or hasattr(src_mod, "_cleanup_or_reuse")
        )
        assert has_utility, (
            "A cleanup_or_reuse() utility should exist to replace the "
            "duplicated check-exists/delete-or-reuse pattern"
        )

    def test_cleanup_removes_directory(self, tmp_path: pathlib.Path) -> None:
        """When cleanup=True, the directory should be removed."""
        from fromager import context as ctx_mod
        from fromager import sources as src_mod

        target = tmp_path / "test-dir"
        target.mkdir()
        (target / "file.txt").write_text("content")

        # Try whichever module has the utility
        mod = ctx_mod if hasattr(ctx_mod, "cleanup_or_reuse") else src_mod
        cleanup_fn = getattr(mod, "cleanup_or_reuse", None) or mod._cleanup_or_reuse

        cleanup_fn(target, cleanup=True)
        assert not target.exists(), "Directory should be removed when cleanup=True"

    def test_reuse_keeps_directory(self, tmp_path: pathlib.Path) -> None:
        """When cleanup=False, the directory should be kept and reused."""
        from fromager import context as ctx_mod
        from fromager import sources as src_mod

        target = tmp_path / "test-dir"
        target.mkdir()
        (target / "file.txt").write_text("content")

        mod = ctx_mod if hasattr(ctx_mod, "cleanup_or_reuse") else src_mod
        cleanup_fn = getattr(mod, "cleanup_or_reuse", None) or mod._cleanup_or_reuse

        result = cleanup_fn(target, cleanup=False)
        assert target.exists(), "Directory should be kept when cleanup=False"
        assert result is True, "Should return True when directory is reused"


# ---------------------------------------------------------------------------
# Issue 7: Break up five 100+ line functions with mixed responsibilities
#
# Current bug: several functions exceed 100 lines and mix multiple
# responsibilities.  Fixed behavior: extracted helpers should exist and
# each function should be under a reasonable line threshold.
# ---------------------------------------------------------------------------

_MAX_FUNCTION_LINES = 80


class TestIssue7LargeFunctionBreakup:
    """Large functions should be broken into smaller helpers."""

    @staticmethod
    def _function_line_count(func: object) -> int:
        """Return the number of source lines of a function."""
        source = inspect.getsource(func)  # type: ignore[arg-type]
        return len(source.splitlines())

    def test_write_constraints_file_line_count(self) -> None:
        """write_constraints_file should be under the line threshold."""
        count = self._function_line_count(cmd_bootstrap.write_constraints_file)
        assert count <= _MAX_FUNCTION_LINES, (
            f"write_constraints_file is {count} lines (max {_MAX_FUNCTION_LINES}). "
            "Extract coherent sub-steps into private helpers."
        )

    def test_get_project_from_pypi_line_count(self) -> None:
        """get_project_from_pypi should be under the line threshold."""
        count = self._function_line_count(resolver.get_project_from_pypi)
        assert count <= _MAX_FUNCTION_LINES, (
            f"get_project_from_pypi is {count} lines (max {_MAX_FUNCTION_LINES}). "
            "Extract helpers like _is_yanked, _matches_python_version, etc."
        )

    def test_build_function_line_count(self) -> None:
        """_build should be under the line threshold."""
        count = self._function_line_count(cmd_build._build)
        assert count <= _MAX_FUNCTION_LINES, (
            f"_build is {count} lines (max {_MAX_FUNCTION_LINES}). "
            "Extract coherent sub-steps into private helpers."
        )

    def test_bootstrap_impl_line_count(self) -> None:
        """_bootstrap_impl should be under the line threshold."""
        count = self._function_line_count(bootstrapper.Bootstrapper._bootstrap_impl)
        assert count <= _MAX_FUNCTION_LINES, (
            f"_bootstrap_impl is {count} lines (max {_MAX_FUNCTION_LINES}). "
            "Extract coherent sub-steps into private helpers."
        )

    def test_build_parallel_line_count(self) -> None:
        """build_parallel should be under the line threshold."""
        count = self._function_line_count(cmd_build.build_parallel)
        assert count <= _MAX_FUNCTION_LINES, (
            f"build_parallel is {count} lines (max {_MAX_FUNCTION_LINES}). "
            "Extract coherent sub-steps into private helpers."
        )

    def test_extracted_helpers_exist_for_pypi(self) -> None:
        """Extracted helpers for get_project_from_pypi should exist."""
        expected_helpers = [
            "_is_yanked",
            "_matches_python_version",
            "_matches_platform_tags",
            "_parse_candidate_version",
        ]
        missing = [name for name in expected_helpers if not hasattr(resolver, name)]
        assert not missing, (
            f"Expected helpers missing from resolver module: {missing}. "
            "These should be extracted from get_project_from_pypi()."
        )


# ---------------------------------------------------------------------------
# Issue 8: Rename getter-named functions that perform side effects
#
# Current bug: functions named get_* perform network calls, file writes,
# and state mutations — violating the convention that get_* implies a
# pure lookup.
#
# Fixed behavior: renamed to fetch_*, resolve_*, or load_* to signal
# side effects.
# ---------------------------------------------------------------------------


class TestIssue8GetterNamingConventions:
    """Functions named get_* that perform side effects should be renamed."""

    def test_get_project_from_pypi_renamed(self) -> None:
        """get_project_from_pypi should be renamed to signal side effects
        (e.g. fetch_project_from_pypi)."""
        assert not hasattr(resolver, "get_project_from_pypi"), (
            "get_project_from_pypi still exists — it should be renamed to "
            "fetch_project_from_pypi (or similar) to signal network side effects"
        )
        # Verify the new name exists
        has_new_name = (
            hasattr(resolver, "fetch_project_from_pypi")
            or hasattr(resolver, "resolve_project_from_pypi")
            or hasattr(resolver, "load_project_from_pypi")
        )
        assert has_new_name, (
            "Neither fetch_project_from_pypi, resolve_project_from_pypi, nor "
            "load_project_from_pypi exists on the resolver module"
        )
