"""Install checksum-pinned kubectl for the isolated runtime image."""

import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

VERSION = "1.37.2"
CHECKSUMS = {
    "amd64": "c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4",
    "arm64": "fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509",
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arch", required=True, choices=CHECKSUMS)
    p.add_argument("--destination", required=True)
    args = p.parse_args()
    with urlopen(f"https://dl.k8s.io/release/v{VERSION}/bin/linux/{args.arch}/kubectl", timeout=60) as response:
        binary = response.read(100 * 1024 * 1024)
    if hashlib.sha256(binary).hexdigest() != CHECKSUMS[args.arch]:
        raise SystemExit("kubectl checksum differs from the pinned official release")
    target = Path(args.destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(binary)
    target.chmod(0o755)


if __name__ == "__main__":
    main()
