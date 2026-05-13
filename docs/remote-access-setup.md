# Remote Access Setup — NUC from MacBook

Connect from anywhere in the world to the NUC over an encrypted private network.
No port forwarding, no static IP, no DDNS required.

## The Stack

| Layer | Tool | Purpose |
|---|---|---|
| Network | Tailscale (WireGuard mesh) | Private encrypted tunnel, NAT traversal automatic |
| Auth | SSH keys (ed25519) | No passwords ever |
| Editor | VS Code / Cursor Remote-SSH | Edit on NUC as if local |
| Sessions | tmux | Survives disconnects |
| Flaky WiFi | mosh (optional) | Survives network changes mid-session |

---

## Step 1 — Install Tailscale on the NUC

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

Open the URL it prints → log in with Google or GitHub → that account owns the "tailnet".

Check NUC's Tailscale hostname:
```bash
tailscale status
# you'll see something like: nuc.tail-scale.ts.net
```

---

## Step 2 — Install Tailscale on the MacBook

```bash
brew install --cask tailscale
```

Log in with the **same account**. Both devices are now on a private network regardless of WiFi.

Test from Mac:
```bash
ping nuc
ssh moazzam@nuc
```

---

## Step 3 — SSH Keys + Hardening

Generate key on the Mac (if you don't have one):
```bash
# run this ON THE MAC
ssh-keygen -t ed25519 -C "macbook"
ssh-copy-id moazzam@nuc
```

Harden SSH on the NUC:
```bash
sudo tee /etc/ssh/sshd_config.d/hardening.conf > /dev/null <<EOF
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF

sudo systemctl restart ssh
```

Lock firewall to Tailscale interface only (no public internet SSH exposure):
```bash
sudo ufw default deny incoming
sudo ufw allow in on tailscale0
sudo ufw enable
```

---

## Step 4 — SSH Config on the Mac

Edit `~/.ssh/config`:
```
Host nuc
    HostName nuc
    User moazzam
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

Now `ssh nuc` works from any network on Earth.

---

## Step 5 — Remote Editor (VS Code / Cursor)

1. Install extension: **Remote - SSH**
2. `Cmd+Shift+P` → `Remote-SSH: Connect to Host` → `nuc`
3. Open folder → `/home/moazzam/ai-trading-assistant`
4. Full IDE experience: terminals, debugger, file tree — all running on NUC, Mac is thin client

To run Claude Code remotely:
```bash
ssh nuc
cd ai-trading-assistant
claude
```

---

## Step 6 — tmux (Persistent Sessions)

Install:
```bash
sudo apt install tmux
```

Workflow:
```bash
ssh nuc
tmux new -s hermes          # create named session
# do work...
# Ctrl+B then D             # detach (session keeps running)
exit                        # close SSH

# reconnect from anywhere:
ssh nuc
tmux attach -t hermes       # back exactly where you left off
```

Useful tmux commands:
```
Ctrl+B c    → new window
Ctrl+B n    → next window
Ctrl+B 0-9  → jump to window number
Ctrl+B d    → detach
Ctrl+B [    → scroll mode (q to exit)
```

---

## Step 7 — Mosh (Optional, for Flaky Connections)

Better than SSH on hotel WiFi, planes, trains. Survives sleep/wake.

```bash
# NUC:
sudo apt install mosh
sudo ufw allow in on tailscale0 to any port 60000:61000 proto udp

# Mac:
brew install mosh

# Connect:
mosh nuc
```

Combine with tmux for best experience: `mosh nuc -- tmux attach -t hermes`

---

## Traveling Workflow

1. Open MacBook anywhere
2. Tailscale auto-connects (menubar icon shows green)
3. `ssh nuc` or open VS Code Remote-SSH → `nuc`
4. Make changes to strategy, configs, etc.
5. `sudo systemctl restart hermes` to deploy changes
6. `journalctl -u hermes -f` to watch live logs
7. Close laptop — Hermes keeps running on NUC

---

## Managing Hermes Service

```bash
sudo systemctl status hermes      # check if running
sudo systemctl restart hermes     # deploy changes
sudo systemctl stop hermes        # pause trading
sudo systemctl start hermes       # resume
journalctl -u hermes -f           # live logs
journalctl -u hermes --since "1 hour ago"   # last hour
```

---

## Security Checklist

- [ ] Tailscale installed on NUC and MacBook, same account
- [ ] SSH password auth disabled on NUC
- [ ] Public internet port 22 closed (no router port-forward)
- [ ] UFW only allows connections on `tailscale0`
- [ ] Automatic security updates: `sudo apt install unattended-upgrades`
- [ ] **2FA on your Tailscale identity provider (Google/GitHub)** — this is the master key to your trading machine. Use hardware key or TOTP app, never SMS.
- [ ] BIOS: AC power loss → Power On (protects against power outages)

---

## Quick Reference

| Task | Command |
|---|---|
| Connect to NUC | `ssh nuc` |
| Attach running session | `ssh nuc -t tmux attach -t hermes` |
| One-liner mosh+tmux | `mosh nuc -- tmux attach -t hermes` |
| Check Hermes | `ssh nuc -t journalctl -u hermes -f` |
| Tailscale status | `tailscale status` |
| List devices on tailnet | `tailscale status --peers` |
