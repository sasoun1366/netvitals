# Release announcements on Telegram

Every published release is posted to the channel by `.github/workflows/telegram-release.yml`.

## One-time setup

1. **Create a bot** — talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and keep
   the token it gives you (`123456789:AAE…`). A bot is the only thing that can post to a
   channel; your own account cannot be scripted.
2. **Add the bot to the channel as an administrator** with *Post messages* allowed.
   Channel → Manage → Administrators → Add administrator → search the bot's username.
3. **Work out the chat id**
   * public channel: use `@your_channel` (simplest), or
   * private channel: the numeric id, `-100…`. Post something in the channel, then run
     `python3 .github/telegram/find_channel_id.py <bot-token>` — see the workspace helper,
     or read `getUpdates` by hand.
4. **Add two repository secrets** (Settings → Secrets and variables → Actions →
   *New repository secret*):

   | Name | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | the token from BotFather |
   | `TELEGRAM_CHAT_ID` | `@your_channel` or `-100…` |

   Optional, same screen but under the **Variables** tab:

   | Name | Value |
   | --- | --- |
   | `TELEGRAM_LANG` | `fa`, `en` or `both` (default `both`) |

## Pull requests are not posted

Only the `release: published` event triggers a post, so nothing leaks about unreleased
work. The workflow needs no write access to the repository at all.

## Testing

Actions → **announce the release on Telegram** → *Run workflow*:

* leave **test_message** empty and tick **dry_run** to print the exact post for the last
  release into the run log, without sending anything;
* put a sentence in **test_message** to send a real test post to the channel.

The self-contained check (`telegram-test.py`) runs the announcer against a local stand-in
for the Bot API and needs no token at all:

```bash
python3 .github/telegram/telegram-test.py
```

## Notes

* If the secrets are missing, the job logs a warning and exits successfully — a missing
  announcement never fails a release.
* If the release has no notes of its own, the post lists the commit subjects since the
  previous tag instead.
* To turn announcements off, delete the secrets or the workflow file.
* To revoke the bot's access at any time: BotFather → `/revoke`, or remove it from the
  channel's administrators.
