"""Isolated upstream API diagnostic. A fault must not kill the qualification controller."""

import resource

import tree_sitter_python
from tree_sitter import Language, Parser

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
parser = Parser(Language(tree_sitter_python.language()))
source = b"x=1\n" * 10000


def progress(offset, has_error):
    return True


try:
    parser.parse(
        lambda offset, point: source[offset : offset + 256], progress_callback=progress
    )
except ValueError:
    print("cancelled")
else:
    raise AssertionError("progress callback did not cancel")
parser.reset()
assert not parser.parse(b"x=1\n").root_node.has_error
