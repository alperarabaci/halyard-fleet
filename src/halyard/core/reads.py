"""The shell commands that may go through without a card: reads, understood whole.

**Why this exists.** A phone asked three hundred times a day is a phone somebody
stops reading. Measured on one machine from 2026-07-20 to 2026-09-25: 15,951
cards, 99% allowed, and about four in ten shell cards a `grep`, a `sed -n` or a
`git log` inside the project. `HALYARD_ALLOW_RISK_AT_OR_BELOW: low` was the
first answer, and it judged a command by regular expressions anchored at its
start, taking the highest risk of whatever matched. A part no rule recognised
raised nothing, so one read made a whole command low: `ls | xargs rm`,
`echo ok; python3 x.py`, `find . -delete` and `git branch -D` all went through
unasked. So did a `Write` whose path contained `pytest`, because the rules read
every tool's text as if it were a command.

**The rule now.** A command goes through without asking only when every part
of it is understood:

- it is parsed the way a shell would parse it — quotes, `2>&1`, `/dev/null`,
  `&&`, `|`, `;` and newlines — and anything the parser cannot vouch for
  (`$(…)`, a `$VAR`, a backtick, a heredoc, a subshell, a redirect into a file)
  is not understood;
- every part is a command known to read, using only options known to read:
  `find` without `-exec` or `-delete`, `rg` without `--pre`, `git` with a read
  subcommand and no `-c`, `sed` with a print-only script — and never `awk`,
  `xargs` or an interpreter, which can run anything;
- every path it names, followed through each `cd` and every symlink, is inside
  the project, and none of them is a file that may hold a secret.

Not understood is the default, and it means a card, not a refusal: the person
answers it as before. Being wrong here costs a question; being wrong the other
way hands an agent something nobody saw.

**What this does not see.** It judges the text of the command. A recursive
search is judged by where it starts, not by every file under it: `rg` keeps to
what git does not ignore, while `grep -r` reads everything under its root,
`.env` included. The runtimes' own read tools are not gated at all, which is
the larger version of the same limit.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: The tools a command arrives as. Only these are judged here: a file write is
#: granted by where it writes (`writes.py`), an MCP call by its name
#: (`tools.py`), and nothing else is granted at all.
SHELL_TOOLS = frozenset({"Bash", "bash", "exec_command", "exec", "shell", "run_command"})

#: Written into the audit record of every command this let through, so a rule
#: that later turns out wrong can be traced to the grants it made. `reads/2`
#: added a project's `runs:`.
VERSION = "reads/2"

#: Files that may hold a secret. Asked about even inside the project.
SENSITIVE = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials*",
    "secrets.*",
    "halyard.yaml",
)

#: What a template of one of those is called. `.env.example` is committed so
#: that people know which settings exist, and holds no values worth hiding.
TEMPLATES = (".example", ".sample", ".template", ".dist")

#: Directories that may hold secrets, wherever they sit in a path.
SENSITIVE_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})

#: Environment settings a read may be run with. Anything else can change what
#: a program does — `GIT_EXTERNAL_DIFF` runs a program, `PATH` changes which
#: one runs — so it is not understood.
SAFE_ENVIRONMENT = frozenset({"LC_ALL", "LANG", "LC_CTYPE", "NO_COLOR", "TERM", "COLUMNS"})

#: Where output may be sent without it being a write.
HARMLESS_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})


@dataclass(frozen=True)
class Verdict:
    """Whether a command may go through without a card, and why — in a few
    words for the audit log when it may, and for the log when it may not."""

    allowed: bool
    why: str


@dataclass
class _Word:
    text: str
    #: An unquoted `*`, `?` or `[`, which the shell expands into paths.
    globbed: bool = False
    #: An unquoted `~` at the start, which the shell expands into a home.
    tilde: bool = False


@dataclass
class _Part:
    """One simple command, and the files it reads through `<`."""

    words: list[_Word] = field(default_factory=list)
    inputs: list[_Word] = field(default_factory=list)


class _NotUnderstoodError(Exception):
    """Raised with a reason, in plain words, wherever judging gives up."""


# --- parsing ------------------------------------------------------------------


def _parse(command: str) -> list[_Part]:
    """The command's simple commands, the way a shell would split them.

    Deliberately small: everything it does not recognise is refused rather than
    approximated, because an approximation is where a hidden command lives.
    """
    parts: list[_Part] = []
    part = _Part()
    buffer: list[str] = []
    started = quoted = globbed = tilde = False
    text = command
    i = 0

    def end_word() -> None:
        nonlocal buffer, started, quoted, globbed, tilde
        if started:
            part.words.append(_Word("".join(buffer), globbed=globbed, tilde=tilde))
        buffer, started, quoted, globbed, tilde = [], False, False, False, False

    def end_part() -> None:
        nonlocal part
        end_word()
        parts.append(part)
        part = _Part()

    def target(at: int) -> tuple[_Word, int]:
        """The word after a redirection, and where parsing resumes."""
        while at < len(text) and text[at] in " \t":
            at += 1
        end = at
        quote = ""
        while end < len(text) and (quote or text[end] not in " \t\n;|&<>"):
            if quote and text[end] == quote:
                quote = ""
            elif not quote and text[end] in "'\"":
                quote = text[end]
            end += 1
        parsed = _parse(text[at:end]) if end > at else []
        if len(parsed) != 1 or len(parsed[0].words) != 1 or parsed[0].inputs:
            raise _NotUnderstoodError("a redirection whose target is not one plain word")
        return parsed[0].words[0], end

    while i < len(text):
        ch = text[i]
        if ch == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise _NotUnderstoodError("an unclosed quote")
            buffer.append(text[i + 1 : end])
            started = quoted = True
            i = end + 1
        elif ch == '"':
            i = _double_quoted(text, i + 1, buffer)
            started = quoted = True
        elif ch == "\\":
            if text[i + 1 : i + 2] == "\n":
                i += 2
                continue
            buffer.append(text[i + 1 : i + 2])
            started = quoted = True
            i += 2
        elif ch in " \t":
            end_word()
            i += 1
        elif ch == "#" and not started:
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif ch == "$":
            follower = text[i + 1 : i + 2]
            if follower and follower not in " \t\n;|&":
                raise _NotUnderstoodError("an expansion, which is only known when it runs")
            buffer.append(ch)
            started = True
            i += 1
        elif ch == "`":
            raise _NotUnderstoodError("a command in backticks")
        elif ch in "(){}!":
            raise _NotUnderstoodError(f"`{ch}`, which groups or runs commands")
        elif ch in ";\n":
            end_part()
            i += 1
        elif ch == "|":
            end_part()
            i += 2 if text[i : i + 2] in ("||", "|&") else 1
        elif ch == "&":
            if text.startswith("&&", i):
                end_part()
                i += 2
            elif text.startswith("&>", i):
                end_word()
                where, i = target(i + (3 if text.startswith("&>>", i) else 2))
                if where.text not in HARMLESS_TARGETS:
                    raise _NotUnderstoodError("output written into a file")
            else:
                raise _NotUnderstoodError("a command sent to the background")
        elif ch == "<":
            if text[i : i + 2] in ("<<", "<>", "<("):
                raise _NotUnderstoodError("a heredoc, or input that is itself a command")
            end_word()
            if text.startswith("<&", i):
                i += 2
                continue
            where, i = target(i + 1)
            part.inputs.append(where)
        elif ch == ">":
            # A number written against it names the stream (`2>`); it is not
            # a word of the command.
            if started and not quoted and "".join(buffer).isdigit():
                buffer, started = [], False
            end_word()
            if text.startswith(">(", i):
                raise _NotUnderstoodError("output that is itself a command")
            if text.startswith(">&", i) and text[i + 2 : i + 3] and text[i + 2] in "0123456789-":
                i += 3
                continue
            skip = 2 if text[i : i + 2] in (">>", ">|", ">&") else 1
            where, i = target(i + skip)
            if where.text not in HARMLESS_TARGETS:
                raise _NotUnderstoodError("output written into a file")
        else:
            if ch in "*?[":
                globbed = True
            if ch == "~" and not started:
                tilde = True
            buffer.append(ch)
            started = True
            i += 1
    end_part()
    return [p for p in parts if p.words or p.inputs]


def _double_quoted(text: str, at: int, buffer: list[str]) -> int:
    """Read to the closing `"`, into `buffer`. Where parsing resumes."""
    while at < len(text):
        ch = text[at]
        if ch == '"':
            return at + 1
        if ch == "\\" and text[at + 1 : at + 2] and text[at + 1] in '$`"\\\n':
            if text[at + 1] != "\n":
                buffer.append(text[at + 1])
            at += 2
            continue
        if ch == "`" or (ch == "$" and text[at + 1 : at + 2] not in ("", " ", "\t", '"')):
            raise _NotUnderstoodError("an expansion inside double quotes")
        buffer.append(ch)
        at += 1
    raise _NotUnderstoodError("an unclosed quote")


# --- paths --------------------------------------------------------------------


@dataclass(frozen=True)
class _Where:
    """The project, and every directory a part may be running in.

    More than one after a `cd`, because whether it happened is not known from
    the text: after `cd x; cat y` the `cat` runs in `x` if the `cd` worked and
    where it started if it did not — and even `cd x && true; cat y` leaves that
    open. A path is only inside the project if it is from every one of them.
    """

    project: str
    cwds: tuple[str, ...]

    def resolve(self, word: _Word) -> list[str]:
        """Every real path this word can name, from every directory — and its
        matches too, for a glob."""
        raw = os.path.expanduser(word.text) if word.tilde else word.text
        found: list[str] = []
        for cwd in self.cwds:
            joined = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
            if not word.globbed:
                found.append(os.path.realpath(joined))
                continue
            prefix = re.split(r"[*?\[]", joined, maxsplit=1)[0]
            found.append(os.path.realpath(os.path.dirname(prefix) or os.sep))
            found += [os.path.realpath(match) for match in glob.glob(joined)]
        return found

    def inside(self, path: str) -> bool:
        return os.path.commonpath([path, self.project]) == self.project


def _sensitive(path: str) -> str | None:
    parts = Path(path).parts
    for directory in parts[:-1]:
        if directory in SENSITIVE_DIRS:
            return directory
    name = parts[-1] if parts else ""
    if name.endswith(TEMPLATES):
        return None
    return next((name for pattern in SENSITIVE if fnmatch.fnmatch(name, pattern)), None)


def _check_path(word: _Word, where: _Where) -> None:
    if word.text == "-":
        return
    for path in where.resolve(word):
        if not where.inside(path):
            raise _NotUnderstoodError(f"`{word.text}` is outside the project")
        if (secret := _sensitive(path)) is not None:
            raise _NotUnderstoodError(f"`{word.text}` may hold a secret ({secret})")


def _repository(start: str) -> str | None:
    """The nearest directory holding `.git`, from `start` upwards."""
    here = start
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


# --- options ------------------------------------------------------------------


@dataclass
class _Args:
    """A command's words after its name, split into options and operands."""

    options: list[str]
    operands: list[_Word]


def _split(words: Sequence[_Word], *, taking: frozenset[str] = frozenset()) -> _Args:
    """Options and operands. `taking` names options whose value is the next word,
    kept with the options so it is never mistaken for a file."""
    options: list[str] = []
    operands: list[_Word] = []
    ended = skip = False
    for word in words:
        if skip:
            options.append(word.text)
            skip = False
        elif not ended and word.text == "--":
            ended = True
        elif not ended and word.text.startswith("-") and word.text != "-":
            options.append(word.text)
            skip = word.text in taking
        else:
            operands.append(word)
    return _Args(options, operands)


def _has(options: Sequence[str], short: str = "", long: Sequence[str] = ()) -> bool:
    """Whether a refused option appears, in any spelling.

    GNU tools and git take any unambiguous start of a long option — `--out`
    is `--output` — so a start of a refused name counts as the name.
    """
    for option in options:
        if option.startswith("--"):
            name = option.split("=", 1)[0]
            if len(name) > 2 and any(spelled.startswith(name) for spelled in long):
                return True
        elif short and option.startswith("-") and any(letter in option[1:] for letter in short):
            return True
    return False


def _value_of(options: Sequence[str], names: Sequence[str]) -> list[str]:
    """The values given to these options, `--name=value` or `--name value`."""
    found = []
    for index, option in enumerate(options):
        name, eq, value = option.partition("=")
        if name in names:
            found.append(value if eq else (options[index + 1] if index + 1 < len(options) else ""))
    return found


def _patterns_elsewhere(options: Sequence[str]) -> tuple[bool, list[str]]:
    """For `grep` and `rg`: whether the pattern came through `-e` or `-f` —
    so every operand is a file — and any pattern file written against its
    letter, as `-fFILE` or `-rnfFILE`."""
    given = False
    files: list[str] = []
    for option in options:
        if option.startswith("--"):
            given = given or option.split("=", 1)[0] in ("--regexp", "--file")
        elif option.startswith("-") and ("e" in option[1:] or "f" in option[1:]):
            given = True
            letter = option.find("f", 1)
            if letter > 0 and option[letter + 1 :]:
                files.append(option[letter + 1 :])
    return given, files


# --- the commands -------------------------------------------------------------


def _files(names: str, *, refuse: str = "", refuse_long: Sequence[str] = (), most: int = 0):
    """A reader whose operands are all files or directories."""

    def judge(words: Sequence[_Word], where: _Where) -> None:
        args = _split(words)
        if _has(args.options, refuse, refuse_long):
            raise _NotUnderstoodError(f"`{names}` with an option that writes or runs something")
        if most and len(args.operands) > most:
            raise _NotUnderstoodError(f"`{names}` given a file to write into")
        for option in args.options:
            _, eq, value = option.partition("=")
            if eq and ("/" in value or value.startswith((".", "~"))):
                _check_path(_Word(value, tilde=value.startswith("~")), where)
        for operand in args.operands:
            _check_path(operand, where)

    return judge


def _words_only(words: Sequence[_Word], where: _Where) -> None:
    """A command whose arguments are text, not files."""


def _printf(words: Sequence[_Word], where: _Where) -> None:
    if any(word.text == "-v" for word in words):
        raise _NotUnderstoodError("`printf -v`, which sets a variable")


def _sleep(words: Sequence[_Word], where: _Where) -> None:
    if not all(re.fullmatch(r"\d+(\.\d+)?[smhd]?", word.text) for word in words):
        raise _NotUnderstoodError("`sleep` with something other than a duration")


def _date(words: Sequence[_Word], where: _Where) -> None:
    if _has([w.text for w in words], "sf", ("--set", "--file")):
        raise _NotUnderstoodError("`date` told to set the clock or read a file")


def _command(words: Sequence[_Word], where: _Where) -> None:
    if not words or words[0].text not in ("-v", "-V"):
        raise _NotUnderstoodError("`command`, which runs what follows it")


_SET_OPTIONS = frozenset({"pipefail", "errexit", "nounset", "xtrace", "noglob"})


def _set(words: Sequence[_Word], where: _Where) -> None:
    if not words:
        raise _NotUnderstoodError("`set` alone, which prints every variable")
    for index, word in enumerate(words):
        # `-o` names an option in the next word, also at the end of `-euo`.
        if index and re.fullmatch(r"[-+][eux]*o", words[index - 1].text):
            if word.text not in _SET_OPTIONS:
                raise _NotUnderstoodError(f"`set -o {word.text}`")
        elif not re.fullmatch(r"[-+][euxo]+", word.text):
            raise _NotUnderstoodError(f"`set {word.text}`")


_GREP_TAKING = frozenset({"-e", "-f", "-A", "-B", "-C", "-m", "--regexp", "--file"})


def _grep(words: Sequence[_Word], where: _Where) -> None:
    args = _split(words, taking=_GREP_TAKING)
    flags = [o for o in args.options if o.startswith("-")]
    if _has(flags, "R", ("--dereference-recursive",)):
        raise _NotUnderstoodError("`grep -R`, which follows links out of the project")
    patterned, attached = _patterns_elsewhere(flags)
    for given in [*_value_of(args.options, ("-f", "--file", "--exclude-from")), *attached]:
        _check_path(_Word(given), where)
    for operand in args.operands if patterned else args.operands[1:]:
        _check_path(operand, where)


_RG_TAKING = frozenset({"-e", "-f", "-g", "-t", "-T", "-A", "-B", "-C", "-m", "-M", "-j"})
_RG_REFUSED = (
    "--pre",
    "--pre-glob",
    "--hostname-bin",
    "--search-zip",
    "--follow",
    "--unrestricted",
    "--no-ignore",
    "--no-ignore-dot",
    "--no-ignore-exclude",
    "--no-ignore-files",
    "--no-ignore-global",
    "--no-ignore-parent",
    "--no-ignore-vcs",
)


def _rg(words: Sequence[_Word], where: _Where) -> None:
    args = _split(words, taking=_RG_TAKING)
    flags = [o for o in args.options if o.startswith("-")]
    if _has(flags, "zLu", _RG_REFUSED):
        raise _NotUnderstoodError("`rg` told to run a program, follow links, or read ignored files")
    patterned, attached = _patterns_elsewhere(flags)
    for given in [*_value_of(args.options, ("-f", "--file", "--ignore-file")), *attached]:
        _check_path(_Word(given), where)
    listing = any(o.split("=", 1)[0] in ("--files", "--type-list") for o in flags)
    for operand in args.operands if patterned or listing else args.operands[1:]:
        _check_path(operand, where)


_FIND_REFUSED = frozenset(
    {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
        "-L",
        "-follow",
    }
)
#: Primaries whose value is a pattern or a number, never a file.
_FIND_PATTERNS = frozenset(
    {
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-wholename",
        "-iwholename",
        "-regex",
        "-iregex",
        "-lname",
        "-ilname",
        "-type",
        "-xtype",
        "-maxdepth",
        "-mindepth",
        "-size",
        "-mtime",
        "-mmin",
        "-atime",
        "-amin",
        "-ctime",
        "-cmin",
        "-perm",
        "-user",
        "-group",
        "-uid",
        "-gid",
        "-links",
        "-inum",
        "-printf",
        "-fstype",
        "-used",
        "-D",
        "-O",
    }
)


def _find(words: Sequence[_Word], where: _Where) -> None:
    """Every word that is not an option or a pattern is taken as a path —
    wherever it sits, so a starting point cannot hide behind a leading option."""
    for index, word in enumerate(words):
        if word.text in _FIND_REFUSED:
            raise _NotUnderstoodError(f"`find {word.text}`, which acts on what it finds")
        if word.text.startswith("-") or word.text in ("(", ")", "!", ","):
            continue
        if index and words[index - 1].text in _FIND_PATTERNS:
            continue
        _check_path(word, where)


_SED_SCRIPT = re.compile(
    r"""^\s*(
        (\d+|\$|/[^/\\]*/)(\s*,\s*(\d+|\$|/[^/\\]*/))?\s*[pd=]
      | [pd=]
      | (\d+|\$)?\s*q
      | ((\d+|\$|/[^/\\]*/)(\s*,\s*(\d+|\$|/[^/\\]*/))?\s*)?
        s(?P<d>[/|#,:@])((?!(?P=d)).)*(?P=d)((?!(?P=d)).)*(?P=d)[gpiI0-9]*
    )\s*$""",
    re.VERBOSE,
)


def _sed(words: Sequence[_Word], where: _Where) -> None:
    args = _split(words, taking=frozenset({"-e", "--expression"}))
    flags = [o for o in args.options if o.startswith("-")]
    if _has(flags, "if", ("--in-place", "--file")):
        raise _NotUnderstoodError("`sed` told to write the file back, or to run a script file")
    scripts = _value_of(args.options, ("-e", "--expression"))
    operands = list(args.operands)
    if not scripts:
        if not operands:
            raise _NotUnderstoodError("`sed` with no script")
        scripts = [operands.pop(0).text]
    for script in scripts:
        for piece in re.split(r"[;\n]", script):
            if piece.strip() and not _SED_SCRIPT.match(piece):
                raise _NotUnderstoodError("a `sed` script that does more than print")
    for operand in operands:
        _check_path(operand, where)


def _jq(words: Sequence[_Word], where: _Where) -> None:
    rest = list(words)
    program: str | None = None
    files: list[_Word] = []
    while rest:
        word = rest.pop(0)
        if word.text in ("-f", "--from-file", "-L", "--library-path"):
            raise _NotUnderstoodError("`jq` given its program or its modules from a file")
        if word.text in ("--arg", "--argjson", "--slurpfile", "--rawfile"):
            if len(rest) < 2:
                raise _NotUnderstoodError(f"`jq {word.text}` without its two values")
            value = rest[1]
            del rest[:2]
            if word.text in ("--slurpfile", "--rawfile"):
                _check_path(value, where)
        elif word.text == "--indent":
            del rest[:1]
        elif word.text.startswith("-") and word.text != "-":
            continue
        elif program is None:
            program = word.text
        else:
            files.append(word)
    if program is not None and re.search(r"\benv\b|\$ENV", program):
        raise _NotUnderstoodError("a `jq` program that reads the environment")
    for file in files:
        _check_path(file, where)


_GIT_GLOBAL_OK = frozenset({"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs"})
_GIT_READS = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "shortlog",
        "rev-list",
        "blame",
        "grep",
        "ls-files",
        "ls-tree",
        "cat-file",
        "rev-parse",
        "merge-base",
        "describe",
        "name-rev",
        "count-objects",
        "for-each-ref",
        "show-ref",
        "check-ignore",
        "check-attr",
        "whatchanged",
    }
)
_GIT_REFUSED = ("--output", "--no-index", "--contents", "--open-files-in-pager", "--ext-diff")
_GIT_LISTING = {
    "branch": (
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-v",
        "-vv",
        "--verbose",
        "--show-current",
        "--no-color",
        "--color",
        "--column",
        "--no-column",
        "-q",
        "--quiet",
    ),
    "tag": ("--column", "--no-column", "--color"),
}
_GIT_LIST_VALUES = (
    "--contains",
    "--no-contains",
    "--merged",
    "--no-merged",
    "--points-at",
    "--sort",
    "--format",
)


def _git(words: Sequence[_Word], where: _Where) -> None:
    rest = list(words)
    starts = where.cwds
    while rest and rest[0].text.startswith("-"):
        flag = rest.pop(0).text
        if flag == "-C" and rest:
            directory = rest.pop(0)
            _check_path(directory, where)
            starts = tuple(where.resolve(directory))
            continue
        if flag not in _GIT_GLOBAL_OK:
            raise _NotUnderstoodError(f"`git {flag}`, which changes what git runs or where")
    # Git reads the whole repository it finds, not the project. One found
    # above the project — a home directory kept in git — reaches outside it.
    for start in starts:
        found = _repository(start)
        if found is not None and not where.inside(found):
            raise _NotUnderstoodError("the repository reaches outside the project")
    if not rest:
        raise _NotUnderstoodError("`git` with nothing to do")
    sub, texts = rest[0].text, [word.text for word in rest[1:]]
    if sub in _GIT_READS:
        if _has(texts, "", _GIT_REFUSED):
            raise _NotUnderstoodError(f"`git {sub}` told to write, run or read outside the tree")
        if sub == "grep" and _has(texts, "O"):
            raise _NotUnderstoodError("`git grep -O`, which runs a pager")
        return
    if sub in _GIT_LISTING:
        _git_listing(sub, texts)
        return
    # Subcommands that read only in some forms: the first word after them says
    # which, and for `remote` and `reflog` no word at all is a listing too.
    reading = {
        "stash": ("list", "show"),
        "remote": ("", "-v", "--verbose", "get-url"),
        "reflog": ("", "show"),
        "worktree": ("list",),
    }
    if (texts[0] if texts else "") in reading.get(sub, ()):
        return
    raise _NotUnderstoodError(f"`git {sub}`, which is not a read")


def _git_listing(sub: str, texts: Sequence[str]) -> None:
    """`git branch` and `git tag` list unless given a name, which makes one."""
    listing = any(t in ("-l", "--list") for t in texts)
    for index, text in enumerate(texts):
        if text in ("-l", "--list"):
            continue
        if text.startswith("-"):
            name = text.split("=", 1)[0]
            if name in _GIT_LIST_VALUES or name in _GIT_LISTING[sub]:
                continue
            if sub == "tag" and re.fullmatch(r"-n\d*", text):
                continue
            raise _NotUnderstoodError(f"`git {sub} {text}`, which changes a {sub}")
        before = texts[index - 1] if index else ""
        if listing or before in _GIT_LIST_VALUES:
            continue
        raise _NotUnderstoodError(f"`git {sub} {text}`, which makes a {sub}")


#: Every command a read may be made of, and how its arguments are judged.
_COMMANDS: dict[str, Callable[[Sequence[_Word], _Where], None]] = {
    **{
        name: _files(name)
        for name in (
            "cat",
            "head",
            "tail",
            "nl",
            "wc",
            "stat",
            "ls",
            "du",
            "df",
            "cmp",
            "diff",
            "comm",
            "cut",
            "column",
            "realpath",
            "readlink",
            "md5",
            "md5sum",
            "shasum",
            "sha256sum",
            "hexdump",
            "od",
            "strings",
        )
    },
    "file": _files("file", refuse="C", refuse_long=("--compile",)),
    "tree": _files("tree", refuse="o", refuse_long=("--output",)),
    "sort": _files(
        "sort",
        refuse="oT",
        refuse_long=("--output", "--compress-program", "--temporary-directory"),
    ),
    "uniq": _files("uniq", most=1),
    "xxd": _files("xxd", most=1),
    "grep": _grep,
    "egrep": _grep,
    "fgrep": _grep,
    "rg": _rg,
    "find": _find,
    "sed": _sed,
    "jq": _jq,
    "git": _git,
    "printf": _printf,
    "sleep": _sleep,
    "date": _date,
    "command": _command,
    "set": _set,
    **{
        name: _words_only
        for name in (
            "echo",
            "true",
            ":",
            "pwd",
            "whoami",
            "uname",
            "ps",
            "which",
            "type",
            "basename",
            "dirname",
            "tr",
            "test",
            "[",
        )
    },
}

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# --- a project's own commands --------------------------------------------------
#
# A test run is not a read: it runs the project's code, and a test the agent
# just wrote runs with it. So it is never let through as one. A project may
# name the commands it trusts that way under `runs:` — its test and lint
# targets — and those go through too, judged part by part like everything
# else: `make test-fast 2>&1 | tail -20` is a run and a read.


@dataclass(frozen=True)
class Run:
    """One entry under a project's `runs:`, as its words.

    Written the way an agent types the command: `make test-fast`, or
    `uv run pytest *` where a last `*` takes any further arguments — each of
    them an option or a path inside the project. A `*` anywhere else is one
    word, and a word with `*` or `?` in it one word matching it. `NAME=*` at the
    front says that setting may be given; the command matches without it too.
    """

    text: str
    words: tuple[str, ...]
    environment: frozenset[str]
    #: A last `*`: further arguments allowed.
    open: bool


#: Commands that run another command, whatever is named after them. An entry
#: starting with one would let anything through under a respectable name.
RUNS_ANYTHING = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "xargs",
        "eval",
        "exec",
        "command",
        "builtin",
        "time",
        "nice",
        "nohup",
        "timeout",
        "watch",
        "bash",
        "sh",
        "zsh",
        "dash",
        "fish",
        "ksh",
        "ssh",
        "scp",
        "docker",
        "podman",
        "kubectl",
    }
)

#: Interpreters, which run code given them as an argument.
INTERPRETERS = frozenset(
    {"python", "python3", "node", "ruby", "perl", "php", "deno", "bun", "lua", "Rscript"}
)

#: What hands an interpreter its code on the command line.
_CODE_FLAGS = frozenset({"*", "-c", "-e", "--eval", "-p", "--print", "-"})

#: Words after which the next named word is what runs: `npx vitest`, `make test`.
_RUNS_NEXT = frozenset({"npx", "uvx", "bunx", "make"})

#: The same, after a package manager: `uv run pytest`, `pnpm exec vitest`.
#: Only there — `vitest run` is vitest's own word.
_PACKAGE_MANAGERS = frozenset(
    {"uv", "npm", "pnpm", "yarn", "poetry", "pipenv", "pipx", "bundle", "cargo", "go", "bun"}
)


def _wild(word: str) -> bool:
    return any(c in word for c in "*?[")


def _runner_at(words: Sequence[str], index: int) -> bool:
    word = words[index]
    if word in _RUNS_NEXT:
        return True
    return word in ("run", "exec", "dlx") and index > 0 and words[index - 1] in _PACKAGE_MANAGERS


def _program_after(words: Sequence[str], index: int) -> str:
    """The word that names what runs after a runner, or nothing. A pattern
    right after an option is that option's value — `--package *` — not it."""
    previous = ""
    for word in words[index + 1 :]:
        if word.startswith("-"):
            previous = word
            continue
        if previous and _wild(word):
            previous = ""
            continue
        return word
    return ""


def run_entry(text: str) -> Run:
    """An entry under `runs:`, or `ValueError` saying why it cannot be one.

    Refused when it could run anything: a shell, a wrapper or a remote runner
    first (`bash`, `sudo`, `env`, `ssh`, `docker`), an interpreter handed code
    (`python -c *`), or a runner with no named program after it (`uv run *`,
    `make *`). Everything else is the project's to decide.
    """
    try:
        parts = _parse(text)
    except _NotUnderstoodError as why:
        raise ValueError(f"`{text}` is not a plain command: {why}") from None
    if len(parts) != 1 or parts[0].inputs or not parts[0].words:
        raise ValueError(f"`{text}` must be one command, with no pipes, separators or redirects")
    words = [word.text for word in parts[0].words]
    environment = set()
    while words and _ASSIGNMENT.match(words[0]):
        environment.add(words.pop(0).split("=", 1)[0])
    if not words:
        raise ValueError(f"`{text}` names no command")
    first = words[0]
    if _wild(first) or os.path.basename(first) in RUNS_ANYTHING:
        raise ValueError(f"`{text}` could run anything: `{first}` runs whatever follows it")
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else ""
        called = os.path.basename(word)
        if (called in INTERPRETERS or called.startswith("python3.")) and (
            following in _CODE_FLAGS or not following
        ):
            raise ValueError(f"`{text}` could run anything: `{word}` given its code")
        if _runner_at(words, index):
            program = _program_after(words, index)
            if not program or _wild(program):
                raise ValueError(f"`{text}` could run anything: nothing named after `{word}`")
    opened = words[-1] == "*"
    return Run(
        text=text,
        words=tuple(words[:-1] if opened else words),
        environment=frozenset(environment),
        open=opened,
    )


def _matches(run: Run, environment: set[str], words: Sequence[_Word], where: _Where) -> bool:
    """Whether a part is this entry: its settings allowed, its words in place,
    and anything past them — for an entry that takes more — inside the project."""
    if not environment <= (run.environment | SAFE_ENVIRONMENT):
        return False
    if len(words) < len(run.words) or (not run.open and len(words) != len(run.words)):
        return False
    for pattern, word in zip(run.words, words, strict=False):
        if any(c in pattern for c in "*?["):
            if not fnmatch.fnmatchcase(word.text, pattern):
                return False
            _check_path(word, where)
        elif word.text != pattern or word.globbed:
            return False
    _files("the rest")(words[len(run.words) :], where)
    return True


# --- judging -------------------------------------------------------------------


def judge(
    command: str,
    *,
    cwd: str | None,
    project: str | None,
    runs: Sequence[Run] = (),
) -> Verdict:
    """Whether this command is understood whole, inside `project`: reads, and
    the commands the project named under `runs:`.

    `cwd` is where it runs; without one it runs at the project's root. Without
    a project there is nothing to measure a path against, and nothing is let
    through.
    """
    if not project or not command or not command.strip():
        return Verdict(False, "no project to measure it against")
    root = os.path.realpath(os.path.expanduser(project))
    here = os.path.realpath(os.path.expanduser(cwd)) if cwd else root
    where = _Where(root, (here,))
    if not where.inside(here):
        return Verdict(False, "it runs outside the project")
    names: list[str] = []
    ran: list[str] = []
    try:
        for part in _parse(command):
            where = _judge_part(part, where, names, runs, ran)
    except _NotUnderstoodError as why:
        return Verdict(False, str(why))
    if not names:
        return Verdict(False, "nothing to run")
    listed = ", ".join(dict.fromkeys(names))
    if ran:
        return Verdict(True, f"the project's own `runs:` and reads inside it: {listed}")
    return Verdict(True, f"a read inside the project: {listed}")


def _judge_part(
    part: _Part, where: _Where, names: list[str], runs: Sequence[Run], ran: list[str]
) -> _Where:
    """Judge one simple command, and say where the next one may be running."""
    for given in part.inputs:
        _check_path(given, where)
    words = list(part.words)
    environment: set[str] = set()
    while words and _ASSIGNMENT.match(words[0].text):
        environment.add(words.pop(0).text.split("=", 1)[0])
    if not words:
        if environment - SAFE_ENVIRONMENT:
            raise _NotUnderstoodError("a setting made for what runs after it")
        return where
    # The project's own, first: it is what somebody wrote down for exactly
    # this, and it may name a command no read covers — `git fetch`.
    for run in runs:
        try:
            if _matches(run, environment, words, where):
                names.append(run.text)
                ran.append(run.text)
                return where
        except _NotUnderstoodError:
            continue
    unsafe = sorted(environment - SAFE_ENVIRONMENT)
    if unsafe:
        raise _NotUnderstoodError(f"`{unsafe[0]}=` set for it, which can change what runs")
    name = words[0].text
    if (words[0].globbed and name != "[") or words[0].tilde or "/" in name:
        raise _NotUnderstoodError(f"`{name}`, a program named by its path or a pattern")
    if name == "cd":
        if len(words) != 2 or words[1].text == "-":
            raise _NotUnderstoodError("a `cd` that does not name one directory")
        _check_path(words[1], where)
        names.append("cd")
        moved = where.resolve(words[1])
        return _Where(where.project, tuple(dict.fromkeys((*where.cwds, *moved))))
    check = _COMMANDS.get(name)
    if check is None:
        raise _NotUnderstoodError(f"`{name}`, which is not a read this knows")
    check(words[1:], where)
    names.append(f"git {words[1].text}" if name == "git" and len(words) > 1 else name)
    return where
