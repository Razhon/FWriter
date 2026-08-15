import json

import keyring


class SecretStorage:
    SERVICE_NAME = "fwriter"

    @classmethod
    def get_auth(cls) -> tuple[dict | None, str | None]:
        cookies = cls._get("cookies")
        if cookies:
            cookies = json.loads(cookies)

        return (cookies or None, cls._get("user_agent"))

    @classmethod
    def set_auth(cls, cookies: dict | None, user_agent: str | None) -> None:
        if cookies:
            cls._set("cookies", json.dumps(cookies))

        if user_agent:
            cls._set("user_agent", user_agent)

    @classmethod
    def _get(cls, key) -> str | None:
        return keyring.get_password(cls.SERVICE_NAME, key)

    @classmethod
    def _set(cls, key, value) -> None:
        keyring.set_password(cls.SERVICE_NAME, key, value)

    @classmethod
    def delete(cls, key) -> None:
        keyring.delete_password(cls.SERVICE_NAME, key)


__all__ = ["SecretStorage"]
