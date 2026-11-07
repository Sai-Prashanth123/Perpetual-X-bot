# Perpetual X Bot — the agency bot. ONE container serves every client: Hermes' multiplexed
# gateway runs the HQ agent (default profile) plus one profile per client in clients.yaml,
# all behind one Slack app. reconcile.py sets everything up on each boot.
FROM nousresearch/hermes-agent:latest

USER root
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
# Our Python deps and shared code live beside Hermes' venv, never inside it.
RUN uv pip install --quiet --python /opt/hermes/.venv/bin/python --target /opt/perpetual/pylib \
        "psycopg[binary]>=3.2,<4" "Pillow>=10.1" "edge-tts>=6.1"
# Free DuckDuckGo web search: Hermes' fallback when no paid search key has credit left.
RUN uv pip install --quiet --python /opt/hermes/.venv/bin/python "ddgs>=9" \
    || echo "ddgs not installed; web search needs a paid key"
# Microsoft MarkItDown (PDF / Word / PowerPoint / Excel / HTML -> text) in its own venv: run as a separate
# process per document, so its libraries never load into the bot.
RUN uv venv --quiet --python /opt/hermes/.venv/bin/python /opt/markitdown \
    && uv pip install --quiet --python /opt/markitdown/bin/python "markitdown[pdf,docx,pptx,xlsx,xls]>=0.1" \
    || echo "markitdown not installed; documents limited to plain text"
# Crawl4AI (light mode: plain HTTP, no browser) and Graphiti (embedded Kuzu graph) in their own venvs, run as
# separate processes; the Langfuse SDK for Hermes' official observability plugin.
RUN uv venv --quiet --python /opt/hermes/.venv/bin/python /opt/crawl4ai \
    && uv pip install --quiet --python /opt/crawl4ai/bin/python "crawl4ai>=0.7" \
    || echo "crawl4ai not installed; crawl_site falls back to Firecrawl"
RUN uv venv --quiet --python /opt/hermes/.venv/bin/python /opt/graphiti \
    && uv pip install --quiet --python /opt/graphiti/bin/python "graphiti-core[kuzu,anthropic,voyageai]" \
    || echo "graphiti not installed; connected memory off"
RUN uv pip install --quiet --python /opt/hermes/.venv/bin/python "langfuse>=3" || echo "langfuse SDK not installed"
# Quote cards and carousels need real fonts.
RUN (apt-get update -qq && apt-get install -y -qq --no-install-recommends fonts-dejavu-core \
     && rm -rf /var/lib/apt/lists/*) || echo "fonts-dejavu-core not installed; media falls back to the default font"
COPY lib/perpetual_core/ /opt/perpetual/pylib/perpetual_core/
COPY hermes-template/ /opt/perpetual/hermes-template/
COPY CRON_VERSION clients.yaml docker/reconcile.py /opt/perpetual/
COPY --chmod=0755 docker/perpetual-init.sh /etc/cont-init.d/03-perpetual

CMD ["gateway", "run"]
