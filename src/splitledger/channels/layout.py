import base64

# a choice title may carry a description after this separator
SEP = " — "
# whatsapp label limits, longer ones are rejected
BUTTON_CHARS = 20
ROW_CHARS = 24


# a button or list row label cut to fit, an ellipsis shows it was cut, keep leaves room after it
def fit(label: str, row: bool = False, keep: int = 0) -> str:
    limit = (ROW_CHARS if row else BUTTON_CHARS) - keep
    return label if len(label) <= limit else label[: limit - 1].rstrip() + "…"


# fitted labels with repeats numbered, whatsapp rejects two buttons with one title
def unique_labels(labels: list[str], row: bool = False) -> list[str]:
    seen: dict[str, int] = {}
    fitted: list[str] = []
    for label in labels:
        short = fit(label, row)
        count = seen[short] = seen.get(short, 0) + 1
        if count > 1:
            tag = f" {count}"
            short = fit(label, row, len(tag)) + tag
        fitted.append(short)
    return fitted


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


# a path as url safe base64 with no padding, so no character can upset a button url
def link_key(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
