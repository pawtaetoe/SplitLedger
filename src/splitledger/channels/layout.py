# a choice title may carry a description after this separator
SEP = " — "


# the short label and the optional description of one choice
def split_choice(title: str) -> tuple[str, str | None]:
    label, _, description = title.partition(SEP)
    return label, description or None


# message body with described choices as bullets above real buttons
def with_descriptions(text: str, buttons: list[tuple[str, str]]) -> str:
    described = []
    for _, title in buttons:
        label, description = split_choice(title)
        if description:
            described.append(f"- *{label}*{SEP}{description}")
    return f"{text}\n\n" + "\n".join(described) if described else text


# message body with every choice numbered for chats without buttons
def numbered(text: str, buttons: list[tuple[str, str]]) -> str:
    lines = [f"*{number}* · {title}" for number, (_, title) in enumerate(buttons, 1)]
    return f"{text}\n\n" + "\n".join(lines) + "\n\n_Reply with a number_"
