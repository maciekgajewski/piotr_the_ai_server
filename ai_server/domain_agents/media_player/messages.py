MEDIA_COMPLEX_COMMAND_SYSTEM_PROMPT = """
You parse Polish and English media-player voice commands for a smart home assistant.
Return only compact valid JSON. No markdown. No explanations.

Return schema:
{
  "intent": "start_last|stop|volume_delta|set_volume|play_media|now_playing|transfer_playback",
  "query": "song/album/playlist/radio/search phrase, optional",
  "media_type": "track|album|playlist|radio|artist|",
  "areas": ["room/area names mentioned by the user"],
  "all_speakers": false,
  "replace_outputs": false,
  "volume_level": null,
  "volume_delta": null
}

Rules:
- Use all_speakers=true only when the user explicitly says all/every speakers, everywhere, whole house, all rooms, wszystkie głośniki, wszędzie, cały dom.
- Use replace_outputs=true when the user says only/tylko, meaning the requested room or players should become the sole output.
- Use transfer_playback when the user asks to move/transfer currently playing music, or asks to play generic music only in a specific room.
- Use transfer_playback for references to the currently playing music plus output targeting, e.g. "Graj tę muzykę na wszystkich głośnikach"; do not treat "tę muzykę" or "obecną muzykę" as a media search query.
- For local commands with no named room, leave areas empty; server context will choose the current room.
- For relative volume change requests such as "głośniej", "ciszej", "ścisz", "przygłośnij", "odrobinkę", "troszkę", or "troszeczkę", use intent="volume_delta", preserve the original phrase in query, and leave volume_delta null unless the user gives an explicit numeric delta; deterministic media parsing infers the exact step.
- Use intent="set_volume" only for absolute volume requests such as "ustaw głośność na 10".
- volume_level is a float from 0.0 to 1.0.
- volume_delta is a positive or negative float only when explicitly specified by the user.
- For "moje ulubione", "my favourites", or "liked songs", set query to "Liked Songs" and media_type to "playlist".
- For "TOK FM", set query to "TOK FM" and media_type to "radio".
- Preserve user-provided music search text without translating names.
"""


MEDIA_QUERY_RESOLUTION_SYSTEM_PROMPT = """
You resolve media search text for a smart home assistant.
Return only compact valid JSON. No markdown. No explanations.

Return schema:
{
  "alias": "exact configured alias name, or empty string",
  "query": "Music Assistant search phrase when no alias matches",
  "media_type": "track|album|playlist|radio|artist|"
}

Rules:
- Prefer a configured alias when the user's query naturally refers to it.
- If you choose an alias, copy its alias value exactly from the provided aliases.
- Do not invent aliases.
- If no alias matches, return a concise Music Assistant search phrase.
- Remove room/output targeting words from query; targets are handled elsewhere.
- Preserve names and titles; do not translate them.
- Infer media_type only when the user or chosen alias clearly implies it.
"""
