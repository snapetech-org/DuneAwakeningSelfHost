#!/usr/bin/env python3
"""Replace only the vendored Python 3.9 packaging files from a pinned wheel."""

import pathlib
import shutil
import sys
import zipfile

SITE = pathlib.Path(
    "/home/dune/server/Engine/Binaries/ThirdParty/Python3/Linux/lib/python3.9/site-packages"
)
REPLACE = ("setuptools", "_distutils_hack", "pkg_resources", "distutils-precedence.pth")


def main(wheel: pathlib.Path) -> None:
    if not SITE.is_dir():
        raise SystemExit("embedded Python site-packages is missing")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        roots = {name.split("/", 1)[0] for name in names if name and not name.endswith("/")}
        dist_info = "setuptools-80.10.2.dist-info"
        if roots != set(REPLACE) | {dist_info}:
            raise SystemExit(f"unexpected wheel contents: {sorted(roots)}")
        if any(".." in pathlib.PurePosixPath(name).parts or name.startswith("/") for name in names):
            raise SystemExit("unsafe wheel path")
        for name in REPLACE:
            target = SITE / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
        for target in SITE.glob("setuptools-*.dist-info"):
            if target.is_dir():
                shutil.rmtree(target)
        archive.extractall(SITE)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: upgrade-embedded-setuptools.py WHEEL")
    main(pathlib.Path(sys.argv[1]))
