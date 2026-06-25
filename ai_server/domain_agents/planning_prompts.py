from __future__ import annotations


HOME_ASSISTANT_PLANNING_PROMPT = """
For home_assistant tasks:
- For singular or local Home Assistant requests with no named area, prefer conversation.area when it is known.
- When using conversation.area for Home Assistant selection, put it in selector.area, never selector.name.
- If the user names an area/room in the utterance, that named area always overrides conversation.area.
- Polish area aliases: salon means living room; biuro means office; sypialnia means bedroom.
- When conversation.home_assistant_areas is present, use it as the source of truth for Home Assistant areas.
- For named rooms in home_assistant selectors, output canonical area_id values from conversation.home_assistant_areas, not the user's inflected phrase.
- If the user names a room but it is not present in conversation.home_assistant_areas, block the task and ask which room they mean.
- Use scope="all" only when the user explicitly asks for all/every/wszystkie/każde/everywhere/whole house.
- For Home Assistant pronouns such as ją/je/it/them, resolve selection from active_context.salient_entities.
- For Home Assistant context_updates.salient_entities, store stable target references like climate.salon or light.bedroom_lamp, not numbers, temperatures, or generic words.
- After a Home Assistant command targets a device type and area, preserve that target as <domain>.<area> for follow-up turns.

Command envelope:
{
  "selection": {
    "include": [{"domain": "light|climate|switch|fan|cover", "scope": "all|single", "name": "optional", "area": "optional"}],
    "exclude": [{"name": "optional", "domain": "optional", "area": "optional"}]
  },
  "operation": {
    "intent": "turn_on|turn_off|set_temperature|set_hvac_mode|set_brightness_percent|adjust|query_state",
    "description": "natural language operation description",
    "parameters": {}
  }
}
"""

TIME_PLANNING_PROMPT = """
For time tasks:
- Include geo_location or timezone only when the user explicitly asks for a geographic place or timezone.
- For plain questions like "która godzina?", omit geo_location and timezone; the time agent already knows server_location and server_timezone.
- Never copy conversation.area into time.geo_location.

Command shape:
{"query": "original time question", "geo_location": "optional geographic place", "timezone": "optional"}
"""

WIKIPEDIA_PLANNING_PROMPT = """
For wikipedia tasks, command should be:
{"intent": "lookup_fact|summary|where_is|coordinates", "topic": "article/search topic", "fact": "birth_year|coordinates|location optional"}
"""

WEATHER_PLANNING_PROMPT = """
For weather tasks:
- For plain local weather questions, omit location; the weather agent already knows server_location.
- For weather questions about later today, tonight, evening, rain, or whether something will happen, use get_weather_forecast, not get_weather_now.
- Use focus="temperature" only when the user asks about temperature or degrees.
- Never copy conversation.area into weather.location.

Command shapes:
{"tool": "get_weather_now", "query": "original weather question", "location": "optional geographic place", "focus": "temperature optional"}
{"tool": "get_weather_forecast", "query": "original weather question", "location": "optional geographic place", "horizon": "today|tomorrow|weekend|next_weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday", "granularity": "daily|hourly", "focus": "temperature optional"}
"""

MEDIA_PLAYER_PLANNING_PROMPT = """
For media_player tasks:
- For music commands without a named room, omit areas; the media player agent will use conversation.area.
- For named rooms in media_player areas, output canonical area_id values from conversation.home_assistant_areas when it is present, not the user's inflected phrase.
- Use all_speakers=true only when the user explicitly asks for all speakers/everywhere/whole house/wszystkie głośniki.
- Use replace_outputs=true only when the user explicitly asks for only that room/player, e.g. "only in the office" or "tylko w biurze".
- Use intent="transfer_playback" when the user asks to move/transfer currently playing music, e.g. "Przenieś muzykę do salonu", or asks to play generic music only in a specific room, e.g. "Graj muzykę tylko w biurze".
- For "moje ulubione", use query="Liked Songs" and media_type="playlist".
- For "TOK FM", use domain="media_player", query="TOK FM", and media_type="radio".

Command shape:
{
  "intent": "start_last|stop|volume_delta|set_volume|play_media|now_playing|transfer_playback",
  "query": "original user phrase or media search text",
  "media_type": "track|album|playlist|radio|artist optional",
  "areas": ["optional named rooms"],
  "all_speakers": false,
  "replace_outputs": false,
  "volume_level": 0.0,
  "volume_delta": 0.0
}
"""

SYSTEM_STATUS_PLANNING_PROMPT = """
For system_status tasks:
- Use system_status for explicit system-health questions and casual assistant check-ins such as "jak się masz?", "co u ciebie?", or "jak tam?".

Command shape:
{"intent": "quick_check|summary|full_report", "query": "original system status or casual check-in phrase"}
"""

_PLANNING_PROMPTS = {
    "home_assistant": HOME_ASSISTANT_PLANNING_PROMPT,
    "time": TIME_PLANNING_PROMPT,
    "wikipedia": WIKIPEDIA_PLANNING_PROMPT,
    "weather": WEATHER_PLANNING_PROMPT,
    "media_player": MEDIA_PLAYER_PLANNING_PROMPT,
    "system_status": SYSTEM_STATUS_PLANNING_PROMPT,
}


def planning_prompt_for_domain(domain: str) -> str:
    return _PLANNING_PROMPTS.get(domain, "")
