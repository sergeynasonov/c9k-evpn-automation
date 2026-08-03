# c9k-evpn-automation — environment setup

This project uses a modern, secure, and automated local development environment. It leverages **Infisical** for centralized secret management, **direnv** for automatic environment variable injection and virtual environment activation, and **Ansible** for Cisco IOS-XE automation using a **NetBox** dynamic inventory.

## 🛠 Prerequisites (macOS)
Ensure you have Homebrew and Python 3 installed on your system.

```bash
# Install Homebrew if you don't have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python 3
brew install python3
```

---

## Step 1: Install System Dependencies
Install the required command-line tools using Homebrew.

```bash
# Install Infisical CLI
brew install infisical/get-infisical/infisical

# Install direnv
brew install direnv
```

Hook `direnv` into your shell by adding it to your `~/.zshrc` file:
```bash
echo 'eval "$(direnv hook zsh)"' >> ~/.zshrc
source ~/.zshrc
```

---

## Step 2: Authenticate with Infisical
Log in to your Infisical account. This will securely cache your session in the macOS keychain.
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
*Note: Do not define the `token:` key. The plugin automatically detects the `NETBOX_TOKEN` environment variable.*
```yaml
plugin: netbox.netbox.nb_inventory
api_endpoint: "{{ lookup('env', 'NETBOX_API') }}"
validate_certs: true
use_extra_vars: true
flatten_custom_fields: true

query_filters:
  - status: active

site_data: true
prefixes: true

compose:
  ansible_host: primary_ip4.address | ipaddr('address')
group_by:
  - device_roles
  - site_group
  - sites
  - tags
```

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