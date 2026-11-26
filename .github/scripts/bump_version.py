"""Bump the manifest's patch version unless the merge that just landed already changed it.

Run on the commit that landed on main. Compares the manifest with the one in the previous main commit
(HEAD^1): if the version is the same, bump the patch (1.3.1 -> 1.3.2) in place. A hand-made minor or
major bump in the PR is left alone. Prints the resulting version.
"""
import json
import re
import subprocess

MANIFEST = "custom_components/climote/manifest.json"

previous = json.loads(
    subprocess.run(["git", "show", f"HEAD^1:{MANIFEST}"], check=True, capture_output=True, text=True).stdout
)["version"]
text = open(MANIFEST).read()
current = json.loads(text)["version"]

if current == previous:
    major, minor, patch = (int(p) for p in current.split("."))
    current = f"{major}.{minor}.{patch + 1}"
    text = re.sub(r'("version":\s*")[^"]+(")', rf"\g<1>{current}\g<2>", text, count=1)
    open(MANIFEST, "w").write(text)
    print(f"bumped {previous} -> {current}")
else:
    print(f"version already changed by the PR: {previous} -> {current}")
print(f"VERSION={current}")
