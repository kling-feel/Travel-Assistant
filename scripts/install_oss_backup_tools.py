"""Install pinned cloud backup tools after checking vendor SHA256 digests."""

import hashlib
import os
from pathlib import Path
import tarfile
import tempfile
import urllib.request
import zipfile


TOOLS = (
    ("age", "https://github.com/FiloSottile/age/releases/download/v1.3.2/"
     "age-v1.3.2-linux-amd64.tar.gz",
     "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10",
     "age/age"),
    ("ossutil", "https://gosspublic.alicdn.com/ossutil/v2/2.4.1/"
     "ossutil-2.4.1-linux-amd64.zip",
     "57ee4c56796a40144acd6b0b96b53b4ba75e5cec09c01d8d0313c09a5524022f",
     "ossutil-2.4.1-linux-amd64/ossutil"),
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "travel-backup-setup/1"})
    with urllib.request.urlopen(request, timeout=60) as source, target.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            output.write(chunk)


def install(name: str, url: str, expected: str, member: str) -> str:
    destination = Path("/usr/local/bin") / name
    if destination.exists():
        raise RuntimeError(f"Refusing to overwrite existing {name}")
    with tempfile.TemporaryDirectory(prefix="backup-tools-") as folder:
        archive = Path(folder) / Path(url).name
        download(url, archive)
        if digest(archive) != expected:
            raise RuntimeError(f"Vendor checksum differs for {name}")
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as source, source.open(member) as binary:
                data = binary.read()
        else:
            with tarfile.open(archive, "r:gz") as source:
                entry = source.extractfile(member)
                if entry is None:
                    raise RuntimeError(f"Vendor archive lacks {name}")
                data = entry.read()
        if len(data) < 100000 or len(data) > 100000000:
            raise RuntimeError(f"Vendor binary size is unexpected for {name}")
        temporary = destination.with_name("." + name + ".new")
        with temporary.open("xb") as output:
            output.write(data)
        os.chmod(temporary, 0o755)
        os.replace(temporary, destination)
    return name + " installed from a checksum-verified vendor release"


def main() -> None:
    if os.name != "posix" or os.geteuid() != 0 or os.uname().machine != "x86_64":
        raise RuntimeError("Run as root on the x86_64 cloud host")
    for item in TOOLS:
        print(install(*item), flush=True)


if __name__ == "__main__":
    main()
