# wargame-bot

Discord bot for war-game timekeeping — multi-guild, MongoDB-backed.

## Setup

```bash
cp .env.example .env  # or edit .env directly
docker compose up -d
```

## Commands

| Command | Description |
|---------|-------------|
| `/setclock start_date pace` | Initialize this server's world clock |
| `/now` | Show current in-world time |
| `/pause` / `/start` | Pause/resume the clock |
| `/reset` | Reset to start date |
| `/reminder message delta/date` | Set an in-world time reminder |
| `/ticker interval` | Auto-post world time on an interval (`off` to stop) |
| `/whenwas message` | Look up in-world time a message was sent |

## Env vars

- `DISCORD_TOKEN` — bot token
- `MONGODB_URI` — connection string
- `MONGODB_DB` — database name (default: `wargame`)
