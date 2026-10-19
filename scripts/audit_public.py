"""Inspect the Git index and every reachable commit before publication."""

import re
import subprocess
import sys
from pathlib import PurePosixPath


ALLOWED_SUFFIXES = {".py", ".swift", ".md", ".toml", ".yml", ".yaml", ".html", ".css", ".js"}
ALLOWED_NAMES = {".gitignore", ".env.example", "LICENSE"}
PATTERNS = [
    rb"/" + rb"Users/[^\s/]+/",
    rb"/" + rb"home/[^\s/]+/",
    rb"gh" + rb"[opusr]_[A-Za-z0-9]{20,}",
    rb"github" + rb"_pat_[A-Za-z0-9_]{20,}",
    rb"sk" + rb"-[A-Za-z0-9_-]{20,}",
    rb"AI" + rb"za[A-Za-z0-9_-]{25,}",
    rb"-----BEGIN " + rb"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
]


def git(*args):
    return subprocess.check_output(["git", *args])


def audit():
    errors = []
    checked = set()

    def inspect(name, oid, location):
        key = (name, oid)
        if key in checked:
            return
        checked.add(key)
        path = PurePosixPath(name)
        if path.name not in ALLOWED_NAMES and path.suffix not in ALLOWED_SUFFIXES:
            errors.append(f"{location}: 公開許可外のファイル形式 {name}")
        if any(part in {"runtime", "private", ".audit", ".venv"} for part in path.parts):
            errors.append(f"{location}: 非公開ディレクトリ {name}")
        blob = git("cat-file", "blob", oid)
        if len(blob) > 512_000:
            errors.append(f"{location}: 大きすぎるファイル {name}")
        try:
            blob.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{location}: バイナリデータ {name}")
        for pattern in PATTERNS:
            if re.search(pattern, blob):
                errors.append(f"{location}: 秘密情報または実ユーザーパスの疑い {name}")

    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, oid, stage = meta.decode().split()
        if mode not in {"100644", "100755"} or stage != "0":
            errors.append(f"index: 特殊ファイルまたは未解決の競合 {name.decode()}")
        else:
            inspect(name.decode(), oid, "index")
    for commit in git("rev-list", "--all").decode().splitlines():
        for entry in git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if entry:
                meta, name = entry.split(b"\t", 1)
                mode, kind, oid = meta.decode().split()
                if kind != "blob" or mode not in {"100644", "100755"}:
                    errors.append(f"{commit[:8]}: 特殊ファイル {name.decode()}")
                else:
                    inspect(name.decode(), oid, commit[:8])
        message = git("show", "-s", "--format=%B", commit)
        if any(re.search(pattern, message) for pattern in PATTERNS):
            errors.append(f"{commit[:8]}: コミットメッセージに非公開データの疑い")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"PASS: index と全到達可能コミットを検査しました。{len(checked)}ファイル版。")
    print("自動検査は任意の個人情報を完全には検出しません。git diff --cached と git log も目視してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(audit())
