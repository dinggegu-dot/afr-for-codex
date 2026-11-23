"""保留已签发文章的路径身份，同时解析正式归档位置。"""
import json
import os
import re
import stat
import unicodedata

from .filing import DOMAIN_TERMS, PENDING_DOMAIN


INBOX_PREFIX = "资料库/新收研报/"
MANIFEST = ".afr/article-locations.json"
_DOMAINS = {*DOMAIN_TERMS, PENDING_DOMAIN}
_LIMIT = 1024 * 1024
_HASH_SUFFIX = re.compile(r".+ · ([0-9a-f]{16})$")
_INVALID_NAME = re.compile(r"[\x00-\x1f\x7f\\\[\]#^|:*?\"<>]")
_WIKI_LINK = re.compile(r"(?<!\\)\[\[([^\[\]\r\n|#]+)(?=(?:[#|][^\[\]\r\n]*)?\]\])")


def _unique_object(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("duplicate_location_key")
    return values


def _folder(relative):
    if not isinstance(relative, str):
        return None
    parts = relative.split("/")
    if len(parts) != 3 or parts[0] != "资料库" or _INVALID_NAME.search(parts[2]):
        return None
    match = _HASH_SUFFIX.fullmatch(parts[2])
    if match is None or parts[2].startswith("."):
        return None
    return parts[1], match[1]


def load(vault):
    """仅接受同一固定原件从旧投放目录移到既有领域的映射。"""
    from .library import RequestError

    path = vault / MANIFEST
    if path.parent.is_symlink():
        raise RequestError("invalid_article_locations")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT:
                raise ValueError("invalid_location_file")
            data = stream.read(_LIMIT + 1)
            if len(data) != info.st_size or os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
                raise ValueError("invalid_location_file")
            manifest = json.loads(data, object_pairs_hook=_unique_object)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise RequestError("invalid_article_locations") from None
    if (not isinstance(manifest, dict) or set(manifest) != {"version", "folders"}
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or not isinstance(manifest["folders"], dict)):
        raise RequestError("invalid_article_locations")

    mapping = manifest["folders"]
    destinations = set()
    for source, target in mapping.items():
        old, new = _folder(source), _folder(target)
        if (old is None or new is None or old[0] != "新收研报" or new[0] not in _DOMAINS
                or old[1] != new[1]):
            raise RequestError("invalid_article_locations")
        destination = unicodedata.normalize("NFC", target).casefold()
        if destination in destinations:
            raise RequestError("invalid_article_locations")
        destinations.add(destination)
    return mapping


def _resolve(relative, paths):
    path, anchor, fragment = relative.partition("#")
    *folder, remainder = path.split("/", 3)
    if len(folder) != 3:
        return paths.get(path, path) + anchor + fragment
    parent = "/".join(folder)
    return paths.get(parent, parent) + "/" + remainder + anchor + fragment


def resolve(relative, mapping, *, reverse=False):
    if not mapping:
        return relative
    paths = {destination: source for source, destination in mapping.items()} if reverse else mapping
    return _resolve(relative, paths)


def rewrite(text, mapping, *, reverse=False):
    """只重写 Obsidian wikilink 的目录前缀，原句、标题、锚点与别名不变。"""
    if not mapping or "[[资料库/" not in text:
        return text
    paths = {destination: source for source, destination in mapping.items()} if reverse else mapping
    return _WIKI_LINK.sub(lambda match: "[[" + _resolve(match[1], paths), text)
