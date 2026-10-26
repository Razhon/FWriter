CAN_MANAGE = frozenset({"author", "coauthor"})


def can_manage(role: str, readonly: bool) -> bool:
    """Название главы и «Новая глава»; текст и отправка — при not readonly."""
    return role in CAN_MANAGE and not readonly
