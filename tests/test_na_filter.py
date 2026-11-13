import pytest

from watcher.na_filter import classify

SKIP = [
    # NA in every case variant
    "NA", "na", "Na", "nA",
    "NA all", "nA all", "NA 1 H1b All", "Na h1 1+1 h4 all", "NA 4 h1b all", "NA H4 All",
    "N/A all locations", "N.A.", "NA all 1+1", "NA all ofc", "na for all 2 h1b",
    "NA HYD 1 h1b", "NA chennai 2 applicants", "NA.",
    # ambiguous NA posts are still skipped
    "Na 2 Hyd Chennai", "NA HYD 20", "Is it always NA?",
    "NA " + "x" * 100,
    # NA not at the start (real posts from the groups)
    "H1 na all", "H1b 1 na all", "H1b NA", "H4 NA All", "H1 + 2 H4 NA all", "H1NALL",
    "H1B all na", "H4NA All", "H1 H4 NA ALL", "Na1 all", "NA1 H1B all", "H1bNA all",
    "H1 and h4 NA all location",
    # no content
    "", "   ", "👍", "🙏🙏",
]

KEEP = [
    # NA but clearly reports availability
    "NA Delhi but CHN OFC Nov 20 available",
    "NA but hyd open",
    "NA mumbai, got chennai 20th",
    "NA 20/11 in chennai?",
    # no NA at all -> AI decides
    "CHN OFC Nov 20 available",
    "HYD CA Dec 3",
    "Chennai slots released",
    "Chennai Nov 13 9 available",
    "H1B slots showing for OFC - October 28 2026, HYD location - two time slots",
    "Saw hyd H1 appointment few mins ago but now it blocked my access",
    "All gone",
    "anyone got slots?",
    "bulk slots in kolkata",
    # words containing "na" are not NA
    "Nagpur?", "Nah nothing", "national holiday?", "Chennai", "banana",
]


@pytest.mark.parametrize("text", SKIP)
def test_skip(text):
    r = classify(text)
    assert r.skip, f"should skip {text!r} ({r.reason})"


@pytest.mark.parametrize("text", KEEP)
def test_keep(text):
    r = classify(text)
    assert not r.skip, f"should keep {text!r} ({r.reason})"


def test_image_always_kept():
    assert not classify("NA", has_image=True).skip
    assert not classify(None, has_image=True).skip


def test_service_and_sticker_skipped():
    assert classify("x joined the group", is_service=True).skip
    assert classify(None, is_sticker=True).skip
