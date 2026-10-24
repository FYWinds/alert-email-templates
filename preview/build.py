#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
# Copyright (c) 2026 FYWinds

"""Render every example scenario with the real amtool and argocd CLIs, and the
Proxmox templates with proxmox-render (proxmox-notify's handlebars setup).

    uv run preview/build.py                          # renders into site/
    uv run --no-project -m http.server -d site 8000  # then open http://localhost:8000

Also the CI gate: the example Alertmanager config must pass
`amtool check-config`, every scenario must render, and each Argo CD trigger
must fire for exactly the scenarios listed in TRIGGER_MATRIX. It regenerates
proxmox/{pve,pbs}/*-body.html.hbs; CI fails if those differ from the commit.

Needs the tools from preview/fetch-tools.sh (in preview/bin/); uv installs
the Python dependencies declared above.
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import yaml

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "preview" / "bin"
PROXMOX_RENDER = BIN / ("proxmox-render.exe" if os.name == "nt" else "proxmox-render")
AM_TEMPLATE = ROOT / "alertmanager" / "email.tmpl"
AM_EXAMPLE = ROOT / "alertmanager" / "examples" / "alertmanager.yml"
ARGO_CM = ROOT / "argocd" / "argocd-notifications-cm.yaml"
NOW = datetime.now(timezone.utc)
ZERO = "0001-01-01T00:00:00Z"
DARK_MQ = "@media (prefers-color-scheme: dark)"


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        sys.exit(f"command failed: {' '.join(map(str, cmd))}\n{r.stderr}{r.stdout}")
    return r.stdout


# --------------------------------------------------------------------------
# Alertmanager scenarios: template.Data as Alertmanager hands it to templates.

PROM = "https://prometheus.example.com"


def alert(status, labels, annotations, start, end=None, expr="up == 0"):
    return {
        "status": status,
        "labels": {"prometheus": "monitoring/k8s", **labels},
        "annotations": annotations,
        "startsAt": ago(start),
        "endsAt": ago(end) if end is not None else ZERO,
        "generatorURL": f"{PROM}/graph?g0.expr={quote_plus(expr)}&g0.tab=1",
        "fingerprint": os.urandom(8).hex(),
    }


def group(alerts, group_labels):
    common = dict(alerts[0]["labels"])
    for a in alerts[1:]:
        common = {k: v for k, v in common.items() if a["labels"].get(k) == v}
    return {
        "receiver": "email",
        "status": "firing" if any(a["status"] == "firing" for a in alerts) else "resolved",
        "alerts": alerts,
        "groupLabels": group_labels,
        "commonLabels": common,
        "commonAnnotations": {},
        "externalURL": "https://alertmanager.example.com",
    }


WAL = {
    "alertname": "PostgresWALArchiveLag", "namespace": "billing", "pod": "billing-db-1",
    "container": "postgres", "job": "billing/billing-db", "instance": "10.244.3.17:9187",
    "severity": "critical",
}
WAL_ANN = {
    "summary": "billing/billing-db-1 has WAL waiting and has not archived for 27m",
    "description": "pg_wal grows until the volume fills; check the archiver sidecar logs and object storage reachability.",
}
WAL_EXPR = 'pg_stat_archiver_seconds_since_last_archival > 900 and on (namespace, pod) pg_wal_archive_ready > 0'


def crash(pod, status="firing", start=48, end=None):
    return alert(status, {
        "alertname": "KubePodCrashLooping", "namespace": "storefront", "pod": pod, "container": "web",
        "job": "kube-state-metrics", "reason": "CrashLoopBackOff", "severity": "warning",
    }, {
        "summary": "Pod is crash looping.",
        "description": f'Pod storefront/{pod} (web) is in waiting state (reason: "CrashLoopBackOff").',
        "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodcrashlooping",
    }, start, end, expr='max_over_time(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}[5m]) >= 1')


def alertmanager_scenarios():
    wal_groups = {"alertname": "PostgresWALArchiveLag", "namespace": "billing"}
    return [
        ("am-critical", "Critical",
         group([alert("firing", WAL, WAL_ANN, 37, expr=WAL_EXPR)], wal_groups), {}),
        ("am-resolved", "Resolved",
         group([alert("resolved", WAL, WAL_ANN, 95, 3, expr=WAL_EXPR)], wal_groups), {}),
        ("am-group", "Grouped",
         group([crash("web-7f9c6d5b8-x2kqp"), crash("web-7f9c6d5b8-mm4zt", start=21),
                crash("web-7f9c6d5b8-a9wlc", "resolved", 130, 64)],
               {"alertname": "KubePodCrashLooping", "namespace": "storefront"}), {}),
        ("am-grafana", "Grafana links",
         group([alert("firing", {
             "alertname": "CertificateExpiringSoon", "namespace": "ingress", "certificate": "storefront-tls",
             "issuer": "letsencrypt", "severity": "warning",
         }, {"summary": "Certificate ingress/storefront-tls expires in 6 days",
             "description": "cert-manager has not renewed it; check the Certificate and Order events."}, 14,
             expr='certmanager_certificate_expiration_timestamp_seconds - time() < 7 * 86400')],
             {"alertname": "CertificateExpiringSoon", "namespace": "ingress"}),
         {"grafanaURL": "https://grafana.example.com"}),
        ("am-plain", "No severity",
         group([alert("firing", {"alertname": "HostClockSkew", "instance": "node-3:9100", "job": "node-exporter"},
                      {"summary": "Clock on node-3 is 1.8s off NTP"}, 63,
                      expr="abs(node_timex_offset_seconds) > 0.5")], {"alertname": "HostClockSkew"}), {}),
    ]


def render_alertmanager(tmpdir, data, settings):
    tmpl = AM_TEMPLATE.read_text(encoding="utf-8")
    for key, value in settings.items():
        tmpl, n = re.subn(rf'"{key}" "[^"]*"', f'"{key}" "{value}"', tmpl, count=1)
        if not n:
            sys.exit(f"setting {key} not found in {AM_TEMPLATE}")
    tmpl_path = tmpdir / "email.tmpl"
    tmpl_path.write_text(tmpl, encoding="utf-8")
    data_path = tmpdir / "data.json"
    data_path.write_text(json.dumps(data), encoding="utf-8")

    def r(kind, text):
        return run([BIN / "amtool", "template", "render", f"--template.glob={tmpl_path}",
                    f"--template.type={kind}", f"--template.data={data_path}", f"--template.text={text}"])

    return {
        "subject": r("text", '{{ template "mail.subject" . }}').strip(),
        "html": r("html", '{{ template "mail.html" . }}'),
        "text": r("text", '{{ template "mail.text" . }}'),
    }


def check_alertmanager_config(tmpdir):
    cfg = yaml.safe_load(AM_EXAMPLE.read_text(encoding="utf-8"))
    cfg["templates"] = [str(AM_TEMPLATE)]
    (tmpdir / "pw").write_text("x")
    cfg["global"]["smtp_auth_password_file"] = str(tmpdir / "pw")
    path = tmpdir / "alertmanager.yml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    run([BIN / "amtool", "check-config", path])


# --------------------------------------------------------------------------
# Argo CD scenarios: Application objects as the notifications controller sees
# them (status included).

REPO = "https://github.com/example/platform-deploy.git"
SHA = "4f1c2e9a7b3d58e60c9a1b2d3e4f5a6b7c8d9e0f"
RECIPIENT = "smtp:you@example.com"


def application(name, health, sync, *, sources=None, op=None, resources=None, conditions=None,
                health_since=20, notified=None):
    sources = sources or [{"repoURL": REPO, "path": f"apps/{name}", "targetRevision": "main"}]
    app = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": name, "namespace": "argocd", "annotations": {}},
        "spec": {"project": "default",
                 "destination": {"server": "https://kubernetes.default.svc", "namespace": name}},
        "status": {
            "health": {"status": health, "lastTransitionTime": ago(health_since)},
            "sync": {"status": sync},
            "operationState": op or {"phase": "Succeeded", "message": "successfully synced (all tasks run)",
                                     "startedAt": ago(300), "finishedAt": ago(299)},
            "resources": resources or [
                {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": name, "name": name,
                 "status": "Synced", "health": {"status": health if health != "Degraded" else "Healthy"}},
                {"version": "v1", "kind": "Service", "namespace": name, "name": name, "status": "Synced",
                 "health": {"status": "Healthy"}},
            ],
        },
    }
    if len(sources) == 1:
        app["spec"]["source"] = sources[0]
        app["status"]["sync"]["revision"] = SHA
    else:
        app["spec"]["sources"] = sources
        app["status"]["sync"]["revisions"] = [s.get("targetRevision") if "chart" in s else SHA for s in sources]
    if conditions:
        app["status"]["conditions"] = conditions
    if notified:
        app["metadata"]["annotations"]["notified.notifications.argoproj.io"] = json.dumps(notified)
    return app


def notified_entry(trigger, once, ts):
    return {f"{once}:{trigger}:[0].preview:{RECIPIENT}": ts}


def argocd_scenarios():
    problem_ts = int((NOW - timedelta(minutes=31)).timestamp())
    failed_msg = ('Deployment.apps "storefront" is invalid: spec.template.spec.containers[0].image: '
                  'Invalid value: "ghcr.io/example/storefront:2.4.0 ": must not have trailing whitespace')
    return [
        ("argo-sync-failed", "Sync failed",
         application("storefront", "Healthy", "OutOfSync", op={
             "phase": "Failed", "startedAt": ago(9), "finishedAt": ago(8),
             "message": f"one or more objects failed to apply, reason: {failed_msg}",
             "syncResult": {"revision": SHA, "resources": [
                 {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "storefront",
                  "name": "storefront", "status": "SyncFailed", "message": failed_msg, "syncPhase": "Sync"}]}}),
         True, "on-sync-failed"),
        ("argo-degraded", "Degraded",
         application("checkout", "Degraded", "Synced", health_since=17, resources=[
             {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "checkout-api",
              "status": "Synced", "health": {"status": "Degraded", "message": 'Deployment "checkout-api" exceeded its progress deadline'}},
             {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "checkout-worker",
              "status": "Synced", "health": {"status": "Healthy"}},
             {"version": "v1", "kind": "Pod", "namespace": "checkout", "name": "checkout-api-6d8b7c9f4-mm4zt",
              "health": {"status": "Degraded", "message": "back-off 5m0s restarting failed container=api <CrashLoopBackOff>"}},
         ]),
         True, "on-health-degraded"),
        ("argo-unknown", "Comparison error",
         application("monitoring", "Healthy", "Unknown", sources=[
             {"repoURL": "https://prometheus-community.github.io/helm-charts", "chart": "kube-prometheus-stack",
              "targetRevision": "79.0.1", "helm": {"valueFiles": ["$values/monitoring/values.yaml"]}},
             {"repoURL": REPO, "targetRevision": "main", "ref": "values"}],
             conditions=[{"type": "ComparisonError", "lastTransitionTime": ago(9), "message": (
                 "Failed to load target state: failed to generate manifest for source 1 of 2: rpc error: code = Unknown "
                 'desc = `helm pull --version 79.0.1 kube-prometheus-stack` failed: chart "kube-prometheus-stack" '
                 'version "79.0.1" not found in https://prometheus-community.github.io/helm-charts repository')}]),
         True, "on-sync-status-unknown"),
        ("argo-recovered", "Recovered",
         application("storefront", "Healthy", "Synced", health_since=2,
                     notified=notified_entry("on-health-degraded", ago(31), problem_ts)),
         True, "on-recovered"),
        # Trigger-only fixtures: on-recovered must stay quiet for these.
        ("argo-routine", "Routine rollout", application("storefront", "Healthy", "Synced"), False, None),
        ("argo-progressing", "Still progressing",
         application("storefront", "Progressing", "Synced",
                     notified=notified_entry("on-health-degraded", ago(31), problem_ts)), False, None),
    ]


TRIGGERS = ["on-sync-failed", "on-health-degraded", "on-sync-status-unknown", "on-recovered"]


def argocd_cli(tmpdir, *args):
    kubeconfig = tmpdir / "kubeconfig"
    if not kubeconfig.exists():
        # The CLI wants a kubeconfig even when everything comes from files.
        kubeconfig.write_text(yaml.safe_dump({
            "apiVersion": "v1", "kind": "Config", "current-context": "none",
            "clusters": [{"name": "none", "cluster": {"server": "https://127.0.0.1:1"}}],
            "users": [{"name": "none", "user": {"token": "none"}}],
            "contexts": [{"name": "none", "context": {"cluster": "none", "user": "none", "namespace": "argocd"}}],
        }))
        (tmpdir / "secret.yaml").write_text(yaml.safe_dump({
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "argocd-notifications-secret"},
            "stringData": {"email-password": "preview"}}))
    return run([BIN / "argocd", "admin", "notifications", *args, "--kubeconfig", kubeconfig,
                "--config-map", ARGO_CM, "--secret", tmpdir / "secret.yaml"])


def render_argocd(tmpdir, app):
    path = tmpdir / f"{app['metadata']['name']}.json"
    path.write_text(json.dumps(app), encoding="utf-8")
    out = yaml.safe_load(argocd_cli(tmpdir, "template", "notify", "app-alert", str(path)))
    return {"subject": out["email"]["subject"].strip(), "html": out["email"]["body"], "text": out.get("message", "")}


def check_triggers(tmpdir, scenarios):
    failures = []
    for sid, _, app, _, expected in scenarios:
        path = tmpdir / f"{sid}.json"
        path.write_text(json.dumps(app), encoding="utf-8")
        for trigger in TRIGGERS:
            out = argocd_cli(tmpdir, "trigger", "run", trigger, str(path)).strip().splitlines()
            fired = out[-1].split()[-1] == "true"
            if fired != (trigger == expected):
                failures.append(f"{sid}: {trigger} fired={fired}, expected {trigger == expected}")
    if failures:
        sys.exit("trigger matrix mismatch:\n  " + "\n  ".join(failures))
    print(f"triggers: {len(scenarios)} fixtures x {len(TRIGGERS)} triggers as expected")


# --------------------------------------------------------------------------
# Proxmox: proxmox/<product>/<type>-body.html.hbs are generated from
# proxmox/src/layout.html.hbs plus one block file per notification type
# (Proxmox loads single files, no partials). Scenarios carry the template data
# Proxmox VE (PVE/VZDump.pm, ...) and Proxmox Backup Server
# (src/server/notifications/template_data.rs) hand to the renderer.

PROXMOX = ROOT / "proxmox"
PRODUCTS = {
    # directory: (header label, default "web-url" setting)
    "pve": ("Proxmox VE", "https://{{fqdn}}:8006"),
    "pbs": ("Proxmox Backup Server", "{{base-url}}"),
}


def generate_proxmox():
    src = PROXMOX / "src"
    layout = (src / "layout.html.hbs").read_text(encoding="utf-8")
    count = 0
    for product, (label, web_url) in PRODUCTS.items():
        out_dir = PROXMOX / product
        written = set()
        for frag in sorted((src / product).glob("*.html.hbs")) + [src / "test.html.hbs"]:
            name = frag.name.replace(".html.hbs", "-body.html.hbs")
            note = (f"Generated by preview/build.py from {frag.relative_to(ROOT).as_posix()} and "
                    "proxmox/src/layout.html.hbs; edit those, not this file.")
            body = (layout.replace("@@SOURCE@@", note).replace("@@PRODUCT@@", label)
                    .replace("@@WEB_URL@@", web_url)
                    .replace("@@FRAGMENT@@", frag.read_text(encoding="utf-8").strip()))
            (out_dir / name).write_text(body, encoding="utf-8", newline="\n")
            written.add(name)
            count += 1
        for stale in out_dir.glob("*-body.html.hbs"):
            if stale.name not in written:
                stale.unlink()
    return count


PVE = {"hostname": "pve1", "fqdn": "pve1.example.com"}
PBS = {"hostname": "pbs1", "fqdn": "pbs1.example.com", "base-url": "https://pbs1.example.com:8007"}
GIB = 1024 ** 3


def unix(minutes_ago):
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp())


def snapshot(kind, vmid, minutes_ago):
    return f"{kind}/{vmid}/{(NOW - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def guest_table(*rows):
    """PVE::VZDump build_guest_table: (vmid, name, status, seconds, bytes, filename)."""
    columns = [("VMID", "vmid", None), ("Name", "name", None), ("Status", "status", None),
               ("Time", "time", "duration"), ("Size", "size", "human-bytes"), ("Filename", "filename", None)]
    return {
        "schema": {"columns": [{"label": l, "id": i, **({"renderer": r} if r else {})} for l, i, r in columns]},
        "data": [dict(zip(("vmid", "name", "status", "time", "size", "filename"), row)) for row in rows],
    }


VZDUMP_CMD = "vzdump 101 102 --mode snapshot --storage pbs --fleecing 0 --notes-template '{{guestname}}'"
VZDUMP_101 = """101: INFO: Starting Backup of VM 101 (qemu)
101: INFO: status = running
101: INFO: backup mode: snapshot
101: INFO: creating Proxmox Backup Server archive 'vm/101/...'
101: INFO: transferred 183.52 GiB in 674 seconds (278.8 MiB/s)
101: INFO: Finished Backup of VM 101 (00:11:16)
"""


def proxmox_scenarios():
    """(id, title, product, type, template data)."""
    return [
        ("pve-vzdump-guest-failed", "Backup with a failed guest", "pve", "vzdump", {
            **PVE, "status-text": "backup failed", "error": None, "total-time": 720, "total-size": 4.27 * GIB,
            "guest-table": guest_table((101, "k3s", "ok", 676, 4.27 * GIB, snapshot("vm", 101, 12)),
                                       (102, "win11", "err", 44, 0, "")),
            "logs": f"{VZDUMP_CMD}\n\n{VZDUMP_101}\n102: INFO: Starting Backup of VM 102 (qemu)\n"
                    "102: ERROR: VM 102 qmp command 'backup' failed - got timeout\n"
                    "102: ERROR: Backup of VM 102 failed - VM 102 qmp command 'backup' failed - got timeout\n",
        }),
        ("pve-vzdump-storage", "Backup storage unreachable", "pve", "vzdump", {
            **PVE, "error": None, "total-time": 3, "total-size": 0,
            "status-text": "backup failed: could not activate storage 'pbs': pbs: error fetching datastores - "
                           "500 Can't connect to 10.0.0.2:8007 (Connection refused)",
            "guest-table": guest_table((101, "k3s", "todo", 0, 0, ""), (102, "win11", "todo", 0, 0, "")),
            "logs": f"{VZDUMP_CMD}\n\nERROR: could not activate storage 'pbs': pbs: error fetching datastores - "
                    "500 Can't connect to 10.0.0.2:8007 (Connection refused)\n",
        }),
        ("pve-vzdump-ok", "Backup finished (info, filtered by default)", "pve", "vzdump", {
            **PVE, "status-text": "backup successful", "error": None, "total-time": 1093, "total-size": 31.8 * GIB,
            "guest-table": guest_table((101, "k3s", "ok", 676, 27.5 * GIB, snapshot("vm", 101, 18)),
                                       (102, "win11", "ok", 417, 4.3 * GIB, snapshot("vm", 102, 7))),
            "logs": f"{VZDUMP_CMD}\n\n{VZDUMP_101}",
        }),
        ("pve-replication", "Replication failed", "pve", "replication", {
            **PVE, "job-id": "100-0", "job-target": "pve2", "job-schedule": "*/15", "failure-count": 3,
            "last-sync": unix(75),
            "error": "command 'set -o pipefail && pvesm export local-zfs:vm-100-disk-0 zfs - -with-snapshots 1 "
                     "| /usr/bin/ssh -e none -o 'BatchMode=yes' -o 'HostKeyAlias=pve2' root@10.0.0.12 -- "
                     "pvesm import local-zfs:vm-100-disk-0 zfs - -with-snapshots 1' failed: exit code 255",
        }),
        ("pve-fencing", "Node fenced", "pve", "fencing", {
            **PVE, "cluster-name": "lab", "is-success": True, "fence-prefix": "SUCCEED",
            "fence-status": "fencing: acknowledged - got agent lock for node 'pve2'",
            "failed-node": "pve2", "master-node": "pve1", "fence-timestamp": unix(2),
            "nodes": [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "fence"},
                      {"node": "pve3", "status": "online"}],
            "resources": [{"resource": "vm:100", "state": "fence", "node": "pve2", "running": 0},
                          {"resource": "vm:101", "state": "started", "node": "pve1", "running": 1},
                          {"resource": "ct:200", "state": "started", "node": "pve3", "running": 1}],
        }),
        ("pve-package-updates", "Package updates (info, filtered by default)", "pve", "package-updates", {
            **PVE, "available-updates": {"data": [
                {"package-name": "pve-manager", "installed-version": "9.2.20", "available-version": "9.2.21"},
                {"package-name": "proxmox-kernel-7.0", "installed-version": "7.0.14-23", "available-version": "7.0.14-24"},
                {"package-name": "qemu-server", "installed-version": "9.1.8", "available-version": "9.1.9"},
            ]},
        }),
        ("pve-test", "Test notification", "pve", "test", {"target": "smtp-relay"}),
        ("pbs-verify-err", "Verification failed", "pbs", "verify-err", {
            **PBS, "datastore": "store1", "job-id": "v-3fa2c1d0-8b7e",
            "failed-snapshot-list": [snapshot("vm", 101, 60 * 26), snapshot("ct", 200, 60 * 50)],
        }),
        ("pbs-gc-err", "Garbage collection failed", "pbs", "gc-err", {
            **PBS, "datastore": "store1",
            "error": "unable to get exclusive lock - EAGAIN: Try again\n\nCaused by:\n    another garbage collection is running",
        }),
        ("pbs-prune-err", "Prune failed", "pbs", "prune-err", {
            **PBS, "datastore": "store1", "job-id": "p-91e0b4c2-6d1a",
            "error": "removing backup snapshot \"vm/101/2026-09-24T06:30:02Z\" failed - backup is protected",
        }),
        ("pbs-sync-err", "Sync failed", "pbs", "sync-err", {
            **PBS, "datastore": "mirror", "job-id": "s-7c5d2e18-04af", "remote": "offsite", "remote-datastore": "store1",
            "error": "sync group vm/101 failed - error trying to connect: tcp connect error: Connection refused (os error 111)",
        }),
        ("pbs-acme-err", "Certificate renewal failed", "pbs", "acme-err", {
            **PBS, "error": "validating challenge 'dns-01' for pbs1.example.com failed: Incorrect TXT record found",
        }),
        ("pbs-test", "Test notification", "pbs", "test", {"target": "smtp-relay"}),
    ]


def render_proxmox(tmpdir, sid, product, ty, data):
    path = tmpdir / f"{sid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return json.loads(run([PROXMOX_RENDER, PROXMOX / product, ty, path]))


# --------------------------------------------------------------------------
# Site

PAGE = (ROOT / "preview" / "page.html").read_text(encoding="utf-8")


def write_email(out_dir, sid, rendered):
    body = rendered["html"]
    (out_dir / f"{sid}.html").write_text(body, encoding="utf-8")
    (out_dir / f"{sid}.light.html").write_text(body.replace(DARK_MQ, "@media not all"), encoding="utf-8")
    (out_dir / f"{sid}.dark.html").write_text(body.replace(DARK_MQ, "@media all"), encoding="utf-8")
    (out_dir / f"{sid}.txt").write_text(rendered["text"], encoding="utf-8")


def preheader(body):
    m = re.search(r'<div style="display:none;[^"]*">\s*(.*?)\s*</div>', body, re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


STATUS_ICONS = (
    '<svg viewBox="0 0 17 11" aria-hidden="true"><rect y="7" width="3" height="4" rx=".8"/><rect x="4.5" y="5" width="3" height="6" rx=".8"/>'
    '<rect x="9" y="2.5" width="3" height="8.5" rx=".8"/><rect x="13.5" width="3" height="11" rx=".8"/></svg>'
    '<svg viewBox="0 0 16 11" aria-hidden="true"><path d="M8 2.2c2.3 0 4.4.9 6 2.4l1.1-1.2A10.4 10.4 0 0 0 8 .5C5.3.5 2.8 1.5.9 3.4L2 4.6a8.6 8.6 0 0 1 6-2.4Zm0 3.4c1.4 0 2.6.5 3.6 1.4l1.1-1.2A7 7 0 0 0 8 3.9a7 7 0 0 0-4.7 1.9l1.1 1.2c1-.9 2.2-1.4 3.6-1.4Zm0 3.4c.5 0 1 .2 1.3.5L8 10.9 6.7 9.5c.3-.3.8-.5 1.3-.5Z"/></svg>'
    '<svg viewBox="0 0 27 12" aria-hidden="true"><rect x=".5" y=".5" width="22" height="11" rx="3" fill="none" stroke="currentColor" opacity=".4"/>'
    '<rect x="2" y="2" width="19" height="8" rx="1.6"/><path d="M24 4v4c.8-.3 1.4-1.1 1.4-2S24.8 4.3 24 4Z" opacity=".4"/></svg>'
)


def device(sid, mode, title, sender, subject):
    """One email in a phone mock (phone view) or a mail window (desktop view)."""
    e = html.escape
    return f"""
        <figure class="device {mode}">
          <div class="bar"><i></i><i></i><i></i></div>
          <div class="status"><span>9:41</span><span class="island"></span><span class="icons">{STATUS_ICONS}</span></div>
          <div class="mailnav"><svg viewBox="0 0 12 20" aria-hidden="true"><path d="M10 2 2 10l8 8"/></svg>Inbox</div>
          <div class="mailhead"><div class="subj">{e(subject)}</div>
            <div class="from"><span class="av">{e(sender[0])}</span><b>{e(sender)}</b><time>9:41</time></div></div>
          <iframe loading="lazy" src="emails/{sid}.{mode}.html" title="{e(title)}, {mode}"></iframe>
          <div class="home"></div>
        </figure>"""


def card(sid, title, sender, rendered):
    e = html.escape
    subject = rendered["subject"]
    return f"""
    <article class="scenario" id="{sid}">
      <div class="side">
        <h3>{e(title)}</h3>
        <div class="inbox"><div class="avatar">{e(sender[0])}</div><div class="inbox-text">
          <div class="inbox-top"><b>{e(sender)}</b></div>
          <div class="inbox-subject">{e(subject)}</div>
          <div class="inbox-pre">{e(preheader(rendered['html']))}</div></div></div>
        <div class="links"><a href="emails/{sid}.html">Full width</a><a href="emails/{sid}.txt">Plain text</a></div>
      </div>
      <div class="frames">{device(sid, "light", title, sender, subject)}{device(sid, "dark", title, sender, subject)}
      </div>
    </article>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "site"), help="output directory (default: site/)")
    out = Path(ap.parse_args().out)
    for tool in (BIN / "amtool", BIN / "argocd", PROXMOX_RENDER):
        if not tool.exists():
            sys.exit(f"missing {tool}: run preview/fetch-tools.sh first")
    print(f"proxmox: generated {generate_proxmox()} HTML templates")

    shutil.rmtree(out, ignore_errors=True)
    (out / "emails").mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        check_alertmanager_config(tmpdir)
        print("alertmanager: example config passes amtool check-config")

        am_cards = []
        for sid, title, data, settings in alertmanager_scenarios():
            rendered = render_alertmanager(tmpdir, data, settings)
            write_email(out / "emails", sid, rendered)
            am_cards.append(card(sid, title, "Alertmanager", rendered))
        print(f"alertmanager: rendered {len(am_cards)} scenarios")

        scenarios = argocd_scenarios()
        check_triggers(tmpdir, scenarios)
        argo_cards = []
        for sid, title, app, shown, _ in scenarios:
            if not shown:
                continue
            rendered = render_argocd(tmpdir, app)
            write_email(out / "emails", sid, rendered)
            argo_cards.append(card(sid, title, "Argo CD", rendered))
        print(f"argocd: rendered {len(argo_cards)} scenarios")

        pmx_cards = {product: [] for product in PRODUCTS}
        for sid, title, product, ty, data in proxmox_scenarios():
            rendered = render_proxmox(tmpdir, sid, product, ty, data)
            write_email(out / "emails", sid, rendered)
            pmx_cards[product].append(card(sid, title, PRODUCTS[product][0], rendered))
        print(f"proxmox: rendered {sum(map(len, pmx_cards.values()))} scenarios")

    page = (PAGE.replace("{{ALERTMANAGER}}", "".join(am_cards)).replace("{{ARGOCD}}", "".join(argo_cards))
            .replace("{{PVE}}", "".join(pmx_cards["pve"])).replace("{{PBS}}", "".join(pmx_cards["pbs"])))
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / ".nojekyll").write_text("")
    print(f"site: {out / 'index.html'}")


if __name__ == "__main__":
    main()
