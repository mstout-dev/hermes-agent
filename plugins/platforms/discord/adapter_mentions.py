import re
from typing import Any


class DiscordMentionMixin:
    def _raw_mentioned_user_ids(self, message: Any) -> set:
        content = getattr(message, "content", "") or ""
        return {match.group(1) for match in re.finditer(r"<@!?(\d+)>", content)}

    def _managed_role_id(self, message: Any) -> str | None:
        guild = getattr(message, "guild", None)
        role = getattr(guild, "self_role", None) if guild else None
        role_id = getattr(role, "id", None)
        return str(role_id) if role_id is not None else None

    def _self_role_is_mentioned(self, message: Any) -> bool:
        role_id = self._managed_role_id(message)
        if role_id is None:
            return False
        if any(
            str(getattr(role, "id", "")) == role_id
            for role in (getattr(message, "role_mentions", None) or [])
        ):
            return True
        content = getattr(message, "content", "") or ""
        return any(
            match.group(1) == role_id for match in re.finditer(r"<@&(\d+)>", content)
        )

    def _self_is_explicitly_mentioned(self, message: Any) -> bool:
        if not self._client or not self._client.user:
            return False
        if self._client.user in getattr(message, "mentions", []):
            return True
        if str(self._client.user.id) in self._raw_mentioned_user_ids(message):
            return True
        return self._self_role_is_mentioned(message)

    def _self_is_raw_mentioned(self, message: Any) -> bool:
        if not self._client or not self._client.user:
            return False
        return str(self._client.user.id) in self._raw_mentioned_user_ids(message)

    def _normalize_self_mentions(self, message: Any, content: str) -> str:
        if not self._client or not self._client.user:
            return content
        normalized = content.replace(f"<@{self._client.user.id}>", "")
        normalized = normalized.replace(f"<@!{self._client.user.id}>", "")
        role_id = self._managed_role_id(message)
        if role_id is not None:
            normalized = normalized.replace(f"<@&{role_id}>", "")
        return normalized.strip()
