#!/usr/bin/env python3
"""
Magnet-to-Folder Downloader (Python 3)

General-purpose downloader for magnet links using libtorrent. Prints live
progress and stops after download completes (no prolonged seeding by default).

Notes:
- Use only with content you have the legal right to download.
- If running on Google Colab, you can manually mount Google Drive and pass
  a save path inside the mounted drive (e.g., /content/drive/My Drive/Torrent).
- Designed to work in a generic Linux environment as well as Colab.

Install libtorrent if needed (one of the following may work depending on env):
    pip install libtorrent
    # or on Ubuntu/Debian
    sudo apt-get update && sudo apt-get install -y python3-libtorrent

Example:
    python3 magnet_to_folder.py \
        --magnet "magnet:?xt=urn:btih:..." \
        --save-path "./downloads"
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download a magnet link to a folder using libtorrent. "
            "Use only with legally distributable content."
        )
    )

    parser.add_argument(
        "--magnet",
        "-m",
        type=str,
        default=None,
        help="Magnet URI. If omitted, you'll be prompted interactively.",
    )
    parser.add_argument(
        "--save-path",
        "-o",
        type=str,
        default=str(Path.cwd() / "torrents"),
        help="Directory to save downloaded data (will be created if missing).",
    )
    parser.add_argument(
        "--listen-start",
        type=int,
        default=6881,
        help="Start of listen port range (default: 6881)",
    )
    parser.add_argument(
        "--listen-end",
        type=int,
        default=6891,
        help="End of listen port range (default: 6891)",
    )
    parser.add_argument(
        "--seed-ratio",
        type=float,
        default=0.0,
        help=(
            "Stop seeding when this ratio is reached. 0 means stop immediately "
            "after download completes."
        ),
    )
    parser.add_argument(
        "--seed-time",
        type=int,
        default=0,
        help=(
            "Stop seeding after this many seconds once complete. 0 means do not "
            "wait for time; ratio takes precedence if both set."
        ),
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=1.0,
        help="Seconds between status updates (default: 1.0)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce verbosity of status output.",
    )

    return parser.parse_args(argv)


def try_import_libtorrent():
    try:
        import libtorrent as lt  # type: ignore
    except Exception as exc:  # pragma: no cover - import path differs by env
        print(
            "Error: libtorrent is not available in this environment.\n"
            "Try installing it first, for example:\n"
            "  pip install libtorrent\n"
            "or on Ubuntu/Debian:\n"
            "  sudo apt-get update && sudo apt-get install -y python3-libtorrent\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    return lt


def ensure_directory(path_str: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def format_eta(bytes_remaining: int, rate_bytes_per_sec: int) -> str:
    if rate_bytes_per_sec <= 0:
        return "∞"
    seconds = int(bytes_remaining / rate_bytes_per_sec)
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


class GracefulExit:
    def __init__(self) -> None:
        self._should_exit = False
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, signum, frame):  # noqa: ARG002 - standard handler signature
        self._should_exit = True

    @property
    def should_exit(self) -> bool:
        return self._should_exit


def create_session(lt, listen_start: int, listen_end: int):
    ses = lt.session()
    try:
        ses.listen_on(listen_start, listen_end)
    except Exception:
        # Fallback to default listen if the range is busy
        pass

    # Enable DHT for peer discovery
    try:
        ses.add_dht_router("router.bittorrent.com", 6881)
        ses.add_dht_router("router.utorrent.com", 6881)
        ses.add_dht_router("dht.transmissionbt.com", 6881)
        ses.start_dht()
    except Exception:
        # Some builds may not support DHT controls; ignore silently
        pass

    return ses


def add_magnet_to_session(lt, ses, magnet: str, save_path: Path):
    params = {"save_path": str(save_path)}
    try:
        handle = lt.add_magnet_uri(ses, magnet, params)
    except Exception as exc:
        print(f"Failed to add magnet URI: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return handle


def prompt_for_magnet() -> str:
    try:
        magnet = input("Enter Magnet Link: ").strip()
    except EOFError:
        magnet = ""
    if not magnet:
        print("No magnet provided. Exiting.", file=sys.stderr)
        raise SystemExit(3)
    return magnet


def run(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    lt = try_import_libtorrent()

    save_path = ensure_directory(args.save_path)

    magnet = args.magnet or prompt_for_magnet()
    if not magnet.startswith("magnet:"):
        print("Error: Provided value is not a magnet URI.", file=sys.stderr)
        return 4

    ses = create_session(lt, args.listen_start, args.listen_end)

    handle = add_magnet_to_session(lt, ses, magnet, save_path)

    if not args.quiet:
        print("Fetching metadata, please wait...")

    exit_signals = GracefulExit()

    # Wait for metadata
    while not exit_signals.should_exit and not handle.has_metadata():
        time.sleep(max(0.2, args.status_interval))
        if not args.quiet:
            print(".", end="", flush=True)

    if exit_signals.should_exit:
        if not args.quiet:
            print("\nInterrupted before metadata retrieval.")
        return 130

    if not args.quiet:
        print("\nMetadata received. Starting download...")

    state_str = [
        "queued",
        "checking",
        "downloading metadata",
        "downloading",
        "finished",
        "seeding",
        "allocating",
        "checking fastresume",
    ]

    start_time = time.time()
    last_print = 0.0

    while not exit_signals.should_exit and not handle.is_seed():
        s = handle.status()
        now = time.time()
        if now - last_print >= args.status_interval:
            last_print = now
            progress = s.progress * 100.0
            down_kbps = s.download_rate / 1000.0
            up_kbps = s.upload_rate / 1000.0
            peers = s.num_peers
            state = state_str[s.state] if 0 <= s.state < len(state_str) else str(s.state)

            total_wanted = getattr(s, "total_wanted", 0)
            total_done = getattr(s, "total_wanted_done", 0)
            bytes_remaining = max(0, int(total_wanted - total_done))
            eta = format_eta(bytes_remaining, int(s.download_rate))

            if args.quiet:
                print(f"{progress:6.2f}% | {down_kbps:8.2f} kB/s | peers: {peers} | {state}")
            else:
                print(
                    f"Downloading: {handle.name()}\n"
                    f"Progress:    {progress:.2f}%\n"
                    f"DL / UL:     {down_kbps:.2f} / {up_kbps:.2f} kB/s\n"
                    f"Peers:       {peers}\n"
                    f"ETA:         {eta}\n"
                    f"State:       {state}\n"
                )
        time.sleep(0.2)

    if exit_signals.should_exit:
        if not args.quiet:
            print("Interrupted. Attempting graceful shutdown...")
        try:
            ses.pause()
        except Exception:
            pass
        return 130

    # Re-check final status
    s = handle.status()
    elapsed = time.time() - start_time
    if not args.quiet:
        print(
            f"\nDownload Complete: {handle.name()}\n"
            f"Elapsed: {int(elapsed)}s\n"
            f"Saved to: {save_path}"
        )

    # Seeding policy
    if args.seed_ratio > 0 or args.seed_time > 0:
        if not args.quiet:
            print(
                f"Seeding until ratio >= {args.seed_ratio} or time >= {args.seed_time}s..."
            )
        seed_start = time.time()
        while not exit_signals.should_exit:
            s = handle.status()
            ratio = s.upload_payload_rate * 0.0  # placeholder to satisfy linters
            try:
                ratio = s.all_time_upload / max(1, s.all_time_download)
            except Exception:
                ratio = 0.0
            if args.seed_ratio > 0 and ratio >= args.seed_ratio:
                if not args.quiet:
                    print(f"Stopping seeding: ratio target reached ({ratio:.2f}).")
                break
            if args.seed_time > 0 and (time.time() - seed_start) >= args.seed_time:
                if not args.quiet:
                    print("Stopping seeding: time limit reached.")
                break
            if not args.quiet:
                print(
                    f"Seeding... UL: {s.upload_rate/1000.0:8.2f} kB/s | peers: {s.num_peers}"
                )
            time.sleep(max(1.0, args.status_interval))

    try:
        ses.pause()
    except Exception:
        pass

    return 0


def main() -> None:
    # Strongly discourage infringing use while keeping the tool general-purpose
    print(
        "Note: Use only legal torrents (e.g., Linux ISOs from official mirrors).",
        file=sys.stderr,
    )
    sys.exit(run())


if __name__ == "__main__":
    main()
