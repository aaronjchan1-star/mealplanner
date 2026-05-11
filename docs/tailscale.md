# Accessing the planner from anywhere — Tailscale guide

Out of the box, the meal planner is only reachable from devices on your home WiFi. This guide adds **Tailscale** — a free, encrypted mesh VPN — so you and your partner can reach it from anywhere (work, the shops, on holiday) without exposing the Pi to the public internet.

## What you're signing up for

- Free for personal use (up to 100 devices).
- Cross-platform: iPhone, Android, Windows, Mac, Linux.
- End-to-end encrypted.
- No port forwarding on your router.
- No public IP needed.
- Only devices signed into *your* Tailscale account can see the Pi.

## What it looks like when it's done

Your wife taps a bookmark on her phone, the page loads at something like `http://meal-pi:8080` whether she's at home, on the bus, or in the supermarket aisle. Same for you. No-one else can reach it — it isn't on the public internet, it's only accessible to devices on your "tailnet".

The Pi's address inside Tailscale is stable — it doesn't change when you move, and your home IP staying the same or not is irrelevant.

## Setup, end to end (~15 minutes)

### 1. Install Tailscale on the Pi

SSH into the Pi:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

The second command prints a URL. Open it in a browser on your laptop, sign in with Google (recommended — it's how you'll authenticate the family later too), and authorize the Pi.

Confirm the Pi has joined your tailnet:

```bash
tailscale ip -4
```

That prints something like `100.x.x.x` — your Pi's Tailscale-only address. It's only reachable from devices in your tailnet.

While you're here, give the Pi a friendly hostname for the tailnet:

```bash
sudo tailscale set --hostname=meal-pi
```

Now it's reachable as `meal-pi` from anywhere on the tailnet.

### 2. Install Tailscale on your phone

Download the Tailscale app:

- iOS: [App Store](https://apps.apple.com/au/app/tailscale/id1470499037)
- Android: [Play Store](https://play.google.com/store/apps/details?id=com.tailscale.ipn)

Open it, sign in with the **same Google account** you used in step 1.

That's it. The phone is now on the tailnet.

### 3. Test it

On the phone, open Safari (iOS) or Chrome (Android) and go to:

```
http://meal-pi:8080
```

If the magic-DNS resolution doesn't kick in immediately, use the Pi's Tailscale IP from earlier:

```
http://100.x.x.x:8080
```

You should see the meal planner. From anywhere in the world.

### 4. Add your partner's phone

There are two ways to do this:

**Option A — share your account**: she signs into Tailscale on her phone with the same Google account. Simplest, perfect for a household.

**Option B — invite her as a separate user**: in the Tailscale admin console at [login.tailscale.com](https://login.tailscale.com/admin), go to **Users → Invite users**, send her a link. She accepts with her own Google account. Her phone joins your tailnet but she's recorded as a separate user. Better for audit logs, more setup.

Either works. For a married couple I'd just share the account.

### 5. Bookmark to the home screen

On her phone, in the browser showing the meal planner:

- iOS: Share → Add to Home Screen
- Android Chrome: ⋮ → Add to Home screen

Now it's a tap from the home screen, looks like an app.

## Maintenance

There essentially isn't any. Tailscale runs in the background on the Pi (low CPU, no noticeable memory). The mobile app sips battery — practically zero in normal use. You can leave it always on.

### When the Pi reboots

Tailscale starts automatically. You don't have to do anything.

### When you reboot the router or your ISP gives you a new public IP

Doesn't matter. Tailscale handles this transparently. The Pi's tailnet IP and hostname don't change.

### Removing a device

If you lose a phone, log into [login.tailscale.com](https://login.tailscale.com/admin) → Machines → find the device → Remove. Instantly revokes its access.

## Why not Cloudflare Tunnel?

Both work for "access the Pi from anywhere". The difference:

- **Tailscale**: app on each device. Nothing is public. Only your devices can ever touch the Pi.
- **Cloudflare Tunnel**: real public URL, anyone with the URL can hit the Pi (unless you put Cloudflare Access auth in front, which is more setup).

For a private family tool with two phones, Tailscale is the right tradeoff. If you ever want to share the planner with extended family without making them install Tailscale, switch to Cloudflare Tunnel + Access then.

## Troubleshooting

**`http://meal-pi:8080` doesn't resolve.** Magic DNS sometimes takes a minute on a new device. Use the `100.x.x.x` IP instead, or wait a minute and try again. Confirm Magic DNS is enabled in the Tailscale admin console under DNS.

**Connection works at home, fails on mobile data.** This is almost always battery-saver killing the Tailscale background service on the phone. Whitelist Tailscale in your phone's battery settings. On iPhone: Settings → Tailscale → Background App Refresh = on.

**It connects but is slow.** Tailscale tries to establish a peer-to-peer connection. If both ends are behind strict NATs, it falls back to a relay (DERP). That's slower but still works. Usually you'll get a direct connection. Check status with `tailscale status` on the Pi — `direct` is fast, `relay "syd"` is slower but acceptable.

**The Pi shuts down or loses internet.** No Tailscale, no remote access. Same as the original setup — the Pi needs to be online.
