from __future__ import annotations

import argparse

import ollama


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull a local Ollama model with progress output.")
    parser.add_argument("model")
    parser.add_argument("--host", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    client = ollama.Client(host=args.host)
    last_status = None
    for progress in client.pull(args.model, stream=True):
        status = progress.status
        if status != last_status:
            print(status, flush=True)
            last_status = status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

