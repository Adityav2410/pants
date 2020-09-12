# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import json
import os.path
import textwrap
import zipfile
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Tuple, cast

import pytest
from pkg_resources import Requirement

from pants.backend.python.target_types import PythonInterpreterCompatibility
from pants.backend.python.util_rules.pex import (
    Pex,
    PexInterpreterConstraints,
    PexPlatforms,
    PexProcess,
    PexRequest,
    PexRequirements,
)
from pants.backend.python.util_rules.pex import rules as pex_rules
from pants.core.util_rules.pants_environment import PantsEnvironment
from pants.engine.addresses import Address
from pants.engine.fs import CreateDigest, Digest, FileContent
from pants.engine.process import Process, ProcessResult
from pants.engine.rules import QueryRule
from pants.engine.target import FieldSet
from pants.option.options_bootstrapper import OptionsBootstrapper
from pants.python.python_setup import PythonSetup
from pants.testutil.option_util import create_options_bootstrapper, create_subsystem
from pants.testutil.rule_runner import RuleRunner
from pants.util.frozendict import FrozenDict


def test_merge_interpreter_constraints() -> None:
    def assert_merged(*, inp: List[List[str]], expected: List[str]) -> None:
        result = sorted(str(req) for req in PexInterpreterConstraints.merge_constraint_sets(inp))
        # Requirement.parse() sorts specs differently than we'd like, so we convert each str to a
        # Requirement.
        normalized_expected = sorted(str(Requirement.parse(v)) for v in expected)
        assert result == normalized_expected

    # Multiple constraint sets get merged so that they are ANDed.
    # A & B => A & B
    assert_merged(inp=[["CPython==2.7.*"], ["CPython==3.6.*"]], expected=["CPython==2.7.*,==3.6.*"])

    # Multiple constraints within a single constraint set are kept separate so that they are ORed.
    # A | B => A | B
    assert_merged(
        inp=[["CPython==2.7.*", "CPython==3.6.*"]], expected=["CPython==2.7.*", "CPython==3.6.*"]
    )

    # Input constraints already were ANDed.
    # A => A
    assert_merged(inp=[["CPython>=2.7,<3"]], expected=["CPython>=2.7,<3"])

    # Both AND and OR.
    # (A | B) & C => (A & B) | (B & C)
    assert_merged(
        inp=[["CPython>=2.7,<3", "CPython>=3.5"], ["CPython==3.6.*"]],
        expected=["CPython>=2.7,<3,==3.6.*", "CPython>=3.5,==3.6.*"],
    )
    # A & B & (C | D) => (A & B & C) | (A & B & D)
    assert_merged(
        inp=[["CPython==2.7.*"], ["CPython==3.6.*"], ["CPython==3.7.*", "CPython==3.8.*"]],
        expected=["CPython==2.7.*,==3.6.*,==3.7.*", "CPython==2.7.*,==3.6.*,==3.8.*"],
    )
    # (A | B) & (C | D) => (A & C) | (A & D) | (B & C) | (B & D)
    assert_merged(
        inp=[["CPython>=2.7,<3", "CPython>=3.5"], ["CPython==3.6.*", "CPython==3.7.*"]],
        expected=[
            "CPython>=2.7,<3,==3.6.*",
            "CPython>=2.7,<3,==3.7.*",
            "CPython>=3.5,==3.6.*",
            "CPython>=3.5,==3.7.*",
        ],
    )
    # A & (B | C | D) & (E | F) & G =>
    # (A & B & E & G) | (A & B & F & G) | (A & C & E & G) | (A & C & F & G) | (A & D & E & G) | (A & D & F & G)
    assert_merged(
        inp=[
            ["CPython==3.6.5"],
            ["CPython==2.7.14", "CPython==2.7.15", "CPython==2.7.16"],
            ["CPython>=3.6", "CPython==3.5.10"],
            ["CPython>3.8"],
        ],
        expected=[
            "CPython==2.7.14,==3.5.10,==3.6.5,>3.8",
            "CPython==2.7.14,>=3.6,==3.6.5,>3.8",
            "CPython==2.7.15,==3.5.10,==3.6.5,>3.8",
            "CPython==2.7.15,>=3.6,==3.6.5,>3.8",
            "CPython==2.7.16,==3.5.10,==3.6.5,>3.8",
            "CPython==2.7.16,>=3.6,==3.6.5,>3.8",
        ],
    )

    # Deduplicate between constraint_sets
    # (A | B) & (A | B) => A | B. Naively, this should actually resolve as follows:
    #   (A | B) & (A | B) => (A & A) | (A & B) | (B & B) => A | (A & B) | B.
    # But, we first deduplicate each constraint_set.  (A | B) & (A | B) can be rewritten as
    # X & X => X.
    assert_merged(
        inp=[["CPython==2.7.*", "CPython==3.6.*"], ["CPython==2.7.*", "CPython==3.6.*"]],
        expected=["CPython==2.7.*", "CPython==3.6.*"],
    )
    # (A | B) & C & (A | B) => (A & C) | (B & C). Alternatively, this can be rewritten as
    # X & Y & X => X & Y.
    assert_merged(
        inp=[
            ["CPython>=2.7,<3", "CPython>=3.5"],
            ["CPython==3.6.*"],
            ["CPython>=3.5", "CPython>=2.7,<3"],
        ],
        expected=["CPython>=2.7,<3,==3.6.*", "CPython>=3.5,==3.6.*"],
    )

    # No specifiers
    assert_merged(inp=[["CPython"]], expected=["CPython"])
    assert_merged(inp=[["CPython"], ["CPython==3.7.*"]], expected=["CPython==3.7.*"])

    # No interpreter is shorthand for CPython, which is how Pex behaves
    assert_merged(inp=[[">=3.5"], ["CPython==3.7.*"]], expected=["CPython>=3.5,==3.7.*"])

    # Different Python interpreters, which are guaranteed to fail when ANDed but are safe when ORed.
    with pytest.raises(ValueError):
        PexInterpreterConstraints.merge_constraint_sets([["CPython==3.7.*"], ["PyPy==43.0"]])
    assert_merged(inp=[["CPython==3.7.*", "PyPy==43.0"]], expected=["CPython==3.7.*", "PyPy==43.0"])

    # Ensure we can handle empty input.
    assert_merged(inp=[], expected=[])


@pytest.mark.parametrize(
    "constraints",
    [
        ["CPython>=2.7,<3"],
        ["CPython>=2.7,<3", "CPython>=3.6"],
        ["CPython>=2.7.13"],
        ["CPython>=2.7.13,<2.7.16"],
        ["CPython>=2.7.13,!=2.7.16"],
        ["PyPy>=2.7,<3"],
    ],
)
def test_interpreter_constraints_includes_python2(constraints) -> None:
    assert PexInterpreterConstraints(constraints).includes_python2() is True


@pytest.mark.parametrize(
    "constraints",
    [
        ["CPython>=3.6"],
        ["CPython>=3.7"],
        ["CPython>=3.6", "CPython>=3.8"],
        ["CPython!=2.7.*"],
        ["PyPy>=3.6"],
    ],
)
def test_interpreter_constraints_do_not_include_python2(constraints):
    assert PexInterpreterConstraints(constraints).includes_python2() is False


@pytest.mark.parametrize(
    "constraints",
    [
        ["CPython==3.6.*"],
        ["CPython==3.6.1"],
        ["CPython==3.7.1"],
        ["CPython==3.8.2"],
        ["CPython==3.9.1"],
        ["CPython>=3.6"],
        ["CPython>=3.7"],
        ["CPython==3.6.*", "CPython==3.7.*"],
        ["PyPy>=3.6"],
    ],
)
def test_interpreter_constraints_require_python36(constraints) -> None:
    assert PexInterpreterConstraints(constraints).requires_python36_or_newer() is True


@pytest.mark.parametrize(
    "constraints",
    [
        ["CPython==3.5.*"],
        ["CPython==3.5.3"],
        ["CPython>=3.5"],
        ["CPython==3.5.*", "CPython==3.6.*"],
        ["CPython==3.5.3", "CPython==3.6.3"],
        ["PyPy>=3.5"],
    ],
)
def test_interpreter_constraints_do_not_require_python36(constraints):
    assert PexInterpreterConstraints(constraints).requires_python36_or_newer() is False


@pytest.mark.parametrize(
    "constraints",
    [
        ["CPython==3.7.*"],
        ["CPython==3.7.1"],
        ["CPython==3.8.1"],
        ["CPython==3.9.1"],
        ["CPython>=3.7"],
        ["CPython>=3.8"],
        ["CPython==3.7.*", "CPython==3.8.*"],
        ["PyPy>=3.7"],
    ],
)
def test_interpreter_constraints_require_python37(constraints) -> None:
    assert PexInterpreterConstraints(constraints).requires_python37_or_newer() is True


@pytest.mark.parametrize(
    "constraints",
    [
        ["CPython==3.5.*"],
        ["CPython==3.6.*"],
        ["CPython==3.6.3"],
        ["CPython>=3.6"],
        ["CPython==3.6.*", "CPython==3.7.*"],
        ["CPython==3.6.3", "CPython==3.7.3"],
        ["PyPy>=3.6"],
    ],
)
def test_interpreter_constraints_do_not_require_python37(constraints):
    assert PexInterpreterConstraints(constraints).requires_python37_or_newer() is False


@pytest.mark.parametrize(
    "constraints",
    [
        ["CPython==3.8.*"],
        ["CPython==3.8.1"],
        ["CPython==3.9.1"],
        ["CPython>=3.8"],
        ["CPython>=3.9"],
        ["CPython==3.8.*", "CPython==3.9.*"],
        ["PyPy>=3.8"],
    ],
)
def test_interpreter_constraints_require_python38(constraints) -> None:
    assert PexInterpreterConstraints(constraints).requires_python38_or_newer() is True


@pytest.mark.parametrize(
    "constraints",
    [
        ["CPython==3.5.*"],
        ["CPython==3.6.*"],
        ["CPython==3.7.*"],
        ["CPython==3.7.3"],
        ["CPython>=3.7"],
        ["CPython==3.7.*", "CPython==3.8.*"],
        ["CPython==3.5.3", "CPython==3.8.3"],
        ["PyPy>=3.7"],
    ],
)
def test_interpreter_constraints_do_not_require_python38(constraints):
    assert PexInterpreterConstraints(constraints).requires_python38_or_newer() is False


@dataclass(frozen=True)
class MockFieldSet(FieldSet):
    compatibility: PythonInterpreterCompatibility

    @classmethod
    def create_for_test(cls, address: str, compat: Optional[str]) -> "MockFieldSet":
        addr = Address.parse(address)
        return cls(address=addr, compatibility=PythonInterpreterCompatibility(compat, address=addr))


def test_group_field_sets_by_constraints() -> None:
    py2_fs = MockFieldSet.create_for_test("//:py2", ">=2.7,<3")
    py3_fs = [
        MockFieldSet.create_for_test("//:py3", "==3.6.*"),
        MockFieldSet.create_for_test("//:py3_second", "==3.6.*"),
    ]
    no_constraints_fs = MockFieldSet.create_for_test("//:no_constraints", None)
    assert PexInterpreterConstraints.group_field_sets_by_constraints(
        [py2_fs, *py3_fs, no_constraints_fs],
        python_setup=create_subsystem(PythonSetup, interpreter_constraints=[]),
    ) == FrozenDict(
        {
            PexInterpreterConstraints(): (no_constraints_fs,),
            PexInterpreterConstraints(["CPython>=2.7,<3"]): (py2_fs,),
            PexInterpreterConstraints(["CPython==3.6.*"]): tuple(py3_fs),
        }
    )


@dataclass(frozen=True)
class ExactRequirement:
    project_name: str
    version: str

    @classmethod
    def parse(cls, requirement: str) -> "ExactRequirement":
        req = Requirement.parse(requirement)
        assert len(req.specs) == 1, (
            "Expected an exact requirement with only 1 specifier, given {requirement} with "
            "{count} specifiers".format(requirement=requirement, count=len(req.specs))
        )
        operator, version = req.specs[0]
        assert operator == "==", (
            "Expected an exact requirement using only the '==' specifier, given {requirement} "
            "using the {operator!r} operator".format(requirement=requirement, operator=operator)
        )
        return cls(project_name=req.project_name, version=version)


def parse_requirements(requirements: Iterable[str]) -> Iterator[ExactRequirement]:
    for requirement in requirements:
        yield ExactRequirement.parse(requirement)


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        rules=[
            *pex_rules(),
            QueryRule(Pex, (PexRequest, OptionsBootstrapper, PantsEnvironment)),
            QueryRule(Process, (PexProcess, OptionsBootstrapper, PantsEnvironment)),
            QueryRule(ProcessResult, (Process,)),
        ]
    )


def create_pex_and_get_all_data(
    rule_runner: RuleRunner,
    *,
    requirements=PexRequirements(),
    entry_point=None,
    interpreter_constraints=PexInterpreterConstraints(),
    platforms=PexPlatforms(),
    sources: Optional[Digest] = None,
    additional_inputs: Optional[Digest] = None,
    additional_pants_args: Tuple[str, ...] = (),
    additional_pex_args: Tuple[str, ...] = (),
) -> Dict:
    request = PexRequest(
        output_filename="test.pex",
        internal_only=True,
        requirements=requirements,
        interpreter_constraints=interpreter_constraints,
        platforms=platforms,
        entry_point=entry_point,
        sources=sources,
        additional_inputs=additional_inputs,
        additional_args=additional_pex_args,
    )
    pex = rule_runner.request_product(
        Pex,
        [
            request,
            create_options_bootstrapper(
                args=["--backend-packages=pants.backend.python", *additional_pants_args]
            ),
            PantsEnvironment(),
        ],
    )
    rule_runner.scheduler.write_digest(pex.digest)
    pex_path = os.path.join(rule_runner.build_root, "test.pex")
    with zipfile.ZipFile(pex_path, "r") as zipfp:
        with zipfp.open("PEX-INFO", "r") as pex_info:
            pex_info_content = pex_info.readline().decode()
            pex_list = zipfp.namelist()
    return {
        "pex": pex,
        "local_path": pex_path,
        "info": json.loads(pex_info_content),
        "files": pex_list,
    }


def create_pex_and_get_pex_info(
    rule_runner: RuleRunner,
    *,
    requirements=PexRequirements(),
    entry_point=None,
    interpreter_constraints=PexInterpreterConstraints(),
    platforms=PexPlatforms(),
    sources: Optional[Digest] = None,
    additional_pants_args: Tuple[str, ...] = (),
    additional_pex_args: Tuple[str, ...] = (),
) -> Dict:
    return cast(
        Dict,
        create_pex_and_get_all_data(
            rule_runner,
            requirements=requirements,
            entry_point=entry_point,
            interpreter_constraints=interpreter_constraints,
            platforms=platforms,
            sources=sources,
            additional_pants_args=additional_pants_args,
            additional_pex_args=additional_pex_args,
        )["info"],
    )


def test_pex_execution(rule_runner: RuleRunner) -> None:
    sources = rule_runner.request_product(
        Digest,
        [
            CreateDigest(
                (
                    FileContent(path="main.py", content=b'print("from main")'),
                    FileContent(path="subdir/sub.py", content=b'print("from sub")'),
                )
            ),
        ],
    )
    pex_output = create_pex_and_get_all_data(rule_runner, entry_point="main", sources=sources)

    pex_files = pex_output["files"]
    assert "pex" not in pex_files
    assert "main.py" in pex_files
    assert "subdir/sub.py" in pex_files

    # We reasonably expect there to be a python interpreter on the test-running process's path.
    env = {"PATH": os.getenv("PATH", "")}

    process = Process(
        argv=("python", "test.pex"),
        env=env,
        input_digest=pex_output["pex"].digest,
        description="Run the pex and make sure it works",
    )
    result = rule_runner.request_product(ProcessResult, [process])
    assert result.stdout == b"from main\n"


def test_pex_environment(rule_runner: RuleRunner) -> None:
    sources = rule_runner.request_product(
        Digest,
        [
            CreateDigest(
                (
                    FileContent(
                        path="main.py",
                        content=textwrap.dedent(
                            """
                        from os import environ
                        print(f"LANG={environ.get('LANG')}")
                        print(f"ftp_proxy={environ.get('ftp_proxy')}")
                        """
                        ).encode(),
                    ),
                )
            ),
        ],
    )
    pex_output = create_pex_and_get_all_data(rule_runner, entry_point="main", sources=sources)

    process = rule_runner.request_product(
        Process,
        [
            PexProcess(
                pex_output["pex"],
                argv=["python", "test.pex"],
                input_digest=pex_output["pex"].digest,
                description="Run the pex and check its reported environment",
            ),
            create_options_bootstrapper(
                args=[
                    "--subprocess-environment-env-vars=LANG",  # Value should come from environment.
                    "--subprocess-environment-env-vars=ftp_proxy=dummyproxy",
                ]
            ),
            PantsEnvironment({"LANG": "es_PY.UTF-8"}),
        ],
    )

    result = rule_runner.request_product(ProcessResult, [process])
    assert b"LANG=es_PY.UTF-8" in result.stdout
    assert b"ftp_proxy=dummyproxy" in result.stdout


def test_resolves_dependencies(rule_runner: RuleRunner) -> None:
    requirements = PexRequirements(["six==1.12.0", "jsonschema==2.6.0", "requests==2.23.0"])
    pex_info = create_pex_and_get_pex_info(rule_runner, requirements=requirements)
    # NB: We do not check for transitive dependencies, which PEX-INFO will include. We only check
    # that at least the dependencies we requested are included.
    assert set(parse_requirements(requirements)).issubset(
        set(parse_requirements(pex_info["requirements"]))
    )


def test_requirement_constraints(rule_runner: RuleRunner) -> None:
    # This is intentionally old; a constraint will resolve us to a more modern version.
    direct_dep = "requests==1.0.0"
    constraints = [
        "requests==2.23.0",
        "certifi==2019.6.16",
        "chardet==3.0.2",
        "idna==2.7",
        "urllib3==1.25.6",
    ]
    rule_runner.create_file("constraints.txt", "\n".join(constraints))

    pex_info = create_pex_and_get_pex_info(
        rule_runner,
        requirements=PexRequirements([direct_dep]),
        additional_pants_args=("--python-setup-requirement-constraints=constraints.txt",),
    )
    assert set(parse_requirements(pex_info["requirements"])) == set(parse_requirements(constraints))


def test_entry_point(rule_runner: RuleRunner) -> None:
    entry_point = "pydoc"
    pex_info = create_pex_and_get_pex_info(rule_runner, entry_point=entry_point)
    assert pex_info["entry_point"] == entry_point


def test_interpreter_constraints(rule_runner: RuleRunner) -> None:
    constraints = PexInterpreterConstraints(["CPython>=2.7,<3", "CPython>=3.6"])
    pex_info = create_pex_and_get_pex_info(rule_runner, interpreter_constraints=constraints)
    assert set(pex_info["interpreter_constraints"]) == {str(c) for c in constraints}


def test_additional_args(rule_runner: RuleRunner) -> None:
    pex_info = create_pex_and_get_pex_info(rule_runner, additional_pex_args=("--not-zip-safe",))
    assert pex_info["zip_safe"] is False


def test_platforms(rule_runner: RuleRunner) -> None:
    # We use Python 2.7, rather than Python 3, to ensure that the specified platform is
    # actually used.
    platforms = PexPlatforms(["linux-x86_64-cp-27-cp27mu"])
    constraints = PexInterpreterConstraints(["CPython>=2.7,<3", "CPython>=3.6"])
    pex_output = create_pex_and_get_all_data(
        rule_runner,
        requirements=PexRequirements(["cryptography==2.9"]),
        platforms=platforms,
        interpreter_constraints=constraints,
    )
    assert any(
        "cryptography-2.9-cp27-cp27mu-manylinux2010_x86_64.whl" in fp for fp in pex_output["files"]
    )
    assert not any("cryptography-2.9-cp27-cp27m-" in fp for fp in pex_output["files"])
    assert not any("cryptography-2.9-cp35-abi3" in fp for fp in pex_output["files"])

    # NB: Platforms override interpreter constraints.
    assert pex_output["info"]["interpreter_constraints"] == []


def test_additional_inputs(rule_runner: RuleRunner) -> None:
    # We use pex's --preamble-file option to set a custom preamble from a file.
    # This verifies that the file was indeed provided as additional input to the pex call.
    preamble_file = "custom_preamble.txt"
    preamble = "#!CUSTOM PREAMBLE\n"
    additional_inputs = rule_runner.request_product(
        Digest, [CreateDigest([FileContent(path=preamble_file, content=preamble.encode())])]
    )
    additional_pex_args = (f"--preamble-file={preamble_file}",)
    pex_output = create_pex_and_get_all_data(
        rule_runner, additional_inputs=additional_inputs, additional_pex_args=additional_pex_args
    )
    with zipfile.ZipFile(pex_output["local_path"], "r") as zipfp:
        with zipfp.open("__main__.py", "r") as main:
            main_content = main.read().decode()
    assert main_content[: len(preamble)] == preamble
