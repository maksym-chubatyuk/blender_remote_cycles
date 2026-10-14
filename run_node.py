"""Run a render node without a Blender window.

    blender -b --factory-startup --python run_node.py -- [options]

Options (after the "--"):
    --master HOST[:PORT]  join this master instead of discovering one
    --join NAME           with discovery, join the master with this name
    --name NAME           name shown on the master (default: host name)
    --cpu                 render on the CPU only
    --local               this is the master machine's own render process
    --once                exit when the master goes away instead of rejoining
    --score N             skip the benchmark and report this score (testing)
    --bench-samples N     samples for the benchmark render (testing)
"""

import argparse
import importlib.util
import os
import socket
import sys
import time

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))


def load_addon():
    """Import this folder as the add-on package and register it."""
    spec = importlib.util.spec_from_file_location(
        "remote_cycles", os.path.join(HERE, "__init__.py"), submodule_search_locations=[HERE])
    rc = importlib.util.module_from_spec(spec)
    sys.modules["remote_cycles"] = rc
    spec.loader.exec_module(rc)
    bpy.context.preferences.addons.new().module = "remote_cycles"
    rc.register()
    return rc


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(prog="run_node.py")
    p.add_argument("--master")
    p.add_argument("--join")
    p.add_argument("--name", default=socket.gethostname())
    p.add_argument("--cpu", action="store_true")
    p.add_argument("--local", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--score", type=float)
    p.add_argument("--bench-samples", type=int)
    args = p.parse_args(argv)

    rc = load_addon()
    bench = {"bench_samples": args.bench_samples} if args.bench_samples else {}
    while True:
        if args.master:
            host, _, port = args.master.partition(":")
            port = int(port) if port else rc.protocol.CLUSTER_PORT
        else:
            print("[remote-cycles node] looking for a master on the network...", flush=True)
            found = None
            while found is None:
                for m in rc.ui.state["listener"].masters():
                    if args.join in (None, m["name"]):
                        found = m
                        break
                time.sleep(0.5)
            host, port = found["address"], found["port"]
        try:
            rc.node.run_headless(host, port, args.name, cpu_only=args.cpu,
                                 score=args.score, local=args.local, **bench)
        except (OSError, ConnectionError) as e:
            print(f"[remote-cycles node] cannot join {host}:{port}: {e}", flush=True)
        if args.once:
            break
        time.sleep(2)


main()
