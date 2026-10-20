#!/command/with-contenv bash
# Perpetual X Bot — container init (/etc/cont-init.d/03-perpetual). Runs after Hermes' own
# setup has seeded /opt/data and before the gateway starts. All the work is in reconcile.py.
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
    exec s6-setuidgid hermes /opt/hermes/.venv/bin/python /opt/perpetual/reconcile.py
fi
exec /opt/hermes/.venv/bin/python /opt/perpetual/reconcile.py
