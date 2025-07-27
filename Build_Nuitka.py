# -*- coding: utf-8 -*-
# Copyright (c) 2025, DLmaster_361
# @ MIT License
import os
import sys
import base64
import toml
from pathlib import Path


if __name__ == "__main__":
    root_path = Path(sys.argv[0]).resolve().parent

    config = toml.load("pyproject.toml")

    version = config["project"]["version"]

    os.system(
        "powershell -Command python -m nuitka --standalone --onefile --mingw64"
        " --onefile-tempdir-spec='{TEMP}\\DIST_REQUESTER'"
        f" --file-version={version} --product-version={version}"
        " --assume-yes-for-downloads --output-filename=Core"
        " --remove-output Main.py"
    )

    with (root_path / "Core.exe").open("rb") as f:
        binary_data = f.read()
    encoded_binary = base64.b64encode(binary_data).decode("utf-8")

    with (root_path / "config.json").open("rb") as f:
        default_config = f.read()
    encoded_base64 = base64.b64encode(default_config).decode("utf-8")

    with (root_path / "do.py").open("w") as f:
        f.write(
            f"""
    import sys
    import subprocess
    import base64
    from pathlib import Path

    root_path = Path(sys.argv[0]).resolve().parent

    core_exe_data = "{encoded_binary}"
    config_data = "{encoded_base64}"

    if not (root_path / "Core.exe").exists():
        print("creating Core.exe from base64 data...")
        (root_path / "Core.exe").write_bytes(base64.b64decode(core_exe_data))

    if not (root_path / "config.json").exists():
        print("creating config.json...")
        (root_path / "config.json").write_bytes(base64.b64decode(config_data))

    subprocess.run(["Core.exe", "--client", "config.json"])
    """
        )

    os.system(
        "powershell -Command python -m nuitka --standalone --onefile --mingw64"
        " --onefile-tempdir-spec='{TEMP}\\DIST_REQUESTER'"
        " --file-version=0.1 --product-version=0.1"
        " --assume-yes-for-downloads --output-filename=JustDoIt"
        " --remove-output do.py"
    )

    (root_path / "Core.exe").unlink()
    (root_path / "do.py").unlink()
