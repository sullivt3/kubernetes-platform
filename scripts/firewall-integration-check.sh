#!/usr/bin/env bash
set -euo pipefail
mkdir -p /run/dbus /run/lock
dbus-daemon --system --fork
firewalld --nofork --nopid > /tmp/firewalld.log 2>&1 &
for _ in $(seq 1 60); do
  if firewall-cmd --state >/dev/null 2>&1; then break; fi
  sleep 1
done
firewall-cmd --state || { cat /tmp/firewalld.log; exit 1; }
# The daemon and D-Bus are real; only systemd's service-status probe is adapted.
cat > /usr/local/bin/systemctl <<'STATUS'
#!/bin/sh
[ "$*" = 'is-active --quiet firewalld' ] && firewall-cmd --state >/dev/null
STATUS
chmod +x /usr/local/bin/systemctl
ip link add kptestbr0 type bridge
firewall-cmd --zone=public --add-interface=kptestbr0
firewall-cmd --zone=public --add-port=2222/tcp
export FIREWALL_CONFIG_B64
FIREWALL_CONFIG_B64="$(python3 - <<'PY'
import base64,json
print(base64.b64encode(json.dumps({'cluster_id':'12345678-1234-4567-8123-123456789abc',
'pod_cidr':'10.244.0.0/16','role':'worker','cni':'calico','peers':['192.168.18.11']}).encode()).decode())
PY
)"
run_installer() { (source /installer/lib.sh; source /installer/firewall.sh); }
run_installer
run_installer | tee /tmp/second-run.log
grep -q 'already verified' /tmp/second-run.log
firewall-cmd --zone=public --query-interface=kptestbr0
firewall-cmd --zone=public --query-port=2222/tcp
if firewall-cmd --permanent --zone=public --query-port=2222/tcp; then
  echo 'Unrelated runtime state was unexpectedly persisted' >&2; exit 1
fi
firewall-cmd --policy=kh-123456781234 --query-rich-rule='rule family="ipv4" source address="192.168.18.11/32" protocol value="4" accept'
firewall-cmd --permanent --policy=kp-123456781234 --query-rich-rule='rule family="ipv4" source address="10.244.0.0/16" destination address="10.244.0.0/16" accept'
# Exercise an existing policy update, as happens when another node joins.
FIREWALL_CONFIG_B64="$(python3 - <<'PYCONFIG'
import base64,json,os
config=json.loads(base64.b64decode(os.environ['FIREWALL_CONFIG_B64']))
config['peers']=['192.168.18.12']
print(base64.b64encode(json.dumps(config).encode()).decode())
PYCONFIG
)"
run_installer
firewall-cmd --policy=kh-123456781234 --query-rich-rule='rule family="ipv4" source address="192.168.18.12/32" protocol value="4" accept'
if firewall-cmd --policy=kh-123456781234 --query-rich-rule='rule family="ipv4" source address="192.168.18.11/32" protocol value="4" accept'; then
  echo 'Departed peer rule was retained' >&2; exit 1
fi
export MODE=remove
run_installer
run_installer
if firewall-cmd --get-policies | tr ' ' '\n' | grep -Eq '^(kh|kp)-123456781234$'; then
  echo 'Owned policies remain after removal' >&2; exit 1
fi
firewall-cmd --zone=public --query-interface=kptestbr0
firewall-cmd --zone=public --query-port=2222/tcp
echo 'Real firewalld integration check passed: apply, retry, runtime preservation and cleanup'
