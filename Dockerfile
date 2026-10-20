# Perpetual X Bot — the agency bot. ONE container serves every client: Hermes' multiplexed
# gateway runs the HQ agent (default profile) plus one profile per client in clients.yaml,
# all behind one Slack app. reconcile.py sets everything up on each boot.
FROM nousresearch/hermes-agent:latest

USER root
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
# Our Python deps and shared code live beside Hermes' venv, never inside it.
RUN uv pip install --quiet --python /opt/hermes/.venv/bin/python --target /opt/perpetual/pylib \
        "psycopg[binary]>=3.2,<4"
COPY lib/perpetual_core/ /opt/perpetual/pylib/perpetual_core/
COPY hermes-template/ /opt/perpetual/hermes-template/
COPY CRON_VERSION clients.yaml docker/reconcile.py /opt/perpetual/
COPY --chmod=0755 docker/perpetual-init.sh /etc/cont-init.d/03-perpetual

CMD ["gateway", "run"]
