"""Comandos de Telegram tipados (spec §5.3, sin `/peso`, con `/fuente` y `/token_vence`). Puro.

`parse_command` solo valida la forma. Las URLs se validan con `SafeUrl` y los precios con
`parse_price` cuando ya se conoce la fuente (app/inbox).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

CATEGORIES = frozenset({"tecnologia", "componentes", "hogar", "ropa", "otros"})
DEFAULT_CATEGORY = "otros"
MAX_URLS = 20
MAX_EXCLUDES = 20
MUTE_DAYS = range(1, 91)

PRICE_HELP = "Formato de precio no válido. Ejemplos: 250.000 (COP) o 99,99 (USD)."
_PRICE_SHAPE = re.compile(r"^(?:US)?\$?\d[\d.,]*$")
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_DROP_REF = re.compile(r"^(?:[pab]\d{1,9}|auto)$")
_WATCH_OR_LISTING = re.compile(r"^(?:p|L)\d{1,12}$")
_COMMAND_RE = re.compile(r"^/([a-z_]+)(?:@[A-Za-z0-9_]+)?$")


@dataclass(frozen=True, slots=True)
class Follow:
    urls: tuple[str, ...]
    target_minor_text: str | None
    category: str | None


@dataclass(frozen=True, slots=True)
class Search:
    source: str
    query: str
    exclude: tuple[str, ...]
    min_text: str | None
    max_text: str | None
    category: str


@dataclass(frozen=True, slots=True)
class ListCmd:
    filter: str | None


@dataclass(frozen=True, slots=True)
class Drop:
    ref: str


@dataclass(frozen=True, slots=True)
class Target:
    ref: str
    price_text: str | None  # None = quitar el objetivo


@dataclass(frozen=True, slots=True)
class Category:
    ref: str
    category: str


@dataclass(frozen=True, slots=True)
class Silence:
    ref: str  # p12 | L1234 | todo
    days: int


@dataclass(frozen=True, slots=True)
class SourceToggle:
    source: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class TokenExpiry:
    day: date


@dataclass(frozen=True, slots=True)
class Status:
    pass


@dataclass(frozen=True, slots=True)
class Help:
    pass


@dataclass(frozen=True, slots=True)
class Topics:
    pass


@dataclass(frozen=True, slots=True)
class CommandRejected:
    reason: str  # texto para David, en español


Command = (
    Follow
    | Search
    | ListCmd
    | Drop
    | Target
    | Category
    | Silence
    | SourceToggle
    | TokenExpiry
    | Status
    | Help
    | Topics
)


_Parser = Callable[[list[str]], Command | CommandRejected]


def parse_command(text: str) -> Command | CommandRejected:
    tokens = text.split()
    if not tokens:
        return CommandRejected("Escribe un comando. Usa /ayuda para ver la sintaxis.")
    m = _COMMAND_RE.fullmatch(tokens[0])
    if m is None:
        return CommandRejected("No entendí. Usa /ayuda para ver los comandos.")
    name, args = m.group(1), tokens[1:]
    parser = _PARSERS.get(name)
    if parser is None:
        return CommandRejected(f"Comando desconocido: /{name}. Usa /ayuda.")
    return parser(args)


def _category_tag(token: str) -> str | CommandRejected:
    cat = token[1:].lower()
    if cat not in CATEGORIES:
        return CommandRejected(f"Categoría desconocida. Usa: {', '.join(sorted(CATEGORIES))}.")
    return cat


def _price_text(token: str) -> str | CommandRejected:
    return token if _PRICE_SHAPE.fullmatch(token) else CommandRejected(PRICE_HELP)


def _follow(args: list[str]) -> Command | CommandRejected:
    urls: list[str] = []
    target: str | None = None
    category: str | None = None
    for tok in args:
        if tok.startswith("#"):
            cat = _category_tag(tok)
            if isinstance(cat, CommandRejected):
                return cat
            category = cat
        elif tok.lower().startswith(("https://", "http://")):
            urls.append(tok)
        else:
            price = _price_text(tok)
            if isinstance(price, CommandRejected) or target is not None:
                return CommandRejected(PRICE_HELP)
            target = price
    if not urls:
        return CommandRejected("Falta la URL. Ejemplo: /seguir https://www.exito.com/... 250.000")
    if len(urls) > MAX_URLS:
        return CommandRejected(f"Máximo {MAX_URLS} URLs por mensaje.")
    return Follow(tuple(urls), target, category)


def _search(args: list[str]) -> Command | CommandRejected:
    if not args or not _SOURCE_RE.fullmatch(args[0]):
        return CommandRejected("Uso: /buscar <fuente> <consulta> [-excluir] [min:] [max:] [#cat]")
    words: list[str] = []
    exclude: list[str] = []
    min_text = max_text = None
    category = DEFAULT_CATEGORY
    for tok in args[1:]:
        low = tok.lower()
        if tok.startswith("#"):
            cat = _category_tag(tok)
            if isinstance(cat, CommandRejected):
                return cat
            category = cat
        elif low.startswith(("min:", "max:")):
            price = _price_text(tok[4:])
            if isinstance(price, CommandRejected):
                return price
            if low.startswith("min:"):
                min_text = price
            else:
                max_text = price
        elif tok.startswith("-") and len(tok) > 1:
            exclude.append(low[1:])
        else:
            words.append(low)
    query = " ".join(words)
    if not 2 <= len(query) <= 100:
        return CommandRejected("La consulta debe tener entre 2 y 100 caracteres.")
    if len(exclude) > MAX_EXCLUDES:
        return CommandRejected(f"Máximo {MAX_EXCLUDES} términos excluidos.")
    return Search(args[0], query, tuple(exclude), min_text, max_text, category)


def _list(args: list[str]) -> Command | CommandRejected:
    if len(args) > 1:
        return CommandRejected("Uso: /lista [#categoria|fuente]")
    return ListCmd(args[0] if args else None)


def _drop(args: list[str]) -> Command | CommandRejected:
    if len(args) != 1 or not _DROP_REF.fullmatch(args[0]):
        return CommandRejected("Uso: /dejar <p12|a12|b3|auto>")
    return Drop(args[0])


def _target(args: list[str]) -> Command | CommandRejected:
    if len(args) != 2 or not re.fullmatch(r"p\d{1,9}", args[0]):
        return CommandRejected("Uso: /objetivo <p12> <precio|no>")
    if args[1].lower() == "no":
        return Target(args[0], None)
    price = _price_text(args[1])
    if isinstance(price, CommandRejected):
        return price
    return Target(args[0], price)


def _category(args: list[str]) -> Command | CommandRejected:
    if len(args) != 2 or not _WATCH_OR_LISTING.fullmatch(args[0]):
        return CommandRejected("Uso: /categoria <p12|L1234> <categoria>")
    cat = _category_tag("#" + args[1])
    if isinstance(cat, CommandRejected):
        return cat
    return Category(args[0], cat)


def _silence(args: list[str]) -> Command | CommandRejected:
    usage = CommandRejected("Uso: /silenciar <p12|L1234|todo> <días de 1 a 90>")
    if len(args) != 2 or not (args[0] == "todo" or _WATCH_OR_LISTING.fullmatch(args[0])):
        return usage
    if not args[1].isdigit() or int(args[1]) not in MUTE_DAYS:
        return usage
    return Silence(args[0], int(args[1]))


def _source(args: list[str]) -> Command | CommandRejected:
    if len(args) != 2 or args[0] not in ("on", "off") or not _SOURCE_RE.fullmatch(args[1]):
        return CommandRejected("Uso: /fuente <on|off> <codigo>")
    return SourceToggle(args[1], args[0] == "on")


def _token(args: list[str]) -> Command | CommandRejected:
    usage = CommandRejected("Uso: /token_vence AAAA-MM-DD")
    if len(args) != 1 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[0]):
        return usage
    try:
        return TokenExpiry(date.fromisoformat(args[0]))
    except ValueError:
        return usage


def _no_args(cmd: Command) -> _Parser:
    def parse(args: list[str]) -> Command | CommandRejected:
        return cmd if not args else CommandRejected("Este comando no lleva argumentos.")

    return parse


_PARSERS: dict[str, _Parser] = {
    "seguir": _follow,
    "buscar": _search,
    "lista": _list,
    "dejar": _drop,
    "objetivo": _target,
    "categoria": _category,
    "silenciar": _silence,
    "fuente": _source,
    "token_vence": _token,
    "estado": _no_args(Status()),
    "ayuda": _no_args(Help()),
    "temas": _no_args(Topics()),
}

# Menú que Telegram muestra al escribir «/» en el grupo (`setup-bot`). Sin /temas: el grupo
# tiene los temas apagados y el comando los recrearía.
BOT_MENU: tuple[tuple[str, str], ...] = (
    ("seguir", "Seguir hasta 20 URLs: /seguir <url…> [objetivo] [#cat]"),
    ("buscar", "Buscar en una tienda: /buscar <fuente> <consulta> [min:] [max:]"),
    ("lista", "Ver los seguimientos: /lista [#cat|fuente]"),
    ("dejar", "Dejar de seguir: /dejar <p12|a12|b3|auto>"),
    ("objetivo", "Precio objetivo: /objetivo <p12> <precio|no>"),
    ("categoria", "Cambiar la categoría: /categoria <p12|L1234> <cat>"),
    ("silenciar", "Silenciar avisos: /silenciar <p12|L1234|todo> <días>"),
    ("fuente", "Encender o apagar una fuente: /fuente <on|off> <codigo>"),
    ("estado", "Salud de las fuentes y del bot"),
    ("token_vence", "Fecha en que vence el token: /token_vence AAAA-MM-DD"),
    ("ayuda", "Todos los comandos y su sintaxis"),
)
