import json
import logging
import math
import re
import string
from abc import abstractmethod
from typing import Any, Dict, Iterable, Iterator, List, MutableMapping, Optional, Sequence, TypeVar, Union
from urllib.parse import urlparse
import operator
import threading
from datetime import datetime
from uuid import UUID

import attrs
from packaging.version import parse as parse_version
import requests
from tabulate import tabulate
from typing_extensions import Self

from data_diff.version import __version__
from rich.status import Status


# -- Common --


def join_iter(joiner: Any, iterable: Iterable) -> Iterable:
    it = iter(iterable)
    try:
        yield next(it)
    except StopIteration:
        return
    for i in it:
        yield joiner
        yield i


def safezip(*args):
    "zip but makes sure all sequences are the same length"
    lens = list(map(len, args))
    if len(set(lens)) != 1:
        raise ValueError(f"Mismatching lengths in arguments to safezip: {lens}")
    return zip(*args)


UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def is_uuid(u: str) -> bool:
    # E.g., hashlib.md5(b'hello') is a 32-letter hex number, but not an UUID.
    # It would fail UUID-like comparison (< & >) because of casing and dashes.
    if not UUID_PATTERN.fullmatch(u):
        return False
    try:
        UUID(u)
    except ValueError:
        return False
    return True


def match_regexps(regexps: Dict[str, Any], s: str) -> Sequence[tuple]:
    for regexp, v in regexps.items():
        m = re.match(regexp + "$", s)
        if m:
            yield m, v


# -- Schema --

V = TypeVar("V")


class CaseAwareMapping(MutableMapping[str, V]):
    @abstractmethod
    def get_key(self, key: str) -> str:
        ...

    def new(self, initial=()) -> Self:
        return type(self)(initial)


class CaseInsensitiveDict(CaseAwareMapping):
    def __init__(self, initial) -> None:
        super().__init__()
        self._dict = {k.lower(): (k, v) for k, v in dict(initial).items()}

    def __getitem__(self, key: str) -> V:
        return self._dict[key.lower()][1]

    def __iter__(self) -> Iterator[V]:
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._dict)

    def __setitem__(self, key: str, value) -> None:
        k = key.lower()
        if k in self._dict:
            key = self._dict[k][0]
        self._dict[k] = key, value

    def __delitem__(self, key: str) -> None:
        del self._dict[key.lower()]

    def get_key(self, key: str) -> str:
        return self._dict[key.lower()][0]

    def __repr__(self) -> str:
        return repr(dict(self.items()))


class CaseSensitiveDict(dict, CaseAwareMapping):
    def get_key(self, key):
        self[key]  # Throw KeyError if key doesn't exist
        return key

    def as_insensitive(self):
        return CaseInsensitiveDict(self)


# -- Alphanumerics --

alphanums = " -" + string.digits + string.ascii_uppercase + "_" + string.ascii_lowercase


@attrs.define(frozen=True)
class ArithString:
    @classmethod
    def new(cls, *args, **kw) -> Self:
        return cls(*args, **kw)

    def range(self, other: "ArithString", count: int) -> List[Self]:
        assert isinstance(other, ArithString)
        checkpoints = split_space(self.int, other.int, count)
        return [self.new(int=i) for i in checkpoints]


def _any_to_uuid(v: Union[str, int, UUID, "ArithUUID"]) -> UUID:
    if isinstance(v, ArithUUID):
        return v.uuid
    elif isinstance(v, UUID):
        return v
    elif isinstance(v, str):
        return UUID(v)
    elif isinstance(v, int):
        return UUID(int=v)
    else:
        raise ValueError(f"Cannot convert a value to UUID: {v!r}")


@attrs.define(frozen=True, eq=False, order=False)
class ArithUUID(ArithString):
    "A UUID that supports basic arithmetic (add, sub)"

    uuid: UUID = attrs.field(converter=_any_to_uuid)
    lowercase: Optional[bool] = None
    uppercase: Optional[bool] = None

    def range(self, other: "ArithUUID", count: int) -> List[Self]:
        assert isinstance(other, ArithUUID)
        checkpoints = split_space(self.uuid.int, other.uuid.int, count)
        return [attrs.evolve(self, uuid=i) for i in checkpoints]

    def __int__(self) -> int:
        return self.uuid.int

    def __add__(self, other: int) -> Self:
        if isinstance(other, int):
            return attrs.evolve(self, uuid=self.uuid.int + other)
        return NotImplemented

    def __sub__(self, other: Union["ArithUUID", int]):
        if isinstance(other, int):
            return attrs.evolve(self, uuid=self.uuid.int - other)
        elif isinstance(other, ArithUUID):
            return self.uuid.int - other.uuid.int
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ArithUUID):
            return self.uuid == other.uuid
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        if isinstance(other, ArithUUID):
            return self.uuid != other.uuid
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, ArithUUID):
            return self.uuid > other.uuid
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, ArithUUID):
            return self.uuid < other.uuid
        return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, ArithUUID):
            return self.uuid >= other.uuid
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, ArithUUID):
            return self.uuid <= other.uuid
        return NotImplemented


def numberToAlphanum(num: int, base: str = alphanums) -> str:
    digits = []
    while num > 0:
        num, remainder = divmod(num, len(base))
        digits.append(remainder)
    return "".join(base[i] for i in digits[::-1])


def alphanumToNumber(alphanum: str, base: str = alphanums) -> int:
    num = 0
    for c in alphanum:
        num = num * len(base) + base.index(c)
    return num


def justify_alphanums(s1: str, s2: str):
    max_len = max(len(s1), len(s2))
    s1 = s1.ljust(max_len)
    s2 = s2.ljust(max_len)
    return s1, s2


def alphanums_to_numbers(s1: str, s2: str):
    s1, s2 = justify_alphanums(s1, s2)
    n1 = alphanumToNumber(s1)
    n2 = alphanumToNumber(s2)
    return n1, n2


@attrs.define(frozen=True, eq=False, order=False, repr=False)
class ArithAlphanumeric(ArithString):
    _str: str
    _max_len: Optional[int] = None

    def __attrs_post_init__(self) -> None:
        if self._str is None:
            raise ValueError("Alphanum string cannot be None")
        if self._max_len and len(self._str) > self._max_len:
            raise ValueError(f"Length of alphanum value '{str}' is longer than the expected {self._max_len}")

        for ch in self._str:
            if ch not in alphanums:
                raise ValueError(f"Unexpected character {ch} in alphanum string")

    # @property
    # def int(self):
    #     return alphanumToNumber(self._str, alphanums)

    def __str__(self) -> str:
        s = self._str
        if self._max_len:
            s = s.rjust(self._max_len, alphanums[0])
        return s

    def __len__(self) -> int:
        return len(self._str)

    def __repr__(self) -> str:
        return f'alphanum"{self._str}"'

    def __add__(self, other: "Union[ArithAlphanumeric, int]") -> Self:
        if isinstance(other, int):
            if other != 1:
                raise NotImplementedError("not implemented for arbitrary numbers")
            num = alphanumToNumber(self._str)
            return self.new(numberToAlphanum(num + 1))

        return NotImplemented

    def range(self, other: "ArithAlphanumeric", count: int) -> List[Self]:
        assert isinstance(other, ArithAlphanumeric)
        n1, n2 = alphanums_to_numbers(self._str, other._str)
        split = split_space(n1, n2, count)
        return [self.new(numberToAlphanum(s)) for s in split]

    def __sub__(self, other: "Union[ArithAlphanumeric, int]") -> float:
        if isinstance(other, ArithAlphanumeric):
            n1, n2 = alphanums_to_numbers(self._str, other._str)
            return n1 - n2

        return NotImplemented

    def __ge__(self, other) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        return self._str >= other._str

    def __lt__(self, other) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        return self._str < other._str

    def __eq__(self, other) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        return self._str == other._str

    def new(self, *args, **kw) -> Self:
        return type(self)(*args, **kw, max_len=self._max_len)


def number_to_human(n):
    millnames = ["", "k", "m", "b"]
    n = float(n)
    millidx = max(
        0,
        min(len(millnames) - 1, int(math.floor(0 if n == 0 else math.log10(abs(n)) / 3))),
    )

    return "{:.0f}{}".format(n / 10 ** (3 * millidx), millnames[millidx])


def split_space(start, end, count) -> List[int]:
    size = end - start
    assert count <= size, (count, size)
    return list(range(start, end, (size + 1) // (count + 1)))[1 : count + 1]


def remove_passwords_in_dict(d: dict, replace_with: str = "***"):
    for k, v in d.items():
        if k == "password":
            d[k] = replace_with
        elif k == "filepath":
            if "motherduck_token=" in v:
                d[k] = v.split("motherduck_token=")[0] + f"motherduck_token={replace_with}"
        elif isinstance(v, dict):
            remove_passwords_in_dict(v, replace_with)
        elif k.startswith("database"):
            d[k] = remove_password_from_url(v, replace_with)


def _join_if_any(sym, args):
    args = list(args)
    if not args:
        return ""
    return sym.join(str(a) for a in args if a)


def remove_password_from_url(url: str, replace_with: str = "***") -> str:
    if "motherduck_token=" in url:
        replace_token_url = url.split("motherduck_token=")[0] + f"motherduck_token={replace_with}"
        return replace_token_url
    else:
        parsed = urlparse(url)
        account = parsed.username or ""
        if parsed.password:
            account += ":" + replace_with
        host = _join_if_any(":", filter(None, [parsed.hostname, parsed.port]))
        netloc = _join_if_any("@", filter(None, [account, host]))
        replaced = parsed._replace(netloc=netloc)
        return replaced.geturl()


def match_like(pattern: str, strs: Sequence[str]) -> Iterable[str]:
    reo = re.compile(pattern.replace("%", ".*").replace("?", ".") + "$")
    for s in strs:
        if reo.match(s):
            yield s


def accumulate(iterable, func=operator.add, *, initial=None):
    "Return running totals"
    # Taken from https://docs.python.org/3/library/itertools.html#itertools.accumulate, to backport 'initial' to 3.7
    it = iter(iterable)
    total = initial
    if initial is None:
        try:
            total = next(it)
        except StopIteration:
            return
    yield total
    for element in it:
        total = func(total, element)
        yield total


def run_as_daemon(threadfunc, *args):
    th = threading.Thread(target=threadfunc, args=args)
    th.daemon = True
    th.start()
    return th


def getLogger(name):
    return logging.getLogger(name.rsplit(".", 1)[-1])


def eval_name_template(name):
    def get_timestamp(_match):
        return datetime.now().isoformat("_", "seconds").replace(":", "_")

    return re.sub("%t", get_timestamp, name)


def truncate_error(error: str):
    first_line = error.split("\n", 1)[0]
    return re.sub("'(.*?)'", "'***'", first_line)


def get_from_dict_with_raise(dictionary: Dict, key: str, exception: Exception):
    if dictionary is None:
        raise exception
    result = dictionary.get(key)
    if result is None:
        raise exception
    return result


class Vector(tuple):

    """Immutable implementation of a regular vector over any arithmetic value

    Implements a product order - https://en.wikipedia.org/wiki/Product_order

    Partial implementation: Only the needed functionality is implemented
    """

    def __lt__(self, other: "Vector") -> bool:
        if isinstance(other, Vector):
            return all(a < b for a, b in safezip(self, other))
        return NotImplemented

    def __le__(self, other: "Vector") -> bool:
        if isinstance(other, Vector):
            return all(a <= b for a, b in safezip(self, other))
        return NotImplemented

    def __gt__(self, other: "Vector") -> bool:
        if isinstance(other, Vector):
            return all(a > b for a, b in safezip(self, other))
        return NotImplemented

    def __ge__(self, other: "Vector") -> bool:
        if isinstance(other, Vector):
            return all(a >= b for a, b in safezip(self, other))
        return NotImplemented

    def __eq__(self, other: "Vector") -> bool:
        if isinstance(other, Vector):
            return all(a == b for a, b in safezip(self, other))
        return NotImplemented

    def __sub__(self, other: "Vector") -> "Vector":
        if isinstance(other, Vector):
            return Vector((a - b) for a, b in safezip(self, other))
        raise NotImplementedError()

    def __repr__(self) -> str:
        return "(%s)" % ", ".join(str(k) for k in self)


def dbt_diff_string_template(
    rows_added: str, rows_removed: str, rows_updated: str, rows_unchanged: str, extra_info_dict: Dict, extra_info_str
) -> str:
    string_output = f"\n{tabulate([[rows_added, rows_removed]], headers=['Rows Added', 'Rows Removed'])}"

    string_output += f"\n\nUpdated Rows: {rows_updated}\n"
    string_output += f"Unchanged Rows: {rows_unchanged}\n\n"

    string_output += extra_info_str

    for k, v in extra_info_dict.items():
        string_output += f"\n{k}: {v}"

    return string_output


def _jsons_equiv(a: str, b: str):
    try:
        return json.loads(a) == json.loads(b)
    except (ValueError, TypeError, json.decoder.JSONDecodeError):  # not valid jsons
        return False


def diffs_are_equiv_jsons(diff: list, json_cols: dict):
    overriden_diff_cols = set()
    if (len(diff) != 2) or ({diff[0][0], diff[1][0]} != {"+", "-"}):
        return False, overriden_diff_cols
    match = True
    for i, (col_a, col_b) in enumerate(safezip(diff[0][1][1:], diff[1][1][1:])):  # index 0 is extra_columns first elem
        # we only attempt to parse columns of JSON type, but we still need to check if non-json columns don't match
        match = col_a == col_b
        if not match and (i in json_cols):
            if _jsons_equiv(col_a, col_b):
                overriden_diff_cols.add(json_cols[i])
                match = True
        if not match:
            break
    return match, overriden_diff_cols


def columns_removed_template(columns_removed) -> str:
    columns_removed_str = f"Column(s) removed: {columns_removed}\n"
    return columns_removed_str


def columns_added_template(columns_added) -> str:
    columns_added_str = f"Column(s) added: {columns_added}\n"
    return columns_added_str


def columns_type_changed_template(columns_type_changed) -> str:
    columns_type_changed_str = f"Type change: {columns_type_changed}\n"
    return columns_type_changed_str


def no_differences_template() -> str:
    return "[bold][green]No row differences[/][/]\n"


def print_version_info() -> None:
    base_version_string = f"Running with data-diff={__version__}"
    logger = getLogger(__name__)
    latest_version = None
    try:
        response = requests.get(url="https://pypi.org/pypi/data-diff/json", timeout=3)
        response.raise_for_status()
        response_json = response.json()
        latest_version = response_json["info"]["version"]
    except Exception as ex:
        logger.debug(f"Failed checking version: {ex}")

    if latest_version and parse_version(__version__) < parse_version(latest_version):
        print(f"{base_version_string} (Update {latest_version} is available!)")
    else:
        print(base_version_string)


class LogStatusHandler(logging.Handler):
    """
    This log handler can be used to update a rich.status every time a log is emitted.
    """

    def __init__(self) -> None:
        super().__init__()
        self.status = Status("")
        self.prefix = ""
        self.diff_status = {}

    def emit(self, record):
        log_entry = self.format(record)
        if self.diff_status:
            self._update_diff_status(log_entry)
        else:
            self.status.update(self.prefix + log_entry)

    def set_prefix(self, prefix_string):
        self.prefix = prefix_string

    def diff_started(self, model_name):
        self.diff_status[model_name] = "[yellow]In Progress[/]"
        self._update_diff_status()

    def diff_finished(self, model_name):
        self.diff_status[model_name] = "[green]Finished   [/]"
        self._update_diff_status()

    def _update_diff_status(self, log=None):
        status_string = "\n"
        for model_name, status in self.diff_status.items():
            status_string += f"{status} {model_name}\n"
        self.status.update(f"{status_string}{log or ''}")


class UnknownMeta(type):
    def __instancecheck__(self, instance):
        return instance is Unknown

    def __repr__(self) -> str:
        return "Unknown"


class Unknown(metaclass=UnknownMeta):
    def __bool__(self) -> bool:
        raise TypeError()

    def __new__(class_, *args, **kwargs):
        raise RuntimeError("Unknown is a singleton")
