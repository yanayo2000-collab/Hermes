from app.native_ocr import infer_screen_type, normalize_native_ocr_fields, parse_native_ocr_text


def test_parse_agency_info_card_text_extracts_sid_person_code_and_agency():
    text = """Gabung Agensi\nSID Saya 45691735\nkode gabung agensi EKVFGQ\nAgensi saya Permata"""
    parsed = parse_native_ocr_text(text)

    assert parsed.screen_type == "agency_info_card"
    assert parsed.sid == "45691735"
    assert parsed.person_code == "EKVFGQ"
    assert parsed.guild_invite_code is None
    assert parsed.invite_code == "EKVFGQ"  # backward-compatible alias
    assert parsed.agency_name == "Permata"
    assert parsed.account_id == "45691735"


def test_parse_agency_info_card_text_extracts_multiline_person_code():
    text = """SID Saya\n45678991\nkode gabung\nagensi\nMPLX5H\nAgensi saya\nPisoID"""
    parsed = parse_native_ocr_text(text)

    assert parsed.screen_type == "agency_info_card"
    assert parsed.person_code == "MPLX5H"
    assert parsed.guild_invite_code is None
    assert parsed.agency_name == "PisoID"


def test_parse_invite_success_toast_extracts_sid_guild_and_guild_invite_code():
    text = """Isi kode undangan\nKode Undangan KK9J8D\nSelamat datang! Kamu berhasil bergabung!\nSID kamu: 45689309, Nama Guild: Permata"""
    parsed = parse_native_ocr_text(text)

    assert parsed.screen_type == "invite_success_toast"
    assert parsed.sid == "45689309"
    assert parsed.guild_name == "Permata"
    assert parsed.guild_invite_code == "KK9J8D"
    assert parsed.person_code is None
    assert parsed.invite_code == "KK9J8D"  # backward-compatible alias


def test_parse_profile_page_extracts_profile_id_and_username():
    text = """Vitoria Amaral\nID: 51302504\n0 Amigos 0 Fãs 0 Seguir"""
    parsed = parse_native_ocr_text(text)

    assert parsed.screen_type == "profile_page"
    assert parsed.profile_id == "51302504"
    assert parsed.sid == "51302504"
    assert parsed.username == "Vitoria Amaral"
    assert parsed.account_id == "51302504"


def test_parse_portuguese_agency_page_extracts_sid_person_code_and_agency():
    text = """Entrar na Agencia\nMeu SID\n51296856\nCodigo da pessoa\nQUS8C7\nMinha Agencia\nWhisky"""
    parsed = parse_native_ocr_text(text)

    assert parsed.screen_type == "agency_info_card"
    assert parsed.sid == "51296856"
    assert parsed.person_code == "QUS8C7"
    assert parsed.guild_invite_code is None
    assert parsed.agency_name == "Whisky"
    assert parsed.username is None


def test_parse_portuguese_agency_page_extracts_sid_person_code_and_emoji_agency():
    text = """Entrar na Agência\nMeu SID 51293720\nCódigo da pessoa 5DNC7D\nMinha Agência Whisky🍸"""
    parsed = parse_native_ocr_text(text)
    normalized = normalize_native_ocr_fields(text)

    assert parsed.sid == "51293720"
    assert parsed.person_code == "5DNC7D"
    assert parsed.guild_invite_code is None
    assert normalized["agency_name"] == "Whisky🍸"
    assert normalized["registration_group"] is None
    assert normalized["agency_joined"] is True


def test_parse_native_ocr_text_tolerates_split_spacing_in_labeled_agency_text():
    text = "SID Saya 45691735\nAge nsi saya Pe rmata-7"
    parsed = parse_native_ocr_text(text)
    normalized = normalize_native_ocr_fields(text)

    assert parsed.sid == "45691735"
    assert parsed.agency_name == "Permata-7"
    assert normalized["agency_name"] == "Permata-7"
    assert normalized["registration_group"] is None


def test_normalize_native_ocr_fields_keeps_labeled_guild_as_guild_not_registration_group():
    text = "SID kamu: 45689309, Nama Guild: Permata"
    normalized = normalize_native_ocr_fields(text)

    assert normalized["account_id"] == "45689309"
    assert normalized["guild_name"] == "Permata"
    assert normalized["registration_group"] is None
    assert normalized["evidence"]["matched_guild_name"] is True


def test_normalize_native_ocr_fields_marks_portuguese_not_joined_agency_state():
    text = "Minha Agencia\nAinda não entrou"
    normalized = normalize_native_ocr_fields(text)

    assert normalized["agency_name"] is None
    assert normalized["registration_group"] is None
    assert normalized["agency_joined"] is False
    assert normalized["evidence"]["matched_unjoined_agency_state"] is True


def test_parse_joined_agency_page_accepts_emoji_agency_as_agency_name():
    text = """Meu SID 51296856\nCódigo da pessoa QUS8C7\nMinha Agência Whisky🍸\nMeu líder da agência aaa"""
    normalized = normalize_native_ocr_fields(text)

    assert normalized["account_id"] == "51296856"
    assert normalized["person_code"] == "QUS8C7"
    assert normalized["guild_invite_code"] is None
    assert normalized["invite_code"] == "QUS8C7"
    assert normalized["agency_name"] == "Whisky🍸"
    assert normalized["agency_joined"] is True


def test_normalize_native_ocr_fields_marks_portuguese_not_joined_agency_as_unjoined():
    text = """Meu SID 51297356\nMinha Agência Ainda não entrou"""
    normalized = normalize_native_ocr_fields(text)

    assert normalized["account_id"] == "51297356"
    assert normalized["agency_name"] is None
    assert normalized["registration_group"] is None
    assert normalized["agency_joined"] is False
    assert normalized["evidence"]["matched_unjoined_agency_state"] is True


def test_normalize_native_ocr_fields_exposes_distinct_code_evidence():
    text = """Isi kode undangan\nKode Undangan 6RHHG6\nSID kamu: 31988412, Nama Guild: Permata"""
    normalized = normalize_native_ocr_fields(text)

    assert normalized["account_id"] == "31988412"
    assert normalized["guild_invite_code"] == "6RHHG6"
    assert normalized["person_code"] is None
    assert normalized["invite_code"] == "6RHHG6"
    assert normalized["guild_name"] == "Permata"
    assert normalized["agency_joined"] is True
    assert normalized["evidence"]["matched_guild_invite_code"] is True
    assert normalized["evidence"]["matched_person_code"] is False


def test_infer_screen_type_falls_back_to_unknown():
    assert infer_screen_type("random text only") == "unknown"
