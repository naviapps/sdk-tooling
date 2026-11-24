"""Run, build, pack and verify restored .NET projects."""

import json
import os
import re
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from .workspaces import temporary_workspace

# The .NET SDK can append machine-local sources and caches after reading NuGet.Config.
DISABLE_IMPLICIT_RESTORE = {
    "DisableImplicitLibraryPacksFolder": "true",
    "DisableImplicitNuGetFallbackFolder": "true",
}

RESTORE_OVERRIDES = {
    *(name.casefold() for name in DISABLE_IMPLICIT_RESTORE),
    "nuget_fallback_packages",
    "nuget_packages",
    "nugetpackagefolders",
    "nugetpackageroot",
    "restoreadditionalprojectfallbackfolders",
    "restoreadditionalprojectsources",
    "restoreconfigfile",
    "restorefallbackfolders",
    "restorepackagespath",
    "restoresources",
}


def isolated_restore_settings(env, cache):
    """Use explicit restore sources and a caller-owned fresh package cache."""
    clean = {key: value for key, value in env.items() if key.casefold() not in RESTORE_OVERRIDES}
    cache = str(cache)
    # An empty fallback setting re-enables the folders from NuGet.Config.
    return clean, {
        **DISABLE_IMPLICIT_RESTORE,
        "RestorePackagesPath": cache,
        "RestoreFallbackFolders": cache,
        "RestoreAdditionalProjectSources": "",
        "RestoreAdditionalProjectFallbackFolders": "",
        "RestoreNoHttpCache": "true",
    }


def pinned_sdk_version(root):
    """Read the exact SDK selected by the checkout's own .NET version file."""
    config = Path(root) / "global.json"
    try:
        sdk = json.loads(config.read_bytes())["sdk"]
        version = sdk["version"]
        if (
            sdk.get("rollForward") == "disable"
            and isinstance(version, str)
            and re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*)?", version
            )
        ):
            return version
    except (KeyError, OSError, TypeError, ValueError):
        pass
    raise ValueError("Declare an exact .NET SDK version with rollForward=disable in global.json")


def subprocess_environment(environment=None):
    return dict(
        os.environ if environment is None else environment,
        DOTNET_CLI_TELEMETRY_OPTOUT="1",
        DOTNET_NOLOGO="1",
        DOTNET_CLI_USE_MSBUILD_SERVER="0",
        MSBUILDDISABLENODEREUSE="1",
    )


def run_command(argv, *, cwd, env, runner, **options):
    """Run an explicit .NET command, preserving the caller's environment."""
    return (runner or subprocess.run)(
        [str(value) for value in argv], cwd=cwd, env=dict(env), check=True, **options
    )


def _properties(properties):
    return ["-p:" + name + "=" + str(value) for name, value in (properties or {}).items()]


def _prepare_managed(
    dotnet,
    project,
    *,
    env,
    cwd,
    properties,
    nuget_config,
    sources,
    runner,
    configuration="Release",
    no_restore=False,
    verify_style=False,
):
    restore = nuget_config is not None or bool(sources) or (verify_style and not no_restore)
    if restore:
        command = [dotnet, "restore", project]
        if nuget_config is not None:
            command += ["--configfile", nuget_config]
        for source in sources:
            command += ["--source", source]
        restore_properties = (
            {"Configuration": configuration, **(properties or {})} if verify_style else properties
        )
        run_command([*command, *_properties(restore_properties)], cwd=cwd, env=env, runner=runner)
    if verify_style:
        verify_managed(
            dotnet,
            project,
            env=env,
            cwd=cwd,
            properties=properties,
            configuration=configuration,
            runner=runner,
        )
    return ["--no-restore"] if restore or no_restore else []


def verify_managed(
    dotnet, project, *, env, cwd, properties=None, configuration="Release", runner=None
):
    """Verify restored source projects and their selected project references.

    Format has no MSBuild property switch. Supply the selected values through its
    environment and reject projects that override them, rather than checking a
    different configuration. Query references with MSBuild, not XML path guesses.
    Neither evaluation nor formatting restores packages or runs the assembly.
    """
    selected = {"Configuration": configuration, **(properties or {})}
    selected = {name: str(value) for name, value in selected.items()}
    environment = {
        key: value
        for key, value in env.items()
        if key.casefold() not in {name.casefold() for name in selected}
    }
    environment.update(selected)
    environment.update(DOTNET_CLI_UI_LANGUAGE="en", LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
    project = Path(project)
    if not project.is_absolute():
        project = Path(cwd) / project
    pending, checked = [project.resolve()], set()
    while pending:
        project = pending.pop(0)
        if project.is_dir():
            candidates = list(project.glob("*.csproj"))
            if len(candidates) != 1:
                raise ValueError("Select one managed project: " + str(project))
            project = candidates[0]
        if project in checked:
            continue
        checked.add(project)
        print("Checking restored managed source: " + str(project), flush=True)
        evaluation = run_command(
            [
                dotnet,
                "msbuild",
                project,
                "-nologo",
                "-getProperty:" + ",".join(selected),
                "-getItem:ProjectReference",
            ],
            cwd=cwd,
            env=environment,
            runner=runner,
            capture_output=True,
            text=True,
        )
        values = json.loads(evaluation.stdout)
        actual = {name.casefold(): value for name, value in values["Properties"].items()}
        for name, value in selected.items():
            if actual.get(name.casefold()) != value:
                raise ValueError(
                    f"Managed style verification cannot retain {name}={value} in {project}"
                )
        for reference in values["Items"]["ProjectReference"]:
            for name in (
                "AdditionalProperties",
                "GlobalPropertiesToRemove",
                "SetConfiguration",
                "SetPlatform",
                "SetTargetFramework",
            ):
                if reference.get(name):
                    raise ValueError(
                        f"Managed style verification cannot retain reference {name} in {project}"
                    )
            pending.append(Path(reference["FullPath"]).resolve())
        try:
            result = run_command(
                [
                    dotnet,
                    "format",
                    "style",
                    project,
                    "--no-restore",
                    "--verify-no-changes",
                    "--severity",
                    "info",
                    "--diagnostics",
                    "IDE0005",
                    "IDE0051",
                    "IDE0052",
                    "IDE0059",
                    "IDE0060",
                    "--verbosity",
                    "normal",
                ],
                cwd=cwd,
                env=environment,
                runner=runner,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as error:
            print(error.stdout or "", end="", flush=True)
            print(error.stderr or "", file=sys.stderr, end="", flush=True)
            raise
        print(result.stdout or "", end="", flush=True)
        print(result.stderr or "", file=sys.stderr, end="", flush=True)
        if "Warnings were encountered while loading the workspace" in (
            (result.stdout or "") + (result.stderr or "")
        ):
            raise ValueError("Managed style workspace did not load cleanly: " + str(project))


def run_managed(
    dotnet,
    project,
    *,
    env,
    cwd,
    arguments=(),
    properties=None,
    nuget_config=None,
    sources=(),
    runtime=None,
    no_restore=False,
    configuration="Release",
    verify_style=False,
    runner=None,
):
    """Run an SDK project, optionally restoring from explicitly selected sources first.

    verify_style checks the restored source graph before execution. Without
    no_restore it makes the project's normal restore explicit, then disables
    implicit restore for execution. Other callers retain their restore policy.
    """
    properties = dict(properties or {})
    if runtime is not None:
        properties["RuntimeIdentifier"] = runtime
    restore_flags = _prepare_managed(
        dotnet,
        project,
        env=env,
        cwd=cwd,
        properties=properties,
        nuget_config=nuget_config,
        sources=sources,
        configuration=configuration,
        no_restore=no_restore,
        verify_style=verify_style,
        runner=runner,
    )
    command = [dotnet, "run", "--project", project, "--configuration", configuration]
    command += [*restore_flags, *_properties(properties)]
    if arguments:
        command += ["--", *arguments]
    return run_command(command, cwd=cwd, env=env, runner=runner)


def build_managed(
    dotnet,
    project,
    *,
    env,
    cwd,
    properties=None,
    nuget_config=None,
    sources=(),
    configuration="Release",
    no_restore=False,
    verify_style=False,
    runner=None,
):
    """Build once so a caller can stage native assets before executing an assembly.

    verify_style shares run_managed's restore and source verification policy.
    """
    restore_flags = _prepare_managed(
        dotnet,
        project,
        env=env,
        cwd=cwd,
        properties=properties,
        nuget_config=nuget_config,
        sources=sources,
        configuration=configuration,
        no_restore=no_restore,
        verify_style=verify_style,
        runner=runner,
    )
    command = [dotnet, "build", project, "--configuration", configuration]
    return run_command(
        [*command, *restore_flags, *_properties(properties)], cwd=cwd, env=env, runner=runner
    )


def pack_project(
    dotnet,
    project,
    *,
    output,
    env,
    cwd,
    properties=None,
    nuget_config=None,
    sources=(),
    configuration="Release",
    no_build=False,
    no_restore=False,
    runner=None,
):
    """Pack using the project's rules, isolating restores from explicit sources."""
    output = Path(output)
    if not output.is_absolute():
        output = (Path(cwd) / output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with ExitStack() as cleanup:
        if sources:
            cache = cleanup.enter_context(temporary_workspace(None, prefix="nuget-restore-"))
            env, restore = isolated_restore_settings(env, cache.resolve())
            properties = (properties or {}) | restore
        restore_flags = _prepare_managed(
            dotnet,
            project,
            env=env,
            cwd=cwd,
            properties=properties,
            nuget_config=nuget_config,
            sources=sources,
            no_restore=no_restore or no_build,
            runner=runner,
        )
        command = [dotnet, "pack", project, "--configuration", configuration]
        if no_build:
            command += ["--no-build"]
        command += [*restore_flags, "--output", output, *_properties(properties)]
        return run_command(command, cwd=cwd, env=env, runner=runner)
