"""Run Mac database export and verified cloud import with account-scoped secrets."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", type=Path, required=True)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--replace-pid", type=int)
    parser.add_argument("--import-only", action="store_true")
    args = parser.parse_args()
    scripts = Path(__file__).resolve().parents[2] / "windows"
    sys.path.insert(0, str(scripts))
    import export_snapshot as es

    from .config import Config
    from .dedupe import repair
    from .sources.staging import StagingPackage
    from .store.oss import ObjectStore
    from .store.tablestore import TableStore

    with tempfile.TemporaryDirectory(prefix="wechat-direct-sync-") as directory:
        out = Path(directory) / "staging"
        if args.resume_from:
            src = args.resume_from.resolve()
            temp = Path(tempfile.gettempdir()).resolve()
            if not src.is_relative_to(temp) or not src.parent.name.startswith(
                ("wechat-mac-import-", "wechat-direct-sync-")
            ):
                raise ValueError("Resume must refer to this task's temporary staging directory")
            shutil.copytree(src, out)
            if args.replace_pid:
                os.kill(args.replace_pid, signal.SIGTERM)
                os.kill(args.replace_pid, signal.SIGCONT)
                # Source ownership and temporary parent were validated above.
                shutil.rmtree(src.parent)
                print("[direct] resumed existing export with updated importer", flush=True)
        code = (
            0
            if args.import_only
            else es.main(
                [
                    "--db-storage",
                    str(args.account_root / "db_storage"),
                    "--out",
                    str(out),
                    "--full",
                    "--workers",
                    "4",
                    "--ffprobe",
                    "none",
                ]
            )
        )
        if code:
            raise RuntimeError(f"Export failed ({code}); import stopped")
        manifest_path = out / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source"] = "mac"
        manifest["notes"] = "Mac direct database snapshot; media archived separately"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        if es.main(["--out", str(out), "--verify", "--ffprobe", "none"]):
            raise RuntimeError("Export verification failed")
        config = Config.from_env()
        config.require_cloud()
        store, oss = TableStore(config), ObjectStore(config)
        from concurrent.futures import ThreadPoolExecutor

        contact_path = out / "contacts.jsonl"
        if contact_path.exists():
            contacts = [json.loads(line) for line in contact_path.read_text().splitlines()]
            with ThreadPoolExecutor(max_workers=16) as pool:
                chunks = [contacts[i : i + 100] for i in range(0, len(contacts), 100)]
                list(
                    pool.map(
                        lambda chunk: store.put_contacts(manifest["account_id"], chunk), chunks
                    )
                )
            print("[direct] contacts imported", len(contacts), flush=True)
        print("[direct] importing", manifest["counts"], flush=True)
        from .import_staging import import_staging

        result = import_staging(
            StagingPackage(out), store, None, config=config, no_media=True, workers=32
        )
        print("[direct] import result", result, flush=True)
        if result["errors"]:
            raise RuntimeError("Some messages failed to import")
        duplicate_result = repair(store, oss, apply=False)
        summary = {
            "source": "mac",
            "export": manifest["counts"],
            "import": result,
            "dedupe": duplicate_result,
            "completed_at": int(time.time()),
        }
        oss.bucket.put_object(
            "manifests/" + args.account_root.name + "/messages-latest.json",
            json.dumps(summary).encode(),
            headers={"Content-Type": "application/json"},
        )
        print("MAC_IMPORT_COMPLETE", json.dumps(summary), flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
