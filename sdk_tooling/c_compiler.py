"""Compile C ABI callers with Clang or MSVC."""

import subprocess
from pathlib import Path


def compile_c(
    source,
    *,
    output,
    compiler,
    env,
    cwd,
    include_dirs=(),
    libraries=(),
    standard="c17",
    flags=(),
    style="clang",
    runner=None,
):
    """Compile/link one SDK-owned C caller; execution and fixture arguments stay local."""
    output = Path(output)
    if not output.is_absolute():
        output = (Path(cwd) / output).resolve()
    if style == "clang":
        arguments = ["-std=" + standard, "-Wall", "-Wextra", "-Werror", *flags]
        for directory in include_dirs:
            arguments += ["-I", directory]
        arguments += [source, *libraries, "-o", output]
    elif style == "msvc":
        arguments = ["/nologo", "/W4", "/WX", "/utf-8", "/std:" + standard, *flags]
        arguments += ["/I" + str(directory) for directory in include_dirs]
        arguments += [
            source,
            *libraries,
            "/Fo:" + str(output.with_suffix(".obj")),
            "/Fe:" + str(output),
        ]
    else:
        raise ValueError("Unknown C compiler style: " + style)
    if not compiler or isinstance(compiler, (str, bytes)):
        raise ValueError("Provide the compiler as an executable/argument sequence")
    output.parent.mkdir(parents=True, exist_ok=True)
    (runner or subprocess.run)(
        [str(value) for value in (*compiler, *arguments)], cwd=cwd, env=dict(env), check=True
    )
    return output
