"""Exercise the bundled search bodies without importing optional providers.

The imports register every musicdl provider. These tests isolate the actual
search methods and replace their HTTP boundary so no account or network is used.
"""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]


def search_method(provider):
    path = ROOT / "musicdl" / "modules" / "audiobooks" / f"{provider}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_search")
    method.decorator_list = []
    module = ast.Module(body=[method], type_ignores=[])
    namespace = {
        "parse_qs": parse_qs,
        "urlparse": urlparse,
        "Progress": object,
        "resp2json": lambda response: {"results": []},
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_search"]


class AudiobookSearchTests(unittest.TestCase):
    def test_search_type_is_initialized_before_progress_and_parser_selection(self):
        for provider, key, album, track in (
            ("qingting", "include", "channel_ondemand", "program_ondemand"),
            ("ximalaya", "core", "album", "track"),
        ):
            for kind, expected in ((album, "album"), (track, "track")):
                with self.subTest(provider=provider, kind=kind):
                    parsed = []
                    labels = []
                    response = SimpleNamespace(raise_for_status=lambda: None)
                    client = SimpleNamespace(
                        source=provider,
                        get=lambda *args, **kwargs: response,
                        _parsebyalbum=lambda *args, **kwargs: parsed.append("album"),
                        _parsebytrack=lambda *args, **kwargs: parsed.append("track"),
                        disable_print=True,
                        logger_handle=SimpleNamespace(error=lambda *args, **kwargs: self.fail(str(args))),
                    )
                    progress = SimpleNamespace(
                        add_task=lambda label, **kwargs: labels.append(label) or 1,
                        update=lambda *args, **kwargs: None,
                    )
                    result = search_method(provider)(
                        client, search_url=f"https://example.invalid/search?page=1&{key}={kind}",
                        song_infos=[], progress=progress,
                    )
                    self.assertEqual(parsed, [expected])
                    self.assertIn(f".{kind}._search", labels[0])
                    self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
