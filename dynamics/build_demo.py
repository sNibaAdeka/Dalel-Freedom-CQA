#!/usr/bin/env python3
"""Build an isolated static T50 example; never mutate web/index.html or copy datasets."""
import argparse
import json
from pathlib import Path
import shutil
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HOST_FILES = (
    "embed-demo.html", "embed-demo.css", "embed-demo.js", "bridge.js", "bootstrap.js",
)


def http_url(value: str, *, origin: bool = False) -> str:
    url = urlsplit(value)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (url.scheme != "https" and not (url.scheme == "http" and local)) or not url.netloc:
        raise argparse.ArgumentTypeError("Use HTTPS, or HTTP on localhost only")
    if url.username or url.password or url.query or url.fragment:
        raise argparse.ArgumentTypeError("Credentials, query and fragment are not allowed")
    if origin and (url.path or value != f"{url.scheme}://{url.netloc}"):
        raise argparse.ArgumentTypeError("Origin must not contain a path or trailing slash")
    return value.rstrip("/")


def embed_html(source: str) -> str:
    # Main owns the one conditional pre-boot hook. Only rebase its two imports for /web/.
    imports = ("import('./dynamics/bootstrap.js')", "import('./dynamics/deployment.js')")
    if source.count(".applyBootstrap(") != 1 or any(source.count(item) != 1 for item in imports):
        raise ValueError("Expected one integrated applyBootstrap hook and two dynamics imports; review index.html")
    for item in imports:
        source = source.replace(item, item.replace("'./dynamics/", "'../dynamics/"))
    return source


def build(args: argparse.Namespace) -> Path:
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError("Output must be a new directory; choose a new --output (nothing overwritten)")
    if output == ROOT or ROOT in output.parents:
        raise ValueError("Keep generated output outside this source checkout, for example /tmp/dalel-t50-demo")
    source = (ROOT / "web/index.html").read_text(encoding="utf-8")
    transformed = embed_html(source)
    settings = {
        "parentOrigins": args.parent_origin,
        "apiBases": args.api_base,
        "framePath": args.frame_url or "../web/index.html",
        "defaultRole": args.role,
        "defaultConvId": args.conv_id,
        "mock": args.mode == "demo",
    }
    # Publish only static app assets. No web/mocks, data, reports, env, Python or Git metadata.
    assets = [ROOT / "web/app.css"]
    for folder, suffix in (("js", ".js"), ("css", ".css")):
        assets.extend(sorted((ROOT / "web" / folder).glob(f"*{suffix}")))
    assets.extend(ROOT / "dynamics" / name for name in HOST_FILES)
    if any(file.is_symlink() or not file.is_file() for file in assets):
        raise ValueError("Missing or symlinked asset; refusing to build")
    output.mkdir(parents=True)
    for file in assets:
        destination = output / file.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, destination)
    (output / "web/index.html").write_text(transformed, encoding="utf-8")
    (output / "dynamics/deployment.js").write_text(
        "// Trusted deployment configuration. No secrets.\nexport const deployment = Object.freeze(" +
        json.dumps(settings, ensure_ascii=True, indent=2) + ");\n", encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parent-origin", action="append", type=lambda value: http_url(value, origin=True))
    parser.add_argument("--api-base", action="append", type=http_url)
    parser.add_argument("--frame-url", type=http_url, help="Optional external iframe URL for a cross-origin rehearsal")
    parser.add_argument("--role", choices=("operator", "reviewer"), default="operator")
    parser.add_argument("--conv-id", default="demo-ru-001")
    parser.add_argument("--mode", choices=("demo", "live"), default="demo")
    args = parser.parse_args()
    args.parent_origin = args.parent_origin or ["http://127.0.0.1:8050"]
    args.api_base = args.api_base or ["http://127.0.0.1:8047/api"]
    try:
        output = build(args)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"Built static example: {output}")
    print("Serve only this output directory. Open /dynamics/embed-demo.html on a configured parent origin.")
    print("Mode: " + args.mode + "; Dynamics 365 deployment NOT performed.")


if __name__ == "__main__":
    main()
