import os
import sys
from pathlib import Path
import base64

root_path = Path(sys.argv[0]).resolve().parent

os.system(
    "powershell -Command python -m nuitka --standalone --onefile --mingw64"
    " --onefile-tempdir-spec='{TEMP}\\DIST_REQUESTER'"
    " --file-version=0.1 --product-version=0.1"
    " --assume-yes-for-downloads --output-filename=Core"
    " --remove-output Main.py"
)

with (root_path / "Core.exe").open("rb") as f:
    data = f.read()
encoded = base64.b64encode(data).decode("utf-8")

with (root_path / "do.py").open("w") as f:
    f.write(
        f"""
import sys
import subprocess
import base64
from pathlib import Path

root_path = Path(sys.argv[0]).resolve().parent

core_exe_data = "{encoded}"

if not (root_path / "Core.exe").exists():
    print("creating Core.exe from base64 data...")
    (root_path / "Core.exe").write_bytes(base64.b64decode(core_exe_data))

if not (root_path / "config.json").exists():
    print("creating config.json...")
    (root_path / "config.json").write_text(
        r'''
{{
    "version": "0.1",
    "tasks": [],
    "client": {{
        "server_url": "http:\\/\\/boom.harryh.cn",
        "report": {{
            "live_report_interval": 10
        }}
    }}
}} 
'''
    )

subprocess.run(
    ["Core.exe", "--client", "config.json"],
)
"""
    )

(root_path / "Core.exe").unlink()

os.system(
    "powershell -Command python -m nuitka --standalone --onefile --mingw64"
    " --onefile-tempdir-spec='{TEMP}\\DIST_REQUESTER'"
    " --file-version=0.1 --product-version=0.1"
    " --assume-yes-for-downloads --output-filename=JustDoIt"
    " --remove-output do.py"
)

(root_path / "do.py").unlink()
