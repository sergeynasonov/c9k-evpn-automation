# c9k-evpn-automation — environment setup

This project uses a modern, secure, and automated local development environment. It leverages **Infisical** for centralized secret management, **direnv** for automatic environment variable injection and virtual environment activation, and **Ansible** for Cisco IOS-XE automation using a **NetBox** dynamic inventory.

## Step 1: Prerequisites and system dependencies

Pick your platform below. **Everything from Step 2 onward is identical**, so
follow one of these and then continue down the page.

### macOS

Ensure you have Homebrew and Python 3:

```bash
# Install Homebrew if you don't have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install python3
brew install infisical/get-infisical/infisical
brew install direnv
```

Hook `direnv` into your shell:

```bash
echo 'eval "$(direnv hook zsh)"' >> ~/.zshrc
source ~/.zshrc
```

### Windows — via WSL2

**Ansible cannot run as a control node on Windows.** `ansible-core` is published
for `Operating System :: POSIX` only, and its SSH connection plugin — which
`network_cli` uses to reach the switches — imports `fcntl`, a module that does
not exist on Windows. This is not a gap to work around; run it under WSL2, where
it is Linux and the rest of this document applies unchanged.

In **PowerShell as Administrator**, then reboot and set a UNIX username when
Ubuntu first launches:

```powershell
wsl --install -d Ubuntu
```

Everything from here runs **inside the Ubuntu shell**, not PowerShell:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip direnv curl git

# Infisical CLI (adds their apt repository)
curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | sudo -E bash
sudo apt update && sudo apt install -y infisical
```

Hook `direnv` into your shell — WSL Ubuntu defaults to **bash**, not zsh:

```bash
echo 'eval "$(direnv hook bash)"' >> ~/.bashrc
source ~/.bashrc
```

> **Clone into the WSL home directory, never `/mnt/c`.**
>
> The Windows filesystem is mounted world-writable inside WSL, and **Ansible
> silently skips `ansible.cfg` in a world-writable directory** — it prints one
> warning and carries on. You lose the NetBox inventory, `roles_path`,
> `strategy = free` and the connection timeouts all at once, and the symptom is
> a playbook that finds no hosts rather than anything pointing at the cause.
>
> Use `~/c9k-evpn-automation`. You can still edit it from Windows: VS Code with
> the WSL extension, or `\\wsl$\Ubuntu\home\<you>\` in Explorer.

Two smaller things worth knowing on WSL. Your switches must be reachable from
the WSL network namespace — WSL2 NATs through the Windows host, so a VPN on the
Windows side normally works, but a split-tunnel or a strict host firewall can
leave WSL without a route to the management network. And `wsl --shutdown` closes
persistent SSH connections, so re-run rather than resume a playbook after one.

---

## Step 2: Authenticate with Infisical
Log in to your Infisical account. The session is cached by your OS credential
store — the macOS keychain, or the Linux keyring under WSL2.
```bash
infisical login
```
*Note: Ensure your Infisical project contains the following in your target
environment (e.g., `dev`). These are every variable the project reads from the
environment — a missing one fails at run time, not at start-up:*

| Variable | Used for | Required |
|---|---|---|
| `NETBOX_API` | NetBox URL, e.g. `https://netbox.example.com` | yes |
| `NETBOX_TOKEN` | NetBox API token | yes |
| `ios_username` | Switch login, and the local admin account the baseline installs | yes |
| `ios_password` | Password for the above | yes |
| `ios_snmp_auth` | SNMPv3 authentication password | only if `readiness_apply_snmp` |
| `ios_snmp_priv` | SNMPv3 privacy password | only if `readiness_apply_snmp` |
| `ios_password_secret_hash` | Pre-hashed (type 9) form of `ios_password` | optional — see below |

`ios_password_secret_hash` is optional but recommended. IOS re-salts a plaintext
`secret`, so a plaintext-configured account never matches what the device reports
back and the baseline reports `changed` on every run. Generate the hash once on
any switch:

```
username tmp privilege 15 secret <plaintext>
show running-config | include ^username tmp
```

The readiness role verifies the hash against `ios_password` on the control node
before it is ever pushed — a mismatch would otherwise lock you out of the
account silently.

---

## Step 3: Project Setup
Navigate to the project directory (or clone the repository) and set up the local Python virtual environment.

```bash
# Clone and enter the project
git clone https://github.com/sergeynasonov/c9k-evpn-automation.git
cd c9k-evpn-automation

# Create the Python virtual environment (named 'venv')
python3 -m venv venv

# Install Ansible, NetBox, and IP Math dependencies
venv/bin/pip install -r requirements.txt

# Install required Ansible collections
venv/bin/ansible-galaxy collection install -r requirements.yml

# Create this fabric's site configuration from the documented template
cp group_vars/all/fabric.yml.example group_vars/all/fabric.yml
```

Then edit `group_vars/all/fabric.yml`: ASNs, underlay addressing, and NTP / SNMP
/ VTP identity. Every key is documented in the `.example` file, which ships with
RFC 5737 placeholder addresses.

**`fabric.yml` is gitignored.** Site values live there and nowhere else, so this
fabric's addressing is never committed and a `git pull` never overwrites it.
Passwords do not go in this file — they come from the environment, as above.

---

## Step 4: Configure `direnv` (.envrc)
Create an `.envrc` file in the root of the project. This file tells `direnv` to automatically fetch secrets from Infisical and activate the Python virtual environment whenever you enter the directory.

Create `.envrc` with the following content:

```bash
# .envrc

# 1. Load secrets from Infisical
eval $(infisical export --format=dotenv-export)

# 2. Activate the virtual environment (Bypassing the activate script for reliability)
export VIRTUAL_ENV="$(pwd)/venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"
unset PYTHONHOME
```

Approve the directory for `direnv` execution:
```bash
direnv allow
```
*You should immediately see the secrets load and your `(venv)` activate in your shell prompt.*

> **If `cd` into the project hangs**, with direnv reporting that it is "taking a
> while to execute", your Infisical session has expired. `infisical export` is
> waiting on an interactive login that direnv cannot answer. Press Ctrl-C, run
> `infisical login`, then `direnv reload`.

---

## Step 5: Ansible Configuration Reference
For this setup to work, ensure your Ansible files are configured to consume the environment variables injected by `direnv`.

### 1. `ansible.cfg`
Ships with the repository — no edit needed. Worth knowing what it sets, because
some of it is load-bearing:

- `inventory = netbox_inv.yml` — NetBox is the inventory, not a hosts file.
- `roles_path = roles` — `acm_evpn` includes `underlay_readiness` as a pre-flight.
- `strategy = free` with `forks = 50` — hosts advance independently, so one slow
  switch does not gate 500. The playbooks override this to `linear` where they
  need `run_once`.
- `gathering = explicit` — no implicit fact gathering; these are network devices.
- `result_format = yaml` — readable multi-line output instead of JSON blobs.

### 2. `netbox_inv.yml` (Dynamic Inventory)
*Define neither `api_endpoint:` nor `token:`. The plugin reads `NETBOX_API` and
`NETBOX_TOKEN` from the environment on its own, which is how this repo avoids
hardcoding a NetBox instance.*

*An inventory plugin config is **not** templated, so
`api_endpoint: "{{ lookup('env', 'NETBOX_API') }}"` does not work — the literal
string reaches urllib and fails with `unknown url type`, taking the whole
inventory with it and leaving every play with "no hosts matched".*
```yaml
plugin: netbox.netbox.nb_inventory
validate_certs: true
use_extra_vars: true
flatten_custom_fields: true

query_filters:
  - status: active

site_data: true
prefixes: true

compose:
  ansible_host: primary_ip4.address | ipaddr('address')

rename_variables:
  - pattern: "^serial$"
    repl: "netbox_serial"
  - pattern: "^tags$"
    repl: "netbox_tags"

group_by:
  - device_roles
  - site_group
  - sites
  - tags
```

*NetBox exposes a device's `serial` and `tags` as host variables, and Ansible
reserves both names — they are play keywords. Without the renames every run
opens with two `Found variable using reserved name` warnings, and `serial` in
particular is the batching keyword, so a host variable shadowing it is a real
hazard rather than cosmetic noise. The `$` matters: nb_inventory matches with
`re.match`, which anchors only at the start, so an unanchored pattern would also
rename anything merely beginning with those names.*

*This renames **host variables** only. Group creation runs through a different
path in the plugin, so `group_by: tags` still produces `tags_<slug>` groups —
`tags_fabric_spine`, which selects every spine switch, is unaffected.*

### 3. `group_vars/all/vars.yml`
Map the Infisical secrets to standard Ansible connection variables using the `env` lookup plugin:
```yaml
# IOS Connection Variables
ansible_user: "{{ lookup('env', 'ios_username') }}"
ansible_password: "{{ lookup('env', 'ios_password') }}"
ansible_connection: network_cli
ansible_network_os: cisco.ios.ios
```

---

## Step 6: Prepare NetBox

NetBox is the source of truth, and the automation reads a specific vocabulary
from it — custom fields, tags, device roles and VLAN roles, all matched **by
slug**. A device whose role is spelled differently is not in the inventory
group, renders nothing, and raises no error, so this step is not optional.

Create the schema once:

```bash
scripts/netbox_provision_schema.py            # dry run — shows what is missing
scripts/netbox_provision_schema.py --apply    # create it
```

It is create-only: anything already present is left untouched, never renamed or
re-described. It creates the *vocabulary*, not your data — no sites, devices,
VLANs or VRFs, because those are your network.

Then model your fabric. The minimum for a device to be provisioned at all:

| | |
|---|---|
| Device role | `fabric_border` or `fabric_leaf` — or the `fabric_spine` **tag** for spines |
| Status | **Active** (the inventory filters on it) |
| Primary IPv4 | **The `Loopback0`/VTEP address** — asserted on every run |
| Site | Required; decides which fabric the device is in and which VLANs reach it |

Sites are buildings and site groups are campuses, which is what decides whether
a VLAN lands on one building or the whole campus.

A **fabric** is one site group — or one site, where that site has no site group.
That is the boundary of an underlay, an ASN and a set of route reflectors, so
switches peer only within it. Site groups must not be nested; use a region for
anything above a fabric.

**[roles/acm_evpn/README.md](roles/acm_evpn/README.md) is the full data-model
contract** — every field, role and tag with its meaning. Read it before
modelling tenants; it is the reference this step summarises.

---

## 🚀 Daily Workflow

You do not need to manually activate virtual environments, decrypt vaults, or export tokens. 

1. Open your terminal.
2. Navigate to the project:
   ```bash
   cd c9k-evpn-automation
   ```
3. Run your playbooks normally:
   ```bash
   ansible-inventory --list                # confirm NetBox inventory resolves
   ansible-playbook readiness.yml          # check the hand-built underlay
   ansible-playbook generate_evpn.yml      # render candidates, no device changes
   ansible-playbook deploy_evpn.yml        # apply under a rollback timer
   ```
   See the [README](README.md) for what each does and the order to run them in.

**To refresh secrets from Infisical (if updated in the web UI):**
```bash
direnv reload
```