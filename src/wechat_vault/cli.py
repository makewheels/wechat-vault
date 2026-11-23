"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import replace

from . import verify
from .config import Config, ConfigError
from .import_staging import import_staging
from .sources.staging import StagingPackage
from .store.oss import ObjectStore
from .store.tablestore import TableStore
from .sync import process_media_tasks, sync_mac


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechat-vault", description="微信聊天归档：WeFlow -> Tablestore + OSS")
    parser.add_argument("--source", default="", help="源标识，默认取 WECHAT_VAULT_SOURCE 或 mac")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="创建 wx_* 表（幂等）")
    p_init.add_argument("--dry-run", action="store_true")

    p_import = sub.add_parser("import-staging", help="导入 Windows 暂存包")
    source = p_import.add_mutually_exclusive_group(required=True)
    source.add_argument("--dir", help="本地 wechat-export-win 目录")
    source.add_argument("--oss-prefix", help="OSS 暂存前缀，如 staging/win/<run_id>")
    p_import.add_argument("--limit", type=int, default=None, help="最多导入多少条消息（冒烟）")
    p_import.add_argument("--no-media", action="store_true", help="只导消息，不碰媒体")
    p_import.add_argument("--dry-run", action="store_true", help="只读，不写云")

    p_sync = sub.add_parser("sync", help="从 WeFlow 增量/全量同步（Mac）")
    p_sync.add_argument("--full", action="store_true", help="忽略水位，全量")
    p_sync.add_argument("--media", action="store_true", help="同步时一并处理媒体")
    p_sync.add_argument("--limit-convs", type=int, default=None)
    p_sync.add_argument("--conv", default=None, help="只同步一个 talker")
    p_sync.add_argument("--my-wxid", default=None, help="自己的 wxid（推断失败时用）")
    p_sync.add_argument("--page-size", type=int, default=200)

    p_media = sub.add_parser("media", help="处理 wx_media_tasks 里的待办媒体")
    p_media.add_argument("--limit", type=int, default=100)

    p_verify = sub.add_parser("verify", help="校验")
    p_verify.add_argument("--dup-msg-uid", action="store_true")
    p_verify.add_argument("--counts", action="store_true")
    p_verify.add_argument("--media", action="store_true")

    sub.add_parser("stats", help="各表行数")
    return parser


def _run_import(package: StagingPackage, config: Config, args) -> int:
    print(
        f"[import] {package.root} source={package.source} account={package.account_id}",
        flush=True,
    )
    stats = import_staging(
        package,
        None if args.dry_run else _store(config),
        None if args.dry_run or args.no_media else _oss(config),
        config=config,
        limit=args.limit,
        no_media=args.no_media,
        dry_run=args.dry_run,
    )
    print(f"[import] {stats}")
    return 0


def _store(config: Config) -> TableStore:
    config.require_cloud()
    return TableStore(config)


def _oss(config: Config) -> ObjectStore:
    config.require_cloud()
    return ObjectStore(config)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    config = Config.from_env()
    if args.source:
        config = replace(config, source=args.source)

    try:
        print(f"[config] {config.summary()}", flush=True)
        if args.command == "init":
            if args.dry_run:
                print("[init] dry-run: would create wx_* tables")
                return 0
            config.require_cloud()
            store = TableStore(config)
            created = store.init_tables()
            print(f"[init] created: {created or 'none (already exists)'}")
            oss = ObjectStore(config)
            try:
                info = oss.bucket.get_bucket_info()
                print(f"[init] bucket ok: {config.bucket} ({getattr(info, 'storage_class', '')})")
            except Exception as exc:
                print(f"[init] bucket check failed: {exc}", file=sys.stderr)
                return 2
            return 0

        if args.command == "import-staging":
            if args.oss_prefix:
                with tempfile.TemporaryDirectory(prefix="wechat-staging-") as tmp:
                    package = StagingPackage.from_oss(_oss(config), args.oss_prefix, tmp)
                    return _run_import(package, config, args)
            package = StagingPackage(args.dir)
            return _run_import(package, config, args)

        if args.command == "sync":
            store = _store(config)
            oss = _oss(config) if args.media else None
            stats = sync_mac(
                config,
                store,
                oss,
                full=args.full,
                include_media=args.media,
                limit_convs=args.limit_convs,
                talker=args.conv,
                my_wxid=args.my_wxid,
                page_size=args.page_size,
            )
            print(f"[sync] {stats}")
            return 0

        if args.command == "media":
            stats = process_media_tasks(config, _store(config), _oss(config), limit=args.limit)
            print(f"[media] {stats}")
            return 0

        if args.command == "verify":
            store = _store(config)
            ran = False
            if args.dup_msg_uid:
                verify.dup_msg_uid(store)
                ran = True
            if args.counts:
                verify.counts(store)
                ran = True
            if args.media:
                verify.media_stats(store)
                ran = True
            if not ran:
                verify.dup_msg_uid(store)
                verify.counts(store)
                verify.media_stats(store)
            return 0

        if args.command == "stats":
            verify.table_stats(_store(config))
            return 0

        parser.error("unknown command")
        return 1
    except ConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())