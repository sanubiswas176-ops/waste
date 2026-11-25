# Waste WordSeek Solver

## Connect
- Start the bot in private chat.
- Use `/add` and complete Telegram OTP / optional 2FA login yourself.
- The existing MongoDB-backed session store restores connected sessions after restart.

## WordSeek commands (send from the connected account in each target group)
- `.auto4` — start and solve 4-letter WordSeek games in that group; after a win, start the next game there.
- `.auto5` — same for 5-letter games.
- `.auto6` — same for 6-letter games.
- `.stop` — stop all active WordSeek workers for this connected account across groups.
- `.help` — show the WordSeek command guide.

Each group has its own worker, so multiple groups can run independently. The solver sends the WordSeek start command only in the chat where `.auto4`, `.auto5`, or `.auto6` was issued. If WordSeek does not respond in that chat, the worker stops.

The solver uses the bundled public/common word lists and the feedback buttons returned by WordSeek. It does not read the game's database or hidden answer.

## Bot menu
- `/start` checks membership in `@wasteBotz` and `@wasteuserbotUpdates`.
- After verification it sends the configured spoiler photo and WordSeek welcome text.
- Help and Owner buttons open the WordSeek help and `@day1nomore` profile.

Telegram inline keyboards do not support custom per-button red/green/blue colors; the menu preserves the requested left/right row layout and labels, while button colors follow the user's Telegram theme.

## Safety and deployment
- No background auto-joining, forced rejoining after a user leaves, or unsolicited `/start` messages to other bots is performed. Users can join/start those destinations manually.
- Existing owner-only MongoDB broadcast command remains available.
- Configure the original Waste Userbot environment: `API_ID`, `API_HASH`, `BOT_TOKEN`, `MONGO_URL`, and `OWNER_ID`.
- The bot must be able to check membership in both force channels; add the bot as an administrator in those channels.
- Keep OTPs, 2FA passwords, and exported session strings private.
