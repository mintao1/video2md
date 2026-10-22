#!/usr/bin/env python3
"""Install/check the Bilibili-only home route; never changes the OS proxy."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import socket
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="lcode-4c", help="Existing trusted SSH host alias")
    parser.add_argument("--check", action="store_true", help="Read-only service/listener check")
    args = parser.parse_args()
    if args.host.startswith("-") or any(c.isspace() for c in args.host):
        parser.error("host must be an SSH alias")
    root = Path.home() / ".config/video2md-bilibili-home"
    binary = Path("/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo")
    labels = {
        "com.linmintao.video2md-bilibili-home": [str(binary), "-d", str(root), "-f", str(root / "config.yaml")],
        "com.linmintao.video2md-bilibili-tunnel": [
            "/usr/bin/ssh", "-N", "-R", "127.0.0.1:17895:127.0.0.1:17895",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", args.host,
        ],
    }
    domain = f"gui/{os.getuid()}"
    states = {}
    if not args.check:
        if not binary.is_file():
            parser.error("Install Clash Verge from its official source first")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        source = Path(__file__).resolve().parents[1] / "deploy/bilibili-home-proxy.yaml"
        config = root / "config.yaml"
        if config.exists() and config.read_bytes() != source.read_bytes():
            parser.error("Existing egress config differs; inspect it before replacing")
        config.write_bytes(source.read_bytes())
        subprocess.run([str(binary), "-t", "-d", str(root), "-f", str(config)], check=True)
    for label, program in labels.items():
        loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                                capture_output=True, check=False).returncode == 0
        if not args.check:
            plist = Path.home() / "Library/LaunchAgents" / (label + ".plist")
            values = {"Label": label, "ProgramArguments": program, "RunAtLoad": True,
                      "KeepAlive": True, "ThrottleInterval": 10,
                      "StandardOutPath": str(root / (label + ".log")),
                      "StandardErrorPath": str(root / (label + ".log"))}
            if plist.exists() and plistlib.loads(plist.read_bytes()) != values:
                parser.error(f"Existing {label} differs; inspect it before replacing")
            plist.write_bytes(plistlib.dumps(values))
            if not loaded:
                subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
                loaded = True
        states[label] = loaded
    with socket.socket() as client:
        client.settimeout(2)
        listening = client.connect_ex(("127.0.0.1", 17895)) == 0
    print(json.dumps({"services_loaded": states, "loopback_listener": listening}))
    if not all(states.values()) or not listening:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
