#!/usr/bin/env python3
"""Small localhost-only HTTP service that keeps a SentenceTransformer in memory."""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def main() -> int:
    args = parse_args()

    from sentence_transformers import SentenceTransformer

    started = time.perf_counter()
    print(f"Loading embedding model: {args.model} ({args.device})", flush=True)
    model = SentenceTransformer(args.model, device=args.device)
    dimension = int(model.get_sentence_embedding_dimension())
    model_lock = threading.Lock()
    load_seconds = time.perf_counter() - started

    class Handler(BaseHTTPRequestHandler):
        server_version = "BGEResident/1.0"

        def send_json(self, status: int, value: Any) -> None:
            body = json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health":
                self.send_json(404, {"error": "not_found"})
                return
            self.send_json(
                200,
                {
                    "status": "ok",
                    "model": args.model,
                    "device": args.device,
                    "dimension": dimension,
                    "load_seconds": round(load_seconds, 3),
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/embed":
                self.send_json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                texts = payload.get("texts")
                if not isinstance(texts, list) or not texts or len(texts) > 32:
                    raise ValueError("texts must be a non-empty list with at most 32 items")
                if not all(isinstance(text, str) and text.strip() for text in texts):
                    raise ValueError("each text must be a non-empty string")

                encode_started = time.perf_counter()
                with model_lock:
                    vectors = model.encode(
                        texts,
                        convert_to_numpy=True,
                        normalize_embeddings=True,
                        show_progress_bar=False,
                    )
                self.send_json(
                    200,
                    {
                        "model": args.model,
                        "dimension": dimension,
                        "encode_seconds": round(time.perf_counter() - encode_started, 4),
                        "vectors": vectors.tolist(),
                    },
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:  # Keep the service alive and report the failed request.
                self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, format: str, *values: Any) -> None:
            print(
                f"{self.client_address[0]} [{self.log_date_time_string()}] "
                f"{format % values}",
                flush=True,
            )

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"BGE ready: http://{args.host}:{args.port} "
        f"(dimension={dimension}, load={load_seconds:.2f}s)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping BGE service...", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
