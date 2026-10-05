import hashlib
import hmac
import json
import logging
import sys
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)


STORE_IMAGE_URL = (
    "https://commerce1.api.np.km.playstation.net/store/api/ps4/"
    "00_09_000/container/US/en/19/{content_id}/image"
)

TMDB_KEY = bytes.fromhex(
    "F5DE66D2680E255B2DF79E74F890EBF349262F618BCAE2A9ACCDEE5156CE8DF2"
    "CDF2D48C71173CDC2594465B87405D197CF1AED3B7E9671EEB56CA6753C2E6B0"
)
TMDB_URL = "https://tmdb.np.dl.playstation.net/tmdb2/{signed_id}_{signature}/{signed_id}.json"


@dataclass
class TitleInfo:
    name: str
    image_url: str | None


def title_from_param(param: dict) -> TitleInfo | None:
    if not isinstance(param, dict):
        return None

    localized = param.get("localizedParameters", {})
    language = localized.get("defaultLanguage")
    name = localized.get(language, {}).get("titleName")
    if not name:
        return None

    content_id = param.get("contentId")
    image_url = STORE_IMAGE_URL.format(content_id=content_id) if content_id else None

    return TitleInfo(name=name, image_url=image_url)


def tmdb_url(title_id: str) -> str:
    signed_id = f"{title_id}_00"
    signature = hmac.new(TMDB_KEY, signed_id.encode(), hashlib.sha1).hexdigest().upper()
    return TMDB_URL.format(signed_id=signed_id, signature=signature)


def lookup_ps4_title(title_id: str) -> TitleInfo | None:
    url = tmdb_url(title_id)
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.load(response)

        name = data["names"][0]["name"]

        image_url = None
        icons = data.get("icons") or []
        if icons and icons[0].get("icon"):
            image_url = icons[0]["icon"].replace("http://", "https://", 1)

    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        log.debug("metadata lookup failed for %s: %r", title_id, e)
        return None

    if not name:
        return None
    return TitleInfo(name=name, image_url=image_url)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_id = sys.argv[1] if len(sys.argv) > 1 else "CUSA00900"
    print(tmdb_url(test_id))
    print(lookup_ps4_title(test_id))