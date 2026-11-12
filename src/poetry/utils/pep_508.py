from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from poetry.core.constraints.version import VersionConstraint
    from poetry.core.constraints.version import VersionRangeConstraint
    from poetry.core.packages.dependency import Dependency
    from poetry.core.packages.package import Package


def relax_version_union(constraint: VersionConstraint) -> VersionConstraint | None:
    """
    Try to express a version constraint without a disjunction ("||").

    PEP 508 has no way to represent a disjunction of version specifiers, so a
    ``VersionUnion`` has to be collapsed into a single range before it can be
    exported as package metadata. That is only possible if the ranges are
    separated solely by a pre-release gate, i.e. a canonical exclusive upper
    bound ``<V.dev0`` (the internal form of a user-typed ``<V``) directly
    followed by an inclusive lower bound ``>=V``. Such a union excludes nothing
    but pre-releases of ``V``, which installers do not select unless explicitly
    requested, so it can safely be widened into a single range
    (e.g. ``^4.5.0 || ^5.0.0`` becomes ``>=4.5.0,<6.0.0``).

    :returns: a constraint without a disjunction, or ``None`` if the constraint
        cannot be represented in PEP 508.
    """
    from poetry.core.constraints.version import VersionRange
    from poetry.core.constraints.version import VersionUnion

    if not isinstance(constraint, VersionUnion):
        return constraint

    if constraint.excludes_single_version or constraint.excludes_single_wildcard_range:
        # These unions are equivalent to a single exclusion ("!=1.2.3" or
        # "!=1.2.*"), which is valid PEP 508 already.
        return constraint

    merged: list[VersionRangeConstraint] = []
    for version_range in constraint.ranges:
        if (
            merged
            and (previous := merged[-1]).max is not None
            and not previous.include_max
            and previous.max.is_devrelease()
            and version_range.min is not None
            and version_range.include_min
            and previous.max.without_devrelease() == version_range.min
        ):
            merged[-1] = VersionRange(
                previous.min,
                version_range.max,
                previous.include_min,
                version_range.include_max,
            )
        else:
            merged.append(version_range)

    if len(merged) == 1:
        return merged[0]

    return None


def to_pep_508_if_possible(dependency: Dependency) -> str | None:
    """
    Render a dependency as PEP 508, widening its constraint if necessary.

    :returns: a valid PEP 508 requirement, or ``None`` if the dependency cannot
        be represented in PEP 508.
    """
    from poetry.core.version.requirements import InvalidRequirementError
    from poetry.core.version.requirements import parse_requirement

    requirement = dependency.to_pep_508()
    try:
        parse_requirement(requirement)
    except InvalidRequirementError:
        relaxed = relax_version_union(dependency.constraint)
        if relaxed is None:
            return None
        requirement = dependency.with_constraint(relaxed).to_pep_508()
        try:
            parse_requirement(requirement)
        except InvalidRequirementError:
            return None
    return requirement


def relax_metadata_constraints(
    package: Package,
) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Make the requirements of a package exportable as valid PEP 508 metadata.

    The metadata contains the requirements of all non-optional dependencies
    (and of the optional ones that belong to an extra). Constraints that can be
    widened into a single range are relaxed in place, because PEP 508 has no
    way to represent a disjunction ("||") of version specifiers.

    :returns: the relaxed requirements as ``(before, after)`` pairs and the
        requirements that cannot be represented in PEP 508 at all.
    """
    from poetry.core.version.requirements import InvalidRequirementError
    from poetry.core.version.requirements import parse_requirement

    relaxed: list[tuple[str, str]] = []
    unrepresentable: list[str] = []
    for dependency in package.requires:
        if dependency.is_optional() and not dependency.in_extras:
            continue

        requirement = dependency.to_pep_508()
        try:
            parse_requirement(requirement)
        except InvalidRequirementError:
            constraint = relax_version_union(dependency.constraint)
            if constraint is None:
                unrepresentable.append(requirement)
            else:
                dependency.constraint = constraint
                relaxed.append((requirement, dependency.to_pep_508()))

    return relaxed, unrepresentable
