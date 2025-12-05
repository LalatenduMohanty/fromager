"""Tests for bootstrap test mode functionality."""

from __future__ import annotations

import datetime
from unittest import mock

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from fromager.bootstrapper import Bootstrapper, BuildFailure
from fromager.requirements_file import RequirementType


class TestBuildFailure:
    """Tests for the BuildFailure dataclass."""

    def test_build_failure_creation(self) -> None:
        """Test creating a BuildFailure instance."""
        req = Requirement("numpy==1.24.0")
        version = Version("1.24.0")
        error = Exception("Build failed")
        chain = [
            (RequirementType.TOP_LEVEL, Requirement("pandas"), Version("2.0.0")),
            (RequirementType.INSTALL, Requirement("numpy"), Version("1.24.0")),
        ]

        failure = BuildFailure(
            req=req,
            version=version,
            error=error,
            error_type="build_wheel",
            timestamp=datetime.datetime.now(),
            dependency_chain=chain,
            fallback_status="prebuilt_success",
        )

        assert failure.req == req
        assert failure.version == version
        assert failure.error_type == "build_wheel"
        assert failure.fallback_status == "prebuilt_success"

    def test_root_package_with_chain(self) -> None:
        """Test root_package property with dependency chain."""
        chain = [
            (RequirementType.TOP_LEVEL, Requirement("pandas"), Version("2.0.0")),
            (RequirementType.INSTALL, Requirement("scipy"), Version("1.10.0")),
        ]
        failure = BuildFailure(
            req=Requirement("numpy"),
            version=Version("1.24.0"),
            error=Exception("error"),
            error_type="build",
            timestamp=datetime.datetime.now(),
            dependency_chain=chain,
            fallback_status="prebuilt_success",
        )

        assert failure.root_package == "pandas"

    def test_root_package_without_chain(self) -> None:
        """Test root_package property without dependency chain."""
        failure = BuildFailure(
            req=Requirement("numpy"),
            version=Version("1.24.0"),
            error=Exception("error"),
            error_type="build",
            timestamp=datetime.datetime.now(),
            dependency_chain=[],
            fallback_status="prebuilt_success",
        )

        assert failure.root_package == "numpy"

    def test_immediate_parent_with_chain(self) -> None:
        """Test immediate_parent property with dependency chain."""
        chain = [
            (RequirementType.TOP_LEVEL, Requirement("pandas"), Version("2.0.0")),
            (RequirementType.INSTALL, Requirement("scipy"), Version("1.10.0")),
        ]
        failure = BuildFailure(
            req=Requirement("numpy"),
            version=Version("1.24.0"),
            error=Exception("error"),
            error_type="build",
            timestamp=datetime.datetime.now(),
            dependency_chain=chain,
            fallback_status="prebuilt_success",
        )

        assert failure.immediate_parent == ("INSTALL", "scipy", "1.10.0")

    def test_immediate_parent_without_chain(self) -> None:
        """Test immediate_parent property without dependency chain."""
        failure = BuildFailure(
            req=Requirement("numpy"),
            version=Version("1.24.0"),
            error=Exception("error"),
            error_type="build",
            timestamp=datetime.datetime.now(),
            dependency_chain=[],
            fallback_status="prebuilt_success",
        )

        assert failure.immediate_parent is None

    def test_chain_depth(self) -> None:
        """Test chain_depth property."""
        chain = [
            (RequirementType.TOP_LEVEL, Requirement("pandas"), Version("2.0.0")),
            (RequirementType.INSTALL, Requirement("scipy"), Version("1.10.0")),
            (RequirementType.BUILD_SYSTEM, Requirement("setuptools"), Version("67.0")),
        ]
        failure = BuildFailure(
            req=Requirement("numpy"),
            version=Version("1.24.0"),
            error=Exception("error"),
            error_type="build",
            timestamp=datetime.datetime.now(),
            dependency_chain=chain,
            fallback_status="prebuilt_success",
        )

        assert failure.chain_depth == 3

    def test_format_chain(self) -> None:
        """Test format_chain method produces readable output."""
        chain = [
            (RequirementType.TOP_LEVEL, Requirement("pandas"), Version("2.0.0")),
            (RequirementType.INSTALL, Requirement("scipy"), Version("1.10.0")),
        ]
        failure = BuildFailure(
            req=Requirement("numpy"),
            version=Version("1.24.0"),
            error=Exception("error"),
            error_type="build",
            timestamp=datetime.datetime.now(),
            dependency_chain=chain,
            fallback_status="prebuilt_success",
        )

        formatted = failure.format_chain()
        assert "TOP_LEVEL: pandas==2.0.0" in formatted
        assert "INSTALL: scipy==1.10.0" in formatted
        assert "FAILED: numpy==1.24.0" in formatted

    def test_to_dict(self) -> None:
        """Test to_dict serialization."""
        chain = [
            (RequirementType.TOP_LEVEL, Requirement("pandas"), Version("2.0.0")),
        ]
        failure = BuildFailure(
            req=Requirement("numpy"),
            version=Version("1.24.0"),
            error=Exception("Build failed"),
            error_type="build_wheel",
            timestamp=datetime.datetime(2025, 12, 5, 10, 30, 0),
            dependency_chain=chain,
            fallback_status="prebuilt_success",
        )

        result = failure.to_dict()

        assert result["package"] == "numpy"
        assert result["version"] == "1.24.0"
        assert result["error_type"] == "build_wheel"
        assert result["fallback_status"] == "prebuilt_success"
        assert result["root_package"] == "pandas"
        assert result["chain_depth"] == 1
        assert len(result["dependency_chain"]) == 1


class TestBootstrapperTestMode:
    """Tests for Bootstrapper test mode functionality."""

    @pytest.fixture
    def mock_ctx(self) -> mock.MagicMock:
        """Create a mock WorkContext."""
        ctx = mock.MagicMock()
        ctx.work_dir = mock.MagicMock()
        ctx.wheel_server_url = "http://localhost:8080"
        return ctx

    def test_test_mode_disabled_by_default(self, mock_ctx: mock.MagicMock) -> None:
        """Test that test_mode is disabled by default."""
        bt = Bootstrapper(ctx=mock_ctx)
        assert bt.test_mode is False
        assert bt._build_failures == []

    def test_test_mode_enabled(self, mock_ctx: mock.MagicMock) -> None:
        """Test that test_mode can be enabled."""
        bt = Bootstrapper(ctx=mock_ctx, test_mode=True)
        assert bt.test_mode is True

    def test_has_failures_empty(self, mock_ctx: mock.MagicMock) -> None:
        """Test has_failures returns False when no failures."""
        bt = Bootstrapper(ctx=mock_ctx, test_mode=True)
        assert bt.has_failures() is False

    def test_has_failures_with_failures(self, mock_ctx: mock.MagicMock) -> None:
        """Test has_failures returns True when failures exist."""
        bt = Bootstrapper(ctx=mock_ctx, test_mode=True)
        bt._build_failures.append(
            BuildFailure(
                req=Requirement("numpy"),
                version=Version("1.24.0"),
                error=Exception("error"),
                error_type="build",
                timestamp=datetime.datetime.now(),
                dependency_chain=[],
                fallback_status="prebuilt_success",
            )
        )
        assert bt.has_failures() is True

    def test_get_build_failures(self, mock_ctx: mock.MagicMock) -> None:
        """Test get_build_failures returns copy of failures list."""
        bt = Bootstrapper(ctx=mock_ctx, test_mode=True)
        failure = BuildFailure(
            req=Requirement("numpy"),
            version=Version("1.24.0"),
            error=Exception("error"),
            error_type="build",
            timestamp=datetime.datetime.now(),
            dependency_chain=[],
            fallback_status="prebuilt_success",
        )
        bt._build_failures.append(failure)

        failures = bt.get_build_failures()
        assert len(failures) == 1
        assert failures[0] == failure
        assert failures is not bt._build_failures

    def test_record_failure(self, mock_ctx: mock.MagicMock) -> None:
        """Test _record_failure method."""
        bt = Bootstrapper(ctx=mock_ctx, test_mode=True)
        bt.why = [
            (RequirementType.TOP_LEVEL, Requirement("pandas"), Version("2.0.0")),
        ]

        bt._record_failure(
            req=Requirement("numpy"),
            version=Version("1.24.0"),
            error=Exception("Build failed"),
            error_type="build_wheel",
            fallback_status="pending",
        )

        assert len(bt._build_failures) == 1
        failure = bt._build_failures[0]
        assert str(failure.req.name) == "numpy"
        assert failure.error_type == "build_wheel"
        assert failure.fallback_status == "pending"
        assert len(failure.dependency_chain) == 1

    def test_get_failures_by_root(self, mock_ctx: mock.MagicMock) -> None:
        """Test get_failures_by_root groups failures correctly."""
        bt = Bootstrapper(ctx=mock_ctx, test_mode=True)

        # Add failure under pandas
        bt._build_failures.append(
            BuildFailure(
                req=Requirement("numpy"),
                version=Version("1.24.0"),
                error=Exception("error"),
                error_type="build",
                timestamp=datetime.datetime.now(),
                dependency_chain=[
                    (RequirementType.TOP_LEVEL, Requirement("pandas"), Version("2.0.0"))
                ],
                fallback_status="prebuilt_success",
            )
        )

        # Add failure under matplotlib
        bt._build_failures.append(
            BuildFailure(
                req=Requirement("pillow"),
                version=Version("9.5.0"),
                error=Exception("error"),
                error_type="build",
                timestamp=datetime.datetime.now(),
                dependency_chain=[
                    (
                        RequirementType.TOP_LEVEL,
                        Requirement("matplotlib"),
                        Version("3.7.0"),
                    )
                ],
                fallback_status="prebuilt_failed",
            )
        )

        by_root = bt.get_failures_by_root()
        assert "pandas" in by_root
        assert "matplotlib" in by_root
        assert len(by_root["pandas"]) == 1
        assert len(by_root["matplotlib"]) == 1

    def test_get_failure_summary(self, mock_ctx: mock.MagicMock) -> None:
        """Test get_failure_summary returns correct summary."""
        bt = Bootstrapper(ctx=mock_ctx, test_mode=True)

        # Add successful fallback
        bt._build_failures.append(
            BuildFailure(
                req=Requirement("numpy"),
                version=Version("1.24.0"),
                error=Exception("error"),
                error_type="build",
                timestamp=datetime.datetime.now(),
                dependency_chain=[],
                fallback_status="prebuilt_success",
            )
        )

        # Add failed fallback
        bt._build_failures.append(
            BuildFailure(
                req=Requirement("pillow"),
                version=Version("9.5.0"),
                error=Exception("error"),
                error_type="build",
                timestamp=datetime.datetime.now(),
                dependency_chain=[],
                fallback_status="prebuilt_failed",
            )
        )

        summary = bt.get_failure_summary()
        assert summary["total_failures"] == 2
        assert summary["fallback_success"] == 1
        assert summary["fallback_failed"] == 1
