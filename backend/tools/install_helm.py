"""Install a pinned, checksum-verified Helm binary without invoking a shell."""

import argparse
import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = "4.3.2"
CHECKSUMS = {
    "darwin-amd64": "9e0068e9ae18c441d27388190ee7f53ae65eba44bfe910b40bdac44c4b51fa71",
    "linux-amd64": "d1c0278d46beab79582d7e50f5b3dc206944ba8e577cde618513fd07daa53983",
    "linux-arm64": "df31cb61680ef728f82c770e5280dd565617bee6b039d37e85c677288be4bbb0",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True)
    parser.add_argument("--os", default=platform.system().lower())
    parser.add_argument(
        "--arch",
        default={"x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine(), platform.machine()),
    )
    args = parser.parse_args()
    target = f"{args.os}-{args.arch}"
    if target not in CHECKSUMS:
        raise SystemExit("Unsupported platform; install Helm 4.3.2 from its official release")
    with urllib.request.urlopen(f"https://get.helm.sh/helm-v{VERSION}-{target}.tar.gz", timeout=60) as response:
        archive = response.read(40 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != CHECKSUMS[target]:
        raise SystemExit("Helm archive checksum did not match the pinned release")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        member = bundle.getmember(f"{target}/helm")
        if not member.isfile() or member.size > 150 * 1024 * 1024:
            raise SystemExit("Invalid Helm binary archive")
        stream = bundle.extractfile(member)
        assert stream is not None
        binary = stream.read()
    destination = Path(args.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".installing")
    temporary.write_bytes(binary)
    temporary.chmod(0o755)
    os.replace(temporary, destination)
    print(f"Installed checksum-verified Helm {VERSION}")


if __name__ == "__main__":
    main()
