<div align="center">

# Alert email templates

**HTML email templates for Prometheus Alertmanager and Argo CD notifications<br>that read well on any screen, in light and dark.**

[![Preview](https://github.com/FYWinds/alert-email-templates/actions/workflows/preview.yml/badge.svg)](https://github.com/FYWinds/alert-email-templates/actions/workflows/preview.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)
[![Alertmanager](https://img.shields.io/badge/Alertmanager-%E2%89%A50.28-e6522c?logo=prometheus&logoColor=white)](https://github.com/prometheus/alertmanager)
[![Argo CD](https://img.shields.io/badge/Argo%20CD-%E2%89%A53.0-ef7b4d?logo=argo&logoColor=white)](https://github.com/argoproj/argo-cd)

[**Live preview**](https://fywinds.github.io/alert-email-templates/) ·
[Alertmanager](#alertmanager) ·
[Argo CD](#argo-cd) ·
[Preview locally](#preview-locally)

<img src="docs/images/hero.png" alt="Four example emails: an Alertmanager critical alert, a grouped warning in dark mode, an Argo CD sync failure, and an Argo CD recovery in dark mode" width="100%">

</div>

## Why

The stock Alertmanager email lists every label before it gets to the part that matters, and Argo CD notifications default to a line of plain text. These templates put the state and the message first:

- **Status at a glance.** A colored header and pill per state: critical, warning, resolved, sync failed, degraded, comparison error, recovered.
- **The message first.** Summary and description, or the sync error and the resources it hit, above the details.
- **Buttons that go somewhere.** Graph, Silence and Runbook for alerts; Open in Argo CD and Commit for apps. Optional Grafana links when Alertmanager and Prometheus are not exposed.
- **Short subjects.** `🔴 Critical: PostgresWALArchiveLag · billing` instead of every common label.
- **Any screen, light or dark.** Fills a phone, caps at 560px in a desktop client, and follows the client's dark mode. Plus an inbox preview line and, for Alertmanager, a plain-text part.
- **A recovered email for Argo CD.** One mail when an app is Healthy and Synced again after a problem, and none for routine rollouts.

Every email in the [live preview](https://fywinds.github.io/alert-email-templates/) is rendered on each build by the same `amtool` and `argocd` binaries the servers use. Switch between phone and desktop there, or append `?view=desktop`.

<img src="docs/images/desktop.png" alt="Desktop mail windows: a grouped Alertmanager warning in light mode and a degraded Argo CD app in dark mode" width="100%">

## Alertmanager

Template, setup and settings: [`alertmanager/`](alertmanager/).

## Argo CD

Template, triggers and setup: [`argocd/`](argocd/).

## Proxmox

Notification template overrides for Proxmox VE and Backup Server: [`proxmox/`](proxmox/).

## Preview locally

```sh
preview/fetch-tools.sh                           # pinned amtool and argocd, and proxmox-render (needs cargo), into preview/bin/
uv run preview/build.py                          # checks, trigger tests, renders into site/
uv run --no-project -m http.server -d site 8000  # http://localhost:8000
```

`build.py` fails if the example config does not pass `amtool check-config`, if any scenario fails to render, or if a trigger fires for the wrong fixture. Scenarios are defined in the script; edit them to preview your own alerts. The Argo CD CLI renders from files only, no cluster needed.

## Compatibility

| | Minimum | Tested |
|---|---|---|
| Alertmanager | 0.28 (`dict`, `list`, `toJson` template functions) | 0.34.1 |
| Argo CD | 3.0 (`status.health.lastTransitionTime`) | 3.5.5 |

The HTML is table-based with inline styles. Dark mode is an `@media (prefers-color-scheme: dark)` block, so it applies in clients that support that media query ([support table](https://www.caniemail.com/features/css-at-media-prefers-color-scheme/)). Clients without it show the light design, or recolor it with their own forced dark mode.

## License

[MIT](LICENSE) © 2026 FYWinds
