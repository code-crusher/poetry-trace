from __future__ import annotations

import pytest

from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from poetry.core.packages.dependency import Dependency
from poetry.core.packages.package import Package

from poetry.utils.pep_508 import relax_metadata_constraints
from poetry.utils.pep_508 import relax_version_union
from poetry.utils.pep_508 import to_pep_508_if_possible


def make_package(*dependencies: Dependency) -> Package:
    package = Package("demo", "0.1.0")
    for dependency in dependencies:
        package.add_dependency(dependency)
    return package


@pytest.mark.parametrize(
    ["constraint", "expected"],
    [
        # https://github.com/python-poetry/poetry/issues/11101
        ("^4.5.0 || ^5.0.0", ">=4.5.0,<6.0.0"),
        # consecutive pre-release gates are merged in one go
        ("^1.2 || ^2.0 || ^3.0", ">=1.2,<4.0"),
        # already valid PEP 508
        ("^1.0", ">=1.0,<2.0"),
        ("!=1.0", "!=1.0"),
        ("!=1.0.*", "!=1.0.*"),
        ("*", "*"),
    ],
)
def test_relax_version_union(constraint: str, expected: str) -> None:
    relaxed = relax_version_union(Dependency("foo", constraint).constraint)

    assert str(relaxed) == expected


@pytest.mark.parametrize(
    "constraint",
    [
        # a real gap between the ranges
        ">=1.0,<1.5 || >=2.0",
        "^0.1 || ^0.3",
        # exclusions that are not a single version or wildcard range
        ">=4,<7,!=5.*",
        "!=44.*,!=45.*",
    ],
)
def test_relax_version_union_returns_none_if_unrepresentable(constraint: str) -> None:
    assert relax_version_union(Dependency("foo", constraint).constraint) is None


def test_to_pep_508_if_possible_relaxes_union() -> None:
    # https://github.com/python-poetry/poetry/issues/11101
    dependency = Dependency("lxml", "^4.5.0 || ^5.0.0")

    requirement = to_pep_508_if_possible(dependency)

    assert requirement == "lxml (>=4.5.0,<6.0.0)"
    # the generated metadata must be valid PEP 508
    Requirement(requirement)


def test_to_pep_508_if_possible_returns_none_for_unrepresentable_union() -> None:
    dependency = Dependency("cachy", "^0.1 || ^0.3")

    assert to_pep_508_if_possible(dependency) is None


def test_to_pep_508_if_possible_keeps_valid_requirement() -> None:
    dependency = Dependency("foo", "^1.2.3", extras=["a"])

    assert to_pep_508_if_possible(dependency) == "foo[a] (>=1.2.3,<2.0.0)"


def test_relax_metadata_constraints_relaxes_prerelease_gates() -> None:
    dependency = Dependency("lxml", "^4.5.0 || ^5.0.0")
    package = make_package(dependency)

    relaxed, unrepresentable = relax_metadata_constraints(package)

    assert relaxed == [
        ("lxml (>=4.5.0,<5.0.0 || >=5.0.0,<6.0.0)", "lxml (>=4.5.0,<6.0.0)")
    ]
    assert unrepresentable == []
    # the dependency is relaxed in place so that the metadata is valid
    assert dependency.to_pep_508() == "lxml (>=4.5.0,<6.0.0)"
    Requirement(dependency.to_pep_508())


def test_relax_metadata_constraints_reports_unrepresentable_union() -> None:
    package = make_package(Dependency("cachy", "^0.1 || ^0.3"))

    relaxed, unrepresentable = relax_metadata_constraints(package)

    assert relaxed == []
    assert unrepresentable == ["cachy (>=0.1,<0.2 || >=0.3,<0.4)"]


def test_relax_metadata_constraints_skips_optional_dependencies_without_extra() -> None:
    package = make_package(Dependency("cachy", "^0.1 || ^0.3", optional=True))

    assert relax_metadata_constraints(package) == ([], [])


def test_relax_metadata_constraints_ignores_valid_requirements() -> None:
    package = make_package(Dependency("foo", "^1.0"), Dependency("bar", "!=1.0"))

    assert relax_metadata_constraints(package) == ([], [])


def test_metadata_would_be_invalid_without_relaxing() -> None:
    # guards the regression itself: poetry-core 2.5.0 renders the raw "||"
    dependency = Dependency("lxml", "^4.5.0 || ^5.0.0")

    with pytest.raises(InvalidRequirement):
        Requirement(dependency.to_pep_508())
