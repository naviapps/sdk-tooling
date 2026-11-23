FIXTURE_PACKAGE = {
    "name": "fixture-ffi",
    "version": "0.1.0",
    "source": "workspace",
}
FIXTURE_UNIT = {
    **FIXTURE_PACKAGE,
    "targetName": "fixture",
    "kind": ["cdylib"],
    "compilation": "target",
    "features": [],
}


def fixture_dependencies(*targets):
    return {
        "roots": ["fixture-ffi"],
        "targets": sorted(targets),
        "packages": [{**FIXTURE_PACKAGE, "features": {target: [] for target in targets}}],
    }
