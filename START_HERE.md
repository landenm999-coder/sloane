# Start here: Sloane, from zero to talking to her

About an hour, most of it waiting. Do the steps in order. Never paste a
password or token into a chat, including one with Claude; they only ever go into the
installer, where they're hidden as you type.

---

## 1. Merge the code (2 minutes)

On GitHub, open each pull request, press **Merge pull request**, then **Confirm merge**:

- [Sloane #12](https://github.com/landenm999-coder/sloane/pull/12)
- [Capture #2](https://github.com/landenm999-coder/capture/pull/2) (Vercel updates the app on its own)

The installer uses `main`, so this comes first.

---

## 2. Collect seven things (15 minutes)

Put them in a note on your computer, not in a chat.

| # | What | Where |
|---|---|---|
| 1 | **Database URL** | [supabase.com](https://supabase.com): New project (give it a database password of **letters and numbers only**; symbols break the address) → **Connect** → *Session pooler* string (port **5432**). Replace `[YOUR-PASSWORD]`, brackets and all, with that password |
| 2 | **Groq key** | [console.groq.com/keys](https://console.groq.com/keys) → Create API key |
| 3 | **Bot token** | Telegram → message **@BotFather** → `/newbot` → pick a name → copy the token. Then open the `t.me/…` link it gives you and press **Start** (a bot can't message you until you do) |
| 4 | **Your chat ID** | Telegram → message **@userinfobot** → copy the number |
| 5 | **Canvas address** | Probably `https://dcsd.instructure.com` (the address you use for Canvas) |
| 6 | **Canvas token** | Canvas (on a computer) → Account → Settings → **+ New Access Token**. No such button? Your district turned it off: instead copy Canvas → **Calendar** → **Calendar Feed** (bottom right). The installer asks for it if you leave the token blank |
| 7 | **Calendar link** | Google Calendar (on a computer) → Settings → your calendar → *Integrate calendar* → **Secret address in iCal format**. Use your personal Google account: school accounts usually hide this |

---

## 3. Get a server (10 minutes, if Oracle has room)

Skip this if you already have one.

1. [Oracle Cloud](https://cloud.oracle.com) → **Compute → Instances → Create instance**.
2. Image: **Ubuntu 24.04**. Shape: **Ampere A1** (ARM), **2 OCPUs, 12 GB**.
3. Download the SSH key it offers and **keep it**.
4. Create. If it says **"Out of host capacity"**, that's Oracle, not you: try again later or pick
   another availability domain. Or use your PC for now (below).
5. Note the instance's **public IP**. Don't open any ports; she doesn't need any.

**No server yet? Run her on your PC for now.** She works the same while the PC is on and awake,
and her memory lives in Supabase, so moving to a server later loses nothing.

1. PowerShell as administrator: `wsl --install -d Ubuntu-24.04`, then restart the PC.
2. Open **Ubuntu** from the Start menu and pick a username and password.
3. In that Ubuntu window, do step 4 from its item 2 (the `curl` command). If it says systemd
   isn't running, it prints the two commands that fix it.
4. Leave the Ubuntu window open (minimize it): closing it stops her.
5. When you move to a server later, stop her on the PC first (`sudo systemctl disable --now sloane`
   in Ubuntu). Two copies on one bot token fight over your messages.

---

## 4. Install (15 minutes)

1. Connect to the server from a terminal (on Windows, PowerShell works):

   ```
   ssh -i path/to/the-key.key ubuntu@YOUR.SERVER.IP
   ```

   If Windows says the key is "unprotected" or "too open", run this once in PowerShell, then try again:

   ```
   icacls path\to\the-key.key /inheritance:r /grant:r "$($env:USERNAME):(R)"
   ```

2. Paste this and press Enter:

   ```
   curl -fsSL https://raw.githubusercontent.com/landenm999-coder/sloane/main/scripts/install.sh | bash
   ```

3. It asks for the seven things from step 2 (the secret ones don't show as you type; that's
   normal). If one looks wrong it says why and asks again. Then three more:
   - **What should she call you?** Press Enter for "Landen", or type `sir`.
   - **A British voice?** Type `y` for the full JARVIS feel.
   - **Morning brief as a voice note?** `y` and your 6:35 AM brief arrives spoken.
4. It asks you to **log in to Claude once**. Choose your Claude account (not an API key), open
   the link it prints, approve, paste the code back if it asks, then type `/exit`.
5. It finishes with **"Done."** Within a minute she messages you on Telegram: *"Sloane here, up
   and running."* That's how you know it worked.

---

## 5. Try everything (as you go)

Send these to her on Telegram. Each should do what it says.

1. `hey, how's it going?` → a line back, in her voice. "Typing…" shows at once.
2. `what's due tomorrow?` then `and friday?` → real Canvas rows; she follows the thread.
3. A **voice note**: "what's on today?" → a voice note back.
4. `remind me in 2 minutes to test this` → a reminder, with Snooze buttons.
5. `put batteries on the grocery list and remind me at 7 to charge the car` → both done.
6. `who won the Broncos game?` → "Checking.", then the answer.
7. Add every school you're applying to in one message, one per line:
   ```
   /college add CU Boulder EA nov 1
   Colorado State University (CSU) RD feb 1
   Colorado School of Mines EA nov 1
   ```
   Then `just finished my Boulder essays` and `what's left for Boulder?`
8. `/roleplay` → a DECA scenario. Present (typing or voice), say "I'm done", answer two
   questions, get a score.
9. `/countdown DECA districts dec 3`, `/habit add reading`, then `did reading`.
10. `spent 12 on lunch`, then `/budget 60`.
11. `/plan` → your free time tonight, filled with what's due soonest.
12. `/today`, `/week`, `/grades`, `/status`.

Type `/` in her chat for the menu of commands; `/help` lists everything. The full table is in
[DEPLOY.md](DEPLOY.md), *The first day: try everything*.

---

## 6. Optional: Capture on your phone (10 minutes)

This lets every Capture note also go to Sloane, so she remembers it.

1. On the server:

   ```
   curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
   sudo tailscale serve --bg 8000
   ```

   The first command prints a link to sign in to Tailscale; the second shows her address,
   `https://….ts.net`. Write it down.
2. Install **Tailscale** on your phone and sign in to the same account.
3. On the server, make the Capture password and allow the app (use the address in your phone's
   address bar when Capture is open):

   ```
   cd /opt/sloane
   python3 -c "import secrets; print('CAPTURE_TOKEN=' + secrets.token_urlsafe(32))" >> .env
   echo 'CORS_ORIGINS=https://YOUR-CAPTURE-ADDRESS' >> .env
   sudo systemctl restart sloane
   grep CAPTURE_TOKEN .env
   ```

4. In Capture: **Settings → Sloane**. Paste the `https://….ts.net` address and the token, then
   press **Connect**. If Chrome asks about devices on your local network, allow it.

---

## If something's wrong

- On Telegram: `/status`.
- On the server:

  ```
  cd /opt/sloane && sudo docker compose run --rm sloane python scripts/doctor.py
  ```

  It checks every connection and says how to fix each one.
- Logs: `journalctl -u sloane -f` (Ctrl-C to stop).
- **She never messaged in step 4.5?** Open her chat and press **Start** (or send her anything); she
  keeps trying for half an hour. Still nothing? Check the bot token and chat ID with
  `nano /opt/sloane/.env`, then `sudo systemctl restart sloane`.
- **To change a setting later** (her voice, what she calls you): `nano /opt/sloane/.env`, save,
  then `sudo systemctl restart sloane`.
- **To upgrade** after new code is merged: run the step 4 command again.
