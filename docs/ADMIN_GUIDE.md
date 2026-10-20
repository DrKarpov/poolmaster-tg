# Administrator guide

Administrative access is based on numeric Telegram IDs configured in `ADMIN_USER_IDS`. Keep that allowlist private. `/admin` and state-changing callbacks require an authorized administrator; private admin screens are available in the administrator's private chat.

## Admin sections

- **Broadcast:** compose text or a photo with caption, preview it, then confirm or cancel. Treat confirmation as a bulk-send action.
- **Channels and groups:** connect a destination through Telegram's native picker, inspect its status, and choose a publication schedule. New destinations start with schedules off.
- **Users:** export registered-user data for administrative use. Treat any export as personal data; never commit or share it publicly.
- **Status:** view data-source status, aggregate counts, and the global automatic user-alert pause.

## Keeping automation controlled

The global automatic user-alert pause and managed-destination schedules are independent. Pausing personal alerts does not disable a channel or group schedule. Keep each managed schedule off until that destination is explicitly approved, and inspect the target and content before a manual publication.

New connections are verified against Telegram chat identity and the bot's effective posting rights. Rights loss disables the destination schedule; restoring rights does not enable it again automatically. An administrator must explicitly reconnect and choose a schedule.

See [Configuration](CONFIGURATION.md) and [Security](../SECURITY.md).
