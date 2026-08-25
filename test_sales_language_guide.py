def test_short_social_replies_are_deterministic():
    import sales_language_guide as language

    assert language.social_reply("يسلموا") == "العفو، أهلاً بكِ."
    assert language.social_reply("شكراً") == "العفو، أهلاً بكِ."
    assert language.social_reply("يعطيكي العافية") == "الله يعافيكِ، أهلاً بكِ."


def test_sanitize_removes_titles_and_canned_boilerplate():
    import sales_language_guide as language

    reply = language.sanitize_response(
        "الله يسلمك أستاذ ويحفظك. يسعدنا دائماً تواصلك معنا وإن شاء الله أهلاً وسهلاً بك."
    )
    assert "أستاذ" not in reply
    assert "يسعدنا دائماً تواصلك معنا" not in reply
    assert "إن شاء الله" not in reply
    assert "الله يسلمك" in reply


def test_sanitize_removes_intimate_phrases():
    import sales_language_guide as language

    reply = language.sanitize_response("عيونك يا قلبي وكرمالك يا قمر")
    assert "عيونك" not in reply
    assert "يا قلبي" not in reply
    assert "كرمالك" not in reply
    assert "يا قمر" not in reply


def test_syrian_language_preferences_are_present():
    import sales_language_guide as language

    guide = language.preferred_examples_prompt()
    for term in ("شو", "هيك", "لانو", "هلق", "هون", "كمان", "بدي", "بدك", "فيكي", "رح"):
        assert term in guide
