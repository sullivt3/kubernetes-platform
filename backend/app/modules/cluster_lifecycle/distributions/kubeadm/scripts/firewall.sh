# Reconcile cluster-owned firewalld policies, preserving unrelated runtime state.
# Env: FIREWALL_CONFIG_B64 (validated cluster/node/network JSON), MODE (apply|remove)
if ! systemctl is-active --quiet firewalld 2>/dev/null; then
  log "firewalld is inactive; leaving firewall services unchanged"
  exit 0
fi
[ -x /usr/bin/python3 ] || fail "firewalld policy management requires system Python 3"
/usr/bin/python3 - <<'KPLATFORM_FIREWALL_PY'
import base64
import fcntl
import ipaddress
import json
import os
import subprocess
import uuid

from firewall.client import FirewallClient, FirewallClientPolicySettings

config = json.loads(base64.b64decode(os.environ['FIREWALL_CONFIG_B64'], validate=True))
cluster_id = str(uuid.UUID(config['cluster_id']))
pod_network = ipaddress.IPv4Network(config['pod_cidr'])
if pod_network.prefixlen == 0:
    raise RuntimeError('Refusing an unrestricted pod network')
peers = sorted({str(ipaddress.IPv4Address(ip)) for ip in config['peers']})
mode = os.environ.get('MODE', 'apply')
if mode not in ('apply', 'remove'):
    raise RuntimeError('Invalid firewall operation')
role = config['role']
cni = config['cni']
if role not in ('control_plane', 'worker') or cni not in ('calico', 'flannel'):
    raise RuntimeError('Unsupported firewall role or CNI')
owner = 'kubernetes-platform cluster ' + cluster_id
prefix = uuid.UUID(cluster_id).hex[:12]
ports = [('10250', 'tcp')]
ports += ([('6443', 'tcp'), ('2379-2380', 'tcp'), ('10257', 'tcp'), ('10259', 'tcp')]
          if role == 'control_plane' else [('30000-32767', 'tcp')])
ports += ([('179', 'tcp'), ('4789', 'udp'), ('5473', 'tcp')]
          if cni == 'calico' else [('8472', 'udp')])
host_rules = ([f'rule family="ipv4" source address="{ip}/32" protocol value="4" accept'
               for ip in peers] if cni == 'calico' else [])
desired = {
    'kh-' + prefix: FirewallClientPolicySettings({
        'description': owner, 'target': 'CONTINUE', 'priority': -1,
        'ingress_zones': ['ANY'], 'egress_zones': ['HOST'],
        'ports': ports, 'rich_rules': host_rules,
    }),
    'kp-' + prefix: FirewallClientPolicySettings({
        'description': owner, 'target': 'CONTINUE', 'priority': -1,
        'ingress_zones': ['ANY'], 'egress_zones': ['ANY'],
        'rich_rules': [f'rule family="ipv4" source address="{pod_network}" '
                       f'destination address="{pod_network}" accept'],
    }),
}

# Serialize reload/snapshot/restore across clusters sharing a host.
with open('/run/lock/kplatform-firewall.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    client = FirewallClient()
    permanent = client.config()
    zones = {z: client.getZoneSettings(z) for z in client.getZones()}
    runtime = {p: client.getPolicySettings(p) for p in client.getPolicies()}
    names = set(permanent.getPolicyNames())
    original = {p: permanent.getPolicyByName(p).getSettings() if p in names else None for p in desired}
    # Inspect ownership of both copies before making any change.
    for p, old in original.items():
        for settings in (old, runtime.get(p)):
            if settings is not None and settings.getDescription() != owner:
                raise RuntimeError('Refusing to modify a firewall policy owned by someone else: ' + p)

    def equivalent(a, b):
        return a is not None and a.getSettingsDict() == b.getSettingsDict()

    changed = any((original[p] is not None or p in runtime) if mode == 'remove'
                  else not equivalent(original[p], settings) or not equivalent(runtime.get(p), settings)
                  for p, settings in desired.items())
    if not changed:
        print('Cluster firewall policies already verified (' + mode + ')', flush=True)
    else:
        if set(runtime) - names or set(zones) - set(permanent.getZoneNames()):
            raise RuntimeError('Runtime-only zones/policies cannot be safely reloaded; persist them before retrying')
        if names - set(runtime) - set(desired):
            raise RuntimeError('Unrelated pending permanent policies must be reviewed before reloading')
        if set(client.listServices()) != set(permanent.getServiceNames()) or set(client.getIPSets()) != set(permanent.getIPSetNames()):
            raise RuntimeError('Runtime/permanent service or IP-set objects differ; review them before reloading')
        for zone, settings in zones.items():
            saved = permanent.getZoneByName(zone).getSettings().getSettingsDict()
            current = settings.getSettingsDict()
            if any(current.get(k) != saved.get(k) for k in ('version', 'short', 'description', 'target')):
                raise RuntimeError('Unrelated pending permanent zone metadata must be reviewed: ' + zone)
        for policy, settings in runtime.items():
            if policy not in desired:
                saved = permanent.getPolicyByName(policy).getSettings().getSettingsDict()
                current = settings.getSettingsDict()
                if any(current.get(k) != saved.get(k) for k in ('version', 'short', 'description', 'target')):
                    raise RuntimeError('Unrelated pending permanent policy metadata must be reviewed: ' + policy)
        # Reload would flush direct runtime-only rules. Refuse rather than lose them.
        for query in ('--get-all-chains', '--get-all-rules', '--get-all-passthroughs'):
            def direct(extra):
                return subprocess.check_output(['firewall-cmd', *extra, '--direct', query], text=True, timeout=30)
            if direct([]) != direct(['--permanent']):
                raise RuntimeError('Runtime-only direct firewall rules must be persisted before retrying')

        def restore_runtime(include_managed=False):
            # Restore interfaces, services, ports, sources, rich rules and other
            # runtime zone settings without persisting unrelated temporary state.
            for zone, settings in zones.items():
                client.setZoneSettings(zone, settings)
            for policy, settings in runtime.items():
                if include_managed or policy not in desired:
                    client.setPolicySettings(policy, settings)

        try:
            for policy, settings in desired.items():
                if mode == 'remove':
                    if original[policy] is not None:
                        permanent.getPolicyByName(policy).remove()
                elif original[policy] is None:
                    permanent.addPolicy(policy, settings)
                elif not equivalent(original[policy], settings):
                    permanent.getPolicyByName(policy).update(settings)
            subprocess.run(['firewall-cmd', '--check-config'], check=True, timeout=60)
            print('Activating cluster firewall policies; preserving runtime zones and VM bindings', flush=True)
            client.reload()
            restore_runtime()
            for policy, settings in desired.items():
                if mode == 'remove':
                    if policy in client.getPolicies() or policy in permanent.getPolicyNames():
                        raise RuntimeError('Firewall policy removal did not complete: ' + policy)
                elif not equivalent(client.getPolicySettings(policy), settings):
                    raise RuntimeError('Runtime firewall policy verification failed: ' + policy)
            for zone, settings in zones.items():
                if client.getZoneSettings(zone).getSettingsDict() != settings.getSettingsDict():
                    raise RuntimeError('Runtime firewall zone was not preserved: ' + zone)
            print('Cluster firewall policies verified at runtime and on disk (' + mode + ')', flush=True)
        except BaseException:
            print('Firewall update failed; restoring previous owned policies and runtime settings', flush=True)
            current = set(permanent.getPolicyNames())
            for policy, old in original.items():
                if old is None and policy in current:
                    permanent.getPolicyByName(policy).remove()
                elif old is not None:
                    if policy in current:
                        permanent.getPolicyByName(policy).update(old)
                    else:
                        permanent.addPolicy(policy, old)
            try:
                client.reload()
            finally:
                restore_runtime(include_managed=True)
            raise
KPLATFORM_FIREWALL_PY
