"""Graphiti knowledge graph (embedded Kuzu database, Anthropic for extraction, Voyage for embeddings).
Runs in its own venv (/opt/graphiti) as a separate process; never imports perpetual_core.

    python graph_worker.py ingest   < [{"name", "body", "source", "at"}]      → {"added": n}
    python graph_worker.py search   < {"query", "limit"}                       → [{"fact", "valid_at", ...}]

Env: GRAPH_DB (path), ANTHROPIC_API_KEY, GRAPH_MODEL, VOYAGE_API_KEY.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

GROUP = "agency"


def _graphiti():
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.driver.kuzu_driver import KuzuDriver
    from graphiti_core.embedder.voyage import VoyageAIEmbedder, VoyageAIEmbedderConfig
    from graphiti_core.llm_client.anthropic_client import AnthropicClient
    from graphiti_core.llm_client.config import LLMConfig

    class KeepOrder(CrossEncoderClient):
        """No reranker service (the default needs OpenAI): keep the search's own order."""
        async def rank(self, query, passages):
            n = len(passages)
            return [(p, 1.0 - i / max(n, 1)) for i, p in enumerate(passages)]

    model = os.environ.get("GRAPH_MODEL") or "claude-haiku-5-5"
    return Graphiti(
        graph_driver=KuzuDriver(db=os.environ["GRAPH_DB"]),
        llm_client=AnthropicClient(config=LLMConfig(api_key=os.environ["ANTHROPIC_API_KEY"], model=model,
                                                    small_model=model, temperature=0), max_tokens=8192),
        embedder=VoyageAIEmbedder(VoyageAIEmbedderConfig(api_key=os.environ["VOYAGE_API_KEY"],
                                                         embedding_model="voyage-3.5-lite", embedding_dim=1024)),
        cross_encoder=KeepOrder(),
        max_coroutines=2,
    )


async def ingest(episodes):
    from graphiti_core.nodes import EpisodeType
    g = _graphiti()
    try:
        await g.build_indices_and_constraints()
        added, failed = 0, []
        for e in episodes:
            try:
                await g.add_episode(name=e["name"], episode_body=e["body"], source_description=e.get("source") or "memory",
                                    reference_time=datetime.fromisoformat(e["at"]), source=EpisodeType.text,
                                    group_id=GROUP)
                added += 1
            except Exception as ex:  # noqa: BLE001 — one bad episode must not stop the night
                failed.append({"name": e["name"], "error": str(ex)[:200]})
        return {"added": added, "failed": failed}
    finally:
        await g.close()


async def search(query, limit):
    g = _graphiti()
    try:
        edges = await g.search(query, group_ids=[GROUP], num_results=limit)
        iso = lambda d: d.isoformat() if d else None  # noqa: E731
        return [{"fact": e.fact, "valid_at": iso(e.valid_at), "invalid_at": iso(e.invalid_at),
                 "created_at": iso(e.created_at), "still_true": e.invalid_at is None or e.invalid_at > datetime.now(timezone.utc)}
                for e in edges]
    finally:
        await g.close()


def main():
    cmd = sys.argv[1]
    data = json.loads(sys.stdin.read() or "null")
    if cmd == "ingest":
        out = asyncio.run(ingest(data))
    elif cmd == "search":
        out = asyncio.run(search(data["query"], int(data.get("limit", 10))))
    else:
        raise SystemExit(f"unknown command {cmd}")
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
