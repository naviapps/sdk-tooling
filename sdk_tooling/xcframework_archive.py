"""Archive verified native frameworks from a committed SDK build."""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .artifacts import build_lock, file_sha256, write_json
from .consumers.workspace import _git
from .xcframework import xcframeworks

INDEX = "xcframeworks.json"


def _check_source(root, expected, products):
    revision = _git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or expected not in (None, revision):
        raise ValueError("Archive requires the exact committed SDK revision")
    exclusions = [
        ":(top,exclude,literal)" + path.relative_to(root).as_posix()
        for product in products
        for path in (product.artifact, product.receipt)
    ]
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", ".", *exclusions):
        raise ValueError("SDK source outside declared generated artifacts must be clean")
    return revision


def _verify_products(root, products, producer):
    if producer is None:
        for product in products:
            product.verify()
        return
    runner = producer.resolve(strict=True) / "ci/run_sdk_tooling.py"
    if runner.is_symlink() or not runner.is_file():
        raise ValueError("Producer must be an SDK Tooling checkout")
    subprocess.run(
        [sys.executable, "-I", "-B", runner, "xcframework", "--verify", root],
        cwd=root,
        check=True,
    )


def _member_name(path, parent, seen):
    name = path.relative_to(parent).as_posix()
    key = unicodedata.normalize("NFC", name).casefold()
    if (
        key in seen
        or any(part.endswith((".", " ")) for part in name.split("/"))
        or any(char in name for char in ("\\", ":"))
        or any(ord(char) < 32 for char in name)
    ):
        raise ValueError(f"Invalid or aliased XCFramework member: {name!r}")
    seen.add(key)
    return name


def _write_archive(bundle, output):
    inventory, seen = {}, set()
    with ZipFile(output, "x") as archive:
        for member in sorted([bundle, *bundle.rglob("*")]):
            mode = member.lstat().st_mode
            if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                raise ValueError(f"Unexpected XCFramework permissions: {member.name}")
            member_name = _member_name(member, bundle.parent, seen)
            info = ZipInfo(member_name + ("/" if stat.S_ISDIR(mode) else ""))
            info.create_system = 3
            info.external_attr = mode << 16
            if stat.S_ISDIR(mode):
                info.external_attr |= 0x10
                archive.writestr(info, b"")
            elif stat.S_ISLNK(mode):
                link = os.readlink(member)
                if Path(link).is_absolute() or not member.resolve(strict=True).is_relative_to(
                    bundle
                ):
                    raise ValueError(f"XCFramework link escapes its bundle: {member_name}")
                data = os.fsencode(link)
                archive.writestr(info, data)
                inventory[member] = hashlib.sha256(b"symlink:" + data).hexdigest()
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                info.compress_type = ZIP_DEFLATED
                info.compress_level = 9
                with member.open("rb") as source, archive.open(info, "w") as destination:
                    while data := source.read(1024 * 1024):
                        destination.write(data)
                        digest.update(data)
                inventory[member] = digest.hexdigest()
            else:
                raise ValueError(f"Unexpected XCFramework file type: {member_name}")
    return inventory


def _archive_product(product, receipt, stage):
    archive = stage / (product.artifact.name + ".zip")
    archived = {
        path.relative_to(product.root).as_posix(): digest
        for path, digest in _write_archive(product.artifact, archive).items()
    }
    if archived != json.loads(receipt)["outputs"] or product.receipt.read_bytes() != receipt:
        raise ValueError(f"Native artifact changed during archive: {product.module}")
    staged_receipt = stage / product.receipt.name
    with staged_receipt.open("xb") as target:
        target.write(receipt)

    def metadata(path):
        return {"file": path.name, "bytes": path.stat().st_size, "sha256": file_sha256(path)}

    return metadata(archive) | {"receipt": metadata(staged_receipt)}


def archive_xcframeworks(root, output, *, revision=None, producer=None):
    """Archive native products only after verification against their SDK source."""
    root = Path(root).resolve(strict=True)
    products = tuple(xcframeworks(root).values())
    output = Path(output)
    output = output.parent.resolve() / output.name
    if output.is_relative_to(root) or output.exists() or output.is_symlink():
        raise ValueError("Archive output must be a new directory outside the SDK checkout")
    with build_lock(root / ".native-build"):
        revision = _check_source(root, revision, products)
        names = {INDEX.casefold()}
        receipts = []
        for product in products:
            if (
                not product.artifact.is_dir()
                or product.artifact.is_symlink()
                or not product.receipt.is_file()
                or product.receipt.is_symlink()
                or product.artifact.parent.is_symlink()
            ):
                raise ValueError("Expected ordinary generated frameworks and receipts in Artifacts")
            for name in (product.artifact.name + ".zip", product.receipt.name):
                if name.casefold() in names:
                    raise ValueError("Duplicate or aliased native archive output: " + name)
                names.add(name.casefold())
            receipts.append(product.receipt.read_bytes())
        _verify_products(root, products, producer)
        with tempfile.TemporaryDirectory(dir=output.parent, prefix="sdk-framework-") as temporary:
            stage = Path(temporary) / "archive"
            stage.mkdir()
            artifacts = {
                product.module: _archive_product(product, receipt, stage)
                for product, receipt in zip(products, receipts, strict=True)
            }
            write_json(stage / INDEX, {"schema": 1, "revision": revision, "artifacts": artifacts})
            _check_source(root, revision, products)
            if output.exists() or output.is_symlink():
                raise ValueError("Archive output already exists")
            stage.rename(output)
    return output / INDEX


def archive_command(args, *, root):
    print(archive_xcframeworks(root, args.output, revision=args.revision, producer=args.producer))
