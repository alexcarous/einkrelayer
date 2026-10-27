"""Command line entry point for einkrelayer."""

import argparse
import json
import os
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from einkrelayer import __version__


def main(argv=None):
    from einkrelayer.diagnostics import clean, configure

    configure()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build a book or digest from reading sources")
    build.add_argument(
        "sources", nargs="*", help="Public URLs or saved HTML/PDF/text/Markdown/DOCX files"
    )
    build.add_argument("--title")
    build.add_argument(
        "--contents", help="URL or saved HTML with an explicit bounded contents list"
    )
    build.add_argument("--mode", choices=["book", "digest"], default="book")
    build.add_argument(
        "--answer-keys",
        action="store_true",
        help="Move each article's answer section into a linked chapter after it",
    )
    build.add_argument("--output", type=Path)
    validate = commands.add_parser("validate", help="Validate EPUB structure and references")
    validate.add_argument("epub", type=Path)
    review = commands.add_parser("review", help="Render four reading layouts")
    review.add_argument("epub", type=Path)
    review.add_argument("--output", type=Path, default=Path("output/review"))
    commands.add_parser("send", help="Preview/send EPUBs using .env", add_help=False)
    run = commands.add_parser("run", help="Read the mailbox, then build and send due digests")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Build previews in output/preview; change no mail, state, or cache",
    )
    run.add_argument("--send-now", metavar="READER", help="Send this reader's digest now")
    queue = commands.add_parser("queue", help="List queued links")
    queue.add_argument("reader", nargs="?")
    history = commands.add_parser("history", help="List recent digest deliveries")
    history.add_argument("--limit", type=int, default=20)
    resend = commands.add_parser("resend", help="Send a stored digest again")
    resend.add_argument("delivery", type=int)
    resend.add_argument("--send", action="store_true", help="Actually send it")
    resolve = commands.add_parser("resolve", help="Settle an uncertain delivery")
    resolve.add_argument("delivery", type=int)
    resolve.add_argument("outcome", choices=["sent", "failed"])
    commands.add_parser("check", help="Check relay settings, mailbox and SMTP logins")
    args, remaining = parser.parse_known_args(argv)
    if args.command != "send" and remaining:
        parser.error("unrecognized arguments: " + " ".join(remaining))
    try:
        if args.command == "build":
            if not args.sources and not args.contents:
                parser.error("Supply sources or --contents")
            if args.sources and args.contents:
                parser.error("Use explicit sources or --contents, not both")
            from dotenv import dotenv_values

            from einkrelayer.books.compose import compose
            from einkrelayer.capture.collections import discover
            from einkrelayer.capture.http import write_json
            from einkrelayer.capture.readers import read
            from einkrelayer.models import BookJob

            settings = {**dotenv_values(".env", interpolate=False), **os.environ}
            sources = discover(args.contents) if args.contents else args.sources
            if len(sources) > 100:
                parser.error("A book can contain at most 100 starting documents")
            documents = [read(source) for source in sources]
            if args.title:
                title = args.title
            elif args.mode == "digest":
                title = (settings.get("DIGEST_TITLE") or "").strip() or "einkrelayer Digest"
            else:
                title = documents[0].title if len(documents) == 1 else "Collected readings"
            destination = args.output or Path(
                "output/digest.epub" if args.mode == "digest" else "output/book.epub"
            )
            job = BookJob(title, documents, mode=args.mode, answer_keys=args.answer_keys)
            result = compose(job, destination)
            if args.contents:
                result["contents_source"] = args.contents
                result["selection"] = "Explicit contents order; one-level discovery"
                write_json(destination.with_suffix(".report.json"), result)
            print(json.dumps(result, indent=2))
        elif args.command == "validate":
            from einkrelayer.books.validation import validate

            validate(args.epub)
            print(json.dumps({"epub": str(args.epub), "valid": True}, indent=2))
        elif args.command == "review":
            from einkrelayer.books.review import review

            return review(args.epub, args.output)
        elif args.command == "send":
            from einkrelayer.delivery.smtp import main as send

            send(remaining)
        elif args.command == "check":
            from einkrelayer.relay.tools import check

            results = check()
            for passed, line in results:
                print(("ok    " if passed else "FAIL  ") + line)
            return 0 if all(passed for passed, _ in results) else 1
        else:
            return relay(args)
    except (ValueError, OSError, BadZipFile, ParseError, KeyError, RuntimeError) as error:
        parser.exit(1, f"Error: {clean(error)}\n")
    return 0


def relay(args):
    from einkrelayer.relay import config, tools

    settings = config.load()
    if args.command == "run":
        from einkrelayer.relay.run import run

        results = run(settings, dry_run=args.dry_run, send_now=args.send_now)
        return 1 if any(r in {"failed", "uncertain"} for r in results.values()) else 0
    if args.command == "queue":
        lines = tools.queue(settings, args.reader)
    elif args.command == "history":
        lines = tools.history(settings, args.limit)
    elif args.command == "resend":
        lines = [tools.resend(settings, args.delivery, send=args.send)]
    else:
        lines = [tools.resolve(settings, args.delivery, args.outcome)]
    print("\n".join(lines))
    return 0
